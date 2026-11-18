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



LITERAL_EMOJI = "😀 要不要先合"
