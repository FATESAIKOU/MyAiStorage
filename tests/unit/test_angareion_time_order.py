"""#510 PR-4: cross-channel order is by the instant `received_at` names, not by its text.

`log_query.select` (C-6) and `Angareion.read` without a channel (C-7) both sort
`(received_at_key(received_at), channel, id)`. A single channel in `select` keeps its
`comment_id` order. Every time here is pinned; nothing reads the wall clock.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from _angareion_fakes import StaticResolver, entity
from angareion.backend import Target
from angareion.fake_backend import FakeBackend
from angareion.client import Angareion
from angareion.log_query import select
from angareion.messages import Message
from angareion.state import CursorStore

ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
GENERAL = entity(channel="general", issue=10)
OTHER = entity(channel="other", backend="github-2", repo="owner/repo2", issue=7)


def _msg(mid: str, channel: str, received, comment_id: int) -> Message:
    m = Message(from_=PM, to=ME, channel=channel, content="hello", id=mid, at="2026-10-10T00:00:00Z")
    setattr(m, "received_at", received)
    setattr(m, "comment_id", comment_id)
    setattr(m, "comment_url", "")
    m.mentions = []
    m.in_reply_to = None
    return m


def _select(by_channel, channels, limit=None):
    return select(
        by_channel, channels, me=ME, from_=None, to=None, mentioned=False, since=None, until=None,
        grep=None, around=None, before=None, after=None, thread=None, limit=limit,
    )


def test_c6_mixed_spellings_order_by_instant_then_channel_then_id():
    by_channel = {
        "general": [
            _msg("N", "general", None, 1),
            _msg("D", "general", "2026-10-10T09:00:00+09:00", 2),  # 00:00Z
            _msg("B", "general", "2026-10-10T10:00:00Z", 3),  # 10:00Z
            _msg("U", "general", "not a time", 4),  # unreadable: last
        ],
        "other": [
            _msg("A", "other", "2026-10-10T19:00:00+09:00", 5),  # 10:00Z, same instant as B
            _msg("C", "other", "2026-10-10T10:00:00.5+00:00", 6),  # 10:00:00.5Z
        ],
    }
    out = _select(by_channel, ["general", "other"])
    # No time first; then by instant; B and A tie on the instant and general < other; U last.
    assert [m.id for m in out] == ["N", "D", "B", "A", "C", "U"]


def test_c6_same_instant_written_differently_breaks_tie_by_channel_then_id():
    by_channel = {
        "other": [_msg("Z2", "other", "2026-10-10T10:00:00Z", 1), _msg("Z1", "other", "2026-10-10T10:00:00+00:00", 2)],
        "general": [_msg("Y1", "general", "2026-10-10T19:00:00+09:00", 3)],
    }
    out = _select(by_channel, ["general", "other"])
    assert [m.id for m in out] == ["Y1", "Z1", "Z2"]


def test_c6_same_channel_same_instant_breaks_tie_by_id():
    # Input lists Q2 first, so only the id key can put Q1 ahead of it.
    by_channel = {
        "general": [_msg("Q2", "general", "2026-10-10T10:00:00Z", 1), _msg("Q1", "general", "2026-10-10T19:00:00+09:00", 2)],
        "other": [_msg("W", "other", "2026-10-10T00:00:00Z", 3)],
    }
    out = _select(by_channel, ["general", "other"])
    assert [m.id for m in out] == ["W", "Q1", "Q2"]


def test_c6_limit_one_takes_the_latest_instant_not_the_largest_text():
    # As text, "09:00:00+09:00" is larger than "00:30:00Z", but 09:00+09:00 is 00:00Z,
    # earlier than 00:30Z. The newest instant is the one that --limit 1 keeps.
    by_channel = {
        "general": [_msg("X", "general", "2026-10-10T09:00:00+09:00", 1)],
        "other": [_msg("Y", "other", "2026-10-10T00:30:00Z", 2)],
    }
    out = _select(by_channel, ["general", "other"], limit=1)
    assert [m.id for m in out] == ["Y"]


def test_c6_single_channel_keeps_comment_id_order_even_when_received_at_disagrees():
    # One channel: sort by comment_id as before, whatever received_at says.
    by_channel = {
        "general": [
            _msg("M1", "general", "2026-10-10T12:00:00Z", 1),
            _msg("M2", "general", "2026-10-10T09:00:00+09:00", 2),  # earlier instant, later comment
        ],
    }
    assert [m.id for m in _select(by_channel, ["general"])] == ["M1", "M2"]
    assert [m.id for m in _select(by_channel, ["general"], limit=1)] == ["M2"]


def _backend(clock: str = "2026-10-09T00:00:00Z") -> FakeBackend:
    resolver = StaticResolver([GENERAL, OTHER])
    backend = FakeBackend(resolver, clock=lambda: clock)
    backend.add_entity("general", number=10)
    backend.add_entity("other", number=7, repo="owner/repo2")
    return backend


def _to_me(mid: str, channel: str = "general") -> Message:
    return Message(from_=PM, to=ME, channel=channel, content="hello", id=mid, at="2026-10-10T00:00:00Z")


def test_c7_read_across_channels_orders_by_instant_with_mixed_spellings(tmp_path):
    backend = _backend()
    backend.deliver(GENERAL, _to_me("01AAAA"), comment_id=1, created_at="2026-10-10T09:00:00+09:00")  # 00:00Z
    backend.deliver(GENERAL, _to_me("01CCCC"), comment_id=2, created_at="2026-10-10T00:15:00.5+00:00")
    backend.deliver(OTHER, _to_me("01BBBB", "other"), comment_id=3, created_at="2026-10-10T00:30:00Z")
    resolver = StaticResolver([GENERAL, OTHER])
    app = Angareion(backend, CursorStore(tmp_path, ME), resolver, ME)

    batch = app.read(Target(recipient=ME))
    # The text order would be CCCC, BBBB, AAAA; the instant order is AAAA, CCCC, BBBB.
    assert [m.id for m in batch.messages] == ["01AAAA", "01CCCC", "01BBBB"]


def test_c7_read_same_channel_same_instant_breaks_tie_by_id(tmp_path):
    # The backend hands them back in comment order (QQQQ first); only the id key puts PPPP first.
    backend = _backend()
    backend.deliver(GENERAL, _to_me("01QQQQ"), comment_id=1, created_at="2026-10-10T10:00:00Z")
    backend.deliver(GENERAL, _to_me("01PPPP"), comment_id=2, created_at="2026-10-10T10:00:00Z")
    resolver = StaticResolver([GENERAL, OTHER])
    app = Angareion(backend, CursorStore(tmp_path, ME), resolver, ME)

    batch = app.read(Target(recipient=ME))
    assert [m.id for m in batch.messages] == ["01PPPP", "01QQQQ"]


def test_c7_read_same_instant_breaks_tie_by_channel_not_entity_order(tmp_path):
    # The resolver lists "other" first, so the entity order would put its message first,
    # and its id ("01AAAA") sorts before general's ("01ZZZZ"): only the channel key gives general first.
    backend = _backend()
    backend.deliver(OTHER, _to_me("01AAAA", "other"), comment_id=1, created_at="2026-10-10T10:00:00Z")
    backend.deliver(GENERAL, _to_me("01ZZZZ"), comment_id=2, created_at="2026-10-10T10:00:00Z")
    resolver = StaticResolver([OTHER, GENERAL])
    app = Angareion(backend, CursorStore(tmp_path, ME), resolver, ME)

    batch = app.read(Target(recipient=ME))
    assert [m.id for m in batch.messages] == ["01ZZZZ", "01AAAA"]


def test_c7_read_same_instant_written_differently_breaks_tie_by_channel(tmp_path):
    backend = _backend()
    backend.deliver(OTHER, _to_me("01BBBB", "other"), comment_id=1, created_at="2026-10-10T10:00:00Z")
    backend.deliver(GENERAL, _to_me("01AAAA"), comment_id=2, created_at="2026-10-10T19:00:00+09:00")
    resolver = StaticResolver([GENERAL, OTHER])
    app = Angareion(backend, CursorStore(tmp_path, ME), resolver, ME)

    batch = app.read(Target(recipient=ME))
    # Same instant: channel "general" sorts before "other", although its text sorts after.
    assert [m.id for m in batch.messages] == ["01AAAA", "01BBBB"]
