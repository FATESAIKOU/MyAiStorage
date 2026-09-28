"""6.5 錯開的單元測試：管理操作與提交流程之間的門擋。

驗的是五件事（tasks 6.5）：
1. 停用的是**設定檔指的那一個** workflow（原本散落硬編碼成不存在的 `commit.yaml`，
   `gh workflow disable` 會直接失敗，於是整個管理操作根本跑不起來）；
2. 確認沒有執行中的 run（逾時就中止，且把自己的旗標清掉）；
3. push 前重讀遠端 manifest（`swap_remote` 的 `recheck-remote` 步驟）；
4. 重建釘選值期間暫停清掃（提交流程看到 pin repo 的旗標就整輪不動——
   真正的鎖是旗標，不是 workflow 的啟用狀態）；
5. 做完再恢復（正常結束才解鎖；出錯保留鎖）。

「管理操作與一輪真的 committer 同時跑、而且用真的 pin-test repo」的驗收在
`tests/integration/test_admin_stagger_integration.py`；這裡全部用假對象。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.lock import (
    DEFAULT_WORKFLOW,
    AdminLock,
    GitHubAdmin,
    MemoryPinFiles,
    admin_lock_if_needed,
    maintenance_relpath,
    read_maintenance,
)
from aistorage.admin.remote import (
    DeleteGroup,
    SwapAborted,
    read_remote_manifest_sha256,
    swap_remote,
)
from aistorage.clock import FixedClock
from aistorage.committer.config import CommitterConfig
from aistorage.drive.fake import FakeDrive
from aistorage.integrity.pin import MemoryPinStore, PinState

UUID = "11111111-2222-3333-4444-555555555555"
BUNDLE = f"GITBUNDLE-s10--{UUID}-{'0' * 64}"
MANIFEST = f"GITMANIFEST--{UUID}"
MANIFEST_SHA = "a" * 64


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


def _pin_state(manifest_sha: str = MANIFEST_SHA) -> PinState:
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


def _gh_fake(*, runs: list[dict] | None = None, enabled: bool = True):
    """假 GitHub：記錄呼叫，並且可以被「住民」從另一邊重新啟用。"""
    state = {"enabled": enabled, "runs": list(runs or [])}
    gh = GitHubAdmin("owner/repo", runner=lambda cmd: _gh_runner(cmd, state))
    return gh, state


def _gh_runner(cmd: list[str], state: dict) -> str:
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


# ---------------------------------------------- 1. workflow 名稱來自設定檔


def test_default_workflow_matches_the_workflow_file() -> None:
    """預設值必須真的指到 repo 裡的那個 workflow 檔（否則 gh 會說找不到）。"""
    from aistorage.committer.config import DEFAULT_COMMITTER_WORKFLOW

    repo_root = Path(__file__).resolve().parents[2]
    assert (repo_root / ".github" / "workflows" / DEFAULT_WORKFLOW).is_file(), \
        f"DEFAULT_WORKFLOW={DEFAULT_WORKFLOW!r} 對不到 repo 裡的 workflow 檔"
    # 兩處預設值（設定檔與 admin 側）不得各寫各的
    assert DEFAULT_COMMITTER_WORKFLOW == DEFAULT_WORKFLOW
    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json")
    assert cfg.committer_workflow == DEFAULT_WORKFLOW


def test_config_reads_committer_workflow(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "format": "aistorage.committer/v1", "repo": "agora", "repo_uuid": UUID,
        "repo_url": "drive://agora", "prefix_folder_id": "p", "quarantine_folder_id": "q",
        "identity_registry_path": "config/identity.json",
        "committer_workflow": "commit.yml",
    }), encoding="utf-8")
    assert CommitterConfig.load(path, env={}).committer_workflow == "commit.yml"


@pytest.mark.parametrize("bad", [".github/workflows/committer.yml", "committer", "x.txt"])
def test_config_rejects_workflow_path_or_extension(tmp_path: Path, bad: str) -> None:
    """名字寫成路徑或沒有副檔名時，錯誤要在載入設定時就出現，不要等到 gh 失敗。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "format": "aistorage.committer/v1", "repo": "agora", "repo_uuid": UUID,
        "repo_url": "drive://agora", "prefix_folder_id": "p", "quarantine_folder_id": "q",
        "identity_registry_path": "config/identity.json",
        "committer_workflow": bad,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="committer_workflow"):
        CommitterConfig.load(path, env={})


