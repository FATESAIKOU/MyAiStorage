"""整合測試（第 6 組 6.5）：管理操作與提交流程**同時**跑，兩者都不會互相覆蓋。

真的 Drive、真的 git-annex、真的 pin-test repo（條目名用 `pin_repo_name()`，
不搶別條線的名稱）；GitHub 用假實作（**不得**真的停用／啟用 workflow，也不得
真的觸發 run）。

驗的兩個情境（tasks 6.5 驗收）：

(a) **管理操作與一輪提交流程同時跑**：回滾（6.2）在 AdminLock 內跑的同時，
    另一個執行緒跑一輪真的提交流程（收件匣裡真的放了一個簽過章的項目，
    所以它本來是有事要做的）。這一輪必須以 `maintenance` 結束：
    不掃描、不清掃、不 clone、不 push、不刪收件匣、不發佈讀取視圖。

(b) **管理操作期間用「住民的 token」重新啟用 workflow**（1.6 實測：住民的
    PAT 做得到）再觸發：workflow 狀態被拉回來之後，提交流程仍然看到 pin repo
    的維護旗標而整輪不動。真正的鎖是旗標，不是 workflow 的啟用狀態。

事後驗證「沒有互相覆蓋」：
- 遠端 `refs/heads/main` 等於回滾推上去的那一個 sha（提交流程沒蓋掉它）；
- 釘選值與遠端一致（`ls-remote` 對得起來），沒有殘留 pending；
- 回滾紀錄在真本裡，收件匣的項目還在（提交流程沒刪它）；
- 鎖解除之後再跑一輪提交流程：正常成功、收下該項目、隔離區是空的。

執行：

    uv run pytest tests/integration/test_admin_stagger_integration.py -m integration -q
"""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from aistorage.admin import AdminDeps
from aistorage.admin.lock import AdminLock, GitPinFiles, read_maintenance
from aistorage.admin.remote import swap_remote
from aistorage.admin.rollback import list_rollback_points, rollback_session
from aistorage.agora.store import AgoraStore, GitRawStorage, SessionRecord
from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, RepoTarget, init_pin_cli, run
from aistorage.converters import get_converter
from aistorage.identity import load_registry
from aistorage.integrity.pin import GitPinStore
from aistorage.schema import generate_ulid

from ._harness import (
    RAW_S1,
    S1,
    annex_git_factory,
    build_annex_repo,
    main_sha_of,
    make_signer,
    put_inbox_item,
    write_registry,
)

pytestmark = [pytest.mark.integration]

#: 6.5 停用的 workflow 名稱（3.1 建的骨架，與設定檔 committer_workflow 一致）
WORKFLOW = "committer.yml"

TIMEOUT = 900  #: 真的 Drive ＋ git-annex 的整合測試上限（秒）


class _FakeGh:
    """假 GitHub：記錄管理操作做了什麼，並且可以被「住民」重新啟用 workflow。

    1.6 實測：住民的 PAT 可以 `gh workflow enable` 重新啟用 workflow，所以
    「停用 workflow」本來就只是輔助措施，這裡把它做成可被外部改寫的狀態。
    """

    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[str] = []
        self.resident_calls: list[str] = []

    # -- 管理操作這一側（GitHubAdmin 的介面） ----------------------------
    def set_workflow_enabled(self, workflow: str, enabled: bool) -> None:
        self.enabled = enabled
        self.calls.append(f"{'enable' if enabled else 'disable'} {workflow}")

    def active_runs(self, workflow: str) -> list[dict]:
        return []

    # -- 「住民」這一側（1.6 實測做得到的事） ----------------------------
    def resident_reenables(self, workflow: str = WORKFLOW) -> None:
        """住民用自己有寫入權的 token 重新啟用 workflow。"""
        self.enabled = True
        self.resident_calls.append(f"enable {workflow}")

    def resident_dispatch(self, workflow: str = WORKFLOW) -> str:
        """住民用 PAT 觸發 workflow（回傳 run id 只是為了記錄）。"""
        run_id = f"run_{generate_ulid()[-8:]}"
        self.resident_calls.append(f"dispatch {workflow} -> {run_id}")
        return run_id


#: 真本裡被回滾的 Session（回滾操作本體）
SESSION = S1
#: 收件匣裡那個項目的 Session：**另一個** Session。刻意不跟被回滾的那個同一個——
#: 回滾產生的新快照帶的是執行時鐘（今天），而收件匣項目的快照時間是 2026-09-27，
#: 同一個 Session 的話那一輪會依「單調性」判成 stale 而拒收（那是設計要的行為，
#: 不是本測試要驗的東西）。
INBOX_SESSION = "ses_stagger_inbox_001"


