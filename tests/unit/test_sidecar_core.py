"""The four actions over fake seams (change sidecar-v0, tasks 2.3/2.6).

Every scenario here runs without a real agent, without Drive and without the
network: the driver, the session store and the subscription source are the
in-memory fakes in `_sidecar_fakes.py`.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import logging
import threading
import time

import pytest

import _sidecar_fakes as F
from sidecar.core import SidecarCore
from sidecar.protocols import (
    AgentNotFoundError,
    AgentNotRunningError,
    BadRequestError,
    DRIVER_EXITED,
    DRIVER_RUNNING,
    GatewayUnreachableError,
    IdConflictError,
    NoSubscriptionError,
    NoSubscriptionForMergeError,
    NotDeployedError,
    PokeTimeoutError,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STARTING,
    SubscriptionNotAllowedError,
    SubscriptionNotFoundError,
    WorkdirNotFoundError,
    iso,
)


def rig(tmp_path, *, subs=None, extra_drivers=None, **core_kwargs):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    clock = F.FakeClock()
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource(subs if subs is not None else F.default_subscriptions())
    drivers = {"claude-code": driver}
    drivers.update(extra_drivers or {})
    core = SidecarCore(
        drivers=drivers, store=store, subscriptions=source, now=clock, **core_kwargs
    )
    return core, driver, store, source, clock, str(workdir)


# --- open: fork a home Session (spec: fork 一個 team 的 home Session) ---------


def test_fork_open_tags_new_session_and_sends_first_input(tmp_path):
    core, driver, store, source, _clock, workdir = rig(tmp_path)
    store.seed("agora:H", tags=("home",))

    rec = core.open(
        "t1",
        "claude-code",
        workdir,
        {"mode": "fork", "from": "agora:H"},
        subscription={"id": "claude-company"},
        input="讀 channel X",
    )

    assert rec.state == STATE_RUNNING
    assert rec.session_id == "agora:H-fork"  # status.session is the new one (D1)
    assert rec.subscription == "claude-company"
    # The fork carries the old tags plus the sidecar tag; H is untouched.
    assert store.sessions["agora:H"]["tags"] == {"home"}
    assert store.sessions["agora:H-fork"]["tags"] == {"home", "sidecar:t1"}
    assert store.starts[0]["mode"] == "fork"
    assert store.starts[0]["ids"] == ["agora:H"]
    assert store.starts[0]["tag"] == "sidecar:t1"
    assert store.starts[0]["by"] == "claude-code"
    # The caller only gave the subscription id; the sidecar fetched the row.
    assert source.gets == [("claude-company", "claude-code")]
    # The first input reached the agent.
    assert driver.handles[0].inbox == ["讀 channel X"]
    # And the secret went to the child environment, never the command line.
    assert driver.launches[0][1] == {"FAKE_AGENT_KEY": "key-company"}
    assert driver.launches[0][0].session_id == "agora:H-fork"


def test_new_mode_has_no_agora_process(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path)
    rec = core.open("t1", "claude-code", workdir, {"mode": "new"})
    assert rec.session_id is None  # until the first import (D5, task 3.5)
    assert store.starts == []
    assert driver.launches[0][0] is None


def test_open_omitting_subscription_uses_the_selector(tmp_path):
    core, _driver, _store, source, _clock, workdir = rig(tmp_path)
    rec = core.open("t1", "claude-code", workdir, {"mode": "new"})
    assert rec.subscription == "claude-personal"  # priority 10 beats 20
    assert rec.state == STATE_IDLE
    assert source.gets == [("claude-personal", "claude-code")]


def test_specified_cooling_subscription_is_used_anyway(tmp_path):
    personal = F.claude_subscription(
        "claude-personal", key="key-personal", available=False,
        cooldown={"5h": "2030-01-01T00:00:00Z"},
    )
    core, _driver, _store, _source, _clock, workdir = rig(tmp_path, subs=[personal])
    rec = core.open(
        "t1", "claude-code", workdir, {"mode": "new"},
        subscription={"id": "claude-personal"},
    )
    assert rec.subscription == "claude-personal"


def test_merge_mode_passes_every_id(tmp_path):
    core, _driver, store, _source, _clock, workdir = rig(tmp_path)
    store.seed("agora:A")
    store.seed("agora:B")
    rec = core.open(
        "t1", "claude-code", workdir, {"mode": "merge", "from": ["agora:A", "agora:B"]}
    )
    assert store.starts[0]["mode"] == "merge"
    assert store.starts[0]["ids"] == ["agora:A", "agora:B"]
    assert rec.session_id == "agora:merged-1"


def test_merge_by_another_kind_gets_its_own_subscription(tmp_path):
    opencode = F.FakeAgentDriver("opencode")
    zen = F.Subscription(
        id="zen", agents=("opencode",), priority=5, key="key-zen", available=True
    )
    subs = F.default_subscriptions() + [zen]
    core, _driver, store, source, _clock, workdir = rig(
        tmp_path, subs=subs, extra_drivers={"opencode": opencode}
    )
    store.seed("agora:A")
    store.seed("agora:B")

    core.open(
        "t1", "claude-code", workdir,
        {"mode": "merge", "from": ["agora:A", "agora:B"], "by": "opencode"},
        subscription={"id": "claude-personal"},
    )

    start = store.starts[0]
    assert start["by"] == "opencode"
    assert start["env"] == {"FAKE_AGENT_KEY": "key-personal"}  # the agent's env
    assert start["merge_env"] == {"FAKE_AGENT_KEY": "key-zen"}  # the summarizer's
    # The summarizer runs inside the store's `agora merge`, so the opencode
    # driver only supplies env_for - it never launches.
    assert opencode.launches == []
    assert ("zen", "opencode") in source.gets


def test_merge_with_no_summarizer_subscription_is_rejected(tmp_path):
    opencode = F.FakeAgentDriver("opencode")
    core, _driver, _store, _source, _clock, workdir = rig(
        tmp_path, extra_drivers={"opencode": opencode}
    )
    with pytest.raises(NoSubscriptionForMergeError):
        core.open(
            "t1", "claude-code", workdir, {"mode": "merge", "from": ["agora:A"], "by": "opencode"}
        )
    assert opencode.launches == []


# --- open: id and input rules -------------------------------------------------


def test_duplicate_id_is_rejected_and_opens_nothing(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"}, input="go")
    with pytest.raises(IdConflictError):
        core.open("t1", "claude-code", workdir, {"mode": "new"})
    assert len(driver.launches) == 1


def test_duplicate_id_while_starting_is_rejected(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    gate = threading.Event()
    driver.launch_gate = gate
    thread = threading.Thread(
        target=lambda: core.open("t1", "claude-code", workdir, {"mode": "new"})
    )
    thread.start()
    assert driver.launch_started.wait(2)
    with pytest.raises(IdConflictError):
        core.open("t1", "claude-code", workdir, {"mode": "new"})
    gate.set()
    thread.join(3)


def test_reopen_exited_id_continues_the_same_session(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path)
    store.seed("agora:A", messages=("earlier",))
    core.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"})
    core.close("t1")
    assert core.get("t1").state == STATE_EXITED

    rec = core.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"})
    assert rec.state == STATE_IDLE
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:A"]
    assert rec.session_id == "agora:A"
    assert len(driver.launches) == 2


def test_open_rejects_bad_agent_kind_and_session(tmp_path):
    core, _driver, _store, _source, _clock, workdir = rig(tmp_path)
    with pytest.raises(BadRequestError):
        core.open("t1", "not-an-agent", workdir, {"mode": "new"})
    with pytest.raises(BadRequestError):
        core.open("t1", "claude-code", workdir, {"mode": "resume"})
    with pytest.raises(BadRequestError):
        core.open("t1", "claude-code", workdir, {"mode": "continue"})
    with pytest.raises(BadRequestError):
        core.open("bad/id", "claude-code", workdir, {"mode": "new"})


def test_open_checks_workdir_exists_and_is_absolute(tmp_path):
    core, _driver, _store, _source, _clock, workdir = rig(tmp_path)
    with pytest.raises(WorkdirNotFoundError):
        core.open("t1", "claude-code", str(tmp_path / "missing"), {"mode": "new"})
    with pytest.raises(WorkdirNotFoundError):
        core.open("t1", "claude-code", "relative/dir", {"mode": "new"})
    assert not (tmp_path / "missing").exists()  # sidecar never creates it


# --- open: subscription error mapping (D4 table) ------------------------------


def test_specified_subscription_refusals_map_to_the_d4_table(tmp_path):
    cases = [
        ("agent_not_allowed", SubscriptionNotAllowedError),
        ("not_found", SubscriptionNotFoundError),
        ("not_deployed", NotDeployedError),
    ]
    for code, error in cases:
        core, driver, _store, source, _clock, workdir = rig(tmp_path)
        source.get_errors["claude-personal"] = code
        with pytest.raises(error):
            core.open(
                "t1", "claude-code", workdir, {"mode": "new"},
                subscription={"id": "claude-personal"},
            )
        assert driver.launches == []  # nothing was opened
        assert core.list_agents() == []


def test_open_with_unreachable_gateway_returns_503_and_opens_nothing(tmp_path):
    core, driver, _store, source, _clock, workdir = rig(tmp_path)
    source.list_unreachable = "the tunnel is down"
    with pytest.raises(GatewayUnreachableError):
        core.open("t1", "claude-code", workdir, {"mode": "new"})
    assert driver.launches == []
    assert core.list_agents() == []
    assert core.list_jobs() == []  # no job is hung either


def test_selector_skips_a_refused_candidate(tmp_path):
    core, _driver, _store, source, _clock, workdir = rig(tmp_path)
    source.get_errors["claude-personal"] = "not_deployed"
    rec = core.open("t1", "claude-code", workdir, {"mode": "new"})
    assert rec.subscription == "claude-company"  # the next candidate


def test_open_without_any_candidate_is_a_409(tmp_path):
    cooling = F.claude_subscription("claude-personal", available=False)
    core, _driver, _store, _source, _clock, workdir = rig(tmp_path, subs=[cooling])
    with pytest.raises(NoSubscriptionError):
        core.open("t1", "claude-code", workdir, {"mode": "new"})


def test_open_without_a_subscription_is_one_source_operation(tmp_path):
    core, _driver, _store, source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    # list + get(s) happen inside one logical operation (design D7/S1).
    assert source.operations == 1


# --- close (spec: 關閉) -------------------------------------------------------


def test_close_stops_gracefully_and_shows_exited(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path, close_timeout=7.0)
    store.seed("agora:A")
    core.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"}, input="go")

    rec = core.close("t1")

    assert rec.state == STATE_EXITED
    assert core.get("t1").state == STATE_EXITED
    handle, graceful, timeout = driver.stop_calls[0]
    assert handle is driver.handles[0]
    assert graceful is True
    assert timeout == 7.0  # the agora process gets close_timeout, then force


def test_close_unknown_id_is_a_404(tmp_path):
    core, _driver, _store, _source, _clock, _workdir = rig(tmp_path)
    with pytest.raises(AgentNotFoundError):
        core.close("nope")


def test_close_is_idempotent_on_an_exited_agent(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    core.close("t1")
    rec = core.close("t1")
    assert rec.state == STATE_EXITED
    assert len(driver.stop_calls) == 1


def test_close_while_starting_stops_the_agent_once_it_comes_up(tmp_path):
    """M1 scenario: close during open's launch is remembered, not refused."""
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    gate = threading.Event()
    driver.launch_gate = gate
    opened: list = []
    thread = threading.Thread(
        target=lambda: opened.append(core.open("t1", "claude-code", workdir, {"mode": "new"}))
    )
    thread.start()
    assert driver.launch_started.wait(2)

    rec = core.close("t1")

    assert rec.state == STATE_STARTING  # close does not 409 and does not lie
    gate.set()
    thread.join(5)
    final = opened[0]
    assert final.state == STATE_EXITED
    assert core.get("t1").state == STATE_EXITED
    assert driver.stop_calls == [(driver.handles[0], True, 30.0)]  # exactly once
    assert driver.handles[0].stopped  # no process left behind


