"""Unit tests for GitHubIssuesBackend: fake transport only, no network (D2)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from typing import Any
import urllib.error

import pytest

from angareion.backend import (
    AuthenticationError,
    BackendError,
    ChannelBackend,
    ChannelNotFoundError,
    Cursor,
    Target,
    address_matches_identity,
)
from angareion.config import Config
from angareion.github import (
    GitHubIssuesBackend,
    HttpResponse,
    parse_channel_members,
    serialize_channel_body,
)
from angareion.messages import Message, parse_message, serialize_message

ISSUES_URL = "https://api.github.com/repos/owner/repo/issues?state=all&per_page=100"
CHANNEL_COMMENTS_URL = "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=100"
REPO_COMMENTS_URL = "https://api.github.com/repos/owner/repo/issues/comments?per_page=100"


def _make_msg(
    channel: str = "general",
    content: str = "Hello GitHub",
    msg_id: str = "01JABCDEF0123456789ABCDEF0",
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


def _comment(msg: Message, *, issue_number: int = 10, comment_id: int = 101) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": serialize_message(msg),
        "html_url": f"https://github.com/owner/repo/issues/{issue_number}#issuecomment-{comment_id}",
        "issue_url": f"https://api.github.com/repos/owner/repo/issues/{issue_number}",
    }


class FakeTransport:
    """Route-based fake HTTP transport; records every request."""

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
        for (r_method, r_url), resp in self.routes.items():
            if r_method == method and (url.startswith(r_url) or r_url.startswith(url)):
                return resp
        return HttpResponse(404, {}, b'{"message": "Not Found"}')


def _issues_route(transport: FakeTransport, issues: list[dict[str, Any]]) -> None:
    transport.set_route("GET", ISSUES_URL, HttpResponse(200, {}, json.dumps(issues).encode("utf-8")))


def _json(resp: HttpResponse) -> Any:
    return json.loads(resp.body.decode("utf-8"))


def test_github_backend_conforms_to_protocol():
    backend = GitHubIssuesBackend("owner/repo", token="fake_token", transport=FakeTransport())
    assert isinstance(backend, ChannelBackend)


def test_from_config_reads_token_and_builds_backend(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("ghp_FAKE_TRANSPORT_TOKEN\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = Config(
        channel_repo="owner/repo",
        identity={"repo": "owner-org/worker", "role": "worker"},
        token_file=token_file,
    )
    backend = GitHubIssuesBackend.from_config(config, transport=FakeTransport())
    assert backend.channel_repo == "owner/repo"
    assert backend.token == "ghp_FAKE_TRANSPORT_TOKEN"


def test_create_and_find_channel():
    members = [{"repo": "owner-org/team", "role": "PM", "name": "PMO"}]
    transport = FakeTransport()
    _issues_route(transport, [])
    transport.set_route(
        "POST",
        "https://api.github.com/repos/owner/repo/issues",
        HttpResponse(201, {}, json.dumps({"number": 55, "title": "new-topic", "body": ""}).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    assert backend.find_channel("new-topic") is None

    channel = backend.create_channel("new-topic", members)
    assert channel.name == "new-topic"
    assert channel.number == 55
    assert channel.members == members
    assert backend.find_channel("new-topic") == 55

    post = [r for r in transport.recorded_requests if r["method"] == "POST"][0]
    payload = _json(HttpResponse(200, {}, post["body"]))
    assert payload["title"] == "new-topic"
    assert parse_channel_members(payload["body"]) == members


def test_create_channel_existing_is_idempotent_and_reads_members():
    members = [{"repo": "owner-org/team", "role": "PM"}]
    body = serialize_channel_body("topic", members)
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "topic", "body": body}])

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    channel = backend.create_channel("topic", [])
    assert channel == type(channel)(name="topic", number=10, members=members)
    assert [r for r in transport.recorded_requests if r["method"] == "POST"] == []


def test_channel_body_parsing_tolerates_foreign_bodies():
    assert parse_channel_members("not front matter") == []
    assert parse_channel_members("---\nangareion: 1\nmembers: oops\n---\n") == []
    assert serialize_channel_body("c", []).startswith("---\nangareion: 1\n")


def test_send_posts_comment_and_returns_message_id():
    msg = _make_msg(channel="general", content="Parsed body", msg_id="01MSG001")
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "POST",
        CHANNEL_COMMENTS_URL,
        HttpResponse(201, {}, json.dumps({"html_url": "https://github.com/owner/repo/issues/10#issuecomment-1"}).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    assert backend.send("general", msg) == "01MSG001"

    post = [r for r in transport.recorded_requests if r["method"] == "POST"][0]
    body = _json(HttpResponse(200, {}, post["body"]))["body"]
    parsed = parse_message(body, strict=True)
    assert parsed is not None
    assert parsed.id == "01MSG001"
    assert parsed.content == "Parsed body"


def test_send_unknown_channel_raises():
    transport = FakeTransport()
    _issues_route(transport, [])
    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(ChannelNotFoundError, match="does not exist"):
        backend.send("missing", _make_msg(channel="missing"))


def test_send_401_is_authentication_error():
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route("POST", CHANNEL_COMMENTS_URL, HttpResponse(401, {}, b'{"message": "Bad credentials"}'))

    backend = GitHubIssuesBackend("owner/repo", token="bad", transport=transport)
    with pytest.raises(AuthenticationError, match="HTTP 401"):
        backend.send("general", _make_msg())


def test_check_uses_in_memory_etag_and_never_touches_the_cursor():
    secret_token = "ghp_FAKE_CHECK_TOKEN_123"

    class ETagTransport:
        def __init__(self):
            self.comments_headers: list[dict[str, str]] = []

        def request(self, method, url, headers=None, body=None):
            if "/issues/10/comments" in url:
                self.comments_headers.append(dict(headers or {}))
                if headers and headers.get("If-None-Match") == 'W/"etag-1"':
                    return HttpResponse(304, {"etag": 'W/"etag-1"'}, b"")
                return HttpResponse(200, {"etag": 'W/"etag-1"'}, b"[]")
            return HttpResponse(200, {}, json.dumps([{"number": 10, "title": "general", "body": ""}]).encode("utf-8"))

    transport = ETagTransport()
    backend = GitHubIssuesBackend("owner/repo", token=secret_token, transport=transport)
    target = Target(channel="general")
    cursor = Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA")

    assert backend.check(target, cursor) is False
    assert cursor == Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA")
    assert backend.check(target, cursor) is False

    assert transport.comments_headers[0].get("If-None-Match") is None
    assert transport.comments_headers[1]["If-None-Match"] == 'W/"etag-1"'
    # The ETag stays in this process: nothing was written anywhere.
    assert backend._check_etags[target.key()] == 'W/"etag-1"'


def test_check_finds_new_message_after_cursor():
    old = _make_msg(msg_id="01AAAA", at="2026-10-10T10:00:00+00:00")
    new = _make_msg(msg_id="01BBBB", at="2026-10-10T11:00:00+00:00")
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route("GET", CHANNEL_COMMENTS_URL, HttpResponse(200, {}, json.dumps([_comment(old), _comment(new)]).encode("utf-8")))

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(channel="general")

    assert backend.check(target, Cursor(at=old.at, id=old.id)) is True
    assert backend.check(target, Cursor(at=new.at, id=new.id)) is False

    # A `since` query parameter is sent when the cursor carries a timestamp.
    comments_get = [r for r in transport.recorded_requests if "/issues/10/comments" in r["url"]][0]
    assert "since=2026-10-10T10%3A00%3A00%2B00%3A00" in comments_get["url"]


def test_check_to_me_uses_the_repo_level_listing_and_filters_recipient():
    me = {"repo": "owner-org/worker", "role": "worker"}
    mine = _make_msg(msg_id="01AAAA", at="2026-10-10T10:00:00+00:00", to=me)
    other = _make_msg(msg_id="01BBBB", at="2026-10-10T11:00:00+00:00",
                      to={"repo": "owner-org/other", "role": "worker"})
    transport = FakeTransport()
    transport.set_route("GET", REPO_COMMENTS_URL, HttpResponse(200, {}, json.dumps([_comment(mine), _comment(other)]).encode("utf-8")))

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(recipient=me)

    assert backend.check(target, Cursor()) is True
    assert backend.check(target, Cursor(at=other.at, id=other.id)) is False

    urls = [r["url"] for r in transport.recorded_requests]
    assert urls and all("/issues/comments" in u for u in urls)
    assert all("/issues/10/comments" not in u for u in urls)


def test_read_parses_paginates_and_keeps_same_second_messages():
    same_second = "2026-10-10T12:00:00+00:00"
    msg_a = _make_msg(channel="general", msg_id="01AAAA", at=same_second, content="A")
    msg_b = _make_msg(channel="general", msg_id="01BBBB", at=same_second, content="B")
    page2_url = CHANNEL_COMMENTS_URL + "&page=2"

    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {"link": f'<{page2_url}>; rel="next"'},
                     json.dumps([{"id": 1, "body": "not a message"}, _comment(msg_a)]).encode("utf-8")),
    )
    transport.set_route(
        "GET",
        page2_url,
        HttpResponse(200, {}, json.dumps([_comment(msg_b)]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(channel="general")

    # Read everything: both messages, oldest first.
    res = backend.read(target, Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert res.cursor == Cursor(at=same_second, id="01BBBB")

    # Read again from the returned cursor: nothing, even though the server
    # listing still contains both comments (an edited comment must not return).
    again = backend.read(target, res.cursor)
    assert again.messages == []
    assert again.cursor == res.cursor

    # A cursor at the first message still delivers the second one: same second,
    # larger ULID.
    just_b = backend.read(target, Cursor(at=same_second, id="01AAAA"))
    assert [m.id for m in just_b.messages] == ["01BBBB"]


def test_read_channel_not_found():
    transport = FakeTransport()
    _issues_route(transport, [])
    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(ChannelNotFoundError):
        backend.read(Target(channel="missing"), Cursor())


def test_read_pagination_failure_raises_backend_error():
    msg = _make_msg(msg_id="01AAAA")
    page2_url = CHANNEL_COMMENTS_URL + "&page=2"
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {"link": f'<{page2_url}>; rel="next"'}, json.dumps([_comment(msg)]).encode("utf-8")),
    )
    transport.set_route("GET", page2_url, HttpResponse(500, {}, b'{"message": "boom"}'))

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(BackendError, match="HTTP 500"):
        backend.read(Target(channel="general"), Cursor())


def test_token_never_appears_in_exception_messages():
    secret_token = "ghp_VERY_SECRET_PAT_987654321"

    class ErrorTransport:
        def request(self, method, url, headers=None, body=None):
            raise urllib.error.URLError(f"Connection failed with token {secret_token}")

    backend = GitHubIssuesBackend("owner/repo", token=secret_token, transport=ErrorTransport())
    with pytest.raises(BackendError) as excinfo:
        backend.read(Target(recipient={"repo": "a", "role": "b"}), Cursor())
    assert secret_token not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_address_matching_helper_is_used_by_backend_filters():
    me = {"repo": "owner-org/worker", "role": "worker"}
    assert address_matches_identity(_make_msg(to=me).to, me)
