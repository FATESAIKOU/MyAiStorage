"""`angareion decision <action>`: the command side of pending decisions (#468, design §5, §7.1, §9, §13.1).

The rules (what a record means, what is open, how a snapshot is read) live in
`decision.py`. This module only syncs the channel's log mirror, asks `decision`
for the summary and the preflight, builds `datatype: decision` messages and
posts them with `backend.send`. It never reads or moves the cursors (C-18).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Protocol, Sequence

import yaml

from angareion import decision as dec
from angareion.backend import BackendError, EnumerableBackend, Entity
from angareion.config import Config, mask_token
from angareion.log_cache import LogCache
from angareion.messages import (
    Message,
    MessageValidationError,
    is_ulid,
    now_iso,
    serialize_message,
)

# The same exit codes as cli.py (cli imports this module, so they are not imported back).
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND = 2

DATATYPE = "decision"
SUBMIT_ITEM_KEYS = {"ref", "base", "choice", "note"}


class DecisionBackend(EnumerableBackend, Protocol):
    """What `decision` asks of a backend: post one message, and list the channel (via `LogCache.sync`)."""

    def send(self, entity: Entity, message: Message) -> str: ...


def _warn(text: str) -> None:
    sys.stderr.write(f"Warning: {text}\n")


def _body_text(args: Any) -> str:
    # Same rule as `send`: only an explicit `--body -` reads stdin.
    if args.body is None:
        raise MessageValidationError(
            "no body: pass --body <YAML>, or --body - to read the YAML on stdin"
        )
    if args.body == "-":
        return sys.stdin.read()
    return args.body


def _mapping(text: str, *, allowed: set[str]) -> dict[str, Any]:
    """The body as a YAML mapping; a key outside `allowed` is an input error (exit 1)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MessageValidationError(f"body is not valid YAML: {e}") from e
    except RecursionError as e:
        # yaml nests by recursion: a body this deep is an input error, not a backend one (§5).
        raise MessageValidationError("body is nested too deeply to read") from e
    if not isinstance(data, dict):
        raise MessageValidationError("body must be a YAML mapping")
    extra = sorted(str(key) for key in data if key not in allowed)
    if extra:
        raise MessageValidationError(f"body has keys this action does not take: {extra}")
    return data


def _ulid_arg(value: Any) -> str:
    if not is_ulid(value):
        raise MessageValidationError(f"--ref must be a 26-character ULID, got '{value}'")
    return value


def _entities(app: Any, channels: Sequence[str] | None) -> list[Entity]:
    """The channels a read or a submit covers: the named ones (repeats dropped), else every registered one."""
    if not channels:
        return list(app.store.entities())
    names: list[str] = []
    for name in channels:
        if name not in names:
            names.append(name)
    return [app.store.entity_for(name) for name in names]


def _sync(config: Config, backend: DecisionBackend, entity: Entity) -> list[dec.Record]:
    """The channel's whole mirror as records, in arrival order (the mirror is what `log` reads)."""
    cache = LogCache(config.state_dir, config.identity)
    messages = cache.sync(backend, entity)
    return [dec.Record(m, int(m.comment_id), m.received_at) for m in messages]


def _message(
    config: Config,
    *,
    channel: str,
    to: str,
    content: str,
    mentions: Sequence[str] = (),
    in_reply_to: str | None = None,
    at: str | None = None,
) -> Message:
    fields: dict[str, Any] = {"at": at} if at is not None else {}
    return Message(
        from_=config.identity,
        to=to,
        channel=channel,
        content=content,
        mentions=list(mentions),
        in_reply_to=in_reply_to,
        datatype=DATATYPE,
        **fields,
    )


def _precheck(result: dec.FoldResult, ref: str, identity: str) -> dict:
    """The owner-side preconditions for update and withdraw (§5); the decision's current state."""
    if ref in result.closed:
        raise MessageValidationError(f"{ref}: closed_ref (the decision is already closed)")
    question = next((q for q in result.decisions if q["id"] == ref), None)
    if question is None:
        raise MessageValidationError(f"{ref}: unknown_ref (no open decision with this id in the channel)")
    if question["asker"] != identity:
        raise MessageValidationError(f"{ref}: not_owner (only the asker of this decision may change it)")
    return question


