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
    # 停用前先查狀態（冪等：已經是停用的就不送 disable，見
    # test_disable_is_idempotent_when_already_disabled）
    assert kinds[0] == ["workflow", "list"]
    assert ["workflow", "disable"] in kinds
    # disable 必須在等安靜（run list）之前、enable 必須在最後
    assert kinds.index(["workflow", "disable"]) < kinds.index(["run", "list"])
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


# ---------------------------------------------------------------------------
# 6.5 收尾：`gh workflow disable/enable` 冪等（部署時 workflow 可能已經是停用的）
# ---------------------------------------------------------------------------


def _gh_fake_real_semantics(*, runs: list[dict] | None = None):
    """模仿真的 `gh`：對**已經是目標狀態**的 workflow，`disable` 會回 403。

    實測 gh 2.101.0：
        $ gh workflow disable committer.yml   # 已經是 disabled_manually
        failed to disable workflow: HTTP 403: Unable to disable a workflow that is not active.
    `enable` 則是冪等的（連按兩次 rc 都是 0）。
    """
    state = {"enabled": True, "runs": list(runs or []), "disable_attempts": 0}

    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "disable"]:
            state["disable_attempts"] += 1
            if not state["enabled"]:
                raise AdminError(
                    "failed to disable workflow: HTTP 403: Unable to disable a "
                    "workflow that is not active.")
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


def test_disable_is_idempotent_when_already_disabled() -> None:
    """workflow 已經是停用的時，`set_workflow_enabled(False)` 當成功、不送指令。

    這一點是 6.5 走真 GitHub 時會撞到的：管理操作開始時 workflow 可能已經停用
    （前一次中止沒清乾淨、人工先停用过）。修之前 `AdminLock.__enter__` 會因此走
    `_mark_aborted()`、把旗標留在 `aborted` 並中止整個操作——為了已經成立的事。
    """
    runner, state = _gh_fake_real_semantics()
    state["enabled"] = False                      # 一開始就已經停用
    gh = GitHubAdmin("owner/repo", runner=runner)

    gh.set_workflow_enabled("committer.yml", False)   # 不該拋

    assert state["enabled"] is False
    assert state["disable_attempts"] == 0, "已經是目標狀態就不該再送 disable"
    assert not [c for c in gh.calls if c[1:3] == ["workflow", "disable"]], gh.calls


def test_enable_is_idempotent_when_already_enabled() -> None:
    """對稱：已經是啟用的時 `set_workflow_enabled(True)` 也是冪等。"""
    runner, state = _gh_fake_real_semantics()
    state["enabled"] = True
    gh = GitHubAdmin("owner/repo", runner=runner)

    gh.set_workflow_enabled("committer.yml", True)

    assert state["enabled"] is True
    assert not [c for c in gh.calls if c[1:3] == ["workflow", "enable"]], gh.calls


def test_disable_still_runs_when_workflow_is_active() -> None:
    """正常情況下 disable 指令照送（冪等不能變成「什麼都不做」）。"""
    runner, state = _gh_fake_real_semantics()
    gh = GitHubAdmin("owner/repo", runner=runner)

    gh.set_workflow_enabled("committer.yml", False)

    assert state["enabled"] is False
    assert state["disable_attempts"] == 1


def test_lock_enter_succeeds_when_workflow_already_disabled() -> None:
    """端到端：workflow 已經停用時上鎖要成功，旗標不能變成 aborted。"""
    pins = MemoryPinFiles()
    runner, state = _gh_fake_real_semantics(runs=[])
    state["enabled"] = False
    gh = GitHubAdmin("owner/repo", runner=runner)
    clock = FixedClock("2026-09-30T10:00:00Z")
    notices: list[str] = []

    with AdminLock(repo="agora", pins=pins, gh=gh, workflow="committer.yml",
                   reason="erase", clock=clock, poll=timedelta(0),
                   precheck=lambda: None, notify=notices.append):
        assert read_maintenance(pins, "agora") is not None
        assert read_maintenance(pins, "agora").state == "active"
        assert notices == [], f"不該有中止通知：{notices}"

    assert read_maintenance(pins, "agora") is None
    assert state["enabled"] is True                   # 解鎖有把它開回來
    kinds = [c[1:3] for c in gh.calls]
    assert kinds[0] == ["workflow", "list"]           # 先查狀態
    assert ["workflow", "enable"] in kinds            # 最後有 enable（解鎖）
    assert ["workflow", "disable"] not in kinds       # 不需要 disable


def test_disable_state_race_is_treated_as_success() -> None:
    """查完狀態到送出指令之間被別人停用 → 指令報 403，但目標狀態已達成，算成功。"""
    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "list"]:
            return json.dumps([{"path": ".github/workflows/committer.yml",
                                "state": "active"}])
        if cmd[1:3] == ["workflow", "disable"]:
            raise AdminError("failed to disable workflow: HTTP 403: Unable to "
                             "disable a workflow that is not active.")
        raise AssertionError(f"未預期的 gh 命令: {cmd}")

    GitHubAdmin("owner/repo", runner=runner).set_workflow_enabled("committer.yml", False)


def test_other_gh_errors_are_not_swallowed() -> None:
    """冪等邏輯只認「已經不是 active」那一句，其他錯誤照舊往外拋。"""
    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "list"]:
            return json.dumps([{"path": ".github/workflows/committer.yml",
                                "state": "active"}])
        if cmd[1:3] == ["workflow", "disable"]:
            raise AdminError("failed to disable workflow: HTTP 404: Not Found")
        raise AssertionError(f"未預期的 gh 命令: {cmd}")

    with pytest.raises(AdminError, match="404"):
        GitHubAdmin("owner/repo", runner=runner).set_workflow_enabled("committer.yml", False)


