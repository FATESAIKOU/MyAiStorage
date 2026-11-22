"""Pure decision payload, folding, snapshot and submission planning rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict
import re
import yaml

from angareion.messages import (
    FlowDepthLoader,
    Message,
    MessageValidationError,
    MAX_MESSAGE_LENGTH,
    is_ulid,
    validate_address,
    validate_channel_name,
)

PAYLOAD_VERSION = 1
SNAPSHOT_EVERY = 200
NOTE_MAX = 4000
STABLE_WINDOW_SECONDS = 60.0
OPS = ("ask", "update", "withdraw", "submit", "snapshot")
QUESTION_FIELDS = (
    "answerer",
    "title",
    "background",
    "body",
    "options",
    "recommended",
    "reason",
)


# Element shapes of the views below. They are documentation for readers and
# type checkers only: at run time these are plain dicts (the JSON shape of §5).
Option = TypedDict("Option", {"key": str, "text": str})
Submitted = TypedDict(
    "Submitted",
    {
        "record": str,
        "from": str,
        "base": str,
        "choice": str | None,
        "note": str | None,
        "submitted_at": str | None,
    },
)
Withdrawn = TypedDict("Withdrawn", {"record": str, "reason": str | None})
Decision = TypedDict(
    "Decision",
    {
        "id": str,
        "channel": str,
        "asker": str,
        "answerer": str,
        "status": str,
        "version": str,
        "revisions": int,
        "title": str,
        "background": str,
        "body": str,
        "options": list[Option],
        "recommended": str,
        "reason": str,
        "asked_at": str | None,
        "updated_at": str | None,
        "submitted": Submitted | None,
        "withdrawn": Withdrawn | None,
    },
)
Ignored = TypedDict(
    "Ignored",
    {
        "record": str,
        "channel": str,
        "op": str | None,
        "ref": str | None,
        "item": int | None,
        "code": str,
    },
)


@dataclass(frozen=True)
class Record:
    message: Message
    position: int
    received_at: str | None


@dataclass(frozen=True)
class FoldResult:
    decisions: list[Decision]  # read-only for callers
    ignored: list[Ignored]  # read-only for callers
    seen: dict[str, frozenset[str]]
    closed: frozenset[str]
    snapshot_used: dict[str, str | None]
    folded: int
    complete: bool
    # The `closed` list of each selected snapshot, taken from its already-parsed payload.
    snapshot_closed: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmitPlan:
    batches: list[tuple[str, str, list[dict]]]
    skipped: list[str]


class PayloadError(MessageValidationError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SubmitRejected(MessageValidationError):
    # Each problem is (ref, code). `ref` is whatever the entry held: a string, None when
    # the entry had no ref, or another YAML value that is not a string (L-A5).
    def __init__(self, problems: list[tuple[Any, str]]):
        self.problems = problems
        super().__init__(str(problems))


def _fail():
    raise MessageValidationError("invalid decision payload")


def _question(p: dict) -> None:
    validate_address(p.get("answerer"))
    title = p.get("title")
    if (
        not isinstance(title, str)
        or not 1 <= len(title) <= 120
        or "\n" in title
        or "\r" in title
    ):
        _fail()
    if (
        any(not isinstance(p.get(k), str) for k in ("background", "body", "reason"))
        or not p["body"]
    ):
        _fail()
    opts = p.get("options")
    if not isinstance(opts, list) or not 2 <= len(opts) <= 9:
        _fail()
    keys = []
    for option in opts:
        if (
            not isinstance(option, dict)
            or not isinstance(option.get("key"), str)
            or not re.fullmatch("[A-Z]", option["key"])
            or not isinstance(option.get("text"), str)
            or not option["text"]
        ):
            _fail()
        keys.append(option["key"])
    if len(set(keys)) != len(keys) or p.get("recommended") not in keys:
        _fail()


def _question_fields(p: Mapping) -> dict:
    """The question fields of a validated payload, as the views hold them (§5).

    Options are cut to `key` and `text`: an unknown option key is read and then
    dropped, so no value a writer put there reaches `list --json` or a snapshot.
    """
    q = {k: p[k] for k in QUESTION_FIELDS}
    q["options"] = [{"key": option["key"], "text": option["text"]} for option in p["options"]]
    return q


def _utf8_text(value: Any) -> bool:
    """False if any string in the parsed payload cannot be encoded as UTF-8 (a lone surrogate, YAML `\\ud800`).

    Walks iteratively and skips containers already seen: a YAML alias can make the
    parsed value contain itself.
    """
    seen: set[int] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return False
        elif isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            else:
                pending.extend(item)
    return True


def _answer(
    item: Any, options: list[dict] | None = None, *, title: bool = False
) -> bool:
    if (
        not isinstance(item, dict)
        or not is_ulid(item.get("ref"))
        or not is_ulid(item.get("base"))
    ):
        return False
    if title and (
        not isinstance(item.get("title"), str)
        or not 1 <= len(item["title"]) <= 120
        or "\n" in item["title"]
        or "\r" in item["title"]
    ):
        return False
    choice, note = item.get("choice"), item.get("note")
    if choice is not None and (
        not isinstance(choice, str)
        or not re.fullmatch("[A-Z]", choice)
        or (options is not None and choice not in [x["key"] for x in options])
    ):
        return False
    if note is not None and (
        not isinstance(note, str) or not 1 <= len(note) <= NOTE_MAX
    ):
        return False
    return choice is not None or note is not None


def _validate(p: Any, *, strict: bool) -> None:
    if not isinstance(p, dict):
        _fail()
    if type(p.get("decision")) is not int or p["decision"] != 1:
        _fail()
    op = p.get("op")
    if op not in OPS:
        _fail()
    common = {"decision", "op"}
    allowed = {
        "ask": {"ask_channel", "asker", *QUESTION_FIELDS},
        "update": {"ref", *QUESTION_FIELDS},
        "withdraw": {"ref", "reason"},
        "submit": {"asker", "items"},
        "snapshot": {"asker", "through", "covered", "items", "closed"},
    }[op]
    if strict and set(p) - common - allowed:
        _fail()
    if op in ("ask", "update"):
        _question(p)
        if strict and any(set(x) != {"key", "text"} for x in p["options"]):
            _fail()
    if op == "ask":
        validate_channel_name(p.get("ask_channel"))
        validate_address(p.get("asker"))
    if op in ("update", "withdraw") and not is_ulid(p.get("ref")):
        _fail()
    if op == "withdraw" and "reason" in p and not isinstance(p["reason"], str):
        _fail()
    if op in ("submit", "snapshot"):
        validate_address(p.get("asker"))
    if op == "submit":
        items = p.get("items")
        if not isinstance(items, list) or not items:
            _fail()
        if strict:
            refs = set()
            for item in items:
                if (
                    not _answer(item, title=True)
                    or set(item) - {"ref", "base", "title", "choice", "note"}
                    or item["ref"] in refs
                ):
                    _fail()
                refs.add(item["ref"])
    if op == "snapshot":
        if (
            not is_ulid(p.get("through"))
            or type(p.get("covered")) is not int
            or p["covered"] < 1
            or not isinstance(p.get("items"), list)
            or not isinstance(p.get("closed"), list)
        ):
            _fail()
        ids = set()
        for item in p["items"]:
            if not isinstance(item, dict):
                _fail()
            _question(item)
            if (
                not is_ulid(item.get("id"))
                or not is_ulid(item.get("version"))
                or type(item.get("revisions")) is not int
                or item["revisions"] < 1
            ):
                _fail()
            versions = item.get("versions")
            if (
                not isinstance(versions, list)
                or not all(is_ulid(v) for v in versions)
                or len(set(versions)) != len(versions)
                or len(versions) != item["revisions"]
                or item["id"] not in versions
                or item["version"] not in versions
            ):
                _fail()
            if (
                any(
                    not isinstance(item.get(k), str) for k in ("asked_at", "updated_at")
                )
                or item["id"] in ids
            ):
                _fail()
            ids.add(item["id"])
        for ref in p["closed"]:
            if not is_ulid(ref) or ref in ids:
                _fail()
            ids.add(ref)


def parse_payload(message: Message) -> dict:
    try:
        p = yaml.load(message.content, Loader=FlowDepthLoader)
        if isinstance(p, dict):
            # No `decision` key at all cannot be any version's layout: the writer broke it (#482 Q-2).
            if "decision" not in p:
                raise PayloadError("bad_payload")
            # A key that is there but not the integer 1 is a version this reader does not know (#468 §4.3).
            if type(p["decision"]) is not int or p["decision"] != 1:
                raise PayloadError("unsupported_version")
        if not _utf8_text(p):
            # Such a record cannot be written out as UTF-8 text, so no view may carry it (R2-2).
            raise PayloadError("bad_payload")
        _validate(p, strict=False)
        return p
    except PayloadError:
        raise
    except (
        MessageValidationError,
        yaml.YAMLError,
        TypeError,
        ValueError,
        KeyError,
        RecursionError,
    ):
        raise PayloadError("bad_payload") from None


def build_payload(op: str, **fields: Any) -> str:
    if "decision" in fields or "op" in fields:
        raise MessageValidationError("reserved payload field")
    p = {"decision": PAYLOAD_VERSION, "op": op, **fields}
    try:
        _validate(p, strict=True)
    except (TypeError, KeyError, ValueError) as exc:
        raise MessageValidationError(str(exc)) from exc
    if not _utf8_text(p):
        # The reader drops such a record as bad_payload (R2-2); refuse it here so the writer never sends it (R3-4).
        raise MessageValidationError(
            "a string holds a character that cannot be encoded as UTF-8 (a lone surrogate, "
            "or a JSON escape of an emoji): write the character literally in YAML, or use json.dumps(..., ensure_ascii=False)"
        )
    try:
        result = yaml.safe_dump(p, allow_unicode=True, sort_keys=False)
        if yaml.load(result, Loader=FlowDepthLoader) != p:
            _fail()
        return result
    except yaml.YAMLError as exc:
        raise MessageValidationError(str(exc)) from exc


@dataclass
class _FoldState:
    """What one `fold` pass accumulates: the views, the tombstones and the diagnostics."""

    decisions: dict[str, dict] = field(default_factory=dict)
    versions: dict[str, set[str]] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)
    owners: dict[str, str] = field(default_factory=dict)
    used: dict[str, str | None] = field(default_factory=dict)
    cutoff: dict[str, int] = field(default_factory=dict)
    snapshot_closed: dict[str, list[str]] = field(default_factory=dict)
    ignored: list[dict] = field(default_factory=list)
    folded: int = 0

    def ignore(self, m: Message, op, code, ref=None, item=None) -> None:
        """Record one ignored record in the `ignored` view (§5)."""
        self.ignored.append(
            dict(record=m.id, channel=m.channel, op=op, ref=ref, item=item, code=code)
        )


def _parse_all(
    records: Sequence[Record],
) -> list[tuple[Record, dict | None, str | None]]:
    """Read every record once, keeping the code of a payload that would not read.

    `_apply_unreadable` needs that code for its diagnostic; keeping it here is why a
    record whose payload is bad is read once per fold, not twice.
    """
    parsed: list[tuple[Record, dict | None, str | None]] = []
    for r in records:
        if r.message.datatype != "decision":
            parsed.append((r, None, None))
            continue
        try:
            parsed.append((r, parse_payload(r.message), None))
        except PayloadError as exc:
            parsed.append((r, None, exc.code))
    return parsed


def _prime(state: _FoldState, parsed) -> None:
    """Fill in what is known before a snapshot is chosen.

    An ask names its asker under its own id, and every asker starts with no snapshot
    used, so the newest usable one wins in `_select_snapshots` (first come, first
    served, §6.4).
    """
    for r, p, _ in parsed:
        if p is None or p["op"] not in ("ask", "snapshot"):
            continue
        if p["op"] == "ask":
            state.owners.setdefault(r.message.id, p["asker"])
        state.used.setdefault(p["asker"], None)


def _select_snapshots(records: Sequence[Record], parsed, state: _FoldState) -> None:
    """Take the newest snapshot of each asker that still checks out, and seed from it."""
    for i in range(len(records) - 1, -1, -1):
        r, p, _ = parsed[i]
        if not p or p["op"] != "snapshot" or state.used[p["asker"]] is not None:
            continue
        m = r.message
        found = [j for j, x in enumerate(records) if x.message.id == p["through"]]
        code = None
        if (
            m.from_ != p["asker"]
            or m.to != p["asker"]
            or len(found) != 1
            or found[0] >= i
        ):
            code = "bad_snapshot"
        elif (
            sum(x.message.datatype == "decision" for x in records[: found[0] + 1])
            != p["covered"]
        ):
            code = "snapshot_stale"
        if code:
            state.ignore(m, "snapshot", code)
            continue
        asker = p["asker"]
        state.used[asker] = m.id
        state.snapshot_closed[asker] = list(p["closed"])
        state.cutoff[asker] = records[found[0]].position
        _seed_from_snapshot(state, m, p)


def _seed_from_snapshot(state: _FoldState, m: Message, p: dict) -> None:
    """Start the views from a selected snapshot's items and tombstones (§5)."""
    asker = p["asker"]
    for x in p["items"]:
        q = _question_fields(x)
        q.update(
            id=x["id"],
            channel=m.channel,
            asker=asker,
            status="open",
            version=x["version"],
            revisions=x["revisions"],
            asked_at=x["asked_at"],
            updated_at=x["updated_at"],
            submitted=None,
            withdrawn=None,
        )
        state.decisions[x["id"]] = q
        state.versions[x["id"]] = set(x["versions"])
        state.owners[x["id"]] = asker
    for ref in p["closed"]:
        state.closed.add(ref)
        state.owners[ref] = asker


