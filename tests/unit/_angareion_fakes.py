"""Shared fakes for the angareion v5 unit tests (no network, no real GitHub).

The `FakeGit` stands in for the real git runner of `ChannelStore`: it keeps the
files that would be in `origin/main` in memory and records every command, so a
test can watch the fetch policy and the reads without touching the disk. The
`FakeRegistryClient` is the in-memory registry issue (comment posting, comment
paging, file reads/writes with the sha optimistic lock).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from angareion.channel_config import GitResult


def channels_json(channels: dict[str, tuple[str, int]]) -> str:
    """The channels.json text for {name: (backend, issue)}."""
    return json.dumps(
        {
            "channels": {
                name: {"backend": backend, "issue": issue}
                for name, (backend, issue) in sorted(channels.items())
            },
            "version": 1,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def backends_json(backends: dict[str, tuple[str, str]]) -> str:
    """The backends.json text for {name: (type, repo)}."""
    return json.dumps(
        {
            "backends": {
                name: {"repo": repo, "type": backend_type}
                for name, (backend_type, repo) in sorted(backends.items())
            },
            "version": 1,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


class FakeGit:
    """A GitRunner over an in-memory `origin/main`."""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        fetch_head_mtime: float | None = 0.0,
        raise_on_fetch: Exception | None = None,
        raise_on_show: Exception | None = None,
        missing: bool = False,
    ):
        # keys are "origin/main:config/angareion/channels.json"
        self.files: dict[str, str] = dict(files or {})
        self.fetch_calls = 0
        self.fetch_timeouts: list[float] = []
        self.commands: list[list[str]] = []
        self.fetch_head_mtime = fetch_head_mtime
        self.raise_on_fetch = raise_on_fetch
        self.raise_on_show = raise_on_show

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
        self.commands.append(list(args))
        if args and args[0] == "fetch":
            self.fetch_calls += 1
            self.fetch_timeouts.append(timeout)
            if self.raise_on_fetch is not None:
                raise self.raise_on_fetch
            return GitResult(0, "", "")
        if args and args[0] == "show":
            if self.raise_on_show is not None:
                raise self.raise_on_show
            key = args[1]
            if key in self.files:
                return GitResult(0, self.files[key], "")
            return GitResult(128, "", f"fatal: path '{key}' does not exist in 'origin/main'")
        raise AssertionError(f"unexpected git command: {args}")

    def set_file(self, path: str, text: str) -> None:
        """`path` is repo-relative, e.g. config/angareion/channels.json."""
        self.files[f"origin/main:{path}"] = text

    def show_calls(self) -> list[str]:
        return [command[1] for command in self.commands if command[0] == "show"]


class FakeRegistryClient:
    """The in-memory registry issue: comments, files and the sha lock."""

    def __init__(
        self,
        *,
        comments: Iterable[dict[str, Any]] | None = None,
        files: dict[str, str] | None = None,
        fail_comments_times: int = 0,
        stale_writes: int = 0,
        page_size: int = 100,
    ):
        self._comments: list[dict[str, Any]] = [dict(c) for c in (comments or [])]
        self.files: dict[str, str] = dict(files or {})
        self.posted: list[str] = []
        self.fail_comments_times = fail_comments_times
        self.stale_writes = stale_writes
        self.page_size = page_size
        self.list_calls: list[int] = []
        self.put_calls: list[tuple[str, str | None]] = []
        self._sha = 1

    # -- comments --------------------------------------------------------

    def add_comment(
        self,
        body: str,
        *,
        author_association: str = "OWNER",
        user_type: str = "User",
        comment_id: int | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        index = len(self._comments) + 1
        comment = {
            "id": comment_id if comment_id is not None else index,
            "body": body,
            "author_association": author_association,
            "user": {"type": user_type, "login": "someone"},
            "created_at": created_at or f"2026-10-10T12:00:{index:02d}Z",
        }
        self._comments.append(comment)
        return comment

    def list_comments(self, page: int) -> list[dict[str, Any]]:
        self.list_calls.append(page)
        if self.fail_comments_times > 0:
            self.fail_comments_times -= 1
            raise RuntimeError("comment listing failed")
        start = (page - 1) * self.page_size
        return self._comments[start : start + self.page_size]

    def post_comment(self, body: str) -> None:
        self.posted.append(body)
        self.add_comment(body, author_association="NONE", user_type="Bot")

    # -- files -----------------------------------------------------------

    def get_file(self, path: str) -> tuple[str, str | None]:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path], f"sha-{self._sha}"

    def put_file(self, path: str, text: str, *, sha: str | None, message: str) -> None:
        self.put_calls.append((path, sha))
        if self.stale_writes > 0:
            self.stale_writes -= 1
            raise StaleWriteError("the file changed while rebuilding")
        self.files[path] = text
        self._sha += 1


class StaleWriteError(Exception):
    """The write was rejected because the file moved (sha mismatch)."""
