"""The registry issue: one-line JSON action comments and the client that posts them.

Standard library only: the registry rebuild workflow runs this module's format
helpers on a bare runner (`PYTHONPATH=src`, no third-party packages).

The format (design section 9/10) is one line of JSON:

    {"schema": 1, "action": "register"|"unregister", "channel": ..., "backend": ..., "issue": N}

Everything else is not an action. A comment that *looks like* an attempt (it
starts with `{`) but fails validation is reported by the rebuild workflow when
its author is OWNER/MEMBER/COLLABORATOR; a plain comment is ignored silently.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import re
import time
from typing import Any, Callable, Protocol

from angareion.transport import DefaultHttpTransport, HttpTransport, HttpResponse

ACTION_SCHEMA = 1
ACTIONS = ("register", "unregister")
AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
REGISTRY_RETRY_ATTEMPTS = 3
REGISTRY_RETRY_INTERVAL_SECONDS = 1.0
REGISTRY_USER_AGENT = "Angareion-Registry/0.2.0"
COMMENTS_PAGE_SIZE = 100
# Mirrors messages.CHANNEL_NAME_RE; kept here so this module stays dependency-free.
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class RegistryError(Exception):
    """The registry issue could not be read or written (exit 2 for the CLI)."""


class RegistryWriteConflict(RegistryError):
    """The file changed while rebuilding (sha mismatch): re-read and retry."""


def action_comment(action: str, channel: str, backend: str, issue: int) -> str:
    """The one-line JSON a register/unregister comment carries."""
    payload = {
        "schema": ACTION_SCHEMA,
        "action": action,
        "channel": channel,
        "backend": backend,
        "issue": issue,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))


def register_comment(channel: str, backend: str, issue: int) -> str:
    return action_comment("register", channel, backend, issue)


def unregister_comment(channel: str, backend: str, issue: int) -> str:
    return action_comment("unregister", channel, backend, issue)


@dataclass(frozen=True)
class Action:
    """A valid register/unregister comment."""

    action: str
    channel: str
    backend: str
    issue: int


@dataclass(frozen=True)
class InvalidAction:
    """A comment that tries to be an action but is not one; the reason is reported."""

    reason: str


def parse_comment(body: Any) -> Action | InvalidAction | None:
    """Classify one comment body.

    None: not an action attempt (a plain comment, the workflow's own report,
    the pinned comment) — ignored silently.
    InvalidAction: starts with `{` but is not a valid action — skipped, and
    reported when the author is authorized.
    Action: a valid register/unregister.
    """
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return InvalidAction("the comment starts with '{' but is not valid JSON")
    if not isinstance(data, dict):
        return InvalidAction("the comment is not a JSON object")

    required = {"schema", "action", "channel", "backend", "issue"}
    missing = required - set(data)
    if missing:
        return InvalidAction(f"missing fields: {', '.join(sorted(missing))}")
    extra = set(data) - required
    if extra:
        return InvalidAction(f"unknown fields: {', '.join(sorted(extra))}")

    if data["schema"] != ACTION_SCHEMA:
        return InvalidAction(f"unsupported schema {data['schema']!r} (expected {ACTION_SCHEMA})")
    if data["action"] not in ACTIONS:
        return InvalidAction(f"unknown action {data['action']!r}")
    if not isinstance(data["channel"], str) or not CHANNEL_NAME_RE.match(data["channel"]):
        return InvalidAction(f"invalid channel name {data['channel']!r}")
    if not isinstance(data["backend"], str) or not data["backend"]:
        return InvalidAction("the backend name must be a non-empty string")
    issue = data["issue"]
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        return InvalidAction(f"the issue number must be a positive integer, got {issue!r}")
    return Action(action=data["action"], channel=data["channel"], backend=data["backend"], issue=issue)


class CommentPoster(Protocol):
    """What `channel create`/`channel delete` need from a registry client."""

    def post_comment(self, body: str) -> None:
        ...


def post_with_retries(
    poster: CommentPoster,
    body: str,
    *,
    attempts: int = REGISTRY_RETRY_ATTEMPTS,
    interval: float = REGISTRY_RETRY_INTERVAL_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Post one comment; retry a couple of times with a short gap, then raise."""
    last_error: RegistryError | None = None
    for attempt in range(attempts):
        try:
            poster.post_comment(body)
            return
        except RegistryError as e:
            last_error = e
            if attempt < attempts - 1:
                sleeper(interval)
    assert last_error is not None
    raise last_error


