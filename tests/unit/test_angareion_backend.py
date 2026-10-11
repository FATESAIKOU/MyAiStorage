"""Unit tests for the ChannelBackend protocol, targets/cursors and FakeBackend (D2/D4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from angareion.backend import (
    CURSOR_SEEN_MARGIN_SECONDS,
    AuthenticationError,
    BackendError,
    ChannelBackend,
    ChannelNotFoundError,
    Cursor,
    FakeBackend,
    ReadResult,
    Target,
    address_matches_identity,
    advance_cursor,
    comment_is_new,
    cursor_since,
    parse_backend_time,
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


def test_backend_time_parsing_and_formatting():
    parsed = parse_backend_time("2026-10-10T12:00:00Z")
    assert parsed.isoformat() == "2026-10-10T12:00:00+00:00"
    # A different offset is the same instant: compare by time, not by string.
    assert parse_backend_time("2026-10-10T21:00:00+09:00") == parsed


def test_cursor_since_uses_margin_for_a_local_cursor_only():
    # No cursor: no since at all.
    assert cursor_since(Cursor()) is None

    # A local cursor asks from at - margin (replication-lag window).
    local = Cursor(position="100", at="2026-10-10T12:00:00Z", seen=["100"])
    assert cursor_since(local) == "2026-10-10T11:59:00Z"
    assert CURSOR_SEEN_MARGIN_SECONDS == 60.0

    # A time-only cursor (`--since`) asks from its time as-is: inclusive.
    assert cursor_since(Cursor(at="2026-10-10T12:00:00Z")) == "2026-10-10T12:00:00Z"


def test_comment_is_new_follows_the_backend_position_and_window():
    # No cursor: everything is new.
    assert comment_is_new(5, "2026-10-10T12:00:00Z", Cursor())

    # Past the position: new, regardless of the window.
    cursor = Cursor(position="100", at="2026-10-10T12:00:00Z", seen=["98", "99", "100"])
    assert comment_is_new(101, "2026-10-10T12:00:01Z", cursor)

    # Behind the position but inside the window and not seen: new (lag).
    assert comment_is_new(97, "2026-10-10T12:00:30Z", cursor)

    # Behind the position and already seen: not new (an edited comment).
    assert not comment_is_new(99, "2026-10-10T12:00:30Z", cursor)

    # Behind the position and out of the window: not new.
    assert not comment_is_new(97, "2026-10-10T11:58:00Z", cursor)

    # A time-only cursor is inclusive: at the boundary counts.
    since = Cursor(at="2026-10-10T12:00:00Z")
    assert comment_is_new(1, "2026-10-10T12:00:00Z", since)
    assert comment_is_new(1, "2026-10-10T12:00:01Z", since)
    assert not comment_is_new(1, "2026-10-10T11:59:59Z", since)


def test_advance_cursor_keeps_position_and_recomputes_seen():
    entries = [(98, "2026-10-10T12:00:00Z"), (99, "2026-10-10T12:00:30Z"), (100, "2026-10-10T12:01:00Z")]
    cursor = advance_cursor(Cursor(), entries)
    assert cursor.position == "100"
    assert cursor.at == "2026-10-10T12:01:00Z"
    # seen keeps the ids within at - margin only (all three here).
    assert set(cursor.seen) == {"98", "99", "100"}

    # A smaller id arriving later never moves the position back; the time stays,
    # and seen is recomputed from the response (which lists both comments).
    lagging = advance_cursor(cursor, [(100, "2026-10-10T12:01:00Z"), (99, "2026-10-10T12:00:30Z")])
    assert lagging.position == "100"
    assert lagging.at == "2026-10-10T12:01:00Z"
    assert set(lagging.seen) == {"99", "100"}

    # An old comment far outside the window drops out of seen.
    old = advance_cursor(cursor, [(100, "2026-10-10T12:01:00Z"), (50, "2026-10-10T10:00:00Z")])
    assert set(old.seen) == {"100"}

    # No entries: the cursor is unchanged.
    assert advance_cursor(cursor, []) == cursor


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
    assert res.cursor == Cursor()

    first = _make_msg(channel="topic-1", msg_id="01AAAA")
    second = _make_msg(channel="topic-1", msg_id="01BBBB")
    backend.deliver("topic-1", first, comment_id=10, created_at="2026-10-10T10:00:00Z")
    backend.deliver("topic-1", second, comment_id=11, created_at="2026-10-10T11:00:00Z")

    assert backend.check(target, Cursor()) is True

    res = backend.read(target, Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert res.cursor.position == "11"
    assert res.cursor.at == "2026-10-10T11:00:00Z"
    assert res.messages[0].received_at == "2026-10-10T10:00:00Z"
    assert res.messages[1].received_at == "2026-10-10T11:00:00Z"

    # Nothing new once the cursor has been advanced onto that read.
    assert backend.check(target, res.cursor) is False

    # A second read from the stored cursor returns nothing.
    again = backend.read(target, res.cursor)
    assert again.messages == []
    assert again.cursor == res.cursor


def test_fake_replication_lag_delivers_the_late_smaller_id():
    """A smaller comment id that shows up after a bigger one must still arrive (D4)."""
    backend = FakeBackend()
    backend.create_channel("topic-1", [])
    target = Target(channel="topic-1")

    backend.deliver("topic-1", _make_msg(channel="topic-1", msg_id="01LATER"), comment_id=100, created_at="2026-10-10T12:00:10Z")
    first = backend.read(target, Cursor())
    assert [m.id for m in first.messages] == ["01LATER"]

    # The lagging comment: smaller id, backend time close behind.
    backend.deliver("topic-1", _make_msg(channel="topic-1", msg_id="01EARLIER"), comment_id=99, created_at="2026-10-10T12:00:05Z")
    assert backend.check(target, first.cursor) is True

    second = backend.read(target, first.cursor)
    assert [m.id for m in second.messages] == ["01EARLIER"]
    assert second.cursor.position == "100"  # the position never moves backwards
    assert second.cursor.at == "2026-10-10T12:00:10Z"
    assert set(second.cursor.seen) == {"99", "100"}

    # And it does not come back a third time.
    assert backend.read(target, second.cursor).messages == []


def test_fake_edited_comment_does_not_come_back():
    backend = FakeBackend()
    backend.create_channel("topic-1", [])
    target = Target(channel="topic-1")
    message = _make_msg(channel="topic-1", msg_id="01AAAA")
    backend.deliver("topic-1", message, comment_id=10, created_at="2026-10-10T12:00:00Z")

    cursor = backend.read(target, Cursor()).cursor
    message.content = "edited after being read"  # the backend's updated_at moved; id and time did not
    assert backend.check(target, cursor) is False
    assert backend.read(target, cursor).messages == []


def test_fake_read_to_me_filters_recipient_and_groups():
    me = {"repo": "owner-org/worker", "role": "team-a"}
    backend = FakeBackend()
    backend.create_channel("topic-1", [])
    backend.create_channel("topic-2", [])
    backend.deliver("topic-1", _make_msg(channel="topic-1", msg_id="01AAAA", to=me), comment_id=1, created_at="2026-10-10T10:00:00Z")
    backend.deliver("topic-2", _make_msg(channel="topic-2", msg_id="01BBBB", to=me), comment_id=2, created_at="2026-10-10T10:01:00Z")
    backend.deliver("topic-1", _make_msg(channel="topic-1", msg_id="01CCCC",
                                         to={"repo": "owner-org/worker", "role": "team-b"}), comment_id=3, created_at="2026-10-10T10:02:00Z")
    backend.deliver("topic-1", _make_msg(channel="topic-1", msg_id="01DDDD", to={"group": "all-workers"}), comment_id=4, created_at="2026-10-10T10:03:00Z")

    res = backend.read(Target(recipient=me), Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert backend.check(Target(recipient=me), res.cursor) is False

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
