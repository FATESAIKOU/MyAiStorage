"""Configuration, paths, token reading and the local-copy settings for Angareion (D5/v5)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from typing import Any

from angareion.messages import MessageValidationError, validate_address

CONFIG_PULL_INTERVAL_DEFAULT = 900


def channel_slug(channel: str) -> str:
    """Reversible percent-encode of a channel name into a filename-safe slug.

    Chars in `[a-z0-9_-]` pass through; uppercase ASCII letters and everything
    else are percent-encoded as `%XX` over their UTF-8 bytes.
    """
    out: list[str] = []
    for ch in str(channel):
        if ch.isascii() and ((ch.isalpha() and ch.islower()) or ch.isdigit() or ch in "_-"):
            out.append(ch)
        else:
            for byte in ch.encode("utf-8"):
                out.append(f"%{byte:02X}")
    return "".join(out)

#: The environment variable that names this seat, overriding config.json (#97).
IDENTITY_ENV = "ANGAREION_IDENTITY"


class ConfigError(ValueError):
    """Raised when configuration is invalid or missing."""


class TokenError(ConfigError):
    """Base error for token issues."""


class TokenMissingError(TokenError):
    """Raised when the token file does not exist."""


class TokenPermissionError(TokenError):
    """Raised when the token file has invalid permissions (not owner-only readable)."""


def get_config_path() -> Path:
    """Return path to config.json, honoring ANGAREION_CONFIG."""
    env = os.environ.get("ANGAREION_CONFIG")
    if env:
        p = Path(os.path.expanduser(env))
        if p.is_dir():
            return p / "config.json"
        return p
    return Path(os.path.expanduser("~/.config/angareion/config.json"))


def get_state_dir() -> Path:
    """Return path to the state directory, honoring ANGAREION_STATE_DIR."""
    env = os.environ.get("ANGAREION_STATE_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~/.local/state/angareion"))


def get_default_token_path() -> Path:
    """Return path to the token file, honoring ANGAREION_TOKEN_FILE."""
    env = os.environ.get("ANGAREION_TOKEN_FILE")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser("~/.config/angareion/token"))


def identity_slug(identity: str) -> str:
    """Derive a filesystem-safe filename slug for an AI name (v5/#97).

    AI names are already `[a-z0-9-]`, so this mostly passes through; anything
    else is replaced so a hand-written identity cannot escape the state
    directory. v0's `{repo}__{role}` slug is gone: old cursor files are
    simply never read again (a new identity starts from the beginning).
    """
    slug = "".join(
        character if (character.isascii() and (character.isalnum() or character in "._-")) else "_"
        for character in str(identity)
    )
    return slug or "unknown"


@dataclass
class Config:
    identity: str
    config_dir: Path
    registry_repo: str
    registry_issue: int
    token_file: Path = field(default_factory=get_default_token_path)
    state_dir: Path = field(default_factory=get_state_dir)
    config_file: Path = field(default_factory=get_config_path)
    config_pull_interval: int = CONFIG_PULL_INTERVAL_DEFAULT

    def validate(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ConfigError(
                "identity must be an AI name such as 'mylinuxpool-impl-w1-1'"
            )
        try:
            validate_address(self.identity, field_name="identity")
        except MessageValidationError as e:
            raise ConfigError(f"Invalid identity: {e}") from e
        if not self.config_dir or not str(self.config_dir).strip():
            raise ConfigError("config_dir must be the path of the local MyAiStorage clone")
        repo = self.registry_repo
        if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
            raise ConfigError("registry_repo must be an 'owner/repo' string")
        if not isinstance(self.registry_issue, int) or isinstance(self.registry_issue, bool) or self.registry_issue <= 0:
            raise ConfigError("registry_issue must be a positive issue number")
        if (
            not isinstance(self.config_pull_interval, int)
            or isinstance(self.config_pull_interval, bool)
            or self.config_pull_interval < 0
        ):
            raise ConfigError("config_pull_interval must be an integer number of seconds (0 = no periodic pull)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "config_dir": str(self.config_dir),
            "registry_repo": self.registry_repo,
            "registry_issue": self.registry_issue,
            "token_file": str(self.token_file),
            "config_pull_interval": self.config_pull_interval,
        }

    def with_identity(self, identity: str) -> "Config":
        """A copy of this config that names a different seat (ANGAREION_IDENTITY).

        Everything else - paths, registry, intervals - stays as it is; only the
        name this run speaks as changes.
        """
        return replace(self, identity=identity)


def resolve_identity(config: Config) -> str:
    """The AI name this run speaks as (#97).

    `ANGAREION_IDENTITY` wins over the `identity` in config.json: one machine
    can hold several seats, and the environment says which one this run is.
    The name decides *everything* about the seat - `from`, "to me" and the
    cursor file - so it is resolved once, here, and the config is replaced
    before anything else is built from it.

    A set-but-illegal value is a configuration error (exit 2), not a silent
    fall back: running as the wrong seat is worse than not running. The raw
    value is validated as it stands - a name with a space around it is a
    mistyped name, and `config.json`'s `identity` is not trimmed either, so the
    two places behave the same way.
    """
    if IDENTITY_ENV not in os.environ:
        return config.identity
    candidate = os.environ[IDENTITY_ENV]
    try:
        validate_address(candidate, field_name=IDENTITY_ENV)
    except MessageValidationError as e:
        raise ConfigError(f"{IDENTITY_ENV} is not a legal AI name: {e}") from e
    return candidate


def _required(data: dict[str, Any], key: str, config_path: Path) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"Configuration is missing '{key}': {config_path}")
    return data[key]


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

    identity = _required(data, "identity", config_path)
    config_dir = _required(data, "config_dir", config_path)
    registry_repo = _required(data, "registry_repo", config_path)
    registry_issue = _required(data, "registry_issue", config_path)

    token_file_env = os.environ.get("ANGAREION_TOKEN_FILE")
    if token_file_env:
        token_file = Path(os.path.expanduser(token_file_env))
    elif data.get("token_file"):
        token_file = Path(os.path.expanduser(str(data["token_file"])))
    else:
        token_file = get_default_token_path()

    cfg = Config(
        identity=str(identity),
        config_dir=Path(os.path.expanduser(str(config_dir))),
        registry_repo=str(registry_repo),
        registry_issue=registry_issue,
        token_file=token_file,
        state_dir=get_state_dir(),
        config_file=config_path,
        config_pull_interval=data.get("config_pull_interval", CONFIG_PULL_INTERVAL_DEFAULT),
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
    """Read the channel token from a file with an owner-only mode requirement.

    Accepts 0600 and 0400 (read-only mounts): group/other must have no
    permission bits at all and the owner must be able to read. 0640, 0644,
    0444 and the like are still rejected.

    Raises TokenMissingError if the file is absent.
    Raises TokenPermissionError if group/other have any permission bits or
    the owner cannot read on POSIX.
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
        if mode & 0o077 or not mode & 0o400:
            raise TokenPermissionError(
                f"Token file permissions must be owner-only with owner read "
                f"(0600 or 0400, got {oct(mode)}): {p}"
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
