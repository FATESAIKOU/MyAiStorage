"""Unit tests for the message `datatype` on the read side, the self-silent rule, and the protocol split (#468 PR-1), and the writer side (#468 PR-2).

C-1, C-2, C-3, C-5, C-20(a)～(c). The CLI cases (C-3 through `read`, C-21) are in
test_angareion_cli.py next to the other CLI tests.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import inspect
from pathlib import Path

import pytest
import yaml

from angareion import backend, messages
from angareion.backend import (
    ChannelBackend,
    EnumerableBackend,
    FakeBackend,
    mention_matches,
    recipient_matches,
)
from angareion.cli import _format_message
from angareion.github import GitHubIssuesBackend
from angareion.log_cache import LOG_CACHE_VERSION
from angareion.messages import (
    DEFAULT_DATATYPE,
    Message,
    MessageValidationError,
    message_to_dict,
    parse_message,
    serialize_message,
    split_front_matter,
)
from _angareion_fakes import StaticResolver, entity

ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
GENERAL = entity(channel="general", issue=10)


def _text(datatype_line: str | None) -> str:
    """A plain message as text, with an optional `datatype:` line after `angareion:`."""
    lines = ["---", "angareion: 1"]
    if datatype_line is not None:
        lines.append(datatype_line)
    lines += [
        "id: 01JB0000000000000000000101",
        "at: '2026-11-15T03:00:00+00:00'",
        f"from: {PM}",
        f"to: {ME}",
        "channel: general",
        "urgency: 5",
        "attachments: []",
        "---",
    ]
    return "\n".join(lines) + "\nhello\n"


# -- C-1 ----------------------------------------------------------------------

def test_c1_text_without_datatype_reads_as_message():
    msg = parse_message(_text(None), strict=True)
    assert msg.datatype == "message"


def test_c1_message_to_dict_carries_datatype_message():
    msg = parse_message(_text(None), strict=True)
    assert message_to_dict(msg)["datatype"] == "message"


def test_c1_default_message_has_datatype_message():
    assert Message(from_=PM, to=ME, channel="general").datatype == DEFAULT_DATATYPE == "message"


# -- C-2 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value_line", "want"),
    [
        ("datatype: 5", "message"),            # not a string
        ("datatype: [a]", "message"),          # not a string
        ("datatype: null", "message"),         # not a string
        ("datatype: Decision", "message"),     # upper case is outside the grammar
        ("datatype: a b", "message"),          # a space is outside the grammar
        ("datatype: " + "a" * 33, "message"),  # 33 characters: one too long
        ("datatype: " + "a" * 32, "a" * 32),   # 32 characters: the longest legal value
        ("datatype: poll", "poll"),            # legal but not known here: kept as it is
        ("datatype: decision", "decision"),
    ],
)
def test_c2_datatype_reads_wide(value_line, want):
    msg = parse_message(_text(value_line), strict=True)
    assert msg is not None
    assert msg.datatype == want
    assert msg.content == "hello\n"


def test_c2_a_bad_datatype_does_not_drop_the_message():
    assert parse_message(_text("datatype: Decision")) is not None


# -- C-3 ----------------------------------------------------------------------

def _decision(from_: str, to: str | None) -> Message:
    return Message(from_=from_, to=to, channel="general", content="x", datatype="decision")


def test_c3_own_decision_is_not_delivered_to_its_writer():
    msg = _decision(ME, ME)
    assert recipient_matches(msg, ME) is False
    assert mention_matches(msg, ME) is False


def test_c3_own_plain_message_to_self_is_unchanged():
    msg = Message(from_=ME, to=ME, channel="general", content="x")
    assert recipient_matches(msg, ME) is True
    assert mention_matches(msg, ME) is True


def test_c3_decision_from_someone_else_is_delivered():
    msg = _decision(PM, ME)
    assert recipient_matches(msg, ME) is True
    assert mention_matches(msg, ME) is True


def test_c3_the_table_is_read_when_called(monkeypatch):
    monkeypatch.setattr(messages, "SELF_SILENT_DATATYPES", frozenset({"decision", "poll"}))
    msg = Message(from_=ME, to=ME, channel="general", content="x", datatype="poll")
    assert recipient_matches(msg, ME) is False
    assert mention_matches(msg, ME) is False


def test_c3_backend_source_does_not_name_a_datatype():
    source = Path(backend.__file__).read_text(encoding="utf-8")
    assert "decision" not in source


def test_c3_default_table_is_decision_only():
    assert messages.SELF_SILENT_DATATYPES == frozenset({"decision"})


# -- C-20 (a)～(c) ------------------------------------------------------------

def _public_methods(cls) -> set[str]:
    return {name for name, value in vars(cls).items() if inspect.isfunction(value) and not name.startswith("_")}


def test_c20a_channel_backend_still_has_the_six_operations():
    assert _public_methods(ChannelBackend) == {"create_channel", "verify_channel", "close_channel", "send", "check", "read"}


def test_c20b_enumerable_backend_is_only_enumerate_conditional():
    assert _public_methods(EnumerableBackend) == {"enumerate_conditional"}


def test_c20c_fake_and_github_backends_are_enumerable():
    assert isinstance(FakeBackend(None), EnumerableBackend)
    github = GitHubIssuesBackend(StaticResolver([GENERAL]), token="fake_token", transport=None)
    assert isinstance(github, EnumerableBackend)
    assert isinstance(github, ChannelBackend)


def test_c20c_a_plain_object_is_not_enumerable():
    assert not isinstance(object(), EnumerableBackend)


# -- the rest of the read side -------------------------------------------------

def test_format_shows_a_datatype_line_only_when_it_is_not_message():
    plain = _format_message(Message(from_=PM, to=ME, channel="general", content="hi"))
    assert "datatype:" not in plain
    other = _format_message(Message(from_=PM, to=ME, channel="general", content="hi", datatype="poll"))
    assert "  datatype: poll\n" in other


def test_validate_refuses_a_datatype_outside_the_grammar():
    msg = Message(from_=PM, to=ME, channel="general", content="x", datatype="Bad")
    with pytest.raises(MessageValidationError):
        msg.validate()


def test_serialize_refuses_a_datatype_outside_the_grammar():
    # The read-back check (serialize_message) is the last line: a datatype the reader would
    # not keep never leaves. validate() refuses it first, with the grammar in the message.
    msg = Message(from_=PM, to=ME, channel="general", content="x", datatype="Decision")
    with pytest.raises(MessageValidationError, match="datatype"):
        serialize_message(msg)


# -- C-5 (PR-2): the writer writes `datatype`, in a fixed key order ------------

C5_ID = "01JB0000000000000000000501"
C5_AT = "2026-11-15T03:00:00+00:00"
FULL_ORDER = ["angareion", "datatype", "id", "at", "from", "to", "mentions", "in_reply_to", "channel", "urgency", "attachments"]
BROADCAST_ORDER = ["angareion", "datatype", "id", "at", "from", "channel", "urgency", "attachments"]


def _front_matter_keys(text: str) -> list[str]:
    """The front matter keys in the order a YAML reader sees them (not the line numbers)."""
    raw_yaml, _ = split_front_matter(text)
    return list(yaml.safe_load(raw_yaml).keys())


def test_c5_send_writes_datatype_message_after_angareion():
    text = serialize_message(Message(from_=PM, to=ME, channel="general", content="hi", id=C5_ID, at=C5_AT))
    data = yaml.safe_load(split_front_matter(text)[0])
    assert data["datatype"] == "message"
    assert "datatype: message\n" in text


def test_c5_key_order_with_every_optional_key_present():
    msg = Message(
        from_=PM,
        to=ME,
        channel="general",
        content="hi",
        id=C5_ID,
        at=C5_AT,
        mentions=[ME],
        in_reply_to=C5_ID,
        attachments=[{"name": "a.txt", "inline": "x"}],
    )
    assert _front_matter_keys(serialize_message(msg)) == FULL_ORDER


def test_c5_key_order_for_a_broadcast_without_to_or_mentions_or_in_reply_to():
    msg = Message(from_=PM, to=None, channel="general", content="hi", id=C5_ID, at=C5_AT)
    assert _front_matter_keys(serialize_message(msg)) == BROADCAST_ORDER


def test_c5_key_order_with_mentions_but_no_to():
    msg = Message(from_=PM, to=None, channel="general", content="hi", id=C5_ID, at=C5_AT, mentions=[ME])
    assert _front_matter_keys(serialize_message(msg)) == BROADCAST_ORDER[:5] + ["mentions", "channel", "urgency", "attachments"]


def test_c5_key_order_with_in_reply_to_only():
    msg = Message(from_=PM, to=ME, channel="general", content="hi", id=C5_ID, at=C5_AT, in_reply_to=C5_ID)
    assert _front_matter_keys(serialize_message(msg)) == [k for k in FULL_ORDER if k != "mentions"]


# -- decision datatype: the writer writes it, and it reads back (hand-built Message, no CLI) --

def test_decision_datatype_serializes_and_reads_back():
    msg = Message(from_=PM, to=ME, channel="general", content="x", id=C5_ID, at=C5_AT, datatype="decision")
    text = serialize_message(msg)
    assert "datatype: decision\n" in text
    back = parse_message(text, strict=True)
    assert back is not None
    assert back.datatype == "decision"
    assert message_to_dict(back) == message_to_dict(msg)


def test_serialize_refuses_a_decision_that_reads_back_as_message(monkeypatch):
    # The read-back check covers `datatype`: a reader that loses it must stop the send.
    msg = Message(from_=PM, to=ME, channel="general", content="x", id=C5_ID, at=C5_AT, datatype="decision")
    real = messages.parse_message

    def loses_datatype(text, *, strict=False):
        back = real(text, strict=strict)
        back.datatype = "message"
        return back

    monkeypatch.setattr(messages, "parse_message", loses_datatype)
    with pytest.raises(MessageValidationError, match="different fields"):
        serialize_message(msg)


def test_log_cache_version_is_2():
    assert LOG_CACHE_VERSION == 2
