"""In-memory fakes for the sidecar seams (change sidecar-v0, tasks 2.2-2.6).

These stand in for Claude Code / opencode drivers, for agora's session store,
for LLMGateway and for its transport, so every unit test runs without opening
a real agent, touching Drive or touching the network.
"""

from __future__ import annotations

import copy
from typing import Any

from sidecar.protocols import (
    DRIVER_EXITED,
    DRIVER_IDLE,
    DRIVER_RUNNING,
    DriverState,
    PreparedSession,
    Subscription,
    SubscriptionError,
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

    def __init__(self, session: PreparedSession) -> None:
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
        self.launches: list[tuple[PreparedSession, dict[str, str], str]] = []
        self.handles: list[FakeHandle] = []
        self.stop_calls: list[tuple[FakeHandle, bool]] = []

    # -- AgentDriver ------------------------------------------------------

    def env_for(self, subscription: Subscription) -> dict[str, str]:
        env = {}
        if subscription.key:
            env["FAKE_AGENT_KEY"] = subscription.key
        if subscription.endpoint:
            env["FAKE_AGENT_ENDPOINT"] = subscription.endpoint
        return env

    def launch(self, session: PreparedSession, env: dict[str, str], workdir: str) -> FakeHandle:
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
        if handle.stopped and handle.state.kind != DRIVER_EXITED:
            return DriverState(kind=DRIVER_EXITED, code=0, reason="stopped")
        return handle.state

    def stop(self, handle: FakeHandle, *, graceful: bool = True, timeout: float = 10.0) -> None:
        self.stop_calls.append((handle, graceful))
        handle.stopped = True
        handle.busy = False
        handle.state = DriverState(kind=DRIVER_EXITED, code=0, reason="closed")

    # -- test helpers -----------------------------------------------------

    def force_state(self, handle: FakeHandle, kind: str, **fields: Any) -> None:
        handle.state = DriverState(kind=kind, **fields)

    def external_handle(self) -> FakeHandle:
        """An agent this sidecar did not open (must never show up in status)."""
        return FakeHandle(PreparedSession(agora_id="agora:not-ours", mode="new"))


class FakeSessionStore:
    """SessionStore double; keeps a tiny in-memory agora."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.starts: list[dict[str, Any]] = []
        self.syncs: list[str] = []
        self._counter = 0

    def seed(self, agora_id: str, *, tags: tuple[str, ...] = (), messages: tuple[str, ...] = ()) -> str:
        self.sessions[agora_id] = {"tags": set(tags), "messages": list(messages)}
        return agora_id

    def start(
        self,
        *,
        mode: str,
        ids: Any,
        agent: str,
        workdir: str,
        tag: str,
    ) -> PreparedSession:
        self._counter += 1
        ids = list(ids)
        if mode == "new":
            agora_id = f"agora:new-{self._counter}"
        elif mode == "continue":
            agora_id = ids[0]
        elif mode == "fork":
            agora_id = f"{ids[0]}-fork"
        elif mode == "merge":
            agora_id = f"agora:merged-{self._counter}"
        else:
            raise AssertionError(f"unknown mode {mode!r}")
        entry = self.sessions.setdefault(agora_id, {"tags": set(), "messages": []})
        entry["tags"].add(tag)
        self.starts.append(
            {
                "mode": mode,
                "ids": ids,
                "agent": agent,
                "workdir": workdir,
                "tag": tag,
                "agora_id": agora_id,
            }
        )
        argv = ("fake-agora", "continue", agora_id, "--agent", agent, "--headless", "--tag", tag)
        return PreparedSession(agora_id=agora_id, mode=mode, ids=tuple(ids), argv=argv)

    def sync(self, session: PreparedSession) -> None:
        self.syncs.append(session.agora_id)


class FakeSubscriptionSource:
    """SubscriptionSource double; cooldowns are in memory only."""

    def __init__(self, subscriptions: Any = ()) -> None:
        self._subs: dict[str, Subscription] = {}
        for sub in subscriptions:
            self._subs[sub.id] = sub
        self.cooldowns: list[tuple[str, str, float]] = []
        self.lists = 0
        self.gets: list[tuple[str, str]] = []

    def add(self, sub: Subscription) -> Subscription:
        self._subs[sub.id] = sub
        return sub

    def set_available(self, subscription_id: str, available: bool) -> None:
        self._subs[subscription_id].available = available

    def list(self) -> list[Subscription]:
        self.lists += 1
        out = []
        for sub in self._subs.values():
            row = copy.copy(sub)
            row.key = None
            row.raw = {}
            out.append(row)
        return out

    def get(self, subscription_id: str, agent: str) -> Subscription:
        self.gets.append((subscription_id, agent))
        sub = self._subs.get(subscription_id)
        if sub is None:
            raise SubscriptionError(f"unknown subscription {subscription_id!r}")
        if agent not in sub.agents:
            raise SubscriptionError(f"subscription {subscription_id!r} does not allow {agent!r}")
        return copy.copy(sub)

    def cooldown(self, subscription_id: str, window: str, resets_at: float) -> None:
        self.cooldowns.append((subscription_id, window, float(resets_at)))
        sub = self._subs.get(subscription_id)
        if sub is not None:
            sub.available = False
            sub.cooldown = {**sub.cooldown, window: float(resets_at)}


def claude_subscription(
    subscription_id: str = "claude-personal",
    *,
    key: str | None = "key-personal",
    priority: int = 10,
    available: bool = True,
    agents: tuple[str, ...] = ("claude-code",),
    cooldown: dict[str, float | None] | None = None,
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


class FakeTransport:
    """LLMGatewayTransport double: canned answers, recorded calls, injectable errors."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, dict(payload)))
        if op in self.errors:
            raise self.errors[op]
        response = self.responses.get(op)
        if callable(response):
            return response(payload)
        return response
