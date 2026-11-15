"""`angareion decision <action>` through the CLI with fakes (#468 PR-4, design §15 C-n).

The backend is a FakeBackend. `Lab` is the one used by most tests: its `send`
goes through `serialize_message` and `parse_message`, the path the real writer
takes, so a message the reader cannot read back fails here (C-4, C-13). The
plain FakeBackend is also run where the contract says nothing about the path.
"""

import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

import contextlib
from datetime import date, datetime, timedelta, timezone
import io
import json
import sys

import pytest
import yaml

from _angareion_fakes import FakeGit, backends_json, channels_json
from angareion import decision as dec
from angareion import decision_cli
from angareion import messages as M
from angareion.backend import BackendError, Cursor, ETagRecord, FakeBackend, format_backend_time
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import build_parser, main
from angareion.config import Config, save_config
from angareion.messages import Message, MessageValidationError, parse_message, serialize_message
from angareion.registry import FakeRegistryClient
from angareion.state import CursorStore, FileETagCache

TOKEN = "gh" + "p_FAKE_DECISION_CLI_TOKEN_123"
P = "mypmo-pm-m1"  # the asker
H = "mylinuxpool-impl-w1-1"  # the answerer
H2 = "mylinuxpool-impl-w1-2"  # a second answerer, used after an update
CHANNEL_NUMBERS = {"general": 10, "other": 7, "third": 6}
BASE = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
NO_ULID = "00000000000000000000000001"
CH, OTHER, THIRD = "general", "other", "third"


def uid(n: int) -> str:
    return f"{n:026d}"


