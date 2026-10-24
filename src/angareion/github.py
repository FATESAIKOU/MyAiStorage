"""GitHub Issues backend: channel = a configured issue, message = a comment (D2/v5).

The channel/backend config files decide which repo and which issue a channel
name points at (`Entity`); nothing here scans titles. The token comes from the
personal config and only ever needs `Issues: Read and write` on the backend
repos (and MyAiStorage, for the registry comments).
"""

from __future__ import annotations

from datetime import timedelta
import json
import re
from typing import Any
import urllib.parse

from angareion.backend import (
    CURSOR_SEEN_MARGIN_SECONDS,
    AuthenticationError,
    BackendError,
    BackendRef,
    Cursor,
    ETagCache,
    ETagRecord,
    Entity,
    EntityResolver,
    MemoryETagCache,
    ReadResult,
    advance_cursor,
    comment_is_new,
    cursor_since,
    format_backend_time,
    parse_backend_time,
    scope_matches,
)
from angareion.config import Config, load_token, mask_token
from angareion.messages import Message, parse_message, serialize_message
from angareion.transport import DefaultHttpTransport, HttpResponse, HttpTransport

USER_AGENT = "Angareion/0.2.0"
HTTP_TIMEOUT_SECONDS = 30.0
LISTING_PAGE_SIZE = 100

__all__ = [
    "DefaultHttpTransport",
    "GitHubIssuesBackend",
    "HttpResponse",
    "HttpTransport",
    "USER_AGENT",
]


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

    def _request(
        self,
        method: str,
        path_or_url: str,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> HttpResponse:
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
        if resp.status == 403:
            raise BackendError(
                "GitHub API refused the request (HTTP 403; secondary rate limit?); not retrying"
            )
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
        resp = self._request(
            "POST",
            f"repos/{backend.repo}/issues",
            headers=self._headers(),
            data={"title": name, "body": f"Angareion channel '{name}'. Messages are comments on this issue.\n"},
        )
        if resp.status not in (200, 201):
            raise BackendError(f"Failed to create channel '{name}': HTTP {resp.status}")
        data = self._json(resp, f"creating channel '{name}'")
        try:
            return int(data["number"])
        except (KeyError, TypeError, ValueError) as e:
            raise BackendError(f"Failed to create channel '{name}': no issue number in the response") from e

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

        resp = self._request(
            "POST",
            f"repos/{entity.repo}/issues/{entity.issue}/comments",
            headers=self._headers(),
            data={"body": serialize_message(message)},
        )
        if resp.status not in (200, 201):
            raise BackendError(f"Failed to send to channel '{entity.channel}': HTTP {resp.status}")
        return message.id

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
        """Whether there is anything new, without trusting a multi-page ETag (D2/M1/#96).

        A conditional request is sent when a record for the same cursor and
        recipient mode exists, even if the in-process scan window moved: a 304
        answers for the representation that was fully scanned and found clean,
        and a different representation is a 200 and a full query. While a
        cursor-window record exists its URL is preferred over `scan_since`, so
        a narrowed scan window is never the cross-restart query. The ETag is
        stored only for a single, not-full page with nothing new; a found
        message, a next page, a full first page, or any unusable state means a
        full query.
        """
        key = self._etag_key(entity, recipient, mentioned)
        cursor_since_value = cursor_since(cursor)
        cursor_url = self._full_url(self._listing_url(entity, since=cursor_since_value))
        record = self._etags.get(key)
        same_cursor = record is not None and self._same_cursor(record.cursor, cursor)

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
            url = self._full_url(self._listing_url(entity, since=since))

        # A record is reusable for the same cursor and recipient mode even when
        # the query window moved: a 304 means the current representation is the
        # one that was fully scanned clean; a different representation is a 200
        # and a full query, so the wider cursor window still covers everything.
        etag = record.etag if same_cursor else None

        resp = self._request("GET", url, headers=self._headers(etag=etag), timeout=timeout)
        if resp.status == 304:
            return False
        if resp.status >= 400:
            raise BackendError(f"Failed to check for new messages: HTTP {resp.status}")

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
        while True:
            page_newest: str | None = None
            for comment in items:
                parsed = parse_message(comment.get("body", ""))
                if parsed is None or not scope_matches(parsed, recipient, mentioned):
                    continue
                meta = self._comment_meta(comment)
                if meta is None:
                    continue
                comment_id, created_at = meta
                if page_newest is None or parse_backend_time(created_at) > parse_backend_time(page_newest):
                    page_newest = created_at
                if comment_is_new(comment_id, created_at, cursor):
                    found = True

            if page_newest is not None:
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
            resp = self._request("GET", next_url, headers=self._headers(), timeout=timeout)
            if resp.status >= 400:
                raise BackendError(f"Failed to check for new messages (page fetch failed): HTTP {resp.status}")
            next_url = self._next_page_url(resp)
            items = self._json(resp, "checking for new messages")

        if storable:
            self._etags.put(key, ETagRecord(url=url, cursor=cursor, etag=first_etag))
        else:
            self._etags.drop(key)
        return False

    def read(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        mentioned: str | None = None,
    ) -> ReadResult:
        """Read the messages after the cursor, oldest first, pages included.

        "New" is decided by the backend's arrival position and time (D4); a
        `--since` cursor (time only) returns the comments at or after that time
        and moves nothing.
        """
        since = cursor_since(cursor)
        url: str | None = self._listing_url(entity, since=since)
        resp = self._request("GET", url, headers=self._headers())
        if resp.status >= 400:
            raise BackendError(f"Failed to read messages: HTTP {resp.status}")

        entries: list[tuple[int, str]] = []
        matched: list[tuple[int, Message]] = []

        while url:
            if resp.status >= 400:
                raise BackendError(f"Failed to read messages (page fetch failed): HTTP {resp.status}")

            for comment in self._json(resp, "reading messages"):
                parsed = parse_message(comment.get("body", ""))
                if parsed is None:
                    continue
                meta = self._comment_meta(comment)
                if meta is None:
                    continue
                comment_id, created_at = meta
                # The cursor tracks every message on the entity (the backend's
                # arrival positions); `recipient`/`mentioned` only decide what is returned.
                entries.append((comment_id, created_at))
                if not scope_matches(parsed, recipient, mentioned):
                    continue
                if not comment_is_new(comment_id, created_at, cursor):
                    continue

                setattr(parsed, "received_at", created_at)
                comment_url = comment.get("html_url", "")
                if comment_url:
                    setattr(parsed, "comment_url", comment_url)

                matched.append((comment_id, parsed))

            url = self._next_page_url(resp)
            if url:
                resp = self._request("GET", url, headers=self._headers())

        matched.sort(key=lambda pair: pair[0])
        messages = [message for _, message in matched]  # oldest arrival first

        if cursor.position is None and cursor.at is not None:
            return ReadResult(messages, cursor)  # `--since`: the local cursor must not move
        return ReadResult(messages, advance_cursor(cursor, entries))
