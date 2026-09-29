"""整合測試：一輪 committer → `agora checkout` → `agora-opencode load`。

驗證的是 I1 真正要成立的那件事：**新 session 送給模型的開頭與原 session
位元組相同**（ADR 0010 的 KV cache 要求）。

真的 Drive、真的 git-annex、真的 pin repo。收尾由 `sandbox` 清掉（釘選值條目
也一併刪掉——pin repo 是所有線共用的，留下會變成垃圾）。

這一輪也驗了「原始紀錄是怎麼讀到的」：讀取視圖**不**發佈 raw 的位元組，
`checkout` 問讀取介面要該快照的 **annex key**，自己去 Agora 真本前綴取回物件，
再用 key 內嵌的 sha256 與 size 驗證（見 `agora_cli/objects.py`）。

**只用測試資源**：Session 的原始紀錄是單元測試的 opencode 黃金樣本
（`tests/unit/data/converters/opencode/basic.json`，形狀真實、內容是測試資料），
不碰任何真實 Session。

關於「位元組相同」的驗證方式：spike（`docs/spike/session-import.md`）是用本機
stub provider 抓 opencode 送模型的 request body 來比。那需要真的 opencode
行程與一次模型往返，整合測試裡不適合（要憑證、慢、會漂）。這裡改驗同一個量
的**決定性來源**：`agora checkout` 交出的原始紀錄位元組 ＋ 轉接器組出的匯出
重播序列。spike Q2／Q3 已證明只要這兩者位元組相同，opencode import 出去之後
送模型的開頭就位元組相同（歷史重播保留原 `tool_call id` 與工具結果）。
環境有 opencode 時，另外真的跑一次 `opencode import` 確認它收。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from aistorage.agora_cli.claims import ClaimJournal
from aistorage.agora_cli.checkout import CheckoutDeps, checkout
from aistorage.agora_cli.objects import ObjectFetcher
from aistorage.agora_cli.package import read_package
from aistorage.clock import SystemClock
from aistorage.committer.publish import NullPublisher
from aistorage.converters import get_converter
from aistorage.inbox_builder import upload_item
from aistorage.integrity.pin import GitPinStore
from aistorage.publish.publisher import DriveReadViewPublisher
from aistorage.reader import AgoraReader
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.readview.model import initial_manifest, serialize_manifest
from aistorage.syncer.core import Signer

from ._harness import (
    RAW_S1,
    S1,
    _put_session,
    annex_git_factory,
    build_annex_repo,
    make_signer,
    write_registry,
)

pytestmark = [pytest.mark.integration]

AGORA_SESSION = f"opencode:{S1}"


# ---------------------------------------------------------------------------
# 「送給模型的開頭」：spike 抓到的那個 request body 的決定性來源
# ---------------------------------------------------------------------------


#: spike 的比對工具（重播匯出檔 → 送給模型的 messages 位元組）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "spike"))
from session_import_compare import export_prefix_bytes as wire_prefix  # noqa: E402


def _raw_export(limit: int | None = None) -> dict:
    """黃金樣本（`_harness.RAW_S1` 就是它改過 id 的位元組）截到第 `limit` 則。

    內容是測試資料、形狀真實——`_harness` 的共用樣本，`test_committer_round_smoke`
    用的是同一份。
    """
    data = json.loads(RAW_S1.decode("utf-8"))
    if limit is not None:
        data["messages"] = data["messages"][:limit]
    return data


def _continuation_point(tmp_path: Path) -> tuple[int, str]:
    """接續點的（則數, message id）。

    真的跑一次轉換器 + 接續點計算，**不重新實作那個判定**——自己寫一份就會和
    寫交接單的那一邊漂移（`syncer/continuation.py` 的模組說明）。
    """
    from aistorage.converters import get_converter
    from aistorage.syncer.continuation import continuation_point

    raw_path = tmp_path / "raw-s1.json"
    raw_path.write_bytes(RAW_S1)
    point = continuation_point(
        get_converter("opencode"), raw_path, session_id=AGORA_SESSION,
        snapshot_sha256=hashlib.sha256(RAW_S1).hexdigest().lower())
    assert point is not None, "黃金樣本應該有可用的接續點"
    for index, message in enumerate(_raw_export()["messages"]):
        if message["info"]["id"] == point.message_id:
            return index + 1, point.message_id
    raise AssertionError(f"接續點訊息不在黃金樣本裡: {point.message_id}")


def _continuable_limit(tmp_path: Path) -> int:
    """到「最後一則已完成、未撤銷的訊息」為止的則數（接續點的定義）。"""
    return _continuation_point(tmp_path)[0]


# ---------------------------------------------------------------------------
# 共用準備：真 Drive、真 git-annex、真 pin repo、真讀取視圖
# ---------------------------------------------------------------------------


class _Bootstrap:
    """一條線起好的環境：pin repo、讀取視圖、收件匣、提交流程設定、讀取端與 S1。"""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _bootstrap(it_settings, real_drive, sandbox, tmp_path) -> _Bootstrap:
    """起好一條線，跑一輪提交流程把 S1 收進 Agora 並發佈到讀取視圖。"""
    from aistorage.committer.config import CommitterConfig
    from aistorage.committer.run import Deps, init_pin_cli, run
    from aistorage.identity import load_registry

    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    harness_signer = make_signer(tmp_path)
    write_registry(harness_signer, inbox_id)

    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=20,
        annex_object_sizes=(300,),
    )
    pins = GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]),
        workdir=tmp_path / "pin",
        key_path=it_settings["pin_key"],
        known_hosts_path=it_settings["known_hosts"],
    )
    git_factory = annex_git_factory(annex)
    sandbox.register_pin_store(pins)

    # 讀取視圖：真的資料夾 ＋ 真的初始 manifest（提交流程要 publish 到它）
    readview_id = sandbox.create_folder(f"{prefix_name}-readview")
    manifest_id = real_drive.create(
        readview_id, "readview-manifest.json", serialize_manifest(initial_manifest(
            agora_main_sha="unborn", published_at="2026-09-27T08:00:00Z")),
        mime_type="application/json").id

    cfg = CommitterConfig(
        repo=sandbox.pin_repo_name(),   # 唯一名稱（pin repo 是所有線共用的）
        repo_uuid=annex.uuid,
        repo_url=annex.url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        identity_registry_path=str(harness_signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=20,
    )
    state = init_pin_cli(cfg, Deps(
        drive=real_drive, pins=pins, git_factory=git_factory, registry=None,
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock()), confirm=True)
    assert state.repo_uuid == annex.uuid

    # 一個 session 進收件匣
    _put_session(real_drive, inbox_id, harness_signer, RAW_S1, S1,
                 "2026-09-27T08:00:00Z")

    deps = Deps(
        drive=real_drive, pins=pins, git_factory=git_factory,
        registry=load_registry(harness_signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=DriveReadViewPublisher(
            real_drive, folder_id=readview_id, manifest_file_id=manifest_id,
            converters={"opencode": get_converter("opencode")},
            clock=SystemClock(), workdir=tmp_path / "publish"),
        clock=SystemClock(),
    )
    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"中止於 {report.aborted_at}:{report.code}"
    assert report.counts["accepted"] == 1 and report.counts["rejected"] == 0
    assert real_drive.list_children(inbox_id) == []

    reader = AgoraReader(
        ReadViewClient(
            real_drive,
            ReaderConfig(manifest_file_id=manifest_id,
                         sa_key_path=tmp_path / "sa.json",
                         cache_dir=tmp_path / "cache"),
            clock=SystemClock(),
        ),
        clock=SystemClock(),
    )
    return _Bootstrap(
        prefix_id=prefix_id, readview_id=readview_id, manifest_id=manifest_id,
        inbox_id=inbox_id, harness_signer=harness_signer, cfg=cfg, deps=deps,
        reader=reader, run=run, tmp_path=tmp_path,
    )


def _writer_deps(boot: _Bootstrap, real_drive, commit: Any) -> Any:
    """`agora checkout` 的寫入端：簽章金鑰 ＋ 收件匣 ＋ 觸發提交流程的函式。

    用的是**登錄檔裡那把**簽章金鑰（`harness_signer`）：收件匣的授權是依登錄檔
    的 `allowed_types` 與金鑰判定的，臨時編一把沒登錄的會被驗章擋下來。

    `commit` 就是「同步並提交」那個可注入的函式：把項目放進收件匣、跑一輪
    提交流程、再等讀取介面看得到。
    """
    signer = boot.harness_signer
    # 讀取身分對 Agora 物件資料夾只有唯讀權限（分享步驟見 deploy.md 步驟 2）；
    # 這裡用測試用的 rclone 憑證（它本來就看得到整個前綴），驗的是機制。
    return CheckoutDeps(
        reader=boot.reader, clock=SystemClock(),
        signer=Signer(signer.profile, signer.key_id, signer.private_key),
        inbox_folder_id=boot.inbox_id, drive=real_drive, commit_claim=commit,
        objects=ObjectFetcher(real_drive, boot.prefix_id),
        journal=ClaimJournal(boot.tmp_path / "checkout-claims"),
    )


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_committer_round_then_checkout_and_load_replay_identical_prefix(
    it_settings, real_drive, sandbox, tmp_path
):
    boot = _bootstrap(it_settings, real_drive, sandbox, tmp_path)
    prefix_id, readview_id, inbox_id = (
        boot.prefix_id, boot.readview_id, boot.inbox_id)

    # 讀取視圖**只**發佈閱讀版——raw 的位元組不進衍生物（那等於把真本的位元組
    # 複製一份出來）。`agora checkout` 改走 annex key。
    published = {f.name for f in real_drive.list_children(readview_id)}
    assert any(n.startswith("reading-") for n in published), sorted(published)
    assert not any(n.startswith("raw-") for n in published), sorted(published)
    # 但真本前綴裡確實有那個 annex 物件（checkout 要依 key 取它）。
    # 前綴裡還有 seed repo 的 payload 物件，所以只檢查「這一個在不在」，
    # 而它的名字必須剛好是「大小 ＋ 內容雜湊」——那正是取回後要驗的東西。
    on_drive = {f.name for f in real_drive.list_children(prefix_id)
                if f.name.startswith("SHA256E-")}
    expected_sha = hashlib.sha256(RAW_S1).hexdigest().lower()
    annex_key = f"SHA256E-s{len(RAW_S1)}--{expected_sha}"
    assert annex_key in on_drive, sorted(on_drive)

    # ------------------------------------------------------------------
    # agora checkout
    # ------------------------------------------------------------------
    # 這支測試只驗「開頭位元組相同」，所以提交那一步只把接續記錄放進收件匣就當
    # 成功；真的跑一輪提交流程並等讀取介面確認的是下一支測試。
    def upload_only(items, *, timeout: Any) -> Any:
        for item in items:
            upload_item(real_drive, inbox_id, item)
        return SimpleNamespace(rejected=(), timed_out=False,
                               summary=lambda: "只上傳，沒跑提交流程")

    deps_out = _writer_deps(boot, real_drive, upload_only)
    pkg_dir = tmp_path / "pkg"
    try:
        pkg = checkout(boot.reader, deps_out, [AGORA_SESSION], pkg_dir,
                       task="接著把這一段做完")
    finally:
        deps_out.objects.close()

    data, raws = read_package(pkg_dir)
    assert raws[0] == RAW_S1, "起點包裡的原始紀錄必須與來源位元組相同"
    assert data["segments"][0]["snapshot_sha256"] == expected_sha
    assert data["new_session"]["session_id"].startswith("opencode:ses_")

    # ------------------------------------------------------------------
    # agora-opencode load（注入 runner；環境有 opencode 時再真的匯入一次）
    # ------------------------------------------------------------------
    from aistorage.adapters.opencode import build_export

    export, new_session_id, segments, source = build_export(pkg_dir)
    assert source == "opencode" and segments == 1
    assert export["info"]["id"] == pkg.new_session_id.split(":", 1)[1]

    # 黃金樣本最後兩則是 reverted（見 test_committer_round_smoke 的註解），
    # 所以對照組取「到接續點為止」的那一段。
    limit = _continuable_limit(tmp_path)
    original = _raw_export(limit)
    # 黃金樣本是合成的：工具 part 沒有 `callID`（真實的 opencode 匯出檔有），
    # 那種匯出檔的 tool_call id 是 opencode 依訊息 id 現算出來的，重編 id 之後
    # 本來就會不同。所以這裡比對時略過 tool_call id——位元組相同的真正理由是
    # `raws[0] == RAW_S1`（下一個斷言）加上真實匯出檔會保留的 `callID`
    # （單元測試 test_checkout_and_load_replay_a_byte_identical_prefix 用有
    # callID 的樣本驗過）。
    assert wire_prefix(export, tool_call_id_key=None) == wire_prefix(
        original, tool_call_id_key=None), (
        "新 session 送給模型的開頭必須與原 session 位元組相同"
        f"（{len(wire_prefix(export, tool_call_id_key=None))} bytes）"
    )
    # 最強的那條：起點包交出的原始紀錄與真本一個位元組都沒差
    assert raws[0] == RAW_S1

    # ------------------------------------------------------------------
    # agora-opencode load
    # ------------------------------------------------------------------
    # 這裡**不**真的呼叫 `opencode import`：共用測試樣本
    # （`tests/unit/data/converters/opencode/basic.json`）是給轉換器用的形狀，
    # 沒有真實匯出檔帶的 `slug`／`version`，`parentID` 也是明確的 null 而不是
    # 省略——opencode 自己的 import 會先擋下來（實測）。拿它驗匯入等於在驗那個
    # 樣本，不是驗這條路徑；「真的匯入」由技術驗證 docs/spike/session-import.md
    # 用**真實**匯出檔做過（Q1〜Q5）。這裡驗的是轉接器交給 opencode 的東西：
    # 呼叫位置、argv、以及匯出內容。
    from aistorage.adapters.opencode import load

    project = tmp_path / "project"
    project.mkdir()
    seen: dict[str, Any] = {}

    def runner(argv: Sequence[str], cwd: Path) -> str:
        seen["argv"] = list(argv)
        seen["cwd"] = cwd
        seen["payload"] = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        return f"Imported session: {seen['payload']['info']['id']}"

    result = load(pkg_dir, workdir=project, runner=runner)
    # import 必須在**目標專案目錄**跑（spike Q1-2：它會把 directory 改寫成當下目錄）
    assert seen["argv"][:2] == ["opencode", "import"]
    assert seen["cwd"] == project
    assert seen["payload"]["info"]["id"] == result.session_id
    assert result.session_id == pkg.new_session_id.split(":", 1)[1], \
        "轉接器要沿用起點包預留的 id（收件匣裡的認領或接續記錄寫的是它）"
    assert result.messages == limit and result.segments == 1


def test_checkout_records_a_continuation_link_for_a_direct_start_point(
    it_settings, real_drive, sandbox, tmp_path
):
    """1→1：直接起點（沒有交接單）也必須在 Agora 留下一條接續 Link。

    完整走一遍：`agora checkout <session>` → 接續單進收件匣 → 一輪提交流程 →
    讀取視圖。驗的是：
    - 新的 S2 在 Agora 裡（自己帶的預留，零則訊息）；
    - 一條從 S2 指向 S1 的接續 Link，接續點就是 checkout 定位的那個位置，
      而且指向**被釘住的那份快照**；
    - `agora show` 讀的那個視圖裡，正向（S2 的 links_out）與反向（S1 的
      links_in）**兩個方向都看得到**。
    """
    from aistorage.syncer.commit import awaited_for_item, wait_visible

    boot = _bootstrap(it_settings, real_drive, sandbox, tmp_path)
    rounds: list[Any] = []

    def commit_and_wait(items, *, timeout: Any) -> Any:
        """把接續記錄放進收件匣，跑一輪提交流程，等讀取介面看得到。"""
        for item in items:
            upload_item(real_drive, boot.inbox_id, item)
        report = boot.run(boot.cfg, boot.deps, dry_run=False)
        assert report.ok is True, f"中止於 {report.aborted_at}:{report.code}"
        rounds.append(report)
        # 真的輪詢讀取介面（用的是同步器同一支可見性判斷）
        result = wait_visible(
            boot.reader, [awaited_for_item(i) for i in items],
            timeout=timedelta(minutes=2), poll=timedelta(seconds=1),
            progress=lambda _m: None, sleeper=time.sleep)
        assert not result.rejected, f"被拒：{result.rejected}"
        return result

    deps_out = _writer_deps(boot, real_drive, commit_and_wait)
    pkg_dir = tmp_path / "pkg"
    try:
        pkg = checkout(boot.reader, deps_out, [AGORA_SESSION], pkg_dir,
                       task="接著把這一段做完")
    finally:
        deps_out.objects.close()

    assert len(rounds) == 1, "checkout 應該只觸發一輪提交流程"
    assert real_drive.list_children(boot.inbox_id) == [], "收件匣要清空"
    new_id = pkg.new_session_id
    _limit, point_id = _continuation_point(tmp_path)
    snap_sha = hashlib.sha256(RAW_S1).hexdigest().lower()

    def _value(result: Any) -> Any:
        return getattr(result, "value", result)

    new_view = _value(boot.reader.get_session(new_id))
    assert new_view.session.status == "running"
    assert new_view.session.raw_sha256, "預留的空匯出檔成為 S2 的第一份快照"
    # 零則訊息：S2 還沒有人開工
    assert _value(boot.reader.get_reading(new_id))["messages"] == []

    out = [(l.kind, l.from_session_id, l.to_session_id, l.snapshot_sha256,
            l.message_id) for l in new_view.links_out]
    assert out == [("continuation", new_id, AGORA_SESSION, snap_sha, point_id)], \
        f"S2 應該有一條指向 S1 的接續 Link，接續點是 checkout 定位的那一則：{out}"

    # 反向：被接續的 S1 看得見誰接續了它，而且自己完全沒變
    old_view = _value(boot.reader.get_session(AGORA_SESSION))
    assert [(l.from_session_id, l.to_session_id, l.snapshot_sha256, l.message_id)
            for l in old_view.links_in] == [(new_id, AGORA_SESSION, snap_sha,
                                             point_id)]
    assert old_view.links_out == []
    assert old_view.session.raw_sha256 == snap_sha
    # 接續點指著的那份快照有被發佈（否則 checkout 交出去的原始紀錄在讀取視圖裡
    # 沒有對應的東西）
    pinned = boot.reader.get_snapshot(AGORA_SESSION, snap_sha)
    assert _value(pinned).annex_key, "被接續 Session 的最新快照就是這一份"


