"""#531: the write-back period runs from a round's end to the next round's start (design P9).

`_wait_agent` is driven directly. A fake agent and a fake clock stand in for the process
and `time.monotonic`, and `_periodic_write` is replaced by a round that costs a set time
on that clock. Nothing sleeps, so the numbers are exact.

C-1: the next round starts no sooner than this round's end plus the interval.
C-2: a forwarded stop takes the agent down at the same point as before (the deadline is
     checked between waits, and `_wait_limit` is unchanged).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import subprocess

import pytest

from agora import cli

INTERVAL = 1.0


class FakeClock:
    """Stands in for the `time` module inside cli: only `monotonic` is used there."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class FakeAgent:
    """Ends at `ends_at` on the fake clock. A wait moves the clock up to its timeout."""

    pid = None

    def __init__(self, clock: FakeClock, ends_at: float) -> None:
        self.clock = clock
        self.ends_at = ends_at
        self.returncode = None

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if timeout is not None and timeout < self.ends_at - self.clock.now:
            self.clock.now += timeout
            raise subprocess.TimeoutExpired("agent", timeout)
        self.clock.now = max(self.clock.now, self.ends_at)
        self.returncode = 0
        return 0

    def poll(self):
        if self.returncode is None and self.clock.now >= self.ends_at:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class Rounds:
    """A round that costs `cost` seconds of fake time; it records (start, end) of each."""

    def __init__(self, clock: FakeClock, cost: float) -> None:
        self.clock = clock
        self.cost = cost
        self.spans: list[tuple[float, float]] = []

    def __call__(self, run, agent) -> None:
        start = self.clock.now
        self.clock.now += self.cost
        self.spans.append((start, self.clock.now))


def drive(monkeypatch, cost: float, ends_at: float, stop=None):
    clock = FakeClock()
    monkeypatch.setattr(cli, "time", clock)
    rounds = Rounds(clock, cost)
    monkeypatch.setattr(cli, "_periodic_write", rounds)
    agent = FakeAgent(clock, ends_at)
    cli._wait_agent(agent, None, None, INTERVAL, stop)
    return clock, rounds.spans, agent


@pytest.mark.parametrize("cost", [1.5 * INTERVAL, 2.5 * INTERVAL])
def test_a_slow_round_is_followed_by_a_full_rest(monkeypatch, cost):
    """C-1: a round that takes longer than the period is followed by a whole period of rest.

    With `due` set before the round (the old order), the next round starts the moment
    this one ends: the gap between the ends and the starts is 0, not the interval.
    """
    _clock, spans, agent = drive(monkeypatch, cost, ends_at=8 * cost)

    assert len(spans) >= 2, spans
    for (start, _end), (next_start, next_end) in zip(spans, spans[1:]):
        gap = next_start - (start + cost)
        assert gap >= INTERVAL - 1e-9, f"rest before the next round was {gap}, not >= {INTERVAL}"
    assert agent.returncode == 0


def test_a_fast_round_keeps_the_period(monkeypatch):
    """C-1 (the usual case): a round much shorter than the period leaves the rhythm alone.

    Rounds start one period plus their own (tiny) cost apart - not drifting further, and
    not bunched up.
    """
    cost = 0.01 * INTERVAL
    _clock, spans, _agent = drive(monkeypatch, cost, ends_at=6 * INTERVAL)

    starts = [s for s, _e in spans]
    assert len(starts) >= 4, starts
    for start, next_start in zip(starts, starts[1:]):
        assert next_start - start == pytest.approx(INTERVAL + cost, abs=1e-9)


@pytest.mark.parametrize("grace", ["0", "2.5"])
def test_a_forwarded_stop_takes_the_agent_down_at_its_deadline(monkeypatch, grace):
    """C-2: after a forwarded SIGTERM, take_down happens when the grace runs out.

    The round is fast, so the deadline is only checked between the waits, as before.
    With grace 0 the agent goes at once.
    """
    monkeypatch.setenv("AGORA_ORPHAN_GRACE", grace)
    stop = cli._GentleStop()
    clock = FakeClock()
    monkeypatch.setattr(cli, "time", clock)
    stop.forwarded()                              # the signal reached the agent at t=0
    deadline = float(grace)

    taken: list[float] = []
    real_take_down = stop.take_down

    def record(proc):
        taken.append(clock.now)
        real_take_down(proc)
    monkeypatch.setattr(stop, "take_down", record)

    rounds = Rounds(clock, 0.01 * INTERVAL)
    monkeypatch.setattr(cli, "_periodic_write", rounds)
    agent = FakeAgent(clock, ends_at=float("inf"))   # an agent that never leaves on its own

    cli._wait_agent(agent, None, None, INTERVAL, stop)

    assert taken == [pytest.approx(deadline)], taken
    assert agent.returncode == -9, "the agent was taken down, not waited on"
