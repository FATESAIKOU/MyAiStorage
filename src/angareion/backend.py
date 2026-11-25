"""ChannelBackend protocol (create/close/send/check/read), EnumerableBackend protocol (enumerate_conditional), targets, cursors, fake backend.

v5: a channel is a name that a config file points at a backend entity; the
backend never stores members. `create_channel` opens the entity, `close_channel`
closes it (without deleting it), `send`/`check`/`read` work on a resolved
`Entity`. "To me" spans every channel, so `check`/`read` take an optional
recipient filter; a message without `to` is a broadcast and counts for everyone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Protocol, runtime_checkable

from angareion import messages as _messages
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


class RateLimitError(BackendError):
    """The backend refused the request because of a rate limit (HTTP 403/429); not retried.

    `kind` is "primary" (the hourly quota is used up; `reset_at` is the UTC
    time it resets) or "secondary" (abuse protection; `retry_after` is the
    number of seconds to wait, when the backend said so).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        retry_after: int | None = None,
        reset_at: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.reset_at = reset_at


# Page limits (design 542 §1, §4). A page is one backend entity (one GitHub issue).
PAGE_WARN_AT = 2000
PAGE_ROLLOVER_AT = 2400
PAGE_COMMENT_LIMIT = 2500

# A retired page stays readable this long after it was retired (design 542 §1.3, §2.1).
RETIRED_READ_GRACE = timedelta(hours=48)
# How long after a rollover a writer may still write the old page (design 542 §1.3, Q-2).
PAGE_FILL_WINDOW = timedelta(hours=24)


def page_level(count: int | None) -> str:
    """The level of a page's message count: ok, warn, rollover, full (or unknown)."""
    if count is None:
        return "unknown"
    if count >= PAGE_COMMENT_LIMIT:
        return "full"
    if count >= PAGE_ROLLOVER_AT:
        return "rollover"
    if count >= PAGE_WARN_AT:
        return "warn"
    return "ok"


class ChannelFullError(BackendError):
    """The current page of a channel is full, or the backend refused the write and it looks full.

    `presumed` is False when the page count itself is at the limit (is full) and
    True when the count is in the rollover band (looks full: the refusal may
    also be a token problem). The message carries the HTTP status and the
    backend's own message, already cleaned by the backend.
    """

    def __init__(
        self,
        channel: str,
        count: int,
        *,
        presumed: bool,
        status: int | None = None,
        detail: str = "",
    ):
        self.channel = channel
        self.count = count
        self.presumed = presumed
        where = f"its current page has {count} of {PAGE_COMMENT_LIMIT} messages"
        if presumed:
            head = f"channel '{channel}' looks full (or the token was refused)"
        else:
            head = f"channel '{channel}' is full"
        cause = http_cause(status, detail)
        super().__init__(f"{head}: {where} and the backend refused the write{cause}")


def http_cause(status: int | None, detail: str) -> str:
    """` (HTTP <code>: <message>)`, dropping whichever part is missing."""
    parts = []
    if status is not None:
        parts.append(f"HTTP {status}")
    if detail:
        parts.append(detail)
    return f" ({': '.join(parts)})" if parts else ""


@dataclass(frozen=True)
class BackendRef:
    """One backends.json entry: a named backend and where its entities live."""

    name: str
    type: str
    repo: str


@dataclass(frozen=True)
class RetiredPage:
    """A page a channel has moved off (channels.json `retired`, design 542 §1.1).

    `until` is the RFC 3339 time it was retired, as written in channels.json;
    compare it with `parse_backend_time`. `side` marks a page that was opened
    by a race and never became the current page.
    """

    issue: int
    until: str
    side: bool = False


