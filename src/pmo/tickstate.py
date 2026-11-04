"""The tick state (`<狀態目錄>/tick.json`): schema and validation (design §3.4).

    {
      "version": 1,
      "last_tick_at": "…",
      "handoffs": {                    keyed by the群請求 id
        "<id>": {"handed_at": "…", "last_reminded_at": "…" | null,
                 "next_due_at": "…"}
      },
      "worker_poll": {                 keyed by the team key
        "<key>": {"last_reported_at": "…" | null, "next_due_at": "…"}
      },
      "wake": {
        "every": "10m" | null,         null = the PMO chose not to schedule
        "set_at": "…",
        "missing_reported_at": "…",
        "error_reported": {"<job id>": "<code>"},
        "orphan_reported_at": "…"
      }
    }

The file does not exist → treated as empty and created (`wake.every` is null,
so nothing is reported). Only tick, `handoff-close` and `pmo wake set|off`
write it; tick only touches the three "already reported" fields inside
`wake`. Written with an atomic replace in the state directory.

`wake.every` is the interval the PMO wants: an integer and `m` or `h`
(`EVERY_RE`, design §3.6). A string outside that grammar makes the file bad at
read time, the way the design's "檔壞掉" branch asks (exit 2) — otherwise tick
would only trip over it later, when it uses the interval. The 2m..24h bounds
are `pmo wake set`'s (T-5), not the file's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from . import _io, _jsonutil, _lock
from ._jsonutil import describe, fail, pick_extra
from .timeutil import iso_utc

#: One change to the tick state: `TickState → TickState`. Tick accumulates these
#: on the state it read **outside** the lock and `commit` applies them to a
#: re-read one, inside `tick.json.lock` (design §3.4; #358 T5-2).
Mutation = Callable[["TickState"], "TickState"]

WHERE = "tick state"

STATE_FILE_NAME = "tick.json"

#: The `wake.every` grammar: an integer and `m` or `h` (design §3.6).
#: The 2m..24h bounds belong to `pmo wake set` (T-5), which must use this same
#: pattern; T-1 only keeps a broken interval from being read as a good file.
EVERY_RE = re.compile(r"[0-9]+[mh]")

_KNOWN = ("version", "last_tick_at", "handoffs", "worker_poll", "wake")
_KNOWN_HANDOFF = ("handed_at", "last_reminded_at", "next_due_at")
_KNOWN_POLL = ("last_reported_at", "next_due_at")
_KNOWN_WAKE = ("every", "set_at", "missing_reported_at", "error_reported", "orphan_reported_at")


@dataclass(frozen=True)
class Handoff:
    """One交接出去的群請求 entry of `handoffs`."""

    handed_at: str
    next_due_at: str
    last_reminded_at: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {"handed_at": self.handed_at, "next_due_at": self.next_due_at}
        if self.last_reminded_at is not None:
            known["last_reminded_at"] = self.last_reminded_at
        known.update(self.extra)
        return known


@dataclass(frozen=True)
class WorkerPoll:
    """One worker team's entry of `worker_poll`."""

    next_due_at: str
    last_reported_at: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {"next_due_at": self.next_due_at}
        if self.last_reported_at is not None:
            known["last_reported_at"] = self.last_reported_at
        known.update(self.extra)
        return known


@dataclass(frozen=True)
class Wake:
    """The `wake` object: what the PMO wants and what was already reported."""

    every: str | None = None
    set_at: str | None = None
    missing_reported_at: str | None = None
    error_reported: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    orphan_reported_at: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {
            "every": self.every,
            "set_at": self.set_at,
            "missing_reported_at": self.missing_reported_at,
            "error_reported": dict(self.error_reported),
            "orphan_reported_at": self.orphan_reported_at,
        }
        known.update(self.extra)
        return known


@dataclass(frozen=True)
class TickState:
    """The whole tick state file, taken apart."""

    last_tick_at: str | None = None
    handoffs: dict[str, Handoff] = field(default_factory=dict, repr=False, compare=False)
    worker_poll: dict[str, WorkerPoll] = field(default_factory=dict, repr=False, compare=False)
    wake: Wake = field(default_factory=Wake)
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {
            "version": 1,
            "last_tick_at": self.last_tick_at,
            "handoffs": {key: value.to_dict() for key, value in self.handoffs.items()},
            "worker_poll": {key: value.to_dict() for key, value in self.worker_poll.items()},
            "wake": self.wake.to_dict(),
        }
        known.update(self.extra)
        return known


def empty() -> TickState:
    """What a missing `tick.json` stands for (`wake.every` is null)."""
    return TickState()


def _parse_handoff(raw: object, where: str) -> Handoff:
    _jsonutil.check_object(raw, where)
    assert isinstance(raw, dict)
    return Handoff(
        handed_at=_jsonutil.check_str(raw, "handed_at", where),
        next_due_at=_jsonutil.check_str(raw, "next_due_at", where),
        last_reminded_at=_jsonutil.optional_str(raw, "last_reminded_at", where),
        extra=pick_extra(raw, _KNOWN_HANDOFF),
    )


