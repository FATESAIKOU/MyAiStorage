"""The channel/backend config files on the local clone (v5).

channels.json maps a channel name to a backend entity; backends.json describes
the backends themselves. The files live in the MyAiStorage repo; angareion only
reads the local clone — `git fetch` to refresh, `git show origin/main:<path>`
to read (never the working tree). Only the registry-rebuild workflow writes
channels.json.

This module holds the reading side plus the pull policy; a test can inject the
git runner and the clocks, so nothing here touches the network by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Protocol, Sequence

from angareion.backend import ChannelNotFoundError
from angareion.config import CONFIG_PULL_INTERVAL_DEFAULT, ConfigError

CHANNELS_PATH = "config/angareion/channels.json"
BACKENDS_PATH = "config/angareion/backends.json"
CONFIG_VERSION = 1
GIT_TIMEOUT_SECONDS = 20.0
LANDING_POLL_TIMEOUT_SECONDS = 120.0
LANDING_POLL_INTERVAL_SECONDS = 5.0
MIN_EVENT_FETCH_INTERVAL_SECONDS = 30.0


class ChannelConfigError(ConfigError):
    """The local configuration copy is missing, broken or inconsistent (exit 2)."""


class BackendChoiceError(ValueError):
    """`channel create` cannot pick a backend from the arguments and config (exit 1)."""


class GitError(Exception):
    """The git command could not run (missing binary, timeout)."""


@dataclass
class GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class GitRunner(Protocol):
    """Runs one git command in the clone; injectable so tests never shell out."""

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
        ...


class SubprocessGitRunner:
    """The real git runner: a fixed 20-second timeout, output captured."""

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise GitError(f"git is not available: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args)} timed out after {timeout:.0f}s") from e
        return GitResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class ChannelRef:
    """One channels.json entry: the backend entity a channel name points to."""

    name: str
    backend: str
    issue: int


@dataclass(frozen=True)
class BackendRef:
    """One backends.json entry."""

    name: str
    type: str
    repo: str


def _default_warn(message: str) -> None:
    sys.stderr.write(f"Warning: {message}\n")


class ChannelStore:
    """Read channels.json / backends.json from the local clone, with the pull policy.

    `resolve` answers "which entity is this channel"; a miss triggers one
    event-driven fetch (unless `config_pull_interval=0`... which only disables
    the periodic pull, never the event-driven ones).
    """

    def __init__(
        self,
        config_dir: Path | str,
        *,
        git: GitRunner | None = None,
        interval: int = CONFIG_PULL_INTERVAL_DEFAULT,
        warn: Callable[[str], None] | None = None,
        now: Callable[[], float] = time.time,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        fetch_head_mtime: Callable[[], float | None] | None = None,
    ):
        self.config_dir = Path(config_dir)
        self.interval = interval
        self._git = git or SubprocessGitRunner()
        self._warn = warn or _default_warn
        self._now = now
        self._clock = clock
        self._sleep = sleeper
        self._fetch_head_mtime = fetch_head_mtime or self._read_fetch_head_mtime

    # -- reading ---------------------------------------------------------

    def _read_fetch_head_mtime(self) -> float | None:
        try:
            return os.stat(self.config_dir / "FETCH_HEAD").st_mtime
        except OSError:
            return None

    def _show(self, path: str) -> str:
        if not self.config_dir.is_dir():
            raise ChannelConfigError(
                f"the local configuration copy does not exist: {self.config_dir} "
                "(clone the MyAiStorage repo there, or fix config_dir)"
            )
        try:
            result = self._git.run(["show", f"origin/main:{path}"], cwd=self.config_dir, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as e:
            raise ChannelConfigError(f"cannot read the configuration file {path}: {e}") from e
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ChannelConfigError(
                f"the configuration file is missing from the local copy: {path}"
                + (f" ({detail})" if detail else "")
            )
        return result.stdout

    def _load_json(self, path: str) -> dict[str, Any]:
        text = self._show(path)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ChannelConfigError(f"the configuration file is not valid JSON: {path}: {e}") from e
        if not isinstance(data, dict):
            raise ChannelConfigError(f"the configuration file must be a JSON object: {path}")
        version = data.get("version")
        if version != CONFIG_VERSION:
            raise ChannelConfigError(
                f"unsupported configuration version {version!r} in {path} (expected {CONFIG_VERSION})"
            )
        return data

    def channels(self) -> dict[str, ChannelRef]:
        """Every channel mapping in the local copy, or a ChannelConfigError."""
        data = self._load_json(CHANNELS_PATH)
        raw = data.get("channels", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ChannelConfigError(f"'channels' must be an object in {CHANNELS_PATH}")
        channels: dict[str, ChannelRef] = {}
        for name, entry in raw.items():
            if not isinstance(name, str) or not name:
                raise ChannelConfigError(f"a channel name in {CHANNELS_PATH} is not a string")
            if not isinstance(entry, dict):
                raise ChannelConfigError(f"the entry for channel '{name}' in {CHANNELS_PATH} is not an object")
            backend = entry.get("backend")
            issue = entry.get("issue")
            if not isinstance(backend, str) or not backend:
                raise ChannelConfigError(f"channel '{name}' in {CHANNELS_PATH} has no backend name")
            if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
                raise ChannelConfigError(f"channel '{name}' in {CHANNELS_PATH} has no entity number")
            channels[name] = ChannelRef(name=name, backend=backend, issue=issue)
        return channels

    def backends(self) -> dict[str, BackendRef]:
        """Every backend entry in the local copy, or a ChannelConfigError."""
        data = self._load_json(BACKENDS_PATH)
        raw = data.get("backends", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ChannelConfigError(f"'backends' must be an object in {BACKENDS_PATH}")
        backends: dict[str, BackendRef] = {}
        for name, entry in raw.items():
            if not isinstance(name, str) or not name:
                raise ChannelConfigError(f"a backend name in {BACKENDS_PATH} is not a string")
            if not isinstance(entry, dict):
                raise ChannelConfigError(f"the entry for backend '{name}' in {BACKENDS_PATH} is not an object")
            backend_type = entry.get("type")
            if not isinstance(backend_type, str) or not backend_type:
                raise ChannelConfigError(f"backend '{name}' in {BACKENDS_PATH} has no type")
            repo = entry.get("repo", "")
            if backend_type == "github" and (not isinstance(repo, str) or not repo.strip()):
                raise ChannelConfigError(f"github backend '{name}' in {BACKENDS_PATH} has no repo")
            backends[name] = BackendRef(
                name=name, type=backend_type, repo=str(repo) if isinstance(repo, str) else ""
            )
        return backends

    def backend(self, name: str) -> BackendRef:
        """The backend with this name, checked for a supported type (exit 2 otherwise)."""
        backend = self.backends().get(name)
        if backend is None:
            raise ChannelConfigError(f"the channel configuration points to an unknown backend: {name}")
        if backend.type != "github":
            raise ChannelConfigError(f"unsupported backend type {backend.type!r} for backend '{name}'")
        return backend

    # -- resolution ------------------------------------------------------

    def resolve(self, name: str) -> ChannelRef | None:
        """The entity for a channel name, or None (the channel does not exist)."""
        ref = self.channels().get(name)
        if ref is None:
            return None
        self.backend(ref.backend)  # unknown backend / type is a config error, not "no channel"
        return ref

    def resolve_or_raise(self, name: str) -> ChannelRef:
        ref = self.resolve(name)
        if ref is None:
            raise ChannelNotFoundError(f"channel '{name}' does not exist")
        return ref

    def channel_names(self) -> list[str]:
        return list(self.channels())

    def choose_backend_for_create(self, requested: str | None) -> BackendRef:
        """The backend `channel create` should open the entity in.

        A usage mistake raises BackendChoiceError (exit 1); an unsupported type
        raises ChannelConfigError (exit 2).
        """
        backends = self.backends()
        if requested is not None:
            backend = backends.get(requested)
            if backend is None:
                raise BackendChoiceError(f"backend '{requested}' is not configured")
        elif len(backends) == 1:
            backend = next(iter(backends.values()))
        elif not backends:
            raise BackendChoiceError("no backend is configured; add one to config/angareion/backends.json")
        else:
            raise BackendChoiceError("several backends are configured; pass --backend <name>")
        if backend.type != "github":
            raise ChannelConfigError(f"unsupported backend type {backend.type!r} for backend '{backend.name}'")
        return backend
