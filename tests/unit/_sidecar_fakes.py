"""In-memory fakes for the sidecar seams (change sidecar-v0, tasks 2.2-2.6).

These stand in for Claude Code / opencode drivers, for agora's session store,
for LLMGateway (connect + client) and for its subscription source, so every
unit test runs without opening a real agent, touching Drive or touching the
network.
"""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sidecar.protocols import (
    DRIVER_EXITED,
    DRIVER_IDLE,
    DRIVER_RUNNING,
    DriverState,
    RunningSession,
    SourceError,
    Subscription,
    Unreachable,
)


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeHandle:
    """One fake agent process: an inbox, an interrupt counter, a state."""

    def __init__(self, session: RunningSession | None) -> None:
        self.session = session
        self.state = DriverState(kind=DRIVER_IDLE)
        self.inbox: list[str] = []
        self.queued: list[str] = []  # sent while a round was running
        self.interrupts = 0
        self.stopped = False
        self.busy = False

    def finish_round(self) -> None:
        """The running round ends; anything queued becomes the next input."""
        self.busy = False
        self.state = DriverState(kind=DRIVER_IDLE)
        self.inbox.extend(self.queued)
        self.queued.clear()


class FakeAgentDriver:
    """AgentDriver double; records launches and lets tests drive state."""

    def __init__(self, name: str = "claude-code") -> None:
        self.name = name
        self.launches: list[tuple[RunningSession | None, dict[str, str], str]] = []
        self.handles: list[FakeHandle] = []
        self.stop_calls: list[tuple[FakeHandle, bool, float]] = []
        self.state_calls = 0
        self.state_fails = False  # raise from state(), for the isolation tests
        self.launch_gate: threading.Event | None = None
        self.launch_started = threading.Event()

    # -- AgentDriver ------------------------------------------------------

    def env_for(self, subscription: Subscription) -> dict[str, str]:
        env = {}
        if subscription.key:
            env["FAKE_AGENT_KEY"] = subscription.key
        if subscription.endpoint:
            env["FAKE_AGENT_ENDPOINT"] = subscription.endpoint
        return env

    def launch(self, session: RunningSession | None, env: dict[str, str], workdir: str) -> FakeHandle:
        self.launch_started.set()
        if self.launch_gate is not None:
            self.launch_gate.wait(timeout=5)
        handle = FakeHandle(session)
        self.launches.append((session, dict(env), workdir))
        self.handles.append(handle)
        return handle

    def send(self, handle: FakeHandle, text: str) -> None:
        if handle.busy:
            handle.queued.append(text)
        else:
            handle.inbox.append(text)
            handle.busy = True
            handle.state = DriverState(kind=DRIVER_RUNNING)

    def interrupt(self, handle: FakeHandle) -> None:
        handle.interrupts += 1
        handle.busy = False
        handle.state = DriverState(kind=DRIVER_IDLE)

    def state(self, handle: FakeHandle) -> DriverState:
        self.state_calls += 1
        if self.state_fails:
            raise RuntimeError("fake driver state exploded")
        if handle.stopped and handle.state.kind != DRIVER_EXITED:
            return DriverState(kind=DRIVER_EXITED, code=0, reason="stopped")
        return handle.state

    def stop(
        self, handle: FakeHandle, *, graceful: bool = True, timeout: float = 30.0
    ) -> None:
        self.stop_calls.append((handle, graceful, timeout))
        handle.stopped = True
        handle.busy = False
        handle.state = DriverState(kind=DRIVER_EXITED, code=0, reason="closed")

    # -- test helpers -----------------------------------------------------

    def force_state(self, handle: FakeHandle, kind: str, **fields: Any) -> None:
        handle.state = DriverState(kind=kind, **fields)

    def external_handle(self) -> FakeHandle:
        """An agent this sidecar did not open (must never show up in status)."""
        return FakeHandle(RunningSession(session_id="agora:not-ours"))


