import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

"""Contract tests of design 542 PR-3a (prerequisites for the rollover): C-17's
prerequisites (the page opener, the registry comment's return value, the
pending-page record), W-4's refused-write class, and the FakeRegistryClient
hooks C-18 uses. Fake transport and in-memory registry only: no network, no
sleeps, no FakeBackend."""

import json
import os

import pytest

from _angareion_fakes import StaticResolver, entity
from angareion.backend import (
    BackendError,
    BackendRef,
    ChannelFullError,
    PageOpener,
    RateLimitError,
    WriteRefusedError,
)
from angareion.github import GitHubIssuesBackend, HttpResponse
from angareion.messages import Message
from angareion.registry import FakeRegistryClient, RegistryClient, RegistryError, post_with_retries
from angareion.state import PendingPageStore, pending_pages_file_path

OWNER_REPO = "owner/repo"
ISSUE = 10
ISSUES_URL = f"https://api.github.com/repos/{OWNER_REPO}/issues"
ISSUE_URL = f"{ISSUES_URL}/{ISSUE}"
COMMENTS_URL = f"{ISSUE_URL}/comments"
REGISTRY_COMMENTS_URL = "https://api.github.com/repos/owner/registry/issues/5/comments"
TOKEN = "fake_token"
ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
GENERAL = entity(channel="general", issue=ISSUE)
OTHER_REPO_GENERAL = entity(channel="general", backend="github-2", repo="owner/repo2", issue=7)
BACKEND_MAIN = BackendRef(name="github-main", type="github", repo=OWNER_REPO)
ISSUE_NUMBER_MESSAGE = "Resource not accessible by integration: see #42 in owner/repo"
BELOW_BAND_MESSAGE = (
    "Failed to send to channel 'general' (HTTP 403: Resource not accessible by integration: see #… in <backend>)"
)


