"""Backend protocol and in-memory fake backend for Angareion."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from angareion.messages import Message, parse_message


class BackendError(Exception):
    """Base exception for Angareion backend errors."""


class ChannelNotFoundError(BackendError):
    """Raised when a channel/issue is not found."""


class ChannelAlreadyExistsError(BackendError):
    """Raised when trying to create an existing channel when disallowed."""


class AuthenticationError(BackendError):
    """Raised when credentials/token are invalid (HTTP 401)."""


class RateLimitError(BackendError):
    """Raised when secondary rate limit is hit (HTTP 403 with Retry-After)."""

    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


class MessageListResult(tuple):
    """Result of list_messages, acting as (messages, etag) tuple with not_modified flag."""

    messages: list[Message]
    etag: str | None
    not_modified: bool

    def __new__(
        cls,
        messages: list[Message],
        etag: str | None = None,
        not_modified: bool = False,
    ):
        instance = super().__new__(cls, (messages, etag))
        instance.messages = messages
        instance.etag = etag
        instance.not_modified = not_modified
        return instance


@runtime_checkable
class Backend(Protocol):
    """Protocol for Angareion channel storage backends."""

    def create_channel(self, channel: str) -> int:
        """Create a new channel topic and return its issue number."""
        ...

    def find_channel(self, channel: str) -> int | None:
        """Find the issue number for a channel name, or None if not found."""
        ...

    def post_message(self, channel: str, message: Message) -> str:
        """Post a message to a channel. Returns comment URL or identifier."""
        ...

    def list_messages(
        self, since: str | None = None, etag: str | None = None
    ) -> MessageListResult:
        """List messages across the channel repository since a timestamp or with ETag."""
        ...


class FakeBackend:
    """In-memory backend implementation for tests and sidecar development."""

    def __init__(
        self,
        channels: dict[str, int] | None = None,
        messages: list[Message] | None = None,
    ):
        self._channels: dict[str, int] = dict(channels or {})
        self._messages: list[Message] = []
        self._etag_counter: int = 1
        self._next_issue_number: int = 1
        self.next_error: Exception | None = None

        if self._channels:
            self._next_issue_number = max(self._channels.values()) + 1

        if messages:
            for msg in messages:
                if msg.channel not in self._channels:
                    issue_num = getattr(msg, "issue_number", None) or self._next_issue_number
                    self._channels[msg.channel] = issue_num
                    if issue_num >= self._next_issue_number:
                        self._next_issue_number = issue_num + 1
                self.post_message(msg.channel, msg)

    def _check_injected_error(self) -> None:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err

    def create_channel(self, channel: str) -> int:
        """Create a new channel topic and return its issue number."""
        self._check_injected_error()
        if not channel or not isinstance(channel, str):
            raise ValueError("Channel name must be a non-empty string")
        if channel in self._channels:
            return self._channels[channel]
        issue_num = self._next_issue_number
        self._next_issue_number += 1
        self._channels[channel] = issue_num
        self._etag_counter += 1
        return issue_num

    def find_channel(self, channel: str) -> int | None:
        """Find the issue number for a channel name, or None if not found."""
        self._check_injected_error()
        return self._channels.get(channel)

    def list_channels(self) -> list[tuple[str, int]]:
        """List all known (channel_name, issue_number) pairs."""
        self._check_injected_error()
        return list(self._channels.items())

    def post_message(self, channel: str, message: Message | str) -> str:
        """Post a message to a channel. Returns comment URL or identifier."""
        self._check_injected_error()
        if channel not in self._channels:
            raise ChannelNotFoundError(f"Channel '{channel}' does not exist")

        if isinstance(message, str):
            parsed = parse_message(message, strict=True)
            if parsed is None:
                raise ValueError("Could not parse message text")
            msg_obj = parsed
        else:
            msg_obj = message

        msg_obj.validate()
        if msg_obj.channel != channel:
            raise ValueError(
                f"Message channel '{msg_obj.channel}' does not match target channel '{channel}'"
            )

        # Idempotency check: if message with this id already exists, do not duplicate
        for existing in self._messages:
            if existing.id == msg_obj.id:
                return getattr(
                    existing,
                    "comment_url",
                    f"https://github.com/fake/issues/{self._channels[channel]}#comment-{existing.id}",
                )

        issue_num = self._channels[channel]
        comment_id = len(self._messages) + 1
        comment_url = f"https://github.com/fake/issues/{issue_num}#comment-{comment_id}"
        setattr(msg_obj, "issue_number", issue_num)
        setattr(msg_obj, "comment_url", comment_url)

        self._messages.append(msg_obj)
        self._etag_counter += 1
        return comment_url

    def list_messages(
        self, since: str | None = None, etag: str | None = None
    ) -> MessageListResult:
        """List messages across the channel repository since a timestamp or with ETag."""
        self._check_injected_error()
        current_etag = f'W/"fake-etag-{self._etag_counter}"'

        # ETag 304 behavior: if etag matches, return not_modified with empty messages list
        if etag is not None and etag == current_etag:
            return MessageListResult([], current_etag, not_modified=True)

        if since is None:
            matched = list(self._messages)
        else:
            matched = [m for m in self._messages if m.at >= since]

        matched.sort(key=lambda m: (m.at, m.id))
        return MessageListResult(matched, current_etag, not_modified=False)
