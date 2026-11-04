"""`pmo wake set|off|show` and tick's wake-job health (design §3.6; #358 T5-4).

The PMO decides whether to be woken on a schedule and how often; this module
only does the mechanical part (D-7): turn the chosen interval into **exactly
one** poke job of the PMO's own on `pmo_sidecar`, and let one tick step report
whether it is still there.

Which jobs are「自己的」is §3.6's content rule, not an owner field: `action` is
`poke`, `args.text` equals the configured wake sentence, and `auto` is not
true. `args.id` is never part of the test (a job left under an old PMO id must
still be cleanable). `pmo.ops.is_own_job` already spells that rule, so `pmo
close` and this module cannot drift apart. Every other poke job is only listed
(`wake show`) and never deleted.

Every HTTP call uses the port's 10-second timeout (design §3.6 step 4), and the
whole `set`／`off` flow holds `wake.lock` (at most 30 seconds), so two of them
queue and never leave two jobs behind. Tick does **not** take `wake.lock`, so a
`set` stuck on HTTP never slows a round down. `tick.json.lock` is taken only at
the very end and the file is re-read inside it, so a `wake.every` that tick just
wrote is not overwritten by a snapshot this command read earlier (§3.4).

`wake set` is「先建後刪」: there is no window in which nobody would wake the
PMO; the price is a moment with two jobs and at most one extra round of tick,
which is harmless (§3.6 step 3). A delete that fails is a `[warn]`, not an exit
code — the new job is already in place. The one non-atomic path is a `POST`
whose 2xx body cannot be read: the job exists but this tool did not see it, so
`set` looks it up once with `GET /jobs` instead of reporting a failed create
(裁定第 1 節); when even that cannot find it, nothing is deleted and
`wake.every` is not written, so the next tick's「自己的叫醒 job 有 n 個」and a
second `wake set` are the way out.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

from . import _lock, ops, teammsg, teamstate, tickstate
from .due import earliest_due
from .errors import UsageError
from .ports import (
    JobCreatedUnreadable,
    JobRecord,
    PortError,
    SidecarError,
    seconds_of,
    sidecar_code,
)
from .runtime import Ctx, sidecar_port
from .timeutil import iso_utc

if TYPE_CHECKING:  # pragma: no cover - typing only; `tick` imports this module
    from .tick import TickRun

__all__ = [
    "MAX_EVERY_SECONDS",
    "MIN_EVERY_SECONDS",
    "TICK_WRITE_TIMEOUT",
    "WAKE_LOCK_TIMEOUT",
    "WakeOff",
    "WakeSet",
    "WakeShow",
    "jobs_warn",
    "parse_every",
    "set_interval",
    "show",
    "step_job_health",
    "turn_off",
]

#: How long `set`／`off` wait for `wake.lock` (design §3.6 step 4: 最多 30 秒).
WAKE_LOCK_TIMEOUT = 30.0

#: How long the final `tick.json` write waits for its lock (design §3.4: 10 秒).
TICK_WRITE_TIMEOUT = 10.0

#: The interval bounds `pmo wake set` accepts (design §3.6 step 1 / Q-8: 2m～24h).
MIN_EVERY_SECONDS = 2 * 60.0
MAX_EVERY_SECONDS = 24 * 60 * 60.0

#: The lock file beside `tick.json` (a different lock from `tick.json.lock`).
WAKE_LOCK_NAME = "wake.lock"


# --------------------------------------------------------------------------- #
# 間隔的驗證（純函式）
# --------------------------------------------------------------------------- #


def parse_every(text: str) -> tuple[str, float]:
    """One `pmo wake set` argument → `(as written, seconds)`.

    Only `‹integer›m` or `‹integer›h` is accepted, 2m through 24h: the bounds
    are the tool's own (Q-8), so a mistyped `600` (seconds) cannot quietly
    become 600 minutes; anything longer than a day is `pmo wake off`'s job.
    The grammar is `tickstate.EVERY_RE`, the one `tick.json`'s `wake.every`
    must also match, so a value this command accepts is a value tick can read.
    """
    every = str(text).strip()
    if tickstate.EVERY_RE.fullmatch(every) is None:
        raise UsageError(
            f"叫醒間隔要寫成 ‹整數›m 或 ‹整數›h（例：10m、2h），收到「{every}」"
        )
    seconds = seconds_of(every)
    if not MIN_EVERY_SECONDS <= seconds <= MAX_EVERY_SECONDS:
        raise UsageError(
            f"叫醒間隔要在 2m～24h 之間（收到「{every}」＝{seconds:g} 秒）；"
            f"要比 24 小時更久就用 pmo wake off"
        )
    return every, seconds


def jobs_warn(error: PortError) -> str:
    """Tick's `[warn]` detail for a failed `GET /jobs` (design §3.6's table).

    A sidecar failure prints its known code (`agent_not_running`, `HTTP 500`,
    「連不上」); any other port error is already a one-line known string.
    """
    if isinstance(error, SidecarError):
        return f"GET /jobs：{sidecar_code(error)}"
    return f"GET /jobs：{error}"


# --------------------------------------------------------------------------- #
# 埠與自己的 job
# --------------------------------------------------------------------------- #


def _pmo_port(ctx: Ctx):
    """The sidecar port for `pmo_sidecar`, or the refusal that is exit 2.

    The PMO's own wake job lives on the sidecar the PMO itself runs in; when
    the settings name no `pmo_sidecar` the PMO is not in one, and `pmo wake`
    has nothing to talk to (design §3.4). A named address with no port wired
    is the same exit 2 with an honest reason.
    """
    address = ctx.config.pmo_sidecar
    if address is None:
        raise SidecarError(
            detail="PMO 不在 sidecar（設定沒有 pmo_sidecar），沒有自己的叫醒 job"
        )
    port = sidecar_port(ctx.deps, address)
    if port is None:
        raise SidecarError(detail="sidecar 埠沒有接上")
    return port


def _own_jobs(jobs: Iterable[JobRecord], ctx: Ctx) -> list[JobRecord]:
    """This PMO's wake jobs: §3.6's content rule, shared with `pmo close`."""
    return [job for job in jobs if ops.is_own_job(job, ctx.config.wake_text)]


def _same_interval(job: JobRecord, seconds: float) -> bool:
    """Whether the job's `every` is this many seconds (`GET /jobs` says 600.0).

    Both shapes are translated to seconds (design §3.6 step 2, M5-1): a job
    with no `every` (an `at` job) is never the interval the PMO asked for.
    """
    if job.every is None:
        return False
    try:
        return seconds_of(job.every) == seconds
    except SidecarError:
        return False


def _keep(jobs: Sequence[JobRecord], wanted: float, agent_id: str) -> JobRecord | None:
    """The one job `wake set` may leave untouched: same interval, current id.

    Exactly one own job, its `every` is the requested interval, and its
    `args.id` is the PMO's current seat: then the sidecar is not touched at
    all. A job under another id (the PMO was reopened under a new name) is kept
    only long enough to be deleted; a different interval needs the new job
    first (design §3.6 step 2).
    """
    if len(jobs) != 1:
        return None
    job = jobs[0]
    if not _same_interval(job, wanted):
        return None
    if not ops.job_targets_agent(job, agent_id):
        return None
    return job


# --------------------------------------------------------------------------- #
# 結果（CLI 只印；這裡不碰 stdout）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WakeSet:
    """What `pmo wake set` did: the job that now carries the interval."""

    every: str
    job: JobRecord
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WakeOff:
    """What `pmo wake off` did: the jobs deleted, and the ones it could not."""

    deleted: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WakeShow:
    """`pmo wake show`'s read-only snapshot (only known fields reach the printer)."""

    every: str | None
    set_at: str | None
    last_tick_at: str | None
    next_due_at: str | None
    jobs: tuple[JobRecord, ...] = ()
    other_jobs: tuple[JobRecord, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "wake": {"every": self.every, "set_at": self.set_at},
            "last_tick_at": self.last_tick_at,
            "next_due_at": self.next_due_at,
            "jobs": [job_dict(job) for job in self.jobs],
            "other_jobs": [job_dict(job) for job in self.other_jobs],
        }


