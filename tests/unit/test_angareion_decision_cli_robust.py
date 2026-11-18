"""`angareion decision` robustness cases through the CLI with fakes (#468 PR-4, design §15 C-n).

H1, M1, M2, R2-1/R2-2, R3-4 and R3-2.
The backend, `World` and the shared helpers live in `_decision_cli_lab.py`.
"""

import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

import contextlib
from datetime import date
import io
import json

import pytest

from _decision_cli_lab import (
    BASE,
    CH,
    H,
    LITERAL_EMOJI,
    P,
    Result,
    _build_history_for_snapshot,
    _snapshot_records,
    question,
    world,
    yaml_of,
)
from angareion import decision as dec
from angareion.backend import format_backend_time
from angareion.cli import main
from angareion.messages import Message, parse_message, serialize_message

# -- H1: a foreign submit item whose ref is not a string never breaks a write ---


@pytest.mark.parametrize("ref", ["[bad]", "{k: v}", "2026-11-15", "42", "null"])
def test_h1_a_malformed_foreign_item_does_not_break_later_writes_or_reads(world, ref):
    first = world.ask(title="先出的題")
    text = f"decision: 1\nop: submit\nasker: {P}\nitems:\n- ref: {ref}\n  base: bad\n  choice: A\n"
    message = Message(from_=H, to=P, mentions=[P], channel=CH, datatype="decision", content=text)
    world.backend.deliver(world.entity(CH), parse_message(serialize_message(message), strict=True))

    listed = world.list_json("--all")
    assert [(x["code"], x["item"], x["ref"]) for x in listed["ignored"]] == [("bad_item", 0, None)]

    asked = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(title="後出的題")))
    assert asked.code == 0 and asked.err == "" and len(asked.out.split()) == 1
    second = asked.out.strip()
    assert world.run(P, "decision", "update", "--channel", CH, "--ref", first, "--body", yaml_of(question(title="改"))).code == 0
    assert world.run(P, "decision", "withdraw", "--channel", CH, "--ref", second, "--reason", "不要了").code == 0
    assert world.run(P, "decision", "snapshot", "--channel", CH).code == 0
    assert world.run(P, "decision", "list", "--json").code == 0
    assert world.run(P, "decision", "list", "--all").code == 0


# -- M1: once the write has landed, nothing in the automatic snapshot can fail it -


def _fail_listing_after_next_send(world, monkeypatch):
    """The next send lands; every listing after it fails (the snapshot's sync is the first to see it)."""
    original = world.backend.send

    def send(entity, message):
        result = original(entity, message)
        world.backend.fail_list = True
        return result

    monkeypatch.setattr(world.backend, "send", send)


@pytest.mark.parametrize("action", ["ask", "update", "withdraw"])
def test_m1_a_listing_failure_after_the_write_only_warns(world, monkeypatch, action):
    qid = world.ask(title="先出")
    before = len(world.stored(CH))
    argv = {
        "ask": ["ask", "--channel", CH, "--body", yaml_of(question(title="新題"))],
        "update": ["update", "--channel", CH, "--ref", qid, "--body", yaml_of(question(title="改"))],
        "withdraw": ["withdraw", "--channel", CH, "--ref", qid, "--reason", "不要了"],
    }[action]
    _fail_listing_after_next_send(world, monkeypatch)

    result = world.run(P, "decision", *argv)
    world.backend.fail_list = False
    assert result.code == 0
    assert len(result.out.split()) == 1
    assert "snapshot was not written" in result.err and "listing failed" in result.err
    assert len(world.stored(CH)) == before + 1


def test_m1_a_snapshot_send_that_fails_only_warns(world, monkeypatch):
    _build_history_for_snapshot(world, monkeypatch)
    world.backend.fail_send = world.backend.sends + 2  # the ask lands; the snapshot's send fails before it lands
    world.at(201)
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(title="四")))
    assert result.code == 0 and len(result.out.split()) == 1
    assert "snapshot was not written" in result.err
    assert _snapshot_records(world) == []


def test_m1_a_failed_write_is_still_exit_2_and_says_nothing_about_snapshots(world):
    world.backend.fail_send = world.backend.sends + 1
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question()))
    assert result.code == 2 and result.out == ""
    assert "snapshot" not in result.err


# -- M2: a too-deep body is an input error (exit 1), before anything is sent ----


