"""`angareion decision` write actions through the CLI with fakes (#468 PR-4, design §15 C-n).

C-22, C-4, C-6, C-7, C-9, C-11, C-12, C-13, C-14, C-25, C-29, L1 and F2.
The backend, `World` and the shared helpers live in `_decision_cli_lab.py`.
"""

import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

from datetime import datetime
import json

import pytest

from _decision_cli_lab import (
    CH,
    H,
    LITERAL_EMOJI,
    NO_ULID,
    OTHER,
    P,
    THIRD,
    Lab,
    World,
    answer,
    question,
    submit_payload,
    world,
    yaml_of,
)
from angareion import decision as dec
from angareion import decision_cli
from angareion import messages as M
from angareion.backend import FakeBackend
from angareion.cli import build_parser
from angareion.messages import Message, MessageValidationError, serialize_message

# -- C-22: the help and the six actions ----------------------------------------


def test_c22_top_level_and_decision_help_have_exactly_the_six_actions(capsys):
    parser = build_parser()
    top = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    assert set(top.choices) == {"channel", "send", "wait", "read", "log", "decision"}
    decision = top.choices["decision"]
    action = next(a for a in decision._actions if a.__class__.__name__ == "_SubParsersAction")
    assert set(action.choices) == {"ask", "update", "withdraw", "submit", "list", "snapshot"}
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["decision", "--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out.lower()
    for word in ("repo", "issue", "comment"):
        assert word not in text


def test_c22_decision_without_an_action_is_a_usage_error(world):
    result = world.run(P, "decision")
    assert result.code == 1
    assert "decision needs an action" in result.err


def test_c22_the_decision_outputs_never_name_a_backend_concept(world):
    q = world.ask(title="Q")
    texts = []
    for argv, who in [
        (["decision", "list"], P),
        (["decision", "list", "--all", "--json"], P),
        (["decision", "submit", "--body", yaml_of({"items": [{"ref": q, "base": NO_ULID, "choice": "A"}]})], H),
        (["decision", "update", "--channel", "nosuch", "--ref", q, "--body", yaml_of(question())], P),
        (["decision", "ask", "--channel", CH, "--body", yaml_of(question(body=""))], P),
        (["decision", "snapshot", "--channel", CH], P),
    ]:
        result = world.run(who, *argv)
        texts += [result.out, result.err]
    for text in texts:
        for word in ("repo", "issue", "comment"):
            assert word not in text.lower(), (word, text)


# -- C-4: each write adds exactly its records; nothing is edited or removed ----


@pytest.mark.parametrize("backend_cls", [Lab, FakeBackend], ids=["serialized", "plain"])
def test_c4_each_write_adds_its_records_and_touches_nothing_else(tmp_path, monkeypatch, backend_cls):
    world = World(tmp_path, monkeypatch, backend_cls=backend_cls)
    monkeypatch.setattr(dec, "SNAPSHOT_EVERY", 10**9)
    monkeypatch.setattr(FakeBackend, "close_channel", lambda self, entity: pytest.fail("no edit or close"))

    qid = world.ask()
    before = [(m.id, m.content) for m in world.stored(CH)]
    result = world.run(P, "decision", "update", "--channel", CH, "--ref", qid, "--body", yaml_of(question(body="改過")))
    assert result.code == 0 and len(world.stored(CH)) == len(before) + 1
    before = [(m.id, m.content) for m in world.stored(CH)]
    result = world.run(P, "decision", "withdraw", "--channel", CH, "--ref", world.ask(title="另一題"))
    assert result.code == 0
    before = [(m.id, m.content) for m in world.stored(CH)]
    current = world.list_json("--channel", CH)["decisions"][0]["version"]
    result = answer(world, qid, who=H, base=current)
    assert result.code == 0 and len(world.stored(CH)) == len(before) + 1
    now = world.stored(CH)
    assert [(m.id, m.content) for m in now[: len(before)]] == before
    assert all(m.datatype == "decision" for m in now[-1:])


# -- C-6: ask's body and the binding -------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{"ask_channel": CH}, {"asker": P}, {"op": "ask"}, {"decision": 1}, {"ref": NO_ULID}, {"unknown": 1}],
)
def test_c6_ask_body_with_binding_or_reserved_or_unknown_keys_sends_nothing(world, extra):
    before = len(world.stored(CH))
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of({**question(), **extra}))
    assert result.code == 1
    assert len(world.stored(CH)) == before


