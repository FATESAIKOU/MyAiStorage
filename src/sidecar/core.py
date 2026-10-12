"""The sidecar core: the four actions over the internal seams (design D1-D5).

State (agents and jobs) lives only in this object - always in memory, never on
disk (design D2).  All public methods take the lock while mutating; driver and
store calls happen outside it so a slow stop cannot freeze status.

Quota handling follows D4: a swap excludes the subscription that just ran out
(no relying on `list`'s `available`), a refused cooldown is retried without
`resets_at`, and when nothing is usable the agent goes waiting-quota with an
auto-reopen job at the earliest lift of the remaining candidates.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .jobs import Job, JobRegistry
from .protocols import (
    ACTIONS,
    ACTIVE_STATES,
    DRIVER_EXITED,
    DRIVER_IDLE,
    DRIVER_QUOTA_EXHAUSTED,
    DRIVER_RUNNING,
    SESSION_MODES,
    STATE_EXITED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_STARTING,
    STATE_WAITING_QUOTA,
    WINDOWS,
    AgentNotFoundError,
    AgentNotRunningError,
    BadRequestError,
    DriverState,
    GatewayUnreachableError,
    IdConflictError,
    NoSubscriptionError,
    NoSubscriptionForMergeError,
    NotDeployedError,
    PokeTimeoutError,
    RunningSession,
    SidecarError,
    SourceError,
    ShuttingDownError,
    Subscription,
    SubscriptionNotAllowedError,
    SubscriptionNotFoundError,
    UnavailableError,
    Unreachable,
    WorkdirNotFoundError,
    epoch,
    iso,
    valid_id,
)
from .selectors import PrioritySelector

logger = logging.getLogger(__name__)

DEFAULT_RESUME_PROMPT = "額度恢復了，請接著剛才的工作"
DEFAULT_RETRY_INTERVAL = 300.0
DEFAULT_POKE_QUEUE_TIMEOUT = 30.0
DEFAULT_CLOSE_TIMEOUT = 30.0
DEFAULT_SYNC_PERIOD = 300.0  # seconds; matches the config default of 5 minutes

# Which keys each action's args may carry in a job (design D1).
_JOB_ARGS: dict[str, frozenset[str]] = {
    "open": frozenset({"id", "agent", "workdir", "session", "subscription", "input"}),
    "close": frozenset({"id"}),
    "status": frozenset({"id"}),
    "poke": frozenset({"id", "text", "interrupt"}),
}
_JOB_REQUIRED: dict[str, tuple[str, ...]] = {
    "open": ("id", "agent", "workdir", "session"),
    "close": ("id",),
    "status": ("id",),
    "poke": ("id", "text"),
}


def parse_session(spec: Any) -> tuple[str, list[str], str | None]:
    """Validate the open `session` object; return (mode, ids, by)."""
    if not isinstance(spec, dict):
        raise BadRequestError("session must be a JSON object with a mode")
    mode = spec.get("mode")
    if mode not in SESSION_MODES:
        raise BadRequestError(f"session mode must be one of {', '.join(SESSION_MODES)}")
    by = spec.get("by")
    if by is not None and (not isinstance(by, str) or not by):
        raise BadRequestError("session.by must be an agent kind")
    if mode == "new":
        return "new", [], by
    source = spec.get("from")
    if mode == "merge":
        if isinstance(source, str) and source:
            ids = [source]
        elif isinstance(source, list) and source and all(isinstance(i, str) and i for i in source):
            ids = list(source)
        else:
            raise BadRequestError("merge session needs from: one or more agora ids")
        return mode, ids, by
    if not isinstance(source, str) or not source:
        raise BadRequestError(f"{mode} session needs from: one agora id")
    return mode, [source], by


@dataclass
class PendingPoke:
    text: str
    interrupt: bool
    deadline: float  # monotonic seconds
    sent: bool = False


@dataclass
class AgentRecord:
    id: str
    agent: str
    workdir: str
    state: str
    started_at: float
    session_id: str | None = None
    subscription: str | None = None
    handle: Any = None
    exit: dict[str, Any] | None = None
    reopen_at: float | None = None
    reopen_job: str | None = None
    busy: bool = False  # a state transition is in flight for this agent (D2)
    closing: bool = False  # close arrived while starting; open finishes the stop (M1)
    new_mode: bool = False  # no agora process; periodic `agora import` (D5/3.5)
    next_import: float = 0.0  # when the next periodic import is due
    external_session_id: str | None = None  # remembered for a pending import (H2)
    import_pending: bool = False  # waiting-quota: the swap import is still owed
    pending: list[PendingPoke] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The GET /agents shape (design D1): no key, nothing but the listed fields."""
        reopen_at = None
        if self.state == STATE_WAITING_QUOTA and self.reopen_job:
            reopen_at = iso(self.reopen_at)
        return {
            "id": self.id,
            "agent": self.agent,
            "session": self.session_id,
            "subscription": self.subscription,
            "state": self.state,
            "reopen_at": reopen_at,
            "started_at": iso(self.started_at),
            "exit": self.exit,
        }


