"""AiStorage AnnexGit: 包裝 git 與 git-annex subprocess 操作。

依據規格：
- docs/impl/group3-modules.md 第 1、3.5 節
- review-g3a.md M5（clone_for_commit、push 多分支、annex_keys_in、環境隔離與超時）
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Protocol, runtime_checkable

from aistorage.annex.manifest import normalize_ls_remote
from aistorage.errors import ReadError, WriteError


#: `annex.largefiles` 的預設規則（M2：single source of truth）。
#: Agora 的原始紀錄路徑是 `sessions/<source>/<id>/raw`（沒有副檔名），所以
#: `include=*.json` 涵蓋不到它——用 *.json 會讓每一則控制記錄（meta.json、
#: handoffs/*.json、_committer/rejections/*.json）都變成 Drive 上的一個獨立
#: annex 物件，bundle 與 API 呼叫次數都會膨脹。要用別的規則由呼叫端傳入。
DEFAULT_LARGEFILES = "include=sessions/*/*/raw"


#: `RCLONE_CONFIG` 解析不出來時指向哪裡：一個**保證不存在**的路徑。
#: 目的不是讓 rclone 讀到東西，是讓它**絕對讀不到預設設定**。
#: rclone 找不到 `RCLONE_CONFIG` 會往 `$XDG_CONFIG_HOME/rclone/rclone.conf`、
#: `~/.config/rclone/rclone.conf` 找——本機上那份常常存在，而且**可能指向別的 Drive
#: 根**。寧可讓它明確地找不到，也不要讓它安靜地連到錯的地方。
_RCLONE_SENTINEL = "/nonexistent/aistorage-rclone-config-not-set"


def get_git_env(
    rclone_conf: str | Path | None = None,
    *,
    require_rclone_conf: bool = False,
) -> dict[str, str]:
    """建立隔離的環境變數，防止終端機互動提示與繼承全域設定。

    順帶把 `RCLONE_CONFIG` 釘死。`git-remote-annex` 與 rclone special remote 是
    **子程序**，它們不會知道 `AISTORAGE_RCLONE_CONF` 這個變數——只認 `RCLONE_CONFIG`
    或自己的預設探索路徑。少了這一行，提交流程在 runner 上會去讀 runner HOME 底下
    根本不存在的設定，於是 `gdrive` 這個 remote 找不到，annex clone 直接
    `ABORTED(annex.git.clone:ReadError)`（2026-09-30 正式 run 36695731310 就是這樣）。

    在本機上同一個洞更危險但是**看不見**：`~/.config/rclone/rclone.conf` 通常存在，
    所以本機測試會通過，卻可能連到與 `AISTORAGE_RCLONE_CONF` 不同的 Drive 根。

    解析順序：`rclone_conf` 參數 → `AISTORAGE_RCLONE_CONF` 環境變數。
    解析不到時：

    - `require_rclone_conf=True` → 立刻丟 `ReadError`，訊息指明要設哪個變數。
      給「確定會 shell out 到 rclone」的呼叫端用（annex clone／replay／rebuild）。
    - 否則 → `RCLONE_CONFIG` 指向一個保證不存在的路徑。純 git 的呼叫端
      （例如 pin repo 用 GitHub SSH）本來就不需要 rclone，不該為了它壞掉；
      但也**絕不**讓它們安靜地 fallback 到使用者的預設設定。
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"

    resolved = rclone_conf or os.environ.get("AISTORAGE_RCLONE_CONF") or ""
    if not resolved:
        if require_rclone_conf:
            raise ReadError(
                "需要 rclone 設定檔，但 AISTORAGE_RCLONE_CONF 沒有設定。"
                "git-remote-annex／rclone special remote 只認 RCLONE_CONFIG，"
                "不會讀 AISTORAGE_RCLONE_CONF，沒有它就會退回 ~/.config/rclone/rclone.conf"
                "——那份可能指向別的 Drive 根。請設定環境變數 AISTORAGE_RCLONE_CONF "
                "指向要用的 rclone conf 路徑。"
            )
        env["RCLONE_CONFIG"] = _RCLONE_SENTINEL
        return env

    env["RCLONE_CONFIG"] = str(resolved)
    return env