def test_c6_a_record_whose_asker_is_not_its_writer_is_binding_mismatch(world):
    payload = dec.build_payload("ask", ask_channel=CH, asker=P, **question())
    world.deliver(CH, H, H, payload)  # written by H, claims P asked
    data = world.list_json("--all")
    assert data["decisions"] == []
    assert [x["code"] for x in data["ignored"]] == ["binding_mismatch"]


# -- C-7: ask's field rules and the length limit -------------------------------

ASK_VIOLATIONS = [
    {"options": [{"key": "A", "text": "甲"}], "recommended": "A"},
    {"options": [{"key": chr(65 + i), "text": "x"} for i in range(10)], "recommended": "A"},
    {"options": [{"key": "A", "text": "甲"}, {"key": "A", "text": "乙"}], "recommended": "A"},
    {"recommended": "C"},
    {"title": "第一行\n第二行"},
    {"title": "題" * 121},
    {"body": ""},
    {"answerer": "NotAnAIName"},
    {"body": "字" * 70000},
]


@pytest.mark.parametrize("fields", ASK_VIOLATIONS, ids=[str(i) for i in range(len(ASK_VIOLATIONS))])
def test_c7_ask_field_violations_exit_1_and_send_nothing(world, fields):
    before = len(world.stored(CH))
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(**fields)))
    assert result.code == 1
    assert len(world.stored(CH)) == before


def _fixed_at(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 15, 3, 0, 0, tzinfo=tz)

    monkeypatch.setattr(M, "datetime", FixedDatetime)


@pytest.mark.parametrize("backend_cls", [Lab, FakeBackend], ids=["serialized", "plain"])
def test_c7_the_limit_counts_characters_and_exactly_65536_is_accepted(tmp_path, monkeypatch, backend_cls):
    _fixed_at(monkeypatch)
    world = World(tmp_path, monkeypatch, backend_cls=backend_cls)

    def text_length(k: int) -> int:
        payload = dec.build_payload("ask", ask_channel=CH, asker=P, **question(body="字" * k))
        message = Message(from_=P, to=H, channel=CH, content=payload, datatype="decision")
        with monkeypatch.context() as unlimited:
            unlimited.setattr(M, "MAX_MESSAGE_LENGTH", 10**9)
            return len(serialize_message(message))

    low, high = 1, 70000
    while low < high:
        middle = (low + high) // 2
        if text_length(middle) < M.MAX_MESSAGE_LENGTH:
            low = middle + 1
        else:
            high = middle
    exact = low
    assert text_length(exact) == M.MAX_MESSAGE_LENGTH

    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(body="字" * exact)))
    assert result.code == 0
    assert result.out.strip() in {m.id for m in world.stored(CH)}
    result = world.run(P, "decision", "ask", "--channel", CH, "--body", yaml_of(question(body="字" * (exact + 1))))
    assert result.code == 1


# -- C-9: only the asker may update or withdraw --------------------------------


def test_c9_update_or_withdraw_by_someone_else_is_refused_and_a_direct_write_is_not_owner(world):
    qid = world.ask()
    before = len(world.stored(CH))
    result = world.run(H, "decision", "update", "--channel", CH, "--ref", qid, "--body", yaml_of(question(body="x")))
    assert result.code == 1 and "not_owner" in result.err
    result = world.run(H, "decision", "withdraw", "--channel", CH, "--ref", qid)
    assert result.code == 1 and "not_owner" in result.err
    assert len(world.stored(CH)) == before

    world.deliver(CH, H, H, dec.build_payload("update", ref=qid, **question(body="y")), in_reply_to=qid)
    data = world.list_json()
    assert data["decisions"][0]["body"] == "內容"
    assert [x["code"] for x in data["ignored"]] == ["not_owner"]


# -- C-11: one answer per decision; withdraw and submit race ------------------


def test_c11_second_submit_for_an_answered_decision_is_closed_ref(world):
    qid = world.ask()
    assert answer(world, qid, choice="A").code == 0
    payload = submit_payload([{"ref": qid, "base": qid, "title": "t", "choice": "B"}])
    world.deliver(CH, H, P, payload, mentions=[P])
    data = world.list_json("--all")
    question_now = data["decisions"][0]
    assert question_now["status"] == "answered" and question_now["submitted"]["choice"] == "A"
    assert [x["code"] for x in data["ignored"]] == ["closed_ref"]


