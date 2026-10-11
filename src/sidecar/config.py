"""sidecar configuration (design D7).

`~/.config/sidecar/config.json` (override with `SIDECAR_CONFIG`) holds the
port, the subscription source, the agent commands and the sync period.  No
secrets live here: LLMGateway credentials belong to its own connector, and a
local file source's `key` fields are only for development.
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


@dataclass
class AgentConfig:
    command: str | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    port: int = DEFAULT_PORT
    subscription_source: dict[str, Any] | None = None
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    sync_period: float = DEFAULT_SYNC_PERIOD
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

    sync_period = data.get("sync_period", DEFAULT_SYNC_PERIOD)
    if isinstance(sync_period, bool) or not isinstance(sync_period, (int, float)) or sync_period < 0:
        raise ConfigError("sync_period must be a non-negative number of minutes")

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
        agents=agents,
        sync_period=float(sync_period),
        path=config_path,
    )
