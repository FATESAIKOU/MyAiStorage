"""Library layer (D2/D4): `wait` polls the backend's `check`; `read` moves the cursor."""

from __future__ import annotations

import time
from typing import Any, Callable

from angareion.backend import ChannelBackend, Channel, Cursor, Target
from angareion.messages import Message
from angareion.state import CursorStore

POLL_INTERVAL_SECONDS = 15.0


class Angareion:
    """The four interfaces over a backend, with the local cursor for read/wait."""

    def __init__(self, backend: ChannelBackend, cursors: CursorStore):
        self.backend = backend
        self.cursors = cursors

    def create_channel(self, name: str, members: list[dict[str, Any]] | None = None) -> Channel:
        return self.backend.create_channel(name, list(members or []))

    def send(self, channel: str, message: Message) -> str:
        return self.backend.send(channel, message)

    def check(self, target: Target) -> bool:
        """Whether the backend has anything new for the target right now.

        Uses the local cursor but never writes it (the spec: waiting must not
        move the cursor). Any ETag used for conditional requests lives in the
        backend instance, for this process only.
        """
        return self.backend.check(target, self.cursors.cursor_for(target))

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

        `timeout=0` checks once and returns (non-blocking). A positive timeout
        waits at most that many seconds, polling every `poll_interval` seconds;
        `None` waits forever.
        """
        if timeout is not None and timeout <= 0:
            return self.check(target)

        deadline = None if timeout is None else clock() + timeout
        while True:
            if self.check(target):
                return True
            if deadline is None:
                sleeper(poll_interval)
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            sleeper(min(poll_interval, remaining))

    def read(self, target: Target, since: str | None = None) -> list[Message]:
        """Read new messages (oldest first).

        Without `since`, starts after the local cursor and advances it; with
        `since`, reads after that timestamp and leaves the local cursor alone.
        """
        use_local_cursor = since is None
        cursor = self.cursors.cursor_for(target) if use_local_cursor else Cursor(at=since)
        result = self.backend.read(target, cursor)
        if use_local_cursor:
            self.cursors.save(target, result.cursor)
        return result.messages
