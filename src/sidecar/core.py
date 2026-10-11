"""The sidecar core: the four actions over the internal seams (design D1-D5).

State (agents and jobs) lives only in this object - always in memory, never on
disk (design D2).  All public methods take the lock while mutating; driver and
store calls happen outside it so a slow stop cannot freeze status.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
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
    STATE_RUNNING,
    STATE_STARTING,
    STATE_WAITING_QUOTA,
    WINDOWS,
    BadRequestError,
    ConflictError,
    DriverState,
    NotFoundError,
    PreparedSession,
    SidecarError,
    UnavailableError,
    iso,
    valid_id,
)
from .selectors import PrioritySelector

logger = logging.getLogger(__name__)

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


def parse_session(spec: Any) -> tuple[str, list[str]]:
    """Validate the open `session` object; return (mode, ids)."""
    if not isinstance(spec, dict):
        raise BadRequestError("session must be a JSON object with a mode")
    mode = spec.get("mode")
    if mode not in SESSION_MODES:
        raise BadRequestError(f"session mode must be one of {', '.join(SESSION_MODES)}")
    if mode == "new":
        return "new", []
    source = spec.get("from")
    if mode == "merge":
        if isinstance(source, str) and source:
            ids = [source]
        elif isinstance(source, list) and source and all(isinstance(i, str) and i for i in source):
            ids = list(source)
        else:
            raise BadRequestError("merge session needs from: one or more agora ids")
        return mode, ids
    if not isinstance(source, str) or not source:
        raise BadRequestError(f"{mode} session needs from: one agora id")
    return mode, [source]


@dataclass
class AgentRecord:
    id: str
    agent: str
    workdir: str
    state: str
    started_at: float
    session: PreparedSession | None = None
    subscription: str | None = None
    handle: Any = None
    exit: dict[str, Any] | None = None
    reopen_at: float | None = None
    reopen_job: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The GET /agents shape (design D1): no key, nothing but the listed fields."""
        out = {
            "id": self.id,
            "agent": self.agent,
            "session": self.session.agora_id if self.session else None,
            "subscription": self.subscription,
            "state": self.state,
            "started_at": iso(self.started_at),
            "exit": self.exit,
        }
        if self.state == STATE_WAITING_QUOTA:
            out["reopen_at"] = iso(self.reopen_at)
        return out