def _apply(r, p: dict | None, code: str | None, state: _FoldState) -> None:
    """Apply one record, in log order: the snapshot cutoff first, then the op."""
    m = r.message
    if m.datatype != "decision":
        return
    if p is None:
        _apply_unreadable(r, code, state)
        return
    op = p["op"]
    if op == "snapshot":
        if r.position > state.cutoff.get(p["asker"], -1):
            state.folded += 1
        return
    owner = p.get("asker") if op in ("ask", "submit") else state.owners.get(p.get("ref"))
    if owner is not None and r.position <= state.cutoff.get(owner, -1):
        return
    state.folded += 1
    if op == "ask":
        _apply_ask(r, p, state)
    elif op in ("update", "withdraw"):
        _apply_change(r, p, state)
    elif op == "submit":
        _apply_submit(r, p, state)


def _apply_unreadable(r, code: str | None, state: _FoldState) -> None:
    """Diagnose a decision record whose payload would not read (or that a snapshot covers)."""
    m = r.message
    if (
        state.cutoff
        and all(value is not None for value in state.used.values())
        and r.position <= min(state.cutoff.values())
    ):
        return
    is_snapshot = bool(re.search(r"(?m)^op:\s*snapshot\s*$", m.content))
    state.ignore(
        m,
        "snapshot" if is_snapshot else None,
        "bad_snapshot" if is_snapshot else code,
    )
    state.folded += 1