@pytest.mark.parametrize("action", ["ask", "update", "submit"])
def test_m2_a_body_nested_past_the_yaml_limit_is_exit_1_and_sends_nothing(world, action):
    qid = world.ask()
    before = len(world.stored(CH))
    deep = "[" * 1500 + "0" + "]" * 1500
    argv = {
        "ask": (P, ["ask", "--channel", CH, "--body", deep]),
        "update": (P, ["update", "--channel", CH, "--ref", qid, "--body", deep]),
        "submit": (H, ["submit", "--channel", CH, "--body", deep]),
    }[action]
    result = world.run(argv[0], "decision", *argv[1])
    assert result.code == 1 and result.out == ""
    assert "nested too deeply" in result.err and "Unexpected error" not in result.err
    assert len(world.stored(CH)) == before


@pytest.mark.parametrize("action", ["ask", "update", "submit"])
def test_m2_a_deep_mapping_or_sequence_where_a_field_belongs_is_exit_1(world, action):
    qid = world.ask()
    before = len(world.stored(CH))
    deep_map = "{a: " * 300 + "1" + "}" * 300
    deep_seq = "[" * 300 + "0" + "]" * 300
    bodies = {
        "ask": [f"title: {deep_map}", f"options: {deep_seq}", f"answerer: {deep_map}"],
        "update": [f"title: {deep_map}", f"options: {deep_seq}"],
        "submit": [f"items: {deep_seq}", f"items: [{{ref: {deep_map}, base: x}}]", f"items: [{{ref: {deep_seq}, base: x}}]"],
    }[action]
    who = H if action == "submit" else P
    for body in bodies:
        if action == "update":
            argv = ["update", "--channel", CH, "--ref", qid, "--body", body]
        else:
            argv = [action, "--channel", CH, "--body", body]
        result = world.run(who, "decision", *argv)
        assert result.code == 1 and result.out == "", (body[:40], result.err)
        assert "Unexpected error" not in result.err
    assert len(world.stored(CH)) == before


# -- R2-1, R2-2: what a reader keeps from a payload it did not write -------------


def _self_containing() -> list:
    loop: list = []
    loop.append(loop)
    return loop


# Values a writer can put under an unknown option key: YAML-only types, a recursive alias, a mapping, a plain string.
UNKNOWN_OPTION_VALUES = {
    "date": lambda: date(2026, 11, 15),
    "alias": _self_containing,
    "mapping": lambda: {"when": date(2026, 11, 15)},
    "string": lambda: "額外的字串",
}


def _with_unknown_option_key(payload: dict, kind: str) -> dict:
    payload["options"][0]["extra"] = UNKNOWN_OPTION_VALUES[kind]()
    return payload


def _assert_views_are_clean(world, count: int) -> None:
    """`list --json` (snapshot path and `--all`) and the text list read the records and show only §5's shape."""
    for flags in ([], ["--all"]):
        result = world.run(P, "decision", "list", "--json", *flags)
        assert result.code == 0, result.err
        data = json.loads(result.out)
        assert len(data["decisions"]) == count
        for view in data["decisions"]:
            assert [sorted(option) for option in view["options"]] == [["key", "text"], ["key", "text"]]
    result = world.run(P, "decision", "list")
    assert result.code == 0, result.err


@pytest.mark.parametrize("kind", list(UNKNOWN_OPTION_VALUES))
def test_r2_1_an_unknown_option_key_in_an_ask_is_dropped(world, kind):
    payload = _with_unknown_option_key({"decision": 1, "op": "ask", "ask_channel": CH, "asker": P, **question()}, kind)
    world.deliver(CH, P, H, yaml_of(payload))
    _assert_views_are_clean(world, 1)


@pytest.mark.parametrize("kind", list(UNKNOWN_OPTION_VALUES))
def test_r2_1_an_unknown_option_key_in_an_update_is_dropped(world, kind):
    qid = world.ask()
    payload = _with_unknown_option_key({"decision": 1, "op": "update", "ref": qid, **question(body="改過")}, kind)
    world.deliver(CH, P, H, yaml_of(payload), in_reply_to=qid)
    _assert_views_are_clean(world, 1)


@pytest.mark.parametrize("kind", list(UNKNOWN_OPTION_VALUES))
def test_r2_1_an_unknown_option_key_in_a_snapshot_item_is_dropped(world, kind):
    qid = world.ask()
    item = {
        "id": qid,
        "version": qid,
        "revisions": 1,
        "versions": [qid],
        "asked_at": "2026-11-15T03:00:00Z",
        "updated_at": "2026-11-15T03:00:00Z",
        **question(),
    }
    snapshot = {
        "decision": 1,
        "op": "snapshot",
        "asker": P,
        "through": qid,
        "covered": 1,
        "closed": [],
        "items": [_with_unknown_option_key(item, kind)],
    }
    world.deliver(CH, P, P, yaml_of(snapshot))
    _assert_views_are_clean(world, 1)