@runtime_checkable
class AnnexGit(Protocol):
    """AnnexGit 操作協定。"""

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        """查詢遠端 ref 集合（經 normalize_ls_remote 正規化之 clean ref -> sha）。"""
        ...

    def add(self, paths: list[str | Path] | str | Path) -> None:
        """執行 git add。"""
        ...

    def commit(self, message: str) -> str:
        """執行 git commit，回傳 commit sha。"""
        ...

    def copy(
        self,
        remote: str,
        to_copy: list[str] | None = None,
        *,
        timeout: float = 900.0,
    ) -> None:
        """執行 git annex copy。"""
        ...

    def push(
        self,
        remote: str = "origin",
        branches: tuple[str, ...] | str = ("main", "git-annex"),
        *,
        timeout: float = 900.0,
    ) -> None:
        """執行 git push 推送指定分支至遠端。

        注意：exit 0 不等於成功，成功與否由 verify_after_push 判定（design D2）。
        """
        ...

    def annex_keys_in(self, remote_uuid: str) -> frozenset[str]:
        """查詢在指定 remote_uuid 上已存在的 annex key 集合。"""
        ...

    def annex_keys_in_branch(self) -> frozenset[str]:
        """查詢**目前分支樹狀裡**的 annex key 集合（不看 remote）。

        H1（review-cdb4a34）：被換掉的舊版本快照不在樹狀裡（真本的 raw 只有
        最新一版），所以「樹狀裡的 key」必須與「location log 宣稱在遠端的 key」
        分開查——兩者對那些 key 本來就不會相同。
        """
        ...

    def origin_url(self) -> str:
        """`git config remote.origin.url`（H1：clone 之後確認真的指向目標 repo）。"""
        ...

    def remote_uuid(self, remote: str = "origin") -> str:
        """git-annex special remote 的 uuid（H1：確認 clone 到的是目標那一個遠端）。"""
        ...

    def lookupkey(self, path: str | Path) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "annex", "lookupkey", str(path)],
                cwd=self.workdir, capture_output=True, text=True, errors="replace",
                env=self._get_env(), timeout=60.0, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise ReadError(f"git annex lookupkey 失敗: {e}") from None
        if proc.returncode != 0:
            # 檔案沒有進 annex（或不是 git repo）→ 回傳 None 讓呼叫端決定
            return None
        return proc.stdout.strip() or None

    def get_key(self, key: str, *, from_remote: str = "origin") -> None:
        try:
            proc = subprocess.run(
                ["git", "annex", "get", f"--key={key}", f"--from={from_remote}"],
                cwd=self.workdir, capture_output=True, text=True, errors="replace",
                env=self._get_env(), timeout=600.0, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ReadError("git annex get 逾時") from None
        except (OSError, subprocess.SubprocessError) as e:
            raise ReadError(f"git annex get 失敗: {e}") from None
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()
            raise ReadError(
                f"從遠端取回 annex 物件失敗 (rc={proc.returncode}): {key}"
                + (f"｜{stderr[-1][:200]}" if stderr else "")
            )

    def local_refs(self, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
        """查詢本地指定分支之完整 ref 集合（refs/heads/<branch> -> sha）。若缺少任一分支拋出 ReadError。"""
        ...

    def lookupkey(self, path: str | Path) -> str | None:
        """查詢某個工作樹檔案實際被 git-annex 放進物件庫的 key。

        這是 annex key 的**唯一**來源（review A-H2）：自己用
        `SHA256E-s<size>--<sha><ext>` 推算一定會與 git-annex 產生的不同
        （副檔名取自工作樹檔名、沒有副檔名就沒有副檔名、還受
        `annex.maxextensionlength` 等規則影響）。檔案沒有進 annex（回傳 None）
        代表 `annex.largefiles` 規則沒有涵蓋它，呼叫端必須立刻 raise，
        不要把內容留在 git blob 裡還假裝進了 annex。
        """
        ...

    def get_key(self, key: str, *, from_remote: str = "origin") -> None:
        """從遠端 special remote 取回指定 annex 物件到本機（`git annex get --key`）。

        提交流程每一輪都是全新 clone，annex 物件的內容不會跟著 clone 下來；
        沒有這一步，`apply_handoff` 驗證前一輪的接續點時取不回舊快照，整輪中止。
        取不到要 raise（ReadError），不要靜默略過。
        """
        ...

    def local_manifest_sha256(self, remote_uuid: str) -> str | None:
        """**這一輪 push 實際寫出去的那份主 manifest 的內容雜湊**（H1）。

        git-remote-annex 在 push 之後會把它剛寫上遠端的 manifest 留在本機
        `.git/annex/git-remote-annex/<uuid>/manifest`——實測（1.75.1 與 1.69.3）
        那個檔案與遠端剛寫出的那一份 sha256 完全相同。提交流程自己知道剛寫了
        什麼位元組，把它記進 pending 之後，settle／sweep／verify 只認這個內容，
        住民就無法用一份「重放出同樣 refs、位元組不同」的變體把 promote 拖住，
        或讓自己的變體被轉正成正式值。

        檔案不存在（還沒 push 過、push 失敗、或 remote 不是 git-remote-annex）
        回傳 `None`：呼叫端要把它當成「沒有這個證據」，而不是猜。
        """
        ...


class SubprocessAnnexGit:
    """以 subprocess 呼叫本機 git / git-annex 之實作。

    M8：建構時就檢查工作目錄安全（不得位於專案 repo 之內），因為這個類別會跑
    `git annex init`／`git annex copy`——兩者都會改寫它所在 repo 的設定與分支。
    檢查放在這裡而不是每個呼叫點，是因為所有呼叫點都會先建構這個物件。
    """

    def __init__(
        self,
        workdir: Path | str,
        *,
        allow_unsafe_workdir: bool = False,
        require_annex_remote: bool = False,
    ) -> None:
        """`require_annex_remote=True`：這個目錄必須已經是 git repo 且
        `remote.origin.url` 是 `annex::` 遠端。

        M4：會改寫**既有目錄**的入口（admin 的 erase／rollback／swap-finish，
        也就是「拿到一個已經存在的 clone 來改寫它」）都必須開這項——誤指到別的
        repo（例如 MyBrain 的工作目錄）時，git-annex 指令就會改寫那個 repo。
        這裡**預設維持 False**：`SubprocessAnnexGit` 也被用在純 git 的工作樹
        （測試的 seed repo、一般 clone），而 `clone_for_commit` 的目的地當下
        還不是 repo。admin 的既有目錄呼叫端一律顯式傳 True。
        """
        from aistorage.safety import assert_safe_workdir

        self.workdir = (
            Path(workdir).resolve()
            if allow_unsafe_workdir
            else assert_safe_workdir(
                workdir, purpose="git-annex 執行目錄",
                require_annex_remote=require_annex_remote)
        )

    def _get_env(self) -> dict[str, str]:
        """建立隔離的環境變數，防止終端機互動提示與繼承全域設定。

        這裡**不**要求 rclone 設定：本類別也有只操作本機 repo 的用法（單元測試、
        已 clone 好之後的維護操作），那些路徑不碰 rclone，不該因為它壞掉。
        但 `RCLONE_CONFIG` 一律被釘死（見 `get_git_env`），所以真的會 shell out 到
        rclone 的操作拿到的永遠是明確指定的那一份，**不會** fallback 到使用者的
        `~/.config/rclone/rclone.conf`。

        確定會碰遠端的入口（`clone_for_commit`、`replay`、rebuild 的 clone）另外用
        `require_rclone_conf=True`，缺設定時在第一個子程序之前就報清楚的錯。
        """
        return get_git_env()

    def _run(
        self,
        cmd: list[str],
        *,
        is_write: bool = False,
        timeout: float = 60.0,
        input_text: str | None = None,
    ) -> str:
        # 環境變數**在建 try 之外**組出來：缺 rclone 設定是一個要讓人一眼看懂的
        # 設定錯誤，不該被下面那個 `except Exception` 改寫成
        # 「Git 命令呼叫失敗: git」。
        env = self._get_env()
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                errors="replace",
                env=env,
                timeout=timeout,
                check=False,
                input=input_text,
            )
        except subprocess.TimeoutExpired:
            err_cls = WriteError if is_write else ReadError
            raise err_cls(f"Git 命令執行逾時 ({cmd[0]} {cmd[1] if len(cmd) > 1 else ''})") from None
        except Exception:
            err_cls = WriteError if is_write else ReadError
            raise err_cls(f"Git 命令呼叫失敗: {cmd[0]}") from None

        if proc.returncode != 0:
            err_cls = WriteError if is_write else ReadError
            cmd_name = f"{cmd[0]} {cmd[1]}" if len(cmd) > 1 else cmd[0]
            # N6: 將 stderr 尾端 4 KiB 寫入 debug/git-<ts>.log，主訊息不洩漏敏感路徑
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            log_filename = f"git-{ts}.log"
            try:
                debug_dir = self.workdir / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                log_file = debug_dir / log_filename
                stderr_tail = (proc.stderr or "")[-4096:]
                log_file.write_text(stderr_tail, encoding="utf-8")
            except Exception:
                pass
            raise err_cls(f"Git 命令失敗 (rc={proc.returncode}, op={cmd_name}, log={log_filename})")
        return proc.stdout

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        stdout = self._run(["git", "ls-remote", remote], is_write=False)
        refs: dict[str, str] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                refs[parts[1]] = parts[0]
        return normalize_ls_remote(refs)

    def add(self, paths: list[str | Path] | str | Path) -> None:
        if isinstance(paths, (str, Path)):
            path_list = [str(paths)]
        else:
            path_list = [str(p) for p in paths]
        self._run(["git", "add"] + path_list, is_write=True)

    def commit(self, message: str) -> str:
        # 固定 user.name 與 user.email，避免在 runner 上因缺少身分設定失敗
        cmd = [
            "git",
            "-c",
            "user.name=AiStorage Committer",
            "-c",
            "user.email=committer@aistorage.local",
            "commit",
            "-m",
            message,
        ]
        self._run(cmd, is_write=True)
        sha = self._run(["git", "rev-parse", "HEAD"], is_write=False).strip()
        return sha

    def copy(
        self,
        remote: str,
        to_copy: list[str] | None = None,
        *,
        timeout: float = 900.0,
    ) -> None:
        """把內容搬到遠端。

        `to_copy` 給的是 **annex key**（不是路徑）。必須走 git-annex 明確的
        `--batch-keys`：把 key 當位置參數丟進去，git-annex 會當成 pathspec，
        對象不在樹狀裡就整個失敗（`pathspec ... did not match any file(s)
        known to git`）——而「不在樹狀裡」正是要搬的對象（同一輪換掉的舊版本）。
        """
        if to_copy:
            self._run(
                ["git", "annex", "copy", f"--to={remote}", "--batch-keys"],
                is_write=True, timeout=timeout,
                input_text="".join(f"{k}\n" for k in to_copy),
            )
            return
        self._run(["git", "annex", "copy", f"--to={remote}"], is_write=True, timeout=timeout)

    def push(
        self,
        remote: str = "origin",
        branches: tuple[str, ...] | str = ("main", "git-annex"),
        *,
        timeout: float = 900.0,
    ) -> None:
        cmd = ["git", "push", remote]
        if isinstance(branches, str):
            cmd.append(branches)
        else:
            cmd.extend(branches)
        self._run(cmd, is_write=True, timeout=timeout)

    def annex_keys_in(self, remote_uuid: str) -> frozenset[str]:
        """M5: 查詢在指定 remote_uuid 上已存在的 annex key 集合。

        整合測試發現：`git annex find` 沒有 `--all` 選項，原本的寫法必定失敗
        （Invalid option `--all`，rc=1），所以 init-pin 與 pending 一直拿不到 key 集合。
        正確的語法是 `git annex find --in=<uuid> '--format=${key}\n'`：
        git-annex 的 `--format` **不會自動換行**，不自己加 `\n` 的話所有 key 會被
        串成一行，變成一個假的「key」。整合測試就是這樣發現的：pin 只記到 1 個
        垃圾字串，下一輪 sweep 就把真的 annex 物件全隔離了。

        語意：回傳**目前分支樹狀中、被這個 remote 持有的** key。Agora 的原始紀錄
        留在樹狀裡且不會被改寫（期 1 不提供改寫），所以實務上等於全部的 key；
        若之後有「從樹狀移除但仍需保留物件」的情境，這裡要另外用 location log 取。
        """
        cmd = ["git", "annex", "find", f"--in={remote_uuid}", "--format=${key}\n"]
        stdout = self._run(cmd, is_write=False)
        keys = set()
        for line in stdout.splitlines():
            k = line.strip()
            if k:
                keys.add(k)
        return frozenset(keys)

    def annex_keys_in_branch(self) -> frozenset[str]:
        """`git annex find --format=${key}\\n`：目前分支樹狀裡的 key。

        H1（review-cdb4a34）：被換掉的舊版本快照不在樹狀裡（真本的 raw 只有
        最新一版），所以「樹狀裡有哪些 key」必須與「location log 宣稱哪些 key
        在遠端」分開查——對那些 key 兩者本來就不會相同。`--format` 不會自動
        換行（見 `annex_keys_in` 的說明），所以要自己帶 `\\n`。
        """
        stdout = self._run(
            ["git", "annex", "find", "--format=${key}\n"], is_write=False)
        return frozenset(line.strip() for line in stdout.splitlines() if line.strip())

    def origin_url(self) -> str:
        """H1（review-25a48a9）：`remote.origin.url`。clone 到錯的 repo 時，
        `verify_clone_identity` 會靠它擋下來（讀不到就 raise，不猜）。"""
        return self._run(
            ["git", "config", "--get", "remote.origin.url"], is_write=False).strip()

    def remote_uuid(self, remote: str = "origin") -> str:
        """H1：git-annex special remote 的 uuid（`git annex info --fast` 的 uuid 行）。"""
        stdout = self._run(
            ["git", "annex", "info", remote, "--fast"], is_write=False)
        for line in stdout.splitlines():
            if line.startswith("uuid:"):
                uuid = line.split(":", 1)[1].strip()
                if uuid:
                    return uuid
        raise ReadError(f"讀不到 annex remote {remote} 的 uuid")

    def lookupkey(self, path: str | Path) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "annex", "lookupkey", str(path)],
                cwd=self.workdir, capture_output=True, text=True, errors="replace",
                env=self._get_env(), timeout=60.0, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise ReadError(f"git annex lookupkey 失敗: {e}") from None
        if proc.returncode != 0:
            # 檔案沒有進 annex（或不是 git repo）→ 回傳 None 讓呼叫端決定
            return None
        return proc.stdout.strip() or None

    def get_key(self, key: str, *, from_remote: str = "origin") -> None:
        try:
            proc = subprocess.run(
                ["git", "annex", "get", f"--key={key}", f"--from={from_remote}"],
                cwd=self.workdir, capture_output=True, text=True, errors="replace",
                env=self._get_env(), timeout=600.0, check=False,
            )
        except subprocess.TimeoutExpired:
            raise ReadError("git annex get 逾時") from None
        except (OSError, subprocess.SubprocessError) as e:
            raise ReadError(f"git annex get 失敗: {e}") from None
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip().splitlines()
            raise ReadError(
                f"從遠端取回 annex 物件失敗 (rc={proc.returncode}): {key}"
                + (f"｜{stderr[-1][:200]}" if stderr else "")
            )

    def local_refs(self, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
        """查詢本地指定分支之完整 ref 集合（refs/heads/<branch> -> sha）。若缺少任一分支拋出 ReadError。"""
        ref_patterns = [f"refs/heads/{b}" if not b.startswith("refs/") else b for b in branches]
        cmd = ["git", "for-each-ref", "--format=%(refname) %(objectname)"] + ref_patterns
        stdout = self._run(cmd, is_write=False)
        found: dict[str, str] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                found[parts[0]] = parts[1]

        missing = [p for p in ref_patterns if p not in found]
        if missing:
            raise ReadError(f"缺少必要之本地分支: {', '.join(missing)}")
        return found

    def local_manifest_sha256(self, remote_uuid: str) -> str | None:
        """H1：push 之後本機 git-remote-annex 狀態裡那份 manifest 的內容雜湊。

        路徑是 git-remote-annex 的本機快取：`.git/annex/git-remote-annex/<uuid>/
        manifest`。用 `git rev-parse --absolute-git-dir` 取得 git 目錄，不要假設
        工作目錄下的 `.git`（worktree／子模組的形狀會不同）。
        """
        git_dir = self._run(
            ["git", "rev-parse", "--absolute-git-dir"], is_write=False
        ).strip()
        if not git_dir:
            return None
        manifest = Path(git_dir) / "annex" / "git-remote-annex" / remote_uuid / "manifest"
        try:
            data = manifest.read_bytes()
        except OSError:
            # 還沒 push 過、或這個 remote 不是 git-remote-annex：沒有這個證據。
            return None
        return hashlib.sha256(data).hexdigest().lower()

    @classmethod
    def clone_for_commit(
        cls,
        url: str,
        dest: Path,
        *,
        max_git_bundles: int = 10,
        largefiles: str = DEFAULT_LARGEFILES,
        timeout: float = 600.0,
    ) -> SubprocessAnnexGit:
        """單一入口完成 clone -b main、git annex init、設定 annex.max-git-bundles 與 annex.largefiles。

        M2：`annex.largefiles` 在**這裡**就設成最終規則（由呼叫端傳入，預設是
        Agora 的 `include=sessions/*/*/raw`；別的實體傳自己的規則），不再設
        `include=*.json`。之前是「先 *.json、再由 AnnexRawStorage 覆寫」，結果
        取決於建構順序：在 AnnexRawStorage 之前寫入的 JSON 會被 annex 收走，
        讀取時要靠 `read_json_file` 的指標相容層才能讀回來（整合測試的
        「第二輪讀到指標文字」就是這個設定的後遺症）。

        同時驗證 clone 後 git-annex 分支存在。
        """
        from aistorage.safety import assert_safe_workdir

        # M8：clone 目的地會被 `git annex init` 寫入 git-annex 分支與 annex filter，
        # 所以必須先確認它不在專案 repo 之內。
        dest_path = assert_safe_workdir(dest, purpose="真本 clone 目的地")
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # 不要求設定存在：這裡也會被本機／測試的純本機 repo 呼叫。
        # RCLONE_CONFIG 一律被釘死，所以真的 clone rclone special remote 時
        # 也不會 fallback 到使用者的預設設定。
        env = get_git_env()

        # 1. clone -b main
        proc = subprocess.run(
            ["git", "clone", "-b", "main", url, str(dest_path)],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise ReadError(f"Git clone 失敗 (rc={proc.returncode})")

        inst = cls(dest_path)

        # 2. git annex init
        proc_init = subprocess.run(
            ["git", "-C", str(dest_path), "annex", "init"],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=60.0,
            check=False,
        )
        if proc_init.returncode != 0:
            raise ReadError(f"Git annex init 失敗 (rc={proc_init.returncode})")

        # 3. git config annex.max-git-bundles <N> 與 annex.largefiles
        inst._run(
            ["git", "config", "annex.max-git-bundles", str(max_git_bundles)],
            is_write=True,
        )
        inst._run(
            ["git", "config", "annex.largefiles", largefiles],
            is_write=True,
        )

        # 4. 驗證 git-annex 分支存在
        chk = subprocess.run(
            ["git", "-C", str(dest_path), "rev-parse", "--verify", "origin/git-annex"],
            capture_output=True,
            check=False,
        )
        if chk.returncode != 0:
            # 亦檢查本地 git-annex
            chk_local = subprocess.run(
                ["git", "-C", str(dest_path), "rev-parse", "--verify", "git-annex"],
                capture_output=True,
                check=False,
            )
            if chk_local.returncode != 0:
                raise ReadError("遠端倉庫缺少必要之 git-annex 分支")

        return inst
