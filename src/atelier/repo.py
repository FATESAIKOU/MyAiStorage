"""The reading layer: the harness lives in the shared MyAiStorage clone (D6).

This mirrors angareion's §11 (src/angareion/channel_config.py) without
changing angareion: the same clone (angareion's `config_dir`, or
`ATELIER_CONFIG_DIR`), the same fetch lock file (`<clone>/.git/
angareion-pull.lock`), the same 20-second git timeout, the same pull interval
(angareion's `config_pull_interval`, 900 seconds when it is not set) and the
same policy - `git fetch origin main` to refresh, `git show origin/main:<path>`
to read, never the working tree. The only local writes are the fetch itself and
its lock file, and a failed fetch warns once and keeps the current copy (exit
code unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Protocol, Sequence

from atelier.harness import HarnessId, path_for, parse_path

try:
    import fcntl
except ImportError:  # non-POSIX: run without the cross-process lock
    fcntl = None  # type: ignore[assignment]

#: The git timeout and the pull policy, the same numbers angareion uses (§11).
GIT_TIMEOUT_SECONDS = 20.0
CONFIG_PULL_INTERVAL_DEFAULT = 900
MIN_EVENT_FETCH_INTERVAL_SECONDS = 30.0

#: The lock file angareion's FileLock takes; sharing it serializes both tools.
FETCH_LOCK_FILENAME = "angareion-pull.lock"

#: Where the harness tree starts inside the clone.
HARNESS_PREFIX = "atelier"


class RepoError(RuntimeError):
    """The copy or git cannot answer (exit 2)."""


class HarnessNotFound(Exception):
    """No such harness id, role or version in the copy (exit 1)."""


class GitError(Exception):
    """The git command could not run (missing binary, timeout)."""


@dataclass
class GitResult:
    returncode: int
    stdout: bytes = b""
    stderr: str = ""


class GitRunner(Protocol):
    """Runs one git command in the clone; injectable so a test can watch it."""

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
        ...


class SubprocessGitRunner:
    """The real git runner: output captured, stdout kept as bytes for `show`."""

    def run(self, args: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise GitError(f"git is not available: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args)} timed out after {timeout:.0f}s") from e
        return GitResult(proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace"))


class FileLock:
    """An exclusive flock on a file: same shape as angareion's FileLock.

    The lock file lives inside the clone's `.git`, the same one angareion takes,
    so a fetch here and a fetch there never run at once on one machine.
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


def _default_warn(message: str) -> None:
    sys.stderr.write(f"Warning: {message}\n")