@dataclass(frozen=True)
class Entity:
    """A resolved channel: the backend entity a command may act on.

    `issue` is the current page; `retired` are the pages it moved off, oldest first.
    """

    channel: str
    backend: str
    repo: str
    issue: int
    retired: tuple[RetiredPage, ...] = ()


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
    """Where to check/read: one channel, everything addressed to me, or everything that @-s me.

    Exactly one of `channel`, `recipient` and `mentioned` must be set.
    `recipient` is the reader's agent name; "to me" spans all channels and
    counts a message addressed to me, a broadcast, or one that mentions me.
    `mentioned` is the reader's agent name for "@ me": `to` is me or `mentions`
    has me, a broadcast does not count; it spans all channels too.
    """

    channel: str | None = None
    recipient: str | None = None
    mentioned: str | None = None

    def __post_init__(self) -> None:
        if sum(value is not None for value in (self.channel, self.recipient, self.mentioned)) != 1:
            raise ValueError("Target needs exactly one of channel, recipient or mentioned")

    def key(self) -> str:
        """Stable key for this target in the local cursor file."""
        if self.channel is not None:
            return f"channel:{self.channel}"
        if self.mentioned is not None:
            return "mention:" + identity_slug(self.mentioned)
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

    `pages` (design 542 §2.1) is each page's own cursor, keyed by issue number;
    None means the entry has none (an old file, or a new cursor). A page's own
    cursor never has `pages` of its own.
    """

    position: str | None = None
    at: str | None = None
    seen: list[str] = field(default_factory=list)
    pages: dict[int, Cursor] | None = None


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


def _plain(cursor: Cursor) -> Cursor:
    """A copy of a cursor's three columns, without its per-page map (no shared lists)."""
    return Cursor(position=cursor.position, at=cursor.at, seen=list(cursor.seen))


def page_entity(entity: Entity, issue: int) -> Entity:
    """The same channel on one of its pages, with no retired pages of its own."""
    return replace(entity, issue=issue, retired=())


def pages_to_read(entity: Entity, cursor: Cursor) -> tuple[Entity, ...]:
    """The pages a read of this channel covers, in reading order (design 542 §2.1, C-9).

    Retired pages come first (oldest first), the current page last; it is always read.
    A retired page is read while the cursor's time is inside its grace period,
    `cursor.at < until + RETIRED_READ_GRACE`. A new cursor (no `at`) instead
    reads only the pages retired within the grace period of the latest
    rollover (`until + RETIRED_READ_GRACE > U`, U the latest `until`), so the
    pages read do not grow with the number of rollovers (A-12).
    """
    latest = max((parse_backend_time(r.until) for r in entity.retired), default=None)
    pages: list[Entity] = []
    for retired in entity.retired:
        until = parse_backend_time(retired.until) + RETIRED_READ_GRACE
        if cursor.at is None:
            wanted = latest is not None and until > latest
        else:
            wanted = parse_backend_time(cursor.at) < until
        if wanted:
            pages.append(page_entity(entity, retired.issue))
    pages.append(page_entity(entity, entity.issue))
    return tuple(pages)


def page_base(cursor: Cursor, issue: int) -> Cursor:
    """Where reading one page starts (design 542 §2.1, C-10).

    `pages` is None: the main cursor is the start for every page (old files, new
    cursors). `pages` holds the page: that page's own cursor. `pages` does not
    hold it: the page was never read, so it starts empty and is read from the start.
    """
    if cursor.pages is None:
        return _plain(cursor)
    own = cursor.pages.get(issue)
    return _plain(own) if own is not None else Cursor()


def pages_after_read(
    prev: Cursor,
    to_read: Sequence[int],
    entries_by_page: Mapping[int, list[tuple[int, str]]],
) -> dict[int, Cursor]:
    """Each page's cursor after a read (design 542 §2.1, C-10).

    A page read this time advances on its own entries. A page that was skipped
    (missing from `entries_by_page`) keeps its place: its cursor does not move,
    and a page never read stays empty (read from the start later).
    """
    return {
        page: advance_cursor(page_base(prev, page), entries_by_page.get(page, []))
        for page in to_read
    }


