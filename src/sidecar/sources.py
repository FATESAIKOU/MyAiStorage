"""SubscriptionSource implementations (design D3/D4/D7, task 2.5).

`FileSubscriptionSource` is for local development: one JSON file in the common
JSON shape LLMGateway and sidecar share.  It accepts either a bare list of
rows or `{"subscriptions": [...]}`.

`LLMGatewaySource` goes through LLMGateway's own connector (`lgw.connect`).
Every method answers a value, a `SourceError` or `Unreachable` - never an
exception (D3).  One logical operation (one open, one swap) runs inside a
single `with lgw.connect.connect(cfg)`; the connect file is re-read for every
operation and never cached (D7), because LLMGateway may rotate the connection
information while the daemon keeps running.

The real `lgw` lives in the optional `gateway` extra; without it every
operation answers Unreachable with a message saying to install the extra.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from .protocols import (
    WINDOWS,
    SidecarError,
    SourceError,
    Subscription,
    Unreachable,
    epoch,
    iso,
)

logger = logging.getLogger(__name__)

GATEWAY_EXTRA_HINT = (
    "LLMGatewaySource needs the optional 'gateway' dependency (lgw); "
    "install it with the [gateway] extra"
)


def _rows_from_data(data: Any, where: str) -> list[dict[str, Any]] | Unreachable:
    if isinstance(data, dict):
        data = data.get("subscriptions")
    if not isinstance(data, list):
        return Unreachable(
            f"{where} must be a JSON list or {{\"subscriptions\": [...]}}"
        )
    return data


class FileSubscriptionSource:
    """Read one JSON file on every call; keep cooldowns in memory for the run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cooldowns: dict[tuple[str, str], str] = {}

    def _rows(self) -> list[dict[str, Any]] | Unreachable:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as e:
            return Unreachable(f"cannot read subscription file {self.path}: {e}")
        except ValueError as e:
            return Unreachable(f"subscription file {self.path} is not JSON: {e}")
        return _rows_from_data(data, f"subscription file {self.path}")

    def list(self) -> list[Subscription] | Unreachable:
        rows = self._rows()
        if isinstance(rows, Unreachable):
            return rows
        try:
            return [self._with_cooldowns(Subscription.from_json(row, with_key=False))
                    for row in rows]
        except SidecarError as e:
            logger.warning("file source list: %s", e)
            return Unreachable(str(e))

    def get(self, subscription_id: str, agent: str) -> Subscription | SourceError | Unreachable:
        rows = self._rows()
        if isinstance(rows, Unreachable):
            return rows
        for row in rows:
            if row.get("id") == subscription_id:
                try:
                    sub = self._with_cooldowns(Subscription.from_json(row, with_key=True))
                except SidecarError as e:
                    logger.warning("file source get: %s", e)
                    return Unreachable(str(e))
                if agent not in sub.agents:
                    return SourceError("agent_not_allowed")
                return sub
        return SourceError("not_found")

    def cooldown(
        self, subscription_id: str, window: str, resets_at: str | None
    ) -> None | SourceError | Unreachable:
        if window not in WINDOWS:
            return SourceError("invalid_window")
        if resets_at is not None:
            try:
                resets_at = iso(epoch(resets_at))
            except SidecarError:
                return SourceError("invalid_resets_at")
        self._cooldowns[(subscription_id, window)] = resets_at
        return None

    def _with_cooldowns(self, sub: Subscription) -> Subscription:
        now = time.time()
        cooldown = dict(sub.cooldown)
        for window in WINDOWS:
            if (sub.id, window) in self._cooldowns:
                cooldown[window] = self._cooldowns[(sub.id, window)]
        active = any(
            until is not None and (epoch(until) or 0) > now for until in cooldown.values()
        )
        sub.cooldown = cooldown
        sub.available = sub.available and not active
        return sub


class LLMGatewayTransport(Protocol):
    """The wire to LLMGateway; the real one is `lgw connect` (task 2.5, D7).

    One `with transport.operation() as source:` is one logical operation; the
    yielded source implements the SubscriptionSource methods.
    """

    def operation(self) -> Any: ...


