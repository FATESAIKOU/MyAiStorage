"""Unit tests for `mentions` and `in_reply_to` (#209): message model, filters, cursors, ETag keys.

Sending is strict, reading is wide; "to me" counts a mention, "@ me" (`Target(mentioned=)`)
counts `to` or a mention but never a broadcast, and each keeps its own cursor.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from typing import Any

import pytest

from _angareion_fakes import StaticResolver, entity
from angareion.backend import Cursor, FakeBackend, Target
from angareion.client import Angareion
from angareion.github import GitHubIssuesBackend, HttpResponse
from angareion.messages import (
    Message,
    MessageValidationError,
    message_to_dict,
    parse_message,
    serialize_message,
)
from angareion.state import CursorStore, FileETagCache, channel_cursor_key, mention_cursor_key, recipient_cursor_key

ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
OTHER_AI = "mylinuxpool-impl-w1-2"
GENERAL = entity(channel="general", issue=10)
OTHER = entity(channel="other", backend="github-2", repo="owner/repo2", issue=7)
ULID_A = "01JABCDEF0123456789ABCDEF0"
ULID_B = "01JABCDEF0123456789ABCDEF1"


def _msg(**kwargs: Any) -> Message:
    base: dict[str, Any] = dict(from_=PM, to=None, channel="general", content="hello", id=ULID_A)
    base.update(kwargs)
    return Message(**base)


def _front_matter(extra: str, *, to: str | None = None) -> str:
    to_line = f"to: {to}\n" if to else ""
    return (
        "---\nangareion: 1\nid: " + ULID_A + "\nat: '2026-10-10T12:00:00+00:00'\n"
        f"from: {PM}\n{to_line}{extra}channel: general\nurgency: 5\nattachments: []\n---\nbody\n"
    )


# -- model: sending is strict ------------------------------------------------


@pytest.mark.parametrize(
    "mentions",
    [
        ["not-an-ai"],
        ["mypmo-pm"],  # the old grammar is read, never sent
        ["MyPMO-pm-m1"],
        [PM, PM],  # duplicate
        [PM, 42],
        "mypmo-pm-m1",  # not a list
    ],
)
def test_sending_refuses_a_bad_mentions_list(mentions):
    with pytest.raises(MessageValidationError):
        serialize_message(_msg(mentions=mentions))


@pytest.mark.parametrize("value", ["", "abc", ULID_A.lower(), ULID_A + "0", "01JABCDEF0123456789ABCDEFI", 5])
def test_sending_refuses_an_in_reply_to_that_is_not_a_ulid(value):
    with pytest.raises(MessageValidationError):
        serialize_message(_msg(in_reply_to=value))


def test_the_defaults_are_empty_and_none():
    message = _msg()
    assert message.mentions == []
    assert message.in_reply_to is None


# -- model: serialization ------------------------------------------------------


def test_mentions_are_written_as_one_flow_line_and_the_version_stays_1():
    text = serialize_message(_msg(mentions=[PM, OTHER_AI], in_reply_to=ULID_B))
    assert f"mentions: [{PM}, {OTHER_AI}]\n" in text
    assert f"in_reply_to: {ULID_B}\n" in text
    assert "angareion: 1\n" in text
    assert "- " + PM not in text  # never the block form

    parsed = parse_message(text, strict=True)
    assert parsed is not None
    assert parsed.mentions == [PM, OTHER_AI]
    assert parsed.in_reply_to == ULID_B
    assert parsed.angareion == 1


def test_empty_mentions_and_no_reply_are_not_written():
    text = serialize_message(_msg())
    assert "mentions" not in text
    assert "in_reply_to" not in text


# -- model: reading is wide ----------------------------------------------------


def test_an_old_message_without_the_keys_reads_as_empty_and_none():
    parsed = parse_message(_front_matter(""), strict=True)
    assert parsed is not None
    assert parsed.mentions == []
    assert parsed.in_reply_to is None


@pytest.mark.parametrize(
    "extra",
    [
        "mentions: mypmo-pm-m1\n",  # a string, not a list
        "mentions: {a: b}\n",
        "mentions: 7\n",
        "mentions: null\n",
    ],
)
def test_a_mentions_of_the_wrong_type_is_dropped_and_the_message_is_kept(extra):
    parsed = parse_message(_front_matter(extra))
    assert parsed is not None
    assert parsed.mentions == []
    assert parsed.content == "body\n"


def test_a_bad_mentions_item_is_dropped_and_the_good_ones_stay():
    parsed = parse_message(_front_matter(f"mentions: [{PM}, not-an-ai, 42, {OTHER_AI}, {PM}]\n"))
    assert parsed is not None
    assert parsed.mentions == [PM, OTHER_AI]


def test_mentions_read_accepts_the_old_grammar_like_from_and_to():
    parsed = parse_message(_front_matter("mentions: [mypmo-pm]\n"))
    assert parsed is not None
    assert parsed.mentions == ["mypmo-pm"]


@pytest.mark.parametrize("value", ["abc", "'01JABCDEF0123456789ABCDEFI'", "[a]", "7"])
def test_a_bad_in_reply_to_is_dropped_and_the_message_is_kept(value):
    parsed = parse_message(_front_matter(f"in_reply_to: {value}\n"))
    assert parsed is not None
    assert parsed.in_reply_to is None
    assert parsed.content == "body\n"


def test_message_to_dict_carries_both_fields():
    data = message_to_dict(_msg(mentions=[PM], in_reply_to=ULID_B))
    assert data["mentions"] == [PM]
    assert data["in_reply_to"] == ULID_B
    plain = message_to_dict(_msg())
    assert plain["mentions"] == []
    assert plain["in_reply_to"] is None


# -- filters (fake backend, via the library) ------------------------------------


def _backend() -> FakeBackend:
    resolver = StaticResolver([GENERAL, OTHER])
    backend = FakeBackend(resolver)
    backend.add_entity("general", number=10)
    backend.add_entity("other", number=7, repo="owner/repo2")
    return backend


def _app(tmp_path, backend: FakeBackend) -> Angareion:
    return Angareion(backend, CursorStore(tmp_path, ME), StaticResolver([GENERAL, OTHER]), ME)


def _filter_backend() -> FakeBackend:
    backend = _backend()
    t = "2026-10-10T10:0%d:00Z"
    backend.deliver(GENERAL, _msg(id="01AAAA", to=ME), comment_id=1, created_at=t % 1)
    backend.deliver(GENERAL, _msg(id="01BBBB", to=None), comment_id=2, created_at=t % 2)  # broadcast
    backend.deliver(GENERAL, _msg(id="01CCCC", to=OTHER_AI, mentions=[ME]), comment_id=3, created_at=t % 3)
    backend.deliver(GENERAL, _msg(id="01DDDD", to=OTHER_AI), comment_id=4, created_at=t % 4)  # not mine
    backend.deliver(GENERAL, _msg(id="01EEEE", to=PM, mentions=[OTHER_AI]), comment_id=5, created_at=t % 5)
    return backend


def test_a_mention_only_message_is_read_by_to_me_and_by_at_me(tmp_path):
    backend = _filter_backend()
    to_me = [m.id for m in _app(tmp_path / "a", backend).read(Target(recipient=ME)).messages]
    at_me = [m.id for m in _app(tmp_path / "b", backend).read(Target(mentioned=ME)).messages]
    assert "01CCCC" in to_me
    assert "01CCCC" in at_me


def test_a_broadcast_is_in_to_me_but_not_in_at_me(tmp_path):
    backend = _filter_backend()
    to_me = [m.id for m in _app(tmp_path / "a", backend).read(Target(recipient=ME)).messages]
    at_me = [m.id for m in _app(tmp_path / "b", backend).read(Target(mentioned=ME)).messages]
    assert to_me == ["01AAAA", "01BBBB", "01CCCC"]
    assert at_me == ["01AAAA", "01CCCC"]


def test_messages_for_others_without_a_mention_of_me_are_in_neither(tmp_path):
    backend = _filter_backend()
    seen = {
        m.id
        for target in (Target(recipient=ME), Target(mentioned=ME))
        for m in _app(tmp_path / target.key(), backend).read(target).messages
    }
    assert "01DDDD" not in seen
    assert "01EEEE" not in seen


def test_at_me_spans_channels(tmp_path):
    backend = _filter_backend()
    backend.deliver(OTHER, _msg(id="01FFFF", channel="other", to=PM, mentions=[ME]), comment_id=6, created_at="2026-10-10T10:06:00Z")
    messages = _app(tmp_path, backend).read(Target(mentioned=ME)).messages
    assert [m.id for m in messages] == ["01AAAA", "01CCCC", "01FFFF"]


def test_check_for_at_me_ignores_a_broadcast(tmp_path):
    backend = _backend()
    backend.deliver(GENERAL, _msg(id="01BBBB", to=None), comment_id=1, created_at="2026-10-10T10:00:00Z")
    app = _app(tmp_path, backend)
    assert app.check(Target(recipient=ME)) is True
    assert app.check(Target(mentioned=ME)) is False
    backend.deliver(GENERAL, _msg(id="01CCCC", to=PM, mentions=[ME]), comment_id=2, created_at="2026-10-10T10:01:00Z")
    assert app.check(Target(mentioned=ME)) is True


def test_target_needs_exactly_one_kind():
    with pytest.raises(ValueError):
        Target(channel="general", mentioned=ME)
    with pytest.raises(ValueError):
        Target(recipient=ME, mentioned=ME)
    with pytest.raises(ValueError):
        Target()


# -- cursors -----------------------------------------------------------------


def test_the_mention_cursor_key_is_its_own_dimension():
    key = mention_cursor_key(ME, "general")
    assert key.startswith("mention:")
    assert key != recipient_cursor_key(ME, "general")
    assert key != channel_cursor_key("general")


def test_reading_at_me_leaves_the_to_me_and_channel_cursors_alone(tmp_path):
    backend = _filter_backend()
    app = _app(tmp_path, backend)
    batch = app.read(Target(mentioned=ME))
    batch.commit()
    stored = json.loads(app.cursors.path.read_text(encoding="utf-8"))
    assert set(stored) == {mention_cursor_key(ME, "general"), mention_cursor_key(ME, "other")}

    # "to me" and the channel still see everything
    assert [m.id for m in app.read(Target(recipient=ME)).messages] == ["01AAAA", "01BBBB", "01CCCC"]
    assert len(app.read(Target(channel="general")).messages) == 5
    # and "@ me" has nothing new
    assert app.read(Target(mentioned=ME)).messages == []


def test_reading_to_me_and_channel_leaves_the_at_me_cursor_alone(tmp_path):
    backend = _filter_backend()
    app = _app(tmp_path, backend)
    app.read(Target(recipient=ME)).commit()
    app.read(Target(channel="general")).commit()
    stored = json.loads(app.cursors.path.read_text(encoding="utf-8"))
    assert not any(key.startswith("mention:") for key in stored)
    assert [m.id for m in app.read(Target(mentioned=ME)).messages] == ["01AAAA", "01CCCC"]


def test_wait_for_at_me_does_not_move_any_cursor(tmp_path):
    backend = _filter_backend()
    app = _app(tmp_path, backend)
    assert app.wait(Target(mentioned=ME), 0) is True
    assert not app.cursors.path.exists()


def test_at_me_with_since_moves_nothing(tmp_path):
    backend = _filter_backend()
    app = _app(tmp_path, backend)
    batch = app.read(Target(mentioned=ME), since="2026-10-10T10:03:00Z")
    assert [m.id for m in batch.messages] == ["01CCCC"]
    batch.commit()
    assert not app.cursors.path.exists()


# -- GitHub backend: ETag key ------------------------------------------------------


class _Transport:
    def __init__(self, comments: list[dict[str, Any]], etag: str = 'W/"v1"') -> None:
        self.body = json.dumps(comments).encode("utf-8")
        self.etag = etag
        self.sent: list[str | None] = []

    def request(self, method, url, headers=None, body=None, timeout=None):
        sent = (headers or {}).get("If-None-Match")
        self.sent.append(sent)
        if sent == self.etag:
            return HttpResponse(304, {"etag": self.etag}, b"")
        return HttpResponse(200, {"etag": self.etag}, self.body)


def _comment(comment_id: int, message: Message) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": serialize_message(message),
        "created_at": "2026-10-10T10:00:00Z",
        "updated_at": "2026-10-10T10:00:00Z",
    }


def test_the_at_me_etag_is_not_shared_with_to_me(tmp_path):
    # one broadcast: clean for "@ me", new for "to me" - a shared record would lie
    cursor = Cursor(position="5", at="2026-10-10T10:00:00Z", seen=["5"])
    transport = _Transport([_comment(10, _msg(to=None))])
    backend = GitHubIssuesBackend(
        StaticResolver([GENERAL]), token="fake_token", transport=transport, etag_cache=FileETagCache(tmp_path, ME)
    )

    assert backend.check(GENERAL, cursor, mentioned=ME) is False  # stores a clean record
    assert transport.sent == [None]
    assert backend.check(GENERAL, cursor, recipient=ME) is True  # the broadcast is new for "to me"
    assert transport.sent == [None, None]
    assert backend.check(GENERAL, cursor, mentioned=ME) is False  # now conditional
    assert transport.sent[-1] == 'W/"v1"'


def test_github_read_filters_by_mention(tmp_path):
    transport = _Transport(
        [
            _comment(1, _msg(id="01AAAA", to=None)),
            _comment(2, _msg(id="01BBBB", to=PM, mentions=[ME])),
        ]
    )
    backend = GitHubIssuesBackend(
        StaticResolver([GENERAL]), token="fake_token", transport=transport, etag_cache=FileETagCache(tmp_path, ME)
    )
    assert [m.id for m in backend.read(GENERAL, Cursor(), mentioned=ME).messages] == ["01BBBB"]
    assert [m.id for m in backend.read(GENERAL, Cursor(), recipient=ME).messages] == ["01AAAA", "01BBBB"]


def test_the_at_me_etag_is_not_shared_with_the_channel_target(tmp_path):
    # a message for someone else: clean for "@ me", new for the whole channel
    cursor = Cursor(position="5", at="2026-10-10T10:00:00Z", seen=["5"])
    transport = _Transport([_comment(10, _msg(to=OTHER_AI))])
    backend = GitHubIssuesBackend(
        StaticResolver([GENERAL]), token="fake_token", transport=transport, etag_cache=FileETagCache(tmp_path, ME)
    )

    assert backend.check(GENERAL, cursor, mentioned=ME) is False
    assert backend.check(GENERAL, cursor) is True
    assert transport.sent == [None, None]  # no If-None-Match borrowed from the "@ me" record