def next_run_at(job: JobRecord) -> str | None:
    """The sidecar's `next_run_at` off the record (`extra`: it is not typed)."""
    value = job.extra.get("next_run_at")
    return value if isinstance(value, str) else None


def job_dict(job: JobRecord) -> dict[str, object]:
    """One job's known fields, the way `wake show --json` prints them (D-12)."""
    return {
        "id": job.job_id,
        "action": job.action,
        "every": job.every,
        "next_run_at": next_run_at(job),
        "last_run": job.last_run,
        "last_error": job.last_error,
    }


# --------------------------------------------------------------------------- #
# `pmo wake set|off|show`
# --------------------------------------------------------------------------- #


def _lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / WAKE_LOCK_NAME


def _tick_file(ctx: Ctx) -> Path:
    return tickstate.tick_path(ctx.state_dir)


def _write_wake(ctx: Ctx, *, now: datetime, every: str | None) -> None:
    """Set `wake.every` in `tick.json`, briefly, re-reading inside its lock (§3.4).

    The design keeps the two locks apart: the whole `set`／`off` flow holds
    `wake.lock`, and only this last step takes `tick.json.lock`. The file is
    re-read inside that lock, so `last_tick_at` and the「已印過」fields tick
    wrote while this command was out on HTTP are kept; only `wake.every`,
    `wake.set_at` and the two「已回報過」marks are touched (a `wake set|off` is
    the moment those marks stop meaning anything).

    A damaged file raises `SchemaError` before anything is written; `set`／`off`
    read it once more before talking to the sidecar, so a bad file is refused
    before any job is created or deleted.
    """
    path = _tick_file(ctx)
    lock = tickstate.lock_path(path)
    with _lock.path_lock(lock, timeout=TICK_WRITE_TIMEOUT):
        state = tickstate.load_or_empty(path)
        wake = replace(
            state.wake,
            every=every,
            set_at=iso_utc(now),
            missing_reported_at=None,
            orphan_reported_at=None,
        )
        tickstate.save(path, replace(state, wake=wake))


