"""Unit tests for GitHubIssuesBackend: fake transport only, no network (D2/D4/v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from typing import Any

import pytest

from _angareion_fakes import StaticResolver, entity
from angareion.backend import (
    AuthenticationError,
    BackendError,
    BackendRef,
    ChannelBackend,
    Cursor,
)
from angareion.config import Config
from angareion.github import GitHubIssuesBackend, HttpResponse
from angareion.messages import Message, serialize_message

ISSUE = 10
COMMENTS_URL = f"https://api.github.com/repos/owner/repo/issues/{ISSUE}/comments?per_page=100"
POST_COMMENTS_URL = f"https://api.github.com/repos/owner/repo/issues/{ISSUE}/comments"
ISSUES_URL = "https://api.github.com/repos/owner/repo/issues"
ISSUE_URL = f"https://api.github.com/repos/owner/repo/issues/{ISSUE}"
OTHER_COMMENTS_URL = "https://api.github.com/repos/owner/repo2/issues/7/comments?per_page=100"
OTHER_POST_COMMENTS_URL = "https://api.github.com/repos/owner/repo2/issues/7/comments"

ME = "MyLinuxPool-impl-2"
PM = "LLMGateway-pm"
GENERAL = entity(channel="general", issue=ISSUE)
OTHER = entity(channel="other", backend="github-2", repo="owner/repo2", issue=7)
BACKEND_MAIN = BackendRef(name="github-main", type="github", repo="owner/repo")


def _make_msg(
    channel: str = "general",
    content: str = "Hello GitHub",
    msg_id: str = "01JABCDEF0123456789ABCDEF0",
    at: str = "2026-10-10T12:00:00+00:00",
    to: str | None = ME,
) -> Message:
    return Message(from_=PM, to=to, channel=channel, content=content, id=msg_id, at=at)


def _comment(
    msg: Message,
    *,
    comment_id: int = 101,
    created_at: str = "2026-10-10T12:00:00Z",
    repo: str = "owner/repo",
    issue: int = ISSUE,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": serialize_message(msg),
        "created_at": created_at,
        "html_url": f"https://github.com/{repo}/issues/{issue}#issuecomment-{comment_id}",
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


def _backend(transport: FakeTransport, *, token: str | None = "fake_token") -> GitHubIssuesBackend:
    return GitHubIssuesBackend(StaticResolver([GENERAL, OTHER]), token=token, transport=transport)


def test_github_backend_conforms_to_protocol():
    assert isinstance(_backend(FakeTransport()), ChannelBackend)


def test_from_config_reads_token_and_builds_backend(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("ghp_FAKE_TRANSPORT_TOKEN\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = Config(
        identity=ME,
        config_dir=tmp_path,
        registry_repo="owner/MyAiStorage",
        registry_issue=1,
        token_file=token_file,
    )
    backend = GitHubIssuesBackend.from_config(config, StaticResolver([GENERAL]), transport=FakeTransport())
    assert backend.token == "ghp_FAKE_TRANSPORT_TOKEN"


def test_create_channel_opens_an_issue_without_members():
    transport = FakeTransport()
    transport.set_route(
        "POST", ISSUES_URL, HttpResponse(201, {}, json.dumps({"number": 12, "title": "fresh"}).encode("utf-8"))
    )
    backend = _backend(transport)

    assert backend.create_channel("fresh", BACKEND_MAIN) == 12
    request = transport.recorded_requests[-1]
    payload = json.loads(request["body"].decode("utf-8"))
    assert payload["title"] == "fresh"
    assert "members" not in payload["body"]  # v5 entities store no member list
    assert "members" not in json.dumps(payload)


def test_create_channel_failures_are_backend_errors():
    transport = FakeTransport()
    transport.set_route("POST", ISSUES_URL, HttpResponse(403, {}, b'{"message": "forbidden"}'))
    with pytest.raises(BackendError):
        _backend(transport).create_channel("fresh", BACKEND_MAIN)

    transport = FakeTransport()
    transport.set_route("POST", ISSUES_URL, HttpResponse(201, {}, b'{"title": "fresh"}'))
    with pytest.raises(BackendError, match="no issue number"):
        _backend(transport).create_channel("fresh", BACKEND_MAIN)


def test_verify_channel_checks_existence_and_the_exact_title():
    transport = FakeTransport()
    transport.set_route("GET", ISSUE_URL, HttpResponse(200, {}, json.dumps({"number": ISSUE, "title": "general"}).encode("utf-8")))
    assert _backend(transport).verify_channel("general", BACKEND_MAIN, ISSUE) is True

    transport.set_route("GET", ISSUE_URL, HttpResponse(200, {}, json.dumps({"number": ISSUE, "title": "general-renamed"}).encode("utf-8")))
    assert _backend(transport).verify_channel("general", BACKEND_MAIN, ISSUE) is False

    transport.set_route("GET", ISSUE_URL, HttpResponse(404, {}, b'{"message": "Not Found"}'))
    assert _backend(transport).verify_channel("general", BACKEND_MAIN, ISSUE) is False

    transport.set_route("GET", ISSUE_URL, HttpResponse(500, {}, b'{"message": "boom"}'))
    with pytest.raises(BackendError, match="verify"):
        _backend(transport).verify_channel("general", BACKEND_MAIN, ISSUE)


def test_close_channel_closes_without_deleting():
    transport = FakeTransport()
    transport.set_route("PATCH", ISSUE_URL, HttpResponse(200, {}, json.dumps({"state": "closed"}).encode("utf-8")))
    _backend(transport).close_channel(GENERAL)
    request = transport.recorded_requests[-1]
    assert request["method"] == "PATCH"
    assert json.loads(request["body"].decode("utf-8")) == {"state": "closed"}

    # 404: the entity is already deleted; treated as already closed.
    transport.set_route("PATCH", ISSUE_URL, HttpResponse(404, {}, b'{"message": "Not Found"}'))
    _backend(transport).close_channel(GENERAL)

    # Anything else fails (no unregister comment may follow).
    transport.set_route("PATCH", ISSUE_URL, HttpResponse(500, {}, b'{"message": "boom"}'))
    with pytest.raises(BackendError, match="close"):
        _backend(transport).close_channel(GENERAL)


def test_send_posts_a_comment_on_the_configured_entity():
    transport = FakeTransport()
    transport.set_route("POST", COMMENTS_URL, HttpResponse(201, {}, b'{"id": 1}'))
    backend = _backend(transport)

    message = _make_msg()
    assert backend.send(GENERAL, message) == message.id
    request = transport.recorded_requests[-1]
    assert request["url"] == POST_COMMENTS_URL
    assert json.loads(request["body"].decode("utf-8"))["body"] == serialize_message(message)

    with pytest.raises(ValueError, match="does not match target channel"):
        backend.send(GENERAL, _make_msg(channel="other"))


def test_send_uses_the_entity_repo():
    transport = FakeTransport()
    transport.set_route("POST", OTHER_POST_COMMENTS_URL, HttpResponse(201, {}, b'{"id": 1}'))
    _backend(transport).send(OTHER, _make_msg(channel="other"))
    assert transport.recorded_requests[-1]["url"] == OTHER_POST_COMMENTS_URL


def test_read_filters_recipient_and_includes_broadcasts():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL,
        HttpResponse(
            200,
            {},
            json.dumps(
                [
                    _comment(_make_msg(msg_id="01AAAA"), comment_id=101, created_at="2026-10-10T12:00:01Z"),
                    _comment(_make_msg(msg_id="01BBBB", to="other-pm"), comment_id=102, created_at="2026-10-10T12:00:02Z"),
                    _comment(_make_msg(msg_id="01CCCC", to=None), comment_id=103, created_at="2026-10-10T12:00:03Z"),
                    {"id": 104, "body": "just a comment", "created_at": "2026-10-10T12:00:04Z"},
                ]
            ).encode("utf-8"),
        ),
    )
    backend = _backend(transport)

    result = backend.read(GENERAL, Cursor(), recipient=ME)
    assert [m.id for m in result.messages] == ["01AAAA", "01CCCC"]
    assert all(m.received_at for m in result.messages)

    result_channel = backend.read(GENERAL, Cursor())
    assert [m.id for m in result_channel.messages] == ["01AAAA", "01BBBB", "01CCCC"]


def test_read_follows_pagination():
    page2_url = COMMENTS_URL + "&page=2"
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL,
        HttpResponse(
            200,
            {"link": f'<{page2_url}>; rel="next"'},
            json.dumps([_comment(_make_msg(msg_id="01AAAA"), comment_id=101, created_at="2026-10-10T12:00:01Z")]).encode("utf-8"),
        ),
    )
    transport.set_route(
        "GET",
        page2_url,
        HttpResponse(
            200,
            {},
            json.dumps([_comment(_make_msg(msg_id="01BBBB"), comment_id=102, created_at="2026-10-10T12:00:02Z")]).encode("utf-8"),
        ),
    )
    result = _backend(transport).read(GENERAL, Cursor())
    assert [m.id for m in result.messages] == ["01AAAA", "01BBBB"]
    assert result.cursor.position == "102"


def test_read_refuses_a_pagination_link_off_host():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL,
        HttpResponse(200, {"link": '<https://evil.example.com/x>; rel="next"'}, b"[]"),
    )
    with pytest.raises(BackendError, match="pagination link outside"):
        _backend(transport).read(GENERAL, Cursor())


def test_read_since_is_inclusive_and_moves_nothing():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL + "&since=2026-10-10T12%3A00%3A00Z",
        HttpResponse(
            200,
            {},
            json.dumps([_comment(_make_msg(msg_id="01AAAA"), comment_id=101, created_at="2026-10-10T12:00:00Z")]).encode("utf-8"),
        ),
    )
    since = Cursor(at="2026-10-10T12:00:00Z")
    result = _backend(transport).read(GENERAL, since)
    assert [m.id for m in result.messages] == ["01AAAA"]
    assert result.cursor == since  # `--since` never moves the cursor


def test_check_finds_new_messages_and_passes_the_timeout_budget():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL,
        HttpResponse(
            200,
            {"etag": 'W/"x"'},
            json.dumps([_comment(_make_msg(msg_id="01AAAA"), comment_id=101, created_at="2026-10-10T12:00:01Z")]).encode("utf-8"),
        ),
    )
    backend = _backend(transport)
    assert backend.check(GENERAL, Cursor(), timeout=5.0) is True
    assert transport.recorded_requests[-1]["timeout"] == 5.0

    # With the cursor on it, the same comment is not new.
    cursor = Cursor(position="101", at="2026-10-10T12:00:01Z", seen=["101"])
    assert backend.check(GENERAL, cursor) is False


def test_check_does_not_trust_a_multi_page_etag():
    """M1: a 304 on page 1 must not hide a new page-2 message."""
    page2_url = COMMENTS_URL + "&page=2"
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL,
        HttpResponse(
            200,
            {"etag": 'W/"page-1-unchanged"', "link": f'<{page2_url}>; rel="next"'},
            json.dumps([_comment(_make_msg(msg_id="01AAAA", to="other-pm"), comment_id=101, created_at="2026-10-10T12:00:01Z")]).encode("utf-8"),
        ),
    )
    transport.set_route(
        "GET",
        page2_url,
        HttpResponse(
            200,
            {},
            json.dumps([_comment(_make_msg(msg_id="01BBBB"), comment_id=102, created_at="2026-10-10T12:00:02Z")]).encode("utf-8"),
        ),
    )
    backend = _backend(transport)
    assert backend.check(GENERAL, Cursor(), recipient=ME) is True
    # The page-1 ETag was never stored or sent back (it speaks for page 1 only).
    assert all("If-None-Match" not in request["headers"] for request in transport.recorded_requests)


def test_check_treats_304_as_nothing_new():
    transport = FakeTransport()
    transport.set_route("GET", COMMENTS_URL, HttpResponse(304, {}, b""))
    assert _backend(transport).check(GENERAL, Cursor()) is False


def test_check_respects_the_recipient_filter():
    transport = FakeTransport()
    transport.set_route(
        "GET",
        COMMENTS_URL,
        HttpResponse(
            200,
            {},
            json.dumps([_comment(_make_msg(msg_id="01AAAA", to="other-pm"), comment_id=101, created_at="2026-10-10T12:00:01Z")]).encode("utf-8"),
        ),
    )
    assert _backend(transport).check(GENERAL, Cursor(), recipient=ME) is False
    assert _backend(transport).check(GENERAL, Cursor()) is True


def test_backend_errors_401_403_non_json_and_redirect():
    transport = FakeTransport()
    transport.set_route("GET", COMMENTS_URL, HttpResponse(401, {}, b'{"message": "bad credentials"}'))
    with pytest.raises(AuthenticationError):
        _backend(transport).read(GENERAL, Cursor())

    transport = FakeTransport()
    transport.set_route("GET", COMMENTS_URL, HttpResponse(403, {}, b'{"message": "secondary rate limit"}'))
    with pytest.raises(BackendError, match="not retrying"):
        _backend(transport).read(GENERAL, Cursor())

    transport = FakeTransport()
    transport.set_route("GET", COMMENTS_URL, HttpResponse(200, {}, b"<html>proxy login page</html>"))
    with pytest.raises(BackendError, match="non-JSON"):
        _backend(transport).read(GENERAL, Cursor())

    transport = FakeTransport()
    transport.set_route("GET", COMMENTS_URL, HttpResponse(302, {"location": "https://evil.example.com"}, b""))
    with pytest.raises(BackendError, match="redirected"):
        _backend(transport).read(GENERAL, Cursor())


def test_the_token_never_leaks_into_errors():
    secret = "ghp_SUPER_SECRET_TOKEN"
    transport = FakeTransport()
    transport.set_route("GET", COMMENTS_URL, HttpResponse(403, {}, f'{{"message": "{secret}"}}'.encode("utf-8")))
    backend = GitHubIssuesBackend(StaticResolver([GENERAL]), token=secret, transport=transport)
    with pytest.raises(BackendError) as excinfo:
        backend.read(GENERAL, Cursor())
    assert secret not in str(excinfo.value)
