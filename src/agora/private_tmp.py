"""Private temporary directories under agora's own state, and the sweep for leftovers (#116).

An export writes the whole raw conversation to a temporary directory before it
is staged (`claude._exported`, `agy.AgyAgent.export`, `opencode._export_file`,
`agy_store.snapshot`, `agy_summaries._read_summaries`). A `TemporaryDirectory`
in the shared system temp cleaned those on the normal and the failing path, but
nothing can run on the SIGKILL path: the whole conversation stayed in
`$TMPDIR` for whoever found it.

They now live under `<state>/tmp/` (0700) instead, named `<prefix>-<pid>-<random>`,
so a leftover is at least private to the user; and the next round, continue or
export sweeps the ones whose owning process is gone. A sweep only touches a
name this module made (`PREFIXES`, or a setup's `.new-<random>`): `AGORA_STATE_DIR`
is user-controlled, and its `tmp/` may have been the user's before agora was
pointed at it, so nothing else there is ever removed.

**How "gone" is decided: a lock, not the pid.** Every directory holds an
`.owner.lock` file the creating process keeps `flock`ed for as long as the
directory exists. The kernel drops that lock when the process dies - SIGKILL
included - so a sweep that can take the lock knows the owner is gone. The pid
in the name is for a person reading the directory, not for the decision:
pids are reused, and several `agora continue` runs export at the same time, so
"this pid is not alive" alone would eventually delete somebody's live directory
(the same reason `store._build_batch` cannot copy its "delete every
`batch-*`" trick here - that one has `upload.lock` to guarantee a single writer).

Two more races are closed by `<state>/tmp/.create.lock`:

- creating a directory (mkdir, lock, then rename into its final name) happens
  under a shared lock, and a sweep takes it exclusively (non-blocking: if a
  creator has it, this sweep just skips, a later one will clean up). So a
  sweep can never take an owner lock that its process has opened but not yet
  locked, and any `.new-*` it sees while holding the gate was left by a
  process that died mid-setup.
- the name only becomes visible after the owner lock is held, so a reader never
  finds a published directory whose lock is not yet taken.

A sweep is best effort and silent: a state directory that cannot be read (or a
filesystem without usable `flock`) leaves the directories alone this time - the
next sweep tries again, and a cleanup failure never breaks the command that
triggered it. Creating one is the other way round: without `flock` there is no
owner that can be proved dead, so `PrivateTmpDir` fails with a message naming
`AGORA_STATE_DIR` rather than writing the conversation into a directory no
later run could safely clean.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import weakref
from pathlib import Path
from shutil import rmtree as _rmtree

#: The subdirectory of agora's state that holds these directories.
DIRNAME = "tmp"

#: Taken shared while a directory is being set up, exclusively by a sweep.
GATE_LOCK = ".create.lock"

#: Held by the owner for the directory's whole life; released by the kernel on death.
OWNER_LOCK = ".owner.lock"

#: A directory's name before it is published (it has no owner lock yet).
SETUP_PREFIX = ".new-"

#: The random suffix `tempfile.mkdtemp` appends: eight characters from its
#: `_RandomNameSequence` (lowercase letters, digits, underscore).
_RANDOM = r"[a-z0-9_]{8}"

#: The prefixes this module's callers use. This is a closed set on purpose:
#: `AGORA_STATE_DIR` is user-controlled and may point at a `tmp/` that already
#: had directories in it, so `sweep` only touches a name it can prove is ours.
PREFIXES = ("agora-raw", "agora-agy", "agora-agy-raw", "agora-agy-sum", "agora-opencode", "agora-copy-headers")

#: Everything `sweep` may remove: a published name, or one whose setup died.
_OUR_NAME = re.compile(
    r"(?:" + "|".join(map(re.escape, PREFIXES)) + r")-[0-9]+-" + _RANDOM
    + r"|" + re.escape(SETUP_PREFIX) + _RANDOM)

#: One sweep per process is enough: it runs before the process's first export,
#: and a command that creates several temp dirs should not rescan each time.
_swept = False


def state_dir() -> Path:
    """Where agora keeps its state; `AGORA_STATE_DIR` overrides it (the others' rule)."""
    return Path(os.environ.get("AGORA_STATE_DIR") or "~/.local/state/agora").expanduser()


def root(state: Path | None = None, *, create: bool = True) -> Path:
    """`<state>/tmp` - 0700, because everything under it is private conversation bytes."""
    base = (Path(state) if state is not None else state_dir()) / DIRNAME
    if create:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        # `mkdir(mode=…)` leaves an existing directory as it is; a `<state>/tmp`
        # someone made 0755 earlier (or a permissive umask at creation) would
        # expose the names, so set it every time. Ours to chmod: we cannot write
        # in it otherwise.
        os.chmod(base, 0o700)
    return base


def _flock(handle, operation: int, base: Path) -> None:
    """`fcntl.flock` at creation time, with the one failure a person can act on named.

    Without `flock` there is no way to tell a dead owner from a live one, and the
    whole point of writing the conversation here is that it can be cleaned up;
    failing is the safe answer. `agora.agents.base` imports this module, so the
    error class is imported here rather than at module level (that would be a cycle).
    """
    try:
        fcntl.flock(handle, operation)
    except OSError as e:
        from agora.agents.base import AgentError
        raise AgentError(f"{base} 所在的檔案系統不支援 flock（{e}）；"
                         "把 AGORA_STATE_DIR 設到本機磁碟") from e


def _owner_is_gone(entry: Path) -> bool:
    """Whether `entry`'s creating process is dead, by taking its owner lock.

    Only a sweep asks, and only while it holds the gate, so nobody is setting one
    up right now. That makes a missing owner lock conclusive: either a setup that
    died between `mkdtemp` and the lock (a `.new-*` name), or a published
    directory whose deletion had already removed the lock - publishing happens
    only after the lock is taken, and the only `rmtree` that removes the lock is
    a cleanup that may itself be interrupted (#116 review A). Anything we cannot
    open or lock is treated as live: a leak is a nuisance, deleting a running
    export is the bug this whole mechanism exists to avoid.
    """
    try:
        lock = open(entry / OWNER_LOCK, "r+b")     # never creates one
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        lock.close()
    return True


def sweep(state: Path | None = None) -> list[Path]:
    """Remove the temp directories whose owner is gone; return what was removed.

    Safe beside other agora runs: a live directory's owner lock cannot be
    taken, and the gate keeps this out of a directory that is still being set
    up. Best effort - an unreadable state directory is "nothing to do".
    """
    base = root(state, create=False)
    if not base.is_dir():
        return []
    try:
        gate = open(base / GATE_LOCK, "a+b")
    except OSError:
        return []
    try:
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        gate.close()               # another creator or sweep is on it; it will do this
        return []
    except BaseException:
        gate.close()
        raise
    removed: list[Path] = []
    own = f"-{os.getpid()}-"       # this process's own directories are never swept
    try:
        for entry in sorted(base.iterdir()):
            try:
                if (own in entry.name or not _OUR_NAME.fullmatch(entry.name)
                        or not entry.is_dir() or not _owner_is_gone(entry)):
                    continue
                _rmtree(entry, ignore_errors=True)
            except OSError:
                continue               # one bad entry must not stop the others
            if not entry.exists():
                removed.append(entry)
    except OSError:
        pass                           # iterdir raced with a creator/remover: keep what we got
    finally:
        gate.close()
    return removed


def _remove(name: str, lock) -> None:
    """The finalizer: the directory, then the lock that kept others off it."""
    try:
        _rmtree(name, ignore_errors=True)
    finally:
        lock.close()


def _sweep_once() -> None:
    global _swept
    if _swept:
        return
    _swept = True
    sweep()


def _prefix(name: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    return clean or "agora"


class PrivateTmpDir:
    """`tempfile.TemporaryDirectory`'s interface, over `<state>/tmp` (#116).

    `prefix` is which kind of export this is - one of `PREFIXES`, because `sweep`
    recognises only those names (a new kind has to be added there, or its
    directories would never be cleaned). `name` is a `str` and `__enter__`
    returns it, exactly as the stdlib class does, so the call sites only swap the
    constructor. `cleanup()` (or leaving the `with`) removes the directory the
    same way it always did; a process that dies without running it leaves the
    directory for the next `sweep`.
    """

    def __init__(self, prefix: str) -> None:
        if prefix not in PREFIXES:
            raise ValueError(f"未登記的暫存前綴：{prefix!r}（要加進 private_tmp.PREFIXES，"
                             "sweep 才認得）")
        _sweep_once()
        base = root()
        gate = open(base / GATE_LOCK, "a+b")
        hidden: Path | None = None
        lock = None
        try:
            _flock(gate, fcntl.LOCK_SH, base)
            hidden = Path(tempfile.mkdtemp(dir=base, prefix=SETUP_PREFIX))
            lock = open(hidden / OWNER_LOCK, "a+b")
            _flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB, base)
            final = base / f"{_prefix(prefix)}-{os.getpid()}-{hidden.name[len(SETUP_PREFIX):]}"
            hidden.rename(final)       # published only with the owner lock held
        except BaseException:
            if lock is not None:
                lock.close()
            if hidden is not None:
                _rmtree(hidden, ignore_errors=True)
            raise
        finally:
            gate.close()
        self.name = str(final)
        self._lock = lock
        self._finalizer = weakref.finalize(self, _remove, self.name, lock)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc_info) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._finalizer()