def _parse_poll(raw: object, where: str) -> WorkerPoll:
    _jsonutil.check_object(raw, where)
    assert isinstance(raw, dict)
    return WorkerPoll(
        next_due_at=_jsonutil.check_str(raw, "next_due_at", where),
        last_reported_at=_jsonutil.optional_str(raw, "last_reported_at", where),
        extra=pick_extra(raw, _KNOWN_POLL),
    )


def _parse_wake(raw: object, where: str) -> Wake:
    _jsonutil.check_object(raw, where)
    assert isinstance(raw, dict)
    every = raw.get("every", None)
    if every is not None:
        if not isinstance(every, str):
            fail(where, f"the field 'every' must be a string or null, got {describe(every)}")
        if not EVERY_RE.fullmatch(every):
            fail(
                where,
                f"the field 'every' must be an interval like '10m' or '2h' "
                f"(an integer and 'm' or 'h'), got {describe(every)}",
            )
    error_reported = _jsonutil.optional_dict(raw, "error_reported", where)
    errors: dict[str, str] = {}
    for job_id, code in error_reported.items():
        if not isinstance(code, str):
            fail(
                where,
                f"the field 'error_reported' holds the last reported code per job id; "
                f"the value for {job_id!r} is not a string ({describe(code)})",
            )
        errors[job_id] = code
    return Wake(
        every=every,
        set_at=_jsonutil.optional_str(raw, "set_at", where),
        missing_reported_at=_jsonutil.optional_str(raw, "missing_reported_at", where),
        error_reported=errors,
        orphan_reported_at=_jsonutil.optional_str(raw, "orphan_reported_at", where),
        extra=pick_extra(raw, _KNOWN_WAKE),
    )


def parse(data: object) -> TickState:
    """Validate one tick state file's content and take it apart."""
    _jsonutil.check_object(data, WHERE)
    assert isinstance(data, dict)
    _jsonutil.check_version(data, WHERE)

    handoffs_raw = _jsonutil.optional_dict(data, "handoffs", WHERE)
    handoffs = {
        key: _parse_handoff(value, f"{WHERE}: handoffs[{key!r}]")
        for key, value in handoffs_raw.items()
    }
    poll_raw = _jsonutil.optional_dict(data, "worker_poll", WHERE)
    worker_poll = {
        key: _parse_poll(value, f"{WHERE}: worker_poll[{key!r}]")
        for key, value in poll_raw.items()
    }
    wake = _parse_wake(data.get("wake", {}), f"{WHERE}: wake")
    return TickState(
        last_tick_at=_jsonutil.optional_str(data, "last_tick_at", WHERE),
        handoffs=handoffs,
        worker_poll=worker_poll,
        wake=wake,
        extra=pick_extra(data, _KNOWN),
    )


def load_or_empty(path: Path) -> TickState:
    """Read the tick state; a missing file counts as empty, a bad file raises."""
    # read_json turns an unreadable file (missing included) into a SchemaError
    if not path.exists():
        return empty()
    return parse(_io.read_json(path))


def save(path: Path, state: TickState) -> None:
    """Write the canonical form of the tick state (same-directory replace)."""
    _io.write_json(path, state.to_dict())


def tick_path(state_dir: Path) -> Path:
    """The tick state file in this state directory (design §3.4)."""
    return Path(state_dir) / STATE_FILE_NAME


def lock_path(path: Path) -> Path:
    """The flock file next to one tick state file (design §3.4)."""
    path = Path(path)
    return path.with_name(f"{path.name}.lock")


def commit(
    path: Path,
    *,
    now: datetime,
    writes: Sequence[Mutation],
    timeout: float = 10.0,
) -> TickState:
    """Apply this round's `writes` to a re-read `tick.json` (design §3.4).

    The whole read-modify-write runs inside `tick.json.lock` and **no HTTP call
    happens here**: tick finishes `GET /agents`／`poke` first, then commits, so
    a `wake set|off` that ran while the HTTP calls were out is not overwritten
    by the snapshot tick read at the start (the file is re-read inside the
    lock). The lock is the shared `_lock.path_lock` (#358); waiting longer than
    `timeout` raises `_lock.LockTimeout`, and the caller prints `[warn]` and
    exits 2 (§3.4: 下一輪再寫).

    `writes` are applied in order on the re-read state, then `last_tick_at` is
    set to `now`. A missing file is created; a bad one raises `SchemaError`
    before anything is written.
    """
    path = Path(path)
    lock = lock_path(path)
    with _lock.path_lock(lock, timeout=timeout):
        state = load_or_empty(path)
        for write in writes:
            state = write(state)
        state = replace(state, last_tick_at=iso_utc(now))
        save(path, state)
        return state
