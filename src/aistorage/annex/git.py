"""AiStorage AnnexGit: 包裝 git 與 git-annex subprocess 操作。

依據規格：docs/impl/group3-modules.md 第 1、3.5 節
- AnnexGit: clone, ls_remote, add, commit, copy, push
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Protocol, runtime_checkable

from aistorage.errors import ReadError, WriteError


@runtime_checkable
class AnnexGit(Protocol):
    """AnnexGit 操作協定。"""

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        """查詢遠端 ref 集合（ref -> sha）。"""
        ...

    def add(self, paths: list[str | Path] | str | Path) -> None:
        """執行 git add。"""
        ...

    def commit(self, message: str) -> str:
        """執行 git commit，回傳 commit sha。"""
        ...

    def copy(self, remote: str, to_copy: list[str] | None = None) -> None:
        """執行 git annex copy。"""
        ...

    def push(self, remote: str = "origin", branch: str | None = None) -> None:
        """執行 git push。"""
        ...


class SubprocessAnnexGit:
    """以 subprocess 呼叫本機 git / git-annex 之實作。"""

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir).resolve()

    def _run(self, cmd: list[str], *, is_write: bool = False) -> str:
        proc = subprocess.run(
            cmd,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err_cls = WriteError if is_write else ReadError
            raise err_cls(f"Git 命令失敗 ({' '.join(cmd)}): {proc.stderr}")
        return proc.stdout

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        stdout = self._run(["git", "ls-remote", remote])
        refs: dict[str, str] = {}
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                refs[parts[1]] = parts[0]
        return refs

    def add(self, paths: list[str | Path] | str | Path) -> None:
        if isinstance(paths, (str, Path)):
            path_list = [str(paths)]
        else:
            path_list = [str(p) for p in paths]
        self._run(["git", "add"] + path_list, is_write=True)

    def commit(self, message: str) -> str:
        self._run(["git", "commit", "-m", message], is_write=True)
        sha = self._run(["git", "rev-parse", "HEAD"]).strip()
        return sha

    def copy(self, remote: str, to_copy: list[str] | None = None) -> None:
        cmd = ["git", "annex", "copy", f"--to={remote}"]
        if to_copy:
            cmd.extend(to_copy)
        self._run(cmd, is_write=True)

    def push(self, remote: str = "origin", branch: str | None = None) -> None:
        cmd = ["git", "push", remote]
        if branch:
            cmd.append(branch)
        self._run(cmd, is_write=True)

    @classmethod
    def clone(cls, url: str, dest: Path) -> SubprocessAnnexGit:
        dest_path = Path(dest).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", url, str(dest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ReadError(f"Git clone 失敗 ({url}): {proc.stderr}")
        return cls(dest_path)