class FakeSessionStore:
    """SessionStore double; keeps a tiny in-memory agora.  No sync (D3)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.starts: list[dict[str, Any]] = []
        self.fail_start: Exception | None = None  # raise from start(), for M3
        self._counter = 0

    def seed(
        self, agora_id: str, *, tags: tuple[str, ...] = (), messages: tuple[str, ...] = ()
    ) -> str:
        self.sessions[agora_id] = {"tags": set(tags), "messages": list(messages)}
        return agora_id

    def start(
        self,
        *,
        mode: str,
        ids: Any,
        by: str,
        agent: str,
        workdir: str,
        tag: str,
        env: dict[str, str],
        merge_env: dict[str, str] | None = None,
    ) -> RunningSession:
        if self.fail_start is not None:
            error, self.fail_start = self.fail_start, None
            raise error
        self._counter += 1
        ids = list(ids)
        if mode == "new":
            session_id = f"agora:new-{self._counter}"
        elif mode == "continue":
            session_id = ids[0]
        elif mode == "fork":
            session_id = f"{ids[0]}-fork"
            # A fork copies the base Session's tags (the spec: 原有的 tags 保留).
            if session_id not in self.sessions:
                base = self.sessions.get(ids[0], {"tags": set(), "messages": []})
                self.sessions[session_id] = {
                    "tags": set(base["tags"]),
                    "messages": list(base["messages"]),
                }
        elif mode == "merge":
            session_id = f"agora:merged-{self._counter}"
        else:
            raise AssertionError(f"unknown mode {mode!r}")
        entry = self.sessions.setdefault(session_id, {"tags": set(), "messages": []})
        entry["tags"].add(tag)
        self.starts.append(
            {
                "mode": mode,
                "ids": ids,
                "by": by,
                "agent": agent,
                "workdir": workdir,
                "tag": tag,
                "env": dict(env),
                "merge_env": None if merge_env is None else dict(merge_env),
                "session_id": session_id,
            }
        )
        return RunningSession(session_id=session_id, process=f"fake-process:{session_id}")


class FakeSubscriptionSource:
    """SubscriptionSource double: values in, values out, no exceptions (D3).

    Failures are scripted per call: `list_unreachable`, `get_errors`,
    `get_unreachable`, `cooldown_results` (popped per call; None means OK).
    """

    def __init__(self, subscriptions: Any = ()) -> None:
        self._subs: dict[str, Subscription] = {}
        for sub in subscriptions:
            self._subs[sub.id] = sub
        self.cooldowns: list[tuple[str, str, str | None]] = []
        self.lists = 0
        self.gets: list[tuple[str, str]] = []
        self.list_unreachable: str | None = None
        self.get_unreachable = False
        self.get_errors: dict[str, str] = {}
        self.cooldown_results: list[Any] = []
        self.cooldown_unreachable = False
        self.cooldown_delay = 0.0  # slow cooldown, for the concurrency tests (H2)
        self.operations = 0  # one logical operation = one source.operation()

    def operation(self) -> Any:
        """Counts logical operations so tests can assert one per open/swap (S1)."""
        self.operations += 1
        return nullcontext(self)

    def add(self, sub: Subscription) -> Subscription:
        self._subs[sub.id] = sub
        return sub

    def set_available(self, subscription_id: str, available: bool) -> None:
        self._subs[subscription_id].available = available

    def list(self) -> list[Subscription] | Unreachable:
        self.lists += 1
        if self.list_unreachable:
            return Unreachable(self.list_unreachable)
        out = []
        for sub in self._subs.values():
            row = copy.copy(sub)
            row.key = None
            row.raw = {}
            out.append(row)
        return out

    def get(self, subscription_id: str, agent: str) -> Subscription | SourceError | Unreachable:
        self.gets.append((subscription_id, agent))
        if self.get_unreachable:
            return Unreachable("fake get cannot reach the gateway")
        code = self.get_errors.get(subscription_id)
        if code:
            return SourceError(code)
        sub = self._subs.get(subscription_id)
        if sub is None:
            return SourceError("not_found")
        if agent not in sub.agents:
            return SourceError("agent_not_allowed")
        return copy.copy(sub)

    def cooldown(self, subscription_id: str, window: str, resets_at: str | None) -> Any:
        self.cooldowns.append((subscription_id, window, resets_at))
        if self.cooldown_delay:
            import time

            time.sleep(self.cooldown_delay)
        if self.cooldown_results:
            scripted = self.cooldown_results.pop(0)
            if scripted is not None:
                return scripted
        elif self.cooldown_unreachable:
            return Unreachable("fake cooldown cannot reach the gateway")
        sub = self._subs.get(subscription_id)
        if sub is not None:
            sub.available = False
            sub.cooldown = {**sub.cooldown, window: resets_at}
        return None


def claude_subscription(
    subscription_id: str = "claude-personal",
    *,
    key: str | None = "key-personal",
    priority: int = 10,
    available: bool = True,
    agents: tuple[str, ...] = ("claude-code",),
    cooldown: dict[str, str | None] | None = None,
) -> Subscription:
    return Subscription(
        id=subscription_id,
        provider="anthropic",
        kind="claude-subscription",
        agents=agents,
        priority=priority,
        key=key,
        cooldown=cooldown or {},
        available=available,
    )


def default_subscriptions() -> list[Subscription]:
    return [
        claude_subscription("claude-personal", key="key-personal", priority=10),
        claude_subscription("claude-company", key="key-company", priority=20),
    ]


# --- lgw.connect / lgw.client doubles (task 2.5) -------------------------------


class FakeConfigError(Exception):
    """Stands in for lgw.connect.ConfigError."""


class FakeUnavailable(Exception):
    """Stands in for lgw.connect.Unavailable (raised when entering the with)."""

    def __init__(self, kind: str = "timeout") -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass
class FakeResult:
    """lgw.client.Result-shaped answer: exactly the real fields (review H1)."""

    ok: bool = True
    status: int = 200
    data: Any = None
    error: str | None = None
    unreachable: bool = False


class FakeGatewayClient:
    """lgw.client double: Result-shaped answers, recorded calls, scripted failures.

    `results` overrides a whole operation with a `FakeResult` (or a callable
    taking the payload and returning one).  Defaults: `list` answers the rows,
    `get` answers `get_row` (404 when it is None), `cooldown` answers success.
    """

    def __init__(
        self,
        *,
        rows: Any = (),
        get_row: dict[str, Any] | None = None,
        results: dict[str, Any] | None = None,
    ) -> None:
        self.rows = list(rows)
        self.get_row = get_row
        self.results = dict(results or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.base_url: str | None = None  # set by the factory

    def _answer(self, op: str, payload: dict[str, Any]) -> FakeResult:
        self.calls.append((op, dict(payload)))
        scripted = self.results.get(op)
        if callable(scripted):
            return scripted(payload)
        if scripted is not None:
            return scripted
        if op == "list":
            return FakeResult(data={"schema": 1, "subscriptions": copy.deepcopy(self.rows)})
        if op == "get":
            if self.get_row is None:
                return FakeResult(ok=False, status=404, error="not_found")
            return FakeResult(data=copy.deepcopy(self.get_row))
        return FakeResult(data={})

    def list(self) -> FakeResult:
        return self._answer("list", {})

    def get(self, subscription_id: str, agent: str) -> FakeResult:
        return self._answer("get", {"id": subscription_id, "agent": agent})

    def cooldown(self, subscription_id: str, window: str, resets_at: str | None) -> FakeResult:
        return self._answer(
            "cooldown", {"id": subscription_id, "window": window, "resets_at": resets_at}
        )


class FakeConnect:
    """lgw.connect double: load_config(path) + connect(cfg) context manager.

    `load_config` reads the real file every time, so a test can atomically
    rewrite it between operations and watch the next operation pick it up.
    `fail_enter` makes the *entering* of the `with` raise a FakeUnavailable
    (once, then it clears) - the real error shape H1 covers.
    """

    ConfigError = FakeConfigError

    def __init__(
        self,
        *,
        base_url: str = "fake://gateway",
        bad_paths: Any = (),
        fail_enter: Exception | None = None,
    ) -> None:
        self.base_url = base_url
        self.bad_paths = {str(p) for p in bad_paths}
        self.fail_enter = fail_enter
        self.configs: list[str] = []
        self.opens: list[dict[str, Any]] = []
        self.closed = 0

    def load_config(self, path: str | Path) -> dict[str, Any]:
        path = str(path)
        self.configs.append(path)
        if path in self.bad_paths:
            raise FakeConfigError(f"bad connect file {path}")
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @contextmanager
    def connect(self, cfg: dict[str, Any]) -> Iterator[str]:
        self.opens.append(dict(cfg))
        if self.fail_enter is not None:
            error, self.fail_enter = self.fail_enter, None
            raise error
        try:
            yield cfg.get("base_url", self.base_url)
        finally:
            self.closed += 1


class FakeClientFactory:
    """Hands one FakeGatewayClient to every connection, recording base_urls."""

    def __init__(self, client: FakeGatewayClient) -> None:
        self.client = client
        self.base_urls: list[str] = []

    def __call__(self, base_url: str) -> FakeGatewayClient:
        self.base_urls.append(base_url)
        self.client.base_url = base_url
        return self.client
