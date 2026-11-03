"""The team state: schema and validation (design §3.4).

One JSON file per team in the state directory (the file name follows MyPMO's
README; T-4a picks it up when it writes the file). The fields are MyPMO's
current `pmo/state/README.md` layout, which the design names as the authority
(Q-9: both sides share the same directory and schema during the transition) —
the fields named here are typed, everything else is an unknown field and is
preserved as it is.

    {
      "agent": "<the team PM's AI name>",
      "state": "<sidecar state>",
      "exit": {...} | null,
      "ticket": "repo#n …",
      "message_id": "<the dispatch (指示信) message id>",
      "dispatched_at": "<the --since time of the first input>",
      "received_at": "<the back-end time of the dispatch>",
      "ack_at": "<time>" | null,          the team PM's 「收到」
      "ack_poke_at": "<time>" | null,     the watchdog's first poke
      "ack_escalated_at": "<time>" | null, the watchdog's escalation to the person
    }

`version` is optional here and nowhere else: MyPMO's team state files carry no
`version`, and this file has to read them unchanged (architect review 中-1).
A missing `version` reads as 1 and is not written back, so the file does not
grow a field the other writer never wrote. When it is present it must still
be the integer 1.

Nothing here is command behavior (T-1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import _io, _jsonutil, sanitize
from ._jsonutil import pick_extra

WHERE = "team state"

_KNOWN = (
    "version",
    "agent",
    "state",
    "exit",
    "ticket",
    "message_id",
    "dispatched_at",
    "received_at",
    "ack_at",
    "ack_poke_at",
    "ack_escalated_at",
)


@dataclass(frozen=True)
class TeamState:
    """One team's state file, taken apart."""

    agent: str | None = None
    state: str | None = None
    exit: dict[str, object] | None = None
    ticket: str | None = None
    message_id: str | None = None
    dispatched_at: str | None = None
    received_at: str | None = None
    ack_at: str | None = None
    ack_poke_at: str | None = None
    ack_escalated_at: str | None = None
    version: int | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {}
        if self.version is not None:
            known["version"] = self.version
        for key, value in (
            ("agent", self.agent),
            ("state", self.state),
            ("exit", self.exit),
            ("ticket", self.ticket),
            ("message_id", self.message_id),
            ("dispatched_at", self.dispatched_at),
            ("received_at", self.received_at),
            ("ack_at", self.ack_at),
            ("ack_poke_at", self.ack_poke_at),
            ("ack_escalated_at", self.ack_escalated_at),
        ):
            if value is not None:
                known[key] = value
        known.update(self.extra)
        return known


def parse(data: object) -> TeamState:
    """Validate one team state file's content and take it apart."""
    _jsonutil.check_object(data, WHERE)
    assert isinstance(data, dict)

    raw_exit = _jsonutil.optional_dict_or_none(data, "exit", WHERE)
    return TeamState(
        version=_jsonutil.check_version_if_present(data, WHERE),
        agent=_jsonutil.optional_str(data, "agent", WHERE),
        state=_jsonutil.optional_str(data, "state", WHERE),
        exit=None if raw_exit is None else dict(raw_exit),
        ticket=_jsonutil.optional_str(data, "ticket", WHERE),
        message_id=_jsonutil.optional_str(data, "message_id", WHERE),
        dispatched_at=_jsonutil.optional_str(data, "dispatched_at", WHERE),
        received_at=_jsonutil.optional_str(data, "received_at", WHERE),
        ack_at=_jsonutil.optional_str_or_none(data, "ack_at", WHERE),
        ack_poke_at=_jsonutil.optional_str_or_none(data, "ack_poke_at", WHERE),
        ack_escalated_at=_jsonutil.optional_str_or_none(data, "ack_escalated_at", WHERE),
        extra=pick_extra(data, _KNOWN),
    )


def load(path: Path) -> TeamState:
    """Read and validate one team state file."""
    return parse(_io.read_json(path))


def save(path: Path, state: TeamState) -> None:
    """Write the canonical form of the state."""
    _io.write_json(path, state.to_dict())


# --------------------------------------------------------------------------- #
# 狀態檔的存取（路徑、讀、驗、原子寫）：只有這一處（arch review 中-1）
# --------------------------------------------------------------------------- #


def state_file(state_dir: Path, key: str) -> Path:
    """One team's state file: `<state_dir>/<key>.json` (MyPMO's layout, Q-9)."""
    return Path(state_dir) / f"{key}.json"


def load_state(state_dir: Path, key: str) -> dict:
    """Read and validate one team's state; a missing file is the empty one.

    The schema is checked **always** now (arch review 中-1): a team state that
    exists but does not match is damage, not an empty file — the watchdog reads
    `ack_at` and `ticket` off it, and treating damage as empty would poke a
    team that already answered.
    """
    path = state_file(state_dir, key)
    if not path.exists():
        return {}
    data = _io.read_json(path)
    parse(data)
    return data


def save_state(state_dir: Path, key: str, data: dict) -> None:
    """Validate, redact (`sanitize` D-12) and atomically write one team state.

    Unknown fields are written back as they are (Q-9: MyPMO's worker-team
    fields pass through), and the keys are sorted the way MyPMO writes them, so
    the shared file does not flip-flop between two shapes.
    """
    parse(json.loads(json.dumps(data)))
    safe = sanitize.sanitize_fields(data)
    _io.write_json(state_file(state_dir, key), safe, sort_keys=True)
