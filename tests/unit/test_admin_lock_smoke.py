"""6.5 AdminLock 的冒煙測試（只寫冒煙）。"""

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from aistorage.admin.lock import (
    AdminLock,
    GitHubAdmin,
    GitPinFiles,
    MaintenanceFlag,
    MemoryPinFiles,
    maintenance_relpath,
    parse_maintenance,
    read_maintenance,
)
from aistorage.admin import AdminError
from aistorage.clock import FixedClock


def _gh_fake(*, runs: list[dict] | None = None):
    state = {"enabled": True, "runs": list(runs or [])}

    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "disable"]:
            state["enabled"] = False
            return ""
        if cmd[1:3] == ["workflow", "enable"]:
            state["enabled"] = True
            return ""
        if cmd[1:3] == ["workflow", "list"]:
            return json.dumps([{"path": ".github/workflows/committer.yml",
                                "state": "active" if state["enabled"] else "disabled_manually"}])
        if cmd[1:3] == ["run", "list"]:
            status = cmd[cmd.index("--status") + 1]
            return json.dumps([r for r in state["runs"] if r["status"] == status])
        raise AssertionError(f"未預期的 gh 命令: {cmd}")

    return runner, state


def test_maintenance_flag_roundtrip() -> None:
    pins = MemoryPinFiles()
    assert read_maintenance(pins, "agora") is None
    pins.write_text(maintenance_relpath("agora"),
                    json.dumps({"reason": "erase", "at": "t", "by": "admin"}), "m")
    flag = read_maintenance(pins, "agora")
    assert flag == MaintenanceFlag(reason="erase", at="t", by="admin")
    with pytest.raises(AdminError):
        parse_maintenance("{broken")


def test_lock_enter_exit_order() -> None:
    pins = MemoryPinFiles()
    runner, state = _gh_fake(runs=[])
    gh = GitHubAdmin("owner/repo", runner=runner)
    prechecks: list[str] = []
    clock = FixedClock("2026-09-27T10:00:00Z")
    with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                   reason="erase-test", clock=clock,
                   timeout=timedelta(0), poll=timedelta(0),
                   precheck=lambda: prechecks.append("precheck")):
        assert read_maintenance(pins, "agora") is not None
        assert state["enabled"] is False
    assert read_maintenance(pins, "agora") is None
    assert state["enabled"] is True
    assert prechecks == ["precheck"]
    kinds = [c[1:3] for c in gh.calls]
    assert kinds[0] == ["workflow", "disable"]
    assert kinds[-1] == ["workflow", "enable"]


def test_lock_waits_for_runs_then_times_out() -> None:
    """逾時屬於「短暫擁塞」：把自己��旗標清掉、workflow 重開。"""
    pins = MemoryPinFiles()
    runner, state = _gh_fake(runs=[{"databaseId": 1, "status": "in_progress"}])
    gh = GitHubAdmin("owner/repo", runner=runner)
    with pytest.raises(AdminError):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="x", timeout=timedelta(0), poll=timedelta(0)):
            pass
    assert read_maintenance(pins, "agora") is None
    assert state["enabled"] is True


def test_lock_error_in_with_keeps_lock_and_workflow_disabled() -> None:
    """H5：with 區塊內出錯 → 保留旗標、維持 workflow 停用，並提示人工處理。"""
    pins = MemoryPinFiles()
    runner, state = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    notices: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="erase", notify=notices.append):
            assert read_maintenance(pins, "agora") is not None
            raise RuntimeError("boom")
    flag = read_maintenance(pins, "agora")
    assert flag is not None and flag.reason == "erase"
    assert state["enabled"] is False
    assert any("unlock --confirm" in n for n in notices)
    assert any("維護中止" in n for n in notices)
    # 手動解除之後才會重開 workflow
    lock = AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                     reason="x", notify=notices.append)
    assert lock.unlock() is not None
    assert read_maintenance(pins, "agora") is None
    assert state["enabled"] is True


def test_lock_workflow_disable_failure_keeps_flag_with_hint() -> None:
    """L3：停用 workflow 失敗 → 旗標留著，但訊息要告訴人怎麼解除。"""
    pins = MemoryPinFiles()

    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "disable"]:
            raise RuntimeError("gh exploded")
        return ""

    gh = GitHubAdmin("owner/repo", runner=runner)
    notices: list[str] = []
    with pytest.raises(AdminError):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="erase", notify=notices.append):
            pass
    assert read_maintenance(pins, "agora") is not None
    assert any("unlock --confirm" in n for n in notices)