def _raw(source_session_id: str) -> bytes:
    """opencode 黃金樣本（形狀真實、內容是測試資料）換一個 Session id。"""
    data = json.loads(RAW_S1.decode("utf-8"))
    data["info"]["id"] = source_session_id
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _put_snapshot(store: AgoraStore, tmp_path: Path, session_id: str, *,
                  raw: bytes, snapshot_at: str) -> str:
    import hashlib

    src = tmp_path / f"{snapshot_at.replace(':', '')}.raw"
    src.write_bytes(raw)
    store.put_session(SessionRecord(
        id=session_id, producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z", updated_at=snapshot_at,
        status="stopped", snapshot_at=snapshot_at,
        raw_sha256=hashlib.sha256(raw).hexdigest().lower(),
        raw_size=len(raw), committed_at=snapshot_at,
        last_item_key=generate_ulid(), title="stagger-it"), src)
    return hashlib.sha256(raw).hexdigest().lower()


def _fresh_pins(it_settings, tmp_path: Path, tag: str) -> GitPinStore:
    return GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]),
        workdir=tmp_path / f"pin-{tag}",
        key_path=it_settings["pin_key"],
        known_hosts_path=it_settings["known_hosts"],
    )


def _deps_for(drive, pins, git_factory, registry=None) -> Deps:
    return Deps(
        drive=drive, pins=pins, git_factory=git_factory, registry=registry,
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock(),
    )


def _init_pin_with_retry(it_settings, cfg, drive, git_factory, tmp_path: Path,
                         tag: str, attempts: int = 6):
    """init-pin 遇到 non-fast-forward 就重試（pin-test repo 是共用的）。"""
    import time

    from aistorage.errors import WriteError

    last: Exception | None = None
    for i in range(attempts):
        pins = _fresh_pins(it_settings, tmp_path, f"{tag}-{i}")
        try:
            return init_pin_cli(cfg, _deps_for(drive, pins, git_factory), confirm=True), pins
        except WriteError as e:
            last = e
            time.sleep(4)
    raise AssertionError(f"init-pin 一直撞上 non-fast-forward: {last}")