# --- poke (spec: 暫停就是戳加中斷 / 不打斷 / starting 排隊) ---------------------


def test_pause_is_poke_with_interrupt(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"}, input="工作中")
    handle = driver.handles[0]
    assert handle.busy  # a round is running

    core.poke("t1", "停下來等指示", interrupt=True)

    assert handle.interrupts == 1
    # The interrupted round is over, so the words are the next input, not a queue.
    assert handle.inbox == ["工作中", "停下來等指示"]
    assert handle.queued == []


def test_poke_without_interrupt_waits_for_the_round(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"}, input="第一輪")
    handle = driver.handles[0]

    core.poke("t1", "第二則")

    assert handle.interrupts == 0
    assert handle.inbox == ["第一輪"]
    assert handle.queued == ["第二則"]
    handle.finish_round()
    assert handle.inbox == ["第一輪", "第二則"]
    core.tick()  # the scheduler is what learns about the round ending (M4)
    assert core.get("t1").state == STATE_IDLE


def test_poke_unknown_and_stopped_agents(tmp_path):
    core, _driver, _store, _source, _clock, workdir = rig(tmp_path)
    with pytest.raises(AgentNotFoundError):
        core.poke("nope", "hello")
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    core.close("t1")
    with pytest.raises(AgentNotRunningError):
        core.poke("t1", "hello")
    with pytest.raises(BadRequestError):
        core.poke("t1", "")


def test_poke_while_starting_queues_until_the_agent_is_up(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    gate = threading.Event()
    driver.launch_gate = gate

    opened: list = []
    thread = threading.Thread(
        target=lambda: opened.append(core.open("t1", "claude-code", workdir, {"mode": "new"}))
    )
    thread.start()
    assert driver.launch_started.wait(2)

    poked: list = []
    poking = threading.Thread(target=lambda: poked.append(core.poke("t1", "hello")))
    poking.start()
    time.sleep(0.05)
    assert poked == []  # queued, not sent yet (state is still starting)

    gate.set()
    thread.join(3)
    poking.join(3)
    assert poked and poked[0].id == "t1"
    assert driver.handles[0].inbox == ["hello"]


def test_poke_while_starting_times_out_with_409(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path, poke_queue_timeout=0.05)
    gate = threading.Event()
    driver.launch_gate = gate

    thread = threading.Thread(
        target=lambda: core.open("t1", "claude-code", workdir, {"mode": "new"})
    )
    thread.start()
    assert driver.launch_started.wait(2)

    with pytest.raises(PokeTimeoutError):
        core.poke("t1", "hello")

    gate.set()
    thread.join(3)
    assert core.get("t1").state == STATE_IDLE  # the agent did come up in the end


# --- status (spec: 不列別人的 / waiting-quota 的預定時刻) ----------------------


def test_status_lists_only_agents_this_sidecar_opened(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    driver.external_handle()  # somebody else's Claude Code on the same machine

    listed = core.list_agents()

    assert [rec.id for rec in listed] == ["t1"]
    payload = listed[0].to_dict()
    assert set(payload) == {
        "id", "agent", "session", "subscription", "state", "reopen_at", "started_at", "exit",
    }
    assert payload["reopen_at"] is None
    assert "key" not in json.dumps(payload)


def test_driver_reported_exit_shows_up_in_status(tmp_path):
    core, driver, _store, _source, _clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    driver.force_state(driver.handles[0], DRIVER_EXITED, code=3, reason="crashed")

    core.tick()  # the scheduler observes the exit; GET only reads the cache
    rec = core.get("t1")

    assert rec.state == STATE_EXITED
    assert rec.exit == {"code": 3, "reason": "crashed"}


# --- scheduler-only transitions and one transition per agent (D2/M4, H2) -----


def test_get_never_triggers_a_transition_or_a_driver_call(tmp_path):
    """Scenario GET /agents 不觸發換把: the response is the last known state."""
    core, driver, store, source, _clock, workdir = rig(tmp_path)
    store.seed("agora:A")
    core.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"},
              input="go")
    first = driver.handles[0]
    driver.force_state(first, "quota_exhausted", window="5h", resets_at=None)
    state_calls = driver.state_calls

    listed = core.list_agents()
    got = core.get("t1")

    assert driver.state_calls == state_calls  # GET never asked the driver
    assert source.cooldowns == [] and driver.stop_calls == []
    assert [rec.id for rec in listed] == ["t1"]
    assert got.state == STATE_RUNNING and got.subscription == "claude-personal"
    assert len(driver.launches) == 1

    core.tick()  # the scheduler's next beat is what swaps
    assert [c[0] for c in source.cooldowns] == ["claude-personal"]
    assert core.get("t1").subscription == "claude-company"


def test_two_threads_seeing_quota_swap_only_once(tmp_path):
    """H2 scenario: a slow cooldown, two concurrent refreshes, one swap."""
    core, driver, store, source, clock, workdir = rig(tmp_path)
    source.cooldown_delay = 0.4
    store.seed("agora:A")
    core.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"},
              subscription={"id": "claude-personal"}, input="go")
    first = driver.handles[0]
    driver.force_state(first, "quota_exhausted", window="5h", resets_at=iso(clock.t + 3600))

    threads = [threading.Thread(target=core.refresh_states) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert [c[0] for c in source.cooldowns] == ["claude-personal"]  # registered once
    assert driver.stop_calls == [(first, True, 30.0)]              # stopped once
    assert len(driver.launches) == 2                               # one new process
    rec = core.get("t1")
    assert rec.subscription == "claude-company"
    assert rec.state in (STATE_IDLE, STATE_RUNNING)
    assert core.list_jobs() == []


def test_one_agents_driver_failure_does_not_stop_the_others(tmp_path):
    opencode = F.FakeAgentDriver("opencode")
    subs = F.default_subscriptions() + [
        F.Subscription(id="zen", agents=("opencode",), key="key-zen", available=True)
    ]
    core, driver, _store, _source, _clock, workdir = rig(
        tmp_path, subs=subs, extra_drivers={"opencode": opencode}
    )
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    core.open("t2", "opencode", workdir, {"mode": "new"})
    driver.state_fails = True
    opencode.force_state(opencode.handles[0], DRIVER_RUNNING)

    core.tick()  # t1's driver blows up; t2 must still be refreshed

    assert core.get("t1").state in (STATE_STARTING, STATE_IDLE)  # unchanged, no crash
    assert core.get("t2").state == STATE_RUNNING
    assert [rec.id for rec in core.list_agents()] == ["t1", "t2"]


# --- shutdown (design D2: SIGTERM 先 close 全部) ------------------------------


def test_shutdown_closes_every_open_agent(tmp_path):
    core, driver, store, _source, _clock, workdir = rig(tmp_path)
    store.seed("agora:A")
    store.seed("agora:B")
    core.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"})
    core.open("t2", "claude-code", workdir, {"mode": "continue", "from": "agora:B"})
    core.close("t2")

    core.shutdown()

    assert core.get("t1").state == STATE_EXITED
    assert core.get("t2").state == STATE_EXITED
    assert len(driver.stop_calls) == 2  # t2 was already closed; no second stop


# --- volatile state (spec: 重啟) ---------------------------------------------


def test_a_fresh_core_starts_empty_but_agora_keeps_the_memory(tmp_path):
    _core, driver, store, source, clock, workdir = rig(tmp_path)
    store.seed("agora:A")
    core1 = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    core1.open("t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"})

    core2 = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )

    assert core2.list_agents() == []
    assert core2.list_jobs() == []
    assert "agora:A" in store.sessions  # the conversation is still in agora


# --- environment values stay out of the command line and the log (task 2.6) ---


def test_subscription_secret_only_reaches_the_child_env(tmp_path, caplog):
    secret = "SUPER-SECRET-KEY"
    sub = F.claude_subscription("claude-personal", key=secret)
    core, driver, store, _source, _clock, workdir = rig(tmp_path, subs=[sub])
    store.seed("agora:A")

    with caplog.at_level(logging.DEBUG):
        core.open(
            "t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"},
            subscription={"id": "claude-personal"}, input="go",
        )
        core.poke("t1", "more")
        core.close("t1")
        core.refresh_states()

    assert secret not in caplog.text
    session, env, _workdir = driver.launches[0]
    assert env["FAKE_AGENT_KEY"] == secret  # the env is how it travels
    assert secret not in json.dumps(session.session_id)
    assert secret not in json.dumps([rec.to_dict() for rec in core.list_agents()])
    # The session-store call's command surface is clean too; the env dict is a
    # separate field that goes to the child environment, never to an argv.
    surface = [
        {k: v for k, v in start.items() if k not in ("env", "merge_env")}
        for start in store.starts
    ]
    assert secret not in json.dumps(surface)
