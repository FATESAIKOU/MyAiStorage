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
from .ports import AicommPort, AngareionPort, GitHubPort, SidecarPort, sidecar_address
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

    Each port may stay `None` when the deployment does not wire it; a command
    that needs one and finds `None` is not a bad backend, it is a missing
    wiring, and the refusal names it. T-2 wires `angareion` (`pmo inbox …`),
    T-4a `github` (`pmo send`), and T-4c `aicomm` (`pmo reply`) plus the sidecar
    port (`pmo poke`／`status`／`close`).

    The sidecar port is looked up **by address** (`sidecar_for(address)`), not
    fixed to one machine: every team record names its own `sidecar`, and the
    PMO's own wake jobs live on `pmo_sidecar`. The two can differ (a team on
    another machine reachable over HTTP), so the port cannot be one instance
    (#356; design §3.1). Addresses are put in one form first, so a trailing
    `/` does not make one machine look like two (#371). `pmo.cli` wires the
    factory to `adapters.Sidecar`; tests hand in a fake that answers per
    address.
    """

    angareion: AngareionPort
    now: Callable[[], datetime] = real_now  # noqa: A003
    aicomm: AicommPort | None = None
    sidecar_for: Callable[[str], SidecarPort] | None = None
    github: GitHubPort | None = None


def sidecar_port(deps: Deps, address: str) -> SidecarPort | None:
    """The sidecar port for one address, or `None` when none is wired.

    The address is put in its one form first (`sidecar_address`, #371): the
    cache and comparisons elsewhere use that same string, so `http://h:8471/`
    and `http://h:8471` reach one port and one machine. The caller decides what
    a missing port means (a refusal for the commands that need it, an honest
    「沒有接上」for `pmo check`).
    """
    if deps.sidecar_for is None:
        return None
    return deps.sidecar_for(sidecar_address(address))


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
