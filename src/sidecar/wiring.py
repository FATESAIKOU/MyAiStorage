"""Default wiring for `sidecar run` (design D7).

The agent drivers (tasks 3.1/3.2) and the agora session store (task 3.3) are
not part of this change's section 2, so `default_drivers` and
`default_session_store` are the seams those tasks fill; until then the daemon
runs and `GET /agents` answers, but no agent kind can be opened.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .core import SidecarCore
from .protocols import ConfigError
from .selectors import PrioritySelector
from .sources import FileSubscriptionSource, connect_llmgateway


def build_core(config: Config) -> SidecarCore:
    return SidecarCore(
        drivers=default_drivers(config),
        store=default_session_store(config),
        subscriptions=build_subscription_source(config),
        selector=PrioritySelector(),
        resume_prompt=config.resume_prompt,
        retry_interval=config.retry_interval,
        poke_queue_timeout=config.poke_queue_timeout,
        close_timeout=config.close_timeout,
    )


def default_drivers(config: Config) -> dict[str, Any]:
    """Task 3.1 (ClaudeCodeDriver) and task 3.2 (OpenCodeDriver) land here."""
    return {}


def default_session_store(config: Config) -> Any | None:
    """Task 3.3 (AgoraSessionStore) lands here."""
    return None


def build_subscription_source(config: Config) -> Any | None:
    source = config.subscription_source
    if source is None:
        return None
    kind = source.get("kind")
    if kind == "file":
        path = source.get("path")
        if not isinstance(path, str) or not path:
            raise ConfigError("subscription_source kind=file needs a 'path'")
        return FileSubscriptionSource(path)
    if kind == "llmgateway":
        # The real connection comes from lgw's `connect` (the `gateway` extra);
        # without it every operation answers Unreachable with a clear message.
        return connect_llmgateway(config)
    raise ConfigError(f"unknown subscription_source kind {kind!r}")