def yaml_of(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def question(**kw) -> dict:
    base = dict(
        answerer=H,
        title="要不要先合 PR-1",
        background="背景",
        body="內容",
        options=[{"key": "A", "text": "先合"}, {"key": "B", "text": "等確認"}],
        recommended="A",
        reason="理由",
    )
    base.update(kw)
    return base


def submit_payload(items, asker=P) -> str:
    return dec.build_payload("submit", asker=asker, items=items)


class Lab(FakeBackend):
    """A FakeBackend that writes like the real one and can be told to fail or hide records.

    - `send` runs the message through serialize_message and parse_message before it
      is stored (the reader sees what the writer wrote, C-4 and C-13).
    - `fail_send` = n with `fail_mode` "before" fails the n-th send without storing it;
      "after" stores it and then fails (the response was lost).
    - `hidden` is a set of message ids the listing does not return (C-14).
    - `fail_list` makes the listing fail (C-23).
    - `calls` records the backend methods the code under test calls (C-20(e)).
    """

    _TRACKED = frozenset(
        {"create_channel", "verify_channel", "close_channel", "send", "check", "read", "enumerate_conditional", "enumerate"}
    )

    def __init__(self, resolver, *, clock):
        super().__init__(resolver, clock=clock)
        self.calls: list[str] = []
        self.sends = 0
        self.fail_send = None
        self.fail_mode = "before"
        self.hidden: set[str] = set()
        self.fail_list = False

    def __getattribute__(self, name):
        if name in Lab._TRACKED:
            object.__getattribute__(self, "calls").append(name)
        return object.__getattribute__(self, name)

    def send(self, entity, message):
        self.sends += 1
        stored = parse_message(serialize_message(message), strict=True)
        failing = self.sends == self.fail_send
        if failing and self.fail_mode == "before":
            raise BackendError("injected: the send failed before it landed")
        message_id = super().send(entity, stored)
        if failing:
            raise BackendError("injected: the send landed but the response was lost")
        return message_id

    def enumerate_conditional(self, entity, *, since=None, etag=None):
        if self.fail_list:
            raise BackendError("injected: listing failed")
        items, new_etag, not_modified = super().enumerate_conditional(entity, since=since, etag=etag)
        visible = [item for item in items if item[2] is None or item[2].id not in self.hidden]
        return visible, new_etag, not_modified


class World:
    def __init__(self, tmp_path, monkeypatch, backend_cls=Lab):
        self.monkeypatch = monkeypatch
        self.now = {"t": BASE}
        config_file = tmp_path / "config.json"
        self.state_dir = tmp_path / "state"
        clone = tmp_path / "clone"
        clone.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        config = Config(
            identity=P,
            config_dir=clone,
            registry_repo="owner/MyAiStorage",
            registry_issue=77,
            token_file=token_file,
            state_dir=self.state_dir,
            config_file=config_file,
        )
        save_config(config, config_file)
        monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
        monkeypatch.setenv("ANGAREION_STATE_DIR", str(self.state_dir))
        monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))

        self.git = FakeGit(
            {
                f"origin/main:{CHANNELS_PATH}": channels_json(
                    {name: ("github-main", number) for name, number in CHANNEL_NUMBERS.items()}
                ),
                f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
            },
            fetch_head_mtime=1000.0,
        )
        self.store = ChannelStore(
            clone,
            git=self.git,
            interval=0,
            warn=lambda message: None,
            now=lambda: 1000.0,
            clock=lambda: 0.0,
            sleeper=lambda seconds: None,
            fetch_head_mtime=lambda: self.git.fetch_head_mtime,
        )
        self.backend = backend_cls(self.store, clock=lambda: format_backend_time(self.now["t"]))
        for name, number in CHANNEL_NUMBERS.items():
            self.backend.add_entity(name, number=number)
        self.registry = FakeRegistryClient()

    def at(self, seconds: int) -> None:
        self.now["t"] = BASE + timedelta(seconds=seconds)

    def run(self, who: str, *argv: str, stdin: str | None = None):
        self.monkeypatch.setenv("ANGAREION_IDENTITY", who)
        if stdin is not None:
            self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                list(argv),
                backend=self.backend,
                store=self.store,
                registry=self.registry,
                sleeper=lambda seconds: None,
                clock=lambda: 0.0,
            )
        return Result(code, out.getvalue(), err.getvalue())

    def ask(self, who: str = P, channel: str = CH, **fields) -> str:
        result = self.run(who, "decision", "ask", "--channel", channel, "--body", yaml_of(question(**fields)))
        assert result.code == 0, result.err
        return result.out.strip()

    def entity(self, channel: str):
        return self.store.entity_for(channel)

    def stored(self, channel: str) -> list[Message]:
        items, _etag, _not_modified = self.backend.enumerate_conditional(self.entity(channel))
        return [parsed for _cid, _at, parsed in items if parsed is not None]

    def records(self, channel: str) -> list[dec.Record]:
        items, _etag, _not_modified = self.backend.enumerate_conditional(self.entity(channel))
        return [
            dec.Record(parsed, cid, created)
            for cid, created, parsed in items
            if parsed is not None
        ]

    def deliver(self, channel: str, sender: str, to, content: str, *, mentions=(), in_reply_to=None, created=None) -> str:
        message = Message(
            from_=sender,
            to=to,
            channel=channel,
            content=content,
            datatype="decision",
            mentions=list(mentions),
            in_reply_to=in_reply_to,
        )
        self.backend.deliver(self.entity(channel), message, created_at=created)
        return message.id

    def list_json(self, *extra: str, who: str = P) -> dict:
        result = self.run(who, "decision", "list", "--json", *extra)
        assert result.code == 0, result.err
        return json.loads(result.out)


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def answer(world, qid, choice="A", base=None, who=H, channel=CH, note=None):
    item = {"ref": qid, "base": base or qid, "choice": choice}
    if note is not None:
        item["note"] = note
    return world.run(who, "decision", "submit", "--channel", channel, "--body", yaml_of({"items": [item]}))


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


def _snapshot_records(world):
    return [m for m in world.stored(CH) if m.datatype == "decision" and "op: snapshot" in m.content]


def _build_history_for_snapshot(world, monkeypatch):
    """Three asks, one unrelated message 200 s later, then a fourth ask: the snapshot's `through` is the third ask."""
    monkeypatch.setattr(dec, "SNAPSHOT_EVERY", 3)
    world.at(0)
    world.ask(title="一")
    world.at(1)
    world.ask(title="二")
    world.at(2)
    world.ask(title="三")
    world.backend.deliver(world.entity(CH), Message(from_=H, to=P, channel=CH, content="一般訊息"),
                          created_at=format_backend_time(BASE.replace(second=0).replace(minute=3, second=20)))


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
    monkeypatch.setattr(dec, "SNAPSHOT_EVERY", 1)
    world.backend.calls.clear()
    world.ask()
    assert set(world.backend.calls) <= C20E_ALLOWED
    assert "enumerate_conditional" in world.backend.calls


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


def test_c23_list_sorts_by_asked_at_then_channel_then_id(world):
    other_first = world.ask(channel=OTHER, title="先問")
    general = world.ask(title="後問")
    ids = [d["id"] for d in world.list_json("--all")["decisions"]]
    assert ids == [general, other_first]  # same backend time: channel name decides before id


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
LITERAL_EMOJI = "😀 要不要先合"


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