class SidecarCore:
    def __init__(
        self,
        *,
        drivers: Mapping[str, Any],
        store: Any | None,
        subscriptions: Any | None = None,
        selector: Any | None = None,
        now: Callable[[], float] = time.time,
        resume_prompt: str = DEFAULT_RESUME_PROMPT,
        retry_interval: float = DEFAULT_RETRY_INTERVAL,
        poke_queue_timeout: float = DEFAULT_POKE_QUEUE_TIMEOUT,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
        sync_period: float = DEFAULT_SYNC_PERIOD,
    ) -> None:
        self._drivers = dict(drivers)
        self._store = store
        self._subscriptions = subscriptions
        self._selector = selector or PrioritySelector()
        self._now = now
        self._resume_prompt = resume_prompt
        self._retry_interval = retry_interval
        self._poke_queue_timeout = poke_queue_timeout
        self._close_timeout = close_timeout
        self._sync_period = sync_period  # seconds; `new` mode's import period (D5)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._agents: dict[str, AgentRecord] = {}
        self._jobs = JobRegistry()
        self._closing = False  # set by begin_shutdown; blocks new work (D2/M5)

    # --- the four actions -------------------------------------------------

    def open(
        self,
        id: str,
        agent: str,
        workdir: str,
        session: Any,
        subscription: Any = None,
        input: str | None = None,
    ) -> AgentRecord:
        valid_id(id)
        self._refuse_if_closing()
        if not isinstance(agent, str) or not agent:
            raise BadRequestError("agent must be a non-empty string")
        if not isinstance(workdir, str) or not workdir:
            raise BadRequestError("workdir must be a non-empty string")
        if not Path(workdir).is_absolute():
            raise WorkdirNotFoundError(f"workdir {workdir!r} must be an absolute path")
        if not Path(workdir).is_dir():
            raise WorkdirNotFoundError(
                f"workdir {workdir!r} does not exist (sidecar does not create it)"
            )
        mode, ids, by = parse_session(session)
        by = by or agent
        if input is not None and not isinstance(input, str):
            raise BadRequestError("input must be a string")
        if subscription is not None and (
            not isinstance(subscription, dict) or not isinstance(subscription.get("id"), str)
        ):
            raise BadRequestError('subscription must look like {"id": ...}')
        driver = self._drivers.get(agent)
        if driver is None:
            raise BadRequestError(f"unsupported agent kind {agent!r}")
        if self._subscriptions is None:
            raise UnavailableError("no subscription source is configured")
        if mode != "new" and self._store is None:
            raise UnavailableError("session store is not configured")

        now = self._now()
        with self._lock:
            existing = self._agents.get(id)
            if existing is not None and existing.state in ACTIVE_STATES:
                raise IdConflictError(f"agent {id!r} already exists in state {existing.state}")
            # From here the id is reserved as starting, so a concurrent open of
            # the same id loses the race here instead of double-launching.
            placeholder = AgentRecord(
                id=id, agent=agent, workdir=workdir, state=STATE_STARTING, started_at=now
            )
            self._agents[id] = placeholder
            previous_job = existing.reopen_job if existing is not None else None

        try:
            with self._source_op() as source:
                if subscription is not None:
                    sub = self._get_specified(source, subscription["id"], agent)
                else:
                    sub, unreachable = self._choose_one(source, agent, ())
                    if unreachable is not None:
                        raise GatewayUnreachableError(
                            unreachable.reason or "LLMGateway cannot be reached"
                        )
                    if sub is None:
                        raise NoSubscriptionError(f"no available subscription for {agent!r}")
                env = driver.env_for(sub)
                merge_env = None
                if mode == "merge" and by != agent:
                    by_driver = self._drivers.get(by)
                    if by_driver is None:
                        raise BadRequestError(f"unsupported merge agent kind {by!r}")
                    by_sub, by_unreachable = self._choose_one(source, by, ())
                    if by_unreachable is not None:
                        raise GatewayUnreachableError(
                            by_unreachable.reason or "LLMGateway cannot be reached"
                        )
                    if by_sub is None:
                        raise NoSubscriptionForMergeError(
                            f"no available subscription for merge agent {by!r}"
                        )
                    merge_env = by_driver.env_for(by_sub)
                if mode == "new":
                    # No agora process (D5): the driver starts the agent itself
                    # and the agora id arrives with the first import (task 3.5).
                    running = None
                    handle = driver.launch(None, env, workdir)
                else:
                    running = self._store.start(
                        mode=mode,
                        ids=ids,
                        by=by,
                        agent=agent,
                        workdir=workdir,
                        tag=f"sidecar:{id}",
                        env=env,
                        merge_env=merge_env,
                    )
                    handle = driver.launch(running, env, workdir)
        except BaseException:
            with self._lock:
                if self._agents.get(id) is placeholder:
                    if existing is not None:
                        self._agents[id] = existing
                    else:
                        del self._agents[id]
            raise

        with self._lock:
            placeholder.session_id = running.session_id if running is not None else None
            placeholder.subscription = sub.id
            placeholder.handle = handle
            if mode == "new":
                # No agora process (D5): periodic imports carry the memory.
                placeholder.new_mode = True
                placeholder.next_import = now + self._sync_period
            if previous_job:
                # Same id reopened from waiting-quota: its auto-reopen job goes.
                self._jobs.delete(previous_job)
                if existing is not None:
                    existing.reopen_job = None
        self._refresh_one(placeholder)
        with self._lock:
            closed_early = placeholder.closing and placeholder.state != STATE_EXITED
        if closed_early:
            # close arrived while this open was still launching (M1): stop the
            # process we just started and report it exited.
            self._stop_and_exit(placeholder)
            return placeholder
        if input is not None:
            # The first input queues and never fails this open (L5).
            self._enqueue_poke(placeholder, input)
        return placeholder

    def _stop_and_exit(self, rec: AgentRecord) -> AgentRecord:
        """Stop the handle and mark exited; used by close and the M1 race path."""
        with self._lock:
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
            new_mode = rec.new_mode
            waiting = rec.state == STATE_WAITING_QUOTA
        if new_mode:
            return self._close_new_mode(rec, driver, handle, waiting=waiting)
        final = self._stop_handle(handle, driver)
        with self._lock:
            rec.state = STATE_EXITED
            rec.exit = {"code": final.code, "reason": final.reason or "closed"}
            rec.reopen_at = None
            rec.reopen_job = None
        return rec

    def _closing_requested(self, rec: AgentRecord) -> bool:
        with self._lock:
            return rec.closing and self._agents.get(rec.id) is rec

    def _set_exited(self, rec: AgentRecord, *, reason: str = "closed during a transition") -> None:
        """Mark exited without another stop; the handle was already stopped."""
        with self._lock:
            if self._agents.get(rec.id) is not rec:
                return
            job_id = rec.reopen_job
            rec.state = STATE_EXITED
            rec.exit = {"code": None, "reason": reason}
            rec.reopen_at = None
            rec.reopen_job = None
            rec.import_pending = False
        if job_id:
            # An exited agent must not be reopened by a job hung during the race.
            self._jobs.delete(job_id)

    def _stop_handle(self, handle: Any, driver: Any) -> DriverState:
        if handle is None or driver is None:
            return DriverState(kind=DRIVER_EXITED, reason="closed without a running agent")
        # Gentle stop: the agora process forwards to the agent, writes the
        # last conversation back on its way out, then releases the lock (D6).
        driver.stop(handle, graceful=True, timeout=self._close_timeout)
        return driver.state(handle)

    def close(self, id: str) -> AgentRecord:
        with self._lock:
            rec = self._agents.get(id)
            if rec is None:
                raise AgentNotFoundError(f"unknown agent {id!r}")
            if rec.reopen_job:
                self._jobs.delete(rec.reopen_job)
                rec.reopen_job = None
                rec.reopen_at = None
            if rec.state == STATE_EXITED:
                return rec
            if rec.state == STATE_STARTING or rec.busy:
                # The launch or a state transition (e.g. a swap) is in flight:
                # remember the close, let that thread do the stop (M1/N1).
                rec.closing = True
                self._clear_pending(rec)
                return rec
            rec.busy = True
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
            waiting = rec.state == STATE_WAITING_QUOTA
            self._clear_pending(rec)
        try:
            if rec.new_mode:
                return self._close_new_mode(rec, driver, handle, waiting=waiting)
            if waiting:
                final = DriverState(kind=DRIVER_EXITED, reason="closed without a running agent")
            else:
                final = self._stop_handle(handle, driver)
            with self._lock:
                rec.state = STATE_EXITED
                rec.exit = {"code": final.code, "reason": final.reason or "closed"}
                rec.reopen_at = None
                rec.reopen_job = None
            return rec
        finally:
            with self._lock:
                rec.busy = False

    def poke(self, id: str, text: str, interrupt: bool = False) -> AgentRecord:
        self._refuse_if_closing()
        if not isinstance(text, str) or not text:
            raise BadRequestError("text must be a non-empty string")
        with self._cond:
            rec = self._agents.get(id)
            if rec is None:
                raise AgentNotFoundError(f"unknown agent {id!r}")
            if rec.state == STATE_STARTING:
                # Queue until the agent is up; a timeout is a 409 (D1/Q5).
                item = PendingPoke(
                    text=text,
                    interrupt=bool(interrupt),
                    deadline=time.monotonic() + self._poke_queue_timeout,
                )
                rec.pending.append(item)
                while item in rec.pending:
                    remaining = item.deadline - time.monotonic()
                    if remaining <= 0:
                        rec.pending.remove(item)
                        raise PokeTimeoutError(
                            f"agent {id!r} did not come up within "
                            f"{self._poke_queue_timeout:g}s"
                        )
                    self._cond.wait(timeout=remaining)
                if not item.sent:
                    raise AgentNotRunningError(
                        f"agent {id!r} stopped before the queued poke could be sent"
                    )
                return rec
            if rec.state not in (STATE_RUNNING, STATE_IDLE):
                raise AgentNotRunningError(f"agent {id!r} is {rec.state}; poke needs it running")
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
        # The driver call happens outside the lock (L1): a slow send must not
        # freeze status.  A concurrent close may stop the handle in between;
        # that surfaces here as the driver's own error.
        try:
            if interrupt:
                driver.interrupt(handle)
            driver.send(handle, text)
        except SidecarError:
            raise
        except Exception as e:
            # L1: the agent ended between the state check and the send.  The
            # driver's RuntimeError becomes 409 agent_not_running, and the
            # record is exited (never a 500).
            logger.warning("agent %s: poke found the agent not running: %s", id, e)
            with self._lock:
                if self._agents.get(id) is rec:
                    rec.state = STATE_EXITED
                    rec.exit = {"code": None, "reason": f"poke failed: {e}"}
                    rec.reopen_at = None
                    rec.reopen_job = None
                    self._clear_pending(rec)
            raise AgentNotRunningError(f"agent {id!r} is not running: {e}") from e
        with self._lock:
            if (
                self._agents.get(id) is rec
                and rec.state != STATE_EXITED
                and rec.handle is handle
            ):
                # D: a close (or a swap) may have finished while the send was
                # in flight; a late poke must not resurrect the record.
                rec.state = STATE_RUNNING
        return rec

    def get(self, id: str) -> AgentRecord:
        """The last known state; GET never triggers a transition (D2/M4)."""
        with self._lock:
            rec = self._agents.get(id)
        if rec is None:
            raise AgentNotFoundError(f"unknown agent {id!r}")
        return rec

    def list_agents(self) -> list[AgentRecord]:
        """The last known list; GET never triggers a transition (D2/M4)."""
        with self._lock:
            return list(self._agents.values())

    # --- jobs (design D1: at / every with until) --------------------------

    def add_job(
        self,
        action: str,
        args: Any,
        *,
        at: float | None = None,
        every: float | None = None,
        until: float | None = None,
    ) -> Job:
        self._refuse_if_closing()
        if action not in ACTIONS:
            raise BadRequestError(f"action must be one of {', '.join(ACTIONS)}")
        if not isinstance(args, dict):
            raise BadRequestError("args must be a JSON object")
        unknown = set(args) - _JOB_ARGS[action]
        if unknown:
            raise BadRequestError(f"{action} args do not take: {', '.join(sorted(unknown))}")
        missing = [key for key in _JOB_REQUIRED[action] if key not in args]
        if missing:
            raise BadRequestError(f"{action} args need: {', '.join(missing)}")
        if action == "open":
            session = args.get("session")
            if isinstance(session, dict) and session.get("mode") == "merge":
                # D1/M4: a merge open runs the summarizer (up to 15 minutes) in
                # the scheduler thread; it stays a direct call, never a job.
                raise BadRequestError(
                    "open with a merge session is not allowed in a job",
                    code="merge_not_allowed_in_job",
                )
        if at is not None and at < self._now():
            raise BadRequestError("at is in the past")
        return self._jobs.add(action, args, at=at, every=every, until=until, now=self._now())

    def delete_job(self, job_id: str) -> bool:
        deleted = self._jobs.delete(job_id)
        if deleted:
            with self._lock:
                for rec in self._agents.values():
                    if rec.reopen_job == job_id:
                        # A deleted auto-reopen job must not keep showing a
                        # reopen_at that will never happen (M3).
                        rec.reopen_job = None
                        rec.reopen_at = None
                        break
        return deleted

    def list_jobs(self) -> list[Job]:
        return self._jobs.list()

    def run_job(self, job: Job) -> Any:
        args = job.args
        if job.action == "open":
            return self.open(**args)
        if job.action == "close":
            return self.close(args["id"])
        if job.action == "status":
            return self.get(args["id"])
        if job.action == "poke":
            return self.poke(args["id"], args["text"], bool(args.get("interrupt", False)))
        raise BadRequestError(f"unknown action {job.action!r}")

    def tick(self, now: float | None = None) -> None:
        """One scheduler beat: observe agents, then run whatever is due."""
        if self._closing:
            return  # no transitions, no jobs, while shutting down (D2/M5)
        now = self._now() if now is None else now
        self.refresh_states(now)
        self._run_periodic_imports(now)
        self._retry_pending_imports(now)
        for job in self._jobs.due(now):
            if self._jobs.get(job.id) is not job:
                continue  # deleted while we were looking
            job.last_run = now
            try:
                result = self.run_job(job)
            except SidecarError as e:
                job.last_error = {"status": e.status, "code": e.code, "message": str(e)}
                logger.warning("job %s (%s) failed: %s", job.id, job.action, e)
                self._jobs.finish(job, now)
                if job.auto:
                    self._retry_auto_job(job, now)
                continue
            except Exception as e:
                job.last_error = {
                    "status": 500,
                    "code": "internal_error",
                    "message": f"{type(e).__name__}: {e}",
                }
                logger.exception("job %s (%s) failed", job.id, job.action)
                self._jobs.finish(job, now)
                if job.auto:
                    self._retry_auto_job(job, now)
                continue
            job.last_error = None
            self._jobs.finish(job, now)
            if job.auto and isinstance(result, AgentRecord):
                # D4: an auto-reopen success resumes the work with resume_prompt.
                self._send_resume(result)

    # --- state watching ---------------------------------------------------

    def refresh_states(self, now: float | None = None) -> None:
        """Ask every running driver for its state and apply what it says.

        The scheduler thread is the only caller (D2/M4); one agent's failure
        is logged and confined to that agent, never raised to the caller.
        """
        with self._lock:
            records = list(self._agents.values())
        for rec in records:
            try:
                self._refresh_one(rec, now)
            except Exception:
                logger.exception("agent %s: state transition failed", rec.id)

    def _refresh_one(self, rec: AgentRecord, now: float | None = None) -> None:
        with self._lock:
            if rec.busy:
                return
            if rec.state not in ACTIVE_STATES or rec.handle is None:
                return
            if rec.closing:
                # close landed while a transition was in flight (M1/N1/N4): this
                # refresh is the one that stops the process and marks exited.
                rec.busy = True
                closing = True
            else:
                closing = False
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
        if closing:
            try:
                self._stop_and_exit(rec)
            finally:
                with self._lock:
                    rec.busy = False
            return
        self._observe_handle(rec, driver, handle, now)

    def _observe_handle(
        self, rec: AgentRecord, driver: Any, handle: Any, now: float | None = None
    ) -> None:
        """Read `handle`'s driver state and apply it.

        No busy gate here: the caller already owns the record's transition
        (H1's swap calls this right after launching the replacement handle,
        while `rec.busy` is still set), and a state observed on the fresh
        handle must land.
        """
        if driver is None:
            return
        try:
            st = driver.state(handle)
        except Exception:
            logger.exception("agent %s: driver.state failed", rec.id)
            return
        with self._lock:
            if self._agents.get(rec.id) is not rec or rec.handle is not handle:
                return  # replaced or switched while the driver was answering
        if st.kind == DRIVER_QUOTA_EXHAUSTED:
            # One transition per agent at a time: if another thread is already
            # swapping it, this observation is stale (D2/H2).
            with self._lock:
                if rec.busy:
                    return
                rec.busy = True
            try:
                self._quota_exhausted(rec, st, now)
            finally:
                with self._lock:
                    rec.busy = False
        elif st.kind == DRIVER_EXITED:
            with self._lock:
                rec.state = STATE_EXITED
                rec.exit = {"code": st.code, "reason": st.reason or "driver reported exited"}
                self._clear_pending(rec)
        elif st.kind in (DRIVER_RUNNING, DRIVER_IDLE):
            with self._lock:
                rec.state = st.kind
            self._flush_pending(rec)  # driver calls outside the lock (L1)

    def _quota_exhausted(self, rec: AgentRecord, st: DriverState, now: float | None = None) -> None:
        """D4: register the cooldown, then switch subscription or wait.

        Any failure while swapping lands the agent in waiting-quota with a
        `now + retry_interval` job instead of vanishing (M3): the agent is
        stopped if the failure came before the stop, and none of this reaches
        the HTTP response or another agent.
        """
        now = self._now() if now is None else now
        state = {"stopped": False}
        try:
            self._swap(rec, st, now, state)
        except Exception:
            logger.exception(
                "agent %s: the swap failed; waiting for the retry job", rec.id
            )
            if not state["stopped"]:
                self._stop_quietly(rec)
            self._wait_quota(rec, now, reopen_at=now + self._retry_interval)

    def _stop_quietly(self, rec: AgentRecord) -> None:
        try:
            with self._lock:
                driver = self._drivers.get(rec.agent)
                handle = rec.handle
            if handle is not None and driver is not None:
                driver.stop(handle, graceful=True, timeout=self._close_timeout)
        except Exception:
            logger.exception("agent %s: stopping after the failed swap failed", rec.id)

    def _swap(self, rec: AgentRecord, st: DriverState, now: float, state: dict) -> None:
        window = st.window if st.window in WINDOWS else "5h"
        if st.window not in WINDOWS:
            logger.warning(
                "agent %s: unknown quota window %r; treating it as 5h", rec.id, st.window
            )
        resets_at = st.resets_at  # RFC 3339 UTC or None (never guessed)
        driver = self._drivers.get(rec.agent)
        with self._source_op() as source:
            cooled = source.cooldown(rec.subscription, window, resets_at)
            if isinstance(cooled, SourceError) and resets_at is not None:
                logger.info(
                    "agent %s: cooldown with resets_at was refused (%s); "
                    "retrying without it",
                    rec.id, cooled.message or cooled.code,
                )
                cooled = source.cooldown(rec.subscription, window, None)
            if isinstance(cooled, (SourceError, Unreachable)):
                logger.warning(
                    "agent %s: cooldown registration failed (%r); the swap will "
                    "not rely on list availability",
                    rec.id, cooled,
                )
            # The subscription that just ran out is never a candidate in this
            # swap, even if list still calls it available (M2).
            exclude = {rec.subscription} if rec.subscription else ()
            subs = source.list()
            picked = None
            unreachable: Unreachable | None = None
            if isinstance(subs, Unreachable):
                unreachable = subs
            else:
                while True:
                    sid = self._selector.choose(rec.agent, subs, exclude=exclude)
                    if sid is None:
                        break
                    got = source.get(sid, rec.agent)
                    if isinstance(got, Unreachable):
                        unreachable = got
                        break
                    if isinstance(got, Subscription):
                        picked = got
                        break
                    exclude.add(sid)  # 403/404/409: unusable, try the next one

        if self._closing_requested(rec):
            # close landed before the stop: stop now and stay exited (N1).
            self._stop_and_exit(rec)
            state["stopped"] = True
            return
        if rec.new_mode:
            self._swap_new_mode(rec, picked, unreachable, subs, driver, now, state)
            return
        # Stop first, let agora write the last conversation back (D4/D6).
        if rec.handle is not None and driver is not None:
            driver.stop(rec.handle, graceful=True, timeout=self._close_timeout)
        state["stopped"] = True
        with self._lock:
            self._clear_pending(rec)

        if picked is not None:
            env = driver.env_for(picked)
            if rec.session_id is None:
                # `new` mode before the first import has no agora id to
                # continue (D5/3.5): wait for the controller instead.
                logger.warning(
                    "agent %s: quota exhausted but there is no agora session to continue",
                    rec.id,
                )
                self._wait_quota(rec, now)
                return
            running = self._store.start(
                mode="continue",
                ids=[rec.session_id],
                by=rec.agent,
                agent=rec.agent,
                workdir=rec.workdir,
                tag=f"sidecar:{rec.id}",
                env=env,
            )
            handle = driver.launch(running, env, rec.workdir)
            if self._closing_requested(rec):
                # close landed while we were relaunching: stop the fresh
                # process and do not put it into service (N1).
                self._stop_handle(handle, driver)
                self._set_exited(rec)
                return
            with self._lock:
                if self._agents.get(rec.id) is not rec:
                    return
                rec.handle = handle
                rec.session_id = running.session_id
                rec.subscription = picked.id
                rec.state = STATE_STARTING
                rec.reopen_at = None
                rec.reopen_job = None
            if self._closing_requested(rec):
                # The tiny window after the write: same treatment.
                self._stop_handle(handle, driver)
                self._set_exited(rec)
                return
            self._observe_handle(rec, driver, handle, now)
            self._send_resume(rec)
            return

        if unreachable is not None:
            logger.warning(
                "agent %s: LLMGateway cannot be reached (%s); retrying in %gs",
                rec.id, unreachable.reason, self._retry_interval,
            )
            self._wait_quota(rec, now, reopen_at=now + self._retry_interval)
            return
        self._wait_quota(rec, now, subs=subs)

    def _swap_new_mode(
        self,
        rec: AgentRecord,
        picked: Subscription | None,
        unreachable: Unreachable | None,
        subs: list[Subscription],
        driver: Any,
        now: float,
        state: dict,
    ) -> None:
        """A quota swap for `new` mode: import -> stop -> import -> continue (D5).

        An import that fails does not keep the agent alive (review H2, D5): the
        conversation stays in the agent's own session file, so the agent is
        stopped and the scheduler retries the import every `retry_interval`;
        only a successful import hangs the normal auto-reopen job.
        """
        external = self._external_session_id(rec)
        import_failed: Exception | None = None
        if external is not None:
            with self._lock:
                rec.external_session_id = external
            try:
                agora_id = self._import_new_mode(rec, external)
                with self._lock:
                    rec.session_id = agora_id
            except Exception as e:
                import_failed = e
                logger.warning(
                    "agent %s: import before the swap failed (%s); stopping anyway", rec.id, e
                )
        if self._closing_requested(rec):
            self._stop_and_exit(rec)
            state["stopped"] = True
            return
        if rec.handle is not None and driver is not None:
            driver.stop(rec.handle, graceful=True, timeout=self._close_timeout)
        state["stopped"] = True
        with self._lock:
            self._clear_pending(rec)
        if external is not None and import_failed is None:
            try:
                self._import_new_mode(rec, external)  # write back the final words
            except Exception as e:
                import_failed = e
                logger.warning("agent %s: final import before continue failed: %s", rec.id, e)
        if import_failed is not None:
            # D5/H2: the agent is stopped; the scheduler retries the import and
            # only its success hangs the auto-reopen job.
            self._wait_import_retry(rec, now)
            return

        if picked is None:
            if unreachable is not None:
                logger.warning(
                    "agent %s: LLMGateway cannot be reached (%s); retrying in %gs",
                    rec.id, unreachable.reason, self._retry_interval,
                )
                self._wait_quota(rec, now, reopen_at=now + self._retry_interval)
            else:
                self._wait_quota(rec, now, subs=subs)
            return

        with self._lock:
            session_id = rec.session_id
        if session_id is None:
            # The agent never announced its session id: nothing to continue.
            logger.warning("agent %s: no agora id to continue after the import", rec.id)
            self._wait_quota(rec, now)
            return
        env = driver.env_for(picked)
        running = self._store.start(
            mode="continue",
            ids=[session_id],
            by=rec.agent,
            agent=rec.agent,
            workdir=rec.workdir,
            tag=f"sidecar:{rec.id}",
            env=env,
        )
        handle = driver.launch(running, env, rec.workdir)
        if self._closing_requested(rec):
            self._stop_handle(handle, driver)
            self._set_exited(rec)
            return
        with self._lock:
            if self._agents.get(rec.id) is not rec:
                return
            rec.handle = handle
            rec.session_id = running.session_id
            rec.subscription = picked.id
            rec.state = STATE_STARTING
            rec.new_mode = False  # it runs under agora from now on
            rec.next_import = 0.0
            rec.reopen_at = None
            rec.reopen_job = None
        if self._closing_requested(rec):
            self._stop_handle(handle, driver)
            self._set_exited(rec)
            return
        self._observe_handle(rec, driver, handle, now)
        self._send_resume(rec)

    def _wait_quota(
        self,
        rec: AgentRecord,
        now: float,
        *,
        subs: list[Subscription] | None = None,
        reopen_at: float | None = None,
    ) -> None:
        """Go waiting-quota and hang the auto-reopen job (D4/M3)."""
        with self._lock:
            if self._agents.get(rec.id) is not rec:
                return
            if rec.closing:
                # close landed after the stop, before the wait: stay exited
                # instead of waiting for a reopen nobody wants (N1).
                rec.state = STATE_EXITED
                rec.exit = {"code": None, "reason": "closed during a transition"}
                rec.reopen_at = None
                rec.reopen_job = None
                return
            rec.state = STATE_WAITING_QUOTA
            rec.reopen_at = None
            rec.reopen_job = None
        if reopen_at is None and subs is not None:
            reopen_at = self._earliest_lift(rec.agent, subs, now=now)
        if reopen_at is not None and reopen_at <= now:
            # The computed lift is already in the past but nothing is usable
            # (e.g. LLMGateway still reports cooling): retry at the fixed
            # interval instead of hammering once per tick.
            reopen_at = now + self._retry_interval
        if reopen_at is None or rec.session_id is None:
            # No candidate to reopen on: no job, reopen_at is null (M3).
            logger.info(
                "agent %s: waiting-quota with no candidate to reopen on", rec.id
            )
            return
        job = self._jobs.add(
            "open",
            {
                "id": rec.id,
                "agent": rec.agent,
                "workdir": rec.workdir,
                "session": {"mode": "continue", "from": rec.session_id},
            },
            at=reopen_at,
            auto=True,
            now=now,
        )
        with self._lock:
            if self._agents.get(rec.id) is rec:
                rec.reopen_job = job.id
                rec.reopen_at = reopen_at

    def _retry_auto_job(self, job: Job, now: float) -> None:
        """An auto-reopen job that failed gets recomputed and re-hung (M3)."""
        agent_id = job.args.get("id") if isinstance(job.args, dict) else None
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None or rec.state != STATE_WAITING_QUOTA:
                return
        self._schedule_reopen(rec, now)

    def _schedule_reopen(self, rec: AgentRecord, now: float) -> None:
        with self._source_op() as source:
            subs = source.list()
        if isinstance(subs, Unreachable):
            logger.warning(
                "agent %s: LLMGateway cannot be reached (%s); retrying in %gs",
                rec.id, subs.reason, self._retry_interval,
            )
            self._wait_quota(rec, now, reopen_at=now + self._retry_interval)
            return
        self._wait_quota(rec, now, subs=subs)

    def _earliest_lift(self, agent: str, subs: list[Subscription], *, now: float) -> float | None:
        """The earliest moment a usable candidate stops cooling (D4/M3).

        Candidates: `agents` contains the kind and the key is deployed; a row
        that says unavailable with no window cooling is "not deployed" and is
        not a candidate at all.  A candidate's lift is the later of its two
        windows' `until`; a deployed candidate with no cooling at all is
        usable now, so its lift is `now`.  Only `agents`/`cooldown` are
        consulted, never the Selector.
        """
        lifts = []
        for sub in subs:
            if agent not in sub.agents or not sub.is_deployed():
                continue
            untils = [epoch(sub.cooldown.get(w)) for w in WINDOWS if sub.cooldown.get(w)]
            untils = [t for t in untils if t is not None]
            lifts.append(max(untils) if untils else now)
        return min(lifts) if lifts else None

    def _send_resume(self, rec: AgentRecord) -> None:
        if not self._resume_prompt:
            return
        with self._lock:
            if rec.state not in (STATE_RUNNING, STATE_IDLE) or rec.handle is None:
                return
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
        driver.send(handle, self._resume_prompt)
        with self._lock:
            if self._agents.get(rec.id) is rec:
                rec.state = STATE_RUNNING

    def _enqueue_poke(self, rec: AgentRecord, text: str, interrupt: bool = False) -> None:
        """Queue a poke and try to deliver it right away (open's first input, L5).

        Never waits and never fails the caller: if the agent is still starting,
        the item stays queued until a refresh sees it up.
        """
        with self._cond:
            rec.pending.append(
                PendingPoke(
                    text=text,
                    interrupt=interrupt,
                    deadline=time.monotonic() + self._poke_queue_timeout,
                )
            )
        self._flush_pending(rec)

    def _take_pending(self, rec: AgentRecord) -> tuple[Any, Any, list[PendingPoke]] | None:
        """Move the flushable pending items out (lock held)."""
        if not rec.pending or rec.state not in (STATE_RUNNING, STATE_IDLE):
            return None
        if rec.handle is None:
            return None
        driver = self._drivers.get(rec.agent)
        if driver is None:
            return None
        items, rec.pending = rec.pending, []
        return driver, rec.handle, items

    def _flush_pending(self, rec: AgentRecord) -> None:
        """Send queued pokes now that the agent is up; driver calls are outside
        the lock (L1) and waiters are woken either way."""
        with self._lock:
            taken = self._take_pending(rec)
        if taken is None:
            return
        driver, handle, items = taken
        try:
            for item in items:
                if item.interrupt:
                    driver.interrupt(handle)
                driver.send(handle, item.text)
                item.sent = True
        finally:
            with self._lock:
                if self._agents.get(rec.id) is rec and rec.state in (STATE_RUNNING, STATE_IDLE):
                    rec.state = STATE_RUNNING
                self._cond.notify_all()

    def _clear_pending(self, rec: AgentRecord) -> None:
        """Drop queued pokes (the agent is gone); waiters see sent=False (lock held)."""
        if rec.pending:
            rec.pending.clear()
            self._cond.notify_all()

    # --- new mode: periodic agora imports (D5/3.5) -------------------------

    def _run_periodic_imports(self, now: float) -> None:
        with self._lock:
            due = [
                rec for rec in self._agents.values()
                if rec.new_mode and rec.state in ACTIVE_STATES and now >= rec.next_import
            ]
        for rec in due:
            with self._lock:
                rec.next_import = now + self._sync_period  # schedule before the slow call
            external = self._external_session_id(rec)
            if external is None:
                continue  # nothing has happened in the agent yet
            try:
                agora_id = self._import_new_mode(rec, external)
            except Exception as e:
                logger.warning("agent %s: periodic import failed: %s", rec.id, e)
                continue
            with self._lock:
                if self._agents.get(rec.id) is rec:
                    rec.session_id = agora_id

    def _external_session_id(self, rec: AgentRecord) -> str | None:
        with self._lock:
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
        if driver is None or handle is None:
            return None
        try:
            return driver.external_session_id(handle)
        except Exception:
            logger.exception("agent %s: external_session_id failed", rec.id)
            return None

    def _import_new_mode(self, rec: AgentRecord, external: str) -> str:
        if self._store is None:
            raise UnavailableError("session store is not configured")
        return self._store.import_external(
            external_id=external, agent=rec.agent, tag=f"sidecar:{rec.id}"
        )

    def _retry_pending_imports(self, now: float) -> None:
        """`new`-mode agents whose swap import failed: retry it (D5/H2).

        The agent is already stopped and waiting-quota; the conversation is
        still in the agent's own session file.  Every `retry_interval` the
        scheduler tries the import again; its first success hangs the normal
        auto-reopen job (reopen_at holds the next retry until then).
        """
        with self._lock:
            due = [
                rec for rec in self._agents.values()
                if rec.import_pending
                and rec.state == STATE_WAITING_QUOTA
                and rec.reopen_at is not None
                and now >= rec.reopen_at
            ]
        for rec in due:
            with self._lock:
                if rec.busy:
                    continue  # a transition (e.g. close) owns it
                rec.busy = True
            try:
                self._retry_import_one(rec, now)
            except Exception:
                logger.exception("agent %s: the import retry failed", rec.id)
            finally:
                with self._lock:
                    rec.busy = False
                    closed = rec.closing and rec.state == STATE_WAITING_QUOTA
                if closed:
                    self._set_exited(rec)

    def _retry_import_one(self, rec: AgentRecord, now: float) -> None:
        external = rec.external_session_id
        if external is None:
            return  # nothing to import; the controller decides
        try:
            agora_id = self._import_new_mode(rec, external)
        except Exception as e:
            logger.warning("agent %s: import retry failed: %s", rec.id, e)
            with self._lock:
                if self._agents.get(rec.id) is rec:
                    rec.reopen_at = now + self._retry_interval
            return
        with self._lock:
            if self._agents.get(rec.id) is not rec:
                return
            rec.session_id = agora_id
            rec.import_pending = False
        # The import succeeded: hang the normal auto-reopen job (D4).
        self._schedule_reopen(rec, now)

    def _wait_import_retry(self, rec: AgentRecord, now: float) -> None:
        """Waiting-quota until the scheduler imports the agent's session (H2)."""
        with self._lock:
            if self._agents.get(rec.id) is not rec:
                return
            if rec.closing:
                rec.state = STATE_EXITED
                rec.exit = {"code": None, "reason": "closed during a transition"}
                rec.import_pending = False
                rec.reopen_at = None
                rec.reopen_job = None
                return
            rec.state = STATE_WAITING_QUOTA
            rec.import_pending = True
            rec.reopen_at = now + self._retry_interval
            rec.reopen_job = None
        logger.info(
            "agent %s: waiting-quota until the next import retry in %gs",
            rec.id, self._retry_interval,
        )

    def _close_new_mode(
        self, rec: AgentRecord, driver: Any, handle: Any, *, waiting: bool
    ) -> AgentRecord:
        """close for `new` mode: import -> stop -> import (D5/3.5)."""
        external = rec.external_session_id or self._external_session_id(rec)
        note: str | None = None
        if external is not None:
            try:
                agora_id = self._import_new_mode(rec, external)
                with self._lock:
                    rec.session_id = agora_id
            except Exception as e:
                note = f"no write-back ({e})"
                logger.warning("agent %s: import before close failed: %s", rec.id, e)
        if waiting or handle is None or driver is None:
            final = DriverState(kind=DRIVER_EXITED, reason="closed without a running agent")
        else:
            final = self._stop_handle(handle, driver)
        if external is not None and note is None:
            try:
                self._import_new_mode(rec, external)  # the final conversation
            except Exception as e:
                note = f"final write-back failed ({e})"
                logger.warning("agent %s: final import on close failed: %s", rec.id, e)
        with self._lock:
            rec.state = STATE_EXITED
            rec.exit = {"code": final.code, "reason": note or final.reason or "closed"}
            rec.new_mode = False
            rec.import_pending = False
            rec.reopen_at = None
            rec.reopen_job = None
        return rec

    # --- lifecycle --------------------------------------------------------

    def begin_shutdown(self) -> None:
        """Enter shutting down: new open/poke/jobs answer 503, ticks do nothing.

        Called before the scheduler is stopped; the caller then joins it and
        calls `shutdown()` to close every agent (D2/M5).
        """
        with self._lock:
            self._closing = True

    def _refuse_if_closing(self) -> None:
        with self._lock:
            closing = self._closing
        if closing:
            raise ShuttingDownError("sidecar is shutting down")

    def shutdown(self) -> None:
        """Stop every agent gracefully before the daemon goes away (design D2)."""
        self.begin_shutdown()
        with self._lock:
            ids = [r.id for r in self._agents.values() if r.state != STATE_EXITED]
        for agent_id in ids:
            try:
                self.close(agent_id)
            except Exception:
                logger.exception("agent %s: close on shutdown failed", agent_id)

    # --- subscription source helpers -------------------------------------

    def _source_op(self) -> Any:
        """One logical operation gets one LLMGateway connection (D7/S1).

        Sources that do not implement `operation` (the file source, test
        fakes) are used directly.
        """
        source = self._subscriptions
        op = getattr(source, "operation", None)
        if op is None:
            return nullcontext(source)
        return op()

    def _get_specified(self, source: Any, subscription_id: str, agent: str) -> Subscription:
        """get() for a caller-specified id, mapped per the D4 table."""
        res = source.get(subscription_id, agent)
        if isinstance(res, Unreachable):
            raise GatewayUnreachableError(res.reason or "LLMGateway cannot be reached")
        if isinstance(res, SourceError):
            if res.code == "agent_not_allowed":
                raise SubscriptionNotAllowedError(
                    f"subscription {subscription_id!r} does not allow agent {agent!r}"
                )
            if res.code == "not_found":
                raise SubscriptionNotFoundError(f"unknown subscription {subscription_id!r}")
            if res.code == "not_deployed":
                raise NotDeployedError(f"subscription {subscription_id!r} has no key deployed")
            raise BadRequestError(
                f"subscription {subscription_id!r} refused: {res.message or res.code}",
                code=res.code,
            )
        return res

    def _choose_one(
        self, source: Any, agent: str, exclude: Any
    ) -> tuple[Subscription | None, Unreachable | None]:
        """Pick candidates in selector order; a refused one means 'next' (D4).

        Returns (picked, None), (None, None) when nothing is usable, or
        (None, unreachable) when the gateway cannot be asked.
        """
        subs = source.list()
        if isinstance(subs, Unreachable):
            return None, subs
        excluded = set(exclude)
        while True:
            sid = self._selector.choose(agent, subs, exclude=excluded)
            if sid is None:
                return None, None
            got = source.get(sid, agent)
            if isinstance(got, Unreachable):
                return None, got
            if isinstance(got, Subscription):
                return got, None
            excluded.add(sid)  # 403/404/409: this one is unusable, try the next
