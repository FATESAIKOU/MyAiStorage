"""The收件帳本 (inbox ledger): schema and validation (design §3.4, §1.1, §1.2).

    {
      "version": 1,
      "items": {                keyed by the work ID
        "<工作 ID>": {
          "id": "<same as the key>",
          "title": "…", "level": "…", "sender": "…", "received_at": "…",
          "ack_at": … | null, "schedule": … | null, "issue": … | null,
          "status": … | null, "reported_at": … | null, "pr": … | null,
          "team": … | null      cannot be rebuilt (§1.2)
        }
      },
      "notified": {                de-dup of the called/urgent acks:
        "call": {"<message id>": "<the time the ack was sent>"},
        "poke": {"<message id>": "<the time it was marked as urgent>"}
      },
      "rebuilt_at": "…"      added by rebuild (new in this design, §3.4)
    }

Per item, `id` (equal to its key) and `received_at` are required; the rest are
typed when present and preserved when unknown. `notified.call`／`notified.poke`
are objects keyed by message id, each value the time of that ack: the same shape
MyPMO's ledger has (architect review 中-3), because the two sides share the file
during the transition (Q-9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import _io, _jsonutil
from ._jsonutil import fail, pick_extra

WHERE = "inbox ledger"

_KNOWN = ("version", "items", "notified", "rebuilt_at")
_KNOWN_ITEM = ("id", "title", "level", "sender", "received_at", "ack_at", "schedule", "issue", "status", "reported_at", "pr", "team")


@dataclass(frozen=True)
class LedgerItem:
    """One dispatch item, as it sits in the ledger."""

    item_id: str
    received_at: str
    title: str | None = None
    level: str | None = None
    sender: str | None = None
    ack_at: str | None = None
    schedule: str | None = None
    issue: str | None = None
    status: str | None = None
    reported_at: str | None = None
    pr: str | None = None
    team: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {"id": self.item_id, "received_at": self.received_at}
        for key, value in (
            ("title", self.title),
            ("level", self.level),
            ("sender", self.sender),
            ("ack_at", self.ack_at),
            ("schedule", self.schedule),
            ("issue", self.issue),
            ("status", self.status),
            ("reported_at", self.reported_at),
            ("pr", self.pr),
            ("team", self.team),
        ):
            if value is not None:
                known[key] = value
        known.update(self.extra)
        return known


@dataclass(frozen=True)
class Ledger:
    """The whole inbox ledger file, taken apart."""

    items: dict[str, LedgerItem]
    notified_call: dict[str, str]
    notified_poke: dict[str, str]
    rebuilt_at: str | None = None
    notified_extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        notified: dict[str, object] = {
            "call": dict(self.notified_call),
            "poke": dict(self.notified_poke),
        }
        notified.update(self.notified_extra)
        known: dict[str, object] = {
            "version": 1,
            "items": {key: item.to_dict() for key, item in self.items.items()},
            "notified": notified,
        }
        if self.rebuilt_at is not None:
            known["rebuilt_at"] = self.rebuilt_at
        known.update(self.extra)
        return known


def _parse_item(key: str, raw: object, where: str) -> LedgerItem:
    _jsonutil.check_object(raw, where)
    assert isinstance(raw, dict)
    item_id = _jsonutil.check_str(raw, "id", where)
    if item_id != key:
        fail(where, f"the item's 'id' {item_id!r} does not match its key {key!r}")
    received_at = _jsonutil.check_str(raw, "received_at", where)
    parsed = LedgerItem(
        item_id=item_id,
        received_at=received_at,
        title=_jsonutil.optional_str(raw, "title", where),
        level=_jsonutil.optional_str(raw, "level", where),
        sender=_jsonutil.optional_str(raw, "sender", where),
        ack_at=_jsonutil.optional_str(raw, "ack_at", where),
        schedule=_jsonutil.optional_str(raw, "schedule", where),
        issue=_jsonutil.optional_str(raw, "issue", where),
        status=_jsonutil.optional_str(raw, "status", where),
        reported_at=_jsonutil.optional_str(raw, "reported_at", where),
        pr=_jsonutil.optional_str(raw, "pr", where),
        team=_jsonutil.optional_str(raw, "team", where),
        extra=pick_extra(raw, _KNOWN_ITEM),
    )
    return parsed


def parse(data: object) -> Ledger:
    """Validate one inbox ledger file's content and take it apart."""
    _jsonutil.check_object(data, WHERE)
    assert isinstance(data, dict)
    _jsonutil.check_version(data, WHERE)

    raw_items = _jsonutil.check_dict(data, "items", WHERE)
    items = {
        key: _parse_item(key, value, f"{WHERE}: items[{key!r}]") for key, value in raw_items.items()
    }

    notified = _jsonutil.check_dict(data, "notified", WHERE)
    notified_call = _jsonutil.dict_of_str(notified, "call", f"{WHERE}: notified")
    notified_poke = _jsonutil.dict_of_str(notified, "poke", f"{WHERE}: notified")

    rebuilt_at = _jsonutil.optional_str(data, "rebuilt_at", WHERE)
    return Ledger(
        items=items,
        notified_call=notified_call,
        notified_poke=notified_poke,
        rebuilt_at=rebuilt_at,
        notified_extra=pick_extra(notified, ("call", "poke")),
        extra=pick_extra(data, _KNOWN),
    )


def load(path: Path) -> Ledger:
    """Read and validate the inbox ledger file."""
    return parse(_io.read_json(path))


def save(path: Path, ledger: Ledger) -> None:
    """Write the canonical form of the ledger."""
    _io.write_json(path, ledger.to_dict())