class Transport:
    """Exact (method, url) routes; a request without a route is a 404. Every request and its body is recorded."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[bytes | None] = []

    def set_route(self, method, url, response):
        self.routes[(method, url)] = response

    def request(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append((method, url))
        self.bodies.append(body)
        return self.routes.get((method, url), HttpResponse(404, {}, b'{"message": "Not Found"}'))


def _json(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _github(transport):
    return GitHubIssuesBackend(StaticResolver([GENERAL, OTHER_REPO_GENERAL]), token=TOKEN, transport=transport)


def _registry(transport):
    return RegistryClient("owner/registry", 5, TOKEN, transport=transport)


def _msg(content="hello"):
    return Message(from_=PM, to=ME, channel="general", content=content, id="01JABCDEF0123456789ABCDEF0", at="2026-10-10T12:00:00+00:00")


def _refused_post(transport, *, status=403, comments=None):
    transport.set_route("POST", COMMENTS_URL, HttpResponse(status, {}, _json({"message": ISSUE_NUMBER_MESSAGE})))
    if comments is not None:
        transport.set_route("GET", ISSUE_URL, HttpResponse(200, {}, _json({"number": ISSUE, "comments": comments})))


# ---------------------------------------------------------------- create_channel (C-17 prerequisite)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (HttpResponse(500, {}, _json({"message": "boom"})), "Failed to create channel 'fresh': HTTP 500"),
        (
            HttpResponse(201, {}, b"<html>proxy login page</html>"),
            "GitHub returned a non-JSON response while creating channel 'fresh' (HTTP 201)",
        ),
        (
            HttpResponse(201, {}, _json({"title": "fresh"})),
            "Failed to create channel 'fresh': no issue number in the response",
        ),
    ],
    ids=["http-500", "non-json", "no-number"],
)
def test_c17_create_channel_messages_are_verbatim(response, message):
    transport = Transport({("POST", ISSUES_URL): response})
    with pytest.raises(BackendError) as excinfo:
        _github(transport).create_channel("fresh", BACKEND_MAIN)
    assert str(excinfo.value) == message


# ---------------------------------------------------------------- open_page (C-17 prerequisite, S-4)


def test_c17_open_page_opens_an_issue_that_names_the_page_it_continues_from():
    transport = Transport({("POST", ISSUES_URL): HttpResponse(201, {}, _json({"number": 12, "title": "general"}))})

    assert _github(transport).open_page(GENERAL) == 12
    assert transport.calls == [("POST", ISSUES_URL)]
    payload = json.loads(transport.bodies[0].decode("utf-8"))
    assert set(payload) == {"title", "body"}
    assert payload["title"] == "general"
    assert "continued from #10" in payload["body"]


def test_c17_open_page_posts_to_the_entitys_own_repo():
    transport = Transport({("POST", "https://api.github.com/repos/owner/repo2/issues"): HttpResponse(201, {}, _json({"number": 13}))})

    assert _github(transport).open_page(OTHER_REPO_GENERAL) == 13
    assert transport.calls == [("POST", "https://api.github.com/repos/owner/repo2/issues")]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            HttpResponse(500, {}, _json({"message": "boom"})),
            "Failed to open a new page for channel 'general': HTTP 500",
        ),
        (
            HttpResponse(201, {}, b"<html>proxy login page</html>"),
            "GitHub returned a non-JSON response while opening a new page for channel 'general' (HTTP 201)",
        ),
        (
            HttpResponse(201, {}, _json({"title": "general"})),
            "Failed to open a new page for channel 'general': no issue number in the response",
        ),
    ],
    ids=["http-500", "non-json", "no-number"],
)
def test_c17_open_page_messages_are_verbatim_and_carry_no_issue_number(response, message):
    transport = Transport({("POST", ISSUES_URL): response})
    with pytest.raises(BackendError) as excinfo:
        _github(transport).open_page(GENERAL)
    assert str(excinfo.value) == message
    assert "#" not in str(excinfo.value)
    assert OWNER_REPO not in str(excinfo.value)


def test_c17_github_backend_is_a_page_opener():
    assert isinstance(_github(Transport()), PageOpener)


# ---------------------------------------------------------------- refused write (W-4 prerequisite)


def test_c2_refused_write_below_the_band_keeps_its_message_verbatim():
    transport = Transport()
    _refused_post(transport, comments=2399)
    with pytest.raises(BackendError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert not isinstance(excinfo.value, ChannelFullError)
    assert str(excinfo.value) == BELOW_BAND_MESSAGE


def test_c2_refused_write_below_the_band_is_a_write_refused_error_with_its_status():
    transport = Transport()
    _refused_post(transport, comments=2399)
    with pytest.raises(WriteRefusedError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert excinfo.value.status == 403
    assert str(excinfo.value) == BELOW_BAND_MESSAGE


def test_c2_a_refused_write_whose_count_request_fails_is_a_write_refused_error_too():
    transport = Transport()
    _refused_post(transport, status=422, comments=None)
    transport.set_route("GET", ISSUE_URL, HttpResponse(500, {}, _json({"message": "boom"})))
    with pytest.raises(WriteRefusedError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert excinfo.value.status == 422
    assert "HTTP 422" in str(excinfo.value)


@pytest.mark.parametrize("comments", [2400, 2499, 2500])
def test_c2_a_refusal_at_the_band_or_full_is_a_channel_full_error_not_a_write_refused_error(comments):
    transport = Transport()
    _refused_post(transport, comments=comments)
    with pytest.raises(ChannelFullError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert not isinstance(excinfo.value, WriteRefusedError)


def test_c2_a_rate_limited_write_is_a_rate_limit_error_not_a_write_refused_error():
    transport = Transport()
    transport.set_route("POST", COMMENTS_URL, HttpResponse(403, {"retry-after": "30"}, _json({"message": "abuse"})))
    with pytest.raises(RateLimitError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert not isinstance(excinfo.value, WriteRefusedError)


def test_c2_write_refused_error_is_a_backend_error():
    assert issubclass(WriteRefusedError, BackendError)


# ---------------------------------------------------------------- registry comments (C-17 prerequisite)


def test_c17_registry_post_comment_returns_what_github_stored():
    transport = Transport({("POST", REGISTRY_COMMENTS_URL): HttpResponse(201, {}, _json({"id": 77, "body": "rollover"}))})

    assert _registry(transport).post_comment("rollover") == {"id": 77, "body": "rollover"}


@pytest.mark.parametrize(
    "reply",
    [b"<html>proxy login page</html>", b"[1, 2]"],
    ids=["non-json", "not-an-object"],
)
def test_c17_registry_post_comment_with_a_bad_reply_is_empty_and_not_an_error(reply):
    transport = Transport({("POST", REGISTRY_COMMENTS_URL): HttpResponse(201, {}, reply)})

    assert _registry(transport).post_comment("rollover") == {}


def test_c17_post_with_retries_returns_the_comment_of_the_successful_post():
    registry = FakeRegistryClient(fail_posts_times=1)
    sleeps: list[float] = []

    comment = post_with_retries(registry, "body", sleeper=sleeps.append)

    assert comment["body"] == "body" and comment["id"] == 1
    assert len(sleeps) == 1


def test_c17_post_with_retries_gives_an_empty_comment_when_the_poster_returns_no_dict():
    class Poster:
        def post_comment(self, body):
            return None

    assert post_with_retries(Poster(), "body", sleeper=lambda _: None) == {}


# ---------------------------------------------------------------- FakeRegistryClient hooks (C-18 prerequisite)


def test_c18_fake_post_association_is_stored_on_the_posted_comment():
    owner = FakeRegistryClient(post_association="OWNER")
    assert owner.post_comment("rollover")["author_association"] == "OWNER"

    default = FakeRegistryClient()
    comment = default.post_comment("x")
    assert comment["author_association"] == "NONE"
    assert comment["user"]["type"] == "Bot"


def test_c18_before_post_runs_before_an_injected_failure():
    registry = FakeRegistryClient(fail_posts_times=1)
    seen: list[int] = []
    registry.before_post = lambda body: seen.append(registry.fail_posts_times)

    with pytest.raises(RegistryError):
        registry.post_comment("x")
    assert seen == [1]  # the failure had not been used up yet when the hook ran


def test_c18_before_post_is_called_outside_the_registry_lock():
    registry = FakeRegistryClient()
    owned: list[bool] = []
    registry.before_post = lambda body: owned.append(registry._lock._is_owned())

    registry.post_comment("x")
    assert owned == [False]


def test_c18_hidden_ids_are_left_out_of_the_listing_but_still_counted():
    registry = FakeRegistryClient(
        comments=[{"id": 1, "body": "a"}, {"id": 2, "body": "b"}, {"id": 3, "body": "c"}],
        page_size=1,
    )
    registry.hidden_ids = {2}

    assert [c["id"] for c in registry.all_comments()] == [1, 3]
    assert registry.comment_count() == 3


def test_c18_hidden_ids_are_removed_before_the_page_is_cut():
    registry = FakeRegistryClient(
        comments=[{"id": 1, "body": "a"}, {"id": 2, "body": "b"}, {"id": 3, "body": "c"}],
        page_size=1,
    )
    registry.hidden_ids = {2}

    assert [c["id"] for c in registry.list_comments(1)] == [1]
    assert [c["id"] for c in registry.list_comments(2)] == [3]


# ---------------------------------------------------------------- pending pages (C-17 prerequisite, §1.3)

GOOD = {"backend": "github-main", "issue": 12, "from": 10}


def _record(entry, *, version=1):
    return json.dumps({"version": version, "pages": {"general": entry}})


def test_c17_pending_pages_file_is_named_after_the_identity(tmp_path):
    assert pending_pages_file_path(tmp_path, ME) == tmp_path / "pending-pages-mylinuxpool-impl-w1-1.json"

    store = PendingPageStore(tmp_path, ME)
    store.put("general", "github-main", 12, 10)
    assert pending_pages_file_path(tmp_path, ME).is_file()


def test_c17_a_pending_record_reads_back_what_was_put(tmp_path):
    store = PendingPageStore(path=tmp_path / "pending.json")

    store.put("general", "github-main", 12, 10)

    assert store.get("general") == ("github-main", 12, 10)
    assert store.get("missing") is None


def test_c17_the_pending_file_has_the_documented_shape(tmp_path):
    path = tmp_path / "pending.json"

    PendingPageStore(path=path).put("general", "github-main", 12, 10)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "pages": {"general": {"backend": "github-main", "issue": 12, "from": 10}},
    }


def test_c17_pending_records_of_two_channels_do_not_touch_each_other(tmp_path):
    store = PendingPageStore(path=tmp_path / "pending.json")
    store.put("general", "github-main", 12, 10)
    store.put("other", "github-2", 7, 3)

    assert store.get("general") == ("github-main", 12, 10)
    assert store.get("other") == ("github-2", 7, 3)

    store.drop("general")

    assert store.get("general") is None
    assert store.get("other") == ("github-2", 7, 3)


def test_c17_dropping_an_unknown_channel_writes_no_file(tmp_path):
    path = tmp_path / "pending.json"

    PendingPageStore(path=path).drop("general")

    assert not path.exists()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("{ not json", id="not-json"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param(json.dumps({"version": 2, "pages": {"general": GOOD}}), id="version-2"),
        pytest.param(json.dumps({"pages": {"general": GOOD}}), id="version-missing"),
        pytest.param(json.dumps({"version": True, "pages": {"general": GOOD}}), id="version-is-a-bool"),
        pytest.param(json.dumps({"version": 1, "pages": []}), id="pages-not-an-object"),
        pytest.param(_record("github-main"), id="entry-not-an-object"),
        pytest.param(_record({**GOOD, "backend": 5}), id="backend-not-a-string"),
        pytest.param(_record({**GOOD, "issue": "12"}), id="issue-not-an-int"),
        pytest.param(_record({**GOOD, "issue": True}), id="issue-is-a-bool"),
        pytest.param(_record({**GOOD, "issue": 0}), id="issue-zero"),
        pytest.param(_record({**GOOD, "issue": -3}), id="issue-negative"),
        pytest.param(_record({"backend": "github-main", "issue": 12}), id="from-missing"),
        pytest.param(_record({**GOOD, "from": False}), id="from-is-a-bool"),
        pytest.param(_record({**GOOD, "from": 10.0}), id="from-not-an-int"),
    ],
)
def test_c17_a_malformed_pending_record_reads_as_no_record_and_raises_nothing(tmp_path, text):
    path = tmp_path / "pending.json"
    path.write_text(text, encoding="utf-8")

    assert PendingPageStore(path=path).get("general") is None


def test_c17_a_put_replaces_a_malformed_pending_file(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("{ not json", encoding="utf-8")
    store = PendingPageStore(path=path)

    store.put("general", "github-main", 12, 10)

    assert store.get("general") == ("github-main", 12, 10)


def test_c17_a_path_that_is_a_directory_is_no_record_and_no_error(tmp_path):
    path = tmp_path / "pending.json"
    path.mkdir()
    store = PendingPageStore(path=path)

    assert store.get("general") is None
    store.put("general", "github-main", 12, 10)  # cannot be written: no error
    assert store.get("general") is None


def test_c17_an_unwritable_directory_is_no_record_and_no_error(tmp_path):
    if getattr(os, "geteuid", lambda: -1)() == 0:
        pytest.skip("root can write into a read-only directory")
    state = tmp_path / "state"
    state.mkdir()
    store = PendingPageStore(state, ME)
    os.chmod(state, 0o500)
    try:
        store.put("general", "github-main", 12, 10)
        store.drop("general")
        assert store.get("general") is None
        assert list(state.iterdir()) == []
    finally:
        os.chmod(state, 0o700)
