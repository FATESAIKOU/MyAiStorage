"""Angareion: Append-only, auditable agent-to-agent message channel."""

from __future__ import annotations

from angareion.backend import (
    AuthenticationError,
    Backend,
    BackendError,
    ChannelAlreadyExistsError,
    ChannelNotFoundError,
    FakeBackend,
    MessageListResult,
    RateLimitError,
)
from angareion.messages import Message

__version__ = "0.1.0"
__all__ = [
    "AuthenticationError",
    "Backend",
    "BackendError",
    "ChannelAlreadyExistsError",
    "ChannelNotFoundError",
    "FakeBackend",
    "Message",
    "MessageListResult",
    "RateLimitError",
]
