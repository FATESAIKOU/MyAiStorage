"""`angareion decision list` and the records it reads, with fakes (#468 PR-4, design §15 C-n).

C-15, C-18, C-19, C-20(e), C-23, C-27 and C-28.
The backend, `World` and the shared helpers live in `_decision_cli_lab.py`.
"""

import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

from datetime import timedelta
import json

import pytest
import yaml

from _decision_cli_lab import (
    BASE,
    CH,
    H,
    H2,
    NO_ULID,
    OTHER,
    P,
    _build_history_for_snapshot,
    _snapshot_records,
    answer,
    question,
    submit_payload,
    uid,
    world,
    yaml_of,
)
from angareion import decision as dec
from angareion import decision_cli
from angareion.backend import Cursor, ETagRecord, format_backend_time
from angareion.messages import Message, MessageValidationError
from angareion.state import CursorStore, FileETagCache

# -- C-15: bad records are ignored, never fatal ---------------------------------


def test_c15_bad_records_are_ignored_and_the_rest_still_counts(world):
    qid = world.ask()
    world.deliver(CH, H, H, "decision: [")
    world.deliver(CH, H, H, "decision: 2\nop: ask\n")
    world.deliver(CH, H, H, "decision: 1\nop: poll\n")
    second = world.ask(title="第二題")
    items = [
        {"ref": qid, "base": qid, "title": "t", "choice": "A"},
        {"ref": second, "base": NO_ULID, "title": "t", "choice": "B"},
    ]
    world.deliver(CH, H, P, _unchecked_submit(P, items), mentions=[P])
    world.deliver(CH, H, H, _unchecked_submit(P, [{"ref": qid, "base": qid, "title": "t", "choice": "B"}]), mentions=[P])
    result = world.run(P, "decision", "list", "--all", "--json")
    assert result.code == 0
    data = json.loads(result.out)
    got = [(x["code"], x["item"], x["ref"]) for x in data["ignored"]]
    assert ("bad_payload", None, None) in got
    assert ("unsupported_version", None, None) in got
    assert ("stale_base", 1, second) in got
    assert {d["id"]: d["status"] for d in data["decisions"]} == {qid: "answered", second: "open"}


# -- #482 PR-2: a record with no `decision` key is bad_payload; a newer version is unsupported_version --


def test_missing_decision_key_is_bad_payload_and_newer_version_stays_unsupported(world):
    # A complete ask with only the `decision` line removed: only the missing key can make it bad.
    full = dec.build_payload("ask", ask_channel=CH, asker=P, **question())
    world.deliver(CH, H, H, full.replace("decision: 1\n", "", 1))
    world.deliver(CH, H, H, "decision: 2\nop: ask\n")
    result = world.run(P, "decision", "list", "--all", "--json")
    assert result.code == 0
    data = json.loads(result.out)
    assert [(x["code"], x["item"], x["ref"]) for x in data["ignored"]] == [
        ("bad_payload", None, None),
        ("unsupported_version", None, None),
    ]


# -- C-18: the cursor and wait files are left alone ---------------------------


def test_c18_no_decision_action_moves_a_cursor_or_wait_file(world, monkeypatch):
    world.run(H, "read")  # leaves a real cursor file for H
    cursor = Cursor(position="1", at="2026-11-15T03:00:00Z", seen=[])
    FileETagCache(world.state_dir, H).put(
        "https://api.example.test/repos/owner/repo/issues/77/comments?since=1",
        ETagRecord(url="https://api.example.test/repos/owner/repo/issues/77/comments?since=1", cursor=cursor, etag='"v1"'),
    )
    cursors = {p: p.read_bytes() for p in world.state_dir.rglob("cursor-*.json")}
    waits = {p: p.read_bytes() for p in world.state_dir.rglob("wait-*.json")}
    assert cursors and waits and all(waits.values())
    # Not just unchanged bytes: a decision action that reads the wait file or a cursor file fails the test.
    monkeypatch.setattr(FileETagCache, "_load", lambda self: pytest.fail("a decision action read the wait file"))
    monkeypatch.setattr(CursorStore, "_load", lambda self: pytest.fail("a decision action read a cursor file"))
    qid = world.ask()
    world.run(H, "decision", "update", "--channel", CH, "--ref", qid, "--body", yaml_of(question(body="x")))
    world.run(P, "decision", "withdraw", "--channel", OTHER, "--ref", world.ask(channel=OTHER))
    answer(world, qid)
    world.run(P, "decision", "list", "--all", "--json")
    world.run(P, "decision", "snapshot", "--channel", CH)
    assert {p: p.read_bytes() for p in world.state_dir.rglob("cursor-*.json")} == cursors
    assert {p: p.read_bytes() for p in world.state_dir.rglob("wait-*.json")} == waits


