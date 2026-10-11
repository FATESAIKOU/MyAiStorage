"""Unit tests for Angareion GitHubBackend (task 1.4).

Tests use mock/injected HttpTransport without external network access.
"""

from __future__ import annotations

import json
from typing import Any
import urllib.error
import pytest

from angareion.backend import (
    AuthenticationError,
    Backend,
    BackendError,
    ChannelNotFoundError,
    RateLimitError,
)
from angareion.github import GitHubBackend, HttpResponse, HttpTransport
from angareion.messages import Message, serialize_message


def _make_msg(
    channel: str = "phase-1",
    content: str = "Hello GitHub",
    msg_id: str = "01JABCDEF0123456789ABCDEF0",
    at: str = "2026-10-10T12:00:00Z",
) -> Message:
    return Message(
        from_={"repo": "owner-org/team", "role": "PM"},
        to={"repo": "owner-org/worker", "role": "worker"},
        channel=channel,
        content=content,
        id=msg_id,
        at=at,
    )


class FakeTransport:
    """Mock HTTP transport recording requests and returning canned responses."""

    def __init__(self, routes: dict[tuple[str, str], HttpResponse] | None = None):
        self.routes: dict[tuple[str, str], HttpResponse] = dict(routes or {})
        self.recorded_requests: list[dict[str, Any]] = []

    def set_route(self, method: str, url: str, response: HttpResponse) -> None:
        self.routes[(method, url)] = response

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.recorded_requests.append(
            {"method": method, "url": url, "headers": headers or {}, "body": body}
        )

        key = (method, url)
        if key in self.routes:
            return self.routes[key]

        # Match prefix or url without query if exact match not found
        for (r_method, r_url), resp in self.routes.items():
            if r_method == method and (url.startswith(r_url) or r_url.startswith(url)):
                return resp

        # Default fallback
        return HttpResponse(404, {}, b'{"message": "Not Found"}')


def test_github_backend_conforms_to_protocol():
    backend = GitHubBackend("owner/repo", token="fake_token", transport=FakeTransport())
    assert isinstance(backend, Backend)


def test_list_messages_etag_304():
    transport = FakeTransport()
    # Route for comments with If-None-Match
    def handle_request(method, url, headers=None, body=None):
        if headers and headers.get("If-None-Match") == 'W/"etag-match"':
            return HttpResponse(304, {"etag": 'W/"etag-match"'}, b"")
        return HttpResponse(200, {"etag": 'W/"etag-new"'}, b"[]")

    class ETagTransport:
        def request(self, method, url, headers=None, body=None):
            if headers and headers.get("If-None-Match") == 'W/"etag-match"':
                return HttpResponse(304, {"etag": 'W/"etag-match"'}, b"")
            return HttpResponse(200, {"etag": 'W/"etag-new"'}, b"[]")

    backend = GitHubBackend("owner/repo", token="token123", transport=ETagTransport())
    res = backend.list_messages(etag='W/"etag-match"')
    assert res.not_modified is True
    assert res.messages == []
    assert res.etag == 'W/"etag-match"'


def test_list_messages_200_and_parsing():
    msg = _make_msg(channel="task-1", content="Parsed body", msg_id="01MSG001")
    serialized = serialize_message(msg)

    comments_json = [
        {
            "id": 101,
            "body": serialized,
            "html_url": "https://github.com/owner/repo/issues/42#issuecomment-101",
            "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
        },
        {
            "id": 102,
            "body": "Normal non-a2a comment (should be ignored)",
            "html_url": "https://github.com/owner/repo/issues/42#issuecomment-102",
            "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
        },
    ]

    transport = FakeTransport()
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues/comments?per_page=100",
        HttpResponse(
            200,
            {"etag": 'W/"etag-200"', "link": ""},
            json.dumps(comments_json).encode("utf-8"),
        ),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    res = backend.list_messages()

    assert res.not_modified is False
    assert res.etag == 'W/"etag-200"'
    assert len(res.messages) == 1
    parsed_msg = res.messages[0]
    assert parsed_msg.id == "01MSG001"
    assert parsed_msg.content == "Parsed body"
    assert getattr(parsed_msg, "issue_number") == 42
    assert getattr(parsed_msg, "comment_url") == "https://github.com/owner/repo/issues/42#issuecomment-101"


def test_list_messages_401_authentication_error():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues/comments?per_page=100",
        HttpResponse(401, {}, b'{"message": "Bad credentials"}'),
    )

    backend = GitHubBackend("owner/repo", token="bad_token", transport=transport)
    with pytest.raises(AuthenticationError, match="HTTP 401"):
        backend.list_messages()


