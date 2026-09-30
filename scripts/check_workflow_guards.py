#!/usr/bin/env python3
"""在**真的 GitHub** 上驗證提交流程 workflow 的防護（tasks 3.1、6.5）。

`tests/unit/test_workflow_committer_yml.py` 證明得了 workflow 檔的靜態性質，但
證明不了「真的跑起來會不會擋」——guard 擋不擋得對、action SHA 能不能解析、
GITHUB_TOKEN 拿到什麼權限、concurrency 會不會讓兩輪同時跑、帶 inputs 會不會被接受。
2026-09-30 的實測就是這樣抓到 `astral-sh/setup-uv` 釘了一個不存在的 SHA，
讓每一輪 run 在 "Set up job" 就死掉、guard 一步都沒跑到。

這支腳本把那些需要真 GitHub 的檢查收在一處，PR 合併後跑一次就能收掉 tasks.md
3.1 的驗收。每一項都回報 pass／fail／skip 與 run 連結，最後 exit code 非 0 表示有 fail。

用法（會真的觸發 run、真的花 Actions 分鐘；正式部署前跑沒有副作用）：

    uv run python scripts/check_workflow_guards.py                  # 全部（不含 rerun）
    uv run python scripts/check_workflow_guards.py --only pins,guard-main
    uv run python scripts/check_workflow_guards.py --rerun <run-id> # 舊 run 的 rerun
    uv run python scripts/check_workflow_guards.py --json out.json

`--rerun` 那一項要等 **main 已經前進**（比對用）才有意义：拿一個在舊 commit 上的
run 按 rerun，guard 應該因為 `github.sha != 遠端 main HEAD` 而拒絕。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "committer.yml"

#: 目標 repo。預設讀 `config/committer.json` 的 `github_repository`；`--repo` 可覆寫。
#: 設成模組層級（不是只在 main() 裡）是為了讓這支腳本也能被 import 來單獨重用某一項。
REPO = "FATESAIKOU/MyAiStorage"
try:
    REPO = json.loads(
        (REPO_ROOT / "config" / "committer.json").read_text(encoding="utf-8")
    )["github_repository"]
except (OSError, ValueError, KeyError):
    pass

#: Actions 隱含給每個 run 的權限，workflow 檔裡不會寫、也不能移除。
IMPLICIT_PERMISSIONS = {"metadata": "read"}

#: log 裡出現這些形狀就當成秘密外洩。值本身絕不印出來。
SECRET_SHAPES = (
    ("private key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("github token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("oauth refresh token", re.compile(r'"refresh_token"\s*:\s*"[^"]{10,}"')),
    ("rclone conf secret", re.compile(r"^\s*(token|client_secret)\s*=\s*\S+", re.MULTILINE)),
    ("aws key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)

ALL_CHECKS = ("pins", "guard-main", "guard-branch", "permissions",
              "concurrency", "inputs", "rerun")


@dataclass
class Result:
    name: str
    status: str  # pass / fail / skip
    detail: str
    run_ids: list[int] = field(default_factory=list)

    @property
    def runs(self) -> str:
        return " ".join(
            f"https://github.com/{r}" for r in self.run_ids
        ) or "（無 run）"


def gh(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                       timeout=180, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失敗: {proc.stderr.strip()}")
    return proc


def gh_json(*args: str) -> Any:
    proc = gh(*args, check=True)
    return json.loads(proc.stdout or "null")


# ---------------------------------------------------------------------------
# 取得 run 狀態
# ---------------------------------------------------------------------------


def run_url(run_id: int) -> str:
    return f"https://github.com/{REPO}/actions/runs/{run_id}"


def latest_run_id(workflow: str, branch: str) -> int | None:
    runs = gh_json("run", "list", "--repo", REPO, "--workflow", workflow,
                   "--branch", branch, "--limit", "1", "--json", "databaseId")
    return runs[0]["databaseId"] if runs else None


def fetch_run(run_id: int) -> dict[str, Any]:
    return gh_json("run", "view", str(run_id), "--repo", REPO,
                   "--json", "status,conclusion,headBranch,headSha,url,event")


def wait_for_run(run_id: int, timeout: float = 900.0,
                 poll: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        data = fetch_run(run_id)
        if data["status"] == "completed":
            return data
        if time.monotonic() >= deadline:
            raise RuntimeError(f"run {run_id} 逾時未結束（{timeout}s）")
        time.sleep(poll)


def run_log(run_id: int) -> str:
    proc = gh("run", "view", str(run_id), "--repo", REPO, "--log")
    if proc.returncode != 0:
        return ""
    return proc.stdout


def steps_of(run_id: int) -> list[dict[str, Any]]:
    """回傳該 run 的步驟（依執行順序），用 job view 才拿得到 step 層級。"""
    data = gh_json("run", "view", str(run_id), "--repo", REPO, "--json", "jobs")
    out: list[dict[str, Any]] = []
    for job in data.get("jobs") or []:
        for step in job.get("steps") or []:
            out.append({"name": step.get("name"), "number": step.get("number"),
                        "status": step.get("status"),
                        "conclusion": step.get("conclusion")})
    return out


def dispatch(workflow: str, ref: str) -> int:
    """手動觸發；回傳新 run 的 id（靠 dispatch 前後的清單差集抓）。"""
    before = {r["databaseId"] for r in
              gh_json("run", "list", "--repo", REPO, "--workflow", workflow,
                      "--limit", "20", "--json", "databaseId")}
    proc = gh("workflow", "run", workflow, "--repo", REPO, "--ref", ref)
    if proc.returncode != 0:
        raise RuntimeError(f"dispatch {ref} 失敗: {proc.stderr.strip()}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        after = gh_json("run", "list", "--repo", REPO, "--workflow", workflow,
                        "--limit", "20", "--json", "databaseId,headBranch")
        for r in after:
            if r["databaseId"] not in before and r["headBranch"] == ref:
                return r["databaseId"]
        time.sleep(2)
    raise RuntimeError(f"dispatch {ref} 之後找不到新 run")


def step_by_name(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for s in steps:
        if s["name"] == name:
            return s
    return None


def steps_after(steps: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    idx = next((i for i, s in enumerate(steps) if s["name"] == name), None)
    return [] if idx is None else steps[idx + 1:]


# ---------------------------------------------------------------------------
# 檢查項目
# ---------------------------------------------------------------------------


def check_pins(workflow: str) -> Result:
    """釘住的每個 action SHA 都必須在該 repo 裡真的存在。

    釘不存在的 SHA 會讓 run 在 "Prepare all required actions" 就死掉，
    連 guard 都還沒跑到——workflow 看起來完全正常，實際上從來沒有執行過。
    """
    text = WORKFLOW_FILE.read_text(encoding="utf-8")
    pins = re.findall(r"uses:\s*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)@([0-9a-f]{40})", text)
    if not pins:
        return Result("pins", "fail", "workflow 檔裡找不到任何釘住的 action（uses: owner/repo@<40 hex>）")
    bad: list[str] = []
    for owner, name, sha in pins:
        proc = gh("api", f"repos/{owner}/{name}/commits/{sha}")
        if proc.returncode != 0:
            bad.append(f"{owner}/{name}@{sha[:12]}…（{proc.returncode}）")
    if bad:
        return Result("pins", "fail",
                      f"{len(bad)}/{len(pins)} 個釘住的 SHA 解析不了：{', '.join(bad)}"
                      "。run 會在 Set up job 就中止，guard 一步都跑不到。")
    return Result("pins", "pass", f"{len(pins)} 個 action SHA 都解析得到：" +
                  ", ".join(f"{o}/{n}@{s[:7]}" for o, n, s in pins))


def check_guard_main(workflow: str) -> Result:
    """在 main 上手動觸發：guard 要放行，然後在需要秘密的那一步失敗。

    正式環境還沒部署、repo 沒有正式 secrets，所以「在 inbox-prescan 失敗」是
    預期結果；要驗的是它**有走到** inbox-prescan，而不是死在 guard 或更早。
    """
    run_id = dispatch(workflow, "main")
    wait_for_run(run_id)
    steps = steps_of(run_id)
    guard = step_by_name(steps, "guard")
    if guard is None:
        return Result("guard-main", "fail",
                      f"run {run_id} 沒有 guard 這個步驟（{[s['name'] for s in steps]}）",
                      [run_id])
    if guard["conclusion"] != "success":
        return Result("guard-main", "fail",
                      f"在 main 上觸發，guard 竟然沒有放行：{guard}", [run_id])
    rest = steps_after(steps, "guard")
    first_fail = next((s for s in rest if s["conclusion"] not in (None, "success", "skipped")), None)
    reached = [s["name"] for s in rest if s["conclusion"] in ("success", "failure", "skipped")]
    if first_fail is None:
        return Result("guard-main", "fail",
                      f"guard 放行了，但之後沒有任何一步失敗（正式部署前應該在需要秘密的"
                      f"那一步失敗）。跑過：{reached}", [run_id])
    return Result("guard-main", "pass",
                  f"guard 放行，之後在 {first_fail['name']!r} 失敗"
                  f"（{first_fail['conclusion']}）；跑過的步驟：{' → '.join(reached)}",
                  [run_id])


def check_guard_branch(workflow: str, branch: str) -> Result:
    """從其他分支觸發：guard 必須拒絕，而且**後面的步驟一步都不能執行**。"""
    run_id = dispatch(workflow, branch)
    wait_for_run(run_id)
    steps = steps_of(run_id)
    guard = step_by_name(steps, "guard")
    if guard is None:
        return Result("guard-branch", "fail",
                      f"run {run_id} 沒有 guard 這個步驟", [run_id])
    if guard["conclusion"] == "success":
        return Result("guard-branch", "fail",
                      f"從 {branch} 觸發，guard 竟然放行了", [run_id])
    later = [s for s in steps_after(steps, "guard")
             if s["status"] not in ("", None) and s["status"] != "skipped"]
    if later:
        return Result("guard-branch", "fail",
                      f"guard 拒絕了，但後面還有步驟被執行："
                      f"{[(s['name'], s['conclusion']) for s in later]}", [run_id])
    log = run_log(run_id)
    reason = ""
    m = re.search(r"Ref (\S+) is not refs/heads/main", log)
    if m:
        reason = f"，log 訊息：{m.group(0)!r}"
    return Result("guard-branch", "pass",
                  f"從 {branch}（{fetch_run(run_id)['headSha'][:7]}）觸發，"
                  f"guard 拒絕（{guard['conclusion']}），後面 0 個步驟被執行{reason}",
                  [run_id])


def check_permissions(workflow: str, main_run: int | None) -> Result:
    """run log 的 "GITHUB_TOKEN Permissions" 只能有 workflow 宣告的那個最小集合。"""
    if main_run is None:
        return Result("permissions", "skip", "沒有可用的 main run（先跑 guard-main）")
    log = run_log(main_run)
    block = re.search(r"##\[group\]GITHUB_TOKEN Permissions(.*?)##\[endgroup\]", log, re.DOTALL)
    if block is None:
        return Result("permissions", "fail",
                      f"run {main_run} 的 log 裡找不到 GITHUB_TOKEN Permissions 一節", [main_run])
    found: dict[str, str] = {}
    for line in (block.group(1) if block else "").splitlines():
        m = re.match(r"\s*([A-Za-z-]+):\s*(\S+)\s*$", line)
        if m:
            found[m.group(1).lower()] = m.group(2)
    extra = {k: v for k, v in found.items()
             if k not in IMPLICIT_PERMISSIONS and v != "read"}
    unknown = set(found) - set(IMPLICIT_PERMISSIONS) - {"contents"}
    if extra or unknown:
        return Result("permissions", "fail",
                      f"權限比宣告的大：額外={extra}、未宣告的={sorted(unknown)}", [main_run])
    return Result("permissions", "pass",
                  f"只有 {sorted(found)}（contents: read 為 workflow 宣告值，"
                  f"metadata: read 是 Actions 隱含、無法移除）", [main_run])


def check_concurrency(workflow: str) -> Result:
    """連續觸發兩次：不能同時跑。第二個要排隊，或被取消。"""
    a = dispatch(workflow, "main")
    b = dispatch(workflow, "main")
    time.sleep(6)
    snap = {r["databaseId"]: r["status"]
            for r in gh_json("run", "list", "--repo", REPO, "--workflow", workflow,
                             "--limit", "20", "--json", "databaseId,status")}
    running = [i for i in (a, b) if snap.get(i) == "in_progress"]
    queued = [i for i in (a, b) if snap.get(i) in ("queued", "pending", "waiting")]
    if len(running) > 1:
        return Result("concurrency", "fail",
                      f"兩輪同時在跑：{running}（concurrency group 沒生效）", [a, b])
    states = {i: snap.get(i) for i in (a, b)}
    wait_for_run(a)
    wait_for_run(b)
    final = {i: fetch_run(i)["conclusion"] for i in (a, b)}
    note = ""
    if any(v == "cancelled" for v in final.values()):
        note = ("（注意：cancel-in-progress: false 只保住**執行中**的那輪；"
                "還在 pending 的會被後來的那一輪取消。這對提交流程無害——"
                "下一輪會撈到同一批收件匣項目——但要記得這是預期行為。）")
    return Result("concurrency", "pass",
                  f"不會同時跑：快照 {states}（排隊 {queued}），"
                  f"最終結論 {final}{note}", [a, b])


def check_inputs(workflow: str) -> Result:
    """workflow_dispatch 沒有 inputs，帶 -f 必須被拒絕，而且不得產生 run。"""
    before = {r["databaseId"] for r in
              gh_json("run", "list", "--repo", REPO, "--workflow", workflow,
                      "--limit", "20", "--json", "databaseId")}
    proc = gh("workflow", "run", workflow, "--repo", REPO, "--ref", "main",
              "-f", "anything=x")
    time.sleep(5)
    after = {r["databaseId"] for r in
             gh_json("run", "list", "--repo", REPO, "--workflow", workflow,
                     "--limit", "20", "--json", "databaseId")}
    new = after - before
    if proc.returncode == 0:
        return Result("inputs", "fail",
                      f"帶 -f anything=x 竟然被接受，產生了 run {sorted(new)}")
    if new:
        return Result("inputs", "fail",
                      f"指令雖被拒絕（rc={proc.returncode}），但仍產生了 run {sorted(new)}")
    msg = (proc.stderr or proc.stdout).strip().splitlines()
    return Result("inputs", "pass",
                  f"被 GitHub API 拒絕（rc={proc.returncode}），沒有產生 run："
                  f"{msg[0] if msg else '(無訊息)'}")


def check_rerun(workflow: str, run_id: int | None) -> Result:
    """rerun 一個舊的 run：main 已經前進，guard 必須拒絕（sha 對不上）。"""
    if run_id is None:
        return Result("rerun", "skip",
                      "沒有指定 --rerun <run-id>。這一項要拿一個在舊 commit 上的 run，"
                      "且必須在 main 前進之後才有意義（對照組：同一個 sha rerun 會通過）。")
    data = fetch_run(run_id)
    if data["status"] != "completed":
        return Result("rerun", "skip", f"run {run_id} 還沒結束（{data['status']}）", [run_id])
    remote = gh_json("api", f"repos/{REPO}/commits/main", "--jq", ".sha")
    if data["headSha"] == remote:
        return Result("rerun", "skip",
                      f"run {run_id} 的 sha 還是遠端 main HEAD（{remote[:7]}），"
                      "guard 沒有東西可擋。請先讓 main 前進（合併下一包）再跑這項。",
                      [run_id])
    proc = gh("run", "rerun", str(run_id), "--repo", REPO)
    if proc.returncode != 0:
        return Result("rerun", "fail",
                      f"gh run rerun 失敗: {proc.stderr.strip()}", [run_id])
    new_id = latest_run_id(workflow, data["headBranch"])
    if new_id is None or new_id == run_id:
        # rerun 不會產生新的 databaseId，就只能看原本那筆的 updated_at／attempt
        time.sleep(20)
        attempt = gh_json("api", f"repos/{REPO}/actions/runs/{run_id}",
                          "--jq", ".run_attempt")
        wait_for_run(run_id)
    else:
        wait_for_run(new_id)
        ids = [run_id, new_id]
        steps = steps_of(new_id)
        guard = step_by_name(steps, "guard")
        if guard is None or guard["conclusion"] == "success":
            return Result("rerun", "fail",
                          f"rerun 在 sha {data['headSha'][:7]}（遠端 main 已經是 "
                          f"{remote[:7]}）竟然通過了 guard", ids)
        later = [s for s in steps_after(steps, "guard")
                 if s["status"] not in ("", None, "skipped")]
        if later:
            return Result("rerun", "fail",
                          f"rerun 的 guard 拒絕了但後面還跑了 {[s['name'] for s in later]}",
                          ids)
        return Result("rerun", "pass",
                      f"rerun 停留在舊 sha {data['headSha'][:7]}（遠端 main 已經是 "
                      f"{remote[:7]}），guard 拒絕，後面 0 個步驟被執行", ids)
    steps = steps_of(run_id)
    guard = step_by_name(steps, "guard")
    ok = guard is not None and guard["conclusion"] not in (None, "success")
    return Result("rerun", "pass" if ok else "fail",
                  f"rerun（attempt {attempt}）在 sha {data['headSha'][:7]} 上，"
                  f"guard = {(guard or {}).get('conclusion')}", [run_id])


def check_log_secrets(run_ids: list[int]) -> Result:
    """log 只能有 id、計數與耗時。秘密形狀一個都不該出現。"""
    hits: list[str] = []
    for run_id in run_ids:
        log = run_log(run_id)
        if not log:
            continue
        for label, pattern in SECRET_SHAPES:
            if pattern.search(log):
                hits.append(f"run {run_id}: {label}")
    if hits:
        return Result("log-secrets", "fail",
                      f"log 裡出現秘密形狀：{hits}（值沒有印出來）", run_ids)
    return Result("log-secrets", "pass",
                  f"{len(run_ids)} 個 run 的 log 掃過 {len(SECRET_SHAPES)} 種秘密形狀"
                  "（私鑰、GitHub token、refresh token、rclone conf 欄位、AWS key），"
                  "一個都沒有", run_ids)


# ---------------------------------------------------------------------------


def main() -> int:
    global REPO
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO,
                    help="owner/name（預設讀 config/committer.json 的 github_repository）")
    ap.add_argument("--workflow", default="committer.yml")
    ap.add_argument("--branch", default="phase1-spike",
                    help="guard-branch 用的非 main 分支（必須含有這個 workflow 檔）")
    ap.add_argument("--only", default=",".join(ALL_CHECKS),
                    help=f"逗號分隔，可選：{','.join(ALL_CHECKS)}")
    ap.add_argument("--rerun", type=int, default=None, metavar="RUN_ID",
                    help="要 rerun 的舊 run id（要 main 已經前進才有意義）")
    ap.add_argument("--json", type=Path, default=None, help="把結果寫成 JSON")
    args = ap.parse_args()
    REPO = args.repo

    wanted = {c.strip() for c in args.only.split(",") if c.strip()}
    unknown = wanted - set(ALL_CHECKS)
    if unknown:
        ap.error(f"未知的檢查：{sorted(unknown)}；可選：{list(ALL_CHECKS)}")
    if not WORKFLOW_FILE.is_file():
        print(f"找不到 {WORKFLOW_FILE}", file=sys.stderr)
        return 2

    results: list[Result] = []
    main_run: int | None = None
    dispatched: list[int] = []

    def do(name: str, fn: Callable[[], Result]) -> None:
        if name not in wanted:
            return
        print(f"→ {name} ...", flush=True)
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 — 一個檢查壞掉不該讓整份報告沒了
            r = Result(name, "fail", f"檢查本身出錯：{type(e).__name__}: {e}")
        results.append(r)
        print(f"  [{r.status}] {r.detail}", flush=True)

    do("pins", lambda: check_pins(args.workflow))
    do("guard-main", lambda: check_guard_main(args.workflow))
    if any(r.name == "guard-main" and r.run_ids for r in results):
        main_run = next(r.run_ids[0] for r in results if r.name == "guard-main")
        dispatched.append(main_run)
    do("guard-branch", lambda: check_guard_branch(args.workflow, args.branch))
    do("permissions", lambda: check_permissions(args.workflow, main_run))
    do("concurrency", lambda: check_concurrency(args.workflow))
    do("inputs", lambda: check_inputs(args.workflow))
    do("rerun", lambda: check_rerun(args.workflow, args.rerun))

    for r in results:
        dispatched.extend(r.run_ids)
    if dispatched and "log-secrets" not in wanted:
        do("log-secrets", lambda: check_log_secrets(sorted(set(dispatched))))

    print("\n" + "=" * 72)
    for r in results:
        print(f"{r.status.upper():5} {r.name:14} {r.detail}")
        if r.run_ids:
            print(f"      runs: {r.runs}")
    failed = [r.name for r in results if r.status == "fail"]
    skipped = [r.name for r in results if r.status == "skip"]
    print("=" * 72)
    print(f"fail={failed or '無'}  skip={skipped or '無'}")
    if args.json:
        args.json.write_text(json.dumps(
            [{"name": r.name, "status": r.status, "detail": r.detail,
              "run_ids": r.run_ids} for r in results],
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"JSON: {args.json}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
