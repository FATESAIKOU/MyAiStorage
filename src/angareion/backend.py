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


class PageUnavailableError(BackendError):
    """One page's listing answered 404, 410, or a 403 that is not a rate limit (design 542 §6.3, 2c).

    Only an older (retired) page is skipped on this; the current page raises it
    like any other BackendError, with the same message as before.
    """

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.status = status


class WriteRefusedError(BackendError):
    """POST 留言被拒（403／422）、不是 rate limit、也不是滿：確定沒有寫進去（設計 542 §1.3 W-4）."""

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.status = status


# Page limits (design 542 §1, §4). A page is one backend entity (one GitHub issue).
PAGE_WARN_AT = 2000
PAGE_ROLLOVER_AT = 2400
PAGE_FILL_LIMIT = 2490  # a retired page may still be written up to here (design 542 §1.3: 10 left for races)
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


def skipped_page_warning(channel: str, status: int) -> str:
    """The line for an older page that `read`/`log` skipped (design 542 C-22).

    No `Warning: ` prefix: the side that prints it adds one, so the text is the same everywhere.
    """
    return f"an older page of channel '{channel}' could not be read (HTTP {status}); skipped"


class ReadResult(NamedTuple):
    """What `read` returns: the new messages (oldest first), the new cursor, and warnings (skipped older pages)."""

    messages: list[Message]
    cursor: Cursor
    warnings: tuple[str, ...] = ()


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
class PageOpener(Protocol):
    """Open the next page of a channel (design 542 §1.3 S-4). Kept apart from PageBackend (A-5)."""

    def open_page(self, entity: Entity) -> int:
        """Open a new issue in `entity.repo` for the channel; return its number.

        `entity` is the current page. The new issue's title is the channel name and
        its body names the page it continues from. A failure raises BackendError
        whose message carries no issue number.
        """
        ...


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