def test_c11_withdraw_first_then_submit_is_refused_and_a_direct_submit_is_closed_ref(world):
    qid = world.ask()
    assert world.run(P, "decision", "withdraw", "--channel", CH, "--ref", qid).code == 0
    result = answer(world, qid)
    assert result.code == 1 and "closed_ref" in result.err
    world.deliver(CH, H, P, submit_payload([{"ref": qid, "base": qid, "title": "t", "choice": "A"}]), mentions=[P])
    data = world.list_json("--all")
    assert data["decisions"][0]["status"] == "withdrawn"
    assert [x["code"] for x in data["ignored"]] == ["closed_ref"]


def test_c11_the_same_ref_twice_in_one_submit_is_refused_and_nothing_is_sent(world):
    qid = world.ask()
    before = len(world.stored(CH))
    body = yaml_of({"items": [{"ref": qid, "base": qid, "choice": "A"}, {"ref": qid, "base": qid, "choice": "B"}]})
    result = world.run(H, "decision", "submit", "--channel", CH, "--body", body)
    assert result.code == 1 and "duplicate_ref" in result.err
    assert len(world.stored(CH)) == before


# -- C-12: submit's preflight ---------------------------------------------------


def _setup_withdrawn(world, qid):
    assert world.run(P, "decision", "withdraw", "--channel", CH, "--ref", qid).code == 0


def _setup_answered_differently(world, qid):
    assert answer(world, qid, choice="B").code == 0


C12_CASES = [
    ("choice_not_in_options", lambda q: [{"ref": q, "base": q, "choice": "Z"}], "bad_item", H, None),
    ("note_4001", lambda q: [{"ref": q, "base": q, "note": "字" * 4001}], "bad_item", H, None),
    ("note_empty", lambda q: [{"ref": q, "base": q, "note": ""}], "bad_item", H, None),
    ("neither_choice_nor_note", lambda q: [{"ref": q, "base": q}], "bad_item", H, None),
    ("duplicate_ref", lambda q: [{"ref": q, "base": q, "choice": "A"}, {"ref": q, "base": q, "choice": "B"}], "duplicate_ref", H, None),
    ("stale_base", lambda q: [{"ref": q, "base": NO_ULID, "choice": "A"}], "stale_base", H, None),
    ("not_answerer", lambda q: [{"ref": q, "base": q, "choice": "A"}], "not_answerer", P, None),
    ("withdrawn", lambda q: [{"ref": q, "base": q, "choice": "A"}], "closed_ref", H, _setup_withdrawn),
    ("answered_differently", lambda q: [{"ref": q, "base": q, "choice": "A"}], "closed_ref", H, _setup_answered_differently),
]


@pytest.mark.parametrize("name,items_for,code,who,setup", C12_CASES, ids=[c[0] for c in C12_CASES])
def test_c12_submit_preflight_refusals_exit_1_and_send_nothing(world, name, items_for, code, who, setup):
    qid = world.ask()
    if setup is not None:
        setup(world, qid)
    before = len(world.stored(CH))
    result = world.run(who, "decision", "submit", "--channel", CH, "--body", yaml_of({"items": items_for(qid)}))
    assert result.code == 1
    assert f"{qid}: {code}" in result.err
    assert len(world.stored(CH)) == before


def test_c12_only_a_note_is_accepted_and_note_of_4000_is_the_limit(world):
    qid = world.ask()
    other = world.ask(title="第二題")
    result = world.run(H, "decision", "submit", "--channel", CH, "--body", yaml_of({"items": [
        {"ref": qid, "base": qid, "note": "字" * 4000},
        {"ref": other, "base": other, "note": "只寫文字"},
    ]}))
    assert result.code == 0
    assert len(result.out.split()) == 1


def test_c12_three_items_two_bad_lists_exactly_those_two(world):
    good, bad_choice, bad_note = world.ask(title="好"), world.ask(title="壞一"), world.ask(title="壞二")
    result = world.run(H, "decision", "submit", "--channel", CH, "--body", yaml_of({"items": [
        {"ref": good, "base": good, "choice": "A"},
        {"ref": bad_choice, "base": bad_choice, "choice": "Z"},
        {"ref": bad_note, "base": bad_note, "note": ""},
    ]}))
    assert result.code == 1
    assert f"{bad_choice}: bad_item" in result.err and f"{bad_note}: bad_item" in result.err
    assert f"{good}:" not in result.err


