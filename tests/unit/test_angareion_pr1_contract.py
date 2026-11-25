import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

"""Contract tests C-1..C-5 of design 542 (PR-1): 403 classification, the page
limits on a refused write, `page_size`, the send-side warning, and `channel
status`. Fake transport and fake backends only: no network, no sleeps."""

import json
import re
from types import SimpleNamespace

import pytest

from _angareion_fakes import FakeRegistryClient, StaticResolver, entity
from angareion.backend import (
    PAGE_COMMENT_LIMIT,
    BackendError,
    BackendRef,
    ChannelFullError,
    Cursor,
    FakeBackend,
    PageBackend,
    RateLimitError,
    page_level,
)
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.github import GitHubIssuesBackend, HttpResponse
from angareion.messages import Message
from angareion.registry import RegistryError

OWNER_REPO = "owner/repo"
ISSUE = 10
ISSUE_URL = f"https://api.github.com/repos/{OWNER_REPO}/issues/{ISSUE}"
COMMENTS_URL = f"{ISSUE_URL}/comments"
LIST_URL = f"{COMMENTS_URL}?per_page=100"  # what `read` lists
TOKEN = "tok" + "en-FAKE-PR1-SECRET-42"
ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
GENERAL = entity(channel="general", issue=ISSUE)
OTHER = entity(channel="other", issue=7)
BACKEND_MAIN = BackendRef(name="github-main", type="github", repo=OWNER_REPO)
BACKEND_NOW = "2026-10-10T09:00:00Z"