class LLMGatewaySource:
    """SubscriptionSource over LLMGateway's connector (design D7, task 2.5).

    `connect` and `client_factory` are injectable so tests can run without the
    `lgw` extra: `connect` must look like `lgw.connect` (`load_config(path)`
    and a `connect(cfg)` context manager yielding the gateway URL), and
    `client_factory(base_url)` must return an `lgw.client`-like object with
    list/get/cooldown answering JSON objects.
    """

    def __init__(
        self,
        connect_file: str | Path,
        *,
        connect: Any | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self.connect_file = Path(connect_file)
        self._connect = connect
        self._client_factory = client_factory

    # Each direct call is one logical operation.
    def list(self) -> list[Subscription] | Unreachable:
        with self.operation() as source:
            return source.list()

    def get(self, subscription_id: str, agent: str) -> Subscription | SourceError | Unreachable:
        with self.operation() as source:
            return source.get(subscription_id, agent)

    def cooldown(
        self, subscription_id: str, window: str, resets_at: str | None
    ) -> None | SourceError | Unreachable:
        with self.operation() as source:
            return source.cooldown(subscription_id, window, resets_at)

    @contextmanager
    def operation(self) -> Iterator[Any]:
        """One connection for one logical operation; config re-read each time."""
        connect = self._connect
        client_factory = self._client_factory
        if connect is None or client_factory is None:
            try:
                connect, client_factory = _import_lgw()
            except ImportError as e:
                reason = f"{GATEWAY_EXTRA_HINT}: {e}"
                logger.warning("%s", reason)
                yield _UnreachableSource(Unreachable(reason))
                return
        try:
            cfg = connect.load_config(self.connect_file)  # never cached (D7)
        except Exception as e:
            reason = f"cannot read the LLMGateway connect file {self.connect_file}: {e}"
            logger.warning("%s", reason)
            yield _UnreachableSource(Unreachable(reason))
            return
        try:
            ctx = connect.connect(cfg)
        except Exception as e:
            reason = f"cannot connect to LLMGateway: {e}"
            logger.warning("%s", reason)
            yield _UnreachableSource(Unreachable(reason))
            return
        with ctx as base_url:
            yield _BoundSource(client_factory(base_url))


def _import_lgw() -> tuple[Any, Any]:
    """Import the real connector; ImportError means the extra is not installed."""
    from lgw import client as lgw_client  # type: ignore[import-not-found]
    from lgw import connect as lgw_connect  # type: ignore[import-not-found]

    # TODO(task 2.5): confirm the actual lgw.client constructor with the
    # LLMGateway PM; until then the injected test doubles are the contract.
    return lgw_connect, lgw_client.Client


class _UnreachableSource:
    """A source stub that answers Unreachable for one failed operation setup."""

    def __init__(self, reason: Unreachable) -> None:
        self._reason = reason

    def list(self) -> Unreachable:
        return self._reason

    def get(self, subscription_id: str, agent: str) -> Unreachable:
        return self._reason

    def cooldown(self, subscription_id: str, window: str, resets_at: str | None) -> Unreachable:
        return self._reason


class _BoundSource:
    """SubscriptionSource over one connected lgw.client-like object."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def list(self) -> list[Subscription] | Unreachable:
        data = self._client.list()
        failure = _failure(data)
        if failure is not None:
            return failure
        rows = data.get("subscriptions", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return Unreachable("LLMGateway list answered something unexpected")
        try:
            return [Subscription.from_json(row, with_key=False) for row in rows]
        except SidecarError as e:
            logger.warning("LLMGateway list: %s", e)
            return Unreachable(str(e))

    def get(self, subscription_id: str, agent: str) -> Subscription | SourceError | Unreachable:
        data = self._client.get(subscription_id, agent)
        failure = _failure(data)
        if failure is not None:
            return failure
        row = data.get("subscription", data) if isinstance(data, dict) else data
        try:
            sub = Subscription.from_json(row, with_key=True)
        except SidecarError as e:
            logger.warning("LLMGateway get: %s", e)
            return Unreachable(str(e))
        if agent not in sub.agents:
            # The gateway enforces this too (403 agent_not_allowed); failing
            # here as well turns a silent misconfiguration into a clear error.
            return SourceError("agent_not_allowed")
        return sub

    def cooldown(
        self, subscription_id: str, window: str, resets_at: str | None
    ) -> None | SourceError | Unreachable:
        if window not in WINDOWS:
            return SourceError("invalid_window")
        data = self._client.cooldown(subscription_id, window, resets_at)
        return _failure(data)


def _failure(data: Any) -> SourceError | Unreachable | None:
    if not isinstance(data, dict):
        return None
    if data.get("unreachable"):
        return Unreachable(str(data.get("reason") or "LLMGateway cannot be reached"))
    error = data.get("error")
    if isinstance(error, dict) and error.get("code"):
        message = error.get("message")
        return SourceError(str(error["code"]), None if message is None else str(message))
    if isinstance(error, str) and error:
        return SourceError(error)
    if data.get("ok") is False:
        return SourceError("refused")
    return None


def connect_llmgateway(config: Any) -> LLMGatewaySource:
    """Build the LLMGateway source from sidecar's own config (D7).

    The connect-file format belongs to `lgw`; sidecar never parses it itself.
    """
    return LLMGatewaySource(Path(config.gateway_connect_file))