def cursor_after_read(
    prev: Cursor,
    to_read: Sequence[int],
    entries_by_page: Mapping[int, list[tuple[int, str]]],
) -> Cursor:
    """The cursor after a read across pages (design 542 §2.1, C-10).

    The main cursor advances on the entries of all pages read together; its
    `pages` is `pages_after_read`.
    """
    merged = [entry for page in to_read for entry in entries_by_page.get(page, [])]
    return replace(
        advance_cursor(_plain(prev), merged),
        pages=pages_after_read(prev, to_read, entries_by_page),
    )


class ReadResult(NamedTuple):
    """What `read` returns: the new messages (oldest first) and the new cursor."""

    messages: list[Message]
    cursor: Cursor


class ETagRecord(NamedTuple):
    """A conditional-request state that was fully scanned and found clean (D2/#96).

    `url` is the exact request URL the response came from (its query includes
    `since`), `cursor` is the cursor it was checked against, and `etag` is the
    backend's validator. It is sent back as `If-None-Match` only when all three
    still match; see `github.GitHubIssuesBackend.check`.
    """

    url: str
    cursor: Cursor
    etag: str


@runtime_checkable
class ETagCache(Protocol):
    """Remembers the last clean conditional-request state per target (D2/#96).

    `GitHubIssuesBackend.check` reads, stores and drops records through this;
    the CLI wires the file-backed cache so a restarted `wait` sends
    `If-None-Match`. It is only a cache: a missing or unusable entry means a
    full query.
    """

    def get(self, key: str) -> ETagRecord | None:
        ...

    def put(self, key: str, record: ETagRecord) -> None:
        ...

    def drop(self, key: str) -> None:
        ...


class MemoryETagCache:
    """In-process ETagCache: today's behaviour when no state directory is wired."""

    def __init__(self) -> None:
        self._records: dict[str, ETagRecord] = {}

    def get(self, key: str) -> ETagRecord | None:
        return self._records.get(key)

    def put(self, key: str, record: ETagRecord) -> None:
        self._records[key] = record

    def drop(self, key: str) -> None:
        self._records.pop(key, None)


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
        mentioned: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """Whether there is any new message for the entity right now.

        `recipient` filters "to me" (messages whose `to` is that agent name,
        that mention it, or that have no `to` at all); `mentioned` filters
        "@ me" (`to` is that agent name or it mentions it, broadcasts do not
        count); None for both means every message in the channel.
        `timeout` is an optional budget in seconds for the backend's own
        network requests (the wait loop passes its remaining time).
        """
        ...

    def read(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        mentioned: str | None = None,
        new_recipient: bool = False,
    ) -> ReadResult:
        """Read the messages after the cursor (oldest first), and the new cursor.

        `new_recipient` marks the reader's first "to me" read (it has no "to me"
        cursor at all yet): messages addressed to it read from the start, a
        plain broadcast only from the backend's current time (#224).
        """
        ...


@runtime_checkable
class PageBackend(Protocol):
    """Read-only page sizes: how many messages the current page holds (design 542 C-3).

    `page_size_at` also gives the backend's own time (the `Date` header, RFC 3339
    UTC, or None) so the caller can judge a window without this machine's clock.
    Backends without these methods are simply not checked for page sizes.
    """

    def page_size(self, entity: Entity) -> int: ...

    def page_size_at(self, entity: Entity) -> tuple[int, str | None]: ...


@runtime_checkable
class EnumerableBackend(Protocol):
    """Read-only listing of a channel's whole history, for the local log mirror (#468).

    Kept apart from `ChannelBackend` (which stays six operations): listing is
    what `log` and the log mirror need, not what posting and reading need.
    Returns `(items, new_etag, not_modified)`, items as `(id, created_at, parsed)`
    in the backend's arrival order; `parsed` is None for a comment that is not a message.
    """

    def enumerate_conditional(
        self,
        entity: Entity,
        *,
        since: str | None = None,
        etag: Any = None,
    ) -> tuple[list[tuple[Any, str, Message | None]], Any, bool]:
        ...


