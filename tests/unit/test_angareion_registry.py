"""Unit tests for the registry comment format, the client and the fake (section 3.4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import base64
import json

import pytest

from _angareion_fakes import FakeRegistryClient
from angareion.github import HttpResponse
from angareion.registry import (
    ACTION_SCHEMA,
    Action,
    InvalidAction,
    RegistryClient,
    RegistryError,
    RegistryWriteConflict,
    action_comment,
    parse_comment,
    post_with_retries,
    register_comment,
    unregister_comment,
)

REPO = "owner/MyAiStorage"
ISSUE = 77
TOKEN = "ghp_REGISTRY_TEST_TOKEN"


class RecordingTransport:
    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def request(self, method, url, headers=None, body=None, timeout=None):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        if (method, url) in self.routes:
            return self.routes[(method, url)]
        for (r_method, r_prefix), response in self.routes.items():
            if r_method == method and url.startswith(r_prefix):
                return response
        return HttpResponse(404, {}, b'{"message": "Not Found"}')


def _client(transport) -> RegistryClient:
    return RegistryClient(REPO, ISSUE, token=TOKEN, transport=transport)


def test_action_comment_is_one_line_json():
    body = action_comment("register", "general", "github-main", 123)
    assert "\n" not in body
    assert json.loads(body) == {
        "schema": ACTION_SCHEMA,
        "action": "register",
        "channel": "general",
        "backend": "github-main",
        "issue": 123,
    }
    assert register_comment("general", "github-main", 123) == body
    assert json.loads(unregister_comment("general", "github-main", 123))["action"] == "unregister"


def test_parse_comment_accepts_valid_actions():
    body = register_comment("general", "github-main", 123)
    parsed = parse_comment(body)
    assert parsed == Action(action="register", channel="general", backend="github-main", issue=123)

    # Surrounding whitespace is fine; it is still one line of JSON.
    assert isinstance(parse_comment(f"  {body}  "), Action)
    assert isinstance(parse_comment(unregister_comment("x-1", "b", 1)), Action)


@pytest.mark.parametrize(
    "body",
    [
        "hello, this is just a comment",
        "Angareion registry: could not apply this comment:\n> {broken}",  # the workflow's report
        "",
        None,
        "   ",
    ],
)
def test_parse_comment_ignores_non_actions(body):
    assert parse_comment(body) is None


@pytest.mark.parametrize(
    "body",
    [
        '{"schema": 1,',                                             # broken JSON
        '{"schema": 1, "action": "register", "channel": "x", "backend": "b"}',   # missing issue
        '{"schema": 1, "action": "register", "channel": "x", "backend": "b", "issue": 1, "note": "hi"}',
        '{"schema": 2, "action": "register", "channel": "x", "backend": "b", "issue": 1}',
        '{"schema": 1, "action": "rename", "channel": "x", "backend": "b", "issue": 1}',
        '{"schema": 1, "action": "register", "channel": "小明", "backend": "b", "issue": 1}',
        '{"schema": 1, "action": "register", "channel": "x", "backend": "", "issue": 1}',
        '{"schema": 1, "action": "register", "channel": "x", "backend": "b", "issue": 0}',
        '{"schema": 1, "action": "register", "channel": "x", "backend": "b", "issue": "1"}',
        '{"schema": 1, "action": "register", "channel": "x", "backend": "b", "issue": true}',
    ],
)
def test_parse_comment_flags_action_attempts(body):
    parsed = parse_comment(body)
    assert isinstance(parsed, InvalidAction)
    assert parsed.reason


def test_post_with_retries_retries_a_couple_of_times_then_raises():
    class FlakyPoster:
        def __init__(self, failures):
            self.failures = failures
            self.calls = 0

        def post_comment(self, body):
            self.calls += 1
            if self.calls <= self.failures:
                raise RegistryError("post failed")

    sleeps: list[float] = []
    poster = FlakyPoster(failures=2)
    post_with_retries(poster, "body", sleeper=sleeps.append)
    assert poster.calls == 3  # 1 try + 2 retries
    assert sleeps == [1.0, 1.0]

    poster = FlakyPoster(failures=3)
    with pytest.raises(RegistryError):
        post_with_retries(poster, "body", sleeper=sleeps.append)
    assert poster.calls == 3  # attempts are capped


def test_client_posts_a_comment_to_the_registry_issue():
    transport = RecordingTransport()
    transport.routes[("POST", f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments")] = HttpResponse(
        201, {}, b'{"id": 1}'
    )
    _client(transport).post_comment("hello")
    request = transport.requests[-1]
    assert request["method"] == "POST"
    assert request["url"] == f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments"
    assert json.loads(request["body"].decode("utf-8")) == {"body": "hello"}
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_client_lists_comments_and_follows_pagination():
    page1_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments?per_page=100&page=1"
    page2_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments?per_page=100&page=2"
    page1 = [{"id": index} for index in range(1, 101)]  # a full page keeps the loop going
    page2 = [{"id": 101}]
    transport = RecordingTransport()
    transport.routes[("GET", page1_url)] = HttpResponse(200, {}, json.dumps(page1).encode("utf-8"))
    transport.routes[("GET", page2_url)] = HttpResponse(200, {}, json.dumps(page2).encode("utf-8"))
    client = _client(transport)

    assert len(client.list_comments(1)) == 100
    # all_comments keeps going until a short page: 100 + 1 comments over two pages.
    assert [c["id"] for c in client.all_comments()][-1] == 101
    assert len(client.all_comments()) == 101
    assert [request["url"] for request in transport.requests if request["method"] == "GET"] == [
        page1_url,          # the single-page call
        page1_url, page2_url,  # the first all_comments
        page1_url, page2_url,  # the second all_comments
    ]


def test_client_reads_and_writes_the_channels_file():
    path = "config/angareion/channels.json"
    text = '{"channels": {}, "version": 1}\n'
    transport = RecordingTransport()
    transport.routes[("GET", f"https://api.github.com/repos/{REPO}/contents/{path}")] = HttpResponse(
        200, {}, json.dumps({"content": base64.b64encode(text.encode()).decode(), "sha": "abc"}).encode()
    )
    transport.routes[("PUT", f"https://api.github.com/repos/{REPO}/contents/{path}")] = HttpResponse(200, {}, b"{}")
    client = _client(transport)

    assert client.get_file(path) == (text, "abc")
    client.put_file(path, text, sha="abc", message="rebuild")
    put = [request for request in transport.requests if request["method"] == "PUT"][-1]
    payload = json.loads(put["body"].decode("utf-8"))
    assert payload["sha"] == "abc"
    assert payload["branch"] == "main"
    assert base64.b64decode(payload["content"]).decode("utf-8") == text


def test_client_get_file_returns_empty_for_a_missing_file():
    transport = RecordingTransport()
    assert _client(transport).get_file("config/angareion/channels.json") == ("", None)


def test_client_write_conflict_and_errors():
    path = "config/angareion/channels.json"
    transport = RecordingTransport()
    transport.routes[("PUT", f"https://api.github.com/repos/{REPO}/contents/{path}")] = HttpResponse(
        409, {}, b'{"message": "sha mismatch"}'
    )
    with pytest.raises(RegistryWriteConflict):
        _client(transport).put_file(path, "x", sha="old", message="rebuild")

    transport = RecordingTransport()
    transport.routes[("POST", f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments")] = HttpResponse(
        401, {}, b'{"message": "bad credentials"}'
    )
    with pytest.raises(RegistryError, match="401"):
        _client(transport).post_comment("hello")

    transport = RecordingTransport()
    transport.routes[("GET", f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments")] = HttpResponse(
        500, {}, b'{"message": "boom"}'
    )
    with pytest.raises(RegistryError, match="500"):
        _client(transport).list_comments(1)


def test_fake_registry_records_and_fails():
    fake = FakeRegistryClient()
    fake.post_comment("one")
    assert fake.posted == ["one"]
    # The posted comment lands in the thread as a Bot comment (author_association NONE).
    assert fake.all_comments()[-1]["user"]["type"] == "Bot"
    assert fake.all_comments()[-1]["author_association"] == "NONE"

    failing = FakeRegistryClient(fail_posts_times=1)
    with pytest.raises(RegistryError):
        failing.post_comment("one")
    failing.post_comment("one")  # works again
    assert failing.posted == ["one"]
