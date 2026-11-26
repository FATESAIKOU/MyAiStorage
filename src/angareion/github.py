"""GitHub Issues backend: channel = a configured issue, message = a comment (D2/v5).

The channel/backend config files decide which repo and which issue a channel
name points at (`Entity`); nothing here scans titles. The token comes from the
personal config and only ever needs `Issues: Read and write` on the backend
repos (and MyAiStorage, for the registry comments).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import email.utils
import json
import re
from typing import Any
import urllib.parse

from angareion.backend import (
    CURSOR_SEEN_MARGIN_SECONDS,
    PAGE_COMMENT_LIMIT,
    PAGE_ROLLOVER_AT,
    AuthenticationError,
    BackendError,
    BackendRef,
    ChannelFullError,
    Cursor,
    ETagCache,
    ETagRecord,
    Entity,
    EntityResolver,
    MemoryETagCache,
    PageUnavailableError,
    RateLimitError,
    ReadResult,
    WriteRefusedError,
    comment_is_new,
    cursor_after_read,
    cursor_since,
    format_backend_time,
    http_cause,
    new_reader_counts,
    page_base,
    pages_to_read,
    parse_backend_time,
    scope_matches,
    skipped_page_warning,
)
from angareion.config import Config, load_token, mask_token
from angareion.messages import Message, parse_message, serialize_message
from angareion.transport import DefaultHttpTransport, HttpResponse, HttpTransport

USER_AGENT = "Angareion/0.2.0"
HTTP_TIMEOUT_SECONDS = 30.0
LISTING_PAGE_SIZE = 100
# A message that goes into an error is cut to this many characters (design 542 §4).
ERROR_MESSAGE_MAX_CHARS = 200
SECONDARY_LIMIT_TEXT = "secondary rate limit"

__all__ = [
    "DefaultHttpTransport",
    "GitHubIssuesBackend",
    "HttpResponse",
    "HttpTransport",
    "USER_AGENT",
]


_REPO_IN_URL = re.compile(r"repos/([^/?]+/[^/?]+)/")
_HASH_NUMBER = re.compile(r"#\s?\d+")
_ISSUE_WORD = re.compile(r"\b(?:issues?[ /]|GH-)\d+", re.IGNORECASE)


def _repo_of(url: str) -> str | None:
    """`owner/repo` out of an API URL, so an error can name the backend instead of the repo."""
    match = _REPO_IN_URL.search(url)
    return match.group(1) if match else None


def _compose(head: str, detail: str, tail: str = "not retrying") -> str:
    """`<head>: <detail>; not retrying`, leaving out whichever of the detail and the tail is empty."""
    text = f"{head}: {detail}" if detail else head
    return f"{text}; {tail}" if tail else text


def _epoch_to_backend_time(raw: str | None) -> str | None:
    """An `x-ratelimit-reset` (epoch seconds) as the backend's own UTC time, or None."""
    if raw is None:
        return None
    try:
        moment = datetime.fromtimestamp(int(raw.strip()), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return format_backend_time(moment)


def _whole_seconds(raw: str | None) -> int | None:
    """A `retry-after` in whole seconds, or None when it is missing or not a number."""
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


class GitHubIssuesBackend:
    """ChannelBackend over GitHub issues and comments, positioned by the config files.

    `create_channel` opens an issue in the chosen backend's repo (title =
    channel name); `close_channel` closes it and keeps it as a record; messages
    are comments. The channel/backend files are read through the injected
    resolver (`ChannelStore`), so a missing channel raises ChannelNotFoundError.
    """

    def __init__(
        self,
        store: EntityResolver,
        token: str | None = None,
        *,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.github.com",
        etag_cache: ETagCache | None = None,
    ):
        self.store = store
        self.token = token
        self.transport: HttpTransport = transport or DefaultHttpTransport()
        self.base_url = base_url.rstrip("/")
        # The last clean conditional-request state (#96). In memory by default;
        # the CLI injects a file-backed cache so a restarted `wait` keeps it.
        self._etags: ETagCache = etag_cache if etag_cache is not None else MemoryETagCache()
        # wait-process-only scan window (D2 rule 3): the newest backend time
        # already scanned, minus the margin. Never part of the cursor.
        self._scan_since: dict[str, str] = {}

    @classmethod
    def from_config(
        cls,
        config: Config,
        store: EntityResolver,
        *,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.github.com",
    ) -> GitHubIssuesBackend:
        token = load_token(config.token_file)
        return cls(store=store, token=token, transport=transport, base_url=base_url)

    def _mask(self, text: str) -> str:
        return mask_token(text, self.token or "")

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _scrub(self, text: str, url: str) -> str:
        """GitHub's own message, made safe for an error: one line, short, no token, no repo name, no issue number."""
        text = self._mask(" ".join(text.split()))
        repo = _repo_of(url)
        if repo:
            # GitHub may spell the repo in another case, or URL-encode the slash.
            spellings = sorted({repo, repo.replace("/", "%2F")}, key=len, reverse=True)
            text = re.sub("|".join(re.escape(s) for s in spellings), "<backend>", text, flags=re.IGNORECASE)
        text = _HASH_NUMBER.sub("#…", text)
        text = _ISSUE_WORD.sub("<issue>", text)
        if len(text) > ERROR_MESSAGE_MAX_CHARS:
            text = text[: ERROR_MESSAGE_MAX_CHARS - 1] + "…"
        return text

    @staticmethod
    def _raw_message(resp: HttpResponse) -> str:
        """The `message` field of a JSON error body, unprocessed; empty when there is none."""
        try:
            body = resp.json()
        except ValueError:
            return ""
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"]
        return ""

    def _rate_limit(self, resp: HttpResponse, url: str) -> RateLimitError | None:
        """A 403/429 that is a rate limit, or None. Headers decide first (primary, then secondary); the text is the fallback."""
        if resp.status not in (403, 429):
            return None
        raw = self._raw_message(resp)
        detail = self._scrub(raw, url)
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.strip() == "0":
            reset_at = _epoch_to_backend_time(resp.headers.get("x-ratelimit-reset"))
            when = f"resets at {reset_at}" if reset_at else "the reset time is unknown"
            head = f"GitHub API rate limit reached (HTTP {resp.status}, primary; {when})"
            return RateLimitError(_compose(head, detail), kind="primary", reset_at=reset_at)
        retry_raw = resp.headers.get("retry-after")
        if retry_raw is not None or SECONDARY_LIMIT_TEXT in raw.lower():
            retry_after = _whole_seconds(retry_raw)
            when = f"retry after {retry_after}s" if retry_after is not None else "no retry time given"
            head = f"GitHub API secondary rate limit (HTTP {resp.status}; {when})"
            return RateLimitError(_compose(head, detail), kind="secondary", retry_after=retry_after)
        return None

    def _refusal_message(self, resp: HttpResponse, url: str) -> str:
        """The message for a 403 that is not a rate limit (shared by `_request` and the listings)."""
        head = f"GitHub API refused the request (HTTP {resp.status})"
        return _compose(head, self._scrub(self._raw_message(resp), url))

    def _listing_failure(self, resp: HttpResponse, url: str, text: str) -> BackendError:
        """The error for a listing answer >= 400 (design 542 §6.3, 2c).

        404, 410 and a refused 403 are PageUnavailableError (an older page may be
        skipped); the others are plain BackendErrors. The texts are the ones these
        listings always had.
        """
        if resp.status == 403:
            return PageUnavailableError(self._refusal_message(resp, url), status=403)
        if resp.status in (404, 410):
            return PageUnavailableError(f"{text}: HTTP {resp.status}", status=resp.status)
        return BackendError(f"{text}: HTTP {resp.status}")

    def _request(
        self,
        method: str,
        path_or_url: str,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        allow_refusal: bool = False,
    ) -> HttpResponse:
        """One request. A 403/429 that is a rate limit raises RateLimitError; another 403 raises
        BackendError, unless `allow_refusal` hands it back (POST comments decide that case, design 542 §4)."""
        url = self._full_url(path_or_url)

        body_bytes = None
        if data is not None:
            body_bytes = json.dumps(data).encode("utf-8")
        req_headers = dict(headers or {})
        if data is not None and "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json"

        try:
            resp = self.transport.request(method, url, headers=req_headers, body=body_bytes, timeout=timeout)
        except Exception as e:
            raise BackendError(f"HTTP request failed: {self._mask(str(e))}") from e

        if resp.status == 401:
            raise AuthenticationError("Invalid or missing GitHub token (HTTP 401)")
        if resp.status in (403, 429):
            limited = self._rate_limit(resp, url)
            if limited is not None:
                raise limited
            if not (resp.status == 403 and allow_refusal):
                raise BackendError(self._refusal_message(resp, url))
        if resp.status in (301, 302, 303, 307, 308):
            raise BackendError(
                f"GitHub API redirected the request (HTTP {resp.status}); not following"
            )
        return resp

    def _next_page_url(self, resp: HttpResponse) -> str | None:
        """The Link header's next-page URL, which must stay on our API host (L3)."""
        link = resp.headers.get("link")
        url = None
        if link:
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        if url is not None and not url.startswith(self.base_url + "/"):
            raise BackendError(
                f"Refusing a pagination link outside {self.base_url} (different host)"
            )
        return url

    def _json(self, resp: HttpResponse, context: str) -> Any:
        """Parse a response body; a non-JSON body (a proxy login page, say) is a backend error (L1)."""
        try:
            return resp.json()
        except ValueError as e:
            raise BackendError(
                f"GitHub returned a non-JSON response while {context} (HTTP {resp.status})"
            ) from e

    # -- entities --------------------------------------------------------

    def create_channel(self, name: str, backend: BackendRef) -> int:
        """Open a new issue in the backend repo; return its number (no members in the body)."""
        if not name or not isinstance(name, str):
            raise ValueError("Channel name must be a non-empty string")
        return self._open_issue(
            backend.repo,
            name,
            f"Angareion channel '{name}'. Messages are comments on this issue.\n",
            what=f"Failed to create channel '{name}'",
            context=f"creating channel '{name}'",
        )

    def open_page(self, entity: Entity) -> int:
        """Open the next page of a channel: a new issue whose body names the page it continues from (design 542 S-4)."""
        return self._open_issue(
            entity.repo,
            entity.channel,
            f"Angareion channel '{entity.channel}', continued from #{entity.issue}. Messages are comments on this issue.\n",
            what=f"Failed to open a new page for channel '{entity.channel}'",
            context=f"opening a new page for channel '{entity.channel}'",
        )

    def _open_issue(self, repo: str, title: str, body: str, *, what: str, context: str) -> int:
        """POST an issue to `repo` and return its number; `what` heads the error messages, `context` the JSON one."""
        resp = self._request(
            "POST",
            f"repos/{repo}/issues",
            headers=self._headers(),
            data={"title": title, "body": body},
        )
        if resp.status not in (200, 201):
            raise BackendError(f"{what}: HTTP {resp.status}")
        data = self._json(resp, context)
        try:
            return int(data["number"])
        except (KeyError, TypeError, ValueError) as e:
            raise BackendError(f"{what}: no issue number in the response") from e

    def verify_channel(self, name: str, backend: BackendRef, entity: int) -> bool:
        """Whether the entity exists and its title is exactly the channel name (review H2)."""
        resp = self._request("GET", f"repos/{backend.repo}/issues/{entity}", headers=self._headers())
        if resp.status == 404:
            return False
        if resp.status >= 400:
            raise BackendError(f"Failed to verify channel '{name}': HTTP {resp.status}")
        data = self._json(resp, f"verifying channel '{name}'")
        return data.get("title") == name

    def close_channel(self, entity: Entity) -> None:
        """Close the issue without deleting it; a 404 means it is already gone (and closed)."""
        resp = self._request(
            "PATCH",
            f"repos/{entity.repo}/issues/{entity.issue}",
            headers=self._headers(),
            data={"state": "closed"},
        )
        if resp.status == 404:
            return  # already deleted: treated as already closed
        if resp.status not in (200, 201):
            raise BackendError(f"Failed to close channel '{entity.channel}': HTTP {resp.status}")

    # -- messages --------------------------------------------------------

    def send(self, entity: Entity, message: Message) -> str:
        """Post a message as a comment on the channel's entity; return its id."""
        message.validate()
        if message.channel != entity.channel:
            raise ValueError(
                f"Message channel '{message.channel}' does not match target channel '{entity.channel}'"
            )

        url = f"repos/{entity.repo}/issues/{entity.issue}/comments"
        resp = self._request(
            "POST",
            url,
            headers=self._headers(),
            data={"body": serialize_message(message)},
            allow_refusal=True,
        )
        if resp.status in (200, 201):
            return message.id
        if resp.status in (403, 422):
            raise self._refused_send(entity, resp, url)
        raise BackendError(f"Failed to send to channel '{entity.channel}': HTTP {resp.status}")

    def _refused_send(self, entity: Entity, resp: HttpResponse, url: str) -> BackendError:
        """A refused write that is not a rate limit: the page may simply be full (design 542 §4, rule 3).

        Look at the page's count with one GET. At or above the rollover band the
        error is ChannelFullError (presumed when below the hard limit); anything
        else, or a failed GET, is a WriteRefusedError: the write is certainly not stored.
        """
        detail = self._scrub(self._raw_message(resp), url)
        try:
            count: int | None = self.page_size(entity)
        except AuthenticationError:
            raise
        except BackendError:
            count = None
        if count is not None and count >= PAGE_ROLLOVER_AT:
            return ChannelFullError(
                entity.channel,
                count,
                presumed=count < PAGE_COMMENT_LIMIT,
                status=resp.status,
                detail=detail,
            )
        return WriteRefusedError(
            f"Failed to send to channel '{entity.channel}'{http_cause(resp.status, detail)}",
            status=resp.status,
        )

    # -- page size (read-only; design 542 C-3) ----------------------------

    def page_size(self, entity: Entity) -> int:
        return self.page_size_at(entity)[0]

    def page_size_at(self, entity: Entity) -> tuple[int, str | None]:
        """The comment count of the entity's issue, with the response's own `Date` (or None)."""
        resp = self._request("GET", f"repos/{entity.repo}/issues/{entity.issue}", headers=self._headers())
        if resp.status >= 400:
            raise BackendError(f"Failed to read the size of channel '{entity.channel}': HTTP {resp.status}")
        data = self._json(resp, f"reading the size of channel '{entity.channel}'")
        count = data.get("comments") if isinstance(data, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BackendError(
                f"Failed to read the size of channel '{entity.channel}': no comment count in the response"
            )
        return count, self._backend_now(resp)

    def _entity_key(self, entity: Entity) -> str:
        return f"{entity.repo}#{entity.issue}"

    def _etag_key(self, entity: Entity, recipient: str | None, mentioned: str | None = None) -> str:
        """One entity and one reader mode: the channel, "to me", or "@ me".

        The modes read the same listing, but "new" means different things
        (the channel: every message; to me: the ones addressed to me, mentioning
        me, or a broadcast; @ me: addressed to me or mentioning me), so a clean
        scan in one mode must never answer for another.
        """
        if mentioned is not None:
            return f"{self._entity_key(entity)}|@{mentioned}"
        return f"{self._entity_key(entity)}|{recipient or '*'}"

    @staticmethod
    def _same_cursor(left: Cursor, right: Cursor) -> bool:
        """Whether two cursors are the same starting point (`seen` is a set)."""
        return (
            left.position == right.position
            and left.at == right.at
            and set(left.seen) == set(right.seen)
        )

    def _full_url(self, path_or_url: str) -> str:
        """The exact URL a request goes to; ETag records are keyed on this string."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _listing_url(self, entity: Entity, *, since: str | None = None) -> str:
        url = f"repos/{entity.repo}/issues/{entity.issue}/comments?per_page={LISTING_PAGE_SIZE}"
        if since:
            url += f"&since={urllib.parse.quote(since, safe='')}"
        return url

    @staticmethod
    def _backend_now(resp: HttpResponse) -> str | None:
        """The backend's own time for a response (#224): its `Date` header.

        A new identity's first "to me" read uses this as the floor for old
        broadcasts, so the moment must come from the backend, not from this
        machine's clock (D4: only the backend decides what is old). The header
        is RFC 7231 (`Tue, 27 Oct 2026 05:04:03 GMT`); it is normalised to the
        same RFC 3339 UTC form the backend uses for `created_at`. A missing or
        unparsable header yields None, and the read then counts every broadcast
        (the behaviour before #224) instead of guessing a local time.
        """
        raw = resp.headers.get("date")
        if not raw:
            return None
        try:
            moment = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return format_backend_time(moment)

    def _comment_meta(self, comment: dict[str, Any]) -> tuple[int, str] | None:
        """(comment id, created_at) of a comment, or None when unusable."""
        created_at = str(comment.get("created_at") or "")
        try:
            comment_id = int(comment["id"])
        except (KeyError, TypeError, ValueError):
            return None
        if not created_at:
            return None
        try:
            parse_backend_time(created_at)
        except ValueError:
            return None
        return comment_id, created_at

    def check(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        mentioned: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Whether there is anything new on any page, without trusting a multi-page ETag (D2/M1/#96, C-11).

        Each page is checked from its own start (`page_base`). An older page that
        cannot be listed is skipped (C-22); the current page's errors propagate.
        """
        for page in pages_to_read(entity, cursor):
            base = page_base(cursor, page.issue)
            unread = base.position is None and base.at is None
            try:
                if self._check_page(page, base, recipient=recipient, mentioned=mentioned, timeout=timeout, unread=unread):
                    return True
            except PageUnavailableError:
                if page.issue == entity.issue:
                    raise
        return False

    def _check_page(
        self,
        page: Entity,
        base: Cursor,
        *,
        recipient: str | None,
        mentioned: str | None,
        timeout: float | None,
        unread: bool,
    ) -> bool:
        """One page of `check`: a conditional request while the page's ETag record matches its start.

        A page read from the start (`unread`: its start is empty) keeps its ETag and
        scan window under their own key (`|unread`): its window moves on every
        comment of the page, not only the ones in scope (C-11, design 542 §2.2).
        An ETag is stored only for a single, not-full page with nothing new; a found
        message, a next page, a full first page, or any unusable state means a full query.
        """
        key = self._etag_key(page, recipient, mentioned)
        if unread:
            key += "|unread"
        cursor_since_value = cursor_since(base)
        cursor_url = self._full_url(self._listing_url(page, since=cursor_since_value))
        record = self._etags.get(key)
        same_cursor = record is not None and self._same_cursor(record.cursor, base)

        if same_cursor and record is not None and record.url == cursor_url:
            # #96 M1: prefer the cursor's own window over a scan window that
            # moved forward. The recorded response was fully scanned clean for
            # this cursor; querying it conditionally keeps restarts cheap and
            # never persists a narrowed scan window (which could hide a
            # replication-lagged comment).
            url = cursor_url
        else:
            since = cursor_since_value
            scan_since = self._scan_since.get(key)
            if scan_since is not None and (since is None or scan_since > since):
                since = scan_since
            url = self._full_url(self._listing_url(page, since=since))

        # A record is reusable for the same cursor and recipient mode even when
        # the query window moved: a 304 means the current representation is the
        # one that was fully scanned clean; a different representation is a 200
        # and a full query, so the wider cursor window still covers everything.
        etag = record.etag if same_cursor else None

        resp = self._request("GET", url, headers=self._headers(etag=etag), timeout=timeout, allow_refusal=True)
        if resp.status == 304:
            return False
        if resp.status >= 400:
            raise self._listing_failure(resp, url, "Failed to check for new messages")

        next_url = self._next_page_url(resp)
        items = self._json(resp, "checking for new messages")
        page_full = isinstance(items, list) and len(items) >= LISTING_PAGE_SIZE
        first_etag = resp.headers.get("etag") or None
        # M1/#96: the ETag speaks for page 1 alone. Never keep one when the
        # listing spans pages, or when page 1 is full: one more comment then
        # lands on a new page 2 while page 1 stays byte-identical, and its 304
        # would hide that comment forever.
        storable = next_url is None and not page_full and first_etag is not None

        found = False
        newest_any: str | None = None
        while True:
            page_newest: str | None = None
            for comment in items:
                parsed = parse_message(comment.get("body", ""))
                if parsed is None:
                    continue
                meta = self._comment_meta(comment)
                if meta is None:
                    continue
                comment_id, created_at = meta
                if newest_any is None or parse_backend_time(created_at) > parse_backend_time(newest_any):
                    newest_any = created_at
                if not scope_matches(parsed, recipient, mentioned):
                    continue
                if page_newest is None or parse_backend_time(created_at) > parse_backend_time(page_newest):
                    page_newest = created_at
                if comment_is_new(comment_id, created_at, base):
                    found = True

            if not unread and page_newest is not None:
                self._scan_since[key] = format_backend_time(
                    parse_backend_time(page_newest) - timedelta(seconds=CURSOR_SEEN_MARGIN_SECONDS)
                )
            if found:
                # A response that contained a new message must never be the
                # validator for the next "nothing new" answer (#96).
                self._etags.drop(key)
                return True
            if next_url is None:
                break
            resp = self._request("GET", next_url, headers=self._headers(), timeout=timeout, allow_refusal=True)
            if resp.status >= 400:
                raise self._listing_failure(resp, next_url, "Failed to check for new messages (page fetch failed)")
            next_url = self._next_page_url(resp)
            items = self._json(resp, "checking for new messages")

        if unread and newest_any is not None:
            self._scan_since[key] = format_backend_time(
                parse_backend_time(newest_any) - timedelta(seconds=CURSOR_SEEN_MARGIN_SECONDS)
            )
        if storable:
            self._etags.put(key, ETagRecord(url=url, cursor=base, etag=first_etag))
        else:
            self._etags.drop(key)
        return False

    def _listing(self, page: Entity, since: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Every comment of one page since `since` (all its listing pages), and the first response's time (#224)."""
        url = self._listing_url(page, since=since)
        resp = self._request("GET", url, headers=self._headers(), allow_refusal=True)
        if resp.status >= 400:
            raise self._listing_failure(resp, self._full_url(url), "Failed to read messages")
        first_date = self._backend_now(resp)
        comments: list[dict[str, Any]] = []
        while True:
            comments.extend(self._json(resp, "reading messages"))
            next_url = self._next_page_url(resp)
            if not next_url:
                break
            resp = self._request("GET", next_url, headers=self._headers(), allow_refusal=True)
            if resp.status >= 400:
                raise self._listing_failure(resp, next_url, "Failed to read messages (page fetch failed)")
        return comments, first_date

    def read(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        mentioned: str | None = None,
        new_recipient: bool = False,
    ) -> ReadResult:
        """Read the messages after the cursor, oldest first, across every page of the channel.

        Each page is read from its own start (`page_base`, design 542 §2.1). The
        messages of all pages come back in comment-id order. `new_recipient` is
        the reader's first "to me" read for this entity (#224): messages addressed
        to it read from the start, a plain broadcast only from the backend's
        current time (the first listing response's `Date`, never the local clock).
        An older page that cannot be listed is skipped with a warning (C-22).
        """
        pages = pages_to_read(entity, cursor)
        wants_floor = new_recipient and recipient is not None and mentioned is None
        floor: str | None = None
        floor_taken = False
        warnings: list[str] = []
        entries_by_page: dict[int, list[tuple[int, str]]] = {}
        matched: list[tuple[int, Message]] = []

        for page in pages:
            base = page_base(cursor, page.issue)
            try:
                comments, first_date = self._listing(page, cursor_since(base))
            except PageUnavailableError as error:
                if page.issue == entity.issue:
                    raise
                warnings.append(skipped_page_warning(entity.channel, error.status))
                continue
            if not floor_taken:
                floor = first_date if wants_floor else None
                floor_taken = True

            entries: list[tuple[int, str]] = []
            for comment in comments:
                parsed = parse_message(comment.get("body", ""))
                if parsed is None:
                    continue
                meta = self._comment_meta(comment)
                if meta is None:
                    continue
                comment_id, created_at = meta
                # The cursor tracks every message on the page (the backend's
                # arrival positions); `recipient`/`mentioned` only decide what is returned.
                entries.append((comment_id, created_at))
                if not scope_matches(parsed, recipient, mentioned):
                    continue
                if not new_reader_counts(parsed, created_at, floor, recipient):
                    continue
                if not comment_is_new(comment_id, created_at, base):
                    continue

                setattr(parsed, "received_at", created_at)
                comment_url = comment.get("html_url", "")
                if comment_url:
                    setattr(parsed, "comment_url", comment_url)

                matched.append((comment_id, parsed))
            entries_by_page[page.issue] = entries

        matched.sort(key=lambda pair: pair[0])
        messages = [message for _, message in matched]  # oldest arrival first

        if cursor.position is None and cursor.at is not None:
            return ReadResult(messages, cursor, tuple(warnings))  # `--since`: the local cursor must not move
        new_cursor = cursor_after_read(cursor, [page.issue for page in pages], entries_by_page)
        return ReadResult(messages, new_cursor, tuple(warnings))

    # -- enumeration (read-only listing for `log`) -----------------------
    def enumerate_conditional(self, entity, *, since=None, etag=None):
        url = self._listing_url(entity, since=since)
        full_url = self._full_url(url)
        # conditional
        etag_str = None
        if isinstance(etag, dict):
            if etag.get("url") == full_url:
                etag_str = etag.get("etag")
        headers = self._headers(etag=etag_str)
        resp = self._request("GET", url, headers=headers, allow_refusal=True)
        if resp.status == 304:
            return [], etag, True
        if resp.status >= 400:
            raise self._listing_failure(resp, full_url, "Failed to enumerate messages")
        result = []
        first_etag = resp.headers.get("etag") or None
        first_page_items = None
        page_resp = resp
        while True:
            items = self._json(page_resp, "enumerating messages")
            if first_page_items is None:
                first_page_items = items
            for comment in items:
                meta = self._comment_meta(comment)
                if meta is None:
                    continue
                comment_id, created_at = meta
                parsed = parse_message(comment.get("body", ""))
                if parsed is not None:
                    setattr(parsed, "received_at", created_at)
                    curl = comment.get("html_url", "")
                    if curl:
                        setattr(parsed, "comment_url", curl)
                result.append((comment_id, created_at, parsed))
            next_url = self._next_page_url(page_resp)
            if not next_url:
                break
            page_resp = self._request("GET", next_url, headers=self._headers(), allow_refusal=True)
            if page_resp.status >= 400:
                raise self._listing_failure(page_resp, next_url, "Failed to enumerate messages")
        result.sort(key=lambda x: x[0])
        # storable etag: single page, not full, has etag
        page_full = isinstance(first_page_items, list) and len(first_page_items) >= LISTING_PAGE_SIZE
        next_url0 = self._next_page_url(resp)
        storable = next_url0 is None and not page_full and first_etag is not None
        new_etag = {"url": full_url, "etag": first_etag} if storable else None
        return result, new_etag, False