def _read_tick(ctx: Ctx) -> tickstate.TickState:
    """Read `tick.json` without a lock (the replace makes one whole file read).

    `pmo wake` reads it before touching the sidecar: a damaged file is exit 2
    (design §3.4) and must not leave a new job behind.
    """
    return tickstate.load_or_empty(_tick_file(ctx))


def _recover_created_job(
    port, before: Sequence[JobRecord], ctx: Ctx, wanted: float
) -> JobRecord | None:
    """The job `POST /jobs` just made, found again after an unreadable 2xx.

    The sidecar creates the job before writing the answer, so the retreat is
    one `GET /jobs`: the one own job that the pre-`POST` list did not have and
    whose `every` is the interval asked for is the new job (裁定第 1 節 step 2).
    `None` — the list did not answer, or nothing matched — means the job may
    still exist but cannot be identified, and `set` must not delete or write.
    """
    seen = {job.job_id for job in before}
    try:
        jobs = port.list_jobs()
    except SidecarError:
        return None
    for job in _own_jobs(jobs, ctx):
        if job.job_id in seen:
            continue
        if _same_interval(job, wanted):
            return job
    return None


def set_interval(ctx: Ctx, text: str) -> WakeSet:
    """`pmo wake set ‹every›` (design §3.6 steps 1–4).

    Validate the interval (exit 1 for a bad one), then inside `wake.lock`:
    `GET /jobs`, keep the one own job when it already has this interval and the
    PMO's current id, otherwise create the new job first and delete the other
    own jobs after (先建後刪). `args.id` decides only whether the existing job
    can be reused; matching by sentence is what finds the jobs to delete, so a
    job left under an old PMO id is cleaned up here.

    A `201` whose body cannot be read is not a failed create: the job was made
    before the sidecar wrote the answer, so `_recover_created_job` looks it up
    with `GET /jobs` and the normal flow continues. When it cannot be found,
    nothing is deleted and `wake.every` is left alone; the `JobCreatedUnreadable`
    escapes so the CLI prints the retry hint and exits 2 (裁定第 1 節 step 3).
    """
    every, wanted = parse_every(text)
    _read_tick(ctx)  # a damaged tick.json is exit 2 before any sidecar change
    port = _pmo_port(ctx)
    now = ctx.deps.now()
    with _lock.path_lock(_lock_path(ctx.state_dir), timeout=WAKE_LOCK_TIMEOUT):
        before = port.list_jobs()
        own = _own_jobs(before, ctx)
        kept = _keep(own, wanted, ctx.config.pmo_identity)
        warnings: list[str] = []
        if kept is None:
            try:
                kept = port.create_job(
                    every=every,
                    agent_id=ctx.config.pmo_identity,
                    text=ctx.config.wake_text,
                    interrupt=False,
                )
            except JobCreatedUnreadable as error:
                recovered = _recover_created_job(port, before, ctx, wanted)
                if recovered is None:
                    raise JobCreatedUnreadable(
                        status=error.status, detail="找不到剛建的 job"
                    ) from None
                kept = recovered
            for job in own:
                try:
                    port.delete_job(job.job_id)
                except SidecarError as error:
                    # The new job is already in place, so the PMO is woken; the
                    # leftover only means one extra round (§3.6 step 3).
                    warnings.append(
                        f"刪不掉自己的叫醒 job {job.job_id}：{sidecar_code(error)}"
                    )
        _write_wake(ctx, now=now, every=every)
    return WakeSet(every=every, job=kept, warnings=tuple(warnings))


