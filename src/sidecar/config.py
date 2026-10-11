"""Configuration for agent-sidecar."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def get_default_config_path() -> Path:
    env = os.environ.get("SIDECAR_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".config" / "sidecar" / "config.json"


def get_default_state_dir() -> Path:
    env = os.environ.get("SIDECAR_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state" / "sidecar"


@dataclass
class SidecarConfig:
    config_path: Path
    state_dir: Path
    a2a_token_path: Path | None = None
    identities: list[str] = field(default_factory=list)
    teams: dict[str, dict[str, Any]] = field(default_factory=dict)
    llm_gateway: str | None = None
    channel_repo: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def verify_token_permissions(self) -> bool:
        """Check if the A2A token file exists and has 0600 (or stricter) permissions."""
        if not self.a2a_token_path or not self.a2a_token_path.is_file():
            return False
        stat = self.a2a_token_path.stat()
        mode = stat.st_mode & 0o777
        return (mode & 0o077) == 0


def load_config(path: Path | str | None = None) -> SidecarConfig:
    config_path = Path(path) if path else get_default_config_path()
    state_dir = get_default_state_dir()

    if not config_path.is_file():
        return SidecarConfig(config_path=config_path, state_dir=state_dir)

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid sidecar configuration format in {config_path}: expected JSON object")

    state_dir_val = data.get("state_dir")
    if state_dir_val:
        state_dir = Path(state_dir_val)

    token_val = data.get("a2a_token_path")
    token_path = Path(token_val) if token_val else None

    identities = list(data.get("identities", []))
    teams = dict(data.get("teams", {}))
    llm_gateway = data.get("llm_gateway")
    channel_repo = data.get("channel_repo")

    return SidecarConfig(
        config_path=config_path,
        state_dir=state_dir,
        a2a_token_path=token_path,
        identities=identities,
        teams=teams,
        llm_gateway=llm_gateway,
        channel_repo=channel_repo,
        raw=data,
    )
