"""ChannelBackend protocol (the four operations), targets, cursors and the fake backend (D2/D4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Protocol, runtime_checkable

from angareion.config import identity_slug
from angareion.messages import Message

CURSOR_SEEN_MARGIN_SECONDS = 60.0
BACKEND_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class BackendError(Exception):
    """Base exception for Angareion backend errors."""


class ChannelNotFoundError(BackendError):
    """Raised when a channel does not exist."""


class AuthenticationError(BackendError):
    """Raised when the token is invalid (HTTP 401)."""


@dataclass(frozen=True)
class Channel:
    """A channel: a name, the backend's number for it, and its members."""

    name: str
    number: int
    members: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Target:
    """Where to check/read: one channel, or everything addressed to me.

    Exactly one of `channel` and `recipient` must be set. `recipient` is the
    reader's agent name; "to me" spans all channels.
    """

    channel: str | None = None
    recipient: str | None = None

    def __post_init__(self) -> None:
        if (self.channel is None) == (self.recipient is None):
            raise ValueError("Target needs exactly one of channel or recipient")

    def key(self) -> str:
        """Stable key for this target in the local cursor file."""
        if self.channel is not None:
            return f"channel:{self.channel}"
        return "to:" + identity_slug(self.recipient or "")


def parse_backend_time(value: str) -> datetime:
    """Parse a backend-recorded RFC 3339 time (GitHub writes `...Z`)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_backend_time(moment: datetime) -> str:
    """Format a backend time the way the backends write it (UTC, `Z`)."""
    return moment.astimezone(timezone.utc).strftime(BACKEND_TIME_FORMAT)


@dataclass
class Cursor:
    """Where a target has been read, in backend terms (D4).

    `position` is the backend's opaque arrival position (GitHub: the comment
    id, a decimal string); `at` is the backend-recorded time of the last read
    message (RFC 3339, UTC); `seen` are the backend ids already read within
    `at - margin`. The sender's `at` and ULID take no part in any of this.
    """

    position: str | None = None
    at: str | None = None
    seen: list[str] = field(default_factory=list)


def cursor_since(cursor: Cursor, *, margin: float = CURSOR_SEEN_MARGIN_SECONDS) -> str | None:
    """The `since` lower bound for a listing query (D4).

    A local cursor asks from `at - margin` (to catch comments that arrive
    behind the position); a time-only cursor (`--since`) asks from its time
    as-is, and the comparison is inclusive.
    """
    if cursor.at is None:
        return None
    at = parse_backend_time(cursor.at)
    if cursor.position is not None:
        at -= timedelta(seconds=margin)
    return format_backend_time(at)


def comment_is_new(comment_id: int, created_at: str, cursor: Cursor) -> bool:
    """Whether a backend comment is new for this cursor (D4).

    New = the comment's arrival position is past the cursor's, or its backend
    time is within `at - margin` of the cursor and its id has not been read.
    The second clause catches GitHub's replication lag (a smaller id showing
    up after a bigger one); an edited already-read comment has an id at or
    below the position and, once out of the window, never comes back.
    """
    if cursor.position is None and cursor.at is None:
        return True
    if cursor.position is None:
        # a time-only cursor (`--since`): backend time >= the given time, inclusive
        return parse_backend_time(created_at) >= parse_backend_time(cursor.at or "")
    if comment_id > int(cursor.position):
        return True
    if cursor.at is None:
        return False
    boundary = parse_backend_time(cursor.at) - timedelta(seconds=CURSOR_SEEN_MARGIN_SECONDS)
    return parse_backend_time(created_at) >= boundary and str(comment_id) not in set(cursor.seen)


def advance_cursor(previous: Cursor, entries: list[tuple[int, str]]) -> Cursor:
    """The cursor after reading (D4).

    `entries` are the in-scope comments seen in the response, as
    (comment id, backend time). The position is the biggest id seen and never
    moves backwards; the time is that comment's backend time; `seen` is
    recomputed as the ids within `at - margin` (entries that fell out of the
    window drop off — they can no longer match `comment_is_new`'s window).
    """
    if not entries:
        return previous

    newest_id, newest_at = max(entries, key=lambda entry: entry[0])
    position = newest_id
    at = newest_at
    if previous.position is not None and newest_id <= int(previous.position):
        position = int(previous.position)
        if previous.at is not None:
            at = previous.at
    if at is None:
        return previous

    boundary = parse_backend_time(at) - timedelta(seconds=CURSOR_SEEN_MARGIN_SECONDS)
    seen = sorted({str(cid) for cid, created in entries if parse_backend_time(created) >= boundary})
    return Cursor(position=str(position), at=at, seen=seen)


class ReadResult(NamedTuple):
    """What `read` returns: the new messages (oldest first) and the new cursor."""

    messages: list[Message]
    cursor: Cursor


@runtime_checkable
class ChannelBackend(Protocol):
    """The backend is the same four operations as the four interfaces."""

    def create_channel(self, name: str, members: list[dict[str, Any]]) -> Channel:
        """Create a channel (idempotent) and return it."""
        ...

    def send(self, channel: str, message: Message) -> str:
        """Post a message to a channel; return the message id."""
        ...

    def check(self, target: Target, cursor: Cursor, *, timeout: float | None = None) -> bool:
        """Whether there is any new message for the target right now.

        `timeout` is an optional budget in seconds for the backend's own
        network requests (the wait loop passes its remaining time); None means
        the backend's own default.
        """
        ...

    def read(self, target: Target, cursor: Cursor) -> ReadResult:
        """Read the messages after the cursor (oldest first), and the new cursor."""
        ...


def address_matches_identity(address: Any, recipient: dict[str, Any]) -> bool:
    """Whether a message's `to` is this member (name is display-only)."""
    if not isinstance(address, dict) or "group" in address:
        return False
    return address.get("repo") == recipient.get("repo") and address.get("role") == recipient.get("role")


@dataclass(frozen=True)
class FakeComment:
    """A comment as the fake backend stores it: arrival id and backend time."""

    id: int
    created_at: str
    message: Message


def _utc_now() -> str:
    return format_backend_time(datetime.now(timezone.utc))


class FakeBackend:
    """In-memory ChannelBackend for tests and local development; never touches the network.

    Comments carry a backend arrival id (like GitHub's comment id) and a
    backend time (like `created_at`); `deliver` lets a test pin both, so a
    replication lag (a smaller id appearing after a bigger one) can be played
    out.
    """

    def __init__(
        self,
        channels: dict[str, int] | None = None,
        messages: list[Message] | None = None,
        members: dict[str, list[dict[str, Any]]] | None = None,
        *,
        start_comment_id: int = 1,
        clock: Any = None,
    ):
        self._channels: dict[str, Channel] = {}
        self._comments: list[FakeComment] = []
        self._next_comment_id = start_comment_id
        self.next_error: Exception | None = None
        self._clock = clock or _utc_now

        for name, number in (channels or {}).items():
            self._channels[name] = Channel(name=name, number=number, members=list((members or {}).get(name, [])))

        for msg in messages or []:
            if msg.channel not in self._channels:
                self.create_channel(msg.channel, [])
            self.deliver(msg.channel, msg)

    def _check_injected_error(self) -> None:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err

    def create_channel(self, name: str, members: list[dict[str, Any]]) -> Channel:
        self._check_injected_error()
        if not name or not isinstance(name, str):
            raise ValueError("Channel name must be a non-empty string")
        if name in self._channels:
            return self._channels[name]
        numbers = [channel.number for channel in self._channels.values()]
        channel = Channel(name=name, number=(max(numbers) + 1) if numbers else 1, members=list(members))
        self._channels[name] = channel
        return channel

    def deliver(
        self,
        channel: str,
        message: Message,
        *,
        comment_id: int | None = None,
        created_at: str | None = None,
    ) -> FakeComment:
        """Record a message as a backend comment; tests may pin id/created_at."""
        self._check_injected_error()
        if channel not in self._channels:
            raise ChannelNotFoundError(f"Channel '{channel}' does not exist")
        message.validate()
        if message.channel != channel:
            raise ValueError(
                f"Message channel '{message.channel}' does not match target channel '{channel}'"
            )
        cid = self._next_comment_id if comment_id is None else comment_id
        if cid >= self._next_comment_id:
            self._next_comment_id = cid + 1
        comment = FakeComment(id=cid, created_at=created_at or self._clock(), message=message)
        self._comments.append(comment)
        return comment

    def send(self, channel: str, message: Message) -> str:
        self.deliver(channel, message)
        return message.id

    def _comments_for(self, target: Target) -> list[FakeComment]:
        if target.channel is not None:
            if target.channel not in self._channels:
                raise ChannelNotFoundError(f"Channel '{target.channel}' does not exist")
            return [c for c in self._comments if c.message.channel == target.channel]
        recipient = target.recipient or {}
        return [c for c in self._comments if address_matches_identity(c.message.to, recipient)]

    def check(self, target: Target, cursor: Cursor, *, timeout: float | None = None) -> bool:
        self._check_injected_error()
        return any(comment_is_new(c.id, c.created_at, cursor) for c in self._comments_for(target))

    def read(self, target: Target, cursor: Cursor) -> ReadResult:
        self._check_injected_error()
        in_scope = self._comments_for(target)
        matched = [c for c in in_scope if comment_is_new(c.id, c.created_at, cursor)]
        matched.sort(key=lambda c: c.id)

        messages: list[Message] = []
        for comment in matched:
            setattr(comment.message, "received_at", comment.created_at)
            messages.append(comment.message)

        if cursor.position is None and cursor.at is not None:
            return ReadResult(messages, cursor)  # a `--since` read moves nothing
        return ReadResult(messages, advance_cursor(cursor, [(c.id, c.created_at) for c in in_scope]))