# -- C-19: automatic and manual snapshots --------------------------------------


def test_c19_the_fourth_related_record_triggers_a_snapshot_that_covers_only_stable_records(world, monkeypatch):
    _build_history_for_snapshot(world, monkeypatch)
    world.at(201)
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(title="四")))
    assert result.code == 0
    assert len(result.out.split()) == 1
    snapshots = _snapshot_records(world)
    assert len(snapshots) == 1
    through = dec.parse_payload(snapshots[0])["through"]
    assert through == [m.id for m in world.stored(CH) if m.datatype == "decision"][2]


def test_c19_a_snapshot_that_cannot_be_written_only_warns(world, monkeypatch):
    _build_history_for_snapshot(world, monkeypatch)
    real = decision_cli.serialize_message

    def refuse_snapshots(message):
        if "op: snapshot" in message.content:
            raise MessageValidationError("too long (injected)")
        return real(message)

    monkeypatch.setattr(decision_cli, "serialize_message", refuse_snapshots)
    world.at(201)
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(title="四")))
    assert result.code == 0
    assert len(result.out.split()) == 1
    assert "snapshot was not written" in result.err
    assert _snapshot_records(world) == []


def test_c19_manual_snapshot_with_nothing_to_cover_prints_nothing(world):
    world.ask()
    result = world.run(P, "decision", "snapshot", "--channel", CH)
    assert result.code == 0 and result.out == ""


def test_c19_manual_snapshot_too_long_exits_1(world, monkeypatch):
    _build_history_for_snapshot(world, monkeypatch)
    real = decision_cli.serialize_message

    def refuse_snapshots(message):
        if "op: snapshot" in message.content:
            raise MessageValidationError("too long (injected)")
        return real(message)

    monkeypatch.setattr(decision_cli, "serialize_message", refuse_snapshots)
    world.at(201)
    result = world.run(P, "decision", "snapshot", "--channel", CH)
    assert result.code == 1
    assert result.out == ""


# -- C-20(e): the decision path calls only send and the listing ----------------


C20E_ALLOWED = {"send", "enumerate_conditional"}


def _c20e_argv(action: str, qid: str) -> list[str]:
    return {
        "ask": ["ask", "--channel", CH, "--body", yaml_of(question(title="新題"))],
        "update": ["update", "--channel", CH, "--ref", qid, "--body", yaml_of(question(title="改"))],
        "withdraw": ["withdraw", "--channel", CH, "--ref", qid, "--reason", "不做了"],
        "submit": ["submit", "--channel", CH, "--body", yaml_of({"items": [{"ref": qid, "base": qid, "choice": "A"}]})],
        "list": ["list", "--all", "--json"],
        "snapshot": ["snapshot", "--channel", CH],
    }[action]


@pytest.mark.parametrize("action,who", [
    ("ask", P), ("update", P), ("withdraw", P), ("submit", H), ("list", P), ("snapshot", P)])
def test_c20e_each_action_calls_only_send_and_the_log_listing(world, action, who):
    qid = world.ask()
    world.backend.calls.clear()
    result = world.run(who, "decision", *_c20e_argv(action, qid))
    assert result.code == 0, result.err
    assert set(world.backend.calls) <= C20E_ALLOWED
    assert "enumerate_conditional" in world.backend.calls


def test_c20e_automatic_snapshot_calls_only_send_and_the_log_listing(world, monkeypatch):
    # A snapshot is only written when something is old enough to cover (the 60 s stable window), so build a
    # history first; otherwise this test would never reach the snapshot's send (#477, architect L-B1).
    _build_history_for_snapshot(world, monkeypatch)
    world.at(201)
    world.backend.calls.clear()
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(title="四")))
    assert result.code == 0
    assert len(_snapshot_records(world)) == 1  # the automatic snapshot really was sent
    assert set(world.backend.calls) <= C20E_ALLOWED
    assert "enumerate_conditional" in world.backend.calls
    assert world.backend.calls.count("send") >= 2  # the ask and the snapshot


# -- C-23: list's defaults, --all, the JSON shape, and failures ---------------


