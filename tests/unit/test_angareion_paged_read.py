"""Unit tests for #542 PR-2c: reading across the pages of a channel.

Contracts C-10 to C-14 (read, check, rollover continuity, the log mirror), C-22
and C-23 (older pages that cannot be read), C-24 (`--capabilities`) and C-26
(`channel status` page counts and flags). Every scenario runs on the Fake
backend and on GitHub, whose listing is a fake transport that honours `since`,
the 100-per-page `Link` header and ETags. Design: docs/design-notes/542-channel-rollover.md
§2, §6.3 (2c), §10.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import dataclasses
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from _angareion_fakes import FakeGit, StaticResolver, backends_json
from angareion.backend import (
    BackendError,
    Cursor,
    Entity,
    PageUnavailableError,
    RateLimitError,
    RetiredPage,
    parse_backend_time,
    skipped_page_warning,
)
from angareion.fake_backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.github import GitHubIssuesBackend
from angareion.log_cache import LOG_CACHE_VERSION, LogCache, log_cache_path
from angareion.messages import Message, serialize_message
from angareion.registry import FakeRegistryClient
from angareion.transport import HttpResponse

REPO = "owner/repo"
ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
OTHER = "mypmo-pm-m1"
TOKEN = "gh" + "p_FAKE_PAGED_READ_TOKEN"
NOW = "2026-11-25T12:00:00Z"  # the Fake backend's clock (`status` reads it as the backend time)
DATE = "Wed, 25 Nov 2026 10:00:00 GMT"  # the GitHub listing's Date header unless a test sets another


def at(minute: int, second: int = 0) -> str:
    """A backend time on 2026-11-25, `minute` minutes after 10:00 UTC."""
    moment = datetime(2026, 11, 25, 10, tzinfo=timezone.utc) + timedelta(minutes=minute, seconds=second)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def chan(current: int, *retired: RetiredPage, channel: str = "general") -> Entity:
    return Entity(channel=channel, backend="github-main", repo=REPO, issue=current, retired=tuple(retired))


def old(issue: int, until: str, *, side: bool = False) -> RetiredPage:
    return RetiredPage(issue=issue, until=until, side=side)


def _texts(messages) -> list[str]:
    return [m.content for m in messages]


class GitHubListing:
    """A fake GitHub for issue comments: `since`, 100 per listing page with `Link`, ETags, Date, refusals and failures per issue."""

    def __init__(self) -> None:
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.dates: dict[int, str] = {}
        self.refusals: dict[int, HttpResponse] = {}
        self.failures: dict[int, Exception] = {}
        self.requests: list[dict[str, Any]] = []

    def add(self, issue: int, comment_id: int, created_at: str, message: Message) -> None:
        self.comments.setdefault(issue, []).append(
            {
                "id": comment_id,
                "body": serialize_message(message),
                "created_at": created_at,
                "html_url": f"https://github.com/{REPO}/issues/{issue}#issuecomment-{comment_id}",
            }
        )

    def requests_to(self, issue: int) -> list[dict[str, Any]]:
        return [r for r in self.requests if re.search(rf"/issues/{issue}/comments", r["url"])]

    def request(self, method, url, headers=None, body=None, timeout=None) -> HttpResponse:
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {})})
        parts = urlsplit(url)
        match = re.fullmatch(r"/repos/owner/repo/issues/(\d+)/comments", parts.path)
        if method != "GET" or match is None:
            return HttpResponse(404, {}, b'{"message": "Not Found"}')
        issue = int(match.group(1))
        if issue in self.failures:
            raise self.failures[issue]
        if issue in self.refusals:
            return self.refusals[issue]
        query = parse_qs(parts.query)
        since = query.get("since", [None])[0]
        page = int(query.get("page", ["1"])[0])
        everything = sorted(self.comments.get(issue, []), key=lambda c: c["id"])
        items = [c for c in everything if since is None or parse_backend_time(c["created_at"]) >= parse_backend_time(since)]
        chunk = items[(page - 1) * 100 : page * 100]
        response_headers = {"date": self.dates.get(issue, DATE), "content-type": "application/json"}
        etag = f'W/"{issue}-{len(everything)}-{since or "all"}"'
        if page == 1:
            response_headers["etag"] = etag
            if (headers or {}).get("If-None-Match") == etag:
                return HttpResponse(304, response_headers, b"")
        if page * 100 < len(items):
            next_query = f"per_page=100&page={page + 1}" + (f"&since={quote(since, safe='')}" if since else "")
            response_headers["link"] = f'<https://api.github.com/repos/{REPO}/issues/{issue}/comments?{next_query}>; rel="next"'
        return HttpResponse(200, response_headers, json.dumps(chunk).encode("utf-8"))


class Pages:
    """One channel's comments on one backend, through the same calls for the Fake and for GitHub."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.listing = GitHubListing()
        if kind == "fake":
            self.backend = FakeBackend(StaticResolver({}), clock=lambda: NOW)
        else:
            self.backend = GitHubIssuesBackend(StaticResolver({}), token=TOKEN, transport=self.listing)

    def post(self, issue: int, comment_id: int, created_at: str, text: str, *, to: str | None = ME, channel: str = "general") -> None:
        message = Message(from_=PM, to=to, channel=channel, content=text, id=f"01MSG{comment_id:021d}", at=created_at)
        if self.kind == "fake":
            page = Entity(channel=channel, backend="github-main", repo=REPO, issue=issue)
            self.backend.deliver(page, message, comment_id=comment_id, created_at=created_at)
        else:
            self.listing.add(issue, comment_id, created_at, message)

    def refuse(self, issue: int, status: int) -> None:
        """Make one page's listing answer `status` (404, 410, or a 403 that is not a rate limit)."""
        if self.kind == "fake":
            self.backend.set_page_error(issue, PageUnavailableError(f"Failed to read messages: HTTP {status}", status=status))
        else:
            body = {403: b'{"message": "Resource not accessible by integration"}', 404: b'{"message": "Not Found"}'}.get(status, b'{"message": "Gone"}')
            self.listing.refusals[issue] = HttpResponse(status, {}, body)

    def server_error(self, issue: int, status: int) -> None:
        """Make one page's listing answer a 5xx: an error that is not a refusal, so it is never skipped."""
        if self.kind == "fake":
            self.backend.set_page_error(issue, BackendError(f"Failed to read messages: HTTP {status}"))
        else:
            self.listing.refusals[issue] = HttpResponse(status, {}, b'{"message": "Server Error"}')

    def rate_limit(self, issue: int) -> None:
        if self.kind == "fake":
            self.backend.set_page_error(issue, RateLimitError("GitHub API rate limit reached", kind="primary"))
        else:
            self.listing.refusals[issue] = HttpResponse(
                403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}, b'{"message": "API rate limit exceeded"}'
            )

    def network_down(self, issue: int) -> None:
        if self.kind == "fake":
            self.backend.set_page_error(issue, BackendError("HTTP request failed: connection reset"))
        else:
            self.listing.failures[issue] = OSError("connection reset")

    def recover(self, issue: int) -> None:
        if self.kind == "fake":
            self.backend.set_page_error(issue, None)
        else:
            self.listing.refusals.pop(issue, None)
            self.listing.failures.pop(issue, None)

    def read(self, entity: Entity, cursor: Cursor, **kwargs):
        return self.backend.read(entity, cursor, **kwargs)

    def check(self, entity: Entity, cursor: Cursor, **kwargs) -> bool:
        return self.backend.check(entity, cursor, **kwargs)


