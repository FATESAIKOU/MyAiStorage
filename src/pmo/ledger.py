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

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import _io, _jsonutil, _lock, sanitize
from ._jsonutil import fail, pick_extra
from .timeutil import iso_utc

WHERE = "inbox ledger"

#: The file name MyPMO writes; both sides share it during the transition (Q-9).
STATE_FILE_NAME = "secretary-inbox.json"

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


# --------------------------------------------------------------------------- #
# 狀態檔的存取（路徑、讀〔含相容補齊〕、驗、鎖、原子寫）：只有這一處
# （design §3.1「狀態儲存」；T-3／T-4a／T-5 都走這裡）
# --------------------------------------------------------------------------- #


def state_file(state_dir: Path) -> Path:
    """The ledger file of one state directory."""
    return Path(state_dir) / STATE_FILE_NAME


def lock_file(state_dir: Path) -> Path:
    """The flock file next to it (the same name MyPMO uses)."""
    return state_file(state_dir).with_name(f"{STATE_FILE_NAME}.lock")


def state_lock(state_dir: Path) -> AbstractContextManager[None]:
    """跨進程檔案鎖（fcntl.flock）＋進程內可重入鎖，綁在一個 state 目錄上。

    保護 `secretary-inbox.json` 的讀寫窗口，防止兩個行程（或 watch 與手動
    `inbox` 指令）並行時覆寫。同一行程對不同 state 目錄拿鎖各自獨立。
    機制在 `pmo._lock.path_lock`（#358）：這裡只決定鎖檔路徑，是薄殼。
    """
    return _lock.path_lock(lock_file(state_dir))


def empty_state() -> dict:
    """The empty ledger: what a missing file means (nothing recorded yet)."""
    return {"version": 1, "updated_at": None, "items": {}, "notified": {"call": {}, "poke": {}}}


def normalize_state(data: dict) -> dict:
    """Fill in the dedupe buckets a MyPMO ledger may not have written yet.

    MyPMO only creates the bucket it touches (`_notified_bucket`), so a ledger
    that has seen a叫人 but no急件 carries `notified.call` alone — and one that
    has seen neither carries no `notified` at all. Both sides share the file
    during the transition (Q-9), so the missing keys are added **in memory**
    here and the file the other side owns is not rewritten. A `notified` of the
    wrong shape is left as it is — `parse` then refuses it, because silently
    resetting it would ack twice.
    """
    notified = data.setdefault("notified", {})
    if isinstance(notified, dict):
        notified.setdefault("call", {})
        notified.setdefault("poke", {})
    return data


def load_state(state_dir: Path) -> dict:
    """Read the ledger of one state directory; missing is empty, damaged raises.

    The content is validated against this module's schema before it goes out,
    so a state-file problem is caught before the cursor can move (design §3.2:
    exit 2,這一輪什麼都沒做).
    """
    path = state_file(state_dir)
    if not path.exists():
        return empty_state()
    data = _io.read_json(path)
    normalize_state(data)
    parse(data)
    return data


def save_state(state_dir: Path, data: dict, now: datetime | None = None) -> None:
    """Validate, redact (`sanitize` D-12) and atomically write the ledger.

    The write happens under the state directory's flock, after a schema check —
    a state-file failure is a `SchemaError` (design §3.2: 寫不進去 → exit 2).
    """
    parse(json.loads(json.dumps(data)))  # 寫前先把關（深拷貝，防 caller 的別名）
    dt = now if now is not None else datetime.now(timezone.utc)
    data["updated_at"] = iso_utc(dt)
    safe = sanitize.sanitize_fields(data)
    with state_lock(state_dir):
        _io.write_json(state_file(state_dir), safe)
