"""Shared fakes for the angareion v5 unit tests (no network, no real GitHub).

The `FakeGit` stands in for the real git runner of `ChannelStore`: it keeps the
files that would be in `origin/main` in memory and records every command, so a
test can watch the fetch policy and the reads without touching the disk.

`FakeRegistryClient` (the in-memory registry issue) lives in the package
(`angareion.registry`) because it is part of the product's test surface; it is
re-exported here for convenience.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from angareion.backend import ChannelNotFoundError, Entity
from angareion.channel_config import GitResult
from angareion.registry import FakeRegistryClient, RegistryWriteConflict  # noqa: F401  (re-export)

__all__ = [
    "FakeGit",
    "FakeRegistryClient",
    "RegistryWriteConflict",
    "StaticResolver",
    "backends_json",
    "channels_json",
    "entity",
]


def entity(channel: str = "general", backend: str = "github-main", repo: str = "owner/repo", issue: int = 1) -> Entity:
    """A resolved channel entity for tests."""
    return Entity(channel=channel, backend=backend, repo=repo, issue=issue)


class StaticResolver:
    """An EntityResolver over a fixed mapping; the store's behaviour without git."""

    def __init__(self, entities: dict[str, Entity] | list[Entity]):
        if isinstance(entities, list):
            entities = {item.channel: item for item in entities}
        self._entities = dict(entities)

    def entity_for(self, channel: str) -> Entity:
        if channel not in self._entities:
            raise ChannelNotFoundError(f"channel '{channel}' does not exist")
        return self._entities[channel]

    def entities(self) -> list[Entity]:
        return list(self._entities.values())


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
        self.on_fetch: Callable[[], None] | None = None

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
        self.commands.append(list(args))
        if args and args[0] == "fetch":
            self.fetch_calls += 1
            self.fetch_timeouts.append(timeout)
            if self.raise_on_fetch is not None:
                raise self.raise_on_fetch
            if self.on_fetch is not None:
                self.on_fetch()
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