@pytest.fixture(params=["fake", "github"])
def pages(request) -> Pages:
    return Pages(request.param)


@pytest.fixture
def github_pages() -> Pages:
    return Pages("github")


# -- C-10: reading across pages ------------------------------------------------


def test_c10_read_returns_every_page_in_comment_id_order(pages):
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    pages.post(10, 3, at(2), "r2")
    pages.post(20, 4, at(3), "n2")

    result = pages.read(chan(20, old(10, at(0))), Cursor())

    assert _texts(result.messages) == ["r1", "n1", "r2", "n2"]
    assert set(result.cursor.pages) == {10, 20}
    assert result.warnings == ()


def test_c10_a_second_read_returns_nothing_and_check_is_false(pages):
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    ent = chan(20, old(10, at(0)))
    first = pages.read(ent, Cursor())

    assert pages.read(ent, first.cursor).messages == []
    assert pages.check(ent, first.cursor) is False


def test_c10_single_page_channel_keeps_its_page_equal_to_the_main_cursor(pages):
    pages.post(20, 1, at(0), "n1")
    ent = chan(20)

    first = pages.read(ent, Cursor())

    assert _texts(first.messages) == ["n1"]
    assert first.cursor.pages == {20: dataclasses.replace(first.cursor, pages=None)}
    assert pages.read(ent, first.cursor).messages == []


