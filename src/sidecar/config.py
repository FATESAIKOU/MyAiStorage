"""sidecar configuration (design D7).

`~/.config/sidecar/config.json` (override with `SIDECAR_CONFIG`) holds the
port, the subscription source, the LLMGateway connect-file path, the agent
commands and the timing knobs.  No secrets live here: LLMGateway credentials
belong to its own connector, and a local file source's `key` fields are only
for development.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .protocols import ConfigError

DEFAULT_PORT = 8471
DEFAULT_SYNC_PERIOD = 5.0
DEFAULT_RESUME_PROMPT = "額度恢復了，請接著剛才的工作"
DEFAULT_RETRY_INTERVAL = 300.0  # seconds
DEFAULT_POKE_QUEUE_TIMEOUT = 30.0  # seconds
DEFAULT_CLOSE_TIMEOUT = 30.0  # seconds
DEFAULT_QUOTA_REASONS = ("free_tier_limit",)


@dataclass
class AgentConfig:
    command: str | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    port: int = DEFAULT_PORT
    subscription_source: dict[str, Any] | None = None
    gateway_connect_file: Path = field(
        default_factory=lambda: Path.home() / ".config" / "sidecar" / "gateway-connect.json"
    )
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    sync_period: float = DEFAULT_SYNC_PERIOD
    resume_prompt: str = DEFAULT_RESUME_PROMPT
    retry_interval: float = DEFAULT_RETRY_INTERVAL
    poke_queue_timeout: float = DEFAULT_POKE_QUEUE_TIMEOUT
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT
    quota_reasons: tuple[str, ...] = DEFAULT_QUOTA_REASONS
    path: Path | None = None


def default_config_path() -> Path:
    override = os.environ.get("SIDECAR_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "sidecar" / "config.json"


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path is not None else default_config_path()
    if not config_path.exists():
        return Config(path=config_path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"{config_path} is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must hold a JSON object")

    port = data.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
        raise ConfigError("port must be an integer between 0 and 65535")

    source = data.get("subscription_source")
    if source is not None and not isinstance(source, dict):
        raise ConfigError("subscription_source must be an object")

    gateway_file = data.get("gateway_connect_file")
    if gateway_file is None:
        gateway_file = Path.home() / ".config" / "sidecar" / "gateway-connect.json"
    elif not isinstance(gateway_file, str) or not gateway_file:
        raise ConfigError("gateway_connect_file must be a path string")

    sync_period = _number(data, "sync_period", DEFAULT_SYNC_PERIOD, minimum=0)
    retry_interval = _number(data, "retry_interval", DEFAULT_RETRY_INTERVAL, minimum=1)
    poke_queue_timeout = _number(
        data, "poke_queue_timeout", DEFAULT_POKE_QUEUE_TIMEOUT, minimum=0.1
    )
    close_timeout = _number(data, "close_timeout", DEFAULT_CLOSE_TIMEOUT, minimum=0.1)

    resume_prompt = data.get("resume_prompt", DEFAULT_RESUME_PROMPT)
    if not isinstance(resume_prompt, str):
        raise ConfigError("resume_prompt must be a string (empty means: send nothing)")

    quota_reasons = data.get("quota_reasons", list(DEFAULT_QUOTA_REASONS))
    if not isinstance(quota_reasons, list) or not all(
        isinstance(reason, str) for reason in quota_reasons
    ):
        raise ConfigError("quota_reasons must be a list of strings")

    raw_agents = data.get("agents") or {}
    if not isinstance(raw_agents, dict):
        raise ConfigError("agents must be an object mapping kind to settings")
    agents: dict[str, AgentConfig] = {}
    for kind, spec in raw_agents.items():
        if isinstance(spec, str):
            agents[kind] = AgentConfig(command=spec)
        elif isinstance(spec, dict):
            extra = spec.get("extra_args") or []
            if not isinstance(extra, list) or not all(isinstance(arg, str) for arg in extra):
                raise ConfigError(f"agents.{kind}.extra_args must be a list of strings")
            agents[kind] = AgentConfig(command=spec.get("command"), extra_args=list(extra))
        else:
            raise ConfigError(f"agents.{kind} must be a command string or an object")

    return Config(
        port=port,
        subscription_source=source,
        gateway_connect_file=Path(gateway_file),
        agents=agents,
        sync_period=sync_period,
        resume_prompt=resume_prompt,
        retry_interval=retry_interval,
        poke_queue_timeout=poke_queue_timeout,
        close_timeout=close_timeout,
        quota_reasons=tuple(quota_reasons),
        path=config_path,
    )


def _number(data: dict[str, Any], key: str, default: float, *, minimum: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{key} must be a number >= {minimum}")
    return float(value)