@pytest.mark.parametrize("kind", list(UNKNOWN_OPTION_VALUES))
def test_r2_1_a_manual_snapshot_survives_an_unknown_option_key(world, kind):
    payload = _with_unknown_option_key({"decision": 1, "op": "ask", "ask_channel": CH, "asker": P, **question()}, kind)
    world.deliver(CH, P, H, yaml_of(payload), created=format_backend_time(BASE))
    world.at(120)
    world.ask(title="後來的題目")  # not stable yet, so the snapshot covers only the first ask
    result = world.run(P, "decision", "snapshot", "--channel", CH)
    assert result.code == 0, result.err
    assert len(result.out.split()) == 1
    snapshots = _snapshot_records(world)
    assert len(snapshots) == 1
    items = dec.parse_payload(snapshots[0])["items"]
    assert [sorted(option) for option in items[0]["options"]] == [["key", "text"], ["key", "text"]]


def _run_on_utf8_stdout(world, who: str, *argv: str):
    """Like World.run, but stdout is a real UTF-8 stream, where a lone surrogate cannot be written."""
    world.monkeypatch.setenv("ANGAREION_IDENTITY", who)
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="utf-8", errors="surrogateescape")
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(
            list(argv),
            backend=world.backend,
            store=world.store,
            registry=world.registry,
            sleeper=lambda seconds: None,
            clock=lambda: 0.0,
        )
    out.flush()
    return Result(code, buf.getvalue().decode("utf-8", "surrogateescape"), err.getvalue())


SURROGATE_ASK = (
    "decision: 1\nop: ask\nask_channel: general\nasker: " + P + "\nanswerer: " + H + "\n"
    'title: "壞\\ud800標題"\nbackground: 背景\nbody: 內容\nreason: 理由\nrecommended: A\n'
    "options:\n  - key: A\n    text: 先合\n  - key: B\n    text: 等確認\n"
)


def test_r2_2_a_lone_surrogate_in_a_payload_makes_that_record_bad_payload(world):
    qid = world.ask()
    world.deliver(CH, P, H, SURROGATE_ASK)
    for flags in ([], ["--json"], ["--all", "--json"]):
        result = _run_on_utf8_stdout(world, P, "decision", "list", *flags)
        assert result.code == 0, result.err
        if flags:
            data = json.loads(result.out)
            assert [d["id"] for d in data["decisions"]] == [qid]
            assert ("bad_payload", None, None) in [(x["code"], x["item"], x["ref"]) for x in data["ignored"]]
        else:
            assert "1 record(s) ignored" in result.err


def test_r2_2_a_lone_surrogate_in_a_submit_note_makes_that_record_bad_payload(world):
    qid = world.ask()
    content = (
        "decision: 1\nop: submit\nasker: " + P + "\nitems:\n"
        f"  - ref: {qid}\n    base: {qid}\n    choice: A\n    note: \"壞\\ud800\"\n"
    )
    world.deliver(CH, H, P, content, mentions=[P])
    result = _run_on_utf8_stdout(world, P, "decision", "list", "--all", "--json")
    assert result.code == 0, result.err
    data = json.loads(result.out)
    assert data["decisions"][0]["status"] == "open"
    assert "bad_payload" in [x["code"] for x in data["ignored"]]


# -- R3-4: the writer refuses what the reader would drop as bad_payload ---------

# json.dumps(...) with its default ensure_ascii=True writes an emoji as a surrogate pair and a lone surrogate as one escape.
ESCAPED_TEXT = {
    "lone_surrogate": "壞\ud800標題",
    "json_emoji_pair": "😀 要不要先合",
}


def _ask_body(title: str) -> str:
    return json.dumps(question(title=title))


def _update_body(body: str) -> str:
    return json.dumps(question(body=body))


def _submit_body(qid: str, note: str) -> str:
    return json.dumps({"items": [{"ref": qid, "base": qid, "choice": "A", "note": note}]})


@pytest.mark.parametrize("name", list(ESCAPED_TEXT))
def test_r3_4_ask_refuses_an_unwritable_title(world, name):
    before = len(world.stored(CH))
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", _ask_body(ESCAPED_TEXT[name]))
    assert result.code == 1, (result.code, result.out, result.err)
    assert result.out == ""
    assert "UTF-8" in result.err
    assert len(world.stored(CH)) == before
    assert world.run(P, "decision", "list", "--all", "--json").code == 0


@pytest.mark.parametrize("name", list(ESCAPED_TEXT))
def test_r3_4_update_refuses_an_unwritable_field(world, name):
    qid = world.ask()
    before = len(world.stored(CH))
    result = world.run(P, "decision", "update", "--channel", CH, "--ref", qid, "--body", _update_body(ESCAPED_TEXT[name]))
    assert result.code == 1, (result.code, result.out, result.err)
    assert "UTF-8" in result.err
    assert len(world.stored(CH)) == before


