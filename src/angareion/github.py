"""GitHub Issues backend: channel = issue, message = comment (D2)."""

from __future__ import annotations

from datetime import timedelta
import json
import re
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

import yaml

from angareion.backend import (
    CURSOR_SEEN_MARGIN_SECONDS,
    AuthenticationError,
    BackendError,
    Channel,
    ChannelNotFoundError,
    Cursor,
    ReadResult,
    Target,
    address_matches_identity,
    advance_cursor,
    comment_is_new,
    cursor_since,
    format_backend_time,
    parse_backend_time,
)
from angareion.config import Config, load_token, mask_token
from angareion.messages import (
    ANGAREION_VERSION,
    Message,
    parse_message,
    serialize_message,
    split_front_matter,
)

USER_AGENT = "Angareion/0.1.0"
HTTP_TIMEOUT_SECONDS = 30.0
ISSUES_PAGE = "repos/{owner}/{repo}/issues?state=all&sort=created&direction=asc&per_page=100"


class HttpResponse:
    """HTTP response wrapper."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpTransport(Protocol):
    """Transport protocol allowing injecting fake HTTP responses for testing."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: Authorization must not travel to another host (L3)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DefaultHttpTransport:
    """Standard library urllib HTTP transport with a fixed socket timeout (M2).

    A half-open connection (NAT, after sleep) must not hang `wait` past its
    `--timeout`; the request times out and the caller sees a BackendError.
    Redirects are not followed (L3): the 3xx response is surfaced and the
    backend turns it into an error.
    """

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        effective_timeout = self.timeout if timeout is None else timeout
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with self._opener.open(req, timeout=effective_timeout) as resp:
                headers_dict = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                return HttpResponse(resp.status, headers_dict, resp.read())
        except urllib.error.HTTPError as e:
            headers_dict = {str(k).lower(): str(v) for k, v in e.headers.items()}
            return HttpResponse(e.code, headers_dict, e.read())
        except TimeoutError as e:
            raise BackendError(f"HTTP request timed out after {effective_timeout:.0f}s") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise BackendError(f"HTTP request timed out after {effective_timeout:.0f}s") from e
            raise BackendError(f"Network error connecting to GitHub: {e.reason}") from e


