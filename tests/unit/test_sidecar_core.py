"""The four actions over fake seams (change sidecar-v0, tasks 2.3/2.6).

Every scenario here runs without a real agent, without Drive and without the
network: the driver, the session store and the subscription source are the
in-memory fakes in `_sidecar_fakes.py`.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import logging

import pytest

import _sidecar_fakes as F
from sidecar.core import SidecarCore
from sidecar.protocols import (
    BadRequestError,
    ConflictError,
    DRIVER_EXITED,
    NotFoundError,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    SubscriptionError,
)


def rig(*, subs=None):
    clock = F.FakeClock()
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource(subs if subs is not None else F.default_subscriptions())
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    return core, driver, store, source, clock


# --- open: fork a home Session (spec: fork 一個 team 的 home Session) ---------


def test_fork_open_tags_new_session_and_sends_first_input():
    core, driver, store, source, _clock = rig()
    store.seed("agora:H", tags=("home",))

    rec = core.open(
        "t1",
        "claude-code",
        "/work/t1",
        {"mode": "fork", "from": "agora:H"},
        subscription={"id": "claude-company"},
        input="讀 channel X",
    )

    assert rec.state == STATE_RUNNING
    assert rec.session.agora_id == "agora:H-fork"
    assert rec.subscription == "claude-company"
    # The fork carries the tag; the home Session H is untouched.
    assert store.sessions["agora:H"]["tags"] == {"home"}
    assert "sidecar:t1" in store.sessions["agora:H-fork"]["tags"]
    assert store.starts[0]["mode"] == "fork"
    assert store.starts[0]["ids"] == ["agora:H"]
    assert store.starts[0]["tag"] == "sidecar:t1"
    # The caller only gave the subscription id; the sidecar fetched the row.
    assert source.gets == [("claude-company", "claude-code")]
    # The first input reached the agent.
    assert driver.handles[0].inbox == ["讀 channel X"]
    # And it went to the child environment, never the command line.
    assert driver.launches[0][1] == {"FAKE_AGENT_KEY": "key-company"}
    assert "key-company" not in " ".join(driver.launches[0][0].argv)


def test_open_omitting_subscription_uses_the_selector():
    core, driver, store, source, _clock = rig()
    rec = core.open("t1", "claude-code", "/w", {"mode": "new"})
    assert rec.subscription == "claude-personal"  # priority 10 beats 20
    assert rec.state == STATE_IDLE
    assert source.gets == [("claude-personal", "claude-code")]


def test_merge_mode_passes_every_id():
    core, _driver, store, _source, _clock = rig()
    store.seed("agora:A")
    store.seed("agora:B")
    rec = core.open("t1", "claude-code", "/w", {"mode": "merge", "from": ["agora:A", "agora:B"]})
    assert store.starts[0]["mode"] == "merge"
    assert store.starts[0]["ids"] == ["agora:A", "agora:B"]
    assert rec.session.agora_id == "agora:merged-1"


# --- open: id rules (spec: id 重複 / 重開已結束的 id) -------------------------


def test_duplicate_id_is_rejected_and_opens_nothing():
    core, driver, store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"}, input="go")
    with pytest.raises(ConflictError):
        core.open("t1", "claude-code", "/w", {"mode": "new"})
    assert len(driver.launches) == 1
    assert len(store.starts) == 1


def test_duplicate_id_while_starting_is_rejected():
    core, _driver, _store, _source, _clock = rig()
    rec = core.open("t1", "claude-code", "/w", {"mode": "new"})
    rec.state = "starting"
    with pytest.raises(ConflictError):
        core.open("t1", "claude-code", "/w", {"mode": "new"})


def test_reopen_exited_id_continues_the_same_session():
    core, driver, store, _source, _clock = rig()
    store.seed("agora:A", messages=("earlier",))
    core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"})
    core.close("t1")
    assert core.get("t1").state == STATE_EXITED

    rec = core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"})
    assert rec.state == STATE_IDLE
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:A"]
    assert len(driver.launches) == 2


def test_open_rejects_unknown_agent_kind_and_bad_session():
    core, _driver, _store, _source, _clock = rig()
    with pytest.raises(BadRequestError):
        core.open("t1", "not-an-agent", "/w", {"mode": "new"})
    with pytest.raises(BadRequestError):
        core.open("t1", "claude-code", "/w", {"mode": "resume"})
    with pytest.raises(BadRequestError):
        core.open("t1", "claude-code", "/w", {"mode": "continue"})
    with pytest.raises(BadRequestError):
        core.open("bad/id", "claude-code", "/w", {"mode": "new"})


def test_open_without_any_available_subscription_is_rejected():
    cooling = F.claude_subscription("claude-personal", available=False)
    core, _driver, _store, _source, _clock = rig(subs=[cooling])
    with pytest.raises(ConflictError):
        core.open("t1", "claude-code", "/w", {"mode": "new"})


def test_open_with_a_subscription_that_refuses_the_agent_is_rejected():
    sub = F.claude_subscription("claude-personal", agents=("opencode",))
    core, _driver, _store, _source, _clock = rig(subs=[sub])
    with pytest.raises(SubscriptionError):
        core.open("t1", "claude-code", "/w", {"mode": "new"}, subscription={"id": "claude-personal"})


# --- close (spec: 關閉) -------------------------------------------------------


def test_close_stops_gracefully_syncs_and_shows_exited():
    core, driver, store, _source, _clock = rig()
    store.seed("agora:A")
    core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"}, input="go")

    rec = core.close("t1")

    assert rec.state == STATE_EXITED
    assert store.syncs == ["agora:A"]
    handle, graceful = driver.stop_calls[0]
    assert handle is driver.handles[0]
    assert graceful is True
    assert core.get("t1").state == STATE_EXITED
    with pytest.raises(NotFoundError):
        core.close("nope")


def test_close_is_idempotent_on_an_exited_agent():
    core, driver, _store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"})
    core.close("t1")
    rec = core.close("t1")
    assert rec.state == STATE_EXITED
    assert len(driver.stop_calls) == 1


# --- poke (spec: 暫停就是戳加中斷 / 不打斷) -----------------------------------


def test_pause_is_poke_with_interrupt():
    core, driver, _store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"}, input="工作中")
    handle = driver.handles[0]
    assert handle.busy  # a round is running

    core.poke("t1", "停下來等指示", interrupt=True)

    assert handle.interrupts == 1
    # The interrupted round is over, so the words are the next input, not a queue.
    assert handle.inbox == ["工作中", "停下來等指示"]
    assert handle.queued == []


def test_poke_without_interrupt_waits_for_the_round():
    core, driver, _store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"}, input="第一輪")
    handle = driver.handles[0]

    core.poke("t1", "第二則")

    assert handle.interrupts == 0
    assert handle.inbox == ["第一輪"]
    assert handle.queued == ["第二則"]
    handle.finish_round()
    assert handle.inbox == ["第一輪", "第二則"]
    assert core.get("t1").state == STATE_IDLE


def test_poke_on_a_stopped_agent_is_rejected():
    core, _driver, _store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"})
    core.close("t1")
    with pytest.raises(ConflictError):
        core.poke("t1", "hello")
    with pytest.raises(BadRequestError):
        core.poke("t1", "")


# --- status (spec: 不列別人的) ------------------------------------------------


def test_status_lists_only_agents_this_sidecar_opened():
    core, driver, _store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"})
    driver.external_handle()  # somebody else's Claude Code on the same machine

    listed = core.list_agents()

    assert [rec.id for rec in listed] == ["t1"]
    payload = [rec.to_dict() for rec in listed]
    assert payload[0]["id"] == "t1"
    assert set(payload[0]) >= {"id", "agent", "session", "subscription", "state", "started_at", "exit"}
    assert "key" not in json.dumps(payload)


def test_driver_reported_exit_shows_up_in_status():
    core, driver, _store, _source, _clock = rig()
    core.open("t1", "claude-code", "/w", {"mode": "new"})
    driver.force_state(driver.handles[0], DRIVER_EXITED, code=3, reason="crashed")

    rec = core.get("t1")

    assert rec.state == STATE_EXITED
    assert rec.exit == {"code": 3, "reason": "crashed"}


# --- shutdown (design D2: sidecar 結束時先 close 全部) ------------------------


def test_shutdown_closes_every_open_agent():
    core, driver, store, _source, _clock = rig()
    store.seed("agora:A")
    store.seed("agora:B")
    core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"})
    core.open("t2", "claude-code", "/w", {"mode": "continue", "from": "agora:B"})
    core.close("t2")

    core.shutdown()

    assert core.get("t1").state == STATE_EXITED
    assert core.get("t2").state == STATE_EXITED
    assert sorted(store.syncs) == ["agora:A", "agora:B"]
    assert len(driver.stop_calls) == 2  # t2 was already closed; no second stop


# --- volatile state (spec: 重啟) ---------------------------------------------


def test_a_fresh_core_starts_empty_but_agora_keeps_the_memory():
    _core, driver, store, source, clock = rig()
    store.seed("agora:A")
    core1 = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    core1.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"})

    core2 = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )

    assert core2.list_agents() == []
    assert core2.list_jobs() == []
    assert "agora:A" in store.sessions  # the conversation is still in agora


# --- environment values stay out of the command line and the log (task 2.6) ---


def test_subscription_secret_only_reaches_the_child_env(caplog):
    secret = "SUPER-SECRET-KEY"
    sub = F.claude_subscription("claude-personal", key=secret)
    core, driver, store, _source, _clock = rig(subs=[sub])
    store.seed("agora:A")

    with caplog.at_level(logging.DEBUG):
        core.open("t1", "claude-code", "/w", {"mode": "continue", "from": "agora:A"},
                  subscription={"id": "claude-personal"}, input="go")
        core.poke("t1", "more")
        core.close("t1")
        core.refresh_states()

    assert secret not in caplog.text
    session, env, _workdir = driver.launches[0]
    assert env["FAKE_AGENT_KEY"] == secret  # the env is how it travels
    assert secret not in " ".join(session.argv)
    assert secret not in json.dumps([rec.to_dict() for rec in core.list_agents()])