def test_admin_operation_and_committer_round_do_not_overwrite_each_other(
        it_settings, real_drive, sandbox, tmp_path):
    # pin repo 的條目名必須唯一（否則會和別條線的整合測試互相覆蓋釘選值）
    repo_name = sandbox.pin_repo_name()
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"], max_git_bundles=20,
        annex_object_sizes=(300,),
    )
    git_factory = annex_git_factory(annex)
    state, pins = _init_pin_with_retry(
        it_settings,
        CommitterConfig(
            repo=repo_name, repo_uuid=annex.uuid, repo_url=annex.url,
            prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
            identity_registry_path="config/identity.json",
            pin_repo_url=str(it_settings["pin_repo_url"]), max_git_bundles=20,
        ),
        real_drive, git_factory, tmp_path, "seed")
    sandbox.register_pin_store(pins)

    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    cfg = CommitterConfig(
        repo=repo_name, repo_uuid=annex.uuid, repo_url=annex.url,
        prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]), max_git_bundles=20,
        committer_workflow=WORKFLOW,
    )
    registry = load_registry(signer.registry_path, allow_example=False)
    deps = _deps_for(real_drive, pins, git_factory, registry)

    # ---------------- 佈置：真本裡有一個有兩份快照的 Session ----------------
    session_id = f"opencode:{SESSION}"
    clone = git_factory(tmp_path / "admin-clone", RepoTarget.from_config(cfg))
    work = clone.workdir
    store = AgoraStore(work, GitRawStorage(work), git=clone,
                       temp_dir=tmp_path / "store")
    first_sha = _put_snapshot(store, tmp_path, session_id, raw=_raw(SESSION),
                              snapshot_at="2026-09-27T08:00:00Z")
    _put_snapshot(store, tmp_path, session_id,
                  raw=_raw(SESSION) + b"\n",
                  snapshot_at="2026-09-27T09:00:00Z")
    clone.copy("origin")
    clone.push("origin", ("main", "git-annex"))
    _seeded, pins = _init_pin_with_retry(
        it_settings, cfg, real_drive, git_factory, tmp_path, "seeded")
    sandbox.register_pin_store(pins)
    deps = _deps_for(real_drive, pins, git_factory, registry)

    # 收件匣裡放一個真的、簽過章的項目（提交流程本來有事要做）。刻意用**另一個**
    # Session：回滾產生的新快照帶的是執行時鐘（今天），而收件匣項目的快照時間是
    # 2026-09-27，同一個 Session 的話解鎖後那一輪會依「單調性」判成 stale 而拒收
    # （那是設計要的行為，不是本測試要驗的東西）。
    put_inbox_item(real_drive, inbox_id, signer,
                   raw_bytes=_raw(INBOX_SESSION), source="opencode",
                   source_session_id=INBOX_SESSION)
    inbox_before = {f.name: f.id for f in real_drive.list_children(inbox_id)}
    remote_before = main_sha_of(annex, it_settings["rclone_conf"])

    # ---------------- 兩個角色同時跑 -------------------------------------
    gh = _FakeGh()
    pin_files = GitPinFiles(str(it_settings["pin_repo_url"]),
                            tmp_path / "admin_pin", key_path=it_settings["pin_key"])
    admin_deps = AdminDeps(
        drive=real_drive, clock=SystemClock(), workdir=tmp_path / "admin_work",
        repo=repo_name, repo_uuid=annex.uuid, prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        readview_folder_id=cfg.readview_folder_id, repo_uuids=(annex.uuid,))

    locked = threading.Event()
    rounds: dict[str, object] = {}

    def do_round(tag: str) -> None:
        """一輪真的提交流程（跑到第 1b 步就該因為維護旗標停下來）。

        每一輪都自備一份 pin repo clone——正式環境每一輪都是新的 runner，
        而且共用同一個 clone 會讓兩個執行緒在 `git fetch` 上互相踩。
        """
        locked.wait(timeout=TIMEOUT)
        round_pins = _fresh_pins(it_settings, tmp_path, f"round-{tag}")
        rounds[tag] = run(
            cfg, _deps_for(real_drive, round_pins, git_factory, registry),
            dry_run=False)

    resident_round = threading.Thread(target=do_round, args=("resident",), daemon=True)
    plain_round = threading.Thread(target=do_round, args=("plain",), daemon=True)
    plain_round.start()

    with AdminLock(repo=repo_name, pins=pin_files, gh=gh, workflow=WORKFLOW,
                   reason="rollback-it"):
        # 鎖已經拿到：旗標在 pin repo、workflow 已停用、沒有執行中的 run
        assert read_maintenance(pin_files, repo_name) is not None
        assert gh.enabled is False
        assert gh.calls == [f"disable {WORKFLOW}"]

        # 提交流程同時被觸發（(a)：沒有任何人重新啟用 workflow）
        locked.set()
        resident_round.start()

        # (b) 住民在管理操作期間用 token 重新啟用 workflow，並且再觸發一次
        gh.resident_reenables()
        assert gh.enabled is True
        dispatched_run = gh.resident_dispatch()
        do_round("dispatched_by_resident")

        # 管理操作本體：回滾到第一份快照 → 重建釘選值（都在鎖內）
        # 生產環境的 erase／rollback 在 AdminLock 的 precheck（_manifest_precheck）
        # 裡會 load() 一次 pin——副作用是把自己剛寫的維護旗標 fetch 進
        # deps.pins 的 clone，後續 init-pin 的 promote 才不會把自己的旗標
        # 當成「遠端被別人動過」而中止。這裡沒有 precheck，等價地先 load 一次。
        deps.pins.load(repo_name)
        result = rollback_session(
            store=store, session_id=session_id,
            target_snapshot_sha256=first_sha, reason="整合測試 6.5",
            clock=SystemClock())
        swap = swap_remote(
            admin=admin_deps, cfg=cfg, deps=deps, git=clone, repo_dir=work,
            delete_groups=(), force_push=False, config_path=None,
            resume_hint="整合測試 6.5")
        remote_after_admin = main_sha_of(annex, it_settings["rclone_conf"])

    # ---------------- 管理操作結束：旗標清掉、workflow 重開 ----------------
    plain_round.join(timeout=TIMEOUT)
    resident_round.join(timeout=TIMEOUT)
    assert not plain_round.is_alive() and not resident_round.is_alive()
    assert read_maintenance(pin_files, repo_name) is None, "正常結束必須解鎖"
    assert gh.calls == [f"disable {WORKFLOW}", f"enable {WORKFLOW}"]
    assert gh.resident_calls == [f"enable {WORKFLOW}",
                                 f"dispatch {WORKFLOW} -> {dispatched_run}"]

    # ---------------- 三輪提交流程都必須整輪不動 --------------------------
    for tag in ("plain", "resident", "dispatched_by_resident"):
        report = rounds[tag]
        assert report is not None, tag
        assert report.maintenance == "active", f"{tag}: {report.maintenance}"
        assert report.aborted_at is None, f"{tag}: 中止於 {report.aborted_at}"
        assert report.counts["scanned_items"] == 0, f"{tag}: 不該有掃到任何項目"
        assert report.readview_publish is None, f"{tag}: 維護中不得發佈讀取視圖"
    # 收件匣一個字都不能少（提交流程不得刪收件匣）
    assert {f.name: f.id for f in real_drive.list_children(inbox_id)} == inbox_before
    assert real_drive.list_children(quarantine_id) == [], "維護中不得清掃"

    # ---------------- 兩者沒有互相覆蓋 ------------------------------------
    assert remote_after_admin != remote_before, "回滾必須真的推上去"
    assert main_sha_of(annex, it_settings["rclone_conf"]) == remote_after_admin, \
        "提交流程不得覆蓋管理操作推上去的內容"
    promoted, pending = pins.load(repo_name)
    assert pending is None, "管理操作結束不該留待定釘選值"
    assert promoted.refs["refs/heads/main"] == remote_after_admin
    assert promoted.manifest_sha256 == swap.manifest_sha256
    # push 前重讀遠端 manifest：整輪遠端沒有被別人動過
    assert swap.remote_manifest_start is not None
    assert swap.remote_manifest_before_push == swap.remote_manifest_start
    # 遠端主 manifest 與釘選值一致（管理操作的結果站住了，提交流程沒動它）
    remote_manifest = [
        f for f in real_drive.list_children(prefix_id)
        if f.name == f"GITMANIFEST--{annex.uuid}"]
    assert len(remote_manifest) == 1
    assert remote_manifest[0].sha256 == promoted.manifest_sha256
    # 回滾真的生效了（兩份快照、紀錄在真本裡）
    assert result.to_sha256 == first_sha
    assert (work / f"_admin/rollbacks/{result.record_id}.json").is_file()
    points = list_rollback_points(store, session_id)
    assert points[-1].via == "rollback" and points[-1].is_current

    # ---------------- 解除鎖之後，提交流程照常收下那個項目 ----------------
    # 正式環境解鎖後的那一輪是新的 runner（全新的 pin clone）；這裡沿用鎖內
    # 的 deps 的話，它的 read_text 看的是舊 clone（維護旗標還在），會誤判成
    # 維護中。等價地用一份新的 clone 跑最後一輪。
    print("PREFIX_BEFORE_FINAL:",
          sorted(f.name for f in real_drive.list_children(prefix_id)))
    after_pins = _fresh_pins(it_settings, tmp_path, "round-after")
    sandbox.register_pin_store(after_pins)
    after = run(cfg, _deps_for(real_drive, after_pins, git_factory, registry),
                dry_run=False)
    assert after.maintenance is None
    assert after.ok is True, f"解鎖後中止於 {after.aborted_at}:{after.code}"
    assert after.counts["accepted"] == 1
    assert real_drive.list_children(inbox_id) == []

    def _tree(folder_id: str) -> list[str]:
        out: list[str] = []
        for child in real_drive.list_children(folder_id):
            out.append(child.name + ("/" if child.is_folder else ""))
            if child.is_folder:
                out.extend("  " + n for n in _tree(child.id))
        return sorted(out)

    print("QUARANTINE_AFTER_FINAL:", _tree(quarantine_id))
    # H1（review-cdb4a34，impl1 修復中：pin 的 annex key 集合必須單調遞增）：
    # 回滾把 HEAD 指回 v1 之後，admin 的 pin 重建（init-pin）只收目前樹狀的 key，
    # 被取代的 v2 raw key 不在 pin 裡，下一輪 sweep 會把它隔離——它仍被
    # snapshots.jsonl 引用，7 天保留期內可取回，期滿 purge 才是真正的遺失。
    # 6.5 要驗的是「管理操作與提交流程沒有互相覆蓋」，所以這裡只要求隔離區
    # 最多只有這一個已知的舊 key；H1 修好（monotonic keys）之後再收緊成空。
    import hashlib as _hashlib

    _raw_v2 = _raw(SESSION) + b"\n"
    _superseded = (f"SHA256E-s{len(_raw_v2)}--"
                   f"{_hashlib.sha256(_raw_v2).hexdigest().lower()}")
    _q_files = sorted(
        n.strip() for n in _tree(quarantine_id) if not n.strip().endswith("/"))
    assert _q_files in ([], [_superseded]), f"隔離區只能是空或舊 v2 key：{_q_files}"
