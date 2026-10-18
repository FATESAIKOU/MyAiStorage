"""One durable write, shared by the two subcommands that write a file.

`install` writes an agent's user-level instruction file and `render` writes the
instruction file of a single agent; both must leave either the old file or the
new one, never a truncated file, so the same-directory temporary file plus
`os.replace` is the only way either writes (spec「安裝」／「產生 agent 指示檔」).
An interrupted or failed write leaves no temporary file behind.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import uuid


class WriteError(RuntimeError):
    """A target cannot be written (exit 2)."""


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    """Best effort: make the rename durable too (ignored where unsupported)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(target: Path, data: bytes, *, make_parents: bool = True) -> None:
    """Write `data` next to `target`, then move it in with `os.replace`.

    A crash or a full disk leaves the old file or the new one, never a truncated
    file: a half-written file is the one result no reader can do anything with.
    The temporary file never survives a failure.

    `make_parents` is False for `render`: the directory of `--out` is the agent's
    own state directory, named by whoever starts the agent, and a wrong path must
    fail instead of growing a directory tree where none was meant to be.
    """
    tmp = target.parent / f".{target.name}.atelier-{uuid.uuid4().hex}.tmp"
    try:
        if make_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():   # keep the target's mode; a fresh file follows the umask
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except OSError as e:
        _unlink_quietly(tmp)
        raise WriteError(f"寫不進去 {target}：{e}") from e
    except BaseException:
        _unlink_quietly(tmp)
        raise