def test_config_empty_workflow_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "format": "aistorage.committer/v1", "repo": "agora", "repo_uuid": UUID,
        "repo_url": "drive://agora", "prefix_folder_id": "p", "quarantine_folder_id": "q",
        "identity_registry_path": "config/identity.json",
        "committer_workflow": "",
    }), encoding="utf-8")
    assert CommitterConfig.load(path, env={}).committer_workflow == DEFAULT_WORKFLOW


def test_admin_lock_uses_workflow_from_args_and_config(monkeypatch) -> None:
    from argparse import Namespace

    import aistorage.admin.lock as admin_lock
    from aistorage.admin.__main__ import _admin_lock

    monkeypatch.setattr(admin_lock, "GitPinFiles",
                        lambda *a, **k: MemoryPinFiles())
    gh, _state = _gh_fake()
    monkeypatch.setattr(admin_lock, "GitHubAdmin", lambda *a, **k: gh)
    deps = FakeDeps(FakeDrive(), MemoryPinStore(initial_state=_pin_state()))
    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json",
        github_repository="owner/repo", committer_workflow="commit.yml")
    with _admin_lock(Namespace(workflow=None), cfg, deps,
                     reason="x", strict_precheck=False):
        assert gh.calls[0][1:3] == ["workflow", "disable"]
        assert "commit.yml" in gh.calls[0]
    with _admin_lock(Namespace(workflow="other.yml"), cfg, deps,
                     reason="x", strict_precheck=False):
        assert "other.yml" in gh.calls[-1]


# ------------------------------------- 2. 確認沒有執行中的 run（＋做完再恢復）


def test_lock_waits_for_runs_and_restores_on_success() -> None:
    pins = MemoryPinFiles()
    gh, state = _gh_fake(runs=[])
    with AdminLock(repo="agora", pins=pins, gh=gh, workflow=DEFAULT_WORKFLOW,
                   reason="rollback"):
        assert read_maintenance(pins, "agora") is not None
        assert state["enabled"] is False
    assert read_maintenance(pins, "agora") is None
    assert state["enabled"] is True
    # 確認過「沒有執行中／排隊中的 run」才進來
    listed = [c for c in gh.calls if c[1:3] == ["run", "list"]]
    assert {c[c.index("--status") + 1] for c in listed} == {"in_progress", "queued"}


def test_lock_gives_up_when_a_run_is_still_going() -> None:
    from datetime import timedelta

    pins = MemoryPinFiles()
    gh, state = _gh_fake(runs=[{"databaseId": 7, "status": "queued"}])
    with pytest.raises(AdminError, match="逾時"):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow=DEFAULT_WORKFLOW,
                       reason="erase", timeout=timedelta(0), poll=timedelta(0)):
            pass
    assert read_maintenance(pins, "agora") is None, "逾時要清掉自己的旗標"
    assert state["enabled"] is True


def test_lock_keeps_flag_when_body_fails() -> None:
    pins = MemoryPinFiles()
    gh, state = _gh_fake()
    with pytest.raises(RuntimeError):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow=DEFAULT_WORKFLOW,
                       reason="erase"):
            raise RuntimeError("push 失敗")
    assert read_maintenance(pins, "agora") is not None
    assert state["enabled"] is False, "失敗後不能讓提交流程回來"


# ------------------------------------------ 3. push 前重讀遠端 manifest


