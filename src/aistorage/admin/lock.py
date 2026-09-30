"""6.5 錯開：AdminLock（pin repo 的 maintenance 旗標當真正的鎖）。

1.6 證實住民的 PAT 可以重新啟用 workflow，所以「停用 workflow」不能作為鎖。
真正的鎖是 pin repo 的維護旗標（住民與 SA 都寫不進 pin repo）；
停用 workflow 只是輔助措施。committer 那一側的檢查是 A 線的檔案
（見本模組底部的給 A 線的介面說明），不由這裡改。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from aistorage.admin import AdminError
from aistorage.clock import Clock, format_rfc3339

#: 提交流程的 workflow 檔名（`.github/workflows/committer.yml`）。
#: `unlock` 刻意不讀設定檔（設定檔壞掉時仍要能解鎖），所以這裡放一份預設值；
#: 與 `CommitterConfig.committer_workflow` 的預設值必須一致。
DEFAULT_WORKFLOW = "committer.yml"


def maintenance_relpath(repo: str) -> str:
    """維護旗標在 pin repo 的路徑：.pin/<repo>.maintenance。"""
    return f".pin/{repo}.maintenance"


@dataclass(frozen=True)
class MaintenanceFlag:
    reason: str
    at: str
    by: str
    #: 這一次上鎖的操作 id（每次 `AdminLock.__enter__` 產生一個新的 ULID）。
    #: 給人查「這個旗標是誰留下的」用，不參與任何判斷。
    op: str = ""
    #: 旗標狀態：`active`（有管理操作正在進行）／`aborted`（做到一半失敗，
    #: 需要按 runbook 做中止處理）。`admin_lock_if_needed` 只允許在
    #: `aborted` 的既有鎖裡做事，其他狀態一律拒絕（M4）。
    state: str = "active"


#: 中止處理中、允許在既有鎖裡做的旗標狀態（runbook：先手動
#: `init-pin --confirm`，再 `swap-finish`）。
ABORTED_STATE = "aborted"


def parse_maintenance(data: str) -> MaintenanceFlag:
    try:
        obj = json.loads(data)
        return MaintenanceFlag(
            reason=obj["reason"], at=obj["at"], by=obj["by"],
            op=str(obj.get("op", "")), state=str(obj.get("state", "active")))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise AdminError(f"維護旗標內容損毀: {e}") from None


def read_maintenance(pins: Any, repo: str) -> MaintenanceFlag | None:
    """給 A 線（committer/run.py）的檢查點：pins.load() 之後呼叫。

    回傳 None 表示沒有維護中；否則 run 必須立刻以 `maintenance` 結束
    （不清掃、不 push、不發佈，也不刪除收件匣）。
    `pins` 只需要一個 `read_text(relpath) -> str | None` 方法
    （GitPinStore 由 A 線補上，或傳本模組的 GitPinFiles）。
    """
    raw = pins.read_text(maintenance_relpath(repo))
    if raw is None:
        return None
    return parse_maintenance(raw)


class PinFiles(Protocol):
    """pin repo 的檔案級存取（管理寫入身分；committer 的唯讀 deploy key 碰不到寫入）。"""

    def read_text(self, relpath: str) -> str | None:
        """讀檔；不存在回傳 None。"""
        ...

    def write_text(self, relpath: str, content: str, message: str) -> None:
        """寫檔並 commit＋push。"""
        ...

    def delete(self, relpath: str, message: str) -> None:
        """刪檔並 commit＋push；不存在則無視。"""
        ...


class MemoryPinFiles(PinFiles):
    """測試用 pin 檔儲存。"""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self._files = dict(files or {})
        self.writes: list[tuple[str, str]] = []

    def read_text(self, relpath: str) -> str | None:
        return self._files.get(relpath)

    def write_text(self, relpath: str, content: str, message: str) -> None:
        self._files[relpath] = content
        self.writes.append((relpath, message))

    def delete(self, relpath: str, message: str) -> None:
        self._files.pop(relpath, None)
        self.writes.append((relpath, f"delete: {message}"))


def empty_pin_repo_message(repo_url: str) -> str:
    """完全空的 pin repo（沒有任何 commit）時給管理者的訊息。

    **為什麼程式不自己建立那個初始 commit**（保守、可逆的選擇）：

    1. pin repo 是可信內容的**唯一來源**（ADR 0008），而它只對提交流程的 deploy key
       有寫入權——那條寫入路徑是整個設計裡最該被監看的一條。在管理操作中「順手」
       推進去一個 commit，等於讓一次以「抹除／回滾／重建釘選值」為名的操作，
       在別人檢查 commit 記錄時看起來像是做了它沒宣告的事。
    2. 不好回退。`.pin/*.json` 的內容雜湊被記在別處（讀取視圖、commit 訊息），
       在一個還沒有任何釘選值的 repo 裡亂加 commit，之後要回頭清掉得動 git 歷史。
    3. **這是設定期的錯誤，不是執行期的狀況。** 空的 pin repo 應該在部署手冊的
       前置檢查就被發現（`deploy.md` 事前檢查第 2 條），不是等到真的要鎖的時候。
    4. 整個 codebase 面對歧義的既定做法都是 fail-closed 並讓人來處理
       （`AdminLock` 的 `_mark_aborted`／H5、提交流程遇到不確定就中止）。自動補一顆
       commit 是唯一一個「自己猜」的做法，方向相反。

    修正的成本很低而且 idempotent：一顆 README commit 就好。所以停在這裡。
    """
    return (
        f"pin repo 裡還沒有任何 commit（{repo_url} 是空的），無法繼續。\n"
        "\n"
        "請先在 pin repo 放一個初始 commit（一個 README.md 就夠），再重跑這個指令。\n"
        "\n"
        "    # 用 git\n"
        f"    git clone {repo_url} /tmp/pin-seed && cd /tmp/pin-seed\n"
        "    printf '# pin repo\\n' > README.md\n"
        "    git add README.md && git commit -m 'init: initial commit' && git push\n"
        "\n"
        "    # 或直接在 GitHub 網頁上 Add a README\n"
        "\n"
        "程式刻意不自動建立這顆 commit：pin repo 是可信內容的唯一來源（ADR 0008），\n"
        "管理操作裡靜靜寫一個 commit 進去既難稽核也不好回退。寧可停在這裡讓人決定。\n"
        "（這是設定期的錯誤，`docs/runbooks/deploy.md` 事前檢查第 2 條就該先發現。）"
    )


class GitPinFiles(PinFiles):
    """本機 git 操作的 pin 檔儲存（管理寫入身分）。

    repo_url 是本機路徑或 file:// 時不需要 SSH（測試與 Mac 本機管理用）；
    其他形式必須提供 key_path（deploy key），一律經 GIT_SSH_COMMAND 使用，
    不讀 ~/.ssh/config，不用 ssh-agent。

    pin repo 必須已經有至少一個 commit；完全空的（`empty_pin_repo_message()`）
    會在 `_ensure()` 擋下來，不會自己去補。
    """

    def __init__(self, repo_url: str, workdir: Path | str, *,
                 key_path: Path | str | None = None,
                 user_name: str = "AiStorage Admin",
                 user_email: str = "admin@aistorage.local") -> None:
        from aistorage.safety import assert_safe_workdir

        self._repo_url = repo_url
        # M8：pin repo 會被 commit＋push，工作目錄不得位於專案 repo 之內
        # （與 A 線 GitPinStore 同一道檢查）。
        self._workdir = assert_safe_workdir(workdir, purpose="pin repo 工作目錄")
        self._key_path = Path(key_path) if key_path is not None else None
        self._user_name = user_name
        self._user_email = user_email
        self._env = self._build_env()
        self._ready = False

    def _is_local(self) -> bool:
        # L2：與 A 線 GitPinStore 共用同一個 URL 分類（避免 scp 形式被當成本機路徑）
        from aistorage.integrity.pin import _is_local_path_or_file_url

        return _is_local_path_or_file_url(self._repo_url)

    def _build_env(self) -> dict[str, str]:
        import os
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = self._user_name
        env["GIT_AUTHOR_EMAIL"] = self._user_email
        env["GIT_COMMITTER_NAME"] = self._user_name
        env["GIT_COMMITTER_EMAIL"] = self._user_email
        if self._is_local():
            return env
        if self._key_path is None:
            raise AdminError("非本機 pin repo 必須提供 key_path（deploy key）")
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {self._key_path} -F /dev/null -o IdentitiesOnly=yes "
            f"-o IdentityAgent=none -o StrictHostKeyChecking=yes")
        return env

    def _run(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or self._workdir, env=self._env,
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AdminError(f"git {' '.join(args)} 失敗 (rc={proc.returncode})")
        return proc.stdout.strip()

    def _has_commits(self) -> bool:
        """這個 clone 裡有沒有任何 commit（`git rev-parse --verify HEAD` 的成敗）。"""
        return subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=self._workdir,
            env=self._env, capture_output=True, text=True, timeout=60,
            check=False,
        ).returncode == 0

    def _ensure(self) -> None:
        if self._ready and (self._workdir / ".git").is_dir():
            self._run("fetch", "origin")
            # 完全空的 pin repo 沒有 `origin/HEAD` 可 reset，`git reset --hard
            # origin/HEAD` 會 rc=128「ambiguous argument 'origin/HEAD'」——那個
            # 訊息對管理者毫無幫助。先確認有 commit，換成下面那則會告訴他怎麼辦的。
            if not self._has_commits():
                raise AdminError(empty_pin_repo_message(self._repo_url))
            self._run("reset", "--hard", "origin/HEAD")
            return
        import shutil
        shutil.rmtree(self._workdir, ignore_errors=True)
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._run("clone", self._repo_url, ".")
        self._ready = True
        # clone 空 repo 會成功（只有一句 warning），所以同樣要在這裡擋。
        if not self._has_commits():
            raise AdminError(empty_pin_repo_message(self._repo_url))

    def read_text(self, relpath: str) -> str | None:
        self._ensure()
        target = self._workdir / relpath
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def _commit_and_push(self, message: str) -> None:
        self._run("add", ".pin")
        status = self._run("status", "--porcelain", "--", ".pin")
        if not status:
            return
        self._run("commit", "-m", message)
        self._run("push", "origin", "HEAD")

    def write_text(self, relpath: str, content: str, message: str) -> None:
        self._ensure()
        target = self._workdir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        try:
            self._commit_and_push(message)
        except AdminError:
            self._ready = False
            raise

    def delete(self, relpath: str, message: str) -> None:
        self._ensure()
        target = self._workdir / relpath
        if target.is_file() or target.is_symlink():
            target.unlink()
        try:
            self._commit_and_push(message)
        except AdminError:
            self._ready = False
            raise


Runner = Callable[[list[str]], str]


def _real_runner(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AdminError(f"命令失敗 (rc={proc.returncode}): {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}")
    return proc.stdout


#: `gh` 對「這個 workflow 本來就不是 active」講的話（實測 gh 2.101.0 回
#: `HTTP 403: Unable to disable a workflow that is not active.`）。
#: 只認這一句，不做模糊比對——寧可漏判（照舊報錯）也不誤判（吞掉真的錯）。
_NOT_ACTIVE_RE = re.compile(
    r"unable to disable a workflow that is not active", re.IGNORECASE)


def _is_not_active_error(err: BaseException) -> bool:
    return bool(_NOT_ACTIVE_RE.search(str(err)))


class GitHubAdmin:
    """gh CLI 包裝（管理者的登入；token 不進 argv、log 與例外）。

    runner 可注入假實作供測試（cmd -> stdout）。
    """

    def __init__(self, repo: str, runner: Runner | None = None) -> None:
        self._repo = repo
        self._runner = runner or _real_runner
        self.calls: list[list[str]] = []

    def _run(self, *args: str) -> str:
        cmd = ["gh", *args, "--repo", self._repo]
        self.calls.append(cmd)
        try:
            return self._runner(cmd)
        except AdminError:
            raise
        except Exception as e:
            raise AdminError(f"gh 執行失敗: {e}") from None

    def set_workflow_enabled(self, workflow: str, enabled: bool) -> None:
        """把 workflow 設成 `enabled`，**冪等**。

        `gh workflow disable` 對一個已經是停用狀態的 workflow 會回錯（實測 gh 2.101.0：
        `HTTP 403: Unable to disable a workflow that is not active.`，rc=1）。這在
        6.5 的路徑上會造成假的失敗：管理操作開始時 workflow 可能**已經**是停用的
        （前一次中止沒清乾淨、人工先停用过、或連續跑兩次），而那正是我們要的狀態。
        原本這裡會讓 `AdminLock.__enter__` 走 `_mark_aborted()`、把旗標留在
        `aborted` 並中止整個管理操作——為了已經成立的事情。

        兩道防線，都不解析錯誤訊息以外的東西：

        1. 先查狀態。已經是目標狀態就直接當成功，不必送指令（`enable` 本身
           冪等，但查一次比較便宜，也讓 `enable` 對稱）。
        2. 查得到狀態卻仍送出指令且失敗——那是「查完到送指令之間狀態被別人改掉」
           的競態。此時若錯誤講的是「已經不是 active」，代表**別人**剛好替我們
           达成了目標狀態，同樣當成功。

        查不到狀態（例如 repo 裡沒有這個 workflow）就**不假裝知道**，照常送指令，
        讓原本的錯誤照舊往外拋——那個錯誤是對的，不該被冪等邏輯吞掉。
        """
        try:
            already = self.workflow_enabled(workflow)
        except AdminError:
            already = None
        if already is enabled:
            return
        try:
            self._run("workflow", "enable" if enabled else "disable", workflow)
        except AdminError as e:
            # 「已經不是 active」這句本身就是**目標狀態已達成**的證明（是 GitHub
            # 自己說的），所以不管前面查得到查不到都算成功。
            if not enabled and _is_not_active_error(e):
                return
            raise

    def workflow_enabled(self, workflow: str) -> bool:
        """查詢 workflow 是否啟用。

        `gh workflow view` 沒有 `--json`（實測 gh 2.101.0，`unknown flag: --json`），
        所以只能從 `gh workflow list --json path,state` 依路徑比對。`workflow` 給
        檔名（`committer.yml`）或完整路徑（`.github/workflows/committer.yml`）都認得。

        **`--all` 不能少**：實測停用中的 workflow 不會出現在預設清單裡（只有
        `--all` 或被 `gh api` 查到時看得到）。少了它，6.3 健康檢查在「被停用」這個
        最該被找出來的情況反而會查不到、回報「未知」。

        查不到這個 workflow 時報錯而**不是**回傳 False：回傳 False 會讓健康檢查
        對一個根本不存在的 workflow 發出「被停用」的假警報。
        """
        out = self._run("workflow", "list", "--all", "--json", "path,state",
                        "--limit", "200")
        try:
            items = json.loads(out or "[]")
        except ValueError as e:
            raise AdminError(f"gh workflow list 解析失敗: {e}") from None
        if not isinstance(items, list):
            raise AdminError("gh workflow list 回傳的不是陣列")
        want = workflow.strip().removeprefix("./")
        for item in items:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if path == want or path.endswith(f"/{want}"):
                return str(item.get("state") or "") == "active"
        raise AdminError(f"repo 裡查不到 workflow {workflow}（檔名寫錯了？）")

    def active_runs(self, workflow: str) -> list[dict]:
        """列出 in_progress 與 queued 的 run（id 與狀態）。"""
        runs: list[dict] = []
        for status in ("in_progress", "queued"):
            out = self._run("run", "list", "--workflow", workflow,
                            "--status", status, "--json",
                            "databaseId,status,headBranch,createdAt")
            try:
                items = json.loads(out or "[]")
            except ValueError as e:
                raise AdminError(f"gh run list 解析失敗: {e}") from None
            runs.extend(items if isinstance(items, list) else [])
        return runs

    def delete_run(self, run_id: int) -> None:
        self._run("run", "delete", str(run_id))


class _BusyTimeout(Exception):
    """內部：等待 quiet 逾時（會觸發回滾，與預檢失敗區分）。"""


class AdminLock:
    """管理操作的互斥鎖（context manager，全程在 with 內執行管理腳本）。"""

    def __init__(self, *, repo: str, pins: PinFiles, gh: GitHubAdmin,
                 workflow: str, reason: str, by: str = "admin",
                 clock: Clock | None = None,
                 timeout: timedelta = timedelta(minutes=10),
                 poll: timedelta = timedelta(seconds=15),
                 precheck: Callable[[], None] | None = None,
                 notify: Callable[[str], None] | None = None) -> None:
        self._repo = repo
        self._pins = pins
        self._gh = gh
        self._workflow = workflow
        self._reason = reason
        self._by = by
        self._clock = clock
        self._timeout = timeout
        self._poll = poll
        self._precheck = precheck
        self._notify = notify or (lambda msg: print(msg))

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        from datetime import timezone
        return datetime.now(timezone.utc)

    def _new_op(self) -> str:
        from aistorage.schema import generate_ulid

        return generate_ulid()

    def _flag_payload(self, op: str) -> str:
        now = self._now()
        at = format_rfc3339(now, include_fraction=True)
        return json.dumps(
            {"reason": self._reason, "at": at, "by": self._by,
             "op": op, "state": "active"},
            sort_keys=True, ensure_ascii=False) + "\n"

    def _mark_aborted(self) -> None:
        """中止處理：把旗標狀態改成 `aborted`（盡力而為）。

        失敗的中途狀態需要人按 runbook 處理（`init-pin --confirm` 再
        `swap-finish`）；`admin_lock_if_needed` 只放行 `aborted` 的既有鎖。
        改寫失敗就保留原旗標（還是擋得住提交流程，只是狀態沒更新）。
        """
        try:
            flag = self.status()
        except AdminError:
            return
        if flag is None or flag.state == ABORTED_STATE:
            return
        try:
            self._pins.write_text(
                maintenance_relpath(self._repo),
                json.dumps(
                    {"reason": flag.reason, "at": flag.at, "by": flag.by,
                     "op": flag.op, "state": ABORTED_STATE},
                    sort_keys=True, ensure_ascii=False) + "\n",
                f"maintenance aborted: {flag.reason} (op {flag.op or '?'})")
        except AdminError:
            pass

    def status(self) -> MaintenanceFlag | None:
        return read_maintenance(self._pins, self._repo)

    def unlock(self) -> MaintenanceFlag | None:
        """手動解除（管理操作失敗後由人確認再解除；CLI：unlock --confirm）。

        沒有旗標就是已經解除了，視為成功（冪等）。
        """
        flag = self.status()
        if flag is not None:
            self._pins.delete(maintenance_relpath(self._repo), "maintenance off (admin unlock)")
        self._gh.set_workflow_enabled(self._workflow, True)
        return flag

    def __enter__(self) -> AdminLock:
        if self.status() is not None:
            raise AdminError("已經有維護旗標，不重複上鎖")
        op = self._new_op()
        relpath = maintenance_relpath(self._repo)
        self._pins.write_text(
            relpath, self._flag_payload(op),
            f"maintenance on: {self._reason} (op {op})")
        # 2. 停用 workflow（輔助措施；住民可能重新啟用，不當作鎖）
        try:
            self._gh.set_workflow_enabled(self._workflow, False)
        except AdminError as e:
            # L3：旗標留著（安全方向），標成 aborted 等人按 runbook 查，
            # 但要明確告訴人怎麼清掉
            self._mark_aborted()
            self._notify(
                f"維護中止，需要人工處理：已寫入 {relpath} 但停用 workflow 失敗（{e}）。"
                "確認狀態後用 `python -m aistorage.admin unlock --confirm` 解除。")
            raise
        try:
            self._wait_quiet(deadline=self._now() + self._timeout)
            # 4. 預檢（例如遠端 manifest 雜湊等於正式 pin）
            if self._precheck is not None:
                try:
                    self._precheck()
                except Exception:
                    # 預檢不符（可能真有問題）：保持暫停並標成 aborted，
                    # 等人按 runbook 查（與 with 區塊內出錯同一個處理方式）。
                    self._mark_aborted()
                    raise
        except _BusyTimeout:
            # 逾時（短暫擁塞）才回滾自己的旗標與停用；預檢不符（可能真有問題）
            # 則保持暫停（旗標留著、workflow 保持停用），等人來查。
            try:
                self._pins.delete(relpath, "maintenance aborted: timeout")
            finally:
                self._gh.set_workflow_enabled(self._workflow, True)
            raise AdminError("仍有執行中的 run，逾時中止") from None
        return self

    def _wait_quiet(self, *, deadline: datetime) -> None:
        poll_s = max(self._poll.total_seconds(), 0)
        while True:
            if not self._gh.active_runs(self._workflow):
                return
            if self._now() >= deadline:
                raise _BusyTimeout()
            if poll_s > 0:
                time.sleep(poll_s)

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # H5：**只在 with 區塊正常結束時**才解除。抹除或回滾做到一半失敗時，
        # 遠端可能已經不一致（bundle 刪了還沒重推），這時解除鎖會讓提交流程
        # 回到不一致的遠端上：settle 每輪 MismatchError 中止，沒有 pending 時
        # 反而會用舊 pin 把新狀態全隔離。與 __enter__ 預檢失敗同一個處理方式。
        if exc_type is not None:
            # 標成 aborted：中止處理（init-pin --confirm → swap-finish）才允許
            # 在這個既有的鎖裡做；其他管理操作看到非 aborted 的旗標一律拒絕。
            self._mark_aborted()
            self._notify(
                "維護中止，需要人工處理：with 區塊內發生例外，已保留 "
                f"{maintenance_relpath(self._repo)} 旗標並維持 workflow 停用。"
                "處理完（必要時重推、重建 pin）再用 "
                "`python -m aistorage.admin unlock --confirm` 解除。")
            return False
        # pin 的重建或確認由呼叫端（抹除等）在 with 內先完成；
        # 這裡只刪旗標並重新啟用 workflow。
        try:
            self._pins.delete(
                maintenance_relpath(self._repo), "maintenance off")
        finally:
            self._gh.set_workflow_enabled(self._workflow, True)
        return False


@contextmanager
def admin_lock_if_needed(*, repo: str, pins: PinFiles, gh: GitHubAdmin,
                         workflow: str, reason: str,
                         precheck: Callable[[], None] | None = None,
                         notify: Callable[[str], None] | None = None,
                         expect_state: str = ABORTED_STATE,
                         **kwargs: Any) -> Iterator[AdminLock | None]:
    """已經是中止處理狀態才在既有鎖裡做事，沒有旗標就自己上鎖（6.5＋M4）。

    用在「可能被包在管理操作裡、也可能被單獨執行」的指令（`init-pin`）：
    - 沒有旗標 → 照 AdminLock 的正常流程上鎖（停用 workflow、等沒有執行中的
      run、預檢），做完解除；
    - 已經有旗標 → 只有 `state` 符合 runbook 預期（預設 `aborted`：之前的管理
      操作做到一半失敗，runbook 讓人先手動 `init-pin --confirm` 再
      `swap-finish`）才直接在既有的鎖裡做事（這時**不能**再上一次鎖，
      `AdminLock` 會拒絕重複上鎖）。另一位管理者正在進行的操作（`active`）、
      或狀態不明的舊旗標，一律拒絕，避免兩個管理操作並行。
      既有旗標本身就是提交流程的門擋。
    """
    existing = read_maintenance(pins, repo)
    if existing is not None:
        if existing.state != expect_state:
            raise AdminError(
                f"已有維護旗標（op={existing.op or '?'} state={existing.state} "
                f"reason={existing.reason}），不是 runbook 預期的"
                f"「{expect_state}」狀態，拒絕在既有的鎖裡執行；"
                "等該操作結束或按 runbook 做中止處理後再來")
        yield None
        return
    with AdminLock(repo=repo, pins=pins, gh=gh, workflow=workflow, reason=reason,
                   precheck=precheck, notify=notify, **kwargs) as lock:
        yield lock