def test_post_message_403_rate_limit_with_retry_after():
    msg = _make_msg(channel="phase-1")
    transport = FakeTransport()

    # Channel exists (issue #10)
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(200, {}, json.dumps([{"number": 10, "title": "phase-1"}]).encode("utf-8")),
    )
    # Recent comments empty
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=30",
        HttpResponse(200, {}, b"[]"),
    )
    # Posting comment hits secondary rate limit 403
    transport.set_route(
        "POST",
        "https://api.github.com/repos/owner/repo/issues/10/comments",
        HttpResponse(
            403,
            {"retry-after": "42"},
            b'{"message": "You have exceeded a secondary rate limit"}',
        ),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(RateLimitError) as exc_info:
        backend.post_message("phase-1", msg)

    assert exc_info.value.retry_after == 42
    assert "retry-after: 42s" in str(exc_info.value)


def test_post_message_idempotency_avoids_duplicate():
    msg = _make_msg(channel="phase-1", msg_id="01MSG_IDEMPOTENT_TEST")
    serialized = serialize_message(msg)

    transport = FakeTransport()
    # Channel exists (issue #10)
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(200, {}, json.dumps([{"number": 10, "title": "phase-1"}]).encode("utf-8")),
    )
    # Recent comments already has this message id
    existing_comments = [
        {
            "id": 999,
            "body": serialized,
            "html_url": "https://github.com/owner/repo/issues/10#issuecomment-999",
        }
    ]
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=30",
        HttpResponse(200, {}, json.dumps(existing_comments).encode("utf-8")),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    url = backend.post_message("phase-1", msg)

    assert url == "https://github.com/owner/repo/issues/10#issuecomment-999"
    # Ensure no POST was made
    post_requests = [r for r in transport.recorded_requests if r["method"] == "POST"]
    assert len(post_requests) == 0