def test_c10_the_first_listing_date_is_the_floor_for_every_page_github(github_pages):
    github_pages.listing.dates[10] = "Wed, 25 Nov 2026 10:00:00 GMT"
    github_pages.listing.dates[20] = "Wed, 25 Nov 2026 11:00:00 GMT"
    github_pages.post(10, 1, at(30), "old-broadcast", to=None)
    github_pages.post(20, 2, at(30), "same-time-broadcast", to=None)

    result = github_pages.read(chan(20, old(10, at(0))), Cursor(), recipient=ME, new_recipient=True)

    # The floor is the first listing's Date (10:00) for both pages, so both broadcasts count.
    assert _texts(result.messages) == ["old-broadcast", "same-time-broadcast"]


def test_c11_check_finds_a_new_message_on_any_page(pages):
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    ent = chan(20, old(10, at(0)))
    cursor = pages.read(ent, Cursor()).cursor

    pages.post(10, 3, at(2), "r2")

    assert pages.check(ent, cursor) is True


def test_c11_check_finds_a_new_message_only_on_the_current_page(pages):
    """PR-2c review 中-2: both pages are read and the retired one is clean, so only the current page can answer."""
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    ent = chan(20, old(10, at(0)))
    cursor = pages.read(ent, Cursor()).cursor
    assert pages.check(ent, cursor) is False

    pages.post(20, 3, at(2), "n2")

    assert pages.check(ent, cursor) is True


def test_c11_a_never_read_page_with_a_message_for_me_is_new(pages):
    pages.post(10, 1, at(0), "for-me")  # on a page the reader has never read
    pages.post(20, 2, at(1), "n1", to=OTHER)
    ent = chan(20, old(10, at(0)))

    assert pages.check(ent, Cursor(), recipient=ME) is True


def test_c11_listing_of_an_unread_page_is_bounded_github(github_pages):
    for i in range(1, 251):
        github_pages.post(20, i, at(0, i), f"x{i}", to=OTHER)
    ent = chan(20)

    # The reader is ME: every message is addressed to someone else, so nothing is new for it.
    results = [github_pages.check(ent, Cursor(), recipient=ME) for _ in range(5)]

    assert results == [False] * 5
    listing = github_pages.listing.requests_to(20)
    assert len(listing) <= 3 + 4  # three listing pages once, then one request per check
    assert all("since=" in request["url"] for request in listing[3:])


def test_c11_new_cursor_retired_page_listing_is_bounded_github(github_pages):
    for i in range(1, 251):
        github_pages.post(10, i, at(0, i), f"x{i}", to=OTHER)
    ent = chan(20, old(10, at(0)))

    results = [github_pages.check(ent, Cursor(), recipient=ME) for _ in range(5)]

    assert results == [False] * 5
    assert len(github_pages.listing.requests_to(10)) <= 3 + 4


def test_c11_the_etag_of_one_page_survives_a_read_of_another_github(github_pages):
    github_pages.post(10, 1, at(0), "r1")
    ent = chan(20, old(10, at(0)))
    cursor = github_pages.read(ent, Cursor()).cursor
    assert github_pages.check(ent, cursor) is False  # stores the old page's etag

    github_pages.post(20, 2, at(1), "n1")
    cursor = github_pages.read(ent, cursor).cursor  # only the current page moves
    assert github_pages.check(ent, cursor) is False  # n1 was just read; the old page still answers 304

    assert "If-None-Match" in github_pages.listing.requests_to(10)[-1]["headers"]