def test_c23_list_defaults_to_open_all_adds_closed_and_json_has_the_documented_keys(world):
    q1, q2, q3 = world.ask(title="一"), world.ask(title="二"), world.ask(title="三")
    answer(world, q1)
    world.run(P, "decision", "withdraw", "--channel", CH, "--ref", q2, "--reason", "不要了")
    default = world.list_json()
    assert [d["id"] for d in default["decisions"]] == [q3]
    full = world.list_json("--all")
    assert {d["id"]: d["status"] for d in full["decisions"]} == {q1: "answered", q2: "withdrawn", q3: "open"}
    by_id = {d["id"]: d for d in full["decisions"]}
    assert set(by_id[q1]) == {
        "id", "channel", "asker", "answerer", "status", "version", "revisions", "title", "background", "body",
        "options", "recommended", "reason", "asked_at", "updated_at", "submitted", "withdrawn",
    }
    assert set(by_id[q1]["submitted"]) == {"record", "from", "base", "choice", "note", "submitted_at"}
    assert set(by_id[q2]["withdrawn"]) == {"record", "reason"}
    assert "draft" not in json.dumps(full)
    assert set(full) == {"decisions", "ignored"}


def test_c23_all_is_not_moved_by_a_tampered_snapshot_but_the_default_list_is(world, monkeypatch):
    _build_history_for_snapshot(world, monkeypatch)
    world.at(201)
    assert world.run(P, "decision", "snapshot", "--channel", CH).code == 0
    payload = dec.parse_payload(_snapshot_records(world)[-1])
    fake_id = uid(999)
    template = payload["items"][0]
    fake = dict(template, id=fake_id, version=fake_id, versions=[fake_id], revisions=1, title="假的")
    tampered = dict(payload, items=payload["items"] + [fake])
    world.deliver(CH, P, P, dec.build_payload("snapshot", **{k: v for k, v in tampered.items() if k not in ("decision", "op")}),
                  mentions=[])
    default = {d["id"]: d for d in world.list_json("--channel", CH)["decisions"]}
    assert default[fake_id]["title"] == "假的"
    full = {d["id"] for d in world.list_json("--all", "--channel", CH)["decisions"]}
    assert fake_id not in full


def test_c23_an_unknown_channel_is_exit_1(world):
    assert world.run(P, "decision", "list", "--channel", "nosuch").code == 1


def test_c23_a_failed_listing_is_exit_2_and_stdout_is_empty(world):
    world.backend.fail_list = True
    result = world.run(P, "decision", "list")
    assert result.code == 2 and result.out == ""


def test_c23_list_sorts_by_asked_at_then_channel_then_id(world):
    other_first = world.ask(channel=OTHER, title="先問")
    general = world.ask(title="後問")
    ids = [d["id"] for d in world.list_json("--all")["decisions"]]
    assert ids == [general, other_first]  # same backend time: channel name decides before id


def test_482_list_orders_by_instant_across_time_formats(world):
    def ask_at(stamp):
        content = dec.build_payload("ask", ask_channel=CH, asker=P, **question())
        return world.deliver(CH, P, H, content, created=stamp)

    late_fraction = ask_at("2026-01-01T10:00:00.5+00:00")
    earliest = ask_at("2026-01-01T09:30:00+09:00")
    before = ask_at("2026-01-01T01:00:00Z")
    same_instant_z = ask_at("2026-01-01T10:00:00Z")
    same_instant_offset = ask_at("2026-01-01T19:00:00+09:00")
    ids = [d["id"] for d in world.list_json("--all")["decisions"]]
    tied = sorted([same_instant_z, same_instant_offset])  # equal instants: id decides
    assert ids == [earliest, before, *tied, late_fraction]


# -- C-27: App-written submits, judged by list --------------------------------


def _app_case_items(qid):
    return {
        "valid": ([{"ref": qid, "base": qid, "title": "t", "choice": "A"}], "answered", None),
        "choice_not_in_options": ([{"ref": qid, "base": qid, "title": "t", "choice": "Z"}], "open", "bad_item"),
        "note_4001": ([{"ref": qid, "base": qid, "title": "t", "note": "字" * 4001}], "open", "bad_item"),
        "note_empty": ([{"ref": qid, "base": qid, "title": "t", "note": ""}], "open", "bad_item"),
        "neither": ([{"ref": qid, "base": qid, "title": "t"}], "open", "bad_item"),
        "choice_null_note": ([{"ref": qid, "base": qid, "title": "t", "choice": None, "note": "x"}], "answered", None),
        "duplicate_ref": ([{"ref": qid, "base": qid, "title": "t", "choice": "A"}, {"ref": qid, "base": qid, "title": "t", "choice": "B"}], "answered", "duplicate_ref"),
        "stale_base": ([{"ref": qid, "base": NO_ULID, "title": "t", "choice": "A"}], "open", "stale_base"),
        "not_answerer": ([{"ref": qid, "base": qid, "title": "t", "choice": "A"}], "open", "not_answerer"),
        "binding_mismatch": ([{"ref": qid, "base": qid, "title": "t", "choice": "A"}], "open", "binding_mismatch"),
        "closed_after_answer": ([{"ref": qid, "base": qid, "title": "t", "choice": "A"}], "answered", "closed_ref"),
    }


