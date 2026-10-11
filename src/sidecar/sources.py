"""SubscriptionSource implementations (design D3/D4/D7, task 2.5; review H1).

`FileSubscriptionSource` is for local development: one JSON file in the common
JSON shape LLMGateway and sidecar share.  It accepts either a bare list of
rows or `{"subscriptions": [...]}`.

`LLMGatewaySource` goes through LLMGateway's own connector (`lgw.connect`).
Every method answers a value, a `SourceError` or `Unreachable` - never an
exception (D3).  One logical operation (one open, one swap) runs inside a
single `with lgw.connect.connect(cfg)`; the connect file is re-read for every
operation and never cached (D7), because LLMGateway may rotate the connection
information while the daemon keeps running.

lgw's real interface (lgw-v0.1.0, design D7):

* `lgw.client.Client(base_url, timeout=10)`; `list()` / `get(id, agent)` /
  `cooldown(id, window, resets_at=None)` all answer
  `Result(ok, status, data, error, unreachable)` and never raise.
  `list` data is `{schema, subscriptions: [...]}`; `get` data is the row
  itself (flat, with `key`); a successful cooldown is 204.  `Result.error` is
  a stable code string (`agent_not_allowed`, `invalid_resets_at`, ...).
* `lgw.connect.load_config(path)` raises `OSError` / `ConfigError`;
  `lgw.connect.connect(cfg)` raises `lgw.connect.Unavailable` - but only when
  the `with` is entered, so `operation()` must catch exceptions from
  `__enter__` too, not just from building the context manager.

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


class LLMGatewaySource:
    """SubscriptionSource over LLMGateway's connector (design D7, task 2.5).

    `connect` and `client_factory` are injectable so tests can run without the
    `lgw` extra: `connect` must look like `lgw.connect` (`load_config(path)`
    and a `connect(cfg)` context manager yielding the gateway URL; exceptions
    may come from entering the `with`), and `client_factory(base_url)` must
    return an `lgw.client.Client`-like object whose list/get/cooldown answer
    `Result`-shaped objects (`ok`, `status`, `data`, `error`, `unreachable`).
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
        """One connection for one logical operation; config re-read each time.

        Anything that stops the operation from starting - a missing lgw, an
        unreadable/invalid connect file, or an `Unavailable` raised while
        *entering* the connection - becomes an `Unreachable`; no exception
        escapes here.
        """
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
            ctx = connect.connect(cfg)
        except Exception as e:
            reason = f"cannot read the LLMGateway connect file {self.connect_file}: {_kind(e)}"
            logger.warning("%s", reason)
            yield _UnreachableSource(Unreachable(reason))
            return
        try:
            base_url = ctx.__enter__()  # Unavailable is raised here, not above
        except Exception as e:
            reason = f"cannot connect to LLMGateway: {_kind(e)}"
            logger.warning("%s", reason)
            yield _UnreachableSource(Unreachable(reason))
            return
        try:
            yield _BoundSource(client_factory(base_url))
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                logger.warning("closing the LLMGateway connection failed", exc_info=True)


def _kind(exc: BaseException) -> str:
    """The kind of a connection exception, without any of its output (D7).

    `lgw.connect.Unavailable` carries `kind` (`ssh_missing`, `timeout`, ...);
    for anything else the class name is the least we can say without leaking
    ssh output or file contents.
    """
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and kind:
        return kind
    return type(exc).__name__


def _import_lgw() -> tuple[Any, Any]:
    """Import the real connector; ImportError means the extra is not installed."""
    from lgw import client as lgw_client  # type: ignore[import-not-found]
    from lgw import connect as lgw_connect  # type: ignore[import-not-found]

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
    """SubscriptionSource over one connected lgw.client-like object (D7 table)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def list(self) -> list[Subscription] | Unreachable:
        result = self._client.list()
        failure = _failure(result)
        if failure is not None:
            return failure
        data = getattr(result, "data", None)
        rows = data.get("subscriptions") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return Unreachable("LLMGateway list answered something unexpected")
        try:
            return [Subscription.from_json(row, with_key=False) for row in rows]
        except SidecarError as e:
            logger.warning("LLMGateway list: %s", e)
            return Unreachable(str(e))

    def get(self, subscription_id: str, agent: str) -> Subscription | SourceError | Unreachable:
        result = self._client.get(subscription_id, agent)
        failure = _failure(result)
        if failure is not None:
            return failure
        row = getattr(result, "data", None)
        if not isinstance(row, dict):
            return Unreachable("LLMGateway get answered something unexpected")
        try:
            sub = Subscription.from_json(row, with_key=True)
        except SidecarError as e:
            logger.warning("LLMGateway get: %s", e)
            return Unreachable(str(e))
        if agent not in sub.agents:
            # The gateway enforces this too; failing here turns a silent
            # misconfiguration into a clear error.
            return SourceError("agent_not_allowed")
        return sub

    def cooldown(
        self, subscription_id: str, window: str, resets_at: str | None
    ) -> None | SourceError | Unreachable:
        if window not in WINDOWS:
            return SourceError("invalid_window")
        result = self._client.cooldown(subscription_id, window, resets_at)
        return _failure(result)


def _failure(result: Any) -> SourceError | Unreachable | None:
    """The D7 mapping from an lgw Result to our answer; None means success.

    `ok=False` is never success; unknown statuses are Unreachable (cannot
    tell), never a silent success and never a fabricated error code.
    """
    if getattr(result, "unreachable", False):
        return Unreachable("LLMGateway cannot be reached")
    if getattr(result, "ok", False):
        return None
    status = getattr(result, "status", None)
    error = getattr(result, "error", None)
    if status == 403:
        return SourceError("agent_not_allowed")
    if status == 404:
        return SourceError("not_found")
    if status == 409:
        return SourceError("not_deployed")
    if status == 413:
        return SourceError("body_too_large")
    if status == 400:
        code = error if isinstance(error, str) and error else "bad_request"
        return SourceError(code)
    if error == "bad_response":
        return Unreachable("LLMGateway answered something that is not a JSON object")
    return Unreachable(f"LLMGateway answered status {status!r} ({error!r})")


def connect_llmgateway(config: Any) -> LLMGatewaySource:
    """Build the LLMGateway source from sidecar's own config (D7).

    The connect-file format belongs to `lgw`; sidecar never parses it itself.
    """
    return LLMGatewaySource(Path(config.gateway_connect_file))
