"""Unit tests for Angareion backend protocol and FakeBackend."""

from __future__ import annotations

import pytest

from angareion.backend import (
    AuthenticationError,
    Backend,
    BackendError,
    ChannelNotFoundError,
    FakeBackend,
    MessageListResult,
    RateLimitError,
)
from angareion.messages import Message
from angareion.state import rebuild_from_backend


def _make_msg(
    channel: str = "test-topic",
    content: str = "hello",
    msg_id: str | None = None,
    at: str = "2026-10-10T12:00:00Z",
    ack: str | None = None,
) -> Message:
    kwargs = {
        "from_": {"repo": "owner-org/team", "role": "PM"},
        "to": {"repo": "owner-org/worker", "role": "worker"},
        "channel": channel,
        "content": content,
        "at": at,
    }
    if msg_id:
        kwargs["id"] = msg_id
    if ack:
        kwargs["ack"] = ack
    return Message(**kwargs)


def test_backend_protocol_conformance():
    fake = FakeBackend()
    assert isinstance(fake, Backend)


def test_backend_protocol_requires_list_channels():
    class IncompleteBackend:
        def create_channel(self, channel: str) -> int: ...
        def find_channel(self, channel: str) -> int | None: ...
        def post_message(self, channel: str, message: Message) -> str: ...
        def list_messages(self, since=None, etag=None) -> MessageListResult: ...

    assert not isinstance(IncompleteBackend(), Backend)
    assert isinstance(FakeBackend(), Backend)


def test_create_and_find_channel():
    backend = FakeBackend()
    assert backend.find_channel("topic-1") is None

    num1 = backend.create_channel("topic-1")
    assert isinstance(num1, int)
    assert backend.find_channel("topic-1") == num1

    # Calling create_channel on same topic returns existing number
    num2 = backend.create_channel("topic-1")
    assert num2 == num1

    num3 = backend.create_channel("topic-2")
    assert num3 != num1
    assert backend.find_channel("topic-2") == num3

    channels = dict(backend.list_channels())
    assert channels == {"topic-1": num1, "topic-2": num3}


def test_post_message_and_channel_not_found():
    backend = FakeBackend()
    msg = _make_msg(channel="unknown-topic")

    with pytest.raises(ChannelNotFoundError):
        backend.post_message("unknown-topic", msg)

    backend.create_channel("known-topic")
    msg2 = _make_msg(channel="known-topic", content="First message")
    comment_url = backend.post_message("known-topic", msg2)

    assert comment_url.startswith("https://")
    assert getattr(msg2, "comment_url") == comment_url
    assert getattr(msg2, "issue_number") == backend.find_channel("known-topic")


def test_post_message_channel_mismatch():
    backend = FakeBackend()
    backend.create_channel("chan-a")
    backend.create_channel("chan-b")
    msg = _make_msg(channel="chan-a")

    with pytest.raises(ValueError, match="does not match target channel"):
        backend.post_message("chan-b", msg)


def test_post_message_idempotent():
    backend = FakeBackend()
    backend.create_channel("topic")
    msg = _make_msg(channel="topic", msg_id="01MSGIDEMPOTENT")

    url1 = backend.post_message("topic", msg)
    url2 = backend.post_message("topic", msg)
    assert url1 == url2

    res = backend.list_messages()
    assert len(res.messages) == 1
    assert res.messages[0].id == "01MSGIDEMPOTENT"


def test_list_messages_etag_304_behavior():
    backend = FakeBackend()
    backend.create_channel("topic")

    # Empty list initial query
    res1 = backend.list_messages()
    assert isinstance(res1, tuple)
    assert len(res1) == 2
    assert res1.not_modified is False
    assert res1.messages == []
    etag1 = res1.etag
    assert etag1 is not None

    # Query with matching etag -> 304 Not Modified
    res2 = backend.list_messages(etag=etag1)
    assert res2.not_modified is True
    assert res2.messages == []
    assert res2.etag == etag1

    # Post message -> bumps etag
    msg = _make_msg(channel="topic", content="new message")
    backend.post_message("topic", msg)

    # Query with old etag1 -> 200 with new message and new etag
    res3 = backend.list_messages(etag=etag1)
    assert res3.not_modified is False
    assert len(res3.messages) == 1
    assert res3.messages[0].content == "new message"
    etag2 = res3.etag
    assert etag2 != etag1

    # Query with etag2 -> 304 Not Modified
    res4 = backend.list_messages(etag=etag2)
    assert res4.not_modified is True
    assert res4.messages == []
    assert res4.etag == etag2


def test_list_messages_since_filter():
    backend = FakeBackend()
    backend.create_channel("topic")

    m1 = _make_msg(channel="topic", at="2026-10-10T10:00:00Z", content="m1")
    m2 = _make_msg(channel="topic", at="2026-10-10T11:00:00Z", content="m2")
    m3 = _make_msg(channel="topic", at="2026-10-10T12:00:00Z", content="m3")
    backend.post_message("topic", m1)
    backend.post_message("topic", m2)
    backend.post_message("topic", m3)

    res_all = backend.list_messages()
    assert [m.content for m in res_all.messages] == ["m1", "m2", "m3"]

    res_since = backend.list_messages(since="2026-10-10T11:00:00Z")
    assert [m.content for m in res_since.messages] == ["m2", "m3"]


def test_backend_error_simulation():
    backend = FakeBackend()
    backend.create_channel("topic")

    backend.next_error = RateLimitError("Rate limit hit", retry_after=45)
    with pytest.raises(RateLimitError) as exc_info:
        backend.find_channel("topic")
    assert exc_info.value.retry_after == 45
    assert isinstance(exc_info.value, BackendError)

    # Once consumed, next call works
    assert backend.find_channel("topic") is not None

    backend.next_error = AuthenticationError("Invalid token")
    with pytest.raises(AuthenticationError):
        backend.list_messages()


def test_rebuild_from_backend_with_fake_backend(tmp_path):
    identity = {"repo": "owner-org/team", "role": "PM"}
    backend = FakeBackend()
    backend.create_channel("phase-1")
    backend.create_channel("phase-2")

    m1 = _make_msg(channel="phase-1", msg_id="01MSG1", at="2026-10-10T09:00:00Z")
    m2 = _make_msg(channel="phase-1", msg_id="01MSG2", at="2026-10-10T10:00:00Z", ack="01MSG1")
    backend.post_message("phase-1", m1)
    backend.post_message("phase-1", m2)

    state = rebuild_from_backend(backend, identity=identity, state_dir=tmp_path)
    assert "phase-1" in state.channels
    assert "phase-2" in state.channels
    assert "01MSG1" in state.acked
    assert state.since == "2026-10-10T10:00:00Z"
    assert state.etags.get("messages") is not None