def _silent_to_its_writer(message: Message, reader: str | None) -> bool:
    """Whether a message of a datatype its writer does not want back is the reader's own.

    The table is read from `messages` on every call (not copied in here), so the
    delivery layer names no datatype itself (#468, C-3).
    """
    return (
        reader is not None
        and message.datatype in _messages.SELF_SILENT_DATATYPES
        and message.from_ == reader
    )


def recipient_matches(message: Message, recipient: str | None) -> bool:
    """Whether a message counts as "to me": addressed to me, a broadcast, or it mentions me."""
    if recipient is None:
        return True
    if _silent_to_its_writer(message, recipient):
        return False
    return message.to is None or message.to == recipient or recipient in message.mentions


def mention_matches(message: Message, mentioned: str | None) -> bool:
    """Whether a message counts as "@ me": addressed to me or it mentions me; a broadcast does not."""
    if mentioned is None:
        return True
    if _silent_to_its_writer(message, mentioned):
        return False
    return message.to == mentioned or mentioned in message.mentions


def scope_matches(message: Message, recipient: str | None, mentioned: str | None) -> bool:
    """The reader's filter: "to me" (`recipient`), "@ me" (`mentioned`), or neither (all)."""
    return recipient_matches(message, recipient) and mention_matches(message, mentioned)


def new_reader_counts(
    message: Message, created_at: str, floor: str | None, recipient: str | None
) -> bool:
    """Whether a message counts on a new identity's first "to me" read (#224).

    A new identity (no "to me" cursor yet) still reads everything
    addressed to it from the start — `to` is its own name, or the message
    mentions it — so a dispatch sent before it opened is never missed. A plain
    broadcast (no `to` and it does not mention the reader) only counts from
    `floor`: the backend's own time at that first read, inclusive (the backend
    decides what is old; D4). Inclusive because the backend time has one-second
    resolution: a broadcast posted in the same second as the first read would
    otherwise be lost for good.

    `floor` is None when the backend gave no time, or when this is not a
    reader's first "to me" read: then every broadcast counts, the behaviour
    before #224.
    """
    if floor is None or recipient is None or message.to is not None:
        return True
    if recipient in message.mentions:
        return True
    return parse_backend_time(created_at) >= parse_backend_time(floor)


