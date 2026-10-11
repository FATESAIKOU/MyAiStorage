"""GitHub Issues backend: channel = issue, message = comment (D2)."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

import yaml

from angareion.backend import (
    AuthenticationError,
    BackendError,
    Channel,
    ChannelNotFoundError,
    Cursor,
    ReadResult,
    Target,
    address_matches_identity,
    is_message_after_cursor,
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
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                headers_dict = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                return HttpResponse(resp.status, headers_dict, resp.read())
        except urllib.error.HTTPError as e:
            headers_dict = {str(k).lower(): str(v) for k, v in e.headers.items()}
            return HttpResponse(e.code, headers_dict, e.read())
        except TimeoutError as e:
            raise BackendError(f"HTTP request timed out after {self.timeout:.0f}s") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise BackendError(f"HTTP request timed out after {self.timeout:.0f}s") from e
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
            resp = self.transport.request(method, url, headers=req_headers, body=body_bytes)
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

    def _list_issues(self) -> list[dict[str, Any]]:
        """Every issue (newest first, as GitHub lists them); caches titles.

        GitHub lists newest first, so the write-through of `_issues_cache`
        leaves the *oldest* issue for a duplicated title — the one that wins
        (L4). Pull requests and entries without a title/number are skipped.
        """
        issues: list[dict[str, Any]] = []
        url: str | None = f"repos/{self.owner}/{self.repo}/issues?state=all&per_page=100"
        while url:
            resp = self._request("GET", url, headers=self._headers())
            if resp.status >= 400:
                raise BackendError(f"Failed to list issues: HTTP {resp.status}")

            for issue in self._json(resp, "listing issues"):
                if "pull_request" in issue:
                    continue
                title = issue.get("title")
                number = issue.get("number")
                if not title or not number:
                    continue
                issues.append(issue)
                self._issues_cache[title] = issue
                self._channel_numbers[title] = number

            url = self._next_page_url(resp)
        return issues

    def _find_issue(self, name: str) -> dict[str, Any] | None:
        """The issue JSON for a channel name, or None."""
        if name in self._issues_cache:
            return self._issues_cache[name]

        self._list_issues()

        if name in self._issues_cache:
            return self._issues_cache[name]
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

    def _listing_url(self, target: Target, cursor: Cursor) -> str:
        if target.channel is not None:
            issue = self._find_issue(target.channel)
            if issue is None:
                raise ChannelNotFoundError(f"Channel '{target.channel}' does not exist")
            url = f"repos/{self.owner}/{self.repo}/issues/{issue['number']}/comments?per_page=100"
        else:
            url = f"repos/{self.owner}/{self.repo}/issues/comments?per_page=100"
        if cursor.at:
            url += f"&since={urllib.parse.quote(cursor.at, safe='')}"
        return url

    def _is_new_for_target(self, message: Message, target: Target, cursor: Cursor) -> bool:
        if not is_message_after_cursor(message, cursor):
            return False
        if target.recipient is not None and not address_matches_identity(message.to, target.recipient):
            return False
        return True

    def _page_has_new(self, comments: list[dict[str, Any]], target: Target, cursor: Cursor) -> bool:
        for comment in comments:
            parsed = parse_message(comment.get("body", ""))
            if parsed is not None and self._is_new_for_target(parsed, target, cursor):
                return True
        return False

    def check(self, target: Target, cursor: Cursor) -> bool:
        """Whether there is anything new, with an in-process ETag if we have one.

        The ETag lives for one process only (`wait` polls in one process); the
        cursor file never learns it, so a later `read` cannot be skipped by a
        304. A 304 means nothing changed since the last poll: no new messages.
        """
        url = self._listing_url(target, cursor)
        key = target.key()
        resp = self._request("GET", url, headers=self._headers(etag=self._check_etags.get(key)))
        if resp.status == 304:
            return False
        if resp.status >= 400:
            raise BackendError(f"Failed to check for new messages: HTTP {resp.status}")

        self._check_etags[key] = resp.headers.get("etag") or None
        if self._page_has_new(self._json(resp, "checking for new messages"), target, cursor):
            return True

        while (next_url := self._next_page_url(resp)):
            resp = self._request("GET", next_url, headers=self._headers())
            if resp.status >= 400:
                raise BackendError(f"Failed to check for new messages (page fetch failed): HTTP {resp.status}")
            if self._page_has_new(self._json(resp, "checking for new messages"), target, cursor):
                return True
        return False

    def read(self, target: Target, cursor: Cursor) -> ReadResult:
        """Read the messages after the cursor, oldest first, pages included.

        Messages are deduplicated by id and filtered against the (at, id)
        cursor boundary; an edited already-read comment therefore does not
        come back, and two messages in the same second both get through.
        """
        url: str | None = self._listing_url(target, cursor)
        resp = self._request("GET", url, headers=self._headers())
        if resp.status >= 400:
            raise BackendError(f"Failed to read messages: HTTP {resp.status}")

        seen_ids: set[str] = set()
        collected: list[Message] = []

        while url:
            if resp.status >= 400:
                raise BackendError(f"Failed to read messages (page fetch failed): HTTP {resp.status}")

            for comment in self._json(resp, "reading messages"):
                parsed = parse_message(comment.get("body", ""))
                if parsed is None or parsed.id in seen_ids:
                    continue
                seen_ids.add(parsed.id)
                if not self._is_new_for_target(parsed, target, cursor):
                    continue

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

                collected.append(parsed)

            url = self._next_page_url(resp)
            if url:
                resp = self._request("GET", url, headers=self._headers())

        collected.sort(key=lambda m: (m.at, m.id))
        if collected:
            last = collected[-1]
            new_cursor = Cursor(at=last.at, id=last.id)
        else:
            new_cursor = Cursor(at=cursor.at, id=cursor.id)
        return ReadResult(collected, new_cursor)
