"""SubscriptionSource implementations (design D3/D4, task 2.5).

`FileSubscriptionSource` is for local development: one JSON file in the common
JSON shape both LLMGateway and sidecar agreed on (v3 proposal 2.4).  It accepts
either a bare list of rows or `{"subscriptions": [...]}`.

`LLMGatewaySource` talks through a transport seam.  The real transport is
LLMGateway's own connector (`lgw connect`); wiring it is a TODO until the PM
has aligned with the LLMGateway PM, so only the interface plus fake-transport
behaviour exists today.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from .protocols import (
    WINDOWS,
    SidecarError,
    Subscription,
    SubscriptionError,
    UnavailableError,
    iso,
)


class FileSubscriptionSource:
    """Read one JSON file on every call; keep cooldowns in memory for the run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cooldowns: dict[tuple[str, str], float] = {}

    def _rows(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as e:
            raise SidecarError(f"cannot read subscription file {self.path}: {e}") from e
        except ValueError as e:
            raise SidecarError(f"subscription file {self.path} is not JSON: {e}") from e
        if isinstance(data, dict):
            data = data.get("subscriptions")
        if not isinstance(data, list):
            raise SidecarError(
                f"subscription file {self.path} must be a JSON list "
                'or {"subscriptions": [...]}'
            )
        return data

    def list(self) -> list[Subscription]:
        return [self._with_cooldowns(Subscription.from_json(row, with_key=False))
                for row in self._rows()]

    def get(self, subscription_id: str, agent: str) -> Subscription:
        for row in self._rows():
            if row.get("id") == subscription_id:
                sub = self._with_cooldowns(Subscription.from_json(row, with_key=True))
                if agent not in sub.agents:
                    raise SubscriptionError(
                        f"subscription {subscription_id!r} does not allow agent {agent!r}"
                    )
                return sub
        raise SubscriptionError(f"unknown subscription {subscription_id!r}")

    def cooldown(self, subscription_id: str, window: str, resets_at: float) -> None:
        if window not in WINDOWS:
            raise SubscriptionError(f"window must be one of {', '.join(WINDOWS)}")
        self._cooldowns[(subscription_id, window)] = float(resets_at)

    def _with_cooldowns(self, sub: Subscription) -> Subscription:
        now = time.time()
        cooldown = dict(sub.cooldown)
        for window in WINDOWS:
            registered = self._cooldowns.get((sub.id, window))
            if registered is not None:
                cooldown[window] = registered
        active = any(until is not None and until > now for until in cooldown.values())
        sub.cooldown = cooldown
        sub.available = sub.available and not active
        return sub


class LLMGatewayTransport(Protocol):
    """The wire to LLMGateway; the real one is `lgw connect` (TODO, task 2.5)."""

    def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """op is one of list / get / cooldown; answers a JSON object."""
        ...


class LLMGatewaySource:
    """SubscriptionSource over an LLMGateway transport (design v3 2.4).

    The transport keeps the connection (and any credentials) to itself; this
    class only translates the three operations to the common JSON shape.
    """

    def __init__(self, transport: LLMGatewayTransport, *, endpoint: str | None = None) -> None:
        self._transport = transport
        self.endpoint = endpoint

    def list(self) -> list[Subscription]:
        data = self._transport.call("list", {})
        rows = data.get("subscriptions") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise SidecarError("LLMGateway list did not answer a list of subscriptions")
        return [Subscription.from_json(row, with_key=False) for row in rows]

    def get(self, subscription_id: str, agent: str) -> Subscription:
        data = self._transport.call("get", {"id": subscription_id, "agent": agent})
        if isinstance(data, dict) and isinstance(data.get("subscription"), dict):
            data = data["subscription"]
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise SidecarError("LLMGateway get did not answer a subscription object")
        sub = Subscription.from_json(data, with_key=True)
        if agent not in sub.agents:
            # The gateway enforces this too (403 agent_not_allowed); failing
            # here as well turns a silent misconfiguration into a clear error.
            raise SubscriptionError(
                f"subscription {subscription_id!r} does not allow agent {agent!r}"
            )
        return sub

    def cooldown(self, subscription_id: str, window: str, resets_at: float) -> None:
        if window not in WINDOWS:
            raise SubscriptionError(f"window must be one of {', '.join(WINDOWS)}")
        self._transport.call(
            "cooldown",
            {"id": subscription_id, "window": window, "resets_at": iso(resets_at)},
        )


def connect_llmgateway(*_: Any, **__: Any) -> LLMGatewaySource:
    """Build the real LLMGateway connection via `lgw connect`.

    TODO(task 2.5): LLMGateway's connector is being aligned with its PM; until
    then a real connection cannot be made and configuring one fails loudly
    instead of starting a daemon that silently cannot open agents.
    """
    raise UnavailableError(
        "LLMGatewaySource is not wired yet: waiting for LLMGateway's connector "
        "(lgw connect) to be aligned (task 2.5 TODO)"
    )


__all__ = [
    "FileSubscriptionSource",
    "LLMGatewaySource",
    "LLMGatewayTransport",
    "connect_llmgateway",
]