class SidecarCore:
    def __init__(
        self,
        *,
        drivers: Mapping[str, Any],
        store: Any | None,
        subscriptions: Any | None = None,
        selector: Any | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._drivers = dict(drivers)
        self._store = store
        self._subscriptions = subscriptions
        self._selector = selector or PrioritySelector()
        self._now = now
        self._lock = threading.RLock()
        self._agents: dict[str, AgentRecord] = {}
        self._jobs = JobRegistry()

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
        if not isinstance(agent, str) or not agent:
            raise BadRequestError("agent must be a non-empty string")
        if not isinstance(workdir, str) or not workdir:
            raise BadRequestError("workdir must be a non-empty string")
        mode, ids = parse_session(session)
        if input is not None and not isinstance(input, str):
            raise BadRequestError("input must be a string")
        if subscription is not None and (
            not isinstance(subscription, dict) or not isinstance(subscription.get("id"), str)
        ):
            raise BadRequestError('subscription must look like {"id": ...}')
        driver = self._drivers.get(agent)
        if driver is None:
            raise BadRequestError(f"unsupported agent kind {agent!r}")
        if self._store is None or self._subscriptions is None:
            raise UnavailableError("session store or subscription source is not configured")

        now = self._now()
        with self._lock:
            existing = self._agents.get(id)
            if existing is not None and existing.state in ACTIVE_STATES:
                raise ConflictError(f"agent {id!r} already exists in state {existing.state}")
            # From here the id is reserved as starting, so a concurrent open of
            # the same id loses the race here instead of double-launching.
            placeholder = AgentRecord(
                id=id, agent=agent, workdir=workdir, state=STATE_STARTING, started_at=now
            )
            self._agents[id] = placeholder
            previous_job = existing.reopen_job if existing is not None else None

        try:
            sid = subscription["id"] if subscription is not None else self._pick_subscription(agent)
            sub = self._subscriptions.get(sid, agent)
            env = driver.env_for(sub)
            prepared = self._store.start(
                mode=mode, ids=ids, agent=agent, workdir=workdir, tag=f"sidecar:{id}"
            )
            handle = driver.launch(prepared, env, workdir)
        except BaseException:
            with self._lock:
                if self._agents.get(id) is placeholder:
                    if existing is not None:
                        self._agents[id] = existing
                    else:
                        del self._agents[id]
            raise

        with self._lock:
            placeholder.session = prepared
            placeholder.subscription = sid
            placeholder.handle = handle
            if previous_job:
                # Same id reopened from waiting-quota: its auto-reopen job goes.
                self._jobs.delete(previous_job)
        if input is not None:
            self.poke(id, input)
        self._refresh_one(placeholder)
        return placeholder

    def close(self, id: str) -> AgentRecord:
        with self._lock:
            rec = self._agents.get(id)
            if rec is None:
                raise NotFoundError(f"unknown agent {id!r}")
            if rec.reopen_job:
                self._jobs.delete(rec.reopen_job)
                rec.reopen_job = None
            if rec.state == STATE_EXITED:
                return rec
            driver = self._drivers.get(rec.agent)
            handle, session = rec.handle, rec.session
            waiting = rec.state == STATE_WAITING_QUOTA
        if waiting or handle is None or driver is None:
            final = DriverState(kind=DRIVER_EXITED, reason="closed without a running agent")
        else:
            driver.stop(handle, graceful=True)
            final = driver.state(handle)
        if self._store is not None and session is not None:
            self._store.sync(session)  # write the last conversation back to agora
        with self._lock:
            rec.state = STATE_EXITED
            rec.exit = {"code": final.code, "reason": final.reason or "closed"}
        return rec

    def poke(self, id: str, text: str, interrupt: bool = False) -> AgentRecord:
        if not isinstance(text, str) or not text:
            raise BadRequestError("text must be a non-empty string")
        with self._lock:
            rec = self._agents.get(id)
            if rec is None:
                raise NotFoundError(f"unknown agent {id!r}")
            if rec.state not in ACTIVE_STATES:
                raise ConflictError(f"agent {id!r} is {rec.state}; poke needs it running")
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
        if interrupt:
            driver.interrupt(handle)
        driver.send(handle, text)
        with self._lock:
            if self._agents.get(id) is rec:
                rec.state = STATE_RUNNING
        return rec

    def get(self, id: str) -> AgentRecord:
        self.refresh_states()
        with self._lock:
            rec = self._agents.get(id)
        if rec is None:
            raise NotFoundError(f"unknown agent {id!r}")
        return rec

    def list_agents(self) -> list[AgentRecord]:
        self.refresh_states()
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
        return self._jobs.add(action, args, at=at, every=every, until=until, now=self._now())

    def delete_job(self, job_id: str) -> bool:
        return self._jobs.delete(job_id)

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
        now = self._now() if now is None else now
        self.refresh_states(now)
        for job in self._jobs.due(now):
            if self._jobs.get(job.id) is not job:
                continue  # deleted while we were looking
            try:
                self.run_job(job)
            except SidecarError as e:
                logger.warning("job %s (%s) failed: %s", job.id, job.action, e)
            except Exception:
                logger.exception("job %s (%s) failed", job.id, job.action)
            self._jobs.finish(job, now)

    # --- state watching ---------------------------------------------------

    def refresh_states(self, now: float | None = None) -> None:
        """Ask every running driver for its state and apply what it says."""
        with self._lock:
            records = list(self._agents.values())
        for rec in records:
            self._refresh_one(rec, now)

    def _refresh_one(self, rec: AgentRecord, now: float | None = None) -> None:
        with self._lock:
            if rec.state not in ACTIVE_STATES or rec.handle is None:
                return
            driver = self._drivers.get(rec.agent)
            handle = rec.handle
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
            self._quota_exhausted(rec, st, now)
        elif st.kind == DRIVER_EXITED:
            with self._lock:
                rec.state = STATE_EXITED
                rec.exit = {"code": st.code, "reason": st.reason or "driver reported exited"}
        elif st.kind in (DRIVER_RUNNING, DRIVER_IDLE):
            with self._lock:
                rec.state = st.kind

    def _quota_exhausted(self, rec: AgentRecord, st: DriverState, now: float | None = None) -> None:
        """D4: register the cooldown, then switch or schedule a reopen."""
        now = self._now() if now is None else now
        window = st.window if st.window in WINDOWS else "5h"
        resets_at = st.resets_at if st.resets_at is not None else now
        self._subscriptions.cooldown(rec.subscription, window, resets_at)
        logger.info(
            "agent %s: subscription %s cooling down on %s until %s",
            rec.id, rec.subscription, window, iso(resets_at),
        )
        subs = self._subscriptions.list()
        picked = self._selector.choose(rec.agent, subs)
        driver = self._drivers.get(rec.agent)
        if picked is not None:
            sub = self._subscriptions.get(picked, rec.agent)
            env = driver.env_for(sub)
            driver.stop(rec.handle, graceful=True)
            self._store.sync(rec.session)
            prepared = self._store.start(
                mode="continue",
                ids=[rec.session.agora_id],
                agent=rec.agent,
                workdir=rec.workdir,
                tag=f"sidecar:{rec.id}",
            )
            handle = driver.launch(prepared, env, rec.workdir)
            with self._lock:
                if self._agents.get(rec.id) is not rec:
                    return
                rec.handle, rec.session, rec.subscription = handle, prepared, picked
                rec.state = STATE_STARTING
                rec.reopen_at = None
            self._refresh_one(rec, now)
            return
        # Every subscription for this kind is cooling down: stop, write back,
        # wait, and hang the same kind of job a caller could have made (D4).
        driver.stop(rec.handle, graceful=True)
        self._store.sync(rec.session)
        reopen_at = self._earliest_lift(rec.agent, subs, now=now)
        if reopen_at is None:
            reopen_at = resets_at
        with self._lock:
            if self._agents.get(rec.id) is not rec:
                return
            rec.state = STATE_WAITING_QUOTA
            rec.reopen_at = reopen_at
        job = self._jobs.add(
            "open",
            {
                "id": rec.id,
                "agent": rec.agent,
                "workdir": rec.workdir,
                "session": {"mode": "continue", "from": rec.session.agora_id},
            },
            at=reopen_at,
            auto=True,
            now=now,
        )
        with self._lock:
            if self._agents.get(rec.id) is rec:
                rec.reopen_job = job.id

    def _earliest_lift(self, agent: str, subs: list[Any], *, now: float) -> float | None:
        """The earliest moment any subscription usable by `agent` cools off."""
        times = []
        for sub in subs:
            if agent not in sub.agents:
                continue
            for window in WINDOWS:
                until = sub.cooldown.get(window)
                if until is not None and until > now:
                    times.append(until)
        return min(times) if times else None

    # --- lifecycle --------------------------------------------------------

    def shutdown(self) -> None:
        """Stop every agent gracefully before the daemon goes away (design D2)."""
        with self._lock:
            ids = [r.id for r in self._agents.values() if r.state != STATE_EXITED]
        for agent_id in ids:
            try:
                self.close(agent_id)
            except Exception:
                logger.exception("agent %s: close on shutdown failed", agent_id)

    # --- subscription picking --------------------------------------------

    def _pick_subscription(self, agent: str) -> str:
        subs = self._subscriptions.list()
        sid = self._selector.choose(agent, subs)
        if sid is None:
            raise ConflictError(f"no available subscription for {agent!r}")
        return sid
