"""Unit tests for the ChannelBackend protocol, targets/cursors and FakeBackend (D2/D4/v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from _angareion_fakes import StaticResolver, entity
from angareion.backend import (
    CURSOR_SEEN_MARGIN_SECONDS,
    AuthenticationError,
    BackendError,
    BackendRef,
    ChannelBackend,
    ChannelNotFoundError,
    Cursor,
    Entity,
    FakeBackend,
    ReadResult,
    Target,
    advance_cursor,
    comment_is_new,
    cursor_since,
    parse_backend_time,
    recipient_matches,
)
from angareion.messages import Message

ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"


def _make_msg(
    channel: str = "general",
    content: str = "hello",
    msg_id: str = "01MSG0000000000000000000001",
    at: str = "2026-10-10T12:00:00+00:00",
    to: str | None = ME,
) -> Message:
    return Message(from_=PM, to=to, channel=channel, content=content, id=msg_id, at=at)


def _backend() -> tuple[FakeBackend, Entity]:
    general = entity(channel="general", issue=10)
    other = entity(channel="other", backend="github-2", repo="owner/repo2", issue=7)
    backend = FakeBackend(StaticResolver([general, other]))
    backend.add_entity("general", number=10)
    backend.add_entity("other", number=7, repo="owner/repo2")
    return backend, general


def test_target_requires_exactly_one_destination():
    with pytest.raises(ValueError):
        Target()
    with pytest.raises(ValueError):
        Target(channel="a", recipient=ME)
    assert Target(channel="a").channel == "a"
    assert Target(recipient=ME).recipient == ME


def test_target_keys():
    assert Target(channel="general").key() == "channel:general"
    assert Target(recipient=ME).key() == "to:mylinuxpool-impl-w1-1"


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

    # Even a response that only lists older comments must not pull the position back.
    older_only = advance_cursor(cursor, [(50, "2026-10-10T12:00:30Z")])
    assert older_only.position == "100"
    assert older_only.at == "2026-10-10T12:01:00Z"
    assert older_only.seen == ["50"]

    # An old comment far outside the window drops out of seen.
    old = advance_cursor(cursor, [(100, "2026-10-10T12:01:00Z"), (50, "2026-10-10T10:00:00Z")])
    assert set(old.seen) == {"100"}

    # No entries: the cursor is unchanged.
    assert advance_cursor(cursor, []) == cursor


def test_recipient_matches_counts_broadcasts():
    assert recipient_matches(_make_msg(to=ME), ME)
    assert not recipient_matches(_make_msg(to="other-pm-m1"), ME)
    # A broadcast (no `to`) is for everyone.
    assert recipient_matches(_make_msg(to=None), ME)
    assert recipient_matches(_make_msg(to=None), "any-arch")
    # No recipient filter: every message is in scope.
    assert recipient_matches(_make_msg(to="other-pm-m1"), None)


def test_fake_backend_conforms_to_protocol():
    backend, _ = _backend()
    assert isinstance(backend, ChannelBackend)


def test_fake_create_verify_close_without_members():
    backend, _ = _backend()
    number = backend.create_channel("fresh", BackendRef(name="github-main", type="github", repo="owner/repo"))
    assert backend.entity_state(number) == "open"
    assert backend.entity_title(number) == "fresh"
    assert backend.verify_channel("fresh", BackendRef("github-main", "github", "owner/repo"), number) is True

    # A different title fails the client-side verification (review H2).
    backend.set_entity_title(number, "renamed")
    assert backend.verify_channel("fresh", BackendRef("github-main", "github", "owner/repo"), number) is False

    backend.close_channel(entity(channel="fresh", issue=number))
    assert backend.entity_state(number) == "closed"

    # A missing entity is a 404: closing it again is fine.
    backend.close_channel(entity(channel="fresh", issue=999))
    # And closing a channel that was never created does not resurrect anything.
    assert backend.entity_state(999) is None


def test_fake_backend_keys_entities_by_repo_and_issue():
    """L5: the same issue number in two repos are different entities."""
    general = entity(channel="general", issue=10)  # owner/repo
    other = entity(channel="other", backend="github-2", repo="owner/repo2", issue=10)
    backend = FakeBackend(StaticResolver([general, other]))
    backend.add_entity("general", number=10, repo="owner/repo")
    backend.add_entity("other", number=10, repo="owner/repo2")

    assert backend.entity_state(10, repo="owner/repo") == "open"
    assert backend.entity_state(10, repo="owner/repo2") == "open"

    # Verify is repo-sensitive: the same number under the other repo has another title.
    assert backend.verify_channel("general", BackendRef("github-main", "github", "owner/repo"), 10) is True
    assert backend.verify_channel("general", BackendRef("github-2", "github", "owner/repo2"), 10) is False

    # Closing one repo's entity leaves the other's alone.
    backend.close_channel(general)
    assert backend.entity_state(10, repo="owner/repo") == "closed"
    assert backend.entity_state(10, repo="owner/repo2") == "open"

    # And sending to the still-open entity works.
    msg = _make_msg(channel="other", msg_id="01OTHER")
    assert backend.send(other, msg) == "01OTHER"


def test_fake_backend_injected_failures():
    backend, general = _backend()
    backend.next_create_error = BackendError("backend refused to open an entity")
    with pytest.raises(BackendError):
        backend.create_channel("fresh", BackendRef("github-main", "github", "owner/repo"))

    backend.next_close_error = BackendError("closing failed")
    with pytest.raises(BackendError):
        backend.close_channel(general)

    backend.next_error = AuthenticationError("Invalid token")
    with pytest.raises(AuthenticationError):
        backend.read(general, Cursor())
    assert isinstance(AuthenticationError("x"), BackendError)
    # Once consumed, the next call works.
    assert backend.check(general, Cursor()) is False


def test_fake_send_requires_the_entity_and_a_matching_name():
    backend, general = _backend()
    with pytest.raises(ChannelNotFoundError):
        backend.send(entity(channel="nope", issue=42), _make_msg(channel="nope"))

    with pytest.raises(ValueError, match="does not match target channel"):
        backend.send(general, _make_msg(channel="other"))

    msg = _make_msg(channel="general", content="first")
    assert backend.send(general, msg) == msg.id


def test_fake_check_and_read_by_entity():
    backend, general = _backend()

    assert backend.check(general, Cursor()) is False
    res = backend.read(general, Cursor())
    assert isinstance(res, ReadResult)
    assert res.messages == []
    assert res.cursor == Cursor()

    first = _make_msg(channel="general", msg_id="01AAAA")
    second = _make_msg(channel="general", msg_id="01BBBB")
    backend.deliver(general, first, comment_id=10, created_at="2026-10-10T10:00:00Z")
    backend.deliver(general, second, comment_id=11, created_at="2026-10-10T11:00:00Z")

    assert backend.check(general, Cursor()) is True

    res = backend.read(general, Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert res.cursor.position == "11"
    assert res.cursor.at == "2026-10-10T11:00:00Z"
    assert res.messages[0].received_at == "2026-10-10T10:00:00Z"
    assert res.messages[1].received_at == "2026-10-10T11:00:00Z"

    # Nothing new once the cursor has been advanced onto that read.
    assert backend.check(general, res.cursor) is False
    assert backend.read(general, res.cursor).messages == []


def test_fake_replication_lag_delivers_the_late_smaller_id():
    """A smaller comment id that shows up after a bigger one must still arrive (D4)."""
    backend, general = _backend()

    backend.deliver(general, _make_msg(msg_id="01LATER"), comment_id=100, created_at="2026-10-10T12:00:10Z")
    first = backend.read(general, Cursor())
    assert [m.id for m in first.messages] == ["01LATER"]

    # The lagging comment: smaller id, backend time close behind.
    backend.deliver(general, _make_msg(msg_id="01EARLIER"), comment_id=99, created_at="2026-10-10T12:00:05Z")
    assert backend.check(general, first.cursor) is True

    second = backend.read(general, first.cursor)
    assert [m.id for m in second.messages] == ["01EARLIER"]
    assert second.cursor.position == "100"  # the position never moves backwards
    assert second.cursor.at == "2026-10-10T12:00:10Z"
    assert set(second.cursor.seen) == {"99", "100"}

    # And it does not come back a third time.
    assert backend.read(general, second.cursor).messages == []


def test_fake_edited_comment_does_not_come_back():
    backend, general = _backend()
    message = _make_msg(msg_id="01AAAA")
    backend.deliver(general, message, comment_id=10, created_at="2026-10-10T12:00:00Z")

    cursor = backend.read(general, Cursor()).cursor
    message.content = "edited after being read"  # the backend's updated_at moved; id and time did not
    assert backend.check(general, cursor) is False
    assert backend.read(general, cursor).messages == []


def test_fake_read_to_me_filters_recipient_and_includes_broadcasts():
    backend, general = _backend()
    other = entity(channel="other", backend="github-2", repo="owner/repo2", issue=7)
    backend.deliver(general, _make_msg(channel="general", msg_id="01AAAA", to=ME), comment_id=1, created_at="2026-10-10T10:00:00Z")
    backend.deliver(other, _make_msg(channel="other", msg_id="01BBBB", to=ME), comment_id=2, created_at="2026-10-10T10:01:00Z")
    backend.deliver(general, _make_msg(channel="general", msg_id="01CCCC", to="other-pm-m1"), comment_id=3, created_at="2026-10-10T10:02:00Z")
    backend.deliver(general, _make_msg(channel="general", msg_id="01DDDD", to=None), comment_id=4, created_at="2026-10-10T10:03:00Z")

    res = backend.read(general, Cursor(), recipient=ME)
    assert [m.id for m in res.messages] == ["01AAAA", "01DDDD"]
    assert backend.check(general, res.cursor, recipient=ME) is False

    # A channel target is not filtered by recipient.
    res_channel = backend.read(general, Cursor())
    assert [m.id for m in res_channel.messages] == ["01AAAA", "01CCCC", "01DDDD"]


def test_fake_read_unknown_entity_raises_nothing_but_send_does():
    backend, _ = _backend()
    ghost = entity(channel="nope", issue=999)
    # Reading an entity the resolver handed out but the backend never opened:
    # comments are simply empty (the resolver owns existence).
    assert backend.read(ghost, Cursor()).messages == []
    with pytest.raises(ChannelNotFoundError):
        backend.send(ghost, _make_msg(channel="nope"))