C27_NAMES = list(_app_case_items("0" * 26).keys())


@pytest.mark.parametrize("name", C27_NAMES)
def test_c27_a_direct_submit_is_judged_by_the_summary(world, name):
    qid = world.ask()
    if name == "closed_after_answer":
        world.deliver(CH, H, P, submit_payload([{"ref": qid, "base": qid, "title": "t", "choice": "B"}]), mentions=[P])
    items, status, code = _app_case_items(qid)[name]
    sender = P if name == "not_answerer" else H
    asker = "mypmo-pm-m2" if name == "binding_mismatch" else P
    world.deliver(CH, sender, asker, _unchecked_submit(asker, items), mentions=[asker])
    data = world.list_json("--all")
    got = {d["id"]: d["status"] for d in data["decisions"]}[qid]
    assert got == status
    if code is None:
        assert data["ignored"] == []
    else:
        assert code in [x["code"] for x in data["ignored"]]
    if name == "choice_null_note":
        submitted = {d["id"]: d for d in data["decisions"]}[qid]["submitted"]
        assert submitted["choice"] is None and submitted["note"] == "x"


def _unchecked_submit(asker, items):
    """A submit payload that build_payload would refuse; written by hand, as an App writer could."""
    return yaml.safe_dump({"decision": 1, "op": "submit", "asker": asker, "items": items},
                          allow_unicode=True, sort_keys=False)


# -- C-28: submit uses the full summary, so a snapshot cannot change the answer -


def _answered_then_snapshot(world, variant):
    q = world.ask(title="Q")
    r = world.ask(channel=OTHER, title="R")
    if variant == "answered":
        assert answer(world, q, choice="A").code == 0
    if variant == "withdrawn":
        assert world.run(P, "decision", "withdraw", "--channel", CH, "--ref", q).code == 0
    if variant == "answered_other":
        assert answer(world, q, choice="B").code == 0
    if variant == "update_to_other":
        world.at(4)
        assert world.run(P, "decision", "update", "--channel", CH, "--ref", q, "--body", yaml_of(question(answerer=H2))).code == 0
        v1 = world.list_json("--channel", CH)["decisions"][0]["version"]
        assert world.run(H2, "decision", "submit", "--channel", CH, "--body", yaml_of({"items": [
            {"ref": q, "base": v1, "choice": "A"}]})).code == 0
    world.at(5)
    world.ask(title="M")
    world.at(200)
    world.backend.deliver(world.entity(CH), Message(from_=H, to=P, channel=CH, content="一般訊息"),
                          created_at=format_backend_time(BASE.replace(minute=3, second=20)))
    world.at(200)
    assert world.run(P, "decision", "snapshot", "--channel", CH).code == 0
    return q, r


def _snapshot_used_and_closed(world):
    result = dec.fold(world.records(CH), use_snapshot=True)
    snapshot = next(m for m in world.stored(CH) if m.datatype == "decision" and "op: snapshot" in m.content)
    return result.snapshot_used.get(P), snapshot.id, result.closed


@pytest.mark.parametrize("variant", ["answered", "withdrawn", "answered_other", "update_to_other"])
def test_c28_a_rerun_after_the_snapshot_is_judged_on_the_full_history(world, monkeypatch, variant):
    q, r = _answered_then_snapshot(world, variant)
    used, snapshot_id, closed = _snapshot_used_and_closed(world)
    assert used == snapshot_id and q in closed
    q_base = {d["id"]: d["version"] for d in world.list_json("--all")["decisions"]}[q]
    result = world.run(H, "decision", "submit", "--body", yaml_of({"items": [
        {"ref": q, "base": q_base, "choice": "A"},
        {"ref": r, "base": r, "choice": "A"},
    ]}))
    if variant == "answered":
        assert result.code == 0
        assert "已送出，略過" in result.err and q in result.err
        assert len(result.out.split()) == 1
        statuses = {d["id"]: d["status"] for d in world.list_json("--all")["decisions"]}
        assert statuses[r] == "answered"
    else:
        assert result.code == 1
        assert f"{q}: closed_ref" in result.err
        assert all(d["status"] != "answered" or d["id"] != r for d in world.list_json("--all")["decisions"])


