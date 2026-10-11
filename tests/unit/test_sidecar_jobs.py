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
    DRIVER_EXITED,
    SourceError,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_WAITING_QUOTA,
    iso,
)


def rig(tmp_path, *, subs=None, **core_kwargs):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    clock = F.FakeClock(1000.0)
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource(subs if subs is not None else F.default_subscriptions())
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock, **core_kwargs
    )
    return core, driver, store, source, clock, str(workdir)


# --- the job forms -----------------------------------------------------------


def test_at_job_is_visible_before_and_opens_at_due_time(tmp_path):
    core, _driver, _store, _source, clock, workdir = rig(tmp_path)
    job = core.add_job(
        "open",
        {"id": "t1", "agent": "claude-code", "workdir": workdir, "session": {"mode": "new"}},
        at=clock.t + 3600,
    )

    assert [j.id for j in core.list_jobs()] == [job.id]
    assert job.to_dict()["at"] == iso(clock.t + 3600)
    assert job.to_dict()["last_run"] is None and job.to_dict()["last_error"] is None

    core.tick()
    assert core.list_agents() == []  # not due yet

    clock.advance(3600)
    core.tick()
    assert [rec.id for rec in core.list_agents()] == ["t1"]
    assert core.list_jobs() == []  # a one-shot job is consumed


def test_every_job_runs_until(tmp_path):
    core, _driver, _store, _source, clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    core.add_job("poke", {"id": "t1", "text": "poll"}, every=60, until=clock.t + 100)

    clock.advance(60)
    core.tick()
    handle = core.get("t1").handle
    assert handle.inbox == ["poll"]

    # next would be 1120, past the 1100 until: the job is dropped.
    assert core.list_jobs() == []
    clock.advance(1000)
    core.tick()
    assert handle.inbox == ["poll"]


def test_every_job_reschedules_before_until(tmp_path):
    core, _driver, _store, _source, clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"})
    job = core.add_job("poke", {"id": "t1", "text": "poll"}, every=60, until=clock.t + 3600)

    clock.advance(60)
    core.tick()
    assert [j.id for j in core.list_jobs()] == [job.id]
    handle = core.get("t1").handle
    handle.finish_round()  # the first polled round ends before the next one
    clock.advance(60)
    core.tick()
    assert handle.inbox == ["poll", "poll"]
    assert job.last_run == clock.t


def test_delete_job(tmp_path):
    core, _driver, _store, _source, clock, workdir = rig(tmp_path)
    job = core.add_job(
        "open",
        {"id": "t1", "agent": "claude-code", "workdir": workdir, "session": {"mode": "new"}},
        at=clock.t + 10,
    )
    assert core.delete_job(job.id) is True
    assert core.delete_job(job.id) is False
    clock.advance(10)
    core.tick()
    assert core.list_agents() == []


def test_add_job_validation(tmp_path):
    core, _driver, _store, _source, clock, workdir = rig(tmp_path)
    args = {"id": "t1", "agent": "claude-code", "workdir": workdir, "session": {"mode": "new"}}
    with pytest.raises(BadRequestError):
        core.add_job("pause", args, at=clock.t + 1)
    with pytest.raises(BadRequestError):
        core.add_job("open", args)  # neither at nor every
    with pytest.raises(BadRequestError):
        core.add_job("open", args, at=clock.t + 1, every=60)
    with pytest.raises(BadRequestError):
        core.add_job("open", args, at=clock.t - 1)  # at in the past
    with pytest.raises(BadRequestError):
        core.add_job("poke", {"id": "t1", "text": "x"}, every=0)
    with pytest.raises(BadRequestError):
        core.add_job("poke", {"id": "t1", "text": "x"}, at=clock.t + 1, until=clock.t + 9)
    with pytest.raises(BadRequestError):
        core.add_job("poke", {"id": "t1"}, every=60)  # text missing
    with pytest.raises(BadRequestError):
        core.add_job("close", {"id": "t1", "text": "x"}, every=60)  # extra key


def test_a_failed_job_is_recorded_and_dropped(tmp_path):
    core, driver, _store, _source, clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"}, input="busy")
    job = core.add_job(
        "open",
        {"id": "t1", "agent": "claude-code", "workdir": workdir, "session": {"mode": "new"}},
        at=clock.t + 1,
    )
    clock.advance(1)
    core.tick()  # the open conflicts (t1 running): the job is logged and dropped

    assert core.list_jobs() == []
    assert job.last_run == clock.t
    assert job.last_error == {
        "status": 409,
        "code": "id_conflict",
        "message": "agent 't1' already exists in state running",
    }
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


def _open_and_exhaust(core, driver, store, source, clock, workdir, **exhaust):
    store.seed("agora:A")
    core.open(
        "t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"},
        subscription={"id": "claude-personal"}, input="go",
    )
    first = driver.handles[0]
    fields = {"window": "5h", "resets_at": iso(clock.t + 3600)}
    fields.update(exhaust)
    driver.force_state(first, "quota_exhausted", **fields)
    return first


