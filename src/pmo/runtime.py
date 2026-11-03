"""What the `pmo` commands share at run time (design §3.1).

`Deps` holds the four ports (Angareion, aicomm, sidecar, GitHub) — the only
seams the command touches the outside through (§3.1; T-2 wires the Angareion
port, the other three come with their tickets) — plus the clock, so tests can
hand in `pmo.fakes` fakes and a fixed time the way the current PMO tool's
tests hand in fake `Deps`.

`Ctx` carries those plus the validated teams settings, the state directory
and the three streams. Every printed line goes through `sanitize` (D-12) at
`emit`／`log`; stdout is the事件行 PMO reads, stderr is diagnosis.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from ainame import is_valid

from .config import TeamsConfig
from .errors import IdentityMismatch
from .ports import AicommPort, AngareionPort, GitHubPort, SidecarPort
from .sanitize import sanitize
from .timeutil import real_now

#: The environment variable naming the seat a command runs as.
IDENTITY_ENV = "ANGAREION_IDENTITY"


def effective_identity() -> str:
    """The seat this process runs as, exactly as the environment says it."""
    return os.environ.get(IDENTITY_ENV, "")


def one_line(text: object, limit: int = 300) -> str:
    """Collapse to one line and cut to length: a diagnosable log line."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def assert_pmo_identity(config: TeamsConfig) -> None:
    """The sending commands' identity gate: run as the settings' PMO seat.

    Refusal happens before anything is sent (design §3.2「送信前的身分守門」,
    C-3); the current tool's message shape is kept so the harness and manuals
    read the same text.
    """
    effective = effective_identity()
    if not is_valid(effective) or effective != config.pmo_identity:
        raise IdentityMismatch(
            f"身分不符拒送：目前 identity 為「{effective}」，非「{config.pmo_identity}」"
        )


@dataclass
class Deps:
    """The four ports, plus the clock.

    `aicomm`／`sidecar`／`github` stay `None` until their tickets wire them
    (T-4a..T-5): a command that needs one and finds `None` is not a bad
    backend, it is a command not implemented yet.
    """

    angareion: AngareionPort
    now: Callable[[], datetime] = real_now  # noqa: A003
    aicomm: AicommPort | None = None
    sidecar: SidecarPort | None = None
    github: GitHubPort | None = None


@dataclass
class Ctx:
    """One command's shared environment (deploys, streams and the state)."""

    deps: Deps
    config: TeamsConfig
    state_dir: Path
    out: TextIO  # the事件行: bracket-tagged one-liners the PMO reads
    err: TextIO  # diagnosis
    inp: TextIO  # `--body -` reads here
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _print(self, text: str, stream: TextIO) -> None:
        with self._lock:
            print(sanitize(text), file=stream, flush=True)

    def emit(self, line: object) -> None:
        self._print(str(line), self.out)

    def log(self, line: object) -> None:
        self._print(str(line), self.err)
