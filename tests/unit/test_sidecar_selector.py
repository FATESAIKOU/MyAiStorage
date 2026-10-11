"""PrioritySelector and the fakes against the Protocols (change sidecar-v0, task 2.2)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import _sidecar_fakes as F
from sidecar.protocols import (
    AgentDriver,
    Selector,
    SessionStore,
    Subscription,
    SubscriptionSource,
)
from sidecar.selectors import PrioritySelector


def sub(sid, *, priority=10, available=True, agents=("claude-code",)):
    return Subscription(id=sid, priority=priority, available=available, agents=agents)


def test_choose_follows_priority():
    selector = PrioritySelector()
    assert selector.choose("claude-code", [sub("slow", priority=20), sub("fast", priority=5)]) == "fast"


def test_choose_skips_unavailable_and_wrong_agent():
    selector = PrioritySelector()
    assert (
        selector.choose(
            "claude-code",
            [sub("cooling", available=False), sub("other-kind", agents=("opencode",))],
        )
        is None
    )


def test_equal_priority_sorts_by_id():
    selector = PrioritySelector()
    assert selector.choose("claude-code", [sub("b"), sub("a")]) == "a"


def test_excluded_ids_are_not_candidates():
    selector = PrioritySelector()
    assert selector.choose("claude-code", [sub("a"), sub("b")], exclude=["a"]) == "b"
    assert selector.choose("claude-code", [sub("a")], exclude=["a"]) is None


def test_fakes_satisfy_the_protocols():
    assert isinstance(F.FakeAgentDriver(), AgentDriver)
    assert isinstance(F.FakeSessionStore(), SessionStore)
    assert isinstance(F.FakeSubscriptionSource(), SubscriptionSource)
    assert isinstance(PrioritySelector(), Selector)
