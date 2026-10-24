"""Library layer (D2/D4/v5): `wait` polls the backend's `check`; `read` moves the cursor.

"To me" spans every channel in the config file; each channel is a separate
backend entity with its own arrival positions, so each keeps its own cursor
entry (`to:<agent name>:<channel>`). "@ me" (`Target(mentioned=)`) spans them
too, with entries of its own (`mention:<agent name>:<channel>`). A channel
target uses one entry (`channel:<name>`).
"""

from __future__ import annotations

import time
from typing import Callable, NamedTuple

from angareion.backend import BackendRef, ChannelBackend, Cursor, Entity, EntityResolver, Target
from angareion.messages import Message
from angareion.state import CursorStore, channel_cursor_key, mention_cursor_key, recipient_cursor_key

POLL_INTERVAL_SECONDS = 15.0
# wait bounds each check's network budget to [5s, 30s]: slow networks still get
# a fair try, and a half-open connection never hangs a poll much past its time.
CHECK_TIMEOUT_FLOOR_SECONDS = 5.0
CHECK_TIMEOUT_CAP_SECONDS = 30.0


class ReadBatch(NamedTuple):
    """New messages plus `commit()`, which moves the local cursor.

    Reading and committing are two steps so the caller can print the messages
    first: if stdout breaks, the cursor stays where it was (L2) and the next
    read yields the messages again.
    """

    messages: list[Message]
    commit: Callable[[], None]


class Angareion:
    """The five interfaces over a backend, with the local cursors for read/wait."""

    def __init__(self, backend: ChannelBackend, cursors: CursorStore, store: EntityResolver, identity: str):
        self.backend = backend
        self.cursors = cursors
        self.store = store
        self.identity = identity

    def send(self, channel: str, message: Message) -> str:
        return self.backend.send(self.store.entity_for(channel), message)

    def create_channel(self, name: str, backend: BackendRef) -> int:
        """Step 1 of `channel create`: open the backend entity; returns its number."""
        return self.backend.create_channel(name, backend)

    def verify_channel(self, name: str, backend: BackendRef, entity: int) -> bool:
        """Step 2's check: the entity exists and its title is exactly the channel name."""
        return self.backend.verify_channel(name, backend, entity)

    def close_channel(self, entity: Entity) -> None:
        """`channel delete` step 2: close the entity without deleting it."""
        self.backend.close_channel(entity)

    def check(self, target: Target, *, timeout: float | None = None) -> bool:
        """Whether the backend has anything new for the target right now.

        Uses the local cursors but never writes them (the spec: waiting must
        not move the cursor). The backend keeps the conditional-request state
        (the CLI wires the file-backed cache, so it can outlive this process).
        `timeout` is an optional budget for the backend's own network requests.
        """
        if target.channel is not None:
            entity = self.store.entity_for(target.channel)
            cursor = self.cursors.cursor(channel_cursor_key(entity.channel))
            return self.backend.check(entity, cursor, timeout=timeout)

        for entity in self.store.entities():
            if target.mentioned is not None:
                cursor = self.cursors.cursor(mention_cursor_key(target.mentioned, entity.channel))
                found = self.backend.check(entity, cursor, mentioned=target.mentioned, timeout=timeout)
            else:
                cursor = self.cursors.cursor(recipient_cursor_key(self.identity, entity.channel))
                found = self.backend.check(entity, cursor, recipient=self.identity, timeout=timeout)
            if found:
                return True
        return False

    def wait(
        self,
        target: Target,
        timeout: float | None,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Block until the target has a new message; True, or False on timeout.

        `timeout=0` checks once and returns (non-blocking), with the backend's
        own default request timeout. A positive timeout waits at most that
        many seconds, polling every `poll_interval` seconds; `None` waits
        forever. Each check's network budget is min(30s, max(5s, remaining)),
        so a hung request fails after at most 30 seconds and a short remaining
        time still gets a fair 5 seconds.
        """
        if timeout is not None and timeout <= 0:
            return self.check(target)

        deadline = None if timeout is None else clock() + timeout
        while True:
            budget = None
            if deadline is not None:
                budget = min(CHECK_TIMEOUT_CAP_SECONDS, max(CHECK_TIMEOUT_FLOOR_SECONDS, deadline - clock()))
            if self.check(target, timeout=budget):
                return True
            if deadline is None:
                sleeper(poll_interval)
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            sleeper(min(poll_interval, remaining))

    def read(self, target: Target, since: str | None = None) -> ReadBatch:
        """Read new messages (oldest first) and return them with a commit.

        Without `since`, starts after the local cursor; `commit()` advances it.
        With `since`, reads after that timestamp and `commit()` does nothing.

        The caller must call `commit()` — and only after the messages have
        actually been delivered (printed and flushed) — so a broken stdout
        does not lose them.
        """
        use_local_cursor = since is None

        if target.channel is not None:
            entity = self.store.entity_for(target.channel)
            key = channel_cursor_key(entity.channel)
            cursor = self.cursors.cursor(key) if use_local_cursor else Cursor(at=since)
            result = self.backend.read(entity, cursor)

            def commit_channel() -> None:
                if use_local_cursor:
                    self.cursors.save(key, result.cursor)

            return ReadBatch(result.messages, commit_channel)

        messages: list[Message] = []
        updates: dict[str, Cursor] = {}
        for entity in self.store.entities():
            if target.mentioned is not None:
                key = mention_cursor_key(target.mentioned, entity.channel)
            else:
                key = recipient_cursor_key(self.identity, entity.channel)
            cursor = self.cursors.cursor(key) if use_local_cursor else Cursor(at=since)
            if target.mentioned is not None:
                result = self.backend.read(entity, cursor, mentioned=target.mentioned)
            else:
                result = self.backend.read(entity, cursor, recipient=self.identity)
            messages.extend(result.messages)
            if use_local_cursor:
                updates[key] = result.cursor

        # Oldest first across channels: by the backend time, then channel/name.
        messages.sort(key=lambda m: (getattr(m, "received_at", "") or "", m.channel, m.id))

        def commit_recipient() -> None:
            if updates:
                self.cursors.save_many(updates)

        return ReadBatch(messages, commit_recipient)
