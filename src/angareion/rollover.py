"""Where a message goes on a paged channel, and how the next page is opened (design 542 §1.3, §6.4 3b).

`PageWriter.write` picks the page (the W table); `PageWriter.ensure_successor`
opens, verifies and registers the next page (the S table). The caller injects what
the writer needs from the local side (`backends`, `refresh`, `pending`, `warn`):
this module imports neither `channel_config` nor `state`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from angareion.backend import (
    PAGE_COMMENT_LIMIT,
    PAGE_FILL_LIMIT,
    PAGE_FILL_WINDOW,
    PAGE_ROLLOVER_AT,
    PAGE_WARN_AT,
    RETIRED_READ_GRACE,
    BackendError,
    BackendRef,
    ChannelBackend,
    ChannelFullError,
    Entity,
    PageBackend,
    PageOpener,
    RetiredPage,
    WriteRefusedError,
    page_entity,
    parse_backend_time,
)
from angareion.messages import Message
from angareion.registry import (
    Action,
    RegistryComments,
    RegistryError,
    parse_comment,
    post_with_retries,
    rollover_comment,
)
from angareion.registry_rules import rebuild_channels

# How many times a write may start over (a new entity, or a refresh) before it gives up (design 542 §1.3).
MAX_RESTARTS = 3

# SuccessorError.kind values (design 542 §1.3, S table)
REGISTRY_UNREADABLE = "registry-unreadable"  # S-1
MISMATCH = "mismatch"  # S-2
OPEN_FAILED = "open-failed"  # S-5
UNVERIFIED = "unverified"  # S-6, S-6′
UNREGISTERED = "unregistered"  # S-7
REJECTED = "rejected"  # S-8′, S-4′

REASONS = {
    REGISTRY_UNREADABLE: "could not read the channel registry",
    MISMATCH: "the local channel configuration does not match the registry",
    OPEN_FAILED: "could not open a new page",
    UNVERIFIED: "the new page could not be verified",
    UNREGISTERED: "opened a new page but could not register it",
    REJECTED: "the registry did not accept the new page",
}


class SuccessorError(BackendError):
    """`ensure_successor` could not give the channel a usable next page; `kind` says which step failed."""

    def __init__(self, kind: str, message: str | None = None):
        self.kind = kind
        self.reason = REASONS[kind]
        super().__init__(message or self.reason)


class PendingPages(Protocol):
    """The record of pages this identity opened but has not registered (design 542 §1.3).

    `state.PendingPageStore` has these three methods; tests use `MemoryPendingPages`.
    """

    def get(self, channel: str) -> tuple[str, int, int] | None:
        """(backend, issue, from issue) of the channel's record; None when there is none."""
        ...

    def put(self, channel: str, backend: str, issue: int, from_issue: int) -> None: ...

    def drop(self, channel: str) -> None: ...


class MemoryPendingPages:
    """An in-memory PendingPages, for tests (the state file is the real one)."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, int, int]] = {}

    def get(self, channel: str) -> tuple[str, int, int] | None:
        return self._records.get(channel)

    def put(self, channel: str, backend: str, issue: int, from_issue: int) -> None:
        self._records[channel] = (backend, issue, from_issue)

    def drop(self, channel: str) -> None:
        self._records.pop(channel, None)


def filling_warning(channel: str, count: int) -> str:
    """C-4's line: the current page is at or above PAGE_WARN_AT (the `Warning: ` prefix comes from `warn`)."""
    return f"channel '{channel}' has {count} of {PAGE_COMMENT_LIMIT} messages on its current page"


def prepare_failed_warning(channel: str, reason: str) -> str:
    return (
        f"channel '{channel}' is nearly full and its next page could not be prepared ({reason}); "
        "the message was written to the current page"
    )


