"""The internal seams of sidecar (change sidecar-v0, design D3, synced with the
2026-10-12 cross-check).

The four external actions only talk to these interfaces, so the agent kinds,
the session store, the subscription source and the pick rule can each be
replaced without touching the control plane.  The implementations live in
`selectors.py` (PrioritySelector) and `sources.py` (FileSubscriptionSource and
LLMGatewaySource); the agent drivers and the agora session store arrive in
tasks 3.1/3.2/3.3.

Subscription results are values, not exceptions (D3): every
`SubscriptionSource` method answers either its normal result, a `SourceError`
(the gateway refused with a code) or `Unreachable` (cannot tell right now).

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
    """An error the HTTP control plane answers as {"error": {code, message}}."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


# --- the D4 error table, as named errors --------------------------------------


class BadRequestError(SidecarError):
    status = 400
    code = "invalid_request"


class WorkdirNotFoundError(BadRequestError):
    code = "workdir_not_found"


class SubscriptionNotAllowedError(BadRequestError):
    code = "subscription_not_allowed"


class SubscriptionNotFoundError(BadRequestError):
    code = "subscription_not_found"


class NotFoundError(SidecarError):
    status = 404
    code = "not_found"


class AgentNotFoundError(NotFoundError):
    code = "agent_not_found"


class JobNotFoundError(NotFoundError):
    code = "job_not_found"


class ConflictError(SidecarError):
    status = 409
    code = "conflict"


class IdConflictError(ConflictError):
    code = "id_conflict"


class AgentNotRunningError(ConflictError):
    code = "agent_not_running"


class PokeTimeoutError(ConflictError):
    code = "poke_timeout"


class NotDeployedError(ConflictError):
    code = "not_deployed"


class NoSubscriptionError(ConflictError):
    code = "no_subscription"


class NoSubscriptionForMergeError(ConflictError):
    code = "no_subscription_for_merge"


class UnavailableError(SidecarError):
    status = 503
    code = "unavailable"


class GatewayUnreachableError(UnavailableError):
    code = "gateway_unreachable"


class ConfigError(SidecarError):
    status = 500
    code = "config_error"


# --- time helpers -------------------------------------------------------------


def iso(ts: float | None) -> str | None:
    """Epoch seconds as RFC 3339 UTC (the shape LLMGateway uses)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(ts: str | float | None) -> float | None:
    """RFC 3339 UTC (or an epoch number) back to epoch seconds; None stays None."""
    if ts is None:
        return None
    if isinstance(ts, bool):
        raise SidecarError(f"unparseable time {ts!r}")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            raise SidecarError(f"unparseable time {ts!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise SidecarError(f"unparseable time {ts!r}")


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise BadRequestError(
            "agent id must be 1-128 chars of letters, digits, '.', '_', ':', '-' "
            "and start with a letter or digit"
        )
    return value


# --- subscription rows (LLMGateway gateway-v0 / v4 2.4) -----------------------


@dataclass
class Subscription:
    """One row of the common JSON that LLMGateway and sidecar share.

    `key` is only present on rows returned by SubscriptionSource.get; it is
    kept out of repr() so an accidental log line never prints it.  Cooldown
    values are RFC 3339 UTC strings (or None for a window that is not cooling).
    """

    id: str
    provider: str | None = None
    kind: str | None = None
    agents: tuple[str, ...] = ()
    priority: int = 0
    models: dict[str, Any] = field(default_factory=dict)
    endpoint: str | None = None
    key: str | None = field(default=None, repr=False)
    cooldown: dict[str, str | None] = field(default_factory=dict)
    available: bool = True
    schema: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, row: dict[str, Any], *, with_key: bool = True) -> Subscription:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise SidecarError(f"subscription row must be a JSON object with an id: {row!r}")
        schema = row.get("schema", 1)
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise SidecarError(f"subscription {row.get('id')!r}: schema must be an integer")
        if schema > 1:
            raise SidecarError(
                f"subscription {row['id']!r}: schema {schema} is newer than this sidecar "
                "understands (1); refusing to guess"
            )
        agents = row.get("agents") or ()
        cooldown = row.get("cooldown") or {}
        parsed_cooldown: dict[str, str | None] = {}
        if isinstance(cooldown, dict):
            for window in WINDOWS:
                parsed_cooldown[window] = _until(cooldown.get(window))
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
            schema=schema,
            raw=dict(row),
        )

    def is_deployed(self) -> bool:
        """False only when the row says unavailable and no window is cooling.

        That combination is "the key is not deployed", not "cooling down"
        (design D4 / M3): such a row can never be a candidate for a reopen.
        """
        return self.available or any(self.cooldown.get(w) for w in WINDOWS)


def _until(entry: Any) -> str | None:
    """`{"until": "<RFC3339>"}` (also tolerates a bare string or epoch number)."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        entry = entry.get("until")
    if entry is None:
        return None
    parsed = epoch(entry)  # validates
    return iso(parsed)