def test_unknown_workflow_still_raises_on_disable() -> None:
    """repo 裡沒有這個 workflow 時，查狀態會失敗——不得因此假裝停用成功。"""
    def runner(cmd: list[str]) -> str:
        if cmd[1:3] == ["workflow", "list"]:
            return json.dumps([])
        if cmd[1:3] == ["workflow", "disable"]:
            raise AdminError("failed to disable workflow: HTTP 404: Not Found")
        raise AssertionError(f"未預期的 gh 命令: {cmd}")

    with pytest.raises(AdminError):
        GitHubAdmin("owner/repo", runner=runner).set_workflow_enabled("nope.yml", False)


# ---------------------------------------------------------------------------
# 6.5 收尾：完全空的 pin repo（沒有任何 commit）
# ---------------------------------------------------------------------------


def _empty_pin_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "empty-pin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    return remote


def test_empty_pin_repo_raises_with_actionable_message(tmp_path: Path) -> None:
    """空 pin repo（沒有任何 commit）要給清楚的訊息，不是 `git reset 失敗 (rc=128)`。

    實測：`git clone` 空 repo 會成功（rc=0，只有一句 warning），但沒有 `origin/HEAD`，
    所以第二次 `_ensure()` 的 `git reset --hard origin/HEAD` 會 rc=128
    「ambiguous argument 'origin/HEAD'」——對管理者毫無幫助。
    """
    remote = _empty_pin_repo(tmp_path)
    pins = GitPinFiles(f"file://{remote}", tmp_path / "work")

    with pytest.raises(AdminError) as exc:
        pins.read_text(".pin/agora.json")

    msg = str(exc.value)
    assert "還沒有任何 commit" in msg
    # 要告訴人怎麼辦：放一個初始 commit
    assert "README.md" in msg
    assert "git push" in msg
    # 要說明為什麼程式不自己來
    assert "ADR 0008" in msg


def test_empty_pin_repo_raises_on_first_clone_too(tmp_path: Path) -> None:
    """第一次 clone 就撞到空 repo 時也要擋（不能只靠第二次的 reset 發現）。"""
    remote = _empty_pin_repo(tmp_path)
    pins = GitPinFiles(f"file://{remote}", tmp_path / "work")
    # 直接再讀一次：第一次 _ensure() 是 clone（rc=0），不會發現
    with pytest.raises(AdminError, match="還沒有任何 commit"):
        pins.read_text(".pin/agora.json")


def test_empty_pin_repo_does_not_create_a_commit(tmp_path: Path) -> None:
    """保守選擇的關鍵：程式**不**自己去補那顆初始 commit。

    pin repo 是可信內容的唯一來源（ADR 0008），管理操作裡靜靜寫一個 commit 進去
    既難稽核也不好回退。所以塞完之後遠端必須**還是空的**。
    """
    remote = _empty_pin_repo(tmp_path)
    pins = GitPinFiles(f"file://{remote}", tmp_path / "work")
    with pytest.raises(AdminError):
        pins.write_text(".pin/agora.maintenance", '{"state": "active"}', "m")

    heads = subprocess.run(["git", "ls-remote", str(remote)],
                           capture_output=True, text=True, check=False)
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.strip() == "", f"遠端不該有 commit：{heads.stdout!r}"


def test_seeded_pin_repo_still_works(tmp_path: Path) -> None:
    """對照組：放過初始 commit 之後一切照舊（不能把正常路徑也擋掉）。"""
    remote = tmp_path / "pin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    (seed / "README.md").write_text("# pin repo\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "init: README"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    pins = GitPinFiles(f"file://{remote}", tmp_path / "work")
    assert pins.read_text(".pin/missing.json") is None
    pins.write_text(".pin/agora.maintenance", '{"a": 1}', "m")
    assert pins.read_text(".pin/agora.maintenance") == '{"a": 1}'


def test_init_pin_lock_path_reports_the_empty_pin_repo(
        tmp_path: Path) -> None:
    """`init-pin --confirm` 走同一條 `GitPinFiles`，所以同一個錯誤訊息。

    `committer/__main__.py` 的 `_init_pin_under_lock` 是用 `GitPinFiles` ＋
    `admin_lock_if_needed`；後者第一個動作就是 `read_maintenance()` → `_ensure()`。
    所以空 pin repo 在那條路上也必須停在這則訊息，而不是 `git reset 失敗 (rc=128)`。
    """
    from aistorage.admin.lock import GitHubAdmin, admin_lock_if_needed

    remote = _empty_pin_repo(tmp_path)
    pins = GitPinFiles(f"file://{remote}", tmp_path / "work")
    runner, _state = _gh_fake_real_semantics()
    gh = GitHubAdmin("owner/repo", runner=runner)

    with pytest.raises(AdminError, match="還沒有任何 commit"), \
            admin_lock_if_needed(repo="agora", pins=pins, gh=gh,
                                  workflow="committer.yml", reason="init-pin",
                                  precheck=lambda: None):
        pass

    # 旗標不該被留下來（連 clone 都還沒成功）
    assert not [c for c in gh.calls if c[1:3] == ["workflow", "disable"]], gh.calls
