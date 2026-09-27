"""AiStorage AnnexGit 測試假實作（FakeAnnexGit）。

依據規格：
- docs/impl/group3-modules.md 第 1 節
- review-g3a.md M5（push_effect, inject, annex_keys_in）
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from aistorage.annex.git import AnnexGit
from aistorage.annex.manifest import normalize_ls_remote
from aistorage.errors import WriteError


class FakeAnnexGit(AnnexGit):
    """記憶體中模擬 AnnexGit 操作。"""

    def __init__(
        self,
        refs: dict[str, str] | None = None,
        workdir: Path | None = None,
        annex_keys: frozenset[str] | set[str] | None = None,
        push_effect: Literal["apply", "silent_fail", "error"] = "apply",
        copy_effect: Literal["noop", "annex_upload"] = "noop",
        local_keys: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.refs: dict[str, str] = dict(refs or {})
        self.workdir: Path = workdir or Path("/mock/repo")
        self.annex_keys: frozenset[str] = frozenset(annex_keys or ())
        self.push_effect: Literal["apply", "silent_fail", "error"] = push_effect
        # review-g3g H1：`git annex copy` 會寫入本機 git-annex 分支的 location log
        # （refs/heads/git-annex 的 sha 改變），並讓本機的 key 變成「在 remote 上」。
        # copy_effect="annex_upload" 才模擬這個副作用，預設維持 noop 不影響既有測試。
        self.copy_effect: Literal["noop", "annex_upload"] = copy_effect
        self.local_keys: frozenset[str] = frozenset(local_keys or ())
        self.added_paths: list[str] = []
        self.commits: list[tuple[str, str]] = []
        self.copied: list[tuple[str, list[str] | None]] = []
        self.pushed: list[tuple[str, tuple[str, ...]]] = []
        self.pending_refs: dict[str, str] = {}
        self._injections: dict[str, type[Exception]] = {}

    def inject(self, op: str, error: type[Exception] = WriteError) -> None:
        """注入指定操作的例外。"""
        self._injections[op] = error

    def _check_injection(self, op: str) -> None:
        if op in self._injections:
            err_cls = self._injections.pop(op)
            raise err_cls(f"FakeAnnexGit injected error on {op}")

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        self._check_injection("ls_remote")
        return normalize_ls_remote(self.refs)

    def add(self, paths: list[str | Path] | str | Path) -> None:
        self._check_injection("add")
        if isinstance(paths, (str, Path)):
            self.added_paths.append(str(paths))
        else:
            self.added_paths.extend(str(p) for p in paths)

    def commit(self, message: str) -> str:
        self._check_injection("commit")
        sha = hashlib.sha1(f"{message}-{len(self.commits)}".encode("utf-8")).hexdigest()
        self.commits.append((message, sha))
        # 模擬本地 HEAD 前進
        self.pending_refs["refs/heads/main"] = sha
        return sha

    def copy(
        self,
        remote: str,
        to_copy: list[str] | None = None,
        *,
        timeout: float = 900.0,
    ) -> None:
        self._check_injection("copy")
        self.copied.append((remote, to_copy))
        if self.copy_effect == "annex_upload":
            # 模擬 `git annex copy`：本機的 key 變成在 remote 上，且 location log
            # 讓 refs/heads/git-annex 的 sha 前進（review-g3g H1）
            if self.local_keys:
                self.annex_keys = frozenset(set(self.annex_keys) | set(self.local_keys))
            current = self.pending_refs.get(
                "refs/heads/git-annex", self.refs.get("refs/heads/git-annex", "")
            )
            self.pending_refs["refs/heads/git-annex"] = hashlib.sha1(
                f"annex-location-log-{current}".encode("utf-8")
            ).hexdigest()

    def push(
        self,
        remote: str = "origin",
        branches: tuple[str, ...] | str = ("main", "git-annex"),
        *,
        timeout: float = 900.0,
    ) -> None:
        self._check_injection("push")
        branch_tuple = (branches,) if isinstance(branches, str) else tuple(branches)
        self.pushed.append((remote, branch_tuple))

        if self.push_effect == "error":
            raise WriteError("FakeAnnexGit simulated push network error")
        elif self.push_effect == "silent_fail":
            # 靜默失敗：exit 0，但遠端 refs 不變
            pass
        elif self.push_effect == "apply":
            # 成功：遠端 refs 更新為 pending_refs
            for k, v in self.pending_refs.items():
                self.refs[k] = v

    def annex_keys_in(self, remote_uuid: str) -> frozenset[str]:
        self._check_injection("annex_keys_in")
        return self.annex_keys

    def local_refs(self, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
        self._check_injection("local_refs")
        combined = dict(self.refs)
        combined.update(self.pending_refs)
        res: dict[str, str] = {}
        for b in branches:
            full = f"refs/heads/{b}" if not b.startswith("refs/") else b
            short = b.replace("refs/heads/", "")
            if full in combined:
                res[full] = combined[full]
            elif short in combined:
                res[full] = combined[short]
        return res