# --- source answers (D3: results or cannot-tell, never exceptions) ------------


@dataclass(frozen=True)
class SourceError:
    """The gateway refused with a code: agent_not_allowed / not_found /
    not_deployed / anything the gateway names (e.g. invalid_resets_at)."""

    code: str
    message: str | None = None


@dataclass(frozen=True)
class Unreachable:
    """The source cannot be asked right now (no connection, bad connect file)."""

    reason: str | None = None


# --- driver / session shapes --------------------------------------------------


@dataclass
class DriverState:
    """What AgentDriver.state reports (design D3)."""

    kind: str
    window: str | None = None  # quota_exhausted: "5h" or "1w"
    resets_at: str | None = None  # quota_exhausted: RFC 3339 UTC, optional
    code: int | None = None  # exited: exit code
    reason: str | None = None  # exited/quota: human-readable


@dataclass
class RunningSession:
    """The agora side of a running agent (design D3: (session_id, process)).

    `session_id` is the agora id the agent runs on: the new one after
    fork/merge, the same one after continue; for `new` there is no agora
    process at all (D5), so the driver is launched with session=None.
    """

    session_id: str | None = None
    process: Any = None


@runtime_checkable
class AgentDriver(Protocol):
    """One coding-agent kind (Claude Code, opencode); impls in task 3.1/3.2."""

    name: str

    def env_for(self, subscription: Subscription) -> dict[str, str]:
        """The child-process environment for this subscription (design D3).

        The returned dict is the only place subscription values may appear.
        """
        ...

    def launch(self, session: RunningSession | None, env: dict[str, str], workdir: str) -> Any:
        """Start or attach to the agent; return a handle.

        `session` is None for open's `new` mode (D5): the driver starts the
        agent itself, and the session id comes from its events later.
        """
        ...

    def send(self, handle: Any, text: str) -> None: ...

    def interrupt(self, handle: Any) -> None: ...

    def state(self, handle: Any) -> DriverState: ...

    def stop(self, handle: Any, *, graceful: bool = True, timeout: float = 30.0) -> None:
        """Stop the agent: SIGTERM to the agora process, escalate after `timeout`.

        The agora process writes the last conversation back on its way out
        (design D6); no separate sync exists (D3).
        """
        ...


@runtime_checkable
class SessionStore(Protocol):
    """Where Session memory lives (agora); impl in task 3.3.

    Continue/fork/merge go through `agora continue --headless`; merge first
    runs `agora merge --agent <by>`.  `new` does not use this store (D5).
    """

    def start(
        self,
        *,
        mode: str,
        ids: Sequence[str],
        by: str,
        agent: str,
        workdir: str,
        tag: str,
        env: dict[str, str],
        merge_env: dict[str, str] | None = None,
    ) -> RunningSession:
        """Prepare and tag the Session; return (session_id, process).

        `merge_env` is the environment for the summarizer when `by` differs
        from `agent` (D5); None means "same as env".
        """
        ...


@runtime_checkable
class SubscriptionSource(Protocol):
    """A pool of subscriptions with cooldowns; impls in `sources.py`."""

    def list(self) -> list[Subscription] | Unreachable:
        """Every subscription, without keys."""
        ...

    def get(self, subscription_id: str, agent: str) -> Subscription | SourceError | Unreachable:
        """One subscription with its key; refuses an agent it does not allow."""
        ...

    def cooldown(
        self, subscription_id: str, window: str, resets_at: str | None
    ) -> None | SourceError | Unreachable:
        """Register `window` cooling down until `resets_at` (RFC 3339, optional)."""
        ...


@runtime_checkable
class Selector(Protocol):
    """The replaceable pick rule (design D3); default `PrioritySelector`."""

    def choose(
        self, agent: str, subscriptions: Sequence[Subscription], exclude: Sequence[str] = ()
    ) -> str | None: ...