class Transport:
    """Exact (method, url) routes; a request without a route is a 404 and is recorded."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str]] = []

    def set_route(self, method, url, response):
        self.routes[(method, url)] = response

    def request(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append((method, url))
        return self.routes.get((method, url), HttpResponse(404, {}, b'{"message": "Not Found"}'))

    def count(self, method, url):
        return sum(1 for call in self.calls if call == (method, url))


def _json(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _github(transport, token=TOKEN):
    return GitHubIssuesBackend(StaticResolver([GENERAL, OTHER]), token=token, transport=transport)


def _msg(content="hello", channel="general"):
    return Message(from_=PM, to=ME, channel=channel, content=content, id="01JABCDEF0123456789ABCDEF0", at="2026-10-10T12:00:00+00:00")


# ---------------------------------------------------------------- C-1


def test_c1_primary_rate_limit_is_a_rate_limit_error_sent_once():
    transport = Transport()
    transport.set_route("GET", LIST_URL, HttpResponse(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793000000"}, _json({"message": "API rate limit exceeded"})))
    with pytest.raises(RateLimitError) as excinfo:
        _github(transport).read(GENERAL, Cursor())
    err = excinfo.value
    assert err.kind == "primary"
    assert err.reset_at is not None and err.reset_at.endswith("Z") and "T" in err.reset_at
    assert "HTTP 403" in str(err) and "not retrying" in str(err)
    assert "resets at" in str(err)
    assert transport.count("GET", LIST_URL) == 1
    assert isinstance(err, BackendError)


def test_c1_retry_after_is_secondary_with_the_seconds():
    transport = Transport()
    transport.set_route("GET", LIST_URL, HttpResponse(403, {"retry-after": "60"}, _json({"message": "slow down"})))
    with pytest.raises(RateLimitError) as excinfo:
        _github(transport).read(GENERAL, Cursor())
    assert excinfo.value.kind == "secondary"
    assert excinfo.value.retry_after == 60
    assert "retry after 60s" in str(excinfo.value)
    assert "HTTP 403" in str(excinfo.value) and "not retrying" in str(excinfo.value)
    assert transport.count("GET", LIST_URL) == 1


def test_c1_secondary_text_in_the_body_is_a_secondary_rate_limit():
    transport = Transport()
    transport.set_route("GET", LIST_URL, HttpResponse(403, {}, _json({"message": "You have exceeded a secondary rate limit."})))
    with pytest.raises(RateLimitError) as excinfo:
        _github(transport).read(GENERAL, Cursor())
    assert excinfo.value.kind == "secondary"
    assert excinfo.value.retry_after is None


def test_c1_a_429_with_retry_after_is_a_rate_limit_too():
    transport = Transport()
    transport.set_route("GET", LIST_URL, HttpResponse(429, {"retry-after": "5"}, _json({"message": "Too Many Requests"})))
    with pytest.raises(RateLimitError, match="HTTP 429"):
        _github(transport).read(GENERAL, Cursor())


def test_c1_primary_header_wins_over_a_retry_after():
    transport = Transport()
    transport.set_route("GET", LIST_URL, HttpResponse(403, {"x-ratelimit-remaining": "0", "retry-after": "60"}, _json({"message": "limit"})))
    with pytest.raises(RateLimitError) as excinfo:
        _github(transport).read(GENERAL, Cursor())
    assert excinfo.value.kind == "primary"


def test_c1_a_plain_403_is_a_backend_error_and_not_a_rate_limit():
    transport = Transport()
    transport.set_route("GET", LIST_URL, HttpResponse(403, {}, _json({"message": "Resource not accessible"})))
    with pytest.raises(BackendError) as excinfo:
        _github(transport).read(GENERAL, Cursor())
    assert not isinstance(excinfo.value, RateLimitError)
    assert "HTTP 403" in str(excinfo.value) and "not retrying" in str(excinfo.value)
    assert "secondary rate limit?" not in str(excinfo.value)


# ---------------------------------------------------------------- C-2


ISSUE_NUMBER_MESSAGE = "Resource not accessible by integration: see #42 in owner/repo"


def _refused_post(transport, *, status=403, message=ISSUE_NUMBER_MESSAGE, comments=None):
    transport.set_route("POST", COMMENTS_URL, HttpResponse(status, {}, _json({"message": message})))
    if comments is not None:
        transport.set_route("GET", ISSUE_URL, HttpResponse(200, {}, _json({"number": ISSUE, "comments": comments})))


def test_c2_refused_write_at_the_hard_limit_is_full_and_not_presumed():
    transport = Transport()
    _refused_post(transport, comments=2500)
    with pytest.raises(ChannelFullError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    err = excinfo.value
    assert err.presumed is False and err.count == 2500
    assert "is full" in str(err) and "looks full" not in str(err)
    assert "HTTP 403" in str(err)
    assert transport.count("GET", ISSUE_URL) == 1


@pytest.mark.parametrize("comments", [2400, 2499])
def test_c2_refused_write_in_the_rollover_band_looks_full_and_is_presumed(comments):
    transport = Transport()
    _refused_post(transport, status=422, comments=comments)
    with pytest.raises(ChannelFullError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    err = excinfo.value
    assert err.presumed is True and err.count == comments
    assert "looks full (or the token was refused)" in str(err)
    assert "HTTP 422" in str(err)


def test_c2_refused_write_below_the_band_is_a_plain_backend_error():
    transport = Transport()
    _refused_post(transport, comments=2399)
    with pytest.raises(BackendError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert not isinstance(excinfo.value, ChannelFullError)
    assert "HTTP 403" in str(excinfo.value)
    assert "secondary rate limit?" not in str(excinfo.value)


def test_c2_failed_count_after_a_refusal_is_a_plain_backend_error_with_the_status():
    transport = Transport()
    _refused_post(transport, status=422, comments=None)
    transport.set_route("GET", ISSUE_URL, HttpResponse(500, {}, _json({"message": "boom"})))
    with pytest.raises(BackendError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    assert not isinstance(excinfo.value, ChannelFullError)
    assert "HTTP 422" in str(excinfo.value)


def test_c2_a_rate_limited_write_is_not_a_full_channel_and_sends_no_count_request():
    transport = Transport()
    transport.set_route("POST", COMMENTS_URL, HttpResponse(403, {"retry-after": "30"}, _json({"message": "abuse"})))
    with pytest.raises(RateLimitError):
        _github(transport).send(GENERAL, _msg())
    assert transport.count("GET", ISSUE_URL) == 0


@pytest.mark.parametrize(
    "message",
    [
        "Resource not accessible by integration: see #42 in owner/repo",
        "Validation Failed for issue #7 of owner/repo",
        "forbidden: owner/repo #123 is locked",
    ],
)
@pytest.mark.parametrize("comments", [2500, 2450, 10])
def test_c2_the_three_messages_carry_no_issue_number_and_no_repo_name(message, comments):
    transport = Transport()
    _refused_post(transport, message=message, comments=comments)
    with pytest.raises(BackendError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    text = str(excinfo.value)
    assert "HTTP " in text
    assert not re.search(r"#\d+", text)
    assert OWNER_REPO not in text
    assert "<backend>" in text  # the GitHub message is kept, with the repo name replaced


def test_c2_the_github_message_is_one_line_short_and_token_free():
    transport = Transport()
    noisy = "line one\nline two " + TOKEN + " " + ("x" * 500)
    _refused_post(transport, message=noisy, comments=10)
    with pytest.raises(BackendError) as excinfo:
        _github(transport).send(GENERAL, _msg())
    text = str(excinfo.value)
    assert "\n" not in text
    assert TOKEN not in text
    assert "***" in text
    assert "line one line two" in text
    assert len(text) < 300


# ---------------------------------------------------------------- C-3


def test_c3_github_page_size_reads_the_comment_count_and_the_backend_date():
    transport = Transport()
    transport.set_route(
        "GET",
        ISSUE_URL,
        HttpResponse(200, {"Date": "Tue, 27 Oct 2026 05:04:03 GMT"}, _json({"number": ISSUE, "comments": 2417})),
    )
    backend = _github(transport)
    assert backend.page_size(GENERAL) == 2417
    assert backend.page_size_at(GENERAL) == (2417, "2026-10-27T05:04:03Z")


@pytest.mark.parametrize(
    "payload",
    [{"number": ISSUE}, {"comments": "12"}, {"comments": True}, {"comments": None}, ["not", "an", "object"]],
)
def test_c3_a_page_size_that_is_missing_or_not_an_integer_is_a_backend_error(payload):
    transport = Transport()
    transport.set_route("GET", ISSUE_URL, HttpResponse(200, {}, _json(payload)))
    with pytest.raises(BackendError):
        _github(transport).page_size(GENERAL)


def test_c3_github_backend_is_a_page_backend():
    assert isinstance(_github(Transport()), PageBackend)


def test_c3_fake_page_size_counts_the_channel_and_a_set_page_size_overrides_it():
    store = StaticResolver([GENERAL, OTHER])
    backend = FakeBackend(store, clock=lambda: BACKEND_NOW)
    backend.add_entity("general", number=ISSUE)
    backend.add_entity("other", number=7)
    backend.deliver(GENERAL, _msg("one"))
    backend.deliver(GENERAL, _msg("two"))
    assert backend.page_size(GENERAL) == 2
    assert backend.page_size(OTHER) == 0
    assert backend.page_size_at(GENERAL) == (2, BACKEND_NOW)
    backend.set_page_size(ISSUE, 2417)
    assert backend.page_size(GENERAL) == 2417


def test_c3_fake_send_is_refused_at_the_comment_limit():
    store = StaticResolver([GENERAL])
    backend = FakeBackend(store, clock=lambda: BACKEND_NOW, comment_limit=3)
    backend.add_entity("general", number=ISSUE)
    for index in range(3):
        backend.deliver(GENERAL, _msg(f"m{index}"))
    with pytest.raises(ChannelFullError) as excinfo:
        backend.send(GENERAL, _msg("one too many"))
    assert excinfo.value.count == 3 and excinfo.value.presumed is False


def test_c3_fake_send_at_the_default_limit_is_refused_with_a_pinned_size():
    store = StaticResolver([GENERAL])
    backend = FakeBackend(store, clock=lambda: BACKEND_NOW)
    backend.add_entity("general", number=ISSUE)
    backend.set_page_size(ISSUE, PAGE_COMMENT_LIMIT)
    with pytest.raises(ChannelFullError):
        backend.send(GENERAL, _msg())


def test_c3_a_plain_object_is_not_a_page_backend():
    class Plain:
        def send(self, entity, message):
            return message.id

    assert not isinstance(Plain(), PageBackend)


# ---------------------------------------------------------------- C-4 (through the CLI)


class _Resolver(StaticResolver):
    def ensure_fresh(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    clone = tmp_path / "clone"
    clone.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = Config(
        identity=ME,
        config_dir=clone,
        registry_repo="owner/MyAiStorage",
        registry_issue=77,
        token_file=token_file,
        state_dir=state_dir,
        config_file=config_file,
    )
    save_config(config, config_file)
    monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))

    store = _Resolver([GENERAL, OTHER])
    backend = FakeBackend(store, clock=lambda: BACKEND_NOW)
    backend.add_entity("general", number=ISSUE)
    backend.add_entity("other", number=7)
    registry = FakeRegistryClient(comments=[{"id": 1, "body": "x", "author_association": "OWNER", "user": {"type": "User", "login": "a"}, "created_at": "2026-10-10T12:00:00Z"}])
    return SimpleNamespace(store=store, backend=backend, registry=registry, state_dir=state_dir)


def _send(env, backend, capsys):
    ret = main(["send", "--channel", "general", "--body", "hi"], backend=backend, store=env.store, registry=env.registry)
    captured = capsys.readouterr()
    return ret, captured.out, captured.err


def test_c4_a_page_at_2000_gets_one_warning_line_and_the_send_goes_ahead(env, capsys):
    env.backend.set_page_size(ISSUE, 2000)
    ret, out, err = _send(env, env.backend, capsys)
    assert ret == 0
    assert out.strip() != ""
    assert err.splitlines() == ["Warning: channel 'general' has 2000 of 2500 messages on its current page"]


def test_c4_a_page_below_2000_gets_no_warning(env, capsys):
    env.backend.set_page_size(ISSUE, 1999)
    ret, out, err = _send(env, env.backend, capsys)
    assert ret == 0
    assert err == ""


def test_c4_a_failed_page_size_is_silent_and_the_send_still_goes_ahead(env, capsys):
    inner = FakeBackend(env.store, clock=lambda: BACKEND_NOW)
    inner.add_entity("general", number=ISSUE)

    class FailingSize:
        """Sizes fail; sending works (the gate inside the fake is not what is under test here)."""

        def send(self, entity, message):
            return inner.send(entity, message)

        def page_size(self, entity):
            raise BackendError("size unavailable")

        def page_size_at(self, entity):
            raise BackendError("size unavailable")

    ret, out, err = _send(env, FailingSize(), capsys)
    assert ret == 0
    assert err == ""
    assert len(inner._comments) == 1


def test_c4_a_backend_without_page_sizes_is_skipped_silently(env, capsys):
    class SendOnly:
        def send(self, entity, message):
            return message.id

    ret, out, err = _send(env, SendOnly(), capsys)
    assert ret == 0
    assert err == ""
    assert out.strip() != ""


def test_c4_a_refused_write_at_the_limit_exits_2_with_the_full_message(env, capsys):
    transport = Transport()
    _refused_post(transport, comments=2500)
    backend = _github(transport)
    ret = main(["send", "--channel", "general", "--body", "hi"], backend=backend, store=env.store, registry=env.registry)
    captured = capsys.readouterr()
    assert ret == 2
    assert "is full" in captured.err
    assert TOKEN not in captured.err


# ---------------------------------------------------------------- C-5


def _status(env, capsys, *args):
    ret = main(["channel", "status", *args], backend=env.backend, store=env.store, registry=env.registry)
    captured = capsys.readouterr()
    return ret, captured.out, captured.err


@pytest.mark.parametrize(
    "count, level",
    [(0, "ok"), (1999, "ok"), (2000, "warn"), (2399, "warn"), (2400, "rollover"), (2499, "rollover"), (2500, "full"), (None, "unknown")],
)
def test_c5_the_levels_use_the_page_thresholds(count, level):
    assert page_level(count) == level


def test_c5_status_lists_each_channel_then_the_registry_with_the_same_fields(env, capsys):
    env.backend.set_page_size(ISSUE, 2010)
    ret, out, err = _status(env, capsys, "--json")
    assert ret == 0
    rows = json.loads(out)
    assert [row["name"] for row in rows] == ["general", "other", "registry"]
    for row in rows:
        assert set(row) == {"name", "count", "limit", "level", "pages", "flags"}
        assert row["limit"] == PAGE_COMMENT_LIMIT
        assert row["pages"] == 1
        assert row["flags"] == []
    general, other, registry = rows
    assert (general["count"], general["level"]) == (2010, "warn")
    assert (other["count"], other["level"]) == (0, "ok")
    assert (registry["count"], registry["level"]) == (1, "ok")


def test_c5_the_text_form_has_a_header_and_one_row_per_channel(env, capsys):
    ret, out, err = _status(env, capsys)
    assert ret == 0
    lines = out.splitlines()
    assert lines[0].split()[:4] == ["name", "count", "limit", "level"]
    assert [line.split()[0] for line in lines[1:]] == ["general", "other", "registry"]


def test_c5_named_channels_are_shown_and_an_unregistered_name_is_exit_1(env, capsys):
    ret, out, err = _status(env, capsys, "other", "--json")
    assert ret == 0
    assert [row["name"] for row in json.loads(out)] == ["other", "registry"]

    ret, out, err = _status(env, capsys, "nosuch")
    assert ret == 1
    assert "not registered" in err
    assert out == ""


def test_c5_a_failed_channel_read_leaves_its_row_unknown_and_the_others_print_then_exit_2(env, capsys):
    class OneBroken(FakeBackend):
        def page_size(self, entity):
            if entity.channel == "other":
                raise BackendError("HTTP 500 for other")
            return super().page_size(entity)

    backend = OneBroken(env.store, clock=lambda: BACKEND_NOW)
    backend.add_entity("general", number=ISSUE)
    backend.add_entity("other", number=7)
    ret = main(["channel", "status", "--json"], backend=backend, store=env.store, registry=env.registry)
    captured = capsys.readouterr()
    assert ret == 2
    assert "other" in captured.err and "HTTP 500" in captured.err
    rows = {row["name"]: row for row in json.loads(captured.out)}
    assert rows["general"]["level"] == "ok"
    assert rows["other"]["count"] is None and rows["other"]["level"] == "unknown"
    assert rows["registry"]["count"] == 1


def test_c5_a_registry_seam_without_a_comment_count_is_unknown_and_not_a_failure(env, capsys):
    class PosterOnly:
        def post_comment(self, body):
            pass

    ret = main(["channel", "status", "--json"], backend=env.backend, store=env.store, registry=PosterOnly())
    captured = capsys.readouterr()
    assert ret == 0
    rows = {row["name"]: row for row in json.loads(captured.out)}
    assert rows["registry"]["count"] is None and rows["registry"]["level"] == "unknown"
    assert rows["general"]["count"] == 0


def test_c5_a_failed_registry_count_is_unknown_and_exit_2(env, capsys):
    class BrokenRegistry(FakeRegistryClient):
        def comment_count(self):
            raise RegistryError("registry down")

    registry = BrokenRegistry()
    ret = main(["channel", "status", "--json"], backend=env.backend, store=env.store, registry=registry)
    captured = capsys.readouterr()
    assert ret == 2
    rows = {row["name"]: row for row in json.loads(captured.out)}
    assert rows["registry"]["level"] == "unknown"
    assert "registry" in captured.err


def test_c5_status_is_read_only_no_posts_no_cursor_no_page_writes(env, capsys):
    env.backend.set_page_size(ISSUE, 2100)
    before = env.backend.page_size(GENERAL)
    ret, out, err = _status(env, capsys, "--json")
    assert ret == 0
    assert env.registry.posted == []
    assert env.backend.page_size(GENERAL) == before
    if env.state_dir.exists():
        assert not any(path.name.startswith("cursor") for path in env.state_dir.iterdir())


def test_c5_no_action_message_names_status(capsys):
    ret = main(["channel"])
    assert ret == 1
    assert "status" in capsys.readouterr().err