def _apply_ask(r, p: dict, state: _FoldState) -> None:
    """Open a decision from an `ask` record (§5)."""
    m = r.message
    if p["ask_channel"] != m.channel or p["asker"] != m.from_:
        state.ignore(m, "ask", "binding_mismatch")
        return
    if m.id in state.decisions or m.id in state.closed:
        state.ignore(m, "ask", "duplicate_id")
        return
    q = _question_fields(p)
    q.update(
        id=m.id,
        channel=m.channel,
        asker=m.from_,
        status="open",
        version=m.id,
        revisions=1,
        asked_at=r.received_at,
        updated_at=r.received_at,
        submitted=None,
        withdrawn=None,
    )
    state.decisions[m.id] = q
    state.versions[m.id] = {m.id}
    state.owners[m.id] = m.from_


def _apply_change(r, p: dict, state: _FoldState) -> None:
    """Apply an `update` or a `withdraw` to an open decision (§5).

    The checks are one chain, in this order, so the first problem names the code:
    tombstoned, unknown, closed, not the asker, and (for an update) a record id
    already counted.
    """
    m = r.message
    op = p["op"]
    ref = p.get("ref")
    q = state.decisions.get(ref)
    if ref in state.closed:
        code = "closed_ref"
    elif q is None:
        code = "unknown_ref"
    elif q["status"] != "open":
        code = "closed_ref"
    elif m.from_ != q["asker"]:
        code = "not_owner"
    elif op == "update" and m.id in state.versions[ref]:
        code = "duplicate_id"
    else:
        code = None
    if code:
        state.ignore(m, op, code, ref)
        return
    if op == "update":
        q.update(_question_fields(p))
        q.update(
            version=m.id, revisions=q["revisions"] + 1, updated_at=r.received_at
        )
        state.versions[ref].add(m.id)
    else:
        q["status"] = "withdrawn"
        q["withdrawn"] = {"record": m.id, "reason": p.get("reason")}
        state.closed.add(ref)


