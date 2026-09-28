"""M4（review-cdb4a34）：6.5 收尾的四個修正，每一條一個測試。

- M4-1 `admin/lock.py`：旗標帶操作 id（op）與狀態（state）；`admin_lock_if_needed`
  只允許在 `aborted` 的既有鎖裡做，其他情況一律拒絕。
- M4-2 `committer/__main__._init_pin_under_lock`：precheck 用 target 自己的
  prefix_folder_id 與 repo_uuid，不要用 cfg 的 Agora 值。
- M4-3 `admin/remote.py`：`delete_groups` 不為空時，push 前的重讀必須是
  `None`（主 manifest 已由本輪刪除）；讀到新的主 manifest 就中止。
- M4-4：拿掉寫死的 `"FATESAIKOU/MyAiStorage"` 預設值，設定檔缺少
  github_repository 時直接 raise。

全部用假對象，不碰 Drive／GitHub。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.lock import (
    ABORTED_STATE,
    AdminLock,
    GitHubAdmin,
    MaintenanceFlag,
    MemoryPinFiles,
    admin_lock_if_needed,
    maintenance_relpath,
    parse_maintenance,
    read_maintenance,
)
from aistorage.admin.remote import (
    DeleteGroup,
    SwapAborted,
    swap_remote,
)
from aistorage.clock import FixedClock
from aistorage.committer.config import CommitterConfig
from aistorage.drive.fake import FakeDrive
from aistorage.integrity.pin import MemoryPinStore, PinState

UUID = "11111111-2222-3333-4444-555555555555"
BUNDLE = f"GITBUNDLE-s10--{UUID}-{'0' * 64}"
MANIFEST = f"GITMANIFEST--{UUID}"


# --------------------------------------------------------------- 假對象


class FakeCfg:
    repo = "agora"
    repo_uuid = UUID
    prefix_folder_id = "folder_prefix"


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def copy(self, remote: str) -> None:
        self.calls.append("copy")

    def push(self, *a: Any, **k: Any) -> None:
        self.calls.append("push")

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        return {"refs/heads/main": "1" * 40, "refs/heads/git-annex": "2" * 40}

    def local_refs(self, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
        return {"refs/heads/main": "1" * 40, "refs/heads/git-annex": "2" * 40}


class FakeDeps:
    def __init__(self, drive: FakeDrive, pins: Any) -> None:
        self.drive = drive
        self.pins = pins
        self.clock = FixedClock("2026-09-27T10:00:00Z")


def _pin_state(manifest_sha: str) -> PinState:
    return PinState(
        repo="agora", repo_uuid=UUID, refs={"refs/heads/main": "1" * 40},
        manifest_sha256=manifest_sha, prev_manifest_sha256=None,
        active_bundles=(BUNDLE,), removed_bundles=frozenset(),
        annex_keys=frozenset(), promoted_at="2026-09-27T09:00:00.000Z", run_id="1")


def _seed(drive: FakeDrive) -> tuple[str, str, str]:
    prefix = drive.seed_folder("prefix")
    mid = drive.seed_file(prefix, MANIFEST, f"{BUNDLE}\n".encode())
    bid = drive.seed_file(prefix, BUNDLE, b"bundle")
    FakeCfg.prefix_folder_id = prefix
    return prefix, mid, bid


def _admin(drive: FakeDrive, tmp_path: Path) -> AdminDeps:
    return AdminDeps(drive=drive, clock=FixedClock("2026-09-27T10:00:00Z"),
                     workdir=tmp_path / "work", repo="agora", repo_uuid=UUID,
                     prefix_folder_id=FakeCfg.prefix_folder_id)


def _push_ok(git: FakeGit):
    def _p() -> dict[str, str]:
        git.calls += ["copy", "push"]
        return git.local_refs()
    return _p


def _pin_ok(manifest_sha: str = "b" * 64):
    def _rebuild() -> PinState:
        return _pin_state(manifest_sha)
    return _rebuild


def _gh_fake(*, runs: list[dict] | None = None):
    state = {"enabled": True, "runs": list(runs or [])}

    def _runner(cmd: list[str]) -> str:
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
            return json.dumps([r for r in state["runs"] if r["status"] == status])
        raise AssertionError(f"未預期的 gh 命令: {cmd}")

    return GitHubAdmin("owner/repo", runner=_runner), state


# ------------------------------------------------- M4-1：旗標帶 op 與 state


def test_m4_flag_carries_op_and_state() -> None:
    """上鎖寫入的旗標帶操作 id（每次上鎖都不同）與 state=active。"""
    pins = MemoryPinFiles()
    gh, _state = _gh_fake()
    with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                   reason="erase"):
        flag = read_maintenance(pins, "agora")
        assert flag is not None
        assert flag.reason == "erase"
        assert flag.state == "active"
        assert flag.op, "操作 id 不可為空"
        first_op = flag.op
    with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                   reason="erase"):
        flag2 = read_maintenance(pins, "agora")
        assert flag2 is not None and flag2.op != first_op


def test_m4_old_flag_without_state_is_treated_as_active() -> None:
    """M4 之前的舊旗標（沒有 op／state 欄位）視為 active：照樣擋提交流程，
    但不允許在它裡面做事。"""
    assert parse_maintenance(
        json.dumps({"reason": "erase", "at": "t", "by": "admin"})) == MaintenanceFlag(
            reason="erase", at="t", by="admin", op="", state="active")


def test_m4_body_failure_marks_flag_aborted() -> None:
    """with 區塊內出錯 → 旗標保留，但狀態變成 aborted（op 與原因不變），
    中止處理（init-pin）才允許在它裡面做；workflow 維持停用。"""
    pins = MemoryPinFiles()
    gh, state = _gh_fake()
    with pytest.raises(RuntimeError):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="erase"):
            raise RuntimeError("push 失敗")
    flag = read_maintenance(pins, "agora")
    assert flag is not None
    assert flag.state == ABORTED_STATE
    assert flag.reason == "erase" and flag.op
    assert state["enabled"] is False


def test_m4_precheck_failure_marks_flag_aborted() -> None:
    """開鎖時的預檢不符（遠端被動過）→ 同樣標成 aborted 等人查，
    而不是留在 active（會讓人以為有操作正在跑）。"""
    from datetime import timedelta

    pins = MemoryPinFiles()
    gh, _state = _gh_fake()

    def _bad() -> None:
        raise AdminError("遠端 manifest 與正式釘選值不符")

    with pytest.raises(AdminError, match="不符"):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="rollback", timeout=timedelta(0),
                       poll=timedelta(0), precheck=_bad):
            pass
    flag = read_maintenance(pins, "agora")
    assert flag is not None and flag.state == ABORTED_STATE


def test_m4_active_flag_refuses_reuse_even_for_init_pin() -> None:
    """另一位管理者正在進行的操作（active）→ init-pin 也不許插隊。"""
    pins = MemoryPinFiles()
    gh, _state = _gh_fake()
    with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                   reason="erase"):
        with pytest.raises(AdminError, match="不是 runbook 預期"):
            with admin_lock_if_needed(
                    repo="agora", pins=pins, gh=gh,
                    workflow="committer.yml", reason="init-pin"):
                pass
    # 自己的鎖正常結束：旗標清掉
    assert read_maintenance(pins, "agora") is None


# ------------------------------- M4-2：init-pin 的 precheck 用這份設定的欄位


def test_m4_init_pin_precheck_uses_the_config_fields(monkeypatch) -> None:
    """`init-pin --confirm` 的 precheck 讀的是**這份設定**的前綴與 uuid。

    ADR 0009：鎖與 precheck 針對的永遠是設定檔描述的那一個實體（期 1 是 Agora）。
    """
    import aistorage.admin.lock as admin_lock
    import aistorage.admin.remote as admin_remote
    import aistorage.committer.__main__ as committer_main

    pins = MemoryPinFiles()
    gh, _state = _gh_fake()
    monkeypatch.setattr(admin_lock, "GitPinFiles", lambda *a, **k: pins)
    monkeypatch.setattr(admin_lock, "GitHubAdmin", lambda *a, **k: gh)
    # init-pin 走的是 AdminLock 裡的 promote（maintenance_ok=True）
    seen_flags: list[bool] = []
    monkeypatch.setattr(committer_main, "init_pin_cli",
                        lambda cfg, deps, *, confirm=False, maintenance_ok=False:
                        seen_flags.append(maintenance_ok))

    seen: dict[str, str] = {}

    def _fake_read(drive: Any, prefix_folder_id: str, repo_uuid: str, *,
                   allow_missing: bool) -> str | None:
        seen["prefix"] = prefix_folder_id
        seen["uuid"] = repo_uuid
        return None

    monkeypatch.setattr(admin_remote, "read_remote_manifest_sha256", _fake_read)

    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="prefix-agora", quarantine_folder_id="q",
        identity_registry_path="config/identity.json",
        github_repository="owner/repo")
    committer_main._init_pin_under_lock(cfg, FakeDeps(FakeDrive(), None))
    assert seen == {"prefix": "prefix-agora", "uuid": UUID}, seen
    assert seen_flags == [True], "在自己的鎖裡 promote，旗標是預期的"
    # 旗標也掛在這個 repo 名稱上（管理操作與提交流程講的是同一個 repo）
    assert read_maintenance(pins, "agora") is None


# --------------------------------- M4-3：有刪檔時重讀必須是 None


def test_m4_swap_aborts_when_manifest_reappears_after_delete(
        tmp_path: Path, monkeypatch) -> None:
    """抹除（有刪遠端檔案）：push 前重讀到新的主 manifest → 中止，不 push。

    主 manifest 已由本輪刪掉，這時出現的只能是別人（另一輪提交流程或另一個
    管理操作）在抹除期間 push 的；用 force push 蓋過去會丟掉別人的東西。
    """
    drive = FakeDrive()
    prefix, mid, bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state("a" * 64)))
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))

    real_find = drive.find_by_name
    calls: list[int] = []

    def find_by_name(parent_id: str, name: str):
        if name == MANIFEST:
            calls.append(1)
            if len(calls) == 2:
                # 刪除之後、push 之前，有人推了一個新的主 manifest
                drive.seed_file(prefix, MANIFEST, b"someone pushed during erase\n")
        return real_find(parent_id, name)

    drive.find_by_name = find_by_name  # type: ignore[method-assign]
    git = FakeGit()
    with pytest.raises(SwapAborted) as excinfo:
        swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                    git=git, repo_dir=repo,
                    delete_groups=[DeleteGroup("repo", (mid, bid), (prefix,))],
                    force_push=True, config_path=None, push_fn=_push_ok(git),
                    pin_rebuild_fn=_pin_ok())
    assert excinfo.value.report.steps[-1].name == "recheck-remote"
    assert git.calls == [], "中止之後不得還有 push"
    assert "與 swap 開始時不同" in str(excinfo.value)


def test_m4_swap_allows_unchanged_manifest_with_delete_groups(
        tmp_path: Path, monkeypatch) -> None:
    """有刪遠端檔案、但主 manifest 沒被本輪動到（與開始時完全相同）→ 放行。

    真正的抹除一定刪掉主 manifest（plan_erase 收全部 GITMANIFEST），這裡覆蓋
    的是「只刪其他類別」的邊界：遠端沒被別人動過就沒理由中止。
    """
    drive = FakeDrive()
    _prefix, mid, bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state("a" * 64)))
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    git = FakeGit()
    # 只刪 bundle，不動主 manifest：重讀與開始時相同 → 放行到 push
    report = swap_remote(
        admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps, git=git,
        repo_dir=repo, delete_groups=[DeleteGroup("repo", (bid,), (FakeCfg.prefix_folder_id,))],
        force_push=True, config_path=None, push_fn=_push_ok(git),
        pin_rebuild_fn=_pin_ok())
    assert report.ok
    assert report.remote_manifest_before_push == report.remote_manifest_start


# ----------------------------------------- M4-4：不再預設 GitHub repo


def test_m4_admin_lock_requires_github_repository(monkeypatch) -> None:
    """設定檔沒有 github_repository → 直接 raise，不再偷用預設 repo。"""
    from argparse import Namespace

    import aistorage.admin.lock as admin_lock
    from aistorage.admin.__main__ import _admin_lock

    monkeypatch.setattr(admin_lock, "GitPinFiles",
                        lambda *a, **k: MemoryPinFiles())
    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json")
    assert cfg.github_repository == ""
    deps = FakeDeps(FakeDrive(), MemoryPinStore(initial_state=_pin_state("a" * 64)))
    with pytest.raises(AdminError, match="github_repository"):
        _admin_lock(Namespace(workflow=None), cfg, deps,
                    reason="x", strict_precheck=False)


def test_m4_unlock_requires_gh_repo() -> None:
    """`unlock` 沒有 `--gh-repo` → 直接 raise（以前會去動預設 repo）。"""
    from argparse import Namespace

    from aistorage.admin.__main__ import _cmd_unlock

    args = Namespace(pin_repo="file:///tmp/pin", key=None, repo="agora",
                     workdir=None, gh_repo="", workflow=None, confirm=True)
    with pytest.raises(AdminError, match="--gh-repo"):
        _cmd_unlock(args)


def test_m4_init_pin_requires_github_repository() -> None:
    """`init-pin --confirm` 沒有 github_repository → 直接 raise。"""
    import aistorage.committer.__main__ as committer_main

    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json")
    with pytest.raises(RuntimeError, match="github_repository"):
        committer_main._init_pin_under_lock(cfg, FakeDeps(FakeDrive(), None))