def _extract_link(link_header: str | None, rel: str = "next") -> str | None:
    """Extract the page URL with the given rel from a Link header."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="' + re.escape(rel) + r'"', link_header)
    return match.group(1) if match else None


def serialize_channel_body(name: str, members: list[dict[str, Any]]) -> str:
    """The issue body of a channel: front matter with its members."""
    fm: dict[str, Any] = {
        "angareion": ANGAREION_VERSION,
        "channel": name,
        "members": list(members),
    }
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    return (
        f"---\n{fm_yaml}---\n"
        f"Angareion channel '{name}'. Messages are comments on this issue.\n"
    )


def parse_channel_members(body: str) -> list[dict[str, Any]]:
    """The members recorded in a channel issue body; [] when absent or unreadable."""
    try:
        raw_yaml, _ = split_front_matter(body)
        data = yaml.safe_load(raw_yaml)
    except Exception:
        return []
    if not isinstance(data, dict) or data.get("angareion") != ANGAREION_VERSION:
        return []
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return [member for member in members if isinstance(member, dict)]


class GitHubIssuesBackend:
    """ChannelBackend over a private GitHub repository's issues and comments.

    The channel repo holds one issue per channel (the title is the channel
    name) and one comment per message. "To me" reads the repo-level comments
    listing, which covers every channel at once.
    """

    def __init__(
        self,
        channel_repo: str,
        token: str | None = None,
        *,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.github.com",
    ):
        if "/" not in channel_repo:
            raise ValueError(f"channel_repo must be in 'owner/repo' format, got '{channel_repo}'")
        self.channel_repo = channel_repo
        self.owner, self.repo = channel_repo.split("/", 1)
        self.token = token
        self.transport: HttpTransport = transport or DefaultHttpTransport()
        self.base_url = base_url.rstrip("/")
        self._issues_cache: dict[str, dict[str, Any]] = {}
        self._channel_numbers: dict[str, int] = {}
        self._check_etags: dict[str, str | None] = {}
        # wait-process-only scan window (D2 rule 3): the newest backend time
        # already scanned, minus the margin. Never part of the cursor.
        self._scan_since: dict[str, str] = {}

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.github.com",
    ) -> GitHubIssuesBackend:
        token = load_token(config.token_file)
        return cls(
            channel_repo=config.channel_repo,
            token=token,
            transport=transport,
            base_url=base_url,
        )

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
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        body_bytes = json.dumps(data).encode("utf-8") if data is not None else None
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
        url = _extract_link(resp.headers.get("link"))
        if url is not None and not url.startswith(self.base_url + "/"):
            raise BackendError(
                f"Refusing a pagination link outside {self.base_url} (different host)"
            )
        return url

    def find_channel(self, name: str) -> int | None:
        """The issue number for a channel name, or None if it does not exist."""
        issue = self._find_issue(name)
        return issue["number"] if issue is not None else None

    def _json(self, resp: HttpResponse, context: str) -> Any:
        """Parse a response body; a non-JSON body (a proxy login page, say) is a backend error (L1)."""
        try:
            return resp.json()
        except ValueError as e:
            raise BackendError(
                f"GitHub returned a non-JSON response while {context} (HTTP {resp.status})"
            ) from e

    def _find_issue(self, name: str, *, timeout: float | None = None) -> dict[str, Any] | None:
        """The oldest issue with this channel name, or None (D2).

        Issues are listed oldest first (`sort=created&direction=asc`); the
        first title match is therefore the oldest and wins, and the scan stops
        there. Pull requests and entries without a title/number are skipped.
        """
        if name in self._issues_cache:
            return self._issues_cache[name]

        url: str | None = ISSUES_PAGE.format(owner=self.owner, repo=self.repo)
        while url:
            resp = self._request("GET", url, headers=self._headers(), timeout=timeout)
            if resp.status >= 400:
                raise BackendError(f"Failed to list issues: HTTP {resp.status}")

            for issue in self._json(resp, "listing issues"):
                if "pull_request" in issue:
                    continue
                title = issue.get("title")
                number = issue.get("number")
                if not title or not number:
                    continue
                self._issues_cache.setdefault(title, issue)
                self._channel_numbers.setdefault(title, number)
                if title == name:
                    return issue

            url = self._next_page_url(resp)

        # A comment may have taught us the number before the listing catches up.
        number = self._channel_numbers.get(name)
        if number is not None:
            return {"title": name, "number": number, "body": ""}
        return None

    def _channel_from_issue(self, name: str, issue: dict[str, Any]) -> Channel:
        return Channel(
            name=name,
            number=issue["number"],
            members=parse_channel_members(issue.get("body") or ""),
        )

    def create_channel(self, name: str, members: list[dict[str, Any]]) -> Channel:
        """Create a channel issue, or return the existing one (idempotent)."""
        if not name or not isinstance(name, str):
            raise ValueError("Channel name must be a non-empty string")

        existing = self._find_issue(name)
        if existing is not None:
            return self._channel_from_issue(name, existing)

        resp = self._request(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues",
            headers=self._headers(),
            data={"title": name, "body": serialize_channel_body(name, members)},
        )
        if resp.status not in (200, 201):
            raise BackendError(f"Failed to create channel '{name}': HTTP {resp.status}")
        data = self._json(resp, f"creating channel '{name}'")

        # L4 race: a concurrent process may have created the same channel first.
        # Re-scan; the oldest same-name issue (smallest number) wins for everyone,
        # whoever created it.
        fresh = self._find_issue(name)
        if fresh is not None:
            return self._channel_from_issue(name, fresh)

        # The listing can also lag right behind our own write; use the POST reply.
        channel = Channel(name=name, number=data["number"], members=list(members))
        self._issues_cache[name] = {"title": name, "number": channel.number, "body": data.get("body", "")}
        self._channel_numbers[name] = channel.number
        return channel

    def send(self, channel: str, message: Message) -> str:
        """Post a message as a comment on the channel issue; return its id."""
        issue = self._find_issue(channel)
        if issue is None:
            raise ChannelNotFoundError(f"Channel '{channel}' does not exist")

        message.validate()
        if message.channel != channel:
            raise ValueError(
                f"Message channel '{message.channel}' does not match target channel '{channel}'"
            )

        resp = self._request(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{issue['number']}/comments",
            headers=self._headers(),
            data={"body": serialize_message(message)},
        )
        if resp.status not in (200, 201):
            raise BackendError(f"Failed to send to channel '{channel}': HTTP {resp.status}")
        return message.id

    def _listing_url(self, target: Target, *, since: str | None = None, timeout: float | None = None) -> str:
        if target.channel is not None:
            issue = self._find_issue(target.channel, timeout=timeout)
            if issue is None:
                raise ChannelNotFoundError(f"Channel '{target.channel}' does not exist")
            url = f"repos/{self.owner}/{self.repo}/issues/{issue['number']}/comments?per_page=100"
        else:
            url = f"repos/{self.owner}/{self.repo}/issues/comments?per_page=100"
        if since:
            url += f"&since={urllib.parse.quote(since, safe='')}"
        return url

    def _is_in_scope(self, message: Message, target: Target) -> bool:
        """A comment on the channel issue, or one addressed to me (D4)."""
        if target.channel is not None:
            return True
        return address_matches_identity(message.to, target.recipient or {})

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

    def check(self, target: Target, cursor: Cursor, *, timeout: float | None = None) -> bool:
        """Whether there is anything new, without trusting a multi-page ETag (D2/M1).

        The ETag lives in this process's memory only, and is stored and used
        only when the response had no next page: it speaks for page 1 alone,
        so a new comment on page 2 would never move it and a 304 would hide
        it. Within one process, `scan_since` moves the query window forward
        past what was already scanned.
        """
        key = target.key()
        since = cursor_since(cursor)
        scan_since = self._scan_since.get(key)
        if scan_since is not None and (since is None or scan_since > since):
            since = scan_since

        url = self._listing_url(target, since=since, timeout=timeout)
        resp = self._request("GET", url, headers=self._headers(etag=self._check_etags.get(key)), timeout=timeout)
        if resp.status == 304:
            return False
        if resp.status >= 400:
            raise BackendError(f"Failed to check for new messages: HTTP {resp.status}")

        next_url = self._next_page_url(resp)
        if next_url is None:
            self._check_etags[key] = resp.headers.get("etag") or None
        else:
            # M1: the ETag only covers page 1; never trust it when the listing
            # spans pages, or a page-2 comment is invisible until page 1 moves.
            self._check_etags[key] = None

        found = False
        while True:
            page_newest: str | None = None
            for comment in self._json(resp, "checking for new messages"):
                parsed = parse_message(comment.get("body", ""))
                if parsed is None or not self._is_in_scope(parsed, target):
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
                return True
            if next_url is None:
                return False
            resp = self._request("GET", next_url, headers=self._headers(), timeout=timeout)
            if resp.status >= 400:
                raise BackendError(f"Failed to check for new messages (page fetch failed): HTTP {resp.status}")
            next_url = self._next_page_url(resp)

    def read(self, target: Target, cursor: Cursor) -> ReadResult:
        """Read the messages after the cursor, oldest first, pages included.

        "New" is decided by the backend's arrival position and time (D4), and

        the returned cursor carries the biggest position seen, its backend
        time, and the ids read within the margin window. A `--since` cursor
        (time only) returns the comments at or after that time and moves
        nothing.
        """
        since = cursor_since(cursor)
        url: str | None = self._listing_url(target, since=since)
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
                if parsed is None or not self._is_in_scope(parsed, target):
                    continue
                meta = self._comment_meta(comment)
                if meta is None:
                    continue
                comment_id, created_at = meta
                entries.append((comment_id, created_at))
                if not comment_is_new(comment_id, created_at, cursor):
                    continue

                setattr(parsed, "received_at", created_at)
                issue_url = comment.get("issue_url", "")
                if issue_url:
                    match = re.search(r"/issues/(\d+)", str(issue_url))
                    if match:
                        setattr(parsed, "issue_number", int(match.group(1)))
                        if parsed.channel:
                            self._channel_numbers[parsed.channel] = int(match.group(1))
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
