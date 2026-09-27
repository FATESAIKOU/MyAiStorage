"""AiStorage AnnexGit: 包裝 git 與 git-annex subprocess 操作。

依據規格：
- docs/impl/group3-modules.md 第 1、3.5 節
- review-g3a.md M5（clone_for_commit、push 多分支、annex_keys_in、環境隔離與超時）
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from typing import Protocol, runtime_checkable

from aistorage.annex.manifest import normalize_ls_remote
from aistorage.errors import ReadError, WriteError


def get_git_env() -> dict[str, str]:
    """建立隔離的環境變數，防止終端機互動提示與繼承全域設定。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
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


class SubprocessAnnexGit:
    """以 subprocess 呼叫本機 git / git-annex 之實作。"""

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir).resolve()

    def _get_env(self) -> dict[str, str]:
        """建立隔離的環境變數，防止終端機互動提示與繼承全域設定。"""
        return get_git_env()

    def _run(
        self,
        cmd: list[str],
        *,
        is_write: bool = False,
        timeout: float = 60.0,
    ) -> str:
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                errors="replace",
                env=self._get_env(),
                timeout=timeout,
                check=False,
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
        cmd = ["git", "annex", "copy", f"--to={remote}"]
        if to_copy:
            cmd.extend(to_copy)
        self._run(cmd, is_write=True, timeout=timeout)

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
        """M5: 查詢在指定 remote_uuid 上已存在的 annex key 集合。"""
        cmd = ["git", "annex", "find", f"--in={remote_uuid}", "--all", "--format=${key}\n"]
        stdout = self._run(cmd, is_write=False)
        keys = set()
        for line in stdout.splitlines():
            k = line.strip()
            if k:
                keys.add(k)
        return frozenset(keys)

    @classmethod
    def clone_for_commit(
        cls,
        url: str,
        dest: Path,
        *,
        max_git_bundles: int = 20,
        timeout: float = 600.0,
    ) -> SubprocessAnnexGit:
        """單一入口完成 clone -b main、git annex init 與設定 annex.max-git-bundles。

        同時驗證 clone 後 git-annex 分支存在。
        """
        dest_path = Path(dest).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

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

        # 3. git config annex.max-git-bundles <N>
        inst._run(
            ["git", "config", "annex.max-git-bundles", str(max_git_bundles)],
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