def _item_code(
    state: _FoldState, m: Message, p: dict, entry, ref, refs: set[str]
) -> str | None:
    """The first problem with one `submit` item, in the order `plan_submit` uses (C-10/C-27).

    This is a second copy of `plan_submit`'s checks, on purpose (#482 §3). They must agree on
    every case except a ref that is not a ULID (`bad_item` here, `unknown_ref` there); the
    parametrized test `test_consistency_fold_and_plan_agree` (#482 PR-4) pins that.
    """
    if isinstance(ref, str) and ref in refs:
        return "duplicate_ref"
    if not is_ulid(ref):
        return "bad_item"
    if ref in state.closed:
        return "closed_ref"
    q = state.decisions.get(ref)
    if q is None:
        return "unknown_ref"
    if q["status"] != "open":
        return "closed_ref"
    if m.from_ != q["answerer"]:
        return "not_answerer"
    if p["asker"] != q["asker"]:
        return "binding_mismatch"
    if entry.get("base") != q["version"]:
        return "stale_base"
    if not _answer(entry, q["options"]):
        return "bad_item"
    return None


def _apply_submit(r, p: dict, state: _FoldState) -> None:
    """Answer the items of a `submit` that check out; diagnose the others, per item (§5)."""
    m = r.message
    if m.to != p["asker"] or p["asker"] not in m.mentions:
        state.ignore(m, "submit", "bad_payload")
        return
    refs: set[str] = set()
    for index, entry in enumerate(p["items"]):
        ref = entry.get("ref") if isinstance(entry, dict) else None
        code = _item_code(state, m, p, entry, ref, refs)
        if isinstance(ref, str):
            refs.add(ref)
        if code:
            # `ref` is the schema's str | None; a foreign value that is not a string is not echoed.
            state.ignore(m, "submit", code, ref if isinstance(ref, str) else None, index)
            continue
        q = state.decisions[ref]
        q["status"] = "answered"
        q["submitted"] = {
            "record": m.id,
            "from": m.from_,
            "base": entry["base"],
            "choice": entry.get("choice"),
            "note": entry.get("note"),
            "submitted_at": r.received_at,
        }
        state.closed.add(ref)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_time(text: str) -> datetime | None:
    """An RFC 3339 time that carries a zone (`Z` is UTC), or None: a naive or unreadable time (#482)."""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _time_key(value: str | None) -> tuple:
    """Sort key for an `asked_at`: no time first, then by instant, unreadable times last (#482).

    Equal instants written differently compare equal; the caller breaks the tie.
    """
    if value is None:
        return (0, _EPOCH)
    moment = _parse_time(value)
    if moment is None:
        return (2, _EPOCH, value)
    return (1, moment)  # aware datetimes compare by instant, whatever their zone