def test_quota_exhaustion_switches_to_the_next_subscription(tmp_path):
    core, driver, store, source, clock, workdir = rig(tmp_path)
    first = _open_and_exhaust(core, driver, store, source, clock, workdir)

    core.tick()

    assert source.cooldowns == [("claude-personal", "5h", iso(clock.t + 3600))]
    # Stop gracefully first (the agora process writes back on its way out),
    # then continue the same Session on the new subscription.
    assert driver.stop_calls == [(first, True, 30.0)]
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:A"]
    assert store.starts[-1]["tag"] == "sidecar:t1"
    assert driver.launches[-1][1] == {"FAKE_AGENT_KEY": "key-company"}

    rec = core.get("t1")
    assert rec.id == "t1"  # id unchanged
    assert rec.subscription == "claude-company"
    assert rec.session_id == "agora:A"  # swap keeps the same Session
    assert rec.state in (STATE_IDLE, STATE_RUNNING)
    # D4: a swap success resumes the work with resume_prompt.
    assert driver.handles[-1].inbox == ["額度恢復了，請接著剛才的工作"]
    assert core.list_jobs() == []
    # The open (explicit subscription) and the swap each used one logical
    # operation: cooldown + list + get never open three connections (S1).
    assert source.operations == 2


def test_empty_resume_prompt_is_not_sent(tmp_path):
    core, driver, store, source, clock, workdir = rig(tmp_path, resume_prompt="")
    _open_and_exhaust(core, driver, store, source, clock, workdir)
    core.tick()
    assert driver.handles[-1].inbox == []


def test_cooldown_rejection_retries_without_resets_at(tmp_path):
    core, driver, store, source, clock, workdir = rig(tmp_path)
    source.cooldown_results = [SourceError("invalid_resets_at"), None]
    _open_and_exhaust(core, driver, store, source, clock, workdir)

    core.tick()

    assert source.cooldowns == [
        ("claude-personal", "5h", iso(clock.t + 3600)),
        ("claude-personal", "5h", None),  # the retry drops resets_at (D4)
    ]
    assert core.get("t1").subscription == "claude-company"


def test_failed_cooldown_never_picks_the_same_id_back(tmp_path):
    core, driver, store, source, clock, workdir = rig(tmp_path)
    # Both cooldown attempts are refused, so list still says personal is
    # available - the swap must still exclude it (M2: no running in circles).
    source.cooldown_results = [SourceError("invalid_resets_at"), SourceError("invalid_resets_at")]
    _open_and_exhaust(core, driver, store, source, clock, workdir)

    core.tick()

    assert core.get("t1").subscription == "claude-company"
    assert source.cooldowns[0][0] == source.cooldowns[1][0] == "claude-personal"


def test_llmgateway_unreachable_during_swap_waits_with_a_retry_job(tmp_path):
    core, driver, store, source, clock, workdir = rig(tmp_path, retry_interval=300.0)
    _open_and_exhaust(core, driver, store, source, clock, workdir)
    source.list_unreachable = "the tunnel is down"

    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA
    assert rec.reopen_at == clock.t + 300.0
    jobs = core.list_jobs()
    assert len(jobs) == 1 and jobs[0].auto is True
    assert jobs[0].next_run_at == clock.t + 300.0
    assert store.starts[-1]["mode"] == "continue"


# --- quota: everything cooling (spec: 全部冷卻 / 解除時刻看兩個窗口) ----------


def _cooling_setup(tmp_path):
    """personal runs out at +120s but its 1w only lifts at +100000; company's
    5h lifts at +240; one row is not deployed and one is another agent kind."""
    clock = F.FakeClock(1000.0)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    workdir = str(workdir)
    personal = F.claude_subscription(
        "claude-personal", key="key-personal", priority=10,
        cooldown={"1w": iso(clock.t + 100000)},
    )
    company = F.claude_subscription(
        "claude-company", key="key-company", priority=20,
        available=False, cooldown={"5h": iso(clock.t + 240)},
    )
    not_deployed = F.claude_subscription(
        "dead", key=None, priority=30, available=False, cooldown={}
    )
    other_kind = F.claude_subscription(
        "zen", key="key-zen", priority=5, available=False,
        cooldown={"5h": iso(clock.t + 50)}, agents=("opencode",),
    )
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource([personal, company, not_deployed, other_kind])
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    first = _open_and_exhaust(
        core, driver, store, source, clock, workdir, resets_at=iso(clock.t + 120)
    )
    return core, driver, store, source, clock, workdir, first


def test_all_cooling_waits_and_schedules_an_auto_reopen_job(tmp_path):
    core, driver, store, source, clock, workdir, first = _cooling_setup(tmp_path)

    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA
    # personal's lift is max(5h=+120, 1w=+100000); company's is +240;
    # the not-deployed row and the other kind do not count.
    assert rec.reopen_at == clock.t + 240
    assert driver.stop_calls == [(first, True, 30.0)]
    # status shows the same moment as the job (spec scenario)
    assert rec.to_dict()["reopen_at"] == iso(clock.t + 240)

    jobs = core.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.auto is True
    assert job.action == "open"
    assert job.args == {
        "id": "t1",
        "agent": "claude-code",
        "workdir": workdir,
        "session": {"mode": "continue", "from": "agora:A"},
    }
    assert "subscription" not in job.args and "input" not in job.args
    assert job.next_run_at == clock.t + 240


