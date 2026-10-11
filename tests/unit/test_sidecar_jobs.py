"""Job scheduling and the quota switch (change sidecar-v0, tasks 2.1/2.4/2.6).

`at` opens an agent at a wall-clock time; `every` polls with an optional
`until`; quota exhaustion switches subscriptions or waits with an auto-reopen
job - all over the in-memory fakes.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import pytest

import _sidecar_fakes as F
from sidecar.core import SidecarCore
from sidecar.jobs import parse_duration, parse_time
from sidecar.protocols import (
    BadRequestError,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_WAITING_QUOTA,
)


def rig(*, subs=None):
    clock = F.FakeClock(1000.0)
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource(subs if subs is not None else F.default_subscriptions())
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    return core, driver, store, source, clock


# --- the job forms -----------------------------------------------------------


def test_at_job_is_visible_before_and_opens_at_due_time():
    core, _driver, _store, _source, clock = rig()
    job = core.add_job(
        "open",
        {"id": "t1", "agent": "claude-code", "workdir": "/w", "session": {"mode": "new"}},
        at=clock.t + 3600,
    )

    assert [j.id for j in core.list_jobs()] == [job.id]
    assert job.to_dict()["at"] == "1970-01-01T01:16:40Z"  # 1000 + 3600 epoch seconds

    core.tick()
    assert core.list_agents() == []  # not due yet

    clock.advance(3600)
    core.tick()
    assert [rec.id for rec in core.list_agents()] == ["t1"]
    assert core.list_jobs() == []  # a one-shot job is consumed


def test_every_job_runs_until():
    core, _driver, _store, _source, clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"})
    core.add_job(
        "poke",
        {"id": "t1", "text": "poll"},
        every=60,
        until=clock.t + 100,
    )

    clock.advance(60)
    core.tick()
    handle = core.get("t1").handle
    assert handle.inbox == ["poll"]

    # 1 → next would be 1120, past the 1100 until: the job is dropped.
    assert core.list_jobs() == []
    clock.advance(1000)
    core.tick()
    assert handle.inbox == ["poll"]


def test_every_job_reschedules_before_until():
    core, _driver, _store, _source, clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"})
    job = core.add_job("poke", {"id": "t1", "text": "poll"}, every=60, until=clock.t + 3600)

    clock.advance(60)
    core.tick()
    assert [j.id for j in core.list_jobs()] == [job.id]
    handle = core.get("t1").handle
    handle.finish_round()  # the first polled round ends before the next one
    clock.advance(60)
    core.tick()
    assert handle.inbox == ["poll", "poll"]


def test_delete_job():
    core, _driver, _store, _source, clock = rig()
    job = core.add_job(
        "open",
        {"id": "t1", "agent": "claude-code", "workdir": "/w", "session": {"mode": "new"}},
        at=clock.t + 10,
    )
    assert core.delete_job(job.id) is True
    assert core.delete_job(job.id) is False
    clock.advance(10)
    core.tick()
    assert core.list_agents() == []


def test_add_job_validation():
    core, _driver, _store, _source, clock = rig()
    args = {"id": "t1", "agent": "claude-code", "workdir": "/w", "session": {"mode": "new"}}
    with pytest.raises(BadRequestError):
        core.add_job("pause", args, at=clock.t + 1)
    with pytest.raises(BadRequestError):
        core.add_job("open", args)  # neither at nor every
    with pytest.raises(BadRequestError):
        core.add_job("open", args, at=clock.t + 1, every=60)
    with pytest.raises(BadRequestError):
        core.add_job("poke", {"id": "t1", "text": "x"}, every=0)
    with pytest.raises(BadRequestError):
        core.add_job("poke", {"id": "t1", "text": "x"}, at=clock.t + 1, until=clock.t + 9)
    with pytest.raises(BadRequestError):
        core.add_job("poke", {"id": "t1"}, every=60)  # text missing
    with pytest.raises(BadRequestError):
        core.add_job("close", {"id": "t1", "text": "x"}, every=60)  # extra key


def test_a_failed_job_is_dropped_without_breaking_the_scheduler():
    core, driver, _store, _source, clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"}, input="busy")
    core.add_job(
        "open",
        {"id": "t1", "agent": "claude-code", "workdir": "/w", "session": {"mode": "new"}},
        at=clock.t + 1,
    )
    clock.advance(1)
    core.tick()  # the open conflicts (t1 running): the job is logged and dropped

    assert core.list_jobs() == []
    assert len(driver.launches) == 1
    assert core.get("t1").state == STATE_RUNNING


def test_time_and_duration_parsing():
    assert parse_time(1500) == 1500.0
    assert parse_time("2030-01-01T00:00:00Z") > 0
    assert parse_duration(90) == 90.0
    assert parse_duration("5m") == 300.0
    assert parse_duration("2h") == 7200.0
    with pytest.raises(BadRequestError):
        parse_time("tomorrow 9:00")
    with pytest.raises(BadRequestError):
        parse_duration("soon")
    with pytest.raises(BadRequestError):
        parse_duration(True)


# --- quota: switch to the next subscription (spec: 換到另一把) ---------------


def test_quota_exhaustion_switches_to_the_next_subscription():
    core, driver, store, source, clock = rig()
    store.seed("agora:A")
    core.open(
        "t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"},
        subscription={"id": "claude-personal"}, input="go",
    )
    first = driver.handles[0]
    driver.force_state(first, "quota_exhausted", window="5h", resets_at=clock.t + 3600)

    core.tick()

    assert source.cooldowns == [("claude-personal", "5h", clock.t + 3600)]
    # Stop gracefully, write the conversation back, then continue the same Session.
    assert driver.stop_calls == [(first, True)]
    assert store.syncs == ["agora:A"]
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:A"]
    assert store.starts[-1]["tag"] == "sidecar:t1"
    assert driver.launches[-1][0].agora_id == "agora:A"
    assert driver.launches[-1][1] == {"FAKE_AGENT_KEY": "key-company"}

    rec = core.get("t1")
    assert rec.id == "t1"  # id unchanged
    assert rec.subscription == "claude-company"
    assert rec.state == STATE_IDLE
    assert rec.session.agora_id == "agora:A"
    assert core.list_jobs() == []


# --- quota: everything cooling (spec: 全部冷卻) ------------------------------


def test_all_cooling_waits_and_schedules_an_auto_reopen_job():
    personal = F.claude_subscription("claude-personal", key="key-personal", priority=10)
    company = F.claude_subscription(
        "claude-company", key="key-company", priority=20,
        available=False, cooldown={"1w": 9000.0},
    )
    core, driver, store, source, clock = rig(subs=[personal, company])
    store.seed("agora:A")
    core.open(
        "t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"},
        subscription={"id": "claude-personal"}, input="go",
    )
    first = driver.handles[0]
    driver.force_state(first, "quota_exhausted", window="5h", resets_at=5000.0)

    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA
    assert rec.reopen_at == 5000.0  # earlier than company's 9000
    assert driver.stop_calls == [(first, True)]
    assert store.syncs == ["agora:A"]
    assert rec.to_dict()["reopen_at"] == "1970-01-01T01:23:20Z"

    jobs = core.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.auto is True
    assert job.action == "open"
    assert job.args == {
        "id": "t1",
        "agent": "claude-code",
        "workdir": "/w",
        "session": {"mode": "continue", "from": "agora:A"},
    }
    assert "subscription" not in job.args
    assert job.next_run_at == 5000.0


def test_auto_reopen_job_opens_when_the_cooldown_lifts():
    personal = F.claude_subscription("claude-personal", key="key-personal", priority=10)
    company = F.claude_subscription(
        "claude-company", key="key-company", priority=20,
        available=False, cooldown={"1w": 9000.0},
    )
    core, driver, store, source, clock = rig(subs=[personal, company])
    store.seed("agora:A")
    core.open(
        "t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"},
        subscription={"id": "claude-personal"}, input="go",
    )
    driver.force_state(driver.handles[0], "quota_exhausted", window="5h", resets_at=5000.0)
    core.tick()
    assert core.get("t1").state == STATE_WAITING_QUOTA

    # At the reopen moment the selector must freshly pick (Q8): company is back.
    source.set_available("claude-company", True)
    clock.t = 5000.0
    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_IDLE
    assert rec.subscription == "claude-company"
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:A"]
    assert core.list_jobs() == []  # the auto job was consumed
    assert len(driver.launches) == 2


def test_reopening_a_waiting_quota_id_deletes_its_auto_job():
    core, driver, store, source, clock = rig()
    store.seed("agora:A")
    core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"})
    driver.force_state(
        driver.handles[0], "quota_exhausted",
        window="5h", resets_at=clock.t + 100,
    )
    source.set_available("claude-company", False)
    core.tick()
    assert core.get("t1").state == STATE_WAITING_QUOTA
    assert len(core.list_jobs()) == 1

    # The caller re-opens the same id with an explicit subscription (D1).
    source.set_available("claude-company", True)
    rec = core.open(
        "t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"},
        subscription={"id": "claude-company"},
    )

    assert rec.subscription == "claude-company"
    assert core.list_jobs() == []  # the auto-reopen job went with the old record


def test_closing_a_waiting_quota_agent_deletes_its_auto_job():
    core, driver, store, source, clock = rig()
    store.seed("agora:A")
    core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"})
    driver.force_state(
        driver.handles[0], "quota_exhausted",
        window="5h", resets_at=clock.t + 100,
    )
    source.set_available("claude-company", False)
    core.tick()
    assert core.get("t1").state == STATE_WAITING_QUOTA

    rec = core.close("t1")

    assert rec.state == STATE_EXITED
    assert core.list_jobs() == []
