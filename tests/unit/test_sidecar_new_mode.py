"""`new` mode: periodic imports, close/swap ordering (task 3.5, design D5).

No agora process: the driver starts the agent itself, and the conversation
reaches agora through `agora import session --external-session-id … --add-tag
…` - periodically, and again (twice) around a close or a swap.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import pytest

import _sidecar_fakes as F
from sidecar.core import SidecarCore
from sidecar.protocols import (
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_WAITING_QUOTA,
    SidecarError,
    iso,
)

EXTERNAL = "11111111-2222-4333-8444-555555555555"


def rig(tmp_path, *, subs=None, sync_period=60.0, retry_interval=300.0):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    clock = F.FakeClock()
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource(subs if subs is not None else F.default_subscriptions())
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock,
        sync_period=sync_period, retry_interval=retry_interval,
    )
    return core, driver, store, source, clock, str(workdir)


def open_new(core, driver, workdir, *, input="go"):
    core.open("t1", "claude-code", workdir, {"mode": "new"}, input=input)
    handle = driver.handles[0]
    handle.agent_session_id = None  # events have not told us yet
    return handle


def test_periodic_import_fills_status_session(tmp_path):
    core, driver, store, _source, clock, workdir = rig(tmp_path, sync_period=60.0)
    handle = open_new(core, driver, workdir)

    assert core.get("t1").session_id is None  # 第一次 import 之前沒有 agora id
    clock.advance(59)
    core.tick()
    assert store.imports == []  # not due yet

    handle.agent_session_id = EXTERNAL
    clock.advance(2)
    core.tick()
    assert store.imports == [
        {"external_id": EXTERNAL, "agent": "claude-code", "tag": "sidecar:t1"}
    ]
    rec = core.get("t1")
    assert rec.session_id == "agora:imported-1"
    assert rec.state == STATE_RUNNING  # the agent kept running

    clock.advance(60)
    core.tick()
    assert len(store.imports) == 2  # again after sync_period
    assert core.get("t1").session_id == "agora:imported-1"  # in place, same id


def test_swap_imports_then_continues(tmp_path):
    core, driver, store, _source, clock, workdir = rig(tmp_path)
    handle = open_new(core, driver, workdir)
    handle.agent_session_id = EXTERNAL
    driver.force_state(handle, "quota_exhausted", window="5h", resets_at=iso(clock.t + 3600))

    core.tick()

    # import -> stop -> import -> continue the imported agora id
    assert [i["external_id"] for i in store.imports] == [EXTERNAL, EXTERNAL]
    assert driver.stop_calls == [(handle, True, 30.0)]
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:imported-1"]
    rec = core.get("t1")
    assert rec.session_id == "agora:imported-1"
    assert rec.subscription == "claude-company"
    assert rec.state in (STATE_IDLE, STATE_RUNNING)
    assert rec.new_mode is False  # it runs under agora from now on
    assert core.list_jobs() == []


def test_all_cooling_gets_a_reopen_job_from_the_imported_session(tmp_path):
    personal = F.claude_subscription("claude-personal", key="key-personal")
    core, driver, store, _source, clock, workdir = rig(tmp_path, subs=[personal])
    handle = open_new(core, driver, workdir)
    handle.agent_session_id = EXTERNAL
    driver.force_state(handle, "quota_exhausted", window="5h", resets_at=iso(clock.t + 3600))

    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA
    assert rec.session_id == "agora:imported-1"  # the import gave us something to continue
    jobs = core.list_jobs()
    assert len(jobs) == 1 and jobs[0].args["session"] == {
        "mode": "continue", "from": "agora:imported-1"
    }
    assert rec.reopen_at == clock.t + 3600


def test_close_imports_stops_and_imports_again(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path)
    handle = open_new(core, driver, workdir)
    handle.agent_session_id = EXTERNAL

    rec = core.close("t1")

    assert [i["external_id"] for i in store.imports] == [EXTERNAL, EXTERNAL]
    assert driver.stop_calls == [(handle, True, 30.0)]
    assert rec.state == STATE_EXITED
    assert rec.session_id == "agora:imported-1"
    assert rec.exit and "closed" in rec.exit["reason"]


def test_close_without_an_agent_session_id_just_stops(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path)
    handle = open_new(core, driver, workdir)  # no events, no external id

    rec = core.close("t1")

    assert store.imports == []  # nothing to import
    assert driver.stop_calls == [(handle, True, 30.0)]
    assert rec.state == STATE_EXITED


def test_swap_import_failure_keeps_the_agent_and_retries(tmp_path):
    core, driver, store, _source, clock, workdir = rig(tmp_path, retry_interval=300.0)
    handle = open_new(core, driver, workdir)
    handle.agent_session_id = EXTERNAL
    clock.advance(60)
    core.tick()  # a periodic import gives us an agora id first
    assert core.get("t1").session_id == "agora:imported-1"

    store.fail_import = SidecarError("agora is not reachable")
    driver.force_state(handle, "quota_exhausted", window="5h", resets_at=iso(clock.t + 60))
    core.tick()

    assert driver.stop_calls == []  # MUST NOT stop on an import failure
    rec = core.get("t1")
    assert rec.state in (STATE_RUNNING, STATE_IDLE)
    assert rec.new_mode is True
    jobs = core.list_jobs()
    assert len(jobs) == 1 and jobs[0].auto is True
    assert jobs[0].args["session"] == {"mode": "continue", "from": "agora:imported-1"}
    assert jobs[0].next_run_at == clock.t + 300.0


def test_close_import_failure_still_closes_and_says_no_write_back(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path)
    handle = open_new(core, driver, workdir)
    handle.agent_session_id = EXTERNAL
    store.fail_import = SidecarError("agora blew up")

    rec = core.close("t1")

    assert driver.stop_calls == [(handle, True, 30.0)]  # still stopped
    assert rec.state == STATE_EXITED
    assert rec.exit and "no write-back" in rec.exit["reason"]