def stuck_message(channel: str, reason: str) -> str:
    return (
        f"channel '{channel}' is full and its next page could not be prepared ({reason}); "
        f"try again later, or run 'angareion channel rollover {channel}'"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _restart(channel: str, restarts: int) -> int:
    """Count one start-over; the write gives up after MAX_RESTARTS of them."""
    restarts += 1
    if restarts > MAX_RESTARTS:
        raise BackendError(f"channel '{channel}': could not settle which page to write; try again later")
    return restarts


def _maybe_recent(now: datetime, until: str) -> bool:
    """Whether a retired page may still be inside its read grace by this machine's clock (L-2).

    An unreadable `until` counts as outside the window: the page is skipped, not an error.
    """
    try:
        ended = parse_backend_time(until)
    except ValueError:
        return False
    return now <= ended + RETIRED_READ_GRACE


def _in_fill_window(date: str | None, until: str) -> bool:
    """Whether the backend's own time (the `Date` header) is inside the fill window after `until` (W-1)."""
    if date is None:
        return False
    try:
        return parse_backend_time(date) - parse_backend_time(until) < PAGE_FILL_WINDOW
    except ValueError:
        return False


def _compatible(entry: dict[str, Any] | None, current: int, local: Entity) -> bool:
    """Whether the registry's mapping still agrees with the local configuration (S-2)."""
    if entry is None or entry["backend"] != local.backend:
        return False
    retired = {page["issue"] for page in entry.get("retired", [])}
    if current != entry["issue"] and current not in retired:
        return False
    return all(page.issue in retired for page in local.retired)


def _entity_from_mapping(local: Entity, entry: dict[str, Any]) -> Entity:
    """The channel as the registry's mapping says it is now (`repo` stays the local one: S-2 checked the backend)."""
    return Entity(
        channel=local.channel,
        backend=entry["backend"],
        repo=local.repo,
        issue=entry["issue"],
        retired=tuple(
            RetiredPage(issue=page["issue"], until=page["until"], side=bool(page.get("side", False)))
            for page in entry.get("retired", [])
        ),
    )


class PageWriter:
    """Writes one message to the right page of a channel and opens the next page when needed.

    `registry` must have `post_comment` and, to roll over, `all_comments` (C-17). A
    backend that is not a PageBackend gets plain sends and nothing else.
    """

    def __init__(
        self,
        backend: ChannelBackend,
        registry: Any,
        *,
        pending: PendingPages,
        backends: Callable[[], Mapping[str, BackendRef]],
        refresh: Callable[[str], Entity],
        warn: Callable[[str], None] = lambda text: None,
        now: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._pending = pending
        self._backends = backends
        self._refresh = refresh
        self._warn = warn
        self._now = now
        self._sleeper = sleeper

    def write(self, entity: Entity, message: Message) -> str:
        """Send `message` to the page the channel's state calls for; return the message id (design 542 §1.3, W table)."""
        if not isinstance(self._backend, PageBackend):
            return self._backend.send(entity, message)
        e, restarts, refreshed = entity, 0, False
        while True:
            # A: the last retired page that is not a side page, while it may still take writes.
            retired = next((p for p in reversed(e.retired) if not p.side), None)
            if retired is not None and _maybe_recent(self._now(), retired.until):
                page = page_entity(e, retired.issue)
                try:
                    n, date = self._backend.page_size_at(page)
                except BackendError:
                    n, date = None, None  # W-3
                if n is not None and _in_fill_window(date, retired.until) and n < PAGE_FILL_LIMIT:
                    try:
                        return self._backend.send(page, message)  # W-1
                    except (ChannelFullError, WriteRefusedError):
                        pass  # W-4: certainly not written, so try the current page
                    # any other BackendError is not caught (W-5)

            # B: the current page.
            current = page_entity(e, e.issue)
            try:
                n, _ = self._backend.page_size_at(current)
            except BackendError:
                n = None  # W-7
            prior: SuccessorError | None = None
            if n is not None and PAGE_ROLLOVER_AT <= n < PAGE_COMMENT_LIMIT:  # W-8
                try:
                    e = self.ensure_successor(e.channel, e.issue, e)
                    restarts = _restart(e.channel, restarts)
                    continue
                except SuccessorError as err:
                    prior = err
                    self._warn(prepare_failed_warning(e.channel, err.reason))
            if n is None or n < PAGE_COMMENT_LIMIT:
                if n is not None and n >= PAGE_WARN_AT:
                    self._warn(filling_warning(e.channel, n))  # C-20
                try:
                    return self._backend.send(current, message)  # W-6, W-7, W-8 failed, W-12
                except ChannelFullError:
                    pass  # W-10: same as the full page below
                # any other BackendError is not caught (W-11)

            # W-9 and W-10: the current page is full.
            if prior is None:
                try:
                    e = self.ensure_successor(e.channel, e.issue, e)
                    restarts = _restart(e.channel, restarts)
                    continue
                except SuccessorError as err:
                    prior = err
            if prior.kind == MISMATCH and not refreshed:
                refreshed = True
                e = self._refresh(e.channel)
                restarts = _restart(e.channel, restarts)
                continue
            if n is not None and n >= PAGE_COMMENT_LIMIT:
                self._warn(filling_warning(e.channel, n))  # keeps the C-4 line before the refusal
            raise SuccessorError(prior.kind, stuck_message(e.channel, prior.reason))

    def ensure_successor(self, channel: str, current: int, local: Entity, *, repost: bool = False) -> Entity:
        """Make sure `channel` has a page after `current` and return the channel as the registry now has it (S table).

        `repost` is for `channel rollover` only: it skips S-4′ and registers the page again.
        """
        if not isinstance(self._registry, RegistryComments):
            raise SuccessorError(REGISTRY_UNREADABLE)
        try:
            before = self._registry.all_comments()  # S-1
        except RegistryError:
            raise SuccessorError(REGISTRY_UNREADABLE)
        try:
            refs = dict(self._backends())
        except Exception:
            raise SuccessorError(MISMATCH)
        channels, _ = rebuild_channels(before, set(refs))
        entry = channels.get(channel)
        if local.backend not in refs or not _compatible(entry, current, local):
            raise SuccessorError(MISMATCH)  # S-2: nothing is opened
        assert entry is not None  # _compatible has rejected a missing entry

        if entry["issue"] != current:  # S-3: another writer already moved the channel
            self._pending.drop(channel)
            return _entity_from_mapping(local, entry)

        record = self._pending.get(channel)  # S-4
        if record is not None and record[0] == local.backend and record[2] == current:
            new = record[1]
            mine = Action("rollover", channel, local.backend, new, current)
            if not repost and any(parse_comment(c.get("body")) == mine for c in before):
                raise SuccessorError(REJECTED)  # S-4′: registered already, the listing still says it is not
        else:
            if not isinstance(self._backend, PageOpener):
                raise SuccessorError(OPEN_FAILED)
            try:
                new = self._backend.open_page(page_entity(local, current))  # S-5
            except BackendError:
                raise SuccessorError(OPEN_FAILED)
            self._pending.put(channel, local.backend, new, current)

        try:
            ok = self._backend.verify_channel(channel, refs[local.backend], new)  # S-6, S-6′
        except BackendError:
            raise SuccessorError(UNVERIFIED)  # the record stays: verify again next time
        if not ok:
            self._pending.drop(channel)
            raise SuccessorError(UNVERIFIED)

        try:
            own = post_with_retries(
                self._registry,
                rollover_comment(channel, local.backend, new, current),
                sleeper=self._sleeper,
            )  # S-7
        except RegistryError:
            raise SuccessorError(UNREGISTERED)  # the record stays: register the same page next time

        own = own if isinstance(own, dict) and "id" in own else None
        try:
            after = self._registry.all_comments()  # S-8
        except RegistryError:
            after = before  # S-9: the listing before the post, plus our own comment
        merged = [c for c in after if own is None or c.get("id") != own["id"]] + ([own] if own else [])
        channels, _ = rebuild_channels(merged, set(refs))
        entry = channels.get(channel)
        if entry is None or entry["issue"] == current:
            raise SuccessorError(REJECTED)  # S-8′: the record stays, and the next try does not post again
        self._pending.drop(channel)
        return _entity_from_mapping(local, entry)
