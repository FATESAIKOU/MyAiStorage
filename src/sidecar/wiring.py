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
from .drivers.opencode import OpenCodeDriver
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
        sync_period=config.sync_period * 60.0,  # `new` mode's import period (D5)
    )


def default_drivers(config: Config) -> dict[str, Any]:
    """The real drivers, built from the config's `agents` section."""
    claude = config.agents.get("claude-code")
    opencode = config.agents.get("opencode")
    return {
        "claude-code": ClaudeCodeDriver(
            command=(claude.command if claude is not None and claude.command else "claude"),
            extra_args=(claude.extra_args if claude is not None else ()),
        ),
        "opencode": OpenCodeDriver(
            command=(opencode.command if opencode is not None and opencode.command else "opencode"),
            extra_args=(opencode.extra_args if opencode is not None else ()),
            quota_reasons=config.quota_reasons,
            poll_timeout=config.close_timeout,  # the wait for the port is bounded (N2)
            http_timeout=config.close_timeout,
        ),
    }


def default_session_store(config: Config) -> Any | None:
    """The real store (task 3.0): spawns the configured `agora` command.

    The command is `config.agora_command` (default `agora` on PATH); sidecar
    never imports the agora package (step 2 of the MyLinuxPool move).
    `start_timeout` is the time the fixed stderr line gets; bounding it by
    close_timeout keeps a hung `agora continue` from wedging a tick (N2).
    (`agora merge` has its own budget: a summarizer round trip is not a
    `close_timeout` thing - see the report.)
    """
    return AgoraSessionStore(
        command=config.agora_command,
        sync_period=config.sync_period,
        start_timeout=config.close_timeout,
        import_timeout=config.close_timeout,
    )


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