def turn_off(ctx: Ctx) -> WakeOff:
    """`pmo wake off`: delete every own job, then set `wake.every` to null.

    Jobs whose sentence differs are somebody else's (or an old sentence) and
    are never touched. A delete that fails is reported and makes the exit code
    2 (a sidecar refusal), while `wake.every` still becomes null: tick's orphan
    warning is then the visible「有 job 但沒記頻率」sign the PMO acts on.
    """
    _read_tick(ctx)
    port = _pmo_port(ctx)
    now = ctx.deps.now()
    deleted: list[str] = []
    warnings: list[str] = []
    with _lock.path_lock(_lock_path(ctx.state_dir), timeout=WAKE_LOCK_TIMEOUT):
        for job in _own_jobs(port.list_jobs(), ctx):
            try:
                if port.delete_job(job.job_id):
                    deleted.append(job.job_id)
            except SidecarError as error:
                warnings.append(
                    f"刪不掉自己的叫醒 job {job.job_id}：{sidecar_code(error)}"
                )
        _write_wake(ctx, now=now, every=None)
    return WakeOff(deleted=tuple(deleted), warnings=tuple(warnings))


def show(ctx: Ctx) -> WakeShow:
    """`pmo wake show`: read-only; the own jobs, the wanted interval, the due time.

    Besides the PMO's own jobs it lists — never deletes — the poke jobs aimed
    at the PMO whose sentence is not the current one (an old sentence, or one
    the person scheduled themselves), so the PMO can decide whether to ask
    whoever scheduled it to remove it (§3.6). Only known fields are printed.

    The earliest due moment includes the watchdog (裁定第 2 節): it reads every
    active team's state file (read-only, no lock, no HTTP — the worker teams'
    files are in this state directory too, and tick's watchdog reads them) and
    calls the same `pmo.due.earliest_due` as `[idle] [next-due=…]`, so the two
    answers cannot drift apart.
    """
    state = _read_tick(ctx)
    team_states = [
        teamstate.load_state(ctx.state_dir, team.key)
        for team in teammsg.active_teams(ctx.config)
    ]
    port = _pmo_port(ctx)
    jobs = port.list_jobs()
    own = _own_jobs(jobs, ctx)
    other = tuple(
        job
        for job in jobs
        if job.action == "poke"
        and ops.job_targets_agent(job, ctx.config.pmo_identity)
        and not ops.has_fixed_sentence(job, ctx.config.wake_text)
    )
    moment = earliest_due(state, team_states, ctx.deps.now())
    return WakeShow(
        every=state.wake.every,
        set_at=state.wake.set_at,
        last_tick_at=state.last_tick_at,
        next_due_at=iso_utc(moment) if moment is not None else None,
        jobs=tuple(own),
        other_jobs=other,
    )


# --------------------------------------------------------------------------- #
# tick 的一步：job 健康與孤兒檢查（design §3.6「tick 怎麼看 job 的健康」）
# --------------------------------------------------------------------------- #


def _replace_wake(state: tickstate.TickState, **fields: object) -> tickstate.TickState:
    """One `wake` field change on a (re-read) tick state; `run.writes` shape."""
    return replace(state, wake=replace(state.wake, **fields))


def _missing_reported(moment: str | None) -> tickstate.Mutation:
    def mutate(state: tickstate.TickState) -> tickstate.TickState:
        return _replace_wake(state, missing_reported_at=moment)

    return mutate


