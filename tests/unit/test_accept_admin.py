"""Independent Acceptance Tests for Admin Operations (Task 6.1 - 6.5).

Adheres strictly to:
- docs/impl/group5-7-modules.md §5, §8.2
- PM Decision 6 (rollback under AdminLock, direct without signed inbox item)
- PM Decision 7 (AdminLock uses pin maintenance flag as primary lock)
- AdminLock, Erase dry-run & partial erase, Rollback, Health Check, Recover
- All git / git-annex repos run strictly in tmp_path
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.erase import (
    EraseTarget,
    apply_erase,
    plan_erase,
    plan_hash,
)
from aistorage.admin.health import (
    Check,
    HealthData,
    run_health,
    summarize,
)
from aistorage.admin.lock import (
    AdminLock,
    GitHubAdmin,
    MaintenanceFlag,
    MemoryPinFiles,
    maintenance_relpath,
    read_maintenance,
)
from aistorage.admin.recover import (
    detect_remote_state,
    plan_recover,
    plan_recover_from_drive,
)
from aistorage.admin.rollback import (
    list_rollback_points,
    rollback_session,
)
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.schema import generate_ulid


T0 = "2026-09-27T08:00:00Z"
T1 = "2026-09-27T09:00:00Z"
T2 = "2026-09-27T10:00:00Z"
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers & Fakes
# ---------------------------------------------------------------------------


def make_gh_fake(*, runs: list[dict] | None = None):
    state = {"enabled": True, "runs": list(runs or [])}

    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "disable"]:
            state["enabled"] = False
            return ""
        if cmd[1:3] == ["workflow", "enable"]:
            state["enabled"] = True
            return ""
        if cmd[1:4] == ["workflow", "view"]:
            return '"active"' if state["enabled"] else '"disabled"'
        if cmd[1:3] == ["run", "list"]:
            status = cmd[cmd.index("--status") + 1]
            return json.dumps([r for r in state["runs"] if r.get("status") == status])
        raise AssertionError(f"Unexpected gh command: {cmd}")

    return runner, state


def make_session_record(sid: str, sha: str, size: int, snap_at: str) -> SessionRecord:
    return SessionRecord(
        id=sid,
        producer="profile:mac-opencode",
        created_at=T0,
        updated_at=snap_at,
        status="running",
        snapshot_at=snap_at,
        raw_sha256=sha,
        raw_size=size,
        committed_at=snap_at,
        last_item_key=generate_ulid(),
        title=f"Session {sid}",
    )


def make_store(tmp_path: Path) -> AgoraStore:
    worktree = tmp_path / "agora"
    worktree.mkdir(parents=True, exist_ok=True)
    return AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")


# ---------------------------------------------------------------------------
# Acceptance Tests: 6.5 AdminLock
# ---------------------------------------------------------------------------


def test_admin_lock_lifecycle_and_order():
    """驗證 AdminLock 完整生命週期: 寫入維護旗標 -> 停用 workflow -> precheck -> 清除旗標 -> 啟用 workflow。"""
    pins = MemoryPinFiles()
    runner, gh_state = make_gh_fake(runs=[])
    gh = GitHubAdmin("owner/repo", runner=runner)
    precheck_called = []
    clock = FixedClock(T0)

    assert read_maintenance(pins, "agora") is None
    assert gh_state["enabled"] is True

    with AdminLock(
        repo="agora",
        pins=pins,
        gh=gh,
        workflow="committer.yml",
        reason="test maintenance",
        by="admin_alice",
        clock=clock,
        timeout=timedelta(seconds=5),
        poll=timedelta(milliseconds=50),
        precheck=lambda: precheck_called.append(True),
    ):
        # 進入 Lock 區間：維護旗標存在，workflow 被停用，precheck 已執行
        flag = read_maintenance(pins, "agora")
        assert flag is not None
        assert flag.reason == "test maintenance"
        assert flag.by == "admin_alice"
        assert gh_state["enabled"] is False
        assert precheck_called == [True]

    # 離開 Lock 區間：維護旗標清除，workflow 重新啟用
    assert read_maintenance(pins, "agora") is None
    assert gh_state["enabled"] is True


def test_admin_lock_timeout_on_active_runs():
    """驗證當 workflow runs 始終有執行中或排隊中任務時，AdminLock 逾時失敗並清理旗標。"""
    pins = MemoryPinFiles()
    runner, gh_state = make_gh_fake(runs=[{"id": 1, "status": "in_progress"}])
    gh = GitHubAdmin("owner/repo", runner=runner)
    clock = FixedClock(T0)

    with pytest.raises(AdminError, match="逾時"):
        with AdminLock(
            repo="agora",
            pins=pins,
            gh=gh,
            workflow="committer.yml",
            reason="busy-test",
            clock=clock,
            timeout=timedelta(0),
            poll=timedelta(0),
        ):
            pass

    # 逾時退出後，旗標已被清理且 workflow 恢復
    assert read_maintenance(pins, "agora") is None
    assert gh_state["enabled"] is True


def test_admin_lock_maintains_lock_when_precheck_fails():
    """驗證當 precheck 拋出異常時，AdminLock 保持鎖定狀態（旗標保留、workflow 保持停用以待調查）。"""
    pins = MemoryPinFiles()
    runner, gh_state = make_gh_fake(runs=[])
    gh = GitHubAdmin("owner/repo", runner=runner)
    clock = FixedClock(T0)

    def failing_precheck():
        raise AdminError("Remote manifest hash does not match pin!")

    with pytest.raises(AdminError, match="Remote manifest hash"):
        with AdminLock(
            repo="agora",
            pins=pins,
            gh=gh,
            workflow="committer.yml",
            reason="precheck-fail-test",
            clock=clock,
            timeout=timedelta(0),
            poll=timedelta(0),
            precheck=failing_precheck,
        ):
            pass

    # 預檢失敗時保持上鎖狀態（待管理員人工排查）
    assert read_maintenance(pins, "agora") is not None
    assert gh_state["enabled"] is False


def test_admin_lock_refuses_double_lock():
    """驗證已存在維護旗標時，拒絕重複上鎖。"""
    pins = MemoryPinFiles({
        maintenance_relpath("agora"): json.dumps({"reason": "existing", "at": T0, "by": "other"})
    })
    runner, gh_state = make_gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    clock = FixedClock(T0)

    with pytest.raises(AdminError, match="重複上鎖"):
        with AdminLock(
            repo="agora",
            pins=pins,
            gh=gh,
            workflow="committer.yml",
            reason="double-lock-test",
            clock=clock,
        ):
            pass


# ---------------------------------------------------------------------------
# Acceptance Tests: 6.1 Erase (Plan, Partial Erase, Apply)
# ---------------------------------------------------------------------------


def test_erase_plan_dry_run_and_partial_erase(tmp_path: Path):
    """驗證 plan_erase: dry-run 產出計畫雜湊，且部分抹除僅針對指定對象，保留其他 Session 與物件。"""
    store = make_store(tmp_path)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    inbox = drive.seed_folder("inbox")

    # 建立兩個 session: s1 (要抹除) 與 s2 (要保留)
    content1 = b'{"msg": "secret data to erase"}'
    sha1 = hashlib.sha256(content1).hexdigest()
    p1 = tmp_path / "s1.raw"
    p1.write_bytes(content1)
    store.put_session(make_session_record("opencode:s1", sha1, len(content1), T0), p1)

    content2 = b'{"msg": "public data to keep"}'
    sha2 = hashlib.sha256(content2).hexdigest()
    p2 = tmp_path / "s2.raw"
    p2.write_bytes(content2)
    store.put_session(make_session_record("opencode:s2", sha2, len(content2), T0), p2)

    # 在 Drive 上放置 s1 的遠端 annex 檔案與無關檔案
    annex_key_s1 = f"SHA256E-s{len(content1)}--{sha1}"
    fid_s1 = drive.seed_file(prefix, annex_key_s1, content1)
    fid_other = drive.seed_file(prefix, "other_file.txt", b"keep this")

    admin_deps = AdminDeps(
        drive=drive,
        prefix_folder_id=prefix,
        inbox_folder_ids=(inbox,),
        clock=FixedClock(T1),
        workdir=tmp_path / "work",
    )
    admin_deps.workdir.mkdir(parents=True, exist_ok=True)

    targets = [
        EraseTarget(kind="session", session_id="opencode:s1"),
        EraseTarget(kind="annex_key", key=annex_key_s1),
    ]
    plan = plan_erase(targets, admin=admin_deps, store=store)

    assert plan_hash(plan) is not None
    assert any(t.session_id == "opencode:s1" for t in plan.targets)

    # 驗證待刪除清單中含有 s1 的 Drive 檔案，但絕不含 other_file.txt
    assert fid_s1 in plan.delete_file_ids
    assert fid_other not in plan.delete_file_ids


def test_erase_apply_requires_hash_confirmation(tmp_path: Path):
    """驗證 apply_erase: 執行必須提供精確相符之 confirm plan_hash，否則拒絕執行。"""
    store = make_store(tmp_path)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    inbox = drive.seed_folder("inbox")

    content = b'{"msg": "erase me"}'
    sha = hashlib.sha256(content).hexdigest()
    p = tmp_path / "s.raw"
    p.write_bytes(content)
    store.put_session(make_session_record("opencode:s", sha, len(content), T0), p)

    admin_deps = AdminDeps(
        drive=drive,
        prefix_folder_id=prefix,
        inbox_folder_ids=(inbox,),
        clock=FixedClock(T1),
        workdir=tmp_path / "work2",
    )
    admin_deps.workdir.mkdir(parents=True, exist_ok=True)

    plan = plan_erase([EraseTarget(kind="session", session_id="opencode:s")], admin=admin_deps, store=store)

    # 隨便給錯誤的 confirm hash -> 拒絕（在碰任何東西之前）
    with pytest.raises(AdminError, match="確認碼與計畫雜湊不符"):
        apply_erase(
            plan,
            confirm="wrong_hash_12345678",
            admin=admin_deps,
            store=store,
            repo_dir=tmp_path / "agora",
            cfg=None,
            deps=None,
            git=None,
            why="測試",
            canary="CANARY_TEST",
        )

    # 沒有 canary / 沒有 why 也拒絕（後置條件與紀錄都要有依據）
    with pytest.raises(AdminError, match="canary"):
        apply_erase(plan, confirm=plan_hash(plan), admin=admin_deps, store=store,
                    repo_dir=tmp_path / "agora", cfg=None, deps=None, git=None,
                    why="測試", canary="   ")
    with pytest.raises(AdminError, match="原因"):
        apply_erase(plan, confirm=plan_hash(plan), admin=admin_deps, store=store,
                    repo_dir=tmp_path / "agora", cfg=None, deps=None, git=None,
                    why="  ", canary="CANARY_TEST")


# ---------------------------------------------------------------------------
# Acceptance Tests: 6.2 Rollback
# ---------------------------------------------------------------------------


def test_rollback_points_and_session_restore(tmp_path: Path):
    """驗證 list_rollback_points 列出歷史快照，且 rollback_session 將歷史版本作為新快照恢復。"""
    store = make_store(tmp_path)
    clock = FixedClock(T2)
    sid = "opencode:ses_rb"

    # 建立版本 0 (snap at T0)
    raw0 = b'{"version": 0, "msg": "first good"}'
    sha0 = hashlib.sha256(raw0).hexdigest()
    p0 = tmp_path / "r0.raw"
    p0.write_bytes(raw0)
    store.put_session(make_session_record(sid, sha0, len(raw0), T0), p0)

    # 建立版本 1 (snap at T1，有問題的版本)
    raw1 = b'{"version": 1, "msg": "bad update"}'
    sha1 = hashlib.sha256(raw1).hexdigest()
    p1 = tmp_path / "r1.raw"
    p1.write_bytes(raw1)
    store.put_session(make_session_record(sid, sha1, len(raw1), T1), p1)

    # 檢查 rollback points
    points = list_rollback_points(store, sid)
    assert len(points) == 2
    assert points[0].snapshot_sha256 == sha0
    assert points[0].is_current is False
    assert points[1].snapshot_sha256 == sha1
    assert points[1].is_current is True

    # 驗證拒絕不合理的 rollback 請求
    with pytest.raises(AdminError, match="說明原因"):
        rollback_session(
            store=store,
            session_id=sid,
            target_snapshot_sha256=sha0,
            reason="   ",
            clock=clock,
        )

    with pytest.raises(AdminError, match="快照不在歷史裡"):
        rollback_session(
            store=store,
            session_id=sid,
            target_snapshot_sha256="0" * 64,
            reason="bogus sha",
            clock=clock,
        )

    # 執行回滾到版本 0
    res = rollback_session(
        store=store,
        session_id=sid,
        target_snapshot_sha256=sha0,
        reason="revert bad update",
        who="admin_bob",
        clock=clock,
    )

    assert res.session_id == sid
    assert res.from_sha256 == sha1
    assert res.to_sha256 == sha0

    # 驗證 store 上的當前 session 已經回到 sha0 內容
    curr = store.get_session(sid)
    assert curr is not None
    assert curr.raw_sha256 == sha0
    assert curr.snapshot_at == format_rfc3339_test(T2)


def format_rfc3339_test(ts: str) -> str:
    # 確保格式對齊 (6 位微秒)
    return "2026-09-27T10:00:00.000000Z"


# ---------------------------------------------------------------------------
# Acceptance Tests: 6.3 Health Checks
# ---------------------------------------------------------------------------


def test_health_checks_all_ok():
    """驗證健康檢查在全正常狀態下返回 ok。"""
    data = HealthData(
        tokens_ok={"committer": True, "worker": True},
        workflow_enabled=True,
        last_success_at="2026-09-27T09:30:00.000Z",
        schedule_interval_ok=True,
        actions_minutes_2w=50.0,
        quota_limit=10000,
        quota_usage=1000,
        index_bytes=1024 * 1024,
        readview_files=100,
        manifest_main_sha="sha_valid",
        pin_main_sha="sha_valid",
        held_files_known=True,
    manifest_conflict_known=True,
        prune_ok=True,
    )
    checks = run_health(data, now=NOW)
    assert summarize(checks) == "ok"
    assert all(c.status == "ok" for c in checks)


def test_health_checks_fail_conditions():
    """驗證各項重大異常觸發 fail: token 損毀、workflow 被停用、連續中斷、過期未成功。"""
    # 1. Token 損毀
    data_token = HealthData(tokens_ok={"committer": False, "worker": True})
    checks_token = run_health(data_token, now=NOW)
    assert summarize(checks_token) == "fail"
    c_token = next(c for c in checks_token if c.name == "token")
    assert c_token.status == "fail"

    # 2. Workflow 被停用
    data_wf = HealthData(workflow_enabled=False)
    checks_wf = run_health(data_wf, now=NOW)
    assert summarize(checks_wf) == "fail"
    c_wf = next(c for c in checks_wf if c.name == "workflow")
    assert c_wf.status == "fail"

    # 3. 連續 abort 次數過多 (>= 3)
    data_abort = HealthData(consecutive_aborts=3)
    checks_abort = run_health(data_abort, now=NOW)
    assert summarize(checks_abort) == "fail"
    c_abort = next(c for c in checks_abort if c.name == "aborts")
    assert c_abort.status == "fail"

    # 4. 上次成功時間超過 48 小時 (stale fail)
    data_stale = HealthData(last_success_at="2026-09-24T00:00:00.000Z")
    checks_stale = run_health(data_stale, now=NOW)
    assert summarize(checks_stale) == "fail"
    c_stale = next(c for c in checks_stale if c.name == "last_success")
    assert c_stale.status == "fail"


def test_health_checks_warn_conditions():
    """驗證警告門檻判定: Actions 分鐘數、配額、索引大小、隔離區成長。"""
    data = HealthData(
        actions_minutes_2w=350.0,  # > 300 min
        quota_limit=1000,
        quota_usage=850,           # > 80%
        index_bytes=60 * 1024 * 1024,  # > 50 MiB
        readview_files=6000,       # > 5000
        quarantine_growing=True,
        quarantine_files=5,
        quarantine_bytes=500,
        schedule_interval_ok=False,
    )
    checks = run_health(data, now=NOW)
    assert summarize(checks) == "warn"
    names_in_warn = {c.name for c in checks if c.status == "warn"}
    assert "actions_minutes" in names_in_warn
    assert "quota" in names_in_warn
    assert "readview_size" in names_in_warn
    assert "quarantine" in names_in_warn
    assert "schedule" in names_in_warn


# ---------------------------------------------------------------------------
# Acceptance Tests: 6.4 Recover
# ---------------------------------------------------------------------------


def test_recover_plan_modes():
    """驗證 plan_recover 的模式判定表: normal / bak-only / disaster recovery。"""
    # 1. 兩者俱在 -> 不需要復原
    plan_normal = plan_recover(manifest_present=True, bak_present=True)
    assert plan_normal.mode == "from-clone"
    assert "不需要復原" in plan_normal.steps[0]

    # 2. Manifest 缺失但 bak 存在 -> bak-only 模式
    plan_bak = plan_recover(manifest_present=False, bak_present=True)
    assert plan_bak.mode == "bak-only"
    assert any("BAK_RECOVERY" in s for s in plan_bak.steps)

    # 3. 兩者皆缺失但未指定 new_prefix -> 拋出 AdminError 拒絕盲目執行
    with pytest.raises(AdminError, match="from-clone"):
        plan_recover(manifest_present=False, bak_present=False)

    # 4. 兩者皆缺失且給定 new_prefix -> 災難復原模式 (從 clone 重建)
    plan_disaster = plan_recover(manifest_present=False, bak_present=False, new_prefix="new_folder_123")
    assert plan_disaster.mode == "from-clone"
    assert plan_disaster.detail["new_prefix"] == "new_folder_123"
    assert any("init-pin" in s for s in plan_disaster.steps)


def test_detect_and_plan_from_drive():
    """驗證以 DriveClient 探測遠端狀態並生成復原計畫。"""
    drive = FakeDrive()
    prefix = drive.seed_folder("test_prefix")
    uuid = "22222222-3333-4444-5555-666666666666"

    # 初始狀態：皆不存在
    assert detect_remote_state(drive, prefix, uuid) == (False, False)

    # 放入 bak 檔案
    drive.seed_file(prefix, f"GITMANIFEST--{uuid}.bak", b"bak_data")
    assert detect_remote_state(drive, prefix, uuid) == (False, True)
    plan_b = plan_recover_from_drive(drive, prefix, uuid)
    assert plan_b.mode == "bak-only"

    # 放入 manifest 檔案
    drive.seed_file(prefix, f"GITMANIFEST--{uuid}", b"manifest_data")
    assert detect_remote_state(drive, prefix, uuid) == (True, True)
    plan_all = plan_recover_from_drive(drive, prefix, uuid)
    assert plan_all.mode == "from-clone"


# ---------------------------------------------------------------------------
# Acceptance Tests: review H5／H6／H7（管理操作失敗時的行為）
# ---------------------------------------------------------------------------


def test_admin_lock_error_in_with_keeps_lock(tmp_path: Path):
    """H5：with 區塊內出錯時不得解除鎖、不得重新啟用 workflow。

    抹除／回滾做到一半失敗時，遠端可能已經不一致；這時解除鎖會讓提交流程
    回到不一致的遠端上（settle 每輪中止，或用舊 pin 把新狀態全隔離）。
    """
    pins = MemoryPinFiles()
    runner, gh_state = make_gh_fake(runs=[])
    gh = GitHubAdmin("owner/repo", runner=runner)
    notices: list[str] = []

    with pytest.raises(RuntimeError):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="erase", notify=notices.append):
            assert read_maintenance(pins, "agora") is not None
            raise RuntimeError("push 失敗")

    flag = read_maintenance(pins, "agora")
    assert flag is not None, "失敗後旗標必須保留"
    assert flag.reason == "erase"
    assert gh_state["enabled"] is False, "失敗後 workflow 必須維持停用"
    assert any("unlock --confirm" in n for n in notices)

    # 只有明確的人工解除才會解鎖
    lock = AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                     reason="erase")
    assert lock.unlock() is not None
    assert read_maintenance(pins, "agora") is None
    assert gh_state["enabled"] is True


def test_swap_order_and_abort_report(tmp_path: Path, monkeypatch):
    """H6：刪遠端之後必須重推並重建 pin；中途失敗要保留進度與下一步。"""
    from aistorage.admin.remote import (
        DeleteGroup, SwapAborted, swap_remote,
    )
    from aistorage.drive.fake import FakeDrive
    from aistorage.integrity.pin import MemoryPinStore, PinState

    class Cfg:
        repo = "agora"
        repo_uuid = "11111111-2222-3333-4444-555555555555"
        prefix_folder_id = ""

    class Git:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.calls: list[str] = []

        def copy(self, remote: str) -> None:
            self.calls.append("copy")
            if self.fail:
                raise RuntimeError("annex copy 失敗")

        def push(self, *a, **k) -> None:
            self.calls.append("push")

        def ls_remote(self, remote: str = "origin") -> dict[str, str]:
            return {"refs/heads/main": "a" * 40, "refs/heads/git-annex": "b" * 40}

        def local_refs(self, branches=("main", "git-annex")) -> dict[str, str]:
            return {"refs/heads/main": "a" * 40, "refs/heads/git-annex": "b" * 40}

    class Deps:
        def __init__(self, drive, pins) -> None:
            self.drive = drive
            self.pins = pins
            self.clock = FixedClock(T1)

    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    bundle_name = "GITBUNDLE-s10--11111111-2222-3333-4444-555555555555-" + "0" * 64
    manifest = drive.seed_file(
        prefix, "GITMANIFEST--11111111-2222-3333-4444-555555555555",
        (bundle_name + "\n").encode())
    bundle = drive.seed_file(prefix, bundle_name, b"b")
    Cfg.prefix_folder_id = prefix
    pins = MemoryPinStore(initial_state=PinState(
        repo="agora", repo_uuid=Cfg.repo_uuid, refs={"refs/heads/main": "a" * 40},
        manifest_sha256="m" * 64, prev_manifest_sha256=None, active_bundles=(),
        removed_bundles=frozenset(), annex_keys=frozenset(),
        promoted_at=T0, run_id="1"))
    deps = Deps(drive, pins)
    admin = AdminDeps(drive=drive, clock=FixedClock(T1), workdir=tmp_path / "w",
                      repo="agora", repo_uuid=Cfg.repo_uuid, prefix_folder_id=prefix)
    repo_dir = tmp_path / "clone"
    repo_dir.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo_dir))
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({"readview_rebuild_epoch": 1}))

    def _push_ok(git: Git):
        def _p() -> dict[str, str]:
            if git.fail:
                raise RuntimeError("annex copy 失敗")
            git.calls += ["copy", "push"]
            return git.local_refs()
        return _p

    def _pin_ok():
        return PinState(repo="agora", repo_uuid=Cfg.repo_uuid,
                        refs={"refs/heads/main": "a" * 40}, manifest_sha256="m" * 64,
                        prev_manifest_sha256=None, active_bundles=(),
                        removed_bundles=frozenset(), annex_keys=frozenset(),
                        promoted_at=T1, run_id="admin")

    git = Git()
    report = swap_remote(admin=admin, cfg=Cfg, deps=deps, git=git, repo_dir=repo_dir,
                         delete_groups=[DeleteGroup("repo", (bundle,), (prefix,))],
                         force_push=True, config_path=config,
                         push_fn=_push_ok(git), pin_rebuild_fn=_pin_ok)
    assert [s.name for s in report.steps] == [
        "delete-remote", "recheck-remote", "push", "verify-remote",
        "rebuild-pin", "readview-epoch"]
    assert report.promoted_at == T1
    assert report.rebuild_epoch == 2          # 讀取視圖要完整重建
    assert set(report.deleted_file_ids) == {bundle}

    # 中途失敗：已完成步驟與下一步都要留給人
    bundle2 = drive.seed_file(prefix, "GITBUNDLE-s11--" + Cfg.repo_uuid + "-" + "1" * 64, b"b2")
    bad = Git(fail=True)
    with pytest.raises(SwapAborted) as excinfo:
        swap_remote(admin=admin, cfg=Cfg, deps=deps, git=bad, repo_dir=repo_dir,
                    delete_groups=[DeleteGroup("repo", (bundle2,), (prefix,))],
                    force_push=True, config_path=None,
                    push_fn=_push_ok(bad), pin_rebuild_fn=_pin_ok)
    assert excinfo.value.report.done == ("delete-remote", "recheck-remote")
    assert "下一步" in str(excinfo.value)