def order_key(q: Decision) -> tuple:
    """The order decisions are listed and planned in: asked time, then channel, then id (#482)."""
    return (_time_key(q["asked_at"]), q["channel"], q["id"])


def fold(records: Sequence[Record], *, use_snapshot: bool) -> FoldResult:
    records = sorted(records, key=lambda r: r.position)
    parsed = _parse_all(records)
    state = _FoldState()
    _prime(state, parsed)
    if use_snapshot:
        _select_snapshots(records, parsed, state)
    for r, p, code in parsed:
        _apply(r, p, code, state)
    ordered = sorted(state.decisions.values(), key=order_key)
    if use_snapshot:
        ordered = [q for q in ordered if q["status"] == "open"]
    return FoldResult(
        decisions=ordered,
        ignored=state.ignored,
        seen={
            k: frozenset(v) for k, v in state.versions.items() if k not in state.closed
        },
        closed=frozenset(state.closed),
        snapshot_used=state.used,
        folded=state.folded,
        complete=not use_snapshot,
        snapshot_closed=state.snapshot_closed,
    )


def snapshot_for(records: Sequence[Record], asker: str) -> str | None:
    validate_address(asker)
    records = sorted(records, key=lambda r: r.position)
    if not records or any(r.received_at is None for r in records):
        return None
    try:
        times = [_parse_time(r.received_at) for r in records]
        if any(t is None for t in times):
            return None
        limit = max(times) - timedelta(seconds=STABLE_WINDOW_SECONDS)
    except OverflowError:
        return None
    through = None
    for i, (r, t) in enumerate(zip(records, times)):
        if t > limit:
            break
        if (
            r.message.datatype == "decision"
            and sum(x.message.id == r.message.id for x in records) == 1
        ):
            through = i
    if through is None:
        return None
    prefix = records[: through + 1]
    result = fold(prefix, use_snapshot=True)
    selected = [q for q in result.decisions if q["asker"] == asker]
    items = []
    for q in selected:
        item = {
            k: q[k]
            for k in (
                "id",
                "version",
                "revisions",
                *QUESTION_FIELDS,
                "asked_at",
                "updated_at",
            )
        }
        item["versions"] = sorted(result.seen[q["id"]])
        items.append(item)
    full = fold(prefix, use_snapshot=False)
    owned = {q["id"] for q in full.decisions if q["asker"] == asker}
    owned.update(result.snapshot_closed.get(asker, []))
    closed = [
        q["id"] for q in full.decisions if q["id"] in result.closed and q["id"] in owned
    ]
    closed.extend(sorted((result.closed & owned) - set(closed)))
    return build_payload(
        "snapshot",
        asker=asker,
        through=records[through].message.id,
        covered=sum(r.message.datatype == "decision" for r in prefix),
        items=items,
        closed=closed,
    )


