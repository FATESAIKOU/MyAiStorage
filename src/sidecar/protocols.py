"""The internal seams of sidecar (change sidecar-v0, design D3).

The four external actions only talk to these interfaces, so the agent kinds,
the session store, the subscription source and the pick rule can each be
replaced without touching the control plane.  The implementations live in
`selectors.py` (PrioritySelector) and `sources.py` (FileSubscriptionSource and
LLMGatewaySource); the agent drivers and the agora session store arrive in
tasks 3.1/3.2/3.3.

A subscription secret (`Subscription.key`) must never reach a command line, a
log line or a file: it is only handed to `AgentDriver.env_for`, which the
driver turns into the child process environment (design D3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence, runtime_checkable

# --- agent states, as seen through GET /agents (design D1) ---

STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_IDLE = "idle"
STATE_WAITING_QUOTA = "waiting-quota"
STATE_EXITED = "exited"

# States in which an id counts as "in use" for open (design D1).
ACTIVE_STATES = frozenset({STATE_STARTING, STATE_RUNNING, STATE_IDLE})

# --- driver-reported states (design D3/D6) ---

DRIVER_RUNNING = "running"
DRIVER_IDLE = "idle"
DRIVER_EXITED = "exited"
DRIVER_QUOTA_EXHAUSTED = "quota_exhausted"

# The only two quota windows, matching LLMGateway (design D4).
WINDOWS = ("5h", "1w")

# Session modes for open (design D1/D5).
SESSION_MODES = ("new", "continue", "fork", "merge")

# Job actions: exactly the four external actions (design D1).
ACTIONS = ("open", "close", "status", "poke")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SidecarError(Exception):
    """An error the HTTP control plane answers as a JSON object."""

    status = 500


class BadRequestError(SidecarError):
    status = 400


class NotFoundError(SidecarError):
    status = 404


class ConfigError(SidecarError):
    status = 500


class ConflictError(SidecarError):
    status = 409


class UnavailableError(SidecarError):
    status = 503


class SubscriptionError(ConflictError):
    """The subscription source could not supply the requested subscription."""

    status = 409


def iso(ts: float | None) -> str | None:
    """Epoch seconds as an ISO 8601 UTC string (or None)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise BadRequestError(
            "agent id must be 1-128 chars of letters, digits, '.', '_', ':', '-' "
            "and start with a letter or digit"
        )
    return value


@dataclass
class Subscription:
    """One row of the common JSON that LLMGateway and sidecar share (v3 2.4).

    `key` is only present on rows returned by SubscriptionSource.get; it is
    kept out of repr() so an accidental log line never prints it.
    """

    id: str
    provider: str | None = None
    kind: str | None = None
    agents: tuple[str, ...] = ()
    priority: int = 0
    models: dict[str, Any] = field(default_factory=dict)
    endpoint: str | None = None
    key: str | None = field(default=None, repr=False)
    cooldown: dict[str, float | None] = field(default_factory=dict)
    available: bool = True
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, row: dict[str, Any], *, with_key: bool = True) -> Subscription:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise SidecarError(f"subscription row must be a JSON object with an id: {row!r}")
        agents = row.get("agents") or ()
        cooldown = row.get("cooldown") or {}
        parsed_cooldown: dict[str, float | None] = {}
        if isinstance(cooldown, dict):
            for window in WINDOWS:
                parsed_cooldown[window] = _parse_until(cooldown.get(window))
        return cls(
            id=row["id"],
            provider=row.get("provider"),
            kind=row.get("kind"),
            agents=tuple(str(a) for a in agents),
            priority=int(row.get("priority") or 0),
            models=dict(row.get("models") or {}),
            endpoint=row.get("endpoint"),
            key=row.get("key") if with_key else None,
            cooldown=parsed_cooldown,
            available=bool(row.get("available", True)),
            raw=dict(row),
        )


def _parse_until(entry: Any) -> float | None:
    """`{"until": "…"}` (ISO) or an epoch number; None when absent."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        entry = entry.get("until")
    if entry is None:
        return None
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return float(entry)
    if isinstance(entry, str):
        try:
            dt = datetime.fromisoformat(entry.replace("Z", "+00:00"))
        except ValueError:
            raise SidecarError(f"unparseable cooldown time {entry!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise SidecarError(f"unparseable cooldown time {entry!r}")


@dataclass
class DriverState:
    """What AgentDriver.state reports (design D3)."""

    kind: str
    window: str | None = None  # quota_exhausted: "5h" or "1w"
    resets_at: float | None = None  # quota_exhausted: epoch seconds
    code: int | None = None  # exited: exit code
    reason: str | None = None  # exited/quota: human-readable


@dataclass
class PreparedSession:
    """The agora Session an agent will run on (design D3/D5/D6).

    `argv` is what the agent driver hands to the session store's process
    (`agora continue … --headless`); it must never carry subscription values.
    """

    agora_id: str
    mode: str
    ids: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()


@runtime_checkable
class AgentDriver(Protocol):
    """One coding-agent kind (Claude Code, opencode); impls in task 3.1/3.2."""

    name: str

    def env_for(self, subscription: Subscription) -> dict[str, str]:
        """The child-process environment for this subscription (design D3).

        The returned dict is the only place subscription values may appear.
        """
        ...

    def launch(self, session: PreparedSession, env: dict[str, str], workdir: str) -> Any:
        """Start the agent on the prepared session; return a handle."""
        ...

    def send(self, handle: Any, text: str) -> None: ...

    def interrupt(self, handle: Any) -> None: ...

    def state(self, handle: Any) -> DriverState: ...

    def stop(self, handle: Any, *, graceful: bool = True, timeout: float = 10.0) -> None:
        """Stop the agent; graceful escalates to force after `timeout`."""
        ...


@runtime_checkable
class SessionStore(Protocol):
    """Where Session memory lives (agora); impl in task 3.3."""

    def start(
        self,
        *,
        mode: str,
        ids: Sequence[str],
        agent: str,
        workdir: str,
        tag: str,
    ) -> PreparedSession:
        """Prepare the Session for `mode` over `ids` and tag it."""
        ...

    def sync(self, session: PreparedSession) -> None:
        """Write the current conversation back (periodic and on stop)."""
        ...


@runtime_checkable
class SubscriptionSource(Protocol):
    """A pool of subscriptions with cooldowns; impls in `sources.py`."""

    def list(self) -> list[Subscription]:
        """Every subscription, without keys."""
        ...

    def get(self, subscription_id: str, agent: str) -> Subscription:
        """One subscription with its key; refuses an agent it does not allow."""
        ...

    def cooldown(self, subscription_id: str, window: str, resets_at: float) -> None:
        """Register `window` ("5h"/"1w") cooling down until `resets_at`."""
        ...


@runtime_checkable
class Selector(Protocol):
    """The replaceable pick rule (design D3); default `PrioritySelector`."""

    def choose(self, agent: str, subscriptions: Sequence[Subscription]) -> str | None: ...