def test_c11_a_read_from_the_start_does_not_narrow_a_cursor_read_window_github(github_pages):
    """PR-2c review 低-3: a page read from the start keeps its scan window under `|unread`.

    The unread check sees only an out-of-scope comment (to OTHER) and moves the
    window to it. A cursor check of the same page must still see the in-scope
    comment that arrived behind that window, so the two modes cannot share a key.
    """
    github_pages.post(20, 2, at(5), "for-other", to=OTHER)
    ent = chan(20)
    assert github_pages.check(ent, Cursor(), recipient=ME) is False

    github_pages.post(20, 3, at(3), "for-me")
    behind = Cursor(position="1", at=at(2), seen=[])

    assert github_pages.check(ent, behind, recipient=ME) is True


def test_c12_read_after_rollover_gives_each_new_message_once(pages):
    pages.post(10, 1, at(0), "m1")
    pages.post(10, 2, at(1), "m2")
    before = pages.read(chan(10), Cursor())
    assert _texts(before.messages) == ["m1", "m2"]

    # The rollover is at 10:05. The old page takes two more messages, the new page three.
    after_rollover = chan(20, old(10, at(5)))
    pages.post(10, 3, at(6), "m3")
    pages.post(10, 4, at(6, 1), "m4")
    pages.post(20, 5, at(6, 2), "n1")
    pages.post(20, 6, at(6, 3), "n2")
    pages.post(20, 7, at(6, 4), "n3")

    result = pages.read(after_rollover, before.cursor)

    assert _texts(result.messages) == ["m3", "m4", "n1", "n2", "n3"]
    assert pages.read(after_rollover, result.cursor).messages == []


def test_c12_a_reader_past_the_grace_period_does_not_ask_the_old_page_github(github_pages):
    github_pages.post(10, 1, "2026-11-23T10:00:00Z", "old")
    github_pages.post(20, 2, "2026-11-25T11:00:00Z", "new")
    cursor = Cursor(position="1", at="2026-11-25T11:00:00Z", seen=[])  # 11:00 is past 10:00 two days on

    result = github_pages.read(chan(20, old(10, "2026-11-23T10:00:00Z")), cursor)

    assert _texts(result.messages) == ["new"]
    assert github_pages.listing.requests_to(10) == []


# -- C-22: an older page that cannot be read ----------------------------------


@pytest.mark.parametrize("status", [404, 410, 403])
def test_c22_an_older_page_that_cannot_be_listed_is_skipped_with_a_warning(pages, status):
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    pages.refuse(10, status)

    result = pages.read(chan(20, old(10, at(0))), Cursor())

    assert _texts(result.messages) == ["n1"]
    assert result.warnings == (skipped_page_warning("general", status),)


def test_c22_the_skip_warning_has_no_prefix_and_names_no_issue():
    text = skipped_page_warning("general", 404)

    assert text == "an older page of channel 'general' could not be read (HTTP 404); skipped"
    assert not text.startswith("Warning")


def test_c22_a_read_page_that_fails_then_recovers_without_resending(pages):
    pages.post(10, 1, at(0), "r0")
    ent = chan(20, old(10, at(0)))
    first = pages.read(ent, Cursor())
    assert _texts(first.messages) == ["r0"]

    pages.post(10, 2, at(1), "r1")
    pages.post(10, 3, at(1, 1), "r2")
    pages.post(20, 4, at(3), "n1")
    pages.refuse(10, 404)
    second = pages.read(ent, first.cursor)
    assert _texts(second.messages) == ["n1"]
    assert second.cursor.pages[10] == first.cursor.pages[10]  # the skipped page keeps its place

    pages.recover(10)
    third = pages.read(ent, second.cursor)
    assert _texts(third.messages) == ["r1", "r2"]
    assert pages.read(ent, third.cursor).messages == []


def test_c22_an_unread_page_that_fails_is_read_from_the_start_after_recovery(pages):
    pages.post(20, 10, at(5), "p1")
    first = pages.read(chan(20), Cursor())
    assert _texts(first.messages) == ["p1"]

    pages.post(5, 1, at(0), "s1")
    pages.post(5, 2, at(0, 1), "s2")
    pages.post(5, 3, at(0, 2), "s3")
    ent = chan(20, old(5, "2026-11-25T09:00:00Z"))
    pages.refuse(5, 404)
    second = pages.read(ent, first.cursor)
    assert second.messages == []
    assert second.cursor.pages[5] == Cursor()  # never read, so it starts empty

    pages.recover(5)
    third = pages.read(ent, second.cursor)
    assert _texts(third.messages) == ["s1", "s2", "s3"]
    assert pages.read(ent, third.cursor).messages == []


