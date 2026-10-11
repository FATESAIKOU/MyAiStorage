"""ChannelBackend protocol (the four operations), targets, cursors and the fake backend (D2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, runtime_checkable

from angareion.config import identity_slug
from angareion.messages import Message


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
    reader's identity {repo, role, [name]}; "to me" spans all channels.
    """

    channel: str | None = None
    recipient: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if (self.channel is None) == (self.recipient is None):
            raise ValueError("Target needs exactly one of channel or recipient")

    def key(self) -> str:
        """Stable key for this target in the local cursor file."""
        if self.channel is not None:
            return f"channel:{self.channel}"
        return "to:" + identity_slug(self.recipient or {})


@dataclass
class Cursor:
    """How far a target has been read: the last read message's (at, id).

    The pair is the boundary: messages strictly after it are new. The ULID
    breaks ties inside the same second (GitHub's `since` is second-granular
    and inclusive), and keeps an edited already-read comment from coming back.
    """

    at: str | None = None
    id: str | None = None


def is_message_after_cursor(message: Message, cursor: Cursor) -> bool:
    """Whether `message` lies strictly after the cursor boundary.

    With no `at`, everything is new. With an `at` but no `id` (a `--since`
    timestamp), only messages strictly after that second are new.
    """
    if cursor.at is None:
        return True
    if message.at != cursor.at:
        return message.at > cursor.at
    if cursor.id is None:
        return False
    return message.id > cursor.id


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

    def check(self, target: Target, cursor: Cursor) -> bool:
        """Whether there is any message after the cursor for the target."""
        ...

    def read(self, target: Target, cursor: Cursor) -> ReadResult:
        """Read the messages after the cursor (oldest first), and the new cursor."""
        ...


def address_matches_identity(address: Any, recipient: dict[str, Any]) -> bool:
    """Whether a message's `to` is this member (name is display-only)."""
    if not isinstance(address, dict) or "group" in address:
        return False
    return address.get("repo") == recipient.get("repo") and address.get("role") == recipient.get("role")


class FakeBackend:
    """In-memory ChannelBackend for tests and local development; never touches the network."""

    def __init__(
        self,
        channels: dict[str, int] | None = None,
        messages: list[Message] | None = None,
        members: dict[str, list[dict[str, Any]]] | None = None,
    ):
        self._channels: dict[str, Channel] = {}
        self._messages: list[Message] = []
        self._next_number = 1
        self.next_error: Exception | None = None

        for name, number in (channels or {}).items():
            self._channels[name] = Channel(name=name, number=number, members=list((members or {}).get(name, [])))
            if number >= self._next_number:
                self._next_number = number + 1

        for msg in messages or []:
            if msg.channel not in self._channels:
                self.create_channel(msg.channel, [])
            self.send(msg.channel, msg)

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
        channel = Channel(name=name, number=self._next_number, members=list(members))
        self._next_number += 1
        self._channels[name] = channel
        return channel

    def send(self, channel: str, message: Message) -> str:
        self._check_injected_error()
        if channel not in self._channels:
            raise ChannelNotFoundError(f"Channel '{channel}' does not exist")
        message.validate()
        if message.channel != channel:
            raise ValueError(
                f"Message channel '{message.channel}' does not match target channel '{channel}'"
            )
        self._messages.append(message)
        return message.id

    def _messages_for(self, target: Target) -> list[Message]:
        if target.channel is not None:
            if target.channel not in self._channels:
                raise ChannelNotFoundError(f"Channel '{target.channel}' does not exist")
            return [m for m in self._messages if m.channel == target.channel]
        recipient = target.recipient or {}
        return [m for m in self._messages if address_matches_identity(m.to, recipient)]

    def check(self, target: Target, cursor: Cursor) -> bool:
        self._check_injected_error()
        return any(is_message_after_cursor(m, cursor) for m in self._messages_for(target))

    def read(self, target: Target, cursor: Cursor) -> ReadResult:
        self._check_injected_error()
        matched = [m for m in self._messages_for(target) if is_message_after_cursor(m, cursor)]
        matched.sort(key=lambda m: (m.at, m.id))
        if matched:
            last = matched[-1]
            new_cursor = Cursor(at=last.at, id=last.id)
        else:
            new_cursor = Cursor(at=cursor.at, id=cursor.id)
        return ReadResult(matched, new_cursor)
