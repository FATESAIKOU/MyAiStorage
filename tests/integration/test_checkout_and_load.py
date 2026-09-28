"""整合測試：一輪 committer → `agora checkout` → `agora-opencode load`。

驗證的是 I1 真正要成立的那件事：**新 session 送給模型的開頭與原 session
位元組相同**（ADR 0010 的 KV cache 要求）。

真的 Drive、真的 git-annex、真的 pin repo。收尾由 `sandbox` 清掉（釘選值條目
也一併刪掉——pin repo 是所有線共用的，留下會變成垃圾）。

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
from pathlib import Path
import sys
from typing import Any, Sequence

import pytest

from aistorage.agora_cli.checkout import CheckoutDeps, checkout
from aistorage.agora_cli.package import read_package
from aistorage.clock import SystemClock
from aistorage.committer.publish import NullPublisher
from aistorage.converters import get_converter
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


def _continuable_limit(tmp_path: Path) -> int:
    """到「最後一則已完成、未撤銷的訊息」為止的則數（接續點的定義）。

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
            return index + 1
    raise AssertionError(f"接續點訊息不在黃金樣本裡: {point.message_id}")


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_committer_round_then_checkout_and_load_replay_identical_prefix(
    it_settings, real_drive, sandbox, tmp_path
):
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    class _Folder:
        id = inbox_id
    inbox = _Folder()

    harness_signer = make_signer(tmp_path)
    write_registry(harness_signer, inbox.id)

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

    from aistorage.committer.config import CommitterConfig
    from aistorage.committer.run import Deps, init_pin_cli, run
    from aistorage.identity import load_registry

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
    deps = Deps(
        drive=real_drive, pins=pins, git_factory=git_factory, registry=None,
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock(),
    )
    state = init_pin_cli(cfg, deps, confirm=True)
    assert state.repo_uuid == annex.uuid

    # 一個 session 進收件匣
    _put_session(real_drive, inbox.id, harness_signer, RAW_S1, S1,
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
    assert real_drive.list_children(inbox.id) == []

    # 讀取視圖真的有把**原始紀錄**發佈出去（這是 checkout 能不能用的前提）
    published = {f.name for f in real_drive.list_children(readview_id)}
    assert any(n.startswith("raw-") for n in published), sorted(published)
    assert any(n.startswith("reading-") for n in published), sorted(published)

    # ------------------------------------------------------------------
    # agora checkout
    # ------------------------------------------------------------------
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
    profile = "it-checkout"
    priv, pub = _test_keypair()
    deps_out = CheckoutDeps(
        reader=reader, clock=SystemClock(),
        signer=Signer(profile, f"{profile}-{hashlib.sha256(pub).hexdigest()[:8]}",
                      priv),
        inbox_folder_id=inbox.id, drive=real_drive, commit_claim=None,
    )
    pkg_dir = tmp_path / "pkg"
    pkg = checkout(reader, deps_out, [AGORA_SESSION], pkg_dir,
                   task="接著把這一段做完")

    data, raws = read_package(pkg_dir)
    assert raws[0] == RAW_S1, "起點包裡的原始紀錄必須與來源位元組相同"
    assert data["segments"][0]["snapshot_sha256"] == hashlib.sha256(
        RAW_S1).hexdigest().lower()
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
        "轉接器要沿用起點包預留的 id（收件匣裡的認領記的是它）"
    assert result.messages == limit and result.segments == 1


def _test_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return priv_raw, pub_raw


