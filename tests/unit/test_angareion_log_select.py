"""Unit tests for angareion log_query.select (pure function)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

from angareion.log_query import AnchorNotFoundError, select
from angareion.messages import Message


def _msg(mid, cid, created, channel="general", content="msg", from_="pm-m1", to="me-1", mentions=None, in_reply_to=None):
    m = Message(from_=from_, to=to, channel=channel, content=content, id=mid, at=created)
    setattr(m, "received_at", created)
    setattr(m, "comment_id", cid)
    setattr(m, "comment_url", "")
    m.mentions = mentions or []
    m.in_reply_to = in_reply_to
    return m


def test_select_intersection():
    msgs = {
        "general": [
            _msg("A", 1, "2026-10-10T12:00:01Z", content="blocker", from_="pm", to="me-1"),
            _msg("B", 2, "2026-10-10T12:00:02Z", content="other", from_="pm", to="someone"),
        ]
    }
    out = select(msgs, ["general"], me="me-1", from_="pm", grep="blocker", to=None, mentioned=False, since=None, until=None, around=None, before=None, after=None, thread=None, limit=None)
    assert [m.id for m in out] == ["A"]


def test_select_limit_tail_cross_channel():
    msgs = {
        "general": [_msg("G1", 1, "2026-10-10T11:00:01Z", channel="general", content="g1")],
        "other": [_msg("O1", 2, "2026-10-10T12:00:00Z", channel="other", content="latest")],
    }
    out = select(msgs, ["general", "other"], me="me-1", from_=None, to=None, mentioned=False, since=None, until=None, grep=None, around=None, before=None, after=None, thread=None, limit=1)
    assert [m.id for m in out] == ["O1"]


def test_select_anchor_filtered_out():
    msgs = {
        "general": [
            _msg("M1", 1, "2026-10-10T12:00:01Z", content="m1"),
            _msg("M2", 2, "2026-10-10T12:00:02Z", content="other"),
            _msg("M3", 3, "2026-10-10T12:00:03Z", content="m3"),
        ]
    }
    out = select(msgs, ["general"], me="me-1", from_=None, to=None, mentioned=False, since=None, until=None, grep="m", around="M2", before=1, after=1, thread=None, limit=None)
    assert "M2" not in [m.id for m in out]
    assert [m.id for m in out] == ["M1", "M3"]


def test_select_anchor_not_found_raises():
    msgs = {"general": [_msg("M1", 1, "2026-10-10T12:00:01Z", content="m1")]}
    try:
        select(msgs, ["general"], me="me-1", from_=None, to=None, mentioned=False, since=None, until=None, grep=None, around="ZZZZ", before=None, after=None, thread=None, limit=None)
        raised = False
    except AnchorNotFoundError:
        raised = True
    assert raised


def test_select_thread_closure():
    msgs = {
        "general": [
            _msg("M3", 3, "2026-10-10T12:00:03Z", content="m3"),
            _msg("M5", 5, "2026-10-10T12:00:05Z", content="m5", in_reply_to="M3"),
            _msg("M8", 8, "2026-10-10T12:00:08Z", content="m8", in_reply_to="M3"),
            _msg("M9", 9, "2026-10-10T12:00:09Z", content="m9", in_reply_to="M5"),
        ]
    }
    out = select(msgs, ["general"], me="me-1", from_=None, to=None, mentioned=False, since=None, until=None, grep=None, around=None, before=None, after=None, thread="M5", limit=None)
    assert [m.id for m in out] == ["M3", "M5", "M8", "M9"] or [m.content for m in out] == ["m3", "m5", "m8", "m9"]
    assert [m.content for m in out] == ["m3", "m5", "m8", "m9"]