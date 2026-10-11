"""Default wiring for `sidecar run` (design D7).

The session store is real from task 3.0 (`agora continue --headless`) and the
drivers land per task: 3.1 registers `claude-code` (3.2 will add `opencode`).
An agent kind with no driver is refused with 400 `unsupported agent kind`
(design D8 / review L6).
"""

from __future__ import annotations

from typing import Any

from .agora_store import AgoraSessionStore
from .config import Config
from .core import SidecarCore
from .drivers.claude_code import ClaudeCodeDriver
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
    """The real drivers, built from the config's `agents` section."""
    claude = config.agents.get("claude-code")
    return {
        "claude-code": ClaudeCodeDriver(
            command=(claude.command if claude is not None and claude.command else "claude"),
            extra_args=(claude.extra_args if claude is not None else ()),
        ),
    }


def default_session_store(config: Config) -> Any | None:
    """The real store (task 3.0): spawns `agora continue --headless`."""
    return AgoraSessionStore(sync_period=config.sync_period)


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
