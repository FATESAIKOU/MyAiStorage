"""Fake backend for tests and local development only; production code must not import it (D-35)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from angareion.backend import (
    PAGE_COMMENT_LIMIT,
    BackendError,
    BackendRef,
    ChannelFullError,
    ChannelNotFoundError,
    Cursor,
    Entity,
    EntityResolver,
    PageUnavailableError,
    ReadResult,
    comment_is_new,
    cursor_after_read,
    format_backend_time,
    new_reader_counts,
    page_base,
    pages_to_read,
    parse_backend_time,
    scope_matches,
    skipped_page_warning,
)
from angareion.messages import Message


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
        page_date: Callable[[], str | None] | None = None,
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
        # Reads of a page raise this error as set by `set_page_error` (design 542 C-22).
        self._page_errors: dict[tuple[str, int], BackendError] = {}
        self.comment_limit = comment_limit
        # The `Date` a page size answers with; None uses the clock, as before.
        self._page_date = page_date
        # Sends to one page raise this error, as set by `set_send_error` (design 542 W-4, W-5, W-10, W-11).
        self._send_errors: dict[tuple[str, int], BackendError] = {}
        self.next_open_error: Exception | None = None
        # The numbers of the pages `open_page` opened, in order.
        self.opened: list[int] = []
        # Every `page_size_at` call, by issue number (a `page_size` call counts too).
        self.size_calls: list[int] = []
        # Guards create_channel, open_page and deliver: two writers may share one fake (design 542 C-18).
        self._lock = threading.RLock()

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

    def set_send_error(self, number: int, error: BackendError | None, *, repo: str = "owner/repo") -> None:
        """Make every send to one page raise `error`; None clears it (design 542 W-4, W-5, W-10, W-11)."""
        key = self._key(repo, number)
        if error is None:
            self._send_errors.pop(key, None)
        else:
            self._send_errors[key] = error

    def set_page_error(self, number: int, error: BackendError | None, *, repo: str = "owner/repo") -> None:
        """Make reads, checks and listings of one page raise `error`; None clears it (design 542 C-22)."""
        key = self._key(repo, number)
        if error is None:
            self._page_errors.pop(key, None)
        else:
            self._page_errors[key] = error

    def _check_page_error(self, entity: Entity) -> None:
        error = self._page_errors.get(self._key(entity.repo, entity.issue))
        if error is not None:
            raise error

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
        with self._lock:
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
        with self._lock:
            number = self._next_entity_id
            self._next_entity_id += 1
            self._entities[self._key(backend.repo, number)] = {"title": name, "state": "open"}
        return number

    def open_page(self, entity: Entity) -> int:
        """Open the next page of a channel, as `PageOpener` does (design 542 S-4); its title is the channel."""
        self._check_injected_error()
        if self.next_open_error is not None:
            err = self.next_open_error
            self.next_open_error = None
            raise err
        with self._lock:
            number = self._next_entity_id
            self._next_entity_id += 1
            self._entities[self._key(entity.repo, number)] = {
                "title": entity.channel,
                "state": "open",
                "body": f"continued from #{entity.issue}",
            }
            self.opened.append(number)
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
        self.size_calls.append(entity.issue)
        self._check_injected_error()
        date = self._page_date() if self._page_date is not None else self._clock()
        return self._current_size(entity), date

    def _current_size(self, entity: Entity) -> int:
        """The page's size as a test set it, else its actual count (not recorded in `size_calls`)."""
        override = self._page_sizes.get(self._key(entity.repo, entity.issue))
        if override is not None:
            return override
        return len(self._comments_for(entity, None))

    def send(self, entity: Entity, message: Message) -> str:
        self._check_injected_error()
        key = self._key(entity.repo, entity.issue)
        if key not in self._entities:
            raise ChannelNotFoundError(f"Channel '{entity.channel}' does not exist")
        if key in self._send_errors:
            raise self._send_errors[key]
        count = self._current_size(entity)
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

    def _page_comments(self, page: Entity) -> list[FakeComment]:
        """Every comment on one page of a channel, after the page's injected error (if any)."""
        self._check_page_error(page)
        return self._comments_for(page, None)

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
        for page in pages_to_read(entity, cursor):
            try:
                self._check_page_error(page)
            except PageUnavailableError:
                if page.issue == entity.issue:
                    raise
                continue  # an older page that cannot be listed is skipped (C-22)
            comments = self._comments_for(page, recipient, mentioned)
            base = page_base(cursor, page.issue)
            if any(comment_is_new(c.id, c.created_at, base) for c in comments):
                return True
        return False

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
        # #224: the backend's own "now" is the floor for a new identity's
        # broadcasts; the fake answers with its injected clock, like the real
        # backend answers with the response's Date header.
        floor = self._clock() if new_recipient and recipient is not None and mentioned is None else None
        pages = pages_to_read(entity, cursor)
        warnings: list[str] = []
        entries_by_page: dict[int, list[tuple[int, str]]] = {}
        matched: list[FakeComment] = []
        for page in pages:
            try:
                # The cursor tracks every comment on the page (the backend's arrival
                # positions); `recipient`/`mentioned` only decide which are returned.
                all_comments = self._page_comments(page)
            except PageUnavailableError as error:
                if page.issue == entity.issue:
                    raise
                warnings.append(skipped_page_warning(entity.channel, error.status))
                continue
            entries_by_page[page.issue] = [(c.id, c.created_at) for c in all_comments]
            base = page_base(cursor, page.issue)
            matched.extend(
                c
                for c in all_comments
                if scope_matches(c.message, recipient, mentioned)
                and new_reader_counts(c.message, c.created_at, floor, recipient)
                and comment_is_new(c.id, c.created_at, base)
            )
        matched.sort(key=lambda c: c.id)

        messages: list[Message] = []
        for comment in matched:
            setattr(comment.message, "received_at", comment.created_at)
            messages.append(comment.message)

        if cursor.position is None and cursor.at is not None:
            return ReadResult(messages, cursor, tuple(warnings))  # a `--since` read moves nothing
        new_cursor = cursor_after_read(cursor, [page.issue for page in pages], entries_by_page)
        return ReadResult(messages, new_cursor, tuple(warnings))

    # -- enumeration (read-only listing for `log`) -----------------------
    def enumerate(self, entity, since=None, etag=None):
        return self.enumerate_conditional(entity, since=since, etag=etag)[0]

    def enumerate_conditional(self, entity, *, since=None, etag=None):
        self._check_page_error(entity)
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