def test_c22_an_old_cursor_without_pages_keeps_the_retired_page_at_the_main_cursor(pages):
    pages.post(10, 1, at(0), "r0")
    first = pages.read(chan(10), Cursor())
    old_file_cursor = dataclasses.replace(first.cursor, pages=None)  # the three columns only

    pages.post(10, 2, at(1), "r1")
    pages.post(20, 3, at(2), "n1")
    ent = chan(20, old(10, at(0)))
    pages.refuse(10, 404)
    second = pages.read(ent, old_file_cursor)
    assert _texts(second.messages) == ["n1"]
    assert second.cursor.pages[10] == Cursor(
        position=old_file_cursor.position, at=old_file_cursor.at, seen=list(old_file_cursor.seen)
    )

    pages.recover(10)
    third = pages.read(ent, second.cursor)
    assert _texts(third.messages) == ["r1"]  # r0 is not sent again
    assert pages.read(ent, third.cursor).messages == []


def test_c22_a_current_page_that_cannot_be_listed_still_fails(github_pages):
    github_pages.post(20, 1, at(1), "n1")
    github_pages.refuse(20, 404)

    with pytest.raises(BackendError) as caught:
        github_pages.read(chan(20, old(10, at(0))), Cursor())

    assert str(caught.value) == "Failed to read messages: HTTP 404"  # the same text as before PR-2c


def test_c22_a_rate_limit_on_an_older_page_is_not_skipped(pages):
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    pages.rate_limit(10)

    with pytest.raises(RateLimitError):
        pages.read(chan(20, old(10, at(0))), Cursor())


def test_c22_a_network_failure_on_an_older_page_is_not_skipped(pages):
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    pages.network_down(10)

    with pytest.raises(BackendError) as caught:
        pages.read(chan(20, old(10, at(0))), Cursor())

    assert not isinstance(caught.value, PageUnavailableError)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_c22_a_server_error_on_an_older_page_is_not_skipped(pages, tmp_path, status):
    """PR-2c review 低-1: only 404, 410 and a refused 403 are skipped; a 5xx is raised by read, check and the log mirror."""
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    pages.server_error(10, status)
    ent = chan(20, old(10, at(0)))

    with pytest.raises(BackendError) as read_error:
        pages.read(ent, Cursor())
    with pytest.raises(BackendError) as check_error:
        pages.check(ent, Cursor())
    with pytest.raises(BackendError) as sync_error:
        LogCache(tmp_path, ME).sync(pages.backend, ent)

    for caught in (read_error, check_error, sync_error):
        assert not isinstance(caught.value, PageUnavailableError)


# -- C-23: the cursor of every page ---------------------------------------------


@pytest.mark.parametrize("side", [False, True])
def test_c23_a_page_that_reaches_the_config_late_is_read_from_the_start(pages, side):
    pages.post(20, 10, at(5), "p1")
    first = pages.read(chan(20), Cursor())
    assert _texts(first.messages) == ["p1"]

    # Two messages written before the reader ever saw this page, both below its position.
    pages.post(5, 1, at(0), "s1")
    pages.post(5, 2, at(0, 1), "s2")
    ent = chan(20, old(5, "2026-11-25T09:00:00Z", side=side))
    second = pages.read(ent, first.cursor)

    assert _texts(second.messages) == ["s1", "s2"]
    assert pages.read(ent, second.cursor).messages == []


def test_c23_an_already_read_page_is_not_read_from_the_start_github(github_pages):
    github_pages.post(5, 1, at(0), "s1")
    github_pages.post(20, 2, at(5), "p1")
    ent = chan(20, old(5, "2026-11-25T09:00:00Z"))
    first = github_pages.read(ent, Cursor())
    assert _texts(first.messages) == ["s1", "p1"]

    assert github_pages.read(ent, first.cursor).messages == []
    last_old = github_pages.listing.requests_to(5)[-1]
    assert "since=" in last_old["url"]


