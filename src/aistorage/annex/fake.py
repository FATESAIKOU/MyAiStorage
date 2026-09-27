"""AiStorage AnnexGit 測試假實作（FakeAnnexGit）。

依據規格：docs/impl/group3-modules.md 第 1 節
- FakeAnnexGit: 供單元測試模擬 AnnexGit，記錄呼叫歷史與自訂 ref 集合
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aistorage.annex.git import AnnexGit


class FakeAnnexGit(AnnexGit):
    """記憶體中模擬 AnnexGit 操作。"""

    def __init__(
        self,
        refs: dict[str, str] | None = None,
        workdir: Path | None = None,
    ) -> None:
        self.refs: dict[str, str] = dict(refs or {})
        self.workdir: Path = workdir or Path("/mock/repo")
        self.added_paths: list[str] = []
        self.commits: list[tuple[str, str]] = []  # (message, sha)
        self.copied: list[tuple[str, list[str] | None]] = []
        self.pushed: list[tuple[str, str | None]] = []

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        return dict(self.refs)

    def add(self, paths: list[str | Path] | str | Path) -> None:
        if isinstance(paths, (str, Path)):
            self.added_paths.append(str(paths))
        else:
            self.added_paths.extend(str(p) for p in paths)

    def commit(self, message: str) -> str:
        sha = hashlib.sha1(f"{message}-{len(self.commits)}".encode("utf-8")).hexdigest()
        self.commits.append((message, sha))
        return sha

    def copy(self, remote: str, to_copy: list[str] | None = None) -> None:
        self.copied.append((remote, to_copy))

    def push(self, remote: str = "origin", branch: str | None = None) -> None:
        self.pushed.append((remote, branch))