def test_c28_plan_submit_refuses_a_fold_that_is_not_complete_and_skips_only_in_the_full_fold(world):
    q, r = _answered_then_snapshot(world, "answered")
    records = world.records(CH) + world.records(OTHER)
    open_fold = dec.fold([x for x in records if x.message.channel == CH], use_snapshot=True)
    full_fold = dec.fold([x for x in records if x.message.channel == CH], use_snapshot=False)
    other_fold = dec.fold([x for x in records if x.message.channel == OTHER], use_snapshot=False)
    entries = [{"ref": q, "base": q, "choice": "A"}]
    with pytest.raises(ValueError):
        dec.plan_submit({CH: open_fold, OTHER: other_fold}, H, entries, render=lambda c, a, i: "")
    plan = dec.plan_submit({CH: full_fold, OTHER: other_fold}, H, entries + [{"ref": r, "base": r, "choice": "A"}],
                           render=lambda c, a, i: "")
    assert plan.skipped == [q]
    assert [len(items) for _c, _a, items in plan.batches] == [1]


def test_c28_a_failed_run_then_snapshot_then_rerun_sends_only_the_rest(world):
    q = world.ask(title="Q")
    r = world.ask(channel=OTHER, title="R")
    world.at(5)
    world.backend.fail_send = world.backend.sends + 2
    body = yaml_of({"items": [{"ref": q, "base": q, "choice": "A"}, {"ref": r, "base": r, "choice": "A"}]})
    first = world.run(H, "decision", "submit", "--body", body)
    assert first.code == 2 and len(first.out.split()) == 1  # Q (general) landed; R (other) did not
    world.at(10)
    world.ask(title="M")
    world.at(200)
    world.backend.deliver(world.entity(CH), Message(from_=H, to=P, channel=CH, content="一般訊息"),
                          created_at=format_backend_time(BASE + timedelta(seconds=200)))
    world.at(200)
    assert world.run(P, "decision", "snapshot", "--channel", CH).code == 0
    used, snapshot_id, closed = _snapshot_used_and_closed(world)
    assert used == snapshot_id and q in closed
    rerun = world.run(H, "decision", "submit", "--body", body)
    assert rerun.code == 0
    assert "已送出，略過" in rerun.err and q in rerun.err
    assert len(rerun.out.split()) == 1
    assert {d["id"]: d["status"] for d in world.list_json("--all")["decisions"]}[r] == "answered"


def test_c28_plan_submit_with_no_usable_snapshot_refuses_the_open_fold_and_uses_the_full_one(world):
    q = world.ask(title="Q")
    assert answer(world, q, choice="A").code == 0
    r = world.ask(title="R")
    records = world.records(CH)
    open_fold = dec.fold(records, use_snapshot=True)
    full_fold = dec.fold(records, use_snapshot=False)
    assert open_fold.snapshot_used == {P: None} and [d["id"] for d in open_fold.decisions] == [r]
    with pytest.raises(ValueError):
        dec.plan_submit({CH: open_fold}, H, [{"ref": q, "base": q, "choice": "A"}], render=lambda c, a, i: "")
    plan = dec.plan_submit({CH: full_fold}, H, [{"ref": q, "base": q, "choice": "A"}], render=lambda c, a, i: "")
    assert plan.skipped == [q] and plan.batches == []


def test_c28_plan_submit_with_only_an_unusable_snapshot_refuses_the_open_fold(world):
    q = world.ask(title="Q")
    assert answer(world, q, choice="A").code == 0
    world.at(200)
    world.deliver(CH, P, P, dec.build_payload("snapshot", asker=P, through=uid(999), covered=1, items=[], closed=[]))
    records = world.records(CH)
    open_fold = dec.fold(records, use_snapshot=True)
    assert [x["code"] for x in open_fold.ignored] == ["bad_snapshot"]
    assert open_fold.snapshot_used == {P: None}
    with pytest.raises(ValueError):
        dec.plan_submit({CH: open_fold}, H, [{"ref": q, "base": q, "choice": "A"}], render=lambda c, a, i: "")