def test_create_and_find_channel():
    transport = FakeTransport()
    # Initially no issues
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(200, {}, b"[]"),
    )
    # Creating channel issue returns #55
    transport.set_route(
        "POST",
        "https://api.github.com/repos/owner/repo/issues",
        HttpResponse(201, {}, json.dumps({"number": 55, "title": "new-topic"}).encode("utf-8")),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    assert backend.find_channel("new-topic") is None

    num = backend.create_channel("new-topic")
    assert num == 55
    assert backend.find_channel("new-topic") == 55


def test_post_message_channel_not_found():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(200, {}, b"[]"),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    msg = _make_msg(channel="missing-topic")

    with pytest.raises(ChannelNotFoundError, match="Use --new to create it"):
        backend.post_message("missing-topic", msg)


def test_pagination_follows_next_link():
    msg1 = _make_msg(content="page 1 msg", msg_id="01MSG_P1")
    msg2 = _make_msg(content="page 2 msg", msg_id="01MSG_P2")

    c1 = [{"id": 1, "body": serialize_message(msg1), "issue_url": "https://api.github.com/repos/owner/repo/issues/1"}]
    c2 = [{"id": 2, "body": serialize_message(msg2), "issue_url": "https://api.github.com/repos/owner/repo/issues/1"}]

    page1_url = "https://api.github.com/repos/owner/repo/issues/comments?per_page=100"
    page2_url = "https://api.github.com/repos/owner/repo/issues/comments?per_page=100&page=2"

    transport = FakeTransport()
    transport.set_route(
        "GET",
        page1_url,
        HttpResponse(
            200,
            {"etag": 'W/"p1"', "link": f'<{page2_url}>; rel="next"'},
            json.dumps(c1).encode("utf-8"),
        ),
    )
    transport.set_route(
        "GET",
        page2_url,
        HttpResponse(200, {"etag": 'W/"p2"'}, json.dumps(c2).encode("utf-8")),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    res = backend.list_messages()
    assert len(res.messages) == 2
    assert [m.id for m in res.messages] == ["01MSG_P1", "01MSG_P2"]


def test_token_never_appears_in_exception_messages():
    secret_token = "ghp_VERY_SECRET_PAT_987654321"

    class ErrorTransport:
        def request(self, method, url, headers=None, body=None):
            raise urllib.error.URLError(f"Connection failed with token {secret_token}")

    backend = GitHubBackend("owner/repo", token=secret_token, transport=ErrorTransport())
    try:
        backend.list_messages()
    except Exception as e:
        err_str = str(e)
        assert secret_token not in err_str
        assert "***" in err_str


def test_list_channels_raises_backend_error_on_http_error():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(500, {}, b'{"message": "Internal Server Error"}'),
    )
    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(BackendError, match="HTTP 500"):
        backend.list_channels()


def test_list_messages_pagination_subsequent_page_failure_raises_backend_error():
    msg1 = _make_msg(content="page 1 msg", msg_id="01MSG_P1")
    c1 = [{"id": 1, "body": serialize_message(msg1), "issue_url": "https://api.github.com/repos/owner/repo/issues/1"}]
    page1_url = "https://api.github.com/repos/owner/repo/issues/comments?per_page=100"
    page2_url = "https://api.github.com/repos/owner/repo/issues/comments?per_page=100&page=2"

    transport = FakeTransport()
    transport.set_route(
        "GET",
        page1_url,
        HttpResponse(
            200,
            {"etag": 'W/"p1"', "link": f'<{page2_url}>; rel="next"'},
            json.dumps(c1).encode("utf-8"),
        ),
    )
    transport.set_route(
        "GET",
        page2_url,
        HttpResponse(500, {}, b'{"message": "Internal Server Error"}'),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(BackendError, match="HTTP 500"):
        backend.list_messages()


def test_post_message_body_mentioning_id_does_not_swallow_new_message():
    target_msg = _make_msg(channel="phase-1", msg_id="01TARGET_MSG_ID", content="Actual target message")
    other_msg = _make_msg(
        channel="phase-1",
        msg_id="01OTHER_MSG_ID",
        content="Discussing another message: id: 01TARGET_MSG_ID in body text",
    )
    serialized_other = serialize_message(other_msg)

    transport = FakeTransport()
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(200, {}, json.dumps([{"number": 10, "title": "phase-1"}]).encode("utf-8")),
    )
    # Existing comments has a comment mentioning 01TARGET_MSG_ID in body, but front matter id is 01OTHER_MSG_ID
    existing_comments = [
        {
            "id": 501,
            "body": serialized_other,
            "html_url": "https://github.com/owner/repo/issues/10#issuecomment-501",
        }
    ]
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=30",
        HttpResponse(200, {}, json.dumps(existing_comments).encode("utf-8")),
    )
    # Expected POST for the actual target message
    transport.set_route(
        "POST",
        "https://api.github.com/repos/owner/repo/issues/10/comments",
        HttpResponse(201, {}, json.dumps({"html_url": "https://github.com/owner/repo/issues/10#issuecomment-502"}).encode("utf-8")),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    url = backend.post_message("phase-1", target_msg)
    assert url == "https://github.com/owner/repo/issues/10#issuecomment-502"
    post_requests = [r for r in transport.recorded_requests if r["method"] == "POST"]
    assert len(post_requests) == 1


def test_post_message_duplicate_detection_locates_last_page_when_over_30_comments():
    msg_35 = _make_msg(channel="phase-1", msg_id="01MSG_NUM_35", content="Message 35")
    serialized_35 = serialize_message(msg_35)

    transport = FakeTransport()
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100",
        HttpResponse(200, {}, json.dumps([{"number": 10, "title": "phase-1"}]).encode("utf-8")),
    )
    # Page 1 has 30 older comments
    older_comments = [
        {
            "id": i,
            "body": serialize_message(_make_msg(channel="phase-1", msg_id=f"01OLD_MSG_{i}", content=f"Old {i}")),
            "html_url": f"https://github.com/owner/repo/issues/10#issuecomment-{i}",
        }
        for i in range(1, 31)
    ]
    last_page_url = "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=30&page=2"
    transport.set_route(
        "GET",
        "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=30",
        HttpResponse(
            200,
            {"link": f'<{last_page_url}>; rel="last", <{last_page_url}>; rel="next"'},
            json.dumps(older_comments).encode("utf-8"),
        ),
    )
    # Page 2 has comments 31 to 35, including msg_35
    page2_comments = [
        {
            "id": 35,
            "body": serialized_35,
            "html_url": "https://github.com/owner/repo/issues/10#issuecomment-35",
        }
    ]
    transport.set_route(
        "GET",
        last_page_url,
        HttpResponse(200, {}, json.dumps(page2_comments).encode("utf-8")),
    )

    backend = GitHubBackend("owner/repo", token="token123", transport=transport)
    url = backend.post_message("phase-1", msg_35)
    # Should find msg_35 on the last page and NOT issue a POST
    assert url == "https://github.com/owner/repo/issues/10#issuecomment-35"
    post_requests = [r for r in transport.recorded_requests if r["method"] == "POST"]
    assert len(post_requests) == 0

