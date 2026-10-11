"""Unit tests for the ChannelBackend protocol, targets/cursors and FakeBackend (D2)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from angareion.backend import (
    AuthenticationError,
    BackendError,
    ChannelBackend,
    ChannelNotFoundError,
    Cursor,
    FakeBackend,
    ReadResult,
    Target,
    address_matches_identity,
    is_message_after_cursor,
)
from angareion.messages import Message


def _make_msg(
    channel: str = "test-topic",
    content: str = "hello",
    msg_id: str = "01MSG0000000000000000000001",
    at: str = "2026-10-10T12:00:00+00:00",
    to: dict | None = None,
) -> Message:
    return Message(
        from_={"repo": "owner-org/team", "role": "PM"},
        to=to or {"repo": "owner-org/worker", "role": "worker"},
        channel=channel,
        content=content,
        id=msg_id,
        at=at,
    )


def test_target_requires_exactly_one_destination():
    with pytest.raises(ValueError):
        Target()
    with pytest.raises(ValueError):
        Target(channel="a", recipient={"repo": "r", "role": "x"})
    assert Target(channel="a").channel == "a"
    assert Target(recipient={"repo": "r", "role": "x"}).recipient == {"repo": "r", "role": "x"}


def test_target_keys():
    assert Target(channel="general").key() == "channel:general"
    assert Target(recipient={"repo": "owner-org/team", "role": "PM"}).key() == "to:owner-org_team__PM"


def test_is_message_after_cursor_boundary():
    msg = _make_msg(at="2026-10-10T12:00:00+00:00", msg_id="01BBBB")

    # No cursor: everything is new.
    assert is_message_after_cursor(msg, Cursor())

    # Time-only cursor: strictly after that second.
    assert is_message_after_cursor(msg, Cursor(at="2026-10-10T11:59:59+00:00"))
    assert not is_message_after_cursor(msg, Cursor(at="2026-10-10T12:00:00+00:00"))
    assert not is_message_after_cursor(msg, Cursor(at="2026-10-10T12:00:01+00:00"))

    # (at, id) cursor: same second is decided by the ULID.
    assert is_message_after_cursor(msg, Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA"))
    assert not is_message_after_cursor(msg, Cursor(at="2026-10-10T12:00:00+00:00", id="01BBBB"))
    assert not is_message_after_cursor(msg, Cursor(at="2026-10-10T12:00:00+00:00", id="01CCCC"))


def test_address_matches_identity():
    me = {"repo": "owner-org/worker", "role": "team-a"}
    assert address_matches_identity({"repo": "owner-org/worker", "role": "team-a", "name": "X"}, me)
    assert not address_matches_identity({"repo": "owner-org/worker", "role": "team-b"}, me)
    assert not address_matches_identity({"group": "all-workers"}, me)
    assert not address_matches_identity("not a dict", me)


def test_fake_backend_conforms_to_protocol():
    assert isinstance(FakeBackend(), ChannelBackend)


def test_fake_create_channel_idempotent_and_members():
    backend = FakeBackend()
    members = [{"repo": "owner-org/team", "role": "PM"}]
    channel = backend.create_channel("topic-1", members)
    assert channel.name == "topic-1"
    assert channel.number == 1
    assert channel.members == members

    again = backend.create_channel("topic-1", [])
    assert again == channel

    second = backend.create_channel("topic-2", [])
    assert second.number == 2


def test_fake_send_requires_channel_and_matching_name():
    backend = FakeBackend()
    with pytest.raises(ChannelNotFoundError):
        backend.send("unknown-topic", _make_msg(channel="unknown-topic"))

    backend.create_channel("topic-1", [])
    with pytest.raises(ValueError, match="does not match target channel"):
        backend.send("topic-1", _make_msg(channel="topic-2"))

    msg = _make_msg(channel="topic-1", content="first")
    assert backend.send("topic-1", msg) == msg.id


def test_fake_check_and_read_by_channel():
    backend = FakeBackend()
    backend.create_channel("topic-1", [])
    target = Target(channel="topic-1")

    assert backend.check(target, Cursor()) is False
    res = backend.read(target, Cursor())
    assert isinstance(res, ReadResult)
    assert res.messages == []
    assert res.cursor == Cursor(at=None, id=None)

    first = _make_msg(channel="topic-1", msg_id="01AAAA", at="2026-10-10T10:00:00+00:00")
    second = _make_msg(channel="topic-1", msg_id="01BBBB", at="2026-10-10T11:00:00+00:00")
    backend.send("topic-1", first)
    backend.send("topic-1", second)

    assert backend.check(target, Cursor()) is True
    assert backend.check(target, Cursor(at=second.at, id=second.id)) is False

    res = backend.read(target, Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert res.cursor == Cursor(at=second.at, id=second.id)

    # A second read from the stored cursor returns nothing.
    again = backend.read(target, res.cursor)
    assert again.messages == []
    assert again.cursor == res.cursor


def test_fake_same_second_messages_are_both_read():
    backend = FakeBackend()
    backend.create_channel("topic-1", [])
    target = Target(channel="topic-1")
    same_second = "2026-10-10T12:00:00+00:00"
    backend.send("topic-1", _make_msg(channel="topic-1", msg_id="01AAAA", at=same_second))

    # A read up to the first message must still deliver the second one,
    # which arrives in the same second but with a larger ULID.
    backend.send("topic-1", _make_msg(channel="topic-1", msg_id="01BBBB", at=same_second))
    res = backend.read(target, Cursor(at=same_second, id="01AAAA"))
    assert [m.id for m in res.messages] == ["01BBBB"]
    assert res.cursor == Cursor(at=same_second, id="01BBBB")


def test_fake_read_to_me_filters_recipient_and_groups():
    me = {"repo": "owner-org/worker", "role": "team-a"}
    backend = FakeBackend()
    backend.create_channel("topic-1", [])
    backend.create_channel("topic-2", [])
    backend.send("topic-1", _make_msg(channel="topic-1", msg_id="01AAAA", to=me))
    backend.send("topic-2", _make_msg(channel="topic-2", msg_id="01BBBB", to=me))  # other channel, still mine
    backend.send("topic-1", _make_msg(channel="topic-1", msg_id="01CCCC",
                                      to={"repo": "owner-org/worker", "role": "team-b"}))
    backend.send("topic-1", _make_msg(channel="topic-1", msg_id="01DDDD", to={"group": "all-workers"}))

    res = backend.read(Target(recipient=me), Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert backend.check(Target(recipient=me), Cursor(at=res.cursor.at, id=res.cursor.id)) is False

    # A channel target is not filtered by recipient.
    res_channel = backend.read(Target(channel="topic-1"), Cursor())
    assert [m.id for m in res_channel.messages] == ["01AAAA", "01CCCC", "01DDDD"]


def test_fake_read_unknown_channel_raises():
    backend = FakeBackend()
    with pytest.raises(ChannelNotFoundError):
        backend.read(Target(channel="nope"), Cursor())
    with pytest.raises(ChannelNotFoundError):
        backend.check(Target(channel="nope"), Cursor())


def test_fake_backend_error_simulation():
    backend = FakeBackend()
    backend.create_channel("topic", [])
    backend.next_error = AuthenticationError("Invalid token")
    with pytest.raises(AuthenticationError):
        backend.read(Target(channel="topic"), Cursor())
    assert isinstance(AuthenticationError("x"), BackendError)
    # Once consumed, the next call works.
    assert backend.check(Target(channel="topic"), Cursor()) is False
