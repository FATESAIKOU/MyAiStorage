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
from angareion.config import (
    Config,
    ConfigError,
    TokenError,
    TokenMissingError,
    TokenPermissionError,
    load_config,
    load_token,
    mask_token,
)
from angareion.github import GitHubBackend, HttpTransport, HttpResponse
from angareion.messages import Message

__version__ = "0.1.0"
__all__ = [
    "AuthenticationError",
    "Backend",
    "BackendError",
    "ChannelAlreadyExistsError",
    "ChannelNotFoundError",
    "Config",
    "ConfigError",
    "FakeBackend",
    "GitHubBackend",
    "HttpResponse",
    "HttpTransport",
    "Message",
    "MessageListResult",
    "RateLimitError",
    "TokenError",
    "TokenMissingError",
    "TokenPermissionError",
    "load_config",
    "load_token",
    "mask_token",
]