def test_read_remote_manifest_uses_only_the_manifest_name() -> None:
    drive = FakeDrive()
    _seed(drive)
    drive.seed_file(FakeCfg.prefix_folder_id, f"{MANIFEST}.bak", b"bak")
    drive.seed_file(FakeCfg.prefix_folder_id, "notes.txt", b"x")
    sha = read_remote_manifest_sha256(
        drive, FakeCfg.prefix_folder_id, UUID, allow_missing=False)
    assert sha and len(sha) == 64


def test_read_remote_manifest_rejects_ambiguous_state() -> None:
    drive = FakeDrive()
    prefix, _mid, _bid = _seed(drive)
    drive.seed_file(prefix, MANIFEST, b"another", file_id="file_dup")
    with pytest.raises(AdminError, match="找到 2 個"):
        read_remote_manifest_sha256(drive, prefix, UUID, allow_missing=True)


def test_read_remote_manifest_missing_is_refused_unless_allowed() -> None:
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    FakeCfg.prefix_folder_id = prefix
    with pytest.raises(AdminError, match="不存在"):
        read_remote_manifest_sha256(drive, prefix, UUID, allow_missing=False)
    assert read_remote_manifest_sha256(drive, prefix, UUID, allow_missing=True) is None


def test_swap_rereads_remote_manifest_before_push(tmp_path: Path, monkeypatch) -> None:
    """回滾（不刪遠端）時，push 前的重讀必須與 swap 開始時相同。"""
    drive = FakeDrive()
    _prefix, _mid, _bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state()))
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    git = FakeGit()
    report = swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                         git=git, repo_dir=repo, delete_groups=(), force_push=False,
                         config_path=None, push_fn=_push_ok(git),
                         pin_rebuild_fn=_pin_ok())
    assert [s.name for s in report.steps][:2] == ["delete-remote", "recheck-remote"]
    assert report.remote_manifest_start == report.remote_manifest_before_push
    # 重讀發生在 push **之前**
    names = [s.name for s in report.steps]
    assert names.index("recheck-remote") < names.index("push")
    assert git.calls == ["copy", "push"]


def test_swap_aborts_when_remote_moved_under_it(tmp_path: Path, monkeypatch) -> None:
    """有人在管理操作期間把遠端 manifest 換掉 → 中止，不覆蓋。"""
    drive = FakeDrive()
    prefix, _mid, _bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state()))
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))

    calls: list[int] = []
    real_find = drive.find_by_name

    def find_by_name(parent_id: str, name: str):
        # 第一次（swap 開始時）之後，模擬另一輪提交流程寫了新的 manifest
        if name == MANIFEST:
            calls.append(1)
            if len(calls) == 2:
                drive.seed_file(prefix, MANIFEST, b"someone else got here first\n")
        return real_find(parent_id, name)

    drive.find_by_name = find_by_name  # type: ignore[method-assign]
    git = FakeGit()
    with pytest.raises(SwapAborted) as excinfo:
        swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                    git=git, repo_dir=repo, delete_groups=(), force_push=False,
                    config_path=None, push_fn=_push_ok(git),
                    pin_rebuild_fn=_pin_ok())
    assert excinfo.value.report.steps[-1].name == "recheck-remote"
    assert git.calls == [], "中止之後不得還有 push"
    assert "swap-finish" in str(excinfo.value) or "釘選值" in str(excinfo.value)


def test_swap_allows_missing_manifest_when_it_deleted_it(tmp_path: Path, monkeypatch) -> None:
    """抹除：主 manifest 是這一輪自己刪掉的，「不存在」是預期結果。"""
    drive = FakeDrive()
    prefix, mid, bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state()))
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    git = FakeGit()

    def push_then_reupload() -> dict[str, str]:
        result = _push_ok(git)()
        drive.seed_file(prefix, MANIFEST, f"{BUNDLE}\n".encode())
        return result

    report = swap_remote(
        admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps, git=git, repo_dir=repo,
        delete_groups=[DeleteGroup("repo", (mid, bid), (prefix,))],
        force_push=True, config_path=None, push_fn=push_then_reupload,
        pin_rebuild_fn=_pin_ok())
    assert report.remote_manifest_start is not None
    assert report.remote_manifest_before_push is None, "主 manifest 是本輪自己刪的"
    assert report.ok


