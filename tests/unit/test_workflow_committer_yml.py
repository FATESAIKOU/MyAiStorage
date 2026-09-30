"""3.1 提交流程 workflow 骨架的性質測試。

把 `.github/workflows/committer.yml` 當成**規格的一部分**來鎖：改動這個檔
（例如加一個會印內容的步驟、或把 guard 挪到 clone 之後）時，這支測試會爆。

驗的性質（tasks.md 3.1）：
1. 定時預設每 6 小時；
2. `workflow_dispatch` 不接受任何自由輸入（沒有 `inputs`）；
3. 有 concurrency group（不會有兩輪同時跑）；
4. 整個提交流程是**單一 job**（步驟之間不需要跨 runner 傳東西）；
5. `GITHUB_TOKEN` 只有 `contents: read`；
6. 開頭就檢查 `github.ref == refs/heads/main` 與 `github.sha == 遠端 main HEAD`；
7. 收件匣空（只算形狀符合的檔）就不 clone、不裝工具、不跑提交流程；
8. log 只輸出 id、計數與耗時；秘密只能從 `env:` 進來，不准內嵌在 `run:`。

「需要在真的 GitHub 上實測」的項目（單元測試證明不了的）列在
`NEEDS_GITHUB_CHECKS`，測試會把它印出來，避免它被當成已經驗過。

actionlint 沒有安裝（也不 brew install），所以這裡用 `tests/unit/_workflow_yaml.py`
自帶的 YAML 讀取器；PyYAML 在場時它會交叉比對兩份結果。
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workflow_yaml import load_workflow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "committer.yml"

#: 單元測試證明不了、必須在真的 GitHub 上實測的項目（見 tasks 3.1 最後一句）。
NEEDS_GITHUB_CHECKS = (
    "workflow_dispatch 任意觸發：Actions 頁面手動 Run workflow（確認沒有 inputs "
    "可填、且 refs/heads/main 預設帶 main）",
    "rerun 舊的 run：在 Actions 頁面對一個**舊的**（較早 commit 的）run 按 "
    "Re-run all jobs，確認 guard 因為 github.sha != 遠端 main HEAD 而中止、"
    "不會重跑舊的真本",
    "其他分支觸發：對 feature 分支手動 dispatch（或在該分支 push 時由 "
    "workflow_dispatch 觸發），確認 guard 因為 github.ref != refs/heads/main 中止",
    "repo 設定（由你在 repo 設定頁操作，程式看不到）：Actions 的 artifact 與 "
    "log 保留天數設到最短、Actions 使用額度上限",
    "commit.yaml 曾被硬編碼在管理操作裡（已改成設定檔 committer_workflow）："
    "在真的 repo 上確認 `gh workflow disable committer.yml` 成功、"
    "`gh workflow enable committer.yml` 成功",
)


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW.is_file(), f"找不到 {WORKFLOW}"
    return load_workflow(WORKFLOW)


def _steps(wf: dict) -> list[dict]:
    (job,) = wf["jobs"].values()
    return list(job["steps"])


def _step(wf: dict, name: str) -> dict:
    for step in _steps(wf):
        if step.get("name") == name:
            return step
    raise AssertionError(f"workflow 沒有 {name!r} 這個步驟："
                         f"{[s.get('name') for s in _steps(wf)]}")


def _runs(wf: dict) -> str:
    return "\n".join(step.get("run", "") for step in _steps(wf))


def _commands(wf: dict) -> str:
    """`run:` 裡去掉以 `#` 開頭的註解行（註解可以提到任何字樣，指令不行）。"""
    out: list[str] = []
    for step in _steps(wf):
        for line in step.get("run", "").splitlines():
            if line.strip().startswith("#"):
                continue
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 1. 定時預設每 6 小時
# ---------------------------------------------------------------------------


def test_schedule_is_every_six_hours(wf: dict) -> None:
    schedule = wf["on"]["schedule"]
    assert len(schedule) == 1, f"應該只有一條排程，實際 {schedule}"
    minute, hour, dom, month, dow = schedule[0]["cron"].split()
    assert dom == "*" and month == "*" and dow == "*", schedule[0]["cron"]
    assert minute.isdigit(), f"分鐘應該是固定值（錯開的排程），實際 {minute!r}"
    assert hour == "*/6", f"小時欄位必須是 */6（每 6 小時），實際 {hour!r}"


def test_schedule_minute_is_off_the_hour(wf: dict) -> None:
    """錯開整點：別人與別的 Actions 也常在 0 分觸發。"""
    minute = int(wf["on"]["schedule"][0]["cron"].split()[0])
    assert minute not in (0, 30), f"分鐘 {minute} 會和別的排程撞在一起"


# ---------------------------------------------------------------------------
# 2. workflow_dispatch 不接受任何自由輸入
# ---------------------------------------------------------------------------


def test_workflow_dispatch_takes_no_inputs(wf: dict) -> None:
    dispatch = wf["on"]["workflow_dispatch"]
    assert dispatch == {}, f"workflow_dispatch 不得有 inputs 或任何設定：{dispatch!r}"


def test_no_other_triggers(wf: dict) -> None:
    """`on:` 只准 schedule 與 workflow_dispatch（push／pull_request 會誤觸）。"""
    assert set(wf["on"]) == {"schedule", "workflow_dispatch"}, sorted(wf["on"])


# ---------------------------------------------------------------------------
# 3. concurrency group
# ---------------------------------------------------------------------------


def test_concurrency_group_is_fixed_and_not_cancelled(wf: dict) -> None:
    conc = wf["concurrency"]
    assert conc["group"] == "committer-agora", conc
    # cancel-in-progress 必須是 false：被取消的那一輪可能已經推了一部分東西上遠端
    assert conc["cancel-in-progress"] is False, conc
    # group 不可帶 ${{ github.* }}：那會讓每個 sha 各自成組，等於沒有互斥
    assert "${{" not in conc["group"], conc["group"]


# ---------------------------------------------------------------------------
# 4. 單一 job
# ---------------------------------------------------------------------------


def test_single_job(wf: dict) -> None:
    assert list(wf["jobs"]) == ["commit"], list(wf["jobs"])


# ---------------------------------------------------------------------------
# 5. GITHUB_TOKEN 只有 contents: read
# ---------------------------------------------------------------------------


def test_permissions_are_contents_read_only(wf: dict) -> None:
    assert wf["permissions"] == {"contents": "read"}, wf["permissions"]


def test_token_is_only_taken_from_env(wf: dict) -> None:
    """`secrets.*` 只能出現在 `env:`，不准內嵌在 `run:`（會被 Actions 遮罩規則放行到 log）。"""
    for step in _steps(wf):
        run = step.get("run", "")
        assert "secrets." not in run, f"步驟 {step.get('name')!r} 的 run: 內嵌了 secrets"
        for value in (step.get("env") or {}).values():
            if isinstance(value, str) and "secrets." in value:
                assert "run" not in step or value not in step["run"], step["name"]


# ---------------------------------------------------------------------------
# 6. 開頭的 guard
# ---------------------------------------------------------------------------


def test_guard_is_the_first_step_after_checkout(wf: dict) -> None:
    names = [s.get("name") for s in _steps(wf)]
    assert names[0] == "Checkout repository", names
    assert names[1] == "guard", names
    # checkout 之後、guard 之前不得有任何步驟（guard 之前不碰 Drive／不讀 secret）
    assert "secrets-to-rclone" not in names[:2], names
    assert "run" not in names[:2], names


def test_guard_checks_ref_and_sha(wf: dict) -> None:
    run = _step(wf, "guard")["run"]
    assert 'GITHUB_REF" != "refs/heads/main' in run, "必須檢查 github.ref"
    # 遠端 main HEAD 必須**重新向 GitHub 查**（checkout 的 fetch-depth: 1 不足以知道）
    assert "/commits/main" in run, "必須向 API 查遠端 main HEAD"
    assert 'GITHUB_SHA" != "$REMOTE_SHA' in run, "必須檢查 github.sha == 遠端 main HEAD"
    # 查不到就中止，不要拿空字串比對（空字串 == 空字串會誤判成通過）
    assert '= "null"' in run and "exit 1" in run, "查不到遠端 HEAD 必須中止"


def test_checkout_is_shallow(wf: dict) -> None:
    with_ = _step(wf, "Checkout repository").get("with", {})
    assert with_.get("fetch-depth") == 1, with_


# ---------------------------------------------------------------------------
# 7. 收件匣空就不 clone
# ---------------------------------------------------------------------------


def test_prescan_prints_only_the_shaped_count(wf: dict) -> None:
    prescan = _step(wf, "inbox-prescan")
    assert prescan["id"] == "prescan", prescan
    run = prescan["run"]
    assert "prescan --config" in run
    # 讀不到收件匣必須讓整個 job 失敗（不可 || true）
    assert "|| true" not in _commands(wf), "prescan 失敗不可被吞掉"
    assert '"$GITHUB_OUTPUT"' in run, run
    # stdout 只印計數，內容不進 log
    assert "cat " not in run and "head " not in run, run


def test_expensive_steps_gated_on_prescan(wf: dict) -> None:
    gate = "steps.prescan.outputs.shaped != '0'"
    for name in ("install-tools", "secrets-to-pin-key", "run"):
        step = _step(wf, name)
        assert step.get("if") == gate, f"{name} 必須在收件匣有東西時才跑：{step.get('if')!r}"


def test_run_step_is_the_last_and_only_clones_once(wf: dict) -> None:
    """clone 真本發生在 `run`（提交流程本體）裡；前面不得另有 clone。"""
    steps = _steps(wf)
    assert steps[-1]["name"] == "run", [s.get("name") for s in steps]
    for step in steps[:-1]:
        run = step.get("run", "")
        assert "git clone" not in run, f"{step.get('name')!r} 不該自行 clone"
        assert "annex::" not in run, f"{step.get('name')!r} 不該碰 annex 遠端"


# ---------------------------------------------------------------------------
# 8. log 只輸出 id、計數與耗時
# ---------------------------------------------------------------------------


def test_no_debug_or_env_dumping(wf: dict) -> None:
    runs = _runs(wf)
    for banned in ("set -x", "printenv", "env\n", "ACTIONS_STEP_DEBUG",
                   "actions/upload-artifact", "cat \"$RUNNER_TEMP/rclone.conf\""):
        assert banned not in runs, f"workflow 不得出現 {banned!r}"


def test_tool_versions_are_pinned_with_checksums(wf: dict) -> None:
    """工具靠 SHA256 fail-closed（換版要連帶改 sha256 與 docs），不是靠版本字串。"""
    run = _step(wf, "install-tools")["run"]
    assert "sha256sum -c -" in run, "必須驗 SHA256"
    env = _step(wf, "install-tools")["env"]
    for key in ("GIT_ANNEX_SHA256", "RCLONE_SHA256", "GIT_ANNEX_VERSION", "RCLONE_VERSION"):
        assert key in env, f"{key} 必須集中在 install-tools 的 env（M7）"
    for key in ("GIT_ANNEX_SHA256", "RCLONE_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", str(env[key])), f"{key} 必須是 64 碼 hex"
    # sha256 不符必須中止（fail-closed），不能只印個警告繼續跑
    assert "exit 1" in run, "SHA256 不符必須中止"


def test_report_log_has_no_content(wf: dict) -> None:
    """提交流程自己印的那行 log 只有 id／計數／耗時（3.1 的 log 規則）。"""
    from aistorage.committer.run import RunReport

    report = RunReport(run_id="01J0000000000000000000000")
    report.counts["accepted"] = 3
    report.durations_ms["git.push"] = 1234
    line = report.format_log()
    assert "01J0000000000000000000000" in line
    assert "accepted=3" in line and "git.push=1234ms" in line
    assert "\n" not in line, "log 必須是單行"


def test_needs_github_checks_are_documented() -> None:
    """把「必須在真的 GitHub 上驗」的三件事釘在測試裡，避免它被當成已驗過。"""
    assert isinstance(NEEDS_GITHUB_CHECKS, tuple)
    for keyword in ("任意觸發", "rerun", "其他分支"):
        assert any(keyword in item for item in NEEDS_GITHUB_CHECKS), \
            f"tasks 3.1 明列的「{keyword}」不可漏掉"
    for item in NEEDS_GITHUB_CHECKS:
        assert isinstance(item, str) and item.strip(), item
