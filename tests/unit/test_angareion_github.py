"""Unit tests for GitHubIssuesBackend: fake transport only, no network (D2/D4)."""

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
)
from angareion.config import Config
from angareion.github import (
    GitHubIssuesBackend,
    HttpResponse,
    parse_channel_members,
    serialize_channel_body,
)
from angareion.messages import Message, parse_message, serialize_message

ISSUES_URL = "https://api.github.com/repos/owner/repo/issues?state=all&sort=created&direction=asc&per_page=100"
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


def _comment(
    msg: Message,
    *,
    issue_number: int = 10,
    comment_id: int = 101,
    created_at: str = "2026-10-10T12:00:00Z",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": serialize_message(msg),
        "created_at": created_at,
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
        timeout: float | None = None,
    ) -> HttpResponse:
        self.recorded_requests.append(
            {"method": method, "url": url, "headers": headers or {}, "body": body, "timeout": timeout}
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
    payload = json.loads(post["body"].decode("utf-8"))
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


def test_duplicate_channel_names_resolve_to_the_oldest_issue():
    """L4/D2: issues are listed oldest first; the first title match wins and the scan stops."""
    transport = FakeTransport()
    _issues_route(transport, [
        {"number": 3, "title": "topic", "body": serialize_channel_body("topic", [])},
        {"number": 7, "title": "topic", "body": ""},
    ])

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    assert backend.find_channel("topic") == 3
    assert backend.create_channel("topic", []).number == 3
    assert [r for r in transport.recorded_requests if r["method"] == "POST"] == []


def test_create_channel_race_rescans_and_returns_the_older_issue():
    class RaceTransport:
        def __init__(self):
            self.list_calls = 0
            self.posts = 0

        def request(self, method, url, headers=None, body=None, timeout=None):
            if method == "GET" and "/issues?" in url:
                self.list_calls += 1
                if self.list_calls == 1:
                    return HttpResponse(200, {}, b"[]")
                # The racing process's issue (4) is older than ours (5).
                return HttpResponse(200, {}, json.dumps([
                    {"number": 4, "title": "topic", "body": serialize_channel_body("topic", [])},
                    {"number": 5, "title": "topic", "body": ""},
                ]).encode("utf-8"))
            if method == "POST" and url.endswith("/repos/owner/repo/issues"):
                self.posts += 1
                return HttpResponse(201, {}, json.dumps({"number": 5, "title": "topic", "body": ""}).encode("utf-8"))
            return HttpResponse(404, {}, b'{"message": "Not Found"}')

    transport = RaceTransport()
    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    channel = backend.create_channel("topic", [])

    assert channel.number == 4  # the older issue wins, not the one we just created (5)
    assert transport.posts == 1
    assert transport.list_calls == 2  # initial lookup plus the post-create re-scan


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
    body = json.loads(post["body"].decode("utf-8"))["body"]
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


def test_check_uses_in_memory_etag_for_a_single_page_and_never_touches_the_cursor():
    secret_token = "ghp_FAKE_CHECK_TOKEN_123"

    class ETagTransport:
        def __init__(self):
            self.comments_headers: list[dict[str, str]] = []

        def request(self, method, url, headers=None, body=None, timeout=None):
            if "/issues/10/comments" in url:
                self.comments_headers.append(dict(headers or {}))
                if headers and headers.get("If-None-Match") == 'W/"etag-1"':
                    return HttpResponse(304, {"etag": 'W/"etag-1"'}, b"")
                return HttpResponse(200, {"etag": 'W/"etag-1"'}, b"[]")
            return HttpResponse(200, {}, json.dumps([{"number": 10, "title": "general", "body": ""}]).encode("utf-8"))

    transport = ETagTransport()
    backend = GitHubIssuesBackend("owner/repo", token=secret_token, transport=transport)
    target = Target(channel="general")
    cursor = Cursor(position="100", at="2026-10-10T10:00:00Z", seen=["100"])

    assert backend.check(target, cursor) is False
    assert cursor == Cursor(position="100", at="2026-10-10T10:00:00Z", seen=["100"])
    assert backend.check(target, cursor) is False

    assert transport.comments_headers[0].get("If-None-Match") is None
    assert transport.comments_headers[1]["If-None-Match"] == 'W/"etag-1"'
    # The ETag stays in this process: nothing was written anywhere.
    assert backend._check_etags[target.key()] == 'W/"etag-1"'


def test_check_finds_new_message_past_the_position():
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {}, json.dumps([_comment(_make_msg(msg_id="01BBBB"), comment_id=101, created_at="2026-10-10T10:00:05Z")]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(channel="general")

    assert backend.check(target, Cursor(position="100", at="2026-10-10T10:00:00Z", seen=["100"])) is True
    assert backend.check(target, Cursor(position="101", at="2026-10-10T10:00:05Z", seen=["101"])) is False

    # The since query uses at - margin (D4), quoted, for a local cursor.
    comments_get = [r for r in transport.recorded_requests if "/issues/10/comments" in r["url"]][0]
    assert "since=2026-10-10T09%3A59%3A00Z" in comments_get["url"]


def test_check_to_me_uses_the_repo_level_listing_and_filters_recipient():
    me = {"repo": "owner-org/worker", "role": "worker"}
    mine = _make_msg(msg_id="01AAAA", to=me)
    other = _make_msg(msg_id="01BBBB", to={"repo": "owner-org/other", "role": "worker"})
    transport = FakeTransport()
    transport.set_route(
        "GET",
        REPO_COMMENTS_URL,
        HttpResponse(200, {}, json.dumps([
            _comment(mine, comment_id=50, created_at="2026-10-10T10:00:00Z"),
            _comment(other, comment_id=60, created_at="2026-10-10T11:00:00Z"),
        ]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(recipient=me)

    assert backend.check(target, Cursor()) is True
    # comment 60 is newer but not addressed to me: not new for me.
    assert backend.check(target, Cursor(position="50", at="2026-10-10T10:00:00Z", seen=["50"])) is False
    assert backend.check(target, Cursor(position="60", at="2026-10-10T11:00:00Z", seen=["60"])) is False

    urls = [r["url"] for r in transport.recorded_requests]
    assert urls and all("/issues/comments" in u for u in urls)
    assert all("/issues/10/comments" not in u for u in urls)


def test_check_does_not_trust_a_multi_page_etag():
    """M1: page 1 unchanged, my message on page 2: check must still say True."""
    me = {"repo": "owner-org/worker", "role": "worker"}
    page2_url = REPO_COMMENTS_URL + "&page=2"
    others_page1 = [
        _comment(_make_msg(msg_id=f"01OTHER{i:02d}", to={"repo": "owner-org/other", "role": "worker"}),
                 comment_id=100 + i, created_at=f"2026-10-10T10:00:{i:02d}Z")
        for i in range(20)
    ]
    mine = _comment(_make_msg(msg_id="01MINE", to=me), comment_id=200, created_at="2026-10-10T11:00:00Z")

    class MultiPageTransport:
        def __init__(self):
            self.state = 1  # 1: page 2 has nothing; 2: page 2 has my message
            self.if_none_match: list[str | None] = []

        def request(self, method, url, headers=None, body=None, timeout=None):
            if url.startswith(REPO_COMMENTS_URL) and "page=2" in url:
                page2 = [mine] if self.state == 2 else []
                return HttpResponse(200, {}, json.dumps(page2).encode("utf-8"))
            if url.startswith(REPO_COMMENTS_URL):
                self.if_none_match.append((headers or {}).get("If-None-Match"))
                return HttpResponse(200, {"etag": 'W/"page1-unchanged"', "link": f'<{page2_url}>; rel="next"'},
                                    json.dumps(others_page1).encode("utf-8"))
            return HttpResponse(404, {}, b'{"message": "Not Found"}')

    transport = MultiPageTransport()
    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(recipient=me)
    cursor = Cursor(position="100", at="2026-10-10T10:00:00Z", seen=["100"])

    assert backend.check(target, cursor) is False
    transport.state = 2
    assert backend.check(target, cursor) is True
    # Multi-page: no If-None-Match was ever sent, and no ETag was kept.
    assert all(value is None for value in transport.if_none_match)
    assert backend._check_etags[target.key()] is None


def test_check_moves_the_scan_window_forward_within_one_process():
    """D2 rule 3: after a scan, the next query starts at the scanned time - margin."""
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {"etag": 'W/"e"'}, json.dumps([
            _comment(_make_msg(msg_id="01AAAA"), comment_id=100, created_at="2026-10-10T12:00:00Z"),
        ]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(channel="general")
    cursor = Cursor(position="100", at="2026-10-10T09:00:00Z", seen=["100"])

    assert backend.check(target, cursor) is False
    assert backend._scan_since[target.key()] == "2026-10-10T11:59:00Z"
    assert backend.check(target, cursor) is False

    urls = [r["url"] for r in transport.recorded_requests if "/issues/10/comments" in r["url"]]
    assert "since=2026-10-10T08%3A59%3A00Z" in urls[0]  # cursor at - margin
    assert "since=2026-10-10T11%3A59%3A00Z" in urls[1]  # scanned time - margin (scan_since wins)


def test_check_passes_its_timeout_budget_to_the_transport():
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route("GET", CHANNEL_COMMENTS_URL, HttpResponse(200, {}, b"[]"))

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    assert backend.check(Target(channel="general"), Cursor(), timeout=7.5) is False

    comments_get = [r for r in transport.recorded_requests if "/issues/10/comments" in r["url"]][0]
    assert comments_get["timeout"] == 7.5


def test_read_parses_paginates_and_returns_the_backend_cursor():
    msg_a = _make_msg(channel="general", msg_id="01AAAA", content="A")
    msg_b = _make_msg(channel="general", msg_id="01BBBB", content="B")
    page2_url = CHANNEL_COMMENTS_URL + "&page=2"

    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {"link": f'<{page2_url}>; rel="next"'},
                     json.dumps([{"id": 1, "body": "not a message"},
                                 _comment(msg_a, comment_id=10, created_at="2026-10-10T12:00:00Z")]).encode("utf-8")),
    )
    transport.set_route(
        "GET",
        page2_url,
        HttpResponse(200, {}, json.dumps([_comment(msg_b, comment_id=11, created_at="2026-10-10T12:00:01Z")]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    target = Target(channel="general")

    res = backend.read(target, Cursor())
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]  # arrival order
    assert res.cursor == Cursor(position="11", at="2026-10-10T12:00:01Z", seen=["10", "11"])
    assert res.messages[0].received_at == "2026-10-10T12:00:00Z"
    assert getattr(res.messages[0], "issue_number") == 10


def test_read_ignores_the_sender_time_and_uses_the_backend_position():
    """Sender clocks, timezones and ULIDs decide nothing; backend arrival does."""
    ahead_clock = _make_msg(msg_id="01AAAA", at="2026-10-10T23:00:00+00:00")     # sender clock far ahead
    other_tz = _make_msg(msg_id="01BBBB", at="2026-10-10T01:00:00+09:00")      # smaller string, earlier instant
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
            HttpResponse(200, {}, json.dumps([
                _comment(ahead_clock, comment_id=100, created_at="2026-10-10T12:00:00Z"),
                _comment(other_tz, comment_id=101, created_at="2026-10-10T12:00:01Z"),
            ]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    res = backend.read(Target(channel="general"), Cursor(position="99", at="2026-10-10T12:00:10Z", seen=["99"]))
    assert [m.id for m in res.messages] == ["01AAAA", "01BBBB"]
    assert res.cursor == Cursor(position="101", at="2026-10-10T12:00:01Z", seen=["100", "101"])


def test_read_edited_comment_does_not_come_back():
    msg = _make_msg(msg_id="01AAAA", content="original")
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {}, json.dumps([_comment(msg, comment_id=10, created_at="2026-10-10T12:00:00Z")]).encode("utf-8")),
    )
    cursor = Cursor(position="10", at="2026-10-10T12:00:00Z", seen=["10"])

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    res = backend.read(Target(channel="general"), cursor)
    assert res.messages == []  # the edited comment reappears in the listing but is not new
    assert res.cursor == cursor


def test_read_delivers_a_late_smaller_id_within_the_window():
    already_read = _make_msg(msg_id="01READ")
    lagging = _make_msg(msg_id="01LAG")
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        # The since query (at - margin) lists the already-read comment 100 too.
        HttpResponse(200, {}, json.dumps([
            _comment(lagging, comment_id=99, created_at="2026-10-10T12:00:30Z"),
            _comment(already_read, comment_id=100, created_at="2026-10-10T12:01:00Z"),
        ]).encode("utf-8")),
    )
    cursor = Cursor(position="100", at="2026-10-10T12:01:00Z", seen=["100"])

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    res = backend.read(Target(channel="general"), cursor)
    assert [m.id for m in res.messages] == ["01LAG"]
    assert res.cursor.position == "100"  # never backwards
    assert res.cursor.at == "2026-10-10T12:01:00Z"
    assert set(res.cursor.seen) == {"99", "100"}


def test_read_with_since_is_inclusive_and_moves_nothing():
    msg = _make_msg(msg_id="01AAAA")
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {}, json.dumps([_comment(msg, comment_id=10, created_at="2026-10-10T12:00:00Z")]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    since_cursor = Cursor(at="2026-10-10T12:00:00Z")
    res = backend.read(Target(channel="general"), since_cursor)

    assert [m.id for m in res.messages] == ["01AAAA"]
    assert res.cursor == since_cursor  # a --since read returns the cursor untouched
    # The since query uses the time as-is (no margin): inclusive boundary.
    comments_get = [r for r in transport.recorded_requests if "/issues/10/comments" in r["url"]][0]
    assert "since=2026-10-10T12%3A00%3A00Z" in comments_get["url"]


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
        def request(self, method, url, headers=None, body=None, timeout=None):
            raise urllib.error.URLError(f"Connection failed with token {secret_token}")

    backend = GitHubIssuesBackend("owner/repo", token=secret_token, transport=ErrorTransport())
    with pytest.raises(BackendError) as excinfo:
        backend.read(Target(recipient={"repo": "a", "role": "b"}), Cursor())
    assert secret_token not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_requests_carry_authorization_and_api_headers():
    """The review's missing test: every request carries the API headers and the token."""
    secret = "ghp_FAKE_HEADER_TOKEN_42"
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "POST",
        CHANNEL_COMMENTS_URL,
        HttpResponse(201, {}, json.dumps({"html_url": "https://github.com/owner/repo/issues/10#issuecomment-1"}).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token=secret, transport=transport)
    backend.send("general", _make_msg())

    assert len(transport.recorded_requests) == 2  # the issues listing and the comment POST
    for request in transport.recorded_requests:
        headers = request["headers"]
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert headers["User-Agent"] == "Angareion/0.1.0"
        assert headers["Authorization"] == f"Bearer {secret}"


def test_non_json_response_becomes_backend_error():
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route("GET", CHANNEL_COMMENTS_URL, HttpResponse(200, {}, b"<html>proxy login</html>"))

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(BackendError, match="non-JSON"):
        backend.read(Target(channel="general"), Cursor())
    with pytest.raises(BackendError, match="HTTP 200"):
        backend.check(Target(channel="general"), Cursor())


def test_default_transport_passes_a_fixed_socket_timeout(monkeypatch):
    from angareion.github import HTTP_TIMEOUT_SECONDS, DefaultHttpTransport

    calls = []

    class OkResponse:
        status = 200
        headers = {}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    transport = DefaultHttpTransport()

    def fake_open(req, timeout=None):
        calls.append(timeout)
        return OkResponse()

    monkeypatch.setattr(transport._opener, "open", fake_open)

    assert transport.request("GET", "https://api.github.com/x").status == 200
    assert HTTP_TIMEOUT_SECONDS == 30.0
    assert calls == [30.0]

    # An explicit per-request budget overrides the fixed timeout.
    transport.request("GET", "https://api.github.com/x", timeout=3.5)
    assert calls == [30.0, 3.5]


def test_default_transport_timeout_becomes_backend_error(monkeypatch):
    from angareion.github import DefaultHttpTransport

    transport = DefaultHttpTransport()

    def timed_out(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(transport._opener, "open", timed_out)
    with pytest.raises(BackendError, match="timed out"):
        transport.request("GET", "https://api.github.com/x")

    def url_error_wrapping_timeout(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(transport._opener, "open", url_error_wrapping_timeout)
    with pytest.raises(BackendError, match="timed out"):
        transport.request("GET", "https://api.github.com/x")


def test_default_transport_refuses_to_follow_redirects(monkeypatch):
    import urllib.error

    from angareion.github import DefaultHttpTransport, _NoRedirectHandler

    handler = _NoRedirectHandler()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None

    def redirecting_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/"}, None)

    transport = DefaultHttpTransport()
    monkeypatch.setattr(transport._opener, "open", redirecting_open)

    # The redirect is surfaced, not followed …
    assert transport.request("GET", "https://api.github.com/repos/owner/repo/issues").status == 302

    # … and the backend turns it into an error instead of parsing the body.
    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(BackendError, match="redirect"):
        backend.read(Target(recipient={"repo": "a", "role": "b"}), Cursor())


def test_next_page_link_outside_the_base_url_is_refused():
    msg = _make_msg(msg_id="01AAAA")
    evil_page2 = "https://evil.example/repos/owner/repo/issues/comments?per_page=100&page=2"
    transport = FakeTransport()
    _issues_route(transport, [{"number": 10, "title": "general", "body": ""}])
    transport.set_route(
        "GET",
        CHANNEL_COMMENTS_URL,
        HttpResponse(200, {"link": f'<{evil_page2}>; rel="next"'}, json.dumps([_comment(msg)]).encode("utf-8")),
    )

    backend = GitHubIssuesBackend("owner/repo", token="token123", transport=transport)
    with pytest.raises(BackendError, match="pagination link"):
        backend.read(Target(channel="general"), Cursor())
