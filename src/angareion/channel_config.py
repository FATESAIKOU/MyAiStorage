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
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Protocol, Sequence

from angareion.backend import BackendRef, ChannelNotFoundError, Entity, RetiredPage
from angareion.config import CONFIG_PULL_INTERVAL_DEFAULT, ConfigError

try:
    import fcntl
except ImportError:  # non-POSIX: run without the cross-process lock
    fcntl = None  # type: ignore[assignment]

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
    """One channels.json entry: the backend entity a channel name points to.

    `issue` is the current page; `retired` are the pages it moved off, oldest first.
    """

    name: str
    backend: str
    issue: int
    retired: tuple[RetiredPage, ...] = ()


_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")


def _is_offset_time(value: Any) -> bool:
    """Whether a value is an RFC 3339 time with its offset written (design 542 C-6)."""
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return False
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _retired_pages(name: str, raw: Any, current: int) -> tuple[RetiredPage, ...]:
    """The `retired` list of one channels.json entry (design 542 C-6), in file order."""
    where = f"channel '{name}' in {CHANNELS_PATH}"
    if not isinstance(raw, list):
        raise ChannelConfigError(f"the 'retired' of {where} is not a list")
    pages: list[RetiredPage] = []
    numbers = {current}
    for item in raw:
        if not isinstance(item, dict):
            raise ChannelConfigError(f"a 'retired' entry of {where} is not an object")
        issue = item.get("issue")
        if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
            raise ChannelConfigError(f"a 'retired' entry of {where} has no entity number")
        if not _is_offset_time(item.get("until")):
            raise ChannelConfigError(f"a 'retired' entry of {where} has no 'until' with a time zone")
        if "side" in item and item["side"] is not True:
            raise ChannelConfigError(f"a 'retired' entry of {where} has a 'side' that is not true")
        if issue in numbers:
            raise ChannelConfigError(f"'retired' of {where} repeats entity number {issue}")
        numbers.add(issue)
        pages.append(RetiredPage(issue=issue, until=item["until"], side="side" in item))
    return tuple(pages)


def _default_warn(message: str) -> None:
    sys.stderr.write(f"Warning: {message}\n")


class FileLock:
    """An exclusive flock on a file: a second process waits for the first.

    The lock file lives inside the clone's `.git` so it never shows up in the
    working tree; without POSIX flock the lock degrades to a no-op.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        if fcntl is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


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
        lock: Callable[[], Any] | None = None,
    ):
        self.config_dir = Path(config_dir)
        self.interval = interval
        self._git = git or SubprocessGitRunner()
        self._warn = warn or _default_warn
        self._now = now
        self._clock = clock
        self._sleep = sleeper
        self._fetch_head_mtime = fetch_head_mtime or self._read_fetch_head_mtime
        self._lock = lock or (lambda: FileLock(self.config_dir / ".git" / "angareion-pull.lock"))

    # -- pulling ---------------------------------------------------------

    def _fetch(self) -> bool:
        """One `git fetch origin main`; failures warn once and keep the copy."""
        try:
            result = self._git.run(["fetch", "--quiet", "origin", "main"], cwd=self.config_dir, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as e:
            self._warn(f"could not refresh the configuration copy: {e}; using the current copy")
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "git fetch failed"
            self._warn(f"could not refresh the configuration copy: {detail}; using the current copy")
            return False
        return True

    def fetch(self) -> bool:
        """Refresh the clone, serialized with other angareion processes on this machine."""
        with self._lock():
            return self._fetch()

    def ensure_fresh(self) -> None:
        """The periodic pull: at the start of any command using the config files.

        `config_pull_interval=0` disables only this periodic pull; the
        event-driven fetches below still happen.
        """
        if self.interval <= 0:
            return
        mtime = self._fetch_head_mtime()
        if mtime is None or self._now() - mtime > self.interval:
            self.fetch()

    def refresh_for_lookup(self) -> None:
        """A lookup miss asks for fresher data, but not more often than every 30s."""
        mtime = self._fetch_head_mtime()
        if mtime is not None and self._now() - mtime < MIN_EVENT_FETCH_INTERVAL_SECONDS:
            return
        self.fetch()

    def poll_for_channel(self, name: str, *, timeout: float = LANDING_POLL_TIMEOUT_SECONDS) -> bool:
        """Poll the clone until a channel mapping appears (or the wait runs out).

        This is the landing wait of `channel create`; it is not used while a
        command is otherwise polling (wait never fetches after it started).
        """
        return self._poll_until(lambda: name in self.channels(), timeout)

    def poll_for_page(self, name: str, issue: int, *, timeout: float = LANDING_POLL_TIMEOUT_SECONDS) -> bool:
        """Poll the clone until the channel's current page is `issue` (the landing wait of `channel rollover`)."""
        def on_page() -> bool:
            ref = self.channels().get(name)
            return ref is not None and ref.issue == issue

        return self._poll_until(on_page, timeout)

    def _poll_until(self, landed: Callable[[], bool], timeout: float) -> bool:
        deadline = self._clock() + timeout
        while True:
            self.fetch()
            if landed():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(min(LANDING_POLL_INTERVAL_SECONDS, max(0.0, deadline - self._clock())))

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
            retired = _retired_pages(name, entry["retired"], issue) if "retired" in entry else ()
            channels[name] = ChannelRef(name=name, backend=backend, issue=issue, retired=retired)
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
        """The entity for a channel name, or None (the channel does not exist).

        A miss asks for one event-driven fetch first (not more often than
        every 30 seconds), so a channel a teammate just created is found.
        """
        ref = self.channels().get(name)
        if ref is None:
            self.refresh_for_lookup()
            ref = self.channels().get(name)
        if ref is None:
            return None
        self.backend(ref.backend)  # unknown backend / type is a config error, not "no channel"
        return ref

    def entity_for(self, name: str) -> Entity:
        """The full target of a channel: its backend repo and entity number."""
        ref = self.resolve(name)
        if ref is None:
            raise ChannelNotFoundError(f"channel '{name}' does not exist")
        backend = self.backend(ref.backend)
        return Entity(
            channel=name, backend=backend.name, repo=backend.repo, issue=ref.issue, retired=ref.retired
        )

    def entities(self) -> list[Entity]:
        """Every channel as a target ("to me" spans them all)."""
        backends = self.backends()
        entities: list[Entity] = []
        for name, ref in self.channels().items():
            backend = backends.get(ref.backend)
            if backend is None:
                raise ChannelConfigError(f"the channel configuration points to an unknown backend: {ref.backend}")
            if backend.type != "github":
                raise ChannelConfigError(
                    f"unsupported backend type {backend.type!r} for backend '{backend.name}'"
                )
            entities.append(
                Entity(
                    channel=name, backend=backend.name, repo=backend.repo, issue=ref.issue, retired=ref.retired
                )
            )
        return entities

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