def test_unlock_is_idempotent() -> None:
    pins = MemoryPinFiles()
    runner, state = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    lock = AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                     reason="x")
    assert lock.unlock() is None      # 沒有旗標也視為成功（冪等）
    assert state["enabled"] is True


def test_workflow_enabled_uses_all_flag() -> None:
    """`gh workflow list` 少了 `--all` 就查不到**停用中**的 workflow。

    2026-09-30 的真 GitHub 實測：整個 repo 只有一個 workflow 而它被停用時，
    預設清單回空陣列。少了 `--all`，6.3 健康檢查在「被停用」這個最該被找出來的
    情況反而查不到、印「未知（查不到狀態）」。這裡把 `--all` 釘住。
    """
    pins = MemoryPinFiles()
    runner, state = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    assert gh.workflow_enabled("committer.yml") is True
    state["enabled"] = False
    assert gh.workflow_enabled("committer.yml") is False
    list_calls = [c for c in gh.calls if c[1:3] == ["workflow", "list"]]
    assert list_calls, "應該走 gh workflow list"
    assert "--all" in list_calls[0], list_calls[0]


def test_workflow_enabled_accepts_filename_or_path() -> None:
    """檔名與完整路徑都要認得（`admin unlock --workflow` 是自由字串）。"""
    runner, _ = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    for name in ("committer.yml",
                 ".github/workflows/committer.yml",
                 "./.github/workflows/committer.yml"):
        assert gh.workflow_enabled(name) is True, name


def test_workflow_enabled_unknown_workflow_raises() -> None:
    """查不到要報錯，不能回 False——否則健康檢查會對不存在的 workflow 發假警報。"""
    runner, _ = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    with pytest.raises(AdminError, match="nope.yml"):
        gh.workflow_enabled("nope.yml")


def test_lock_precheck_failure_aborts() -> None:
    """預檢不符（可能真有問題）→ 保持暫停：旗標留著、workflow 保持停用，等人來查。"""
    pins = MemoryPinFiles()
    runner, state = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)

    def precheck() -> None:
        raise AdminError("manifest 不符")

    with pytest.raises(AdminError, match="manifest 不符"):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="x", precheck=precheck):
            pass
    assert read_maintenance(pins, "agora") is not None
    assert state["enabled"] is False


def test_lock_refuses_double_lock() -> None:
    pins = MemoryPinFiles({
        maintenance_relpath("agora"): json.dumps(
            {"reason": "other", "at": "t", "by": "admin"})})
    runner, _ = _gh_fake()
    gh = GitHubAdmin("owner/repo", runner=runner)
    with pytest.raises(AdminError):
        with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                       reason="x"):
            pass


def test_git_pin_files_local_roundtrip(tmp_path: Path) -> None:
    """file:// pin repo 不需要 SSH 即可讀寫（管理者在 Mac 上用同一條路徑）。"""
    remote = tmp_path / "pin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    (seed / ".pin").mkdir()
    (seed / ".pin" / "keep.txt").write_text("keep")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    pins = GitPinFiles(f"file://{remote}", tmp_path / "work")
    assert pins.read_text(".pin/missing.json") is None
    pins.write_text(".pin/agora.maintenance", '{"a": 1}', "test write")
    assert pins.read_text(".pin/agora.maintenance") == '{"a": 1}'
    pins.delete(".pin/agora.maintenance", "test delete")
    assert pins.read_text(".pin/agora.maintenance") is None
    # 遠端真的被更新（第二個 clone 看得到）
    seed2 = tmp_path / "seed2"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed2)], check=True)
    assert not (seed2 / ".pin" / "agora.maintenance").exists()


def test_git_pin_files_remote_requires_key(tmp_path: Path) -> None:
    with pytest.raises(AdminError, match="key_path"):
        GitPinFiles("git@github.com:o/r.git", tmp_path / "work")


def test_git_pin_files_scp_url_is_remote_not_local(tmp_path: Path) -> None:
    """L2：沒有 key 的 scp 形式 URL 不得被當成本機路徑。"""
    with pytest.raises(AdminError):
        GitPinFiles("github-alias:o/pin.git", tmp_path / "work")


def test_git_pin_files_rejects_project_workdir() -> None:
    """M8：pin repo 的工作目錄不得在專案 repo 之內。"""
    from aistorage.safety import UnsafeWorkdirError, project_repo_toplevel

    project = project_repo_toplevel()
    if project is None:
        pytest.skip("判斷不出專案 repo")
    with pytest.raises(UnsafeWorkdirError):
        GitPinFiles("file:///tmp/whatever-pin.git", project / "docs")