def _angareion_config_data() -> object:
    """angareion's config.json as parsed JSON, or None when absent or unreadable.

    angareion's config is only read for `config_dir` (the clone both tools
    share) and `config_pull_interval` (the pull policy both tools share); no
    token or other field is read or needed.
    """
    angareion_config = os.environ.get("ANGAREION_CONFIG")
    config_file = (Path(angareion_config).expanduser() if angareion_config
                   else Path("~/.config/angareion/config.json").expanduser())
    if config_file.is_dir():
        config_file = config_file / "config.json"
    try:
        return json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def configured_clone_path() -> Path | None:
    """Where the shared copy is: `ATELIER_CONFIG_DIR`, else angareion's `config_dir`."""
    override = os.environ.get("ATELIER_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    data = _angareion_config_data()
    if not isinstance(data, dict):
        return None
    clone = data.get("config_dir")
    if not isinstance(clone, str) or not clone.strip():
        return None
    return Path(clone).expanduser()


def configured_pull_interval() -> int | None:
    """angareion's `config_pull_interval`, honoured when it is set and sane (L4).

    `0` means "no periodic pull" there and means the same here; a missing or
    unusable value leaves the caller on the 900-second default.
    """
    data = _angareion_config_data()
    if not isinstance(data, dict):
        return None
    value = data.get("config_pull_interval")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class Resolved:
    """One harness version read out of the clone."""

    ident: HarnessId      # version is always set here
    path: str
    raw: bytes


class Repo:
    """Read the harness tree from the local clone (design D6, spec「讀取工具」)."""

    def __init__(
        self,
        clone: Path | str,
        *,
        git: GitRunner | None = None,
        interval: int = CONFIG_PULL_INTERVAL_DEFAULT,
        warn: Callable[[str], None] | None = None,
        now: Callable[[], float] = time.time,
        fetch_head_mtime: Callable[[], float | None] | None = None,
        lock: Callable[[], object] | None = None,
    ):
        self.clone = Path(clone)
        self.interval = interval
        self.lock_path = self.clone / ".git" / FETCH_LOCK_FILENAME
        self._runner = git or SubprocessGitRunner()
        self._warn = warn or _default_warn
        self._now = now
        self._fetch_head_mtime = fetch_head_mtime or self._read_fetch_head_mtime
        self._lock = lock or (lambda: FileLock(self.lock_path))
        self._tried_fetch = False   # one fetch attempt per command, not one per lookup

    @classmethod
    def from_env(cls, **kwargs) -> Repo:
        clone = configured_clone_path()
        if clone is None:
            raise RepoError(
                "找不到本機複本：設 ATELIER_CONFIG_DIR，或在 angareion 的 config.json 設 config_dir"
                "（複本＝MyAiStorage 的 clone；見 docs/atelier/tool.md）"
            )
        if "interval" not in kwargs:
            configured = configured_pull_interval()
            kwargs["interval"] = (CONFIG_PULL_INTERVAL_DEFAULT if configured is None
                                  else configured)
        return cls(clone, **kwargs)

    # -- the clone itself --------------------------------------------------

    def _ensure_clone(self) -> None:
        if not self.clone.is_dir():
            raise RepoError(
                f"本機複本不存在：{self.clone}（先 clone MyAiStorage 到這個路徑；見 docs/atelier/tool.md）"
            )
        git_dir = self.clone / ".git"
        if not (git_dir.is_dir() and (git_dir / "HEAD").exists()):
            # A git worktree or submodule has a `.git` *file*, and a plain or
            # empty directory can fake one. The shared lock lives under .git/,
            # so both are refused here, before the lock is taken: a lock file
            # must never be left in something that is not a copy (中1/L4).
            raise RepoError(
                f"本機複本不是 git 複本（沒有 .git 目錄）：{self.clone}"
                "（先 clone MyAiStorage 到這個路徑；見 docs/atelier/tool.md）"
            )

    def _read_fetch_head_mtime(self) -> float | None:
        try:
            return os.stat(self.clone / "FETCH_HEAD").st_mtime
        except OSError:
            return None

    # -- fetching ----------------------------------------------------------

    def fetch(self) -> bool:
        """Refresh the clone, serialized with angareion (the same lock file).

        One attempt per command: `_tried_fetch` keeps a later lookup miss from
        fetching (and warning) a second time after the periodic pull already
        ran. A lock that cannot be taken at all is a copy problem, not a crash.
        """
        self._ensure_clone()   # never create a stray .git under a missing or non-git directory
        self._tried_fetch = True
        try:
            with self._lock():
                return self._fetch()
        except OSError as e:
            raise RepoError(f"取不到共用鎖（{self.lock_path}）：{e}") from e

    def _fetch(self) -> bool:
        """One `git fetch origin main`; a failure warns once and keeps the copy."""
        self._ensure_clone()
        try:
            result = self._runner.run(["fetch", "--quiet", "origin", "main"],
                                      cwd=self.clone, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as e:
            self._warn(f"could not refresh the harness copy: {e}; using the current copy")
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout.decode("utf-8", "replace")).strip() or "git fetch failed"
            self._warn(f"could not refresh the harness copy: {detail}; using the current copy")
            return False
        return True

    def ensure_fresh(self) -> None:
        """The periodic pull at the start of a command (0 = only on lookups)."""
        if self.interval <= 0:
            return
        mtime = self._fetch_head_mtime()
        if mtime is None or self._now() - mtime > self.interval:
            self.fetch()

    def refresh_for_lookup(self) -> None:
        """A lookup miss asks for fresher data, once per command and not more often
        than every 30s - a fetch that already ran (and may have failed) is not tried
        again, so an offline miss warns exactly once."""
        if self._tried_fetch:
            return
        mtime = self._fetch_head_mtime()
        if mtime is not None and self._now() - mtime < MIN_EVENT_FETCH_INTERVAL_SECONDS:
            return
        self.fetch()

    # -- reading -----------------------------------------------------------

    def _run(self, args: list[str]) -> GitResult:
        self._ensure_clone()
        try:
            return self._runner.run(args, cwd=self.clone, timeout=GIT_TIMEOUT_SECONDS)
        except GitError as e:
            raise RepoError(f"git 讀取失敗（{self.clone}）：{e}") from e

    def list_paths(self, prefix: str = HARNESS_PREFIX) -> list[str]:
        """Every path under `prefix` at origin/main; the working tree is not read."""
        result = self._run(["ls-tree", "-r", "--name-only", "origin/main", "--", prefix])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout.decode("utf-8", "replace")).strip()
            raise RepoError(f"讀不到 harness 樹（{self.clone}）：{detail}")
        return [line for line in result.stdout.decode("utf-8", "replace").splitlines() if line]

    def read(self, path: str) -> bytes | None:
        """The file's exact bytes at origin/main, or None when it is not there."""
        result = self._run(["show", f"origin/main:{path}"])
        if result.returncode == 0:
            return result.stdout
        detail = (result.stderr or result.stdout.decode("utf-8", "replace")).strip()
        if result.returncode == 128 and ("does not exist in" in detail or "exists on disk, but not in" in detail):
            return None
        raise RepoError(f"讀不到 {path}（{self.clone}）：{detail}")

    def entries(self) -> list[tuple[str, str, int]]:
        """(team, role, version) for every `atelier/.../v<N>.md` in the copy."""
        found: list[tuple[str, str, int]] = []
        for path in self.list_paths():
            parsed = parse_path(path)
            if parsed is not None:
                found.append(parsed)
        return found

    def versions(self, team: str, role: str) -> list[int]:
        return sorted({version for t, r, version in self.entries() if (t, r) == (team, role)})

    def resolve(self, ident: HarnessId) -> Resolved:
        """The pinned version, or the largest one when none is pinned (D4).

        A miss fetches once (the 30-second throttle still applies) and retries.
        """
        if ident.version is not None:
            path = path_for(ident.team, ident.role, ident.version)
            raw = self.read(path)
            if raw is None:
                self.refresh_for_lookup()
                raw = self.read(path)
            if raw is None:
                raise HarnessNotFound(
                    f"找不到版本：atelier:{ident.name}@v{ident.version}（複本裡沒有這一版）"
                )
            return Resolved(ident=ident, path=path, raw=raw)

        versions = self.versions(ident.team, ident.role)
        if not versions:
            self.refresh_for_lookup()
            versions = self.versions(ident.team, ident.role)
        if not versions:
            raise HarnessNotFound(
                f"找不到 harness：atelier:{ident.name}（複本裡沒有這個職務的版本）"
            )
        version = max(versions)
        pinned = HarnessId(team=ident.team, role=ident.role, version=version)
        path = path_for(ident.team, ident.role, version)
        raw = self.read(path)
        if raw is None:  # raced a fetch: say not found rather than crash
            raise HarnessNotFound(f"找不到版本：atelier:{ident.name}@v{version}")
        return Resolved(ident=pinned, path=path, raw=raw)