def test_auto_reopen_job_opens_when_the_cooldown_lifts(tmp_path):
    core, driver, store, source, clock, workdir, _first = _cooling_setup(tmp_path)
    core.tick()
    assert core.get("t1").state == STATE_WAITING_QUOTA

    # At the reopen moment the selector must freshly pick (Q8): company is back.
    source.set_available("claude-company", True)
    clock.t = 1000.0 + 240
    core.tick()

    rec = core.get("t1")
    assert rec.state in (STATE_IDLE, STATE_RUNNING)
    assert rec.subscription == "claude-company"
    assert rec.reopen_at is None
    assert store.starts[-1]["mode"] == "continue"
    assert store.starts[-1]["ids"] == ["agora:A"]
    assert core.list_jobs() == []  # the auto job was consumed
    # D4: an auto-reopen success also resumes with resume_prompt.
    assert driver.handles[-1].inbox == ["額度恢復了，請接著剛才的工作"]


def test_auto_reopen_that_fails_recomputes_and_rehangs(tmp_path):
    core, driver, store, source, clock, workdir, _first = _cooling_setup(tmp_path)
    core.tick()
    first_job = core.list_jobs()[0]

    # The moment arrives but company is still cooling and personal is far off.
    source._subs["claude-company"].cooldown["5h"] = iso(clock.t + 1000)
    clock.t = 1000.0 + 240
    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA
    assert first_job.id not in [j.id for j in core.list_jobs()]
    jobs = core.list_jobs()
    assert len(jobs) == 1 and jobs[0].auto is True
    # personal lifts at +100000, company at +1000: the new job waits for the min.
    assert jobs[0].next_run_at == 1000.0 + 1000
    assert rec.reopen_at == 1000.0 + 1000


def test_no_candidate_means_no_job_and_null_reopen_at(tmp_path):
    personal = F.claude_subscription("claude-personal", key="key-personal")
    company = F.claude_subscription(
        "claude-company", key=None, available=False, cooldown={}
    )
    core, driver, store, source, clock, workdir = rig(tmp_path, subs=[personal, company])
    # The driver cannot tell when the window ends: no resets_at (D4: never guess).
    _open_and_exhaust(core, driver, store, source, clock, workdir, resets_at=None)

    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA
    assert rec.reopen_at is None
    assert rec.to_dict()["reopen_at"] is None
    assert core.list_jobs() == []


def test_deleting_the_auto_job_nulls_reopen_at(tmp_path):
    core, _driver, _store, _source, clock, _workdir, _first = _cooling_setup(tmp_path)
    core.tick()
    job = core.list_jobs()[0]

    assert core.delete_job(job.id) is True

    rec = core.get("t1")
    assert rec.state == STATE_WAITING_QUOTA  # still waiting, controller decides
    assert rec.reopen_at is None


def test_controller_reopen_of_waiting_quota_deletes_the_auto_job(tmp_path):
    core, _driver, _store, source, clock, workdir, _first = _cooling_setup(tmp_path)
    core.tick()
    assert len(core.list_jobs()) == 1

    source.set_available("claude-company", True)
    rec = core.open(
        "t1", "claude-code", workdir, {"mode": "continue", "from": "agora:A"},
        subscription={"id": "claude-company"},
    )

    assert rec.subscription == "claude-company"
    assert core.list_jobs() == []


def test_close_waiting_quota_deletes_the_auto_job(tmp_path):
    core, driver, _store, _source, _clock, _workdir, first = _cooling_setup(tmp_path)
    core.tick()
    assert len(core.list_jobs()) == 1

    rec = core.close("t1")

    assert rec.state == STATE_EXITED
    assert core.list_jobs() == []
    assert len(driver.stop_calls) == 1  # no second stop for a stopped agent


def test_driver_reported_exit_does_not_swap(tmp_path):
    core, driver, _store, source, clock, workdir = rig(tmp_path)
    core.open("t1", "claude-code", workdir, {"mode": "new"}, input="go")
    driver.force_state(driver.handles[0], DRIVER_EXITED, code=1, reason="crash")

    core.tick()

    rec = core.get("t1")
    assert rec.state == STATE_EXITED
    assert rec.exit == {"code": 1, "reason": "crash"}
    assert source.cooldowns == []
    assert core.list_jobs() == []


def test_unknown_quota_window_is_treated_as_5h_and_logged(tmp_path, caplog):
    core, driver, _store, source, clock, workdir = rig(tmp_path)
    _open_and_exhaust(core, driver, _store, source, clock, workdir, window="9h")
    core.tick()
    assert source.cooldowns[0][1] == "5h"
    assert "unknown quota window" in caplog.text