@pytest.mark.parametrize(
    "body",
    [
        {"items": [{"ref": NO_ULID, "choice": "A"}]},
        {"items": []},
        {"items": [{"ref": NO_ULID, "base": NO_ULID, "title": "抄"}]},
        {"items": [{"ref": NO_ULID, "base": NO_ULID, "choice": "A"}], "asker": P},
    ],
    ids=["no_base", "empty_items", "title_in_item", "asker_in_body"],
)
def test_c12_usage_errors_exit_1(world, body):
    before = len(world.stored(CH))
    result = world.run(H, "decision", "submit", "--channel", CH, "--body", yaml_of(body))
    assert result.code == 1
    assert len(world.stored(CH)) == before


# -- C-13: two channels, one record per (channel, asker), in order -------------


def test_c13_two_channels_give_two_records_in_channel_order_and_q4_stays_open(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    q1, q2 = world.ask(title="一"), world.ask(title="二")
    q3 = world.ask(channel=OTHER, title="三")
    q4 = world.ask(title="四")
    body = yaml_of({"items": [
        {"ref": q1, "base": q1, "choice": "A"},
        {"ref": q2, "base": q2, "choice": "B"},
        {"ref": q3, "base": q3, "choice": "A"},
    ]})
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 0
    ids = result.out.split()
    assert len(ids) == 2
    general, other = world.stored(CH), world.stored(OTHER)
    sent = {m.id: m for m in general + other if m.id in ids}
    assert [sent[i].channel for i in ids] == [CH, OTHER]
    for mid in ids:
        assert sent[mid].datatype == "decision"
        assert sent[mid].to == P and sent[mid].mentions == [P]
    data = world.list_json()
    assert [d["id"] for d in data["decisions"]] == [q4]


# -- C-14: a failed record stops the run; the rerun sends only what is left ----


def _three_channel_setup(world):
    q1 = world.ask(title="一")
    q2 = world.ask(channel=OTHER, title="二")
    q3 = world.ask(channel=THIRD, title="三")
    body = yaml_of({"items": [
        {"ref": q1, "base": q1, "choice": "A"},
        {"ref": q2, "base": q2, "choice": "A"},
        {"ref": q3, "base": q3, "choice": "A"},
    ]})
    return (q1, q2, q3), body


def test_c14_a_failed_second_record_exits_2_and_rerun_sends_only_the_rest(world):
    world = world
    (q1, q2, q3), body = _three_channel_setup(world)
    world.backend.fail_send = world.backend.sends + 2
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 2
    assert len(result.out.split()) == 1
    assert "record 2/3 failed" in result.err
    assert len(world.stored(THIRD)) == 1  # only the question itself: record 3 was never sent

    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 0
    assert "已送出，略過" in result.err and q1 in result.err
    assert len(result.out.split()) == 2
    assert {d["id"]: d["status"] for d in world.list_json("--all")["decisions"]} == {q1: "answered", q2: "answered", q3: "answered"}


def test_c14_a_lost_response_is_skipped_on_rerun_when_the_listing_sees_it(world):
    world = world
    (q1, q2, q3), body = _three_channel_setup(world)
    world.backend.fail_send, world.backend.fail_mode = world.backend.sends + 2, "after"
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 2
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 0
    assert "已送出，略過" in result.err
    statuses = {d["id"]: d["status"] for d in world.list_json("--all")["decisions"]}
    assert statuses == {q1: "answered", q2: "answered", q3: "answered"}


def test_c14_a_record_the_listing_cannot_see_is_resent_and_its_items_are_closed_ref(world):
    world = world
    (q1, q2, q3), body = _three_channel_setup(world)
    world.backend.fail_send, world.backend.fail_mode = world.backend.sends + 2, "after"
    assert world.run(H, "decision", "submit", "--body", body).code == 2
    landed = [m.id for m in world.stored(OTHER) if m.datatype == "decision" and m.id != q2]
    world.backend.hidden.update(landed)
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 0
    world.backend.hidden.clear()
    data = world.list_json("--all")
    assert {d["id"]: d["status"] for d in data["decisions"]}[q2] == "answered"
    assert [x["code"] for x in data["ignored"]] == ["closed_ref"]


def test_c14_when_everything_is_already_sent_it_exits_0_with_empty_stdout(world):
    world = world
    (q1, q2, q3), body = _three_channel_setup(world)
    assert world.run(H, "decision", "submit", "--body", body).code == 0
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 0
    assert result.out == ""
    assert "Nothing to send" in result.err


# -- C-25: submit capacity, cross-group order, preflight and resume -----------


def _hundred_questions(world):
    qids = []
    for n in range(100):
        payload = dec.build_payload("ask", ask_channel=CH, asker=P, **question(title=f"題{n}"))
        qids.append(world.deliver(CH, P, H, payload))
    other = world.deliver(OTHER, P, H, dec.build_payload("ask", ask_channel=OTHER, asker=P, **question(title="外")))
    return qids, other


def _big_items(qids):
    return [{"ref": q, "base": q, "choice": "A", "note": "字" * 1000} for q in qids]


def test_c25_a_large_group_splits_and_the_groups_go_in_order(world):
    qids, other = _hundred_questions(world)
    body = yaml_of({"items": _big_items(qids) + [{"ref": other, "base": other, "choice": "A"}]})
    result = world.run(H, "decision", "submit", "--body", body)
    assert result.code == 0
    sent = result.out.split()
    assert len(sent) == 3
    records = {m.id: m for m in world.stored(CH) + world.stored(OTHER) if m.id in sent}
    assert [records[i].channel for i in sent] == [CH, CH, OTHER]
    for mid in sent:
        assert len(serialize_message(records[mid])) <= M.MAX_MESSAGE_LENGTH
        assert records[mid].to == P and records[mid].mentions == [P]
    refs = []
    for mid in sent[:2]:
        refs += [i["ref"] for i in dec.parse_payload(records[mid])["items"]]
    assert sorted(refs) == sorted(qids) and len(set(refs)) == 100
    data = world.list_json("--all")
    assert all(d["status"] == "answered" for d in data["decisions"] if d["id"] in qids + [other])


def test_c25_a_preflight_failure_in_one_group_sends_nothing(world, monkeypatch):
    qids, other = _hundred_questions(world)
    real = decision_cli._submit_render

    def render_fails_for_other(config, at):
        inner = real(config, at)

        def render(channel, asker, items):
            if channel == OTHER:
                raise MessageValidationError("injected")
            return inner(channel, asker, items)

        return render

    monkeypatch.setattr(decision_cli, "_submit_render", render_fails_for_other)
    before = len(world.stored(CH)) + len(world.stored(OTHER))
    result = world.run(H, "decision", "submit", "--body", yaml_of({"items": _big_items(qids) + [
        {"ref": other, "base": other, "choice": "A"}]}))
    assert result.code == 1
    assert len(world.stored(CH)) + len(world.stored(OTHER)) == before


def test_c25_a_failed_second_record_then_a_rerun_sends_the_rest(world):
    qids, other = _hundred_questions(world)
    body = yaml_of({"items": _big_items(qids) + [{"ref": other, "base": other, "choice": "A"}]})
    world.backend.fail_send = world.backend.sends + 2
    first = world.run(H, "decision", "submit", "--body", body)
    assert first.code == 2
    assert len(first.out.split()) == 1
    rerun = world.run(H, "decision", "submit", "--body", body)
    assert rerun.code == 0
    assert len(rerun.out.split()) == 2
    assert all(d["status"] == "answered" for d in world.list_json("--all")["decisions"] if d["id"] in qids + [other])


# -- C-29: the rerun rules: same answer skipped, anything else refused --------


def _answered_base(world, base_kind):
    q = world.ask(title="Q")
    if base_kind == "update_first":
        assert world.run(P, "decision", "update", "--channel", CH, "--ref", q, "--body", yaml_of(question(title="改"))).code == 0
        version = world.list_json()["decisions"][0]["version"]
        assert answer(world, q, choice="A", base=version).code == 0
    else:
        assert answer(world, q, choice="A").code == 0
    return q


def test_c29_a_different_base_is_closed_ref_and_nothing_is_sent(world):
    q = _answered_base(world, "update_first")
    before = len(world.stored(CH))
    result = answer(world, q, choice="A", base=q)
    assert result.code == 1 and f"{q}: closed_ref" in result.err
    assert len(world.stored(CH)) == before


def test_c29_a_different_base_in_the_other_direction_is_closed_ref(world):
    q = _answered_base(world, "plain")
    result = answer(world, q, choice="A", base=NO_ULID)
    assert result.code == 1 and f"{q}: closed_ref" in result.err


def test_c29_missing_note_and_null_note_are_the_same_so_both_are_skipped(world):
    q = world.ask()
    assert answer(world, q, choice="A").code == 0
    for item in ({"ref": q, "base": q, "choice": "A", "note": None}, {"ref": q, "base": q, "choice": "A"}):
        result = world.run(H, "decision", "submit", "--body", yaml_of({"items": [item]}))
        assert result.code == 0 and result.out == ""
        assert "已送出，略過" in result.err


def test_c29_a_note_that_differs_from_the_accepted_one_is_closed_ref(world):
    q = world.ask()
    assert answer(world, q, choice=None, note="x").code == 0
    result = answer(world, q, choice=None, note="y")
    assert result.code == 1 and f"{q}: closed_ref" in result.err


def test_c29_the_same_answer_twice_in_one_run_is_duplicate_ref_and_nothing_is_sent(world):
    q = world.ask()
    assert answer(world, q, choice="A").code == 0
    before = len(world.stored(CH))
    result = world.run(H, "decision", "submit", "--body", yaml_of({"items": [
        {"ref": q, "base": q, "choice": "A"}, {"ref": q, "base": q, "choice": "A"}]}))
    assert result.code == 1 and f"{q}: duplicate_ref" in result.err
    assert len(world.stored(CH)) == before


def test_c29_a_mixed_run_with_a_repeat_sends_nothing(world):
    q = world.ask(title="Q")
    r = world.ask(title="R")
    assert answer(world, q, choice="A").code == 0
    before = len(world.stored(CH))
    result = world.run(H, "decision", "submit", "--body", yaml_of({"items": [
        {"ref": q, "base": q, "choice": "A"}, {"ref": r, "base": r, "choice": "A"}, {"ref": r, "base": r, "choice": "A"}]}))
    assert result.code == 1 and f"{r}: duplicate_ref" in result.err
    assert len(world.stored(CH)) == before


def test_c29_a_sent_item_is_skipped_and_the_new_one_is_sent(world):
    q = world.ask(title="Q")
    r = world.ask(title="R")
    assert answer(world, q, choice="A").code == 0
    result = world.run(H, "decision", "submit", "--body", yaml_of({"items": [
        {"ref": q, "base": q, "choice": "A"}, {"ref": r, "base": r, "choice": "A"}]}))
    assert result.code == 0
    assert "已送出，略過" in result.err and len(result.out.split()) == 1


# -- L1: the thread reference and the stdin body are part of the contract -------


def test_thread_and_stdin(world):
    asked = world.run(P, "decision", "ask", "--channel", CH, "--body", "-", stdin=yaml_of(question(title="從 stdin")))
    assert asked.code == 0
    qid = asked.out.strip()
    assert dec.parse_payload(world.stored(CH)[-1])["title"] == "從 stdin"

    updated = world.run(P, "decision", "update", "--channel", CH, "--ref", qid, "--body", yaml_of(question(title="改過")))
    assert updated.code == 0
    assert world.stored(CH)[-1].in_reply_to == qid
    assert dec.parse_payload(world.stored(CH)[-1])["title"] == "改過"

    withdrawn = world.run(P, "decision", "withdraw", "--channel", CH, "--ref", qid, "--reason", "done")
    assert withdrawn.code == 0
    assert world.stored(CH)[-1].in_reply_to == qid


# F2: withdraw --reason is a write too, so it is checked the same way (spec: every write action's --reason).
UNWRITABLE_REASON = {
    "lone_surrogate": "壞\ud800標題",
    "surrogate_pair": "\ud83d\ude00 不做了",
}


@pytest.mark.parametrize("name", list(UNWRITABLE_REASON))
def test_f2_withdraw_refuses_an_unwritable_reason(world, name):
    qid = world.ask()
    before = len(world.stored(CH))
    result = world.run(P, "decision", "withdraw", "--channel", CH, "--ref", qid, "--reason", UNWRITABLE_REASON[name])
    assert result.code == 1, (result.code, result.out, result.err)
    assert result.out == ""
    assert "UTF-8" in result.err
    assert len(world.stored(CH)) == before


def test_f2_a_literal_emoji_withdraw_reason_is_written_and_read_back(world):
    qid = world.ask()
    reason = "不做了 " + LITERAL_EMOJI
    result = world.run(P, "decision", "withdraw", "--channel", CH, "--ref", qid, "--reason", reason)
    assert result.code == 0, result.err
    data = json.loads(world.run(P, "decision", "list", "--all", "--json").out)
    assert data["ignored"] == []
    assert [d["withdrawn"]["reason"] for d in data["decisions"] if d["id"] == qid] == [reason]