class RegistryClient:
    """The real registry issue on GitHub: comments plus the channels.json file.

    The token is the channel token (`Issues: Read and write` on MyAiStorage);
    the file writes use the same repo. Only the rebuild workflow uses the file
    side; `channel create`/`channel delete` only post comments.
    """

    def __init__(
        self,
        repo: str,
        issue: int,
        token: str | None = None,
        *,
        transport: HttpTransport | None = None,
        base_url: str = "https://api.github.com",
        branch: str = "main",
    ):
        if "/" not in repo:
            raise RegistryError(f"registry repo must be 'owner/repo', got {repo!r}")
        self.repo = repo
        self.issue = issue
        self.token = token
        self.transport: HttpTransport = transport or DefaultHttpTransport()
        self.base_url = base_url.rstrip("/")
        self.branch = branch

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": REGISTRY_USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
        accept: tuple[int, ...] = (),
    ) -> HttpResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self.transport.request(method, url, headers=headers, body=body, timeout=timeout)
        except Exception as e:
            raise RegistryError(f"HTTP request failed: {e}") from e
        if resp.status in accept:
            return resp
        if resp.status == 401:
            raise RegistryError("the registry token was rejected (HTTP 401)")
        if resp.status in (409, 422) and method == "PUT":
            raise RegistryWriteConflict(f"the file changed while writing (HTTP {resp.status})")
        if resp.status >= 400:
            raise RegistryError(f"the registry request failed (HTTP {resp.status})")
        return resp

    # -- comments --------------------------------------------------------

    def post_comment(self, body: str) -> None:
        self._request(
            "POST",
            f"repos/{self.repo}/issues/{self.issue}/comments",
            data={"body": body},
        )

    def list_comments(self, page: int) -> list[dict[str, Any]]:
        """One page of the registry thread (100 comments per page), oldest first."""
        resp = self._request(
            "GET",
            f"repos/{self.repo}/issues/{self.issue}/comments?per_page={COMMENTS_PAGE_SIZE}&page={page}",
        )
        try:
            data = resp.json()
        except ValueError as e:
            raise RegistryError("the comment listing was not JSON") from e
        if not isinstance(data, list):
            raise RegistryError("the comment listing was not a JSON array")
        return data

    def all_comments(self) -> list[dict[str, Any]]:
        """Every comment on the registry issue, following pagination (review M4)."""
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.list_comments(page)
            comments.extend(batch)
            if len(batch) < COMMENTS_PAGE_SIZE:
                return comments
            page += 1

    # -- files -----------------------------------------------------------

    def get_file(self, path: str) -> tuple[str, str | None]:
        """The file text and its blob sha; a missing file is (``""``, None)."""
        resp = self._request(
            "GET", f"repos/{self.repo}/contents/{path}?ref={self.branch}", accept=(404,)
        )
        if resp.status == 404:
            return "", None
        try:
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
        except Exception as e:
            raise RegistryError(f"could not decode {path} from the repository") from e
        return content, data.get("sha")

    def put_file(self, path: str, text: str, *, sha: str | None, message: str) -> None:
        """Write the file on the branch; a stale sha raises RegistryWriteConflict."""
        data: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha is not None:
            data["sha"] = sha
        self._request("PUT", f"repos/{self.repo}/contents/{path}", data=data)


class FakeRegistryClient:
    """In-memory registry issue for tests: comments, files and the sha lock."""

    def __init__(
        self,
        *,
        comments: list[dict[str, Any]] | None = None,
        files: dict[str, str] | None = None,
        fail_comments_times: int = 0,
        fail_posts_times: int = 0,
        stale_writes: int = 0,
        page_size: int = COMMENTS_PAGE_SIZE,
    ):
        self._comments: list[dict[str, Any]] = [dict(c) for c in (comments or [])]
        self.files: dict[str, str] = dict(files or {})
        self.posted: list[str] = []
        self.fail_comments_times = fail_comments_times
        self.fail_posts_times = fail_posts_times
        self.stale_writes = stale_writes
        self.page_size = page_size
        self.list_calls: list[int] = []
        self.put_calls: list[tuple[str, str | None]] = []
        self._sha = 1

    def add_comment(
        self,
        body: str,
        *,
        author_association: str = "OWNER",
        user_type: str = "User",
        comment_id: int | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        index = len(self._comments) + 1
        comment = {
            "id": comment_id if comment_id is not None else index,
            "body": body,
            "author_association": author_association,
            "user": {"type": user_type, "login": "someone"},
            "created_at": created_at or f"2026-10-10T12:00:{index:02d}Z",
        }
        self._comments.append(comment)
        return comment

    def list_comments(self, page: int) -> list[dict[str, Any]]:
        self.list_calls.append(page)
        if self.fail_comments_times > 0:
            self.fail_comments_times -= 1
            raise RegistryError("comment listing failed")
        start = (page - 1) * self.page_size
        return self._comments[start : start + self.page_size]

    def all_comments(self) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.list_comments(page)
            comments.extend(batch)
            if len(batch) < self.page_size:
                return comments
            page += 1

    def post_comment(self, body: str) -> None:
        if self.fail_posts_times > 0:
            self.fail_posts_times -= 1
            raise RegistryError("posting the comment failed")
        self.posted.append(body)
        self.add_comment(body, author_association="NONE", user_type="Bot")

    def get_file(self, path: str) -> tuple[str, str | None]:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path], f"sha-{self._sha}"

    def put_file(self, path: str, text: str, *, sha: str | None, message: str) -> None:
        self.put_calls.append((path, sha))
        if self.stale_writes > 0:
            self.stale_writes -= 1
            raise RegistryWriteConflict("the file changed while rebuilding")
        self.files[path] = text
        self._sha += 1