@dataclass(frozen=True)
class FakeComment:
    """A comment as the fake backend stores it: arrival id and backend time.

    `page` is the (repo, issue) it was posted on; None (a comment added to the
    list directly) is on every page of its channel.
    """

    id: int
    created_at: str
    message: Message
    page: tuple[str, int] | None = None


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
        comment_limit: int = PAGE_COMMENT_LIMIT,
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
        # A page's size as a test set it (`set_page_size`); absent, the actual count is used.
        self._page_sizes: dict[tuple[str, int], int] = {}
        self.comment_limit = comment_limit

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

    def set_page_size(self, number: int, n: int, *, repo: str = "owner/repo") -> None:
        """Pin the page size an entity reports (instead of its real comment count)."""
        self._page_sizes[self._key(repo, number)] = n

    def deliver(
        self,
        entity: Entity,
        message: Message,
        *,
        comment_id: int | None = None,
        created_at: str | None = None,
        page: int | None = None,
    ) -> FakeComment:
        """Record a message as a backend comment; tests may pin id/created_at.

        The comment goes on the entity's page; `page` posts it on another page
        of the same repo instead (design 542 C-15).
        """
        self._check_injected_error()
        message.validate()
        if message.channel != entity.channel:
            raise ValueError(
                f"Message channel '{message.channel}' does not match target channel '{entity.channel}'"
            )
        cid = self._next_comment_id if comment_id is None else comment_id
        if cid >= self._next_comment_id:
            self._next_comment_id = cid + 1
        on_page = self._key(entity.repo, entity.issue if page is None else page)
        comment = FakeComment(
            id=cid, created_at=created_at or self._clock(), message=message, page=on_page
        )
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

    def page_size(self, entity: Entity) -> int:
        return self.page_size_at(entity)[0]

    def page_size_at(self, entity: Entity) -> tuple[int, str | None]:
        self._check_injected_error()
        override = self._page_sizes.get(self._key(entity.repo, entity.issue))
        if override is not None:
            return override, self._clock()
        return len(self._comments_for(entity, None)), self._clock()

    def send(self, entity: Entity, message: Message) -> str:
        self._check_injected_error()
        if self._key(entity.repo, entity.issue) not in self._entities:
            raise ChannelNotFoundError(f"Channel '{entity.channel}' does not exist")
        count = self.page_size(entity)
        if count >= self.comment_limit:
            raise ChannelFullError(entity.channel, count, presumed=False)
        self.deliver(entity, message)
        return message.id

    def _on_page(self, comment: FakeComment, entity: Entity) -> bool:
        """Whether a comment is on the entity's page (untagged comments are on every page)."""
        return comment.page is None or comment.page == self._key(entity.repo, entity.issue)

    def _comments_for(
        self, entity: Entity, recipient: str | None, mentioned: str | None = None
    ) -> list[FakeComment]:
        return [
            comment
            for comment in self._comments
            if comment.message.channel == entity.channel
            and self._on_page(comment, entity)
            and scope_matches(comment.message, recipient, mentioned)
        ]

    def check(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        mentioned: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        self._check_injected_error()
        return any(
            comment_is_new(c.id, c.created_at, cursor)
            for c in self._comments_for(entity, recipient, mentioned)
        )

    def read(
        self,
        entity: Entity,
        cursor: Cursor,
        *,
        recipient: str | None = None,
        mentioned: str | None = None,
        new_recipient: bool = False,
    ) -> ReadResult:
        self._check_injected_error()
        # The cursor tracks every comment on the entity (the backend's arrival
        # positions); `recipient`/`mentioned` only decide which messages are returned.
        all_comments = self._comments_for(entity, None)
        # #224: the backend's own "now" is the floor for a new identity's
        # broadcasts; the fake answers with its injected clock, like the real
        # backend answers with the response's Date header.
        floor = self._clock() if new_recipient and recipient is not None and mentioned is None else None
        in_scope = [
            c
            for c in all_comments
            if scope_matches(c.message, recipient, mentioned)
            and new_reader_counts(c.message, c.created_at, floor, recipient)
        ]
        matched = [c for c in in_scope if comment_is_new(c.id, c.created_at, cursor)]
        matched.sort(key=lambda c: c.id)

        messages: list[Message] = []
        for comment in matched:
            setattr(comment.message, "received_at", comment.created_at)
            messages.append(comment.message)

        if cursor.position is None and cursor.at is not None:
            return ReadResult(messages, cursor)  # a `--since` read moves nothing
        return ReadResult(messages, advance_cursor(cursor, [(c.id, c.created_at) for c in all_comments]))


    # -- enumeration (read-only listing for `log`) -----------------------
    def enumerate(self, entity, since=None, etag=None):
        return self.enumerate_conditional(entity, since=since, etag=etag)[0]

    def enumerate_conditional(self, entity, *, since=None, etag=None):
        items = []
        for comment in self._comments:
            msg = comment.message
            if msg is not None and getattr(msg, "channel", None) != entity.channel:
                continue
            if not self._on_page(comment, entity):
                continue
            if since is not None:
                try:
                    if parse_backend_time(comment.created_at) < parse_backend_time(since):
                        continue
                except Exception:
                    pass
            parsed = msg if isinstance(msg, Message) else None
            # non-angareion: if msg is not a Message, parsed=None
            if not isinstance(msg, Message):
                parsed = None
            items.append((comment.id, comment.created_at, parsed))
        items.sort(key=lambda x: x[0])
        return items, None, False