# ------------------------------- 預檢：遠端 manifest 等於正式釘選值（開鎖時）


def test_lock_precheck_compares_remote_manifest_to_pin(tmp_path: Path) -> None:
    from aistorage.admin.__main__ import _manifest_precheck

    drive = FakeDrive()
    _prefix, _mid, _bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state(_manifest_of(drive))))
    check = _manifest_precheck(FakeCfg, deps, strict=True)
    check()  # 相符就放行

    deps.pins = MemoryPinStore(initial_state=_pin_state("f" * 64))
    with pytest.raises(AdminError, match="與正式釘選值"):
        check()


def test_lenient_precheck_only_refuses_ambiguous_state(tmp_path: Path) -> None:
    """swap-finish／init-pin 的寬鬆預檢：遠端本來就不該等於釘選值。"""
    from aistorage.admin.__main__ import _manifest_precheck

    drive = FakeDrive()
    prefix, _mid, _bid = _seed(drive)
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state("f" * 64)))
    _manifest_precheck(FakeCfg, deps, strict=False)()
    drive.seed_file(prefix, MANIFEST, b"dup", file_id="file_dup")
    with pytest.raises(AdminError, match="找到 2 個"):
        _manifest_precheck(FakeCfg, deps, strict=False)()


def _manifest_of(drive: FakeDrive) -> str:
    from aistorage.drive.model import DriveFile

    found = drive.find_by_name(FakeCfg.prefix_folder_id, MANIFEST)
    assert len(found) == 1
    return found[0].sha256 or ""


# ------------- 4. 重建釘選值期間暫停清掃（真正的鎖是 pin repo 的旗標）


def test_resident_reenabling_workflow_does_not_defeat_the_lock() -> None:
    """1.6：住民的 PAT 可以重新啟用 workflow。真正的鎖是 pin repo 的旗標，
    所以「workflow 被重新啟用」之後，提交流程看到旗標仍然整輪不動。"""
    from aistorage.committer.run import RunReport, _check_maintenance

    pins = MemoryPinFiles()
    gh, state = _gh_fake()

    class _Pins(MemoryPinStore):
        """提交流程那一側看到的 pin repo（和 admin 用同一份檔案）。"""

        def read_text(self, relpath: str) -> str | None:
            return pins.read_text(relpath)

    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json")
    deps = FakeDeps(FakeDrive(), _Pins(initial_state=_pin_state()))

    with AdminLock(repo="agora", pins=pins, gh=gh, workflow=DEFAULT_WORKFLOW,
                   reason="erase"):
        # 住民在管理操作期間重新啟用 workflow（1.6 實測：PAT 做得到）
        gh.set_workflow_enabled(DEFAULT_WORKFLOW, True)
        assert state["enabled"] is True
        report = RunReport(run_id="r1")
        assert _check_maintenance(cfg, deps, report) is True
        assert report.aborted_at is None      # 不是中止，是「因為維護中而結束」
        assert report.maintenance == "active"
    # 管理操作結束：旗標清掉、workflow 重開（這時提交流程才會真的跑）
    assert read_maintenance(pins, "agora") is None
    report = RunReport(run_id="r2")
    assert _check_maintenance(cfg, deps, report) is False
    assert report.maintenance is None


def test_lock_if_needed_locks_when_no_flag() -> None:
    pins = MemoryPinFiles()
    gh, _state = _gh_fake()
    with admin_lock_if_needed(repo="agora", pins=pins, gh=gh,
                              workflow=DEFAULT_WORKFLOW, reason="init-pin") as lock:
        assert lock is not None
        assert read_maintenance(pins, "agora") is not None
    assert read_maintenance(pins, "agora") is None


