"""Unit tests for the wait ETag cache (#96): persisted conditional requests, fake transport only.

The fake transport models the one GitHub behaviour the design relies on: a
request whose `If-None-Match` equals the current body's ETag gets 304, and
anything else gets the body with a 200. `serve` replaces the body and its
ETag, which is how a test makes a stored ETag stale.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from typing import Any

import pytest

from _angareion_fakes import StaticResolver, entity
from angareion.backend import Cursor, ETagRecord
from angareion.github import GitHubIssuesBackend, HttpResponse
from angareion.messages import Message, serialize_message
from angareion.state import CursorStore, FileETagCache, wait_state_file_path

ME = "MyLinuxPool-impl-2"
PM = "LLMGateway-pm"
GENERAL = entity(channel="general", issue=10)
OTHER = entity(channel="other", backend="github-main", repo="owner/repo2", issue=7)
COMMENTS = "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=100"
COMMENTS_SINCE = COMMENTS + "&since=2026-10-10T11%3A59%3A00Z"
OTHER_COMMENTS = "https://api.github.com/repos/owner/repo2/issues/7/comments?per_page=100"
OTHER_SINCE = OTHER_COMMENTS + "&since=2026-10-10T11%3A59%3A00Z"
CURSOR = Cursor(position="100", at="2026-10-10T12:00:00Z", seen=["100"])
NEW_CURSOR = Cursor(position="110", at="2026-10-10T13:00:00Z", seen=["110"])
NEW_COMMENTS_SINCE = COMMENTS + "&since=2026-10-10T12%3A59%3A00Z"
WAIT_KEY = "owner/repo#10|*"
NEW_AT = "2026-10-10T13:00:00Z"


class ETagTransport:
    """A URL -> (body, ETag) fake that answers If-None-Match the way GitHub does."""

    def __init__(self) -> None:
        self.routes: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[int] = []

    def serve(self, url: str, comments: list[dict[str, Any]], *, etag: str | None, link: str | None = None) -> None:
        self.routes[url] = {"body": json.dumps(comments).encode("utf-8"), "etag": etag, "link": link}

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {})})
        entry = self.routes.get(url)
        if entry is None:
            self.statuses.append(404)
            return HttpResponse(404, {}, b'{"message": "Not Found"}')
        sent = (headers or {}).get("If-None-Match")
        if sent is not None and sent == entry["etag"]:
            self.statuses.append(304)
            return HttpResponse(304, {"etag": entry["etag"]}, b"")
        response_headers: dict[str, str] = {}
        if entry["etag"]:
            response_headers["etag"] = entry["etag"]
        if entry["link"]:
            response_headers["link"] = entry["link"]
        self.statuses.append(200)
        return HttpResponse(200, response_headers, entry["body"])


def _comment(
    comment_id: int,
    *,
    created_at: str = "2026-10-10T10:00:00Z",
    to: str | None = ME,
    channel: str = "general",
) -> dict[str, Any]:
    message = Message(
        from_=PM,
        to=to,
        channel=channel,
        content=f"message {comment_id}",
        id="01JABCDEF0123456789ABCDEF0",
        at="2026-10-10T10:00:00+00:00",
    )
    return {
        "id": comment_id,
        "body": serialize_message(message),
        "created_at": created_at,
        "updated_at": created_at,
    }


def _comments(count: int) -> list[dict[str, Any]]:
    return [_comment(comment_id) for comment_id in range(1, count + 1)]


def _backend(transport: ETagTransport, cache: FileETagCache) -> GitHubIssuesBackend:
    return GitHubIssuesBackend(StaticResolver([GENERAL, OTHER]), token="fake_token", transport=transport, etag_cache=cache)


def _targets(state_dir) -> dict[str, Any]:
    path = wait_state_file_path(state_dir, ME)
    return json.loads(path.read_text(encoding="utf-8"))["targets"]


def test_a_restarted_wait_sends_if_none_match_and_gets_304(tmp_path):
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"v1"')

    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    assert "If-None-Match" not in transport.requests[-1]["headers"]

    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    assert transport.requests[-1]["headers"]["If-None-Match"] == 'W/"v1"'
    assert transport.statuses == [200, 304]


def test_ten_restarts_make_only_the_first_full_request(tmp_path):
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"v1"')

    for _ in range(10):
        assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False

    full = [request for request in transport.requests if "If-None-Match" not in request["headers"]]
    assert len(full) == 1
    assert transport.statuses.count(304) == 9


def test_an_invalid_etag_is_replaced_and_a_found_message_clears_the_record(tmp_path):
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"clean-1"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False

    transport.serve(COMMENTS_SINCE, [_comment(101, created_at=NEW_AT)], etag='W/"changed-2"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is True
    assert transport.requests[-1]["headers"]["If-None-Match"] == 'W/"clean-1"'
    # The response that contained a new message is not remembered as "clean".
    assert _targets(tmp_path) == {}

    # The unread message is still called out after a restart, not hidden by a 304.
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is True
    assert "If-None-Match" not in transport.requests[-1]["headers"]

    # Once a clean response is seen again, its new ETag is what the restart sends.
    transport.serve(COMMENTS_SINCE, [], etag='W/"clean-3"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    assert transport.requests[-1]["headers"]["If-None-Match"] == 'W/"clean-3"'


@pytest.mark.parametrize(
    "payload",
    [
        None,  # missing file
        "not json",
        '{"version": 2, "targets": {}}',
        '{"version": 1, "targets": []}',
        json.dumps(
            {
                "version": 1,
                "targets": {
                    WAIT_KEY: {
                        "url": COMMENTS_SINCE,
                        "cursor": {"position": "99", "at": "2026-10-10T11:00:00Z", "seen": ["99"]},
                        "etag": 'W/"other-cursor"',
                    }
                },
            }
        ),
        json.dumps({"version": 1, "targets": {WAIT_KEY: {"url": COMMENTS_SINCE, "cursor": {}, "etag": ""}}}),
    ],
)
def test_a_bad_or_mismatched_state_file_falls_back_to_a_full_query(tmp_path, payload):
    state_file = wait_state_file_path(tmp_path, ME)
    if payload is not None:
        state_file.write_text(payload, encoding="utf-8")

    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [_comment(101, created_at=NEW_AT)], etag='W/"v1"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is True
    assert "If-None-Match" not in transport.requests[0]["headers"]

    if payload is None:
        # A full query that finds a message clears the key; with no file there is nothing to create.
        assert not state_file.exists()
    else:
        # A file that could not be used (or whose record was dropped) is rewritten as valid JSON.
        assert json.loads(state_file.read_text(encoding="utf-8")) == {"version": 1, "targets": {}}


def test_a_record_from_another_query_window_is_still_usable_for_the_same_cursor(tmp_path):
    """REVIEW-96 M1: the record's URL is a window note; cursor + mode decide reuse.

    A 304 then proves the current representation is the one that was fully
    scanned clean; a changed representation is a 200 and a full query.
    """
    state_file = wait_state_file_path(tmp_path, ME)
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "targets": {
                    WAIT_KEY: {
                        "url": COMMENTS + "&since=2026-10-10T13%3A29%3A00Z",
                        "cursor": {"position": "100", "at": "2026-10-10T12:00:00Z", "seen": ["100"]},
                        "etag": 'W/"other-window"',
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [_comment(101, created_at=NEW_AT)], etag='W/"current"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is True
    assert transport.requests[0]["headers"]["If-None-Match"] == 'W/"other-window"'
    assert transport.statuses == [200]  # the representation changed -> full query, not a false 304


def test_each_entity_keeps_its_own_etag(tmp_path):
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"general"')
    transport.serve(OTHER_SINCE, [], etag='W/"other"')

    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(OTHER, CURSOR) is False
    assert "If-None-Match" not in transport.requests[-1]["headers"]  # OTHER never borrows GENERAL's

    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    assert transport.requests[-1]["headers"]["If-None-Match"] == 'W/"general"'
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(OTHER, CURSOR) is False
    assert transport.requests[-1]["headers"]["If-None-Match"] == 'W/"other"'


def test_another_identity_or_state_dir_does_not_reuse_the_etag(tmp_path):
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"v1"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False

    other_dir = tmp_path / "elsewhere"
    assert _backend(transport, FileETagCache(other_dir, ME)).check(GENERAL, CURSOR) is False
    assert "If-None-Match" not in transport.requests[-1]["headers"]

    assert _backend(transport, FileETagCache(tmp_path, "Other-pm")).check(GENERAL, CURSOR) is False
    assert "If-None-Match" not in transport.requests[-1]["headers"]


@pytest.mark.parametrize("count", [99, 100])
def test_a_full_first_page_is_never_reused(tmp_path, count):
    """99 comments fit with room to grow; 100 could grow a page 2 while page 1 stays identical."""
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, _comments(count), etag='W/"v1"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False

    transport.requests.clear()
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False
    sent = transport.requests[0]["headers"].get("If-None-Match")
    if count == 99:
        assert sent == 'W/"v1"'
    else:
        assert sent is None


def test_a_full_first_page_with_a_next_page_is_never_reused(tmp_path):
    page2 = COMMENTS_SINCE + "&page=2"
    cursor = Cursor(position="200", at="2026-10-10T12:00:00Z", seen=[])
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, _comments(100), etag='W/"v1"', link=f'<{page2}>; rel="next"')
    transport.serve(page2, [_comment(101)], etag='W/"v2"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, cursor) is False

    transport.requests.clear()
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, cursor) is False
    assert transport.requests[0]["headers"].get("If-None-Match") is None


def test_a_read_that_moves_the_cursor_invalidates_the_record(tmp_path):
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"old"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False

    wait_file = wait_state_file_path(tmp_path, ME)
    before = wait_file.read_text(encoding="utf-8")
    cursors = CursorStore(tmp_path, ME)
    cursors.save("channel:general", NEW_CURSOR)
    assert wait_file.read_text(encoding="utf-8") == before  # read never touches the wait state

    transport.serve(NEW_COMMENTS_SINCE, [], etag='W/"new"')
    transport.requests.clear()
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, NEW_CURSOR) is False
    assert transport.requests[0]["headers"].get("If-None-Match") is None  # the URL/since moved
    assert cursors.cursor("channel:general") == NEW_CURSOR  # wait did not move the cursor
    assert _targets(tmp_path)[WAIT_KEY]["url"].endswith("since=2026-10-10T12%3A59%3A00Z")


@pytest.mark.parametrize(
    "later_created, drift_since",
    [
        ("2026-10-10T13:01:00Z", "2026-10-10T13%3A00%3A00Z"),
        ("2026-10-10T14:29:00Z", "2026-10-10T13%3A29%3A00Z"),
    ],
)
def test_a_lagging_smaller_id_does_not_make_every_restart_pay_a_full_query(tmp_path, later_created, drift_since):
    """REVIEW-96 M1: the in-process scan window drifting past the cursor must not defeat the record.

    The cursor stops on id=150 at 13:00; a smaller id=120 was created later
    (the review's out-of-order case). 13:01 moves `since` to 13:00, 14:29 to
    13:29, both past the cursor's 12:59 window. Without the fix each restart
    pays two full queries forever; with it, the cursor-window record is reused
    (the scan window is never allowed to narrow the cross-restart query).
    A wait run is two checks; six restarts must leave exactly one full query.
    """
    cursor = Cursor(position="150", at="2026-10-10T13:00:00Z", seen=["120", "150"])
    wide = [_comment(150, created_at="2026-10-10T13:00:00Z"), _comment(120, created_at=later_created)]
    wide_url = COMMENTS + "&since=2026-10-10T12%3A59%3A00Z"
    drift_url = COMMENTS + "&since=" + drift_since
    transport = ETagTransport()
    transport.serve(wide_url, wide, etag='W/"wide"')
    if later_created.endswith("13:01:00Z"):
        # The 13:00 window still contains the 13:00 comment, so the body (and ETag) is identical.
        transport.serve(drift_url, wide, etag='W/"wide"')
    else:
        transport.serve(drift_url, [_comment(120, created_at=later_created)], etag='W/"narrow"')

    for _ in range(6):
        backend = _backend(transport, FileETagCache(tmp_path, ME))
        backend.check(GENERAL, cursor)
        backend.check(GENERAL, cursor)

    assert transport.statuses.count(200) == 1
    assert transport.statuses.count(304) == 11


def test_two_waiters_share_the_state_file_without_corrupting_it_or_hiding_messages(tmp_path):
    """REVIEW-96 L3 / spec: two writers interleave; the file stays valid JSON.

    A lost record can only cost a full query, never a false 304 or a missed
    message.
    """
    state_file = wait_state_file_path(tmp_path, ME)
    cache_a = FileETagCache(tmp_path, ME)
    cache_b = FileETagCache(tmp_path, ME)
    record_a = ETagRecord(url="https://example.invalid/a", cursor=CURSOR, etag='W/"a"')
    record_b = ETagRecord(url="https://example.invalid/b", cursor=NEW_CURSOR, etag='W/"b"')

    for step in range(6):
        cache = cache_a if step % 2 == 0 else cache_b
        cache.put(f"owner/repo#{step}|*", record_a if step % 2 == 0 else record_b)
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert isinstance(data["targets"], dict)
    assert len(json.loads(state_file.read_text(encoding="utf-8"))["targets"]) == 6

    # Last writer wins, and the other writer reads back the surviving record.
    cache_a.put("shared", record_a)
    cache_b.put("shared", record_b)
    assert cache_a.get("shared") == record_b

    # The stale records (a different cursor) never make a false 304: the
    # message in the listing is found by a full query.
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [_comment(101, created_at=NEW_AT)], etag='W/"new"')
    transport.requests.clear()
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is True
    # The only record with this entity key has NEW_CURSOR; it is not usable.
    assert transport.requests[0]["headers"].get("If-None-Match") is None


def test_wait_after_the_cursor_file_is_deleted_does_a_full_query(tmp_path):
    """REVIEW-96 L3 / spec scenario: deleting the cursor means starting over, not reusing the ETag."""
    cursors = CursorStore(tmp_path, ME)
    cursors.save("channel:general", CURSOR)
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"old"')
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, CURSOR) is False

    assert cursors.path.is_file()
    cursors.path.unlink()
    empty = cursors.cursor("channel:general")
    assert empty == Cursor()

    transport.serve(COMMENTS, [_comment(101, created_at=NEW_AT)], etag='W/"new"')
    transport.requests.clear()
    assert _backend(transport, FileETagCache(tmp_path, ME)).check(GENERAL, empty) is True
    assert transport.requests[0]["headers"].get("If-None-Match") is None


def test_the_state_directory_is_created_owner_only(tmp_path):
    state_dir = tmp_path / "fresh" / "state"
    old_umask = os.umask(0o022)
    try:
        cache = FileETagCache(state_dir, ME)
        cache.put("k", ETagRecord(url="https://example.invalid/x", cursor=CURSOR, etag='W/"1"'))
    finally:
        os.umask(old_umask)
    assert (state_dir.stat().st_mode & 0o777) == 0o700


def test_a_write_failure_warns_once_and_the_check_still_works(tmp_path):
    if getattr(os, "geteuid", lambda: -1)() == 0:
        pytest.skip("root can write into a read-only directory")
    messages: list[str] = []
    cache = FileETagCache(tmp_path, ME, warn=messages.append)
    transport = ETagTransport()
    transport.serve(COMMENTS_SINCE, [], etag='W/"v1"')
    backend = _backend(transport, cache)

    os.chmod(tmp_path, 0o500)
    try:
        assert backend.check(GENERAL, CURSOR) is False
        assert backend.check(GENERAL, CURSOR) is False
    finally:
        os.chmod(tmp_path, 0o700)

    assert len(messages) == 1
    assert "could not write the wait state file" in messages[0]
    assert "Messages are not affected" in messages[0]


def test_the_wait_state_file_is_separate_from_the_cursor_file(tmp_path):
    assert wait_state_file_path(tmp_path, ME) == tmp_path / "wait-MyLinuxPool-impl-2.json"
    assert CursorStore(tmp_path, ME).path == tmp_path / "cursor-MyLinuxPool-impl-2.json"


def test_the_file_cache_roundtrips_a_record(tmp_path):
    record = ETagRecord(url="https://example.invalid/x", cursor=CURSOR, etag='W/"v1"')
    cache = FileETagCache(tmp_path, ME)
    assert cache.get("k") is None
    cache.put("k", record)
    assert cache.get("k") == record
    assert FileETagCache(tmp_path, ME).get("k") == record
    cache.drop("k")
    assert cache.get("k") is None


def test_the_file_cache_does_not_create_a_file_for_a_drop(tmp_path):
    FileETagCache(tmp_path, ME).drop("k")
    assert not wait_state_file_path(tmp_path, ME).exists()


def test_the_file_cache_rewrites_an_unchanged_record_only_once(tmp_path, monkeypatch):
    import angareion.state as state_module

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def counting_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(state_module.os, "replace", counting_replace)
    cache = FileETagCache(tmp_path, ME)
    record = ETagRecord(url="https://example.invalid/x", cursor=CURSOR, etag='W/"v1"')
    cache.put("k", record)
    cache.put("k", record)
    assert len(calls) == 1