def _maybe_snapshot(config: Config, backend: DecisionBackend, entity: Entity, token: str) -> None:
    """Automatic snapshot after the asker's own write (§7.1).

    The whole step (listing, counting, building, sending) runs after the write
    has succeeded, so any failure here only warns; the write's exit code and
    stdout stand. The caller's own send errors are raised before this runs.
    """
    try:
        records = _sync(config, backend, entity)
        if dec.related_count(records, config.identity) < dec.SNAPSHOT_EVERY:
            return
        text = dec.snapshot_for(records, config.identity)
        if text is None:
            return
        message = _message(config, channel=entity.channel, to=config.identity, content=text)
        serialize_message(message)
        backend.send(entity, message)
    except Exception as e:
        _warn(
            f"snapshot was not written ({mask_token(str(e), token)}); "
            "the decision itself was written and is unchanged."
        )


def _write_one(config: Config, backend: DecisionBackend, entity: Entity, message: Message) -> str:
    """Check a message can be read back (strict, §4) before it is sent; a refusal sends nothing."""
    serialize_message(message)
    return backend.send(entity, message)


def cmd_ask(args: Any, config: Config, app: Any, backend: DecisionBackend, token: str) -> int:
    entity = app.store.entity_for(args.channel)
    fields = _mapping(_body_text(args), allowed=set(dec.QUESTION_FIELDS))
    content = dec.build_payload("ask", ask_channel=args.channel, asker=config.identity, **fields)
    _sync(config, backend, entity)
    message = _message(config, channel=args.channel, to=fields["answerer"], content=content)
    sys.stdout.write(f"{_write_one(config, backend, entity, message)}\n")
    _maybe_snapshot(config, backend, entity, token)
    return EXIT_OK


def cmd_update(args: Any, config: Config, app: Any, backend: DecisionBackend, token: str) -> int:
    entity = app.store.entity_for(args.channel)
    ref = _ulid_arg(args.ref)
    fields = _mapping(_body_text(args), allowed=set(dec.QUESTION_FIELDS))
    content = dec.build_payload("update", ref=ref, **fields)
    result = dec.fold(_sync(config, backend, entity), use_snapshot=True)
    _precheck(result, ref, config.identity)
    message = _message(config, channel=args.channel, to=fields["answerer"], content=content, in_reply_to=ref)
    sys.stdout.write(f"{_write_one(config, backend, entity, message)}\n")
    _maybe_snapshot(config, backend, entity, token)
    return EXIT_OK


def cmd_withdraw(args: Any, config: Config, app: Any, backend: DecisionBackend, token: str) -> int:
    entity = app.store.entity_for(args.channel)
    ref = _ulid_arg(args.ref)
    fields = {} if args.reason is None else {"reason": args.reason}
    content = dec.build_payload("withdraw", ref=ref, **fields)
    result = dec.fold(_sync(config, backend, entity), use_snapshot=True)
    question = _precheck(result, ref, config.identity)
    message = _message(
        config, channel=args.channel, to=question["answerer"], content=content, in_reply_to=ref
    )
    sys.stdout.write(f"{_write_one(config, backend, entity, message)}\n")
    _maybe_snapshot(config, backend, entity, token)
    return EXIT_OK