def test_lock_if_needed_reuses_only_aborted_flag() -> None:
    """M4：中止處理流程：只有 `aborted` 的旗標才允許在既有的鎖裡做事。

    另一位管理者正在進行的操作（`active`）、或狀態不明的舊旗標（沒有 state
    欄位，視為 `active`），一律拒絕，避免兩個管理操作並行。
    """
    pins = MemoryPinFiles()
    gh, _state = _gh_fake()
    # 進行中的鎖：拒絕
    pins.write_text(maintenance_relpath("agora"),
                    json.dumps({"reason": "erase", "at": "t", "by": "admin",
                                "op": "op-live", "state": "active"}), "m")
    with pytest.raises(AdminError, match="不是 runbook 預期"):
        with admin_lock_if_needed(repo="agora", pins=pins, gh=gh,
                                  workflow=DEFAULT_WORKFLOW, reason="init-pin"):
            pass
    # 狀態不明的舊旗標（M4 之前寫的，沒有 state）：視為 active，一樣拒絕
    pins.write_text(maintenance_relpath("agora"),
                    json.dumps({"reason": "erase", "at": "t", "by": "admin"}), "m")
    with pytest.raises(AdminError, match="不是 runbook 預期"):
        with admin_lock_if_needed(repo="agora", pins=pins, gh=gh,
                                  workflow=DEFAULT_WORKFLOW, reason="init-pin"):
            pass
    assert gh.calls == [], "拒絕時不得動 workflow"
    # 中止處理中的鎖：放行，且不得把別人的旗標清掉、不得動 workflow
    pins.write_text(maintenance_relpath("agora"),
                    json.dumps({"reason": "erase", "at": "t", "by": "admin",
                                "op": "op-old", "state": "aborted"}), "m")
    with admin_lock_if_needed(repo="agora", pins=pins, gh=gh,
                              workflow=DEFAULT_WORKFLOW, reason="init-pin") as lock:
        assert lock is None
        assert gh.calls == [], "既有的鎖不該再動 workflow"
    assert read_maintenance(pins, "agora") is not None, "不得把別人的旗標清掉"


def test_init_pin_confirm_takes_the_lock(tmp_path: Path, monkeypatch) -> None:
    """`python -m aistorage.committer init-pin --confirm`（6.4 的重建釘選值）
    也要走錯開：停用 workflow、等沒有執行中的 run、重讀遠端 manifest。"""
    import aistorage.committer.__main__ as committer_main
    import aistorage.admin.lock as admin_lock

    pins = MemoryPinFiles()
    gh, state = _gh_fake()

    class _Pins(admin_lock.MemoryPinFiles):  # type: ignore[misc]
        def __init__(self, *_a: Any, **_k: Any) -> None:
            super().__init__()

    monkeypatch.setattr(admin_lock, "GitPinFiles", lambda *a, **k: pins)
    monkeypatch.setattr(admin_lock, "GitHubAdmin", lambda *a, **k: gh)
    seen: list[bool] = []
    # init-pin 走的是 AdminLock 裡的 promote（maintenance_ok=True）
    monkeypatch.setattr(
        committer_main, "init_pin_cli",
        lambda cfg, deps, *, confirm=False, maintenance_ok=False: seen.append(confirm))

    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json",
        github_repository="owner/repo")
    drive = FakeDrive()
    drive.seed_folder("p")
    deps = FakeDeps(drive, MemoryPinStore(initial_state=_pin_state()))

    committer_main._init_pin_under_lock(cfg, deps)

    assert seen == [True]
    assert gh.calls[0][1:3] == ["workflow", "disable"]
    assert read_maintenance(pins, "agora") is None, "做完要恢復"
    assert state["enabled"] is True