def _orphan_reported(moment: str | None) -> tickstate.Mutation:
    def mutate(state: tickstate.TickState) -> tickstate.TickState:
        return _replace_wake(state, orphan_reported_at=moment)

    return mutate


def _error_reported(changes: dict[str, str | None]) -> tickstate.Mutation:
    """Record (or drop, for a `None` value) the last printed code per job id.

    Applied to the re-read state inside `tick.json.lock`, so a code recorded
    there by another writer is changed only where this round has news.
    """

    def mutate(state: tickstate.TickState) -> tickstate.TickState:
        errors = dict(state.wake.error_reported)
        for job_id, code in changes.items():
            if code is None:
                errors.pop(job_id, None)
            else:
                errors[job_id] = code
        return _replace_wake(state, error_reported=errors)

    return mutate


def step_job_health(run: "TickRun") -> None:
    """One `GET /jobs` and the wake job's health (design §3.6; #358 T5-4).

    Whenever the settings name `pmo_sidecar` the round asks once — even when
    `wake.every` is null, because then the question is the other one (a job
    the PMO no longer remembers scheduling). `pmo wake show` is the PMO's own
    read; this step is what makes a sidecar restart visible.

    - `wake.every` not null: no own job → `[wake-missing]` once (the mark is
      only cleared when a job is seen again, so the same disappearance is not
      reprinted every round); an own job's `last_error` → `[wake-error]` only
      when the code differs from the one already reported (a failure whose
      `last_run` keeps moving prints once); an entry is dropped when the code
      goes back to null or the job is gone; more than one own job → `[warn]`;
    - `wake.every` null: only the orphan check — an own job on the sidecar
      that tick.json does not remember → `[warn]` once (`orphan_reported_at`).

    A failed `GET /jobs` is a `[warn]` and the round's other steps still run
    (§2.3's 部分失敗); nothing here prints on stdout except the two wake
    events.
    """
    config = run.ctx.config
    if config.pmo_sidecar is None:
        return
    port = sidecar_port(run.ctx.deps, config.pmo_sidecar)
    if port is None:
        raise SidecarError(detail="sidecar 埠沒有接上")
    jobs = port.list_jobs()
    own = _own_jobs(jobs, run.ctx)
    if run.snapshot.wake.every is None:
        _orphan_check(run, own)
    else:
        _health_check(run, own)


def _health_check(run: "TickRun", own: list[JobRecord]) -> None:
    """`wake.every` is set: is the job there, and is it failing? (design §3.6)"""
    wake = run.snapshot.wake
    changes: dict[str, str | None] = {}
    seen = {job.job_id for job in own}
    for job_id in wake.error_reported:
        if job_id not in seen:
            changes[job_id] = None
    if not own:
        if wake.missing_reported_at is None:
            run.events.append(f"[wake-missing] every={wake.every}")
            run.writes.append(_missing_reported(iso_utc(run.now)))
    else:
        if wake.missing_reported_at is not None:
            # The job is back (or a new one was set): the next disappearance is
            # a new event and prints again.
            run.writes.append(_missing_reported(None))
        if len(own) > 1:
            run.ctx.log(f"[warn] 自己的叫醒 job 有 {len(own)} 個")
        for job in own:
            if job.last_error is None:
                if job.job_id in wake.error_reported:
                    changes[job.job_id] = None
                continue
            if wake.error_reported.get(job.job_id) != job.last_error:
                run.events.append(f"[wake-error] {job.job_id} {job.last_error}")
                changes[job.job_id] = job.last_error
    if changes:
        run.writes.append(_error_reported(changes))


def _orphan_check(run: "TickRun", own: list[JobRecord]) -> None:
    """`wake.every` is null: a job is on the sidecar that tick.json forgot."""
    wake = run.snapshot.wake
    if not own:
        if wake.orphan_reported_at is not None:
            # The job is gone: the next orphan is a new event and prints again.
            run.writes.append(_orphan_reported(None))
        return
    if wake.orphan_reported_at is not None:
        return
    listed = "、".join(f"{job.job_id} {ops.every_text(job)}" for job in own)
    run.ctx.log(f"[warn] tick.json 沒記叫醒頻率，sidecar 上有自己的 job {listed}")
    run.writes.append(_orphan_reported(iso_utc(run.now)))