def related_count(records: Sequence[Record], asker: str) -> int:
    """Records after the asker's current snapshot that belong to the asker (§7.1).

    The records are external input, read as leniently as `fold` reads them: a
    bad item is simply not counted, so nothing here may raise on it.
    """
    owners: dict[str, str] = {}
    parsed: list[tuple[Record, dict]] = []
    for r in records:
        if r.message.datatype != "decision":
            continue
        try:
            p = parse_payload(r.message)
        except PayloadError:
            continue
        parsed.append((r, p))
        if p["op"] == "ask":
            owners.setdefault(r.message.id, p["asker"])

    floor = -1
    used = fold(list(records), use_snapshot=True).snapshot_used.get(asker)
    if used is not None:
        snapshot = next(r for r in records if r.message.id == used)
        through = parse_payload(snapshot.message)["through"]
        floor = next(r.position for r in records if r.message.id == through)

    count = 0
    for r, p in parsed:
        if r.position <= floor or p["op"] == "snapshot":
            continue
        if p["op"] == "ask":
            hit = p["asker"] == asker
        elif p["op"] in ("update", "withdraw"):
            hit = owners.get(p["ref"]) == asker
        else:
            # A submit item whose ref is not a string names no decision, so it is not counted.
            refs = [item.get("ref") for item in p["items"] if isinstance(item, dict)]
            hit = any(isinstance(ref, str) and owners.get(ref) == asker for ref in refs)
        count += hit
    return count


def plan_submit(
    results: Mapping[str, FoldResult], answerer: str, entries: Sequence[dict], *, render
) -> SubmitPlan:
    if any(not result.complete for result in results.values()):
        raise ValueError("submit requires complete folds")
    questions = {q["id"]: q for result in results.values() for q in result.decisions}
    seen: set[str] = set()
    problems = []
    skipped = []
    groups: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for entry in entries:
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if isinstance(ref, str) and ref in seen:
            problems.append((ref, "duplicate_ref"))
            continue
        if isinstance(ref, str):
            seen.add(ref)
        q = questions.get(ref) if isinstance(ref, str) else None
        if q and q["status"] == "answered":
            s = q["submitted"]
            if s and all(
                a == b
                for a, b in (
                    (s.get("from"), answerer),
                    (s.get("base"), entry.get("base")),
                    (s.get("choice"), entry.get("choice")),
                    (s.get("note"), entry.get("note")),
                )
            ):
                skipped.append(ref)
                continue
        if q is None:
            code = "unknown_ref"
        elif q["status"] != "open":
            code = "closed_ref"
        elif q["answerer"] != answerer:
            code = "not_answerer"
        elif entry.get("base") != q["version"]:
            code = "stale_base"
        elif not _answer(entry, q["options"]) or set(entry) - {
            "ref",
            "base",
            "choice",
            "note",
        }:
            code = "bad_item"
        else:
            code = None
        if code:
            problems.append((ref, code))
            continue
        item = {"ref": ref, "base": entry["base"], "title": q["title"]}
        for k in ("choice", "note"):
            if k in entry:
                item[k] = entry[k]
        groups.setdefault((q["channel"], q["asker"]), []).append(
            (_time_key(q["asked_at"]), item)
        )
    if problems:
        raise SubmitRejected(problems)
    batches = []
    for (channel, asker), items in sorted(groups.items()):
        current = []
        for _, item in sorted(items, key=lambda v: (v[0], v[1]["ref"])):
            trial = current + [item]
            if len(render(channel, asker, trial)) > MAX_MESSAGE_LENGTH:
                if not current:
                    raise MessageValidationError("submit item exceeds maximum length")
                batches.append((channel, asker, current))
                current = [item]
                if len(render(channel, asker, current)) > MAX_MESSAGE_LENGTH:
                    raise MessageValidationError("submit item exceeds maximum length")
            else:
                current = trial
        if current:
            batches.append((channel, asker, current))
    return SubmitPlan(batches=batches, skipped=skipped)
