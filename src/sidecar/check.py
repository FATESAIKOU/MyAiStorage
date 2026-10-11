"""`sidecar check`: health verification with three-state exit codes (D11).

Exit codes (MyLinuxPool rule): 0 healthy; 1 definitely unhealthy (bad
config, token missing or not owner-only, herdr incompatible, 401/403 on the
channel read); 2 unknown (herdr unreachable, 5xx, connection errors).
Per-check reasons never contain secrets.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from angareion.backend import AuthenticationError, BackendError, RateLimitError
from angareion.config import TokenError, load_token, mask_token

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 2


@dataclass
class CheckItem:
    name: str
    status: str  # "ok" | "fail" | "unknown"
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "reason": self.reason}


def _http_status(message: str) -> int | None:
    match = re.search(r"HTTP (\d{3})", message)
    return int(match.group(1)) if match else None


def run_checks(
    config: Any,
    herdr_client: Any,
    backend_factory: Callable[[str, str], Any],
) -> tuple[list[CheckItem], int]:
    """Run the four health checks, returning items and the exit code."""
    items: list[CheckItem] = []
    token = ""

    channel_repo = config.channel_repo or ""
    if not channel_repo:
        items.append(
            CheckItem("config", "fail", "channel_repo is not configured")
        )
    else:
        items.append(
            CheckItem(
                "config",
                "ok",
                f"config readable ({config.config_path}), "
                f"channel_repo={channel_repo}",
            )
        )

    if not getattr(config, "a2a_token_path", None):
        items.append(CheckItem("token", "fail", "a2a_token_path is not configured"))
    else:
        try:
            token = load_token(config.a2a_token_path)
            items.append(
                CheckItem(
                    "token",
                    "ok",
                    f"token file readable and owner-only ({config.a2a_token_path})",
                )
            )
        except TokenError as e:
            items.append(CheckItem("token", "fail", f"token unreadable: {e}"))

    try:
        status = herdr_client.status_server()
    except Exception as e:
        items.append(
            CheckItem("herdr", "unknown", f"herdr server unreachable: {e}")
        )
        status = None
    if status is not None:
        if not isinstance(status, dict):
            items.append(
                CheckItem("herdr", "unknown", "herdr server returned unparseable status")
            )
        elif status.get("compatible") is False:
            items.append(
                CheckItem(
                    "herdr",
                    "fail",
                    f"herdr server incompatible (version={status.get('version')})",
                )
            )
        elif status.get("running") is not True:
            items.append(
                CheckItem("herdr", "fail", "herdr server is not running")
            )
        else:
            items.append(
                CheckItem(
                    "herdr",
                    "ok",
                    f"herdr server running (version={status.get('version')}, "
                    f"session={status.get('session')})",
                )
            )

    token_item = next(i for i in items if i.name == "token")
    config_item = next(i for i in items if i.name == "config")
    if token_item.status != "ok" or config_item.status != "ok":
        items.append(
            CheckItem("channel", "unknown", "channel not probed (config or token failed)")
        )
    else:
        try:
            backend_factory(channel_repo, token).list_channels()
            items.append(
                CheckItem("channel", "ok", f"channel repo readable ({channel_repo})")
            )
        except (AuthenticationError, RateLimitError) as e:
            items.append(
                CheckItem("channel", "fail", f"channel repo refused the token: {e}")
            )
        except BackendError as e:
            http_status = _http_status(str(e))
            if http_status is not None and 500 <= http_status <= 599:
                items.append(
                    CheckItem("channel", "unknown", f"channel repo error: {e}")
                )
            elif http_status is not None:
                items.append(
                    CheckItem("channel", "fail", f"channel repo error: {e}")
                )
            else:
                items.append(
                    CheckItem("channel", "unknown", f"channel repo unreachable: {e}")
                )
        except Exception as e:
            items.append(
                CheckItem("channel", "unknown", f"channel probe failed: {e}")
            )

    if token:
        for item in items:
            item.reason = mask_token(item.reason, token)

    if any(i.status == "fail" for i in items):
        return items, EXIT_FAIL
    if any(i.status == "unknown" for i in items):
        return items, EXIT_UNKNOWN
    return items, EXIT_OK


def load_config_for_check(path: Path | str | None) -> Any:
    """Load the sidecar config, raising a plain error when it is missing/bad."""
    from sidecar.config import load_config

    config_path = Path(path) if path else None
    if config_path is None:
        from sidecar.config import get_default_config_path

        config_path = get_default_config_path()
    if not config_path.is_file():
        raise FileNotFoundError(f"sidecar config not found: {config_path}")
    return load_config(config_path)