def _submit_entries(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise MessageValidationError("submit body needs a non-empty items list")
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise MessageValidationError("each submit item must be a mapping with ref and base")
        extra = sorted(str(key) for key in item if key not in SUBMIT_ITEM_KEYS)
        if extra:
            raise MessageValidationError(f"submit item has keys this action does not take: {extra}")
        if "ref" not in item or "base" not in item:
            raise MessageValidationError(
                "each submit item needs ref and base (base is the version you answered: take it from `decision list --json`)"
            )
        entries.append(dict(item))
    return entries


def _submit_render(config: Config, at: str) -> Callable[[str, str, list[dict]], str]:
    """The text a batch would have, for plan_submit to measure: the front matter plus the payload.

    The front matter does not depend on the content, so the length is exact;
    a batch that is too long is split by plan_submit, and one that cannot be
    read back is refused when the batches are built (exit 1, nothing sent).
    """

    def render(channel: str, asker: str, items: list[dict]) -> str:
        payload = dec.build_payload("submit", asker=asker, items=items)
        head = serialize_message(_message(config, at=at, channel=channel, to=asker, mentions=[asker], content=""))
        return head + payload

    return render


def cmd_submit(args: Any, config: Config, app: Any, backend: DecisionBackend, token: str) -> int:
    entries = _submit_entries(_mapping(_body_text(args), allowed={"items"}))
    entities = _entities(app, args.channel)
    results = {e.channel: dec.fold(_sync(config, backend, e), use_snapshot=False) for e in entities}
    at = now_iso()
    try:
        plan = dec.plan_submit(results, config.identity, entries, render=_submit_render(config, at))
    except dec.SubmitRejected as e:
        for ref, code in e.problems:
            sys.stderr.write(f"Error: {ref}: {code}\n")
        sys.stderr.write("Error: nothing was sent.\n")
        return EXIT_USAGE

    for ref in plan.skipped:
        sys.stderr.write(f"已送出，略過：{ref}\n")
    if not plan.batches:
        sys.stderr.write("Nothing to send: every item was already sent with the same answer.\n")
        return EXIT_OK

    # Build and check every message before the first send: a batch that cannot
    # be written sends nothing (§9, 整批組裝).
    outgoing: list[tuple[str, Message]] = []
    for channel, asker, items in plan.batches:
        content = dec.build_payload("submit", asker=asker, items=items)
        message = _message(config, at=at, channel=channel, to=asker, mentions=[asker], content=content)
        serialize_message(message)
        outgoing.append((channel, message))

    total = len(outgoing)
    for index, (channel, message) in enumerate(outgoing, start=1):
        entity = app.store.entity_for(channel)
        try:
            message_id = backend.send(entity, message)
        except BackendError as e:
            sys.stderr.write(
                f"Error: record {index}/{total} failed ({mask_token(str(e), token)}); "
                f"the {index - 1} before it were sent and the rest were not. "
                "Run the same command again to send the rest.\n"
            )
            return EXIT_BACKEND
        sys.stdout.write(f"{message_id}\n")
    return EXIT_OK


def cmd_snapshot(args: Any, config: Config, app: Any, backend: DecisionBackend, token: str) -> int:
    entity = app.store.entity_for(args.channel)
    text = dec.snapshot_for(_sync(config, backend, entity), config.identity)
    if text is None:
        return EXIT_OK
    message = _message(config, channel=args.channel, to=config.identity, content=text)
    sys.stdout.write(f"{_write_one(config, backend, entity, message)}\n")
    return EXIT_OK


def _as_text(question: dict) -> str:
    lines = [
        f"{question['id']}  {question['status']}  channel: {question['channel']}  "
        f"asker: {question['asker']}  answerer: {question['answerer']}  "
        f"version: {question['version']}  revisions: {question['revisions']}",
        f"  title: {question['title']}",
    ]
    for key in ("background", "body"):
        if question[key]:
            lines.extend(f"  {key}: {line}" for line in question[key].splitlines())
    lines.extend(f"  {option['key']}. {option['text']}" for option in question["options"])
    lines.append(f"  recommended: {question['recommended']}")
    if question["reason"]:
        lines.append(f"  reason: {question['reason']}")
    if question["submitted"]:
        s = question["submitted"]
        lines.append(f"  answered: choice={s['choice']} note={s['note']} (from {s['from']}, record {s['record']})")
    if question["withdrawn"]:
        lines.append(f"  withdrawn: reason={question['withdrawn']['reason']} (record {question['withdrawn']['record']})")
    return "\n".join(lines) + "\n"


def cmd_list(args: Any, config: Config, app: Any, backend: DecisionBackend, token: str) -> int:
    decisions: list[dict] = []
    ignored: list[dict] = []
    for entity in _entities(app, args.channel):
        result = dec.fold(_sync(config, backend, entity), use_snapshot=not args.all)
        decisions.extend(result.decisions)
        ignored.extend(result.ignored)
    decisions.sort(key=dec.order_key)

    if args.json:
        sys.stdout.write(json.dumps({"decisions": decisions, "ignored": ignored}, indent=2, ensure_ascii=False) + "\n")
    else:
        for question in decisions:
            sys.stdout.write(_as_text(question))
        if ignored:
            sys.stderr.write(f"{len(ignored)} record(s) ignored; use --json to see them.\n")
    return EXIT_OK


_ACTIONS = {
    "ask": cmd_ask,
    "update": cmd_update,
    "withdraw": cmd_withdraw,
    "submit": cmd_submit,
    "list": cmd_list,
    "snapshot": cmd_snapshot,
}


def cmd_decision(args: Any, config: Config, app: Any, token: str) -> int:
    app.store.ensure_fresh()
    return _ACTIONS[args.decision_action](args, config, app, app.backend, token)