# -- the CLI on both backends: read, wait, log ----------------------------------


class CliEnv:
    """`main` over channels in one config, with a Pages board as the backend."""

    def __init__(self, kind: str, tmp_path, monkeypatch) -> None:
        config_file = tmp_path / "config.json"
        self.state_dir = tmp_path / "state"
        clone = tmp_path / "clone"
        clone.mkdir()
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        self.config = Config(
            identity=ME,
            config_dir=clone,
            registry_repo="owner/MyAiStorage",
            registry_issue=77,
            token_file=token_file,
            state_dir=self.state_dir,
            config_file=config_file,
        )
        save_config(self.config, config_file)
        monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
        monkeypatch.setenv("ANGAREION_STATE_DIR", str(self.state_dir))
        monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))
        self.git = FakeGit({f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", REPO)})}, fetch_head_mtime=1000.0)
        self.store = ChannelStore(clone, git=self.git, interval=0)
        self.pages = Pages(kind)
        self.registry = FakeRegistryClient()

    def set_channels(self, channels: dict[str, tuple[int, list[RetiredPage]]]) -> None:
        entries = {}
        for name, (issue, retired) in channels.items():
            entry: dict[str, Any] = {"backend": "github-main", "issue": issue}
            if retired:
                entry["retired"] = [
                    {"issue": r.issue, "until": r.until, **({"side": True} if r.side else {})} for r in retired
                ]
            entries[name] = entry
        self.git.set_file(CHANNELS_PATH, json.dumps({"channels": entries, "version": 1}))

    def run(self, argv: list[str]) -> int:
        return main(argv, backend=self.pages.backend, store=self.store, registry=self.registry)


@pytest.fixture(params=["fake", "github"])
def cli(request, tmp_path, monkeypatch) -> CliEnv:
    env = CliEnv(request.param, tmp_path, monkeypatch)
    env.set_channels({"general": (20, [old(10, at(0))])})
    return env


@pytest.fixture
def fake_cli(tmp_path, monkeypatch) -> CliEnv:
    """The Fake only: `channel status` needs page sizes, which the GitHub listing fake does not serve."""
    env = CliEnv("fake", tmp_path, monkeypatch)
    env.set_channels({"general": (20, [old(10, at(0))])})
    return env


def _skip_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if "could not be read" in line]


def test_c22_read_warns_once_and_still_prints_the_rest(cli, capsys):
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")
    cli.pages.refuse(10, 404)

    assert cli.run(["read", "--channel", "general", "--json"]) == 0

    captured = capsys.readouterr()
    assert [m["content"] for m in json.loads(captured.out)] == ["n1"]
    assert _skip_lines(captured.err) == [f"Warning: {skipped_page_warning('general', 404)}"]
    assert "Warning: Warning:" not in captured.err


def test_c22_log_warns_once_without_a_double_prefix(cli, capsys):
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")
    cli.pages.refuse(10, 404)

    assert cli.run(["log", "--channel", "general", "--json"]) == 0

    captured = capsys.readouterr()
    assert [m["content"] for m in json.loads(captured.out)] == ["n1"]
    assert captured.err.splitlines() == [f"Warning: {skipped_page_warning('general', 404)}"]


def test_c22_wait_checks_quietly_past_a_skipped_page(cli, capsys):
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")
    cli.pages.refuse(10, 404)

    assert cli.run(["wait", "--channel", "general", "--timeout", "0"]) == 0

    assert _skip_lines(capsys.readouterr().err) == []


def test_c22_a_skipped_page_does_not_fail_other_channels(cli, capsys):
    cli.set_channels({"general": (20, [old(10, at(0))]), "other": (30, [])})
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")
    cli.pages.post(30, 3, at(2), "o1", channel="other")
    cli.pages.refuse(10, 404)

    assert cli.run(["read", "--json"]) == 0

    captured = capsys.readouterr()
    assert sorted(m["content"] for m in json.loads(captured.out)) == ["n1", "o1"]
    assert len(_skip_lines(captured.err)) == 1


def test_c22_every_channel_with_a_skipped_page_warns_in_name_order(cli, capsys):
    """PR-2c review 低-3: each channel's skipped page is warned about once, sorted by channel name."""
    cli.set_channels({"zeta": (20, [old(10, at(0))]), "alpha": (30, [old(40, at(0))])})
    cli.pages.post(10, 1, at(0), "z-r1", channel="zeta")
    cli.pages.post(20, 2, at(1), "z-n1", channel="zeta")
    cli.pages.post(40, 3, at(0), "a-r1", channel="alpha")
    cli.pages.post(30, 4, at(1), "a-n1", channel="alpha")
    cli.pages.refuse(10, 404)
    cli.pages.refuse(40, 404)

    assert cli.run(["read", "--json"]) == 0

    captured = capsys.readouterr()
    assert sorted(m["content"] for m in json.loads(captured.out)) == ["a-n1", "z-n1"]
    assert _skip_lines(captured.err) == [
        f"Warning: {skipped_page_warning('alpha', 404)}",
        f"Warning: {skipped_page_warning('zeta', 404)}",
    ]


def test_c22_a_current_page_that_cannot_be_listed_is_exit_2(cli, capsys):
    cli.pages.post(20, 2, at(1), "n1")
    cli.pages.refuse(20, 404)

    assert cli.run(["read", "--channel", "general"]) == 2

    assert "Failed to read messages: HTTP 404" in capsys.readouterr().err


def test_c14_log_reads_every_page_into_one_mirror(cli, capsys):
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")

    assert cli.run(["log", "--channel", "general", "--json"]) == 0

    assert [m["content"] for m in json.loads(capsys.readouterr().out)] == ["r1", "n1"]
    data = json.loads(log_cache_path(cli.state_dir, ME, "general").read_text(encoding="utf-8"))
    assert data["version"] == LOG_CACHE_VERSION == 3
    assert set(data["pages"]) == {"10", "20"}


def test_c14_a_page_that_cannot_be_listed_keeps_its_mirrored_messages(cli, capsys):
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")
    assert cli.run(["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()

    cli.pages.refuse(10, 404)
    cli.pages.post(20, 3, at(2), "n2")
    assert cli.run(["log", "--channel", "general", "--json"]) == 0

    captured = capsys.readouterr()
    assert [m["content"] for m in json.loads(captured.out)] == ["r1", "n1", "n2"]
    assert len(_skip_lines(captured.err)) == 1


def test_c14_messages_of_a_page_the_channel_no_longer_has_are_dropped(tmp_path):
    pages = Pages("fake")
    pages.post(10, 1, at(0), "r1")
    pages.post(20, 2, at(1), "n1")
    cache = LogCache(tmp_path, ME)

    before = cache.sync(pages.backend, chan(20, old(10, at(0))))
    after = cache.sync(pages.backend, chan(20))

    assert _texts(before) == ["r1", "n1"]
    assert _texts(after) == ["n1"]


def test_c14_the_mirror_is_rebuilt_when_the_channel_moves_to_another_repo(tmp_path):
    """PR-2c review 低-2: a cached file for another repo is not trusted; the mirror is rebuilt from the backend."""
    first = Pages("fake")
    first.post(10, 1, at(0), "r1")
    first.post(20, 2, at(1), "n1")
    cache = LogCache(tmp_path, ME)
    assert _texts(cache.sync(first.backend, chan(20, old(10, at(0))))) == ["r1", "n1"]

    elsewhere = Pages("fake")
    moved = dataclasses.replace(chan(20, old(10, at(0))), repo="owner/other")
    elsewhere.backend.deliver(
        dataclasses.replace(moved, issue=20, retired=()),
        Message(from_=PM, to=ME, channel="general", content="n9", id="01MSG" + "0" * 21, at=at(5)),
        comment_id=3,
        created_at=at(5),
    )

    assert _texts(cache.sync(elsewhere.backend, moved)) == ["n9"]


def test_c14_a_skipped_page_keeps_its_place_in_the_mirror(cli, capsys):
    """PR-2c review 低-2: a page that cannot be listed keeps its `pages` entry in the file."""
    cli.pages.post(10, 1, at(0), "r1")
    cli.pages.post(20, 2, at(1), "n1")
    assert cli.run(["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()
    path = log_cache_path(cli.state_dir, ME, "general")
    before = json.loads(path.read_text(encoding="utf-8"))["pages"]["10"]

    cli.pages.refuse(10, 404)
    cli.pages.post(20, 3, at(2), "n2")
    assert cli.run(["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["pages"]) == {"10", "20"}
    assert data["pages"]["10"] == before


# -- C-24 and C-26 ------------------------------------------------------------


def test_c24_capabilities_prints_one_line_and_exits_0(capsys):
    assert main(["--capabilities"]) == 0

    assert capsys.readouterr().out == "paged-read\n"


def _status_rows(env: CliEnv, capsys) -> tuple[int, dict[str, dict[str, Any]], str, str]:
    """(exit code, rows by name, stdout, stderr) of `channel status --json`."""
    rc = env.run(["channel", "status", "--json"])
    captured = capsys.readouterr()
    rows = {row["name"]: row for row in json.loads(captured.out)}
    return rc, rows, captured.out, captured.err


def test_c26_pages_are_the_current_page_plus_the_retired_ones(fake_cli, capsys):
    fake_cli.set_channels({"general": (30, [old(10, at(0)), old(20, at(10))])})

    rc, rows, out, _ = _status_rows(fake_cli, capsys)

    assert rc == 0
    assert rows["general"]["pages"] == 3
    assert rows["registry"]["pages"] == 1
    assert rows["general"]["flags"] == []
    assert "issue" not in out  # no issue numbers in the output (C-26)


def test_c26_side_flag_when_a_recent_side_page_has_messages(fake_cli, capsys):
    fake_cli.set_channels({"general": (30, [old(10, at(0)), old(20, at(5), side=True)])})
    fake_cli.pages.post(20, 9, at(6), "stray")

    rc, rows, _, _ = _status_rows(fake_cli, capsys)

    assert rc == 0
    assert rows["general"]["flags"] == ["side"]


def test_c26_late_flag_when_the_last_retired_page_gets_messages_after_the_fill_window(fake_cli, capsys):
    fake_cli.set_channels({"general": (30, [old(10, at(0))])})
    fake_cli.pages.post(10, 1, at(0), "early")
    fake_cli.pages.post(10, 2, "2026-11-26T10:30:00Z", "late")

    rc, rows, _, _ = _status_rows(fake_cli, capsys)

    assert rc == 0
    assert rows["general"]["flags"] == ["late"]


def test_c26_no_late_flag_for_a_message_inside_the_fill_window(fake_cli, capsys):
    """PR-2c review 低-3: a message on the last retired page within PAGE_FILL_WINDOW is not `late`."""
    fake_cli.set_channels({"general": (30, [old(10, at(0))])})
    fake_cli.pages.post(10, 1, at(0), "early")
    fake_cli.pages.post(10, 2, at(60), "inside-the-window")

    rc, rows, _, _ = _status_rows(fake_cli, capsys)

    assert rc == 0
    assert rows["general"]["flags"] == []


def test_c26_retired_pages_older_than_seven_days_get_no_flags(fake_cli, capsys):
    fake_cli.set_channels(
        {"general": (30, [old(10, "2026-11-10T10:00:00Z"), old(20, "2026-11-11T10:00:00Z", side=True)])}
    )
    fake_cli.pages.post(10, 1, "2026-11-12T10:00:00Z", "late-but-old")
    fake_cli.pages.post(20, 2, "2026-11-12T10:00:00Z", "side-but-old")

    rc, rows, _, _ = _status_rows(fake_cli, capsys)

    assert rc == 0
    assert rows["general"]["flags"] == []


def test_c26_a_failed_flag_read_is_unknown_and_exit_2(fake_cli, capsys):
    fake_cli.set_channels({"general": (30, [old(10, at(0))])})
    fake_cli.pages.backend.set_page_error(10, BackendError("HTTP 500"))

    rc, rows, _, err = _status_rows(fake_cli, capsys)

    assert rc == 2
    assert rows["general"]["flags"] == ["unknown"]
    assert rows["general"]["count"] == 0
    assert "page flags could not be read" in err
