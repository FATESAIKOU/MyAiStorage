"""GitHub issues and comments backend for Angareion (task 1.4)."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
import urllib.error
import urllib.request

from angareion.backend import (
    AuthenticationError,
    BackendError,
    ChannelNotFoundError,
    MessageListResult,
    RateLimitError,
)
from angareion.config import Config, load_token, mask_token
from angareion.messages import Message, parse_message, serialize_message


class HttpResponse:
    """HTTP response wrapper."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


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


class DefaultHttpTransport:
    """Standard library urllib HTTP transport."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                headers_dict = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                return HttpResponse(resp.status, headers_dict, resp.read())
        except urllib.error.HTTPError as e:
            headers_dict = {str(k).lower(): str(v) for k, v in e.headers.items()}
            return HttpResponse(e.code, headers_dict, e.read())
        except urllib.error.URLError as e:
            raise BackendError(f"Network error connecting to GitHub: {e.reason}") from e


def _extract_next_link(link_header: str | None) -> str | None:
    """Extract next page URL from Link header."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


class GitHubBackend:
    """Angareion backend backed by a private GitHub repository's issues and comments."""

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
        self._channel_cache: dict[str, int] = {}

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.github.com",
    ) -> GitHubBackend:
        token = load_token(config.token_file)
        return cls(
            channel_repo=config.channel_repo,
            token=token,
            transport=transport,
            base_url=base_url,
        )

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Angareion-A2A/0.1.0",
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
            msg = mask_token(str(e), self.token or "")
            raise BackendError(f"HTTP request failed: {msg}") from e

        if resp.status == 401:
            raise AuthenticationError("Invalid or missing GitHub token (HTTP 401)")

        if resp.status == 403:
            retry_after_str = resp.headers.get("retry-after")
            retry_after = int(retry_after_str) if retry_after_str and retry_after_str.isdigit() else 60
            raise RateLimitError(
                f"Secondary rate limit reached on GitHub (HTTP 403, retry-after: {retry_after}s)",
                retry_after=retry_after,
            )

        return resp

    def find_channel(self, channel: str) -> int | None:
        """Find the issue number for a channel name, or None if not found."""
        if channel in self._channel_cache:
            return self._channel_cache[channel]

        url: str | None = f"repos/{self.owner}/{self.repo}/issues?state=all&per_page=100"
        while url:
            resp = self._request("GET", url, headers=self._headers())
            if resp.status >= 400:
                raise BackendError(f"Failed to find channel '{channel}': HTTP {resp.status}")

            issues = resp.json()
            for issue in issues:
                if "pull_request" in issue:
                    continue
                title = issue.get("title")
                number = issue.get("number")
                if title and number:
                    self._channel_cache[title] = number
                    if title == channel:
                        return number

            url = _extract_next_link(resp.headers.get("link"))

        return None

    def create_channel(self, channel: str) -> int:
        """Create a new channel topic (issue) and return its issue number."""
        if not channel or not isinstance(channel, str):
            raise ValueError("Channel name must be a non-empty string")

        existing = self.find_channel(channel)
        if existing is not None:
            return existing

        path = f"repos/{self.owner}/{self.repo}/issues"
        resp = self._request(
            "POST",
            path,
            headers=self._headers(),
            data={"title": channel, "body": f"Angareion channel: {channel}"},
        )
        if resp.status not in (200, 201):
            raise BackendError(f"Failed to create channel '{channel}': HTTP {resp.status}")

        data = resp.json()
        number = data["number"]
        self._channel_cache[channel] = number
        return number

    def list_channels(self) -> list[tuple[str, int]]:
        """List all known (channel, issue_number) pairs."""
        url: str | None = f"repos/{self.owner}/{self.repo}/issues?state=all&per_page=100"
        while url:
            resp = self._request("GET", url, headers=self._headers())
            if resp.status >= 400:
                break
            issues = resp.json()
            for issue in issues:
                if "pull_request" in issue:
                    continue
                title = issue.get("title")
                number = issue.get("number")
                if title and number:
                    self._channel_cache[title] = number
            url = _extract_next_link(resp.headers.get("link"))

        return list(self._channel_cache.items())

    def post_message(self, channel: str, message: Message | str) -> str:
        """Post a message to a channel. Checks recent comments to prevent duplicates."""
        issue_number = self.find_channel(channel)
        if issue_number is None:
            raise ChannelNotFoundError(
                f"Channel '{channel}' does not exist. Use --new to create it."
            )

        if isinstance(message, str):
            parsed = parse_message(message, strict=True)
            if parsed is None:
                raise ValueError("Could not parse message text")
            msg_obj = parsed
            serialized_text = message
        else:
            msg_obj = message
            msg_obj.validate()
            serialized_text = serialize_message(msg_obj)

        if msg_obj.channel != channel:
            raise ValueError(
                f"Message channel '{msg_obj.channel}' does not match target channel '{channel}'"
            )

        # Idempotency check: check recent comments for the same message id
        recent_url = f"repos/{self.owner}/{self.repo}/issues/{issue_number}/comments?per_page=30"
        resp_recent = self._request("GET", recent_url, headers=self._headers())
        if resp_recent.status == 200:
            for c in resp_recent.json():
                body = c.get("body", "")
                if f"id: {msg_obj.id}" in body or f'id: "{msg_obj.id}"' in body:
                    comment_url = c.get("html_url", "")
                    setattr(msg_obj, "issue_number", issue_number)
                    setattr(msg_obj, "comment_url", comment_url)
                    return comment_url

        # Post comment to issue
        post_url = f"repos/{self.owner}/{self.repo}/issues/{issue_number}/comments"
        resp_post = self._request("POST", post_url, headers=self._headers(), data={"body": serialized_text})
        if resp_post.status not in (200, 201):
            raise BackendError(
                f"Failed to post comment to channel '{channel}' (issue {issue_number}): HTTP {resp_post.status}"
            )

        post_data = resp_post.json()
        comment_url = post_data.get("html_url", "")
        setattr(msg_obj, "issue_number", issue_number)
        setattr(msg_obj, "comment_url", comment_url)
        return comment_url

    def list_messages(
        self, since: str | None = None, etag: str | None = None
    ) -> MessageListResult:
        """List messages across the repository using the repo-level comments endpoint."""
        url_params = ["per_page=100"]
        if since:
            url_params.append(f"since={since}")
        query = "&".join(url_params)
        first_url: str | None = f"repos/{self.owner}/{self.repo}/issues/comments?{query}"

        # First request sends ETag if provided
        resp = self._request("GET", first_url, headers=self._headers(etag=etag))
        if resp.status == 304:
            return MessageListResult([], etag, not_modified=True)

        if resp.status >= 400:
            raise BackendError(f"Failed to list messages: HTTP {resp.status}")

        new_etag = resp.headers.get("etag")
        collected_messages: list[Message] = []

        url: str | None = first_url
        current_resp = resp

        while url:
            if url != first_url:
                current_resp = self._request("GET", url, headers=self._headers())
                if current_resp.status >= 400:
                    break

            comments = current_resp.json()
            for comment in comments:
                body = comment.get("body", "")
                parsed = parse_message(body)
                if parsed is not None:
                    issue_url = comment.get("issue_url", "")
                    comment_url = comment.get("html_url", "")
                    issue_num: int | None = None
                    if issue_url:
                        match = re.search(r"/issues/(\d+)", issue_url)
                        if match:
                            issue_num = int(match.group(1))

                    if issue_num is not None:
                        setattr(parsed, "issue_number", issue_num)
                        if parsed.channel:
                            self._channel_cache[parsed.channel] = issue_num

                    if comment_url:
                        setattr(parsed, "comment_url", comment_url)

                    collected_messages.append(parsed)

            url = _extract_next_link(current_resp.headers.get("link"))

        collected_messages.sort(key=lambda m: (m.at, m.id))
        return MessageListResult(collected_messages, new_etag, not_modified=False)
