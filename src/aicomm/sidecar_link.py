"""The sidecar-link directory: is the link fresh, and which loopback ports can be used.

`~/.mylinuxpool/sidecar-link/` holds two files. `heartbeat` contains the pid of the
process that last wrote it; its mtime is the last round, and the link is fresh when that
mtime is within `LINK_FRESH_SECONDS` of now **and** the pid is still alive. `ports` holds
one decimal port per line (ascending, no repeats); each one is some machine's sidecar on
`127.0.0.1:<port>`.

This module only reads those two files. It is the one place that knows their format, so a
change on that side only touches this module. It never talks to a sidecar, never decides
what a reason code is, and never works out the local sidecar's own port: the caller passes
it in, and the ports list that comes back has that port removed.
"""

from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path
from typing import NamedTuple

# The directory: `AICOMM_SIDECAR_LINK` (tests and manual runs only) or the default under
# home. The two file names are part of the interface.
LINK_ENV = "AICOMM_SIDECAR_LINK"
LINK_DIR = (".mylinuxpool", "sidecar-link")
HEARTBEAT = "heartbeat"
PORTS = "ports"

# How old a heartbeat may be, in either direction: a clock that ran forward must not keep
# an old file fresh forever. The clock here is the wall clock (mtime is not monotonic).
LINK_FRESH_SECONDS = 90.0

# A pid we may hand to `os.kill`: no leading zero, no sign, and no more than the largest
# value it accepts. 0 and negatives would signal a whole process group, never a process.
PID_RE = re.compile(r"[1-9][0-9]*")
MAX_PID = 2147483647

# One port per line: at most five digits. The range itself is checked after the shape,
# so a line with six digits is a broken line, not a port outside the range.
PORT_RE = re.compile(r"[0-9]{1,5}")
PORT_MIN = 1
PORT_MAX = 65535

# The three ways the directory can be no use. These are not reason codes: the caller maps
# them to its own vocabulary, so the codes stay in one place (the command line module).
UNAVAILABLE = "unavailable"
BAD_FORMAT = "bad-format"
OUT_OF_RANGE = "out-of-range"


class Ports(NamedTuple):
    """The usable loopback ports, ascending, without the caller's own sidecar port."""

    ports: tuple[int, ...]


class Unusable(NamedTuple):
    """Why there are no usable ports: one of `UNAVAILABLE`, `BAD_FORMAT`, `OUT_OF_RANGE`."""

    kind: str


def _pid_alive(pid: int) -> bool:
    """Whether `pid` is a live process; no permission to signal it still counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _directory(env, home) -> Path | None:
    """The directory to read, or None when there is none to read (no link here).

    An override that is empty or not absolute is no directory at all - not a usage error:
    this is read after the message was delivered, so it must never change the exit code.
    """
    override = env.get(LINK_ENV)
    if override is not None:
        if not override or not os.path.isabs(override):
            return None
        return Path(override)
    try:
        base = home()
    except (OSError, RuntimeError):  # no home for this uid, or home is unreadable
        return None
    return base.joinpath(*LINK_DIR)


def _fresh(directory: Path, now, pid_alive) -> bool:
    """Whether the heartbeat in `directory` is fresh: recent enough and a live pid."""
    path = directory / HEARTBEAT
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):  # a directory, a socket, anything but a file
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    age = now() - info.st_mtime
    if not -LINK_FRESH_SECONDS <= age <= LINK_FRESH_SECONDS:
        return False
    text = text.strip()
    if PID_RE.fullmatch(text) is None:
        return False
    pid = int(text)
    if pid > MAX_PID:  # `os.kill` would raise OverflowError, which is not an OSError
        return False
    return pid_alive(pid)


def _read_ports(directory: Path) -> tuple[int, ...] | Unusable:
    """The ports `ports` lists, deduplicated and ascending, or why the file is no use."""
    try:
        data = (directory / PORTS).read_bytes()
    except OSError:
        return Unusable(UNAVAILABLE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:  # the list cannot be read at all: no lines to decide from
        return Unusable(BAD_FORMAT)
    if text.endswith("\n"):
        text = text[:-1]  # exactly one trailing newline is the normal end of a list
    if not text:  # an empty file is no list, not one blank line
        return Unusable(UNAVAILABLE)
    ports: list[int] = []
    seen: set[int] = set()
    for line in text.split("\n"):
        if PORT_RE.fullmatch(line) is None:
            return Unusable(BAD_FORMAT)  # the first broken line decides the whole list
        port = int(line)
        if not PORT_MIN <= port <= PORT_MAX:
            return Unusable(OUT_OF_RANGE)
        if port not in seen:  # a repeated port is one port, the first time it appears
            seen.add(port)
            ports.append(port)
    return tuple(sorted(ports))


def read(*, local_port: int, env=os.environ, home=Path.home, now=time.time,
         pid_alive=_pid_alive) -> Ports | Unusable:
    """The loopback ports that may hold another machine's sidecar, minus `local_port`.

    Reads the two files on every call (no caching across recipients). `now`, `pid_alive`
    and `home` are injected so the boundaries can be tested without touching a clock, a
    process, or the real home directory.
    """
    directory = _directory(env, home)
    if directory is None:
        return Unusable(UNAVAILABLE)
    if not _fresh(directory, now, pid_alive):
        return Unusable(UNAVAILABLE)
    listed = _read_ports(directory)
    if isinstance(listed, Unusable):
        return listed
    remaining = tuple(port for port in listed if port != local_port)
    if not remaining:  # no port to ask: that is "cannot look", never "everybody said no"
        return Unusable(UNAVAILABLE)
    return Ports(remaining)
