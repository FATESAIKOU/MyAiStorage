"""Configuration and paths for Angareion."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from angareion.messages import validate_address, MessageValidationError


class ConfigError(ValueError):
    """Raised when configuration is invalid or missing."""


class TokenError(ConfigError):
    """Base error for token issues."""


class TokenMissingError(TokenError):
    """Raised when token file does not exist."""


class TokenPermissionError(TokenError):
    """Raised when token file has invalid permissions (not 0600)."""


def get_config_path() -> Path:
    """Return path to config.json, honoring A2A_CONFIG environment variable."""
    env = os.environ.get("A2A_CONFIG")
    if env:
        p = Path(os.path.expanduser(env))
        if p.is_dir():
            return p / "config.json"
        return p
    return Path(os.path.expanduser("~/.config/angareion/config.json"))


def get_state_dir() -> Path:
    """Return path to state directory, honoring A2A_STATE_DIR environment variable."""
    env = os.environ.get("A2A_STATE_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~/.local/state/angareion"))


def get_default_token_path() -> Path:
    """Return path to token file, honoring A2A_TOKEN_FILE environment variable."""
    env = os.environ.get("A2A_TOKEN_FILE")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~/.config/angareion/token"))


@dataclass
class Config:
    channel_repo: str
    identity: dict[str, Any]
    token_file: Path = field(default_factory=get_default_token_path)
    state_dir: Path = field(default_factory=get_state_dir)
    config_file: Path = field(default_factory=get_config_path)

    def validate(self) -> None:
        if not self.channel_repo or not isinstance(self.channel_repo, str):
            raise ConfigError("channel_repo must be a non-empty string (e.g. 'owner-org/repo-channel')")
        try:
            validate_address(self.identity, field_name="identity")
        except MessageValidationError as e:
            raise ConfigError(f"Invalid identity: {e}") from e

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_repo": self.channel_repo,
            "identity": self.identity,
            "token_file": str(self.token_file),
        }


def load_config(path: Path | str | None = None) -> Config:
    """Load configuration from config.json with env overrides."""
    config_path = Path(path) if path is not None else get_config_path()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ConfigError(f"Failed to parse config file {config_path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a JSON object in {config_path}")

    channel_repo = data.get("channel_repo", "")
    identity = data.get("identity", {})

    token_file_env = os.environ.get("A2A_TOKEN_FILE")
    if token_file_env:
        token_file = Path(os.path.expanduser(token_file_env))
    elif "token_file" in data and data["token_file"]:
        token_file = Path(os.path.expanduser(str(data["token_file"])))
    else:
        token_file = get_default_token_path()

    state_dir = get_state_dir()

    cfg = Config(
        channel_repo=channel_repo,
        identity=identity,
        token_file=token_file,
        state_dir=state_dir,
        config_file=config_path,
    )
    cfg.validate()
    return cfg


def save_config(config: Config, path: Path | str | None = None) -> None:
    """Save configuration to config.json, creating parent directories if needed."""
    config.validate()
    config_path = Path(path) if path is not None else config.config_file
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def load_token(token_source: Config | Path | str | None = None) -> str:
    """Read fine-grained PAT from file with 0600 mode requirement.

    Raises TokenMissingError if the file is absent.
    Raises TokenPermissionError if the file permissions are not 0600 on POSIX.
    Raises TokenError if the token file is empty or unreadable.
    """
    if isinstance(token_source, Config):
        p = token_source.token_file
    elif token_source is None:
        p = get_default_token_path()
    else:
        p = Path(os.path.expanduser(str(token_source)))

    if not p.is_file():
        raise TokenMissingError(
            f"Token file not found: {p}. "
            "Please create a GitHub Fine-grained Personal Access Token with "
            "'Issues: Read and write' permissions and save it with 0600 permissions."
        )

    if os.name == "posix":
        st = p.stat()
        mode = st.st_mode & 0o777
        if mode != 0o600:
            raise TokenPermissionError(
                f"Token file permissions must be 0600 (owner read/write only, got {oct(mode)}): {p}"
            )

    try:
        token = p.read_text(encoding="utf-8").strip()
    except Exception as e:
        raise TokenError(f"Failed to read token file {p}: {type(e).__name__}") from e

    if not token:
        raise TokenError(f"Token file is empty: {p}")

    return token


def mask_token(text: str, token: str) -> str:
    """Mask any occurrences of token in text with asterisks."""
    if not token:
        return text
    return text.replace(token, "***")