@pytest.mark.parametrize("name", list(ESCAPED_TEXT))
def test_r3_4_submit_refuses_an_unwritable_note(world, name):
    qid = world.ask()
    before = len(world.stored(CH))
    result = world.run(H, "decision", "submit", "--channel", CH, "--body", _submit_body(qid, ESCAPED_TEXT[name]))
    assert result.code == 1, (result.code, result.out, result.err)
    assert "UTF-8" in result.err
    assert len(world.stored(CH)) == before


def test_r3_4_a_literal_emoji_is_written_and_read_back(world):
    body = json.dumps(question(title=LITERAL_EMOJI), ensure_ascii=False)
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", body)
    assert result.code == 0, result.err
    qid = result.out.strip()
    listed = world.run(P, "decision", "list", "--json")
    data = json.loads(listed.out)
    assert data["ignored"] == []
    assert [(d["id"], d["title"]) for d in data["decisions"]] == [(qid, LITERAL_EMOJI)]


def test_r3_4_a_literal_emoji_in_a_submit_note_is_written_and_read_back(world):
    qid = world.ask()
    body = json.dumps(
        {"items": [{"ref": qid, "base": qid, "choice": "A", "note": LITERAL_EMOJI}]}, ensure_ascii=False
    )
    result = world.run(H, "decision", "submit", "--channel", CH, "--body", body)
    assert result.code == 0, result.err
    data = json.loads(world.run(P, "decision", "list", "--all", "--json").out)
    assert data["ignored"] == []
    assert data["decisions"][0]["submitted"]["note"] == LITERAL_EMOJI



# -- R3-2: a lone surrogate in a nested string, or in a key the views drop -------

SURROGATE = "壞\ud800"
READ_PLACES = ["option_text", "background", "reason", "unknown_top", "unknown_option"]


def _plant(payload: dict, where: str) -> dict:
    """Put SURROGATE where a reader must notice it: a field the view carries (top or nested), or a key it drops."""
    if where == "option_text":
        payload["options"][1]["text"] = SURROGATE
    elif where == "background":
        payload["background"] = SURROGATE
    elif where == "reason":
        payload["reason"] = SURROGATE
    elif where == "unknown_top":
        payload["extra"] = SURROGATE
    elif where == "unknown_option":
        payload["options"][0]["extra"] = SURROGATE
    else:
        raise AssertionError(where)
    return payload


@pytest.mark.parametrize("where", READ_PLACES)
def test_r3_2_a_lone_surrogate_anywhere_in_an_ask_makes_it_bad_payload(world, where):
    payload = _plant({"decision": 1, "op": "ask", "ask_channel": CH, "asker": P, **question()}, where)
    world.deliver(CH, P, H, json.dumps(payload))
    for flags in ([], ["--json"], ["--all", "--json"]):
        result = _run_on_utf8_stdout(world, P, "decision", "list", *flags)
        assert result.code == 0, result.err
        if flags:
            data = json.loads(result.out)
            assert data["decisions"] == []
            assert [x["code"] for x in data["ignored"]] == ["bad_payload"]
        else:
            assert "1 record(s) ignored" in result.err


@pytest.mark.parametrize("where", ["option_text", "unknown_option"])
def test_r3_2_a_lone_surrogate_in_an_update_leaves_the_question_as_asked(world, where):
    qid = world.ask()
    payload = _plant({"decision": 1, "op": "update", "ref": qid, **question(body="改過")}, where)
    world.deliver(CH, P, H, json.dumps(payload), in_reply_to=qid)
    result = _run_on_utf8_stdout(world, P, "decision", "list", "--json")
    assert result.code == 0, result.err
    data = json.loads(result.out)
    assert [(d["id"], d["body"]) for d in data["decisions"]] == [(qid, "內容")]
    assert [x["code"] for x in data["ignored"]] == ["bad_payload"]


def test_r3_2_a_lone_surrogate_in_a_submit_choice_makes_that_record_bad_payload(world):
    qid = world.ask()
    content = json.dumps({"decision": 1, "op": "submit", "asker": P, "items": [{"ref": qid, "base": qid, "choice": SURROGATE}]})
    world.deliver(CH, H, P, content, mentions=[P])
    result = _run_on_utf8_stdout(world, P, "decision", "list", "--all", "--json")
    assert result.code == 0, result.err
    data = json.loads(result.out)
    assert data["decisions"][0]["status"] == "open"
    assert [x["code"] for x in data["ignored"]] == ["bad_payload"]
