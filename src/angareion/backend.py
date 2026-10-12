"""ChannelBackend protocol (create/close/send/check/read), targets, cursors, fake backend.

v5: a channel is a name that a config file points at a backend entity; the
backend never stores members. `create_channel` opens the entity, `close_channel`
closes it (without deleting it), `send`/`check`/`read` work on a resolved
`Entity`. "To me" spans every channel, so `check`/`read` take an optional
recipient filter; a message without `to` is a broadcast and counts for everyone.
"""

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
class BackendRef:
    """One backends.json entry: a named backend and where its entities live."""

    name: str
    type: str
    repo: str


@dataclass(frozen=True)
class Entity:
    """A resolved channel: the backend entity a command may act on."""

    channel: str
    backend: str
    repo: str
    issue: int


class EntityResolver(Protocol):
    """What a backend needs to turn a channel name into an entity."""

    def entity_for(self, channel: str) -> Entity:
        """The entity for a channel name; raises ChannelNotFoundError when absent."""
        ...

    def entities(self) -> list[Entity]:
        """Every configured channel as an entity ("to me" spans them all)."""
        ...


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
    """The backend is the same operations as the five interfaces."""

    def create_channel(self, name: str, backend: BackendRef) -> int:
        """Open the backend entity for a channel; return its number."""
        ...

    def verify_channel(self, name: str, backend: BackendRef, entity: int) -> bool:
        """Whether the entity exists and its title is exactly the channel name."""
        ...

    def close_channel(self, entity: Entity) -> None:
        """Close the entity (without deleting it); an already-deleted entity is fine."""
        ...

    def send(self, entity: Entity, message: Message) -> str:
        """Post a message to the entity; return the message id."""
        ...

    def check(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Whether there is any new message for the entity right now.

        `recipient` filters "to me" (messages whose `to` is that agent name or
        that have no `to` at all); None means every message in the channel.
        `timeout` is an optional budget in seconds for the backend's own
        network requests (the wait loop passes its remaining time).
        """
        ...

    def read(self, entity: Entity, cursor: Cursor, *, recipient: str | None = None) -> ReadResult:
        """Read the messages after the cursor (oldest first), and the new cursor."""
        ...


def recipient_matches(message: Message, recipient: str | None) -> bool:
    """Whether a message counts as "to me": addressed to me, or a broadcast."""
    if recipient is None:
        return True
    return message.to is None or message.to == recipient


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

    Entities live in memory: `create_channel` opens one (title = channel name),
    `close_channel` closes it without deleting it. Comments carry a backend
    arrival id (like GitHub's comment id) and a backend time (like
    `created_at`); `deliver` lets a test pin both, so a replication lag (a
    smaller id appearing after a bigger one) can be played out.
    """

    def __init__(
        self,
        resolver: EntityResolver,
        *,
        start_entity_id: int = 1,
        start_comment_id: int = 1,
        clock: Any = None,
    ):
        self.resolver = resolver
        self._entities: dict[tuple[str, int], dict[str, Any]] = {}
        self._comments: list[FakeComment] = []
        self._next_entity_id = start_entity_id
        self._next_comment_id = start_comment_id
        self.next_error: Exception | None = None
        self.next_create_error: Exception | None = None
        self.next_close_error: Exception | None = None
        self._clock = clock or _utc_now

    @staticmethod
    def _key(repo: str, number: int) -> tuple[str, int]:
        """Entities live per repo: the same issue number in two repos are different entities."""
        return (repo, number)

    def _check_injected_error(self) -> None:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err

    # -- test helpers ----------------------------------------------------

    def add_entity(self, title: str, *, number: int | None = None, repo: str = "owner/repo") -> int:
        """Pre-create an entity for tests that set up the config file first."""
        number = self._next_entity_id if number is None else number
        self._entities[self._key(repo, number)] = {"title": title, "state": "open"}
        self._next_entity_id = max(self._next_entity_id, number + 1)
        return number

    def entity_state(self, number: int, repo: str = "owner/repo") -> str | None:
        """The state of an entity ("open"/"closed"), or None when it does not exist."""
        entry = self._entities.get(self._key(repo, number))
        return entry["state"] if entry else None

    def set_entity_title(self, number: int, title: str, *, repo: str = "owner/repo") -> None:
        self._entities[self._key(repo, number)]["title"] = title

    def entity_title(self, number: int, repo: str = "owner/repo") -> str | None:
        entry = self._entities.get(self._key(repo, number))
        return entry["title"] if entry else None

    def deliver(
        self,
        entity: Entity,
        message: Message,
        *,
        comment_id: int | None = None,
        created_at: str | None = None,
    ) -> FakeComment:
        """Record a message as a backend comment; tests may pin id/created_at."""
        self._check_injected_error()
        message.validate()
        if message.channel != entity.channel:
            raise ValueError(
                f"Message channel '{message.channel}' does not match target channel '{entity.channel}'"
            )
        cid = self._next_comment_id if comment_id is None else comment_id
        if cid >= self._next_comment_id:
            self._next_comment_id = cid + 1
        comment = FakeComment(id=cid, created_at=created_at or self._clock(), message=message)
        self._comments.append(comment)
        return comment

    # -- the backend protocol -------------------------------------------

    def create_channel(self, name: str, backend: BackendRef) -> int:
        self._check_injected_error()
        if self.next_create_error is not None:
            err = self.next_create_error
            self.next_create_error = None
            raise err
        if not name or not isinstance(name, str):
            raise ValueError("Channel name must be a non-empty string")
        number = self._next_entity_id
        self._next_entity_id += 1
        self._entities[self._key(backend.repo, number)] = {"title": name, "state": "open"}
        return number

    def verify_channel(self, name: str, backend: BackendRef, entity: int) -> bool:
        self._check_injected_error()
        entry = self._entities.get(self._key(backend.repo, entity))
        return entry is not None and entry["title"] == name

    def close_channel(self, entity: Entity) -> None:
        self._check_injected_error()
        if self.next_close_error is not None:
            err = self.next_close_error
            self.next_close_error = None
            raise err
        entry = self._entities.get(self._key(entity.repo, entity.issue))
        if entry is None:
            return  # 404: already deleted, treated as already closed
        entry["state"] = "closed"

    def send(self, entity: Entity, message: Message) -> str:
        self._check_injected_error()
        if self._key(entity.repo, entity.issue) not in self._entities:
            raise ChannelNotFoundError(f"Channel '{entity.channel}' does not exist")
        self.deliver(entity, message)
        return message.id

    def _comments_for(self, entity: Entity, recipient: str | None) -> list[FakeComment]:
        return [
            comment
            for comment in self._comments
            if comment.message.channel == entity.channel
            and recipient_matches(comment.message, recipient)
        ]

    def check(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        self._check_injected_error()
        return any(
            comment_is_new(c.id, c.created_at, cursor)
            for c in self._comments_for(entity, recipient)
        )

    def read(self, entity: Entity, cursor: Cursor, *, recipient: str | None = None) -> ReadResult:
        self._check_injected_error()
        # The cursor tracks every comment on the entity (the backend's arrival
        # positions); `recipient` only decides which messages are returned.
        all_comments = self._comments_for(entity, None)
        in_scope = [c for c in all_comments if recipient_matches(c.message, recipient)]
        matched = [c for c in in_scope if comment_is_new(c.id, c.created_at, cursor)]
        matched.sort(key=lambda c: c.id)

        messages: list[Message] = []
        for comment in matched:
            setattr(comment.message, "received_at", comment.created_at)
            messages.append(comment.message)

        if cursor.position is None and cursor.at is not None:
            return ReadResult(messages, cursor)  # a `--since` read moves nothing
        return ReadResult(messages, advance_cursor(cursor, [(c.id, c.created_at) for c in all_comments]))
