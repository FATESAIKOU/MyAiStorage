"""`pmo reply` / `pmo poke` / `pmo status` / `pmo close`: the rest of T-4c.

The design's table (§3.2) gives each row's input, output and exit codes; this
module holds the behavior and `pmo.cli` only registers the commands and maps
exceptions onto the codes (the same split `pmo.dispatch` uses for T-4a).

What the design fixes, row by row:

- `reply ‹team› [--urgency N] --body -` goes through aicomm (write plus wake)
  and answers with the message id; aicomm failing is exit 2, and an aicomm
  *timeout* is the one case where the tool says「送出結果不明，不要直接重送」
  (the message may already be written — `pmo.ports.AicommTimeout`).
- `poke ‹team› [--interrupt] --body -` sends one sentence to the sidecar and
  answers with its response code.
- `status [‹team›] [--json]` reads each team PM's state／exit／session, the jobs
  the PMO itself scheduled, and (for a named team) the jobs aimed at that team
  PM, which it only lists.
- `close ‹team› [--timeout 秒]` follows the environment's Close SOP: delete the
  sidecar's auto-reopen jobs for this team PM **first** (the `auto: true` jobs
  it schedules while the agent waits for quota; left in place they would open
  the agent again as soon as they fire), poke with `interrupt`（「停下來，等
  指示」）, `POST /agents/<id>/close`, and on a 409 `close_in_progress` poll
  `GET /agents/<id>` instead of re-sending close. Any poke job is left alone:
  the ones aimed at the team PM are somebody else's (§3.6「別人排的一律不
  碰」), and the PMO's own wake job aims at the PMO.

裁定 (d)（#357 架構師；§3.2 的「`pmo close` 的 job 處理」）settles what close
deletes: only `auto: true` jobs whose `args.id` is the team PM. The non-auto
jobs aimed at the agent are printed with their id, action, `every` and whether
their sentence is the PMO's fixed one, and nothing else. An agent that is
already `exited` still gets the close (the sidecar's close is idempotent and
drops the handoff); a 404 means it was never open and the team state still
says `closed: true`; a 409 `close_in_progress` is never re-sent — it is polled,
and one that has not finished by the timeout still exits 0 with
`close_requested_at` recorded (the close runs in the background; `pmo status`
is how to check). `reply`／`poke` refuse a retired team (exit 1); `status` and
`close` keep working, since closing the last agent is what a retired team
needs.

待確認（the design does not spell these out; they are kept small and named in
the #357 report）：

- The output wording of `status` and `close` is not in the table; the labels
  `[status]`／`[close]`／`[poke]`／`[reply]` are new and only carry known fields
  (D-12).
- `close` also writes `closed: true` (and the state it saw) into the team state:
  the team-message match (`pmo.teammsg`) treats a dispatch as over only then,
  and a new dispatch writes `closed: false` again (T-4a). The design names the
  field but not the writer.
- A close that is still running in the background writes `close_requested_at`
  instead of `closed: true`: the agent may still be `running`, so claiming it
  closed would be wrong, and the next `pmo status` is what tells the PMO.

The sidecar port is looked up by address (`Deps.sidecar_for`): a team's
operations use that team's `sidecar`, and the PMO's own wake jobs use
`pmo_sidecar` (#356, design §3.1). First phase still refuses `worker` teams
(D-8) instead of guessing how to reach another machine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import teamstate
from .config import Team
from .errors import SchemaError, UsageError
from .ports import (
    AgentRecord,
    JobRecord,
    PortError,
    SidecarError,
    sidecar_address,
    sidecar_code,
)
from .runtime import Ctx, one_line, sidecar_port
from .timeutil import iso_utc

__all__ = [
    "CLOSE_POKE_TEXT",
    "CLOSE_TIMEOUT_SECONDS",
    "CloseResult",
    "StatusReport",
    "TeamStatus",
    "close",
    "every_text",
    "has_fixed_sentence",
    "is_auto_job",
    "is_own_job",
    "poke",
    "reply",
    "status",
    "team_or_usage",
]

#: The sentence the Close SOP's interrupt poke carries (environment Close SOP:
#: poke `{"text": "停下來，等指示", "interrupt": true}`, then close).
CLOSE_POKE_TEXT = "停下來，等指示"

#: `pmo close --timeout`'s default, in seconds (the Close SOP's `--max-time 60`).
CLOSE_TIMEOUT_SECONDS = 60.0

#: How long one poll of `GET /agents/<id>` waits before asking again.
CLOSE_POLL_SECONDS = 0.5

#: The two clocks, module-level so tests do not wait for real seconds.
_sleep = time.sleep
_monotonic = time.monotonic


# --------------------------------------------------------------------------- #
# 共用：team、sidecar、自己的 job
# --------------------------------------------------------------------------- #


def team_or_usage(ctx: Ctx, key: str, *, refuses_retired: bool = False) -> Team:
    """The named team, or a usage error (exit 1: the arguments name nothing).

    `refuses_retired` is for the commands that start work or wake somebody up
    (`reply`／`poke`; `pmo send` refuses a retired team on its own, with the
    same meaning): a retired team is done being dispatched to, so a command that
    would write into its channel or poke its agent is refused (#357 ruling (d),
    §3.2). The read and wind-down commands (`check`, `status`, `close`) do not
    pass it — closing the last agent is exactly what a retired team needs.
    """
    team = ctx.config.team(key)
    if team is None:
        raise UsageError(f"未知的 team：{key}")
    if refuses_retired and team.retired:
        raise UsageError(f"team {team.key} 已退役（retired=true），不回話也不叫醒")
    return team


def _sidecar(ctx: Ctx, address: str):
    """The sidecar port for one address, or a refusal that reads like a failure.

    `Deps.sidecar_for` is `None` when the deployment wires no sidecar at all
    (tests that do not need one), and the same refusal covers a `worker`
    address, which is never reached (D-8). The refusal only names the wiring,
    not the address (D-12 keeps the output to known fields; the address is a
    setting, but there is nothing to gain from printing it twice).
    """
    port = sidecar_port(ctx.deps, address)
    if port is None:
        raise SidecarError(detail="sidecar 埠沒有接上（Deps.sidecar_for）")
    return port


def _local_team(team: Team) -> None:
    """Refuse a `worker` team (D-8, first phase).

    The team's own `sidecar` address is used, not the PMO's `pmo_sidecar`
    (#356): a local team on another machine reachable over HTTP is operated on
    that machine. Only `worker` is refused — first phase does not carry the
    worker transport (ssh, forwarded ports).
    """
    if team.sidecar == "worker":
        raise SidecarError(
            detail=(
                f"team {team.key} 是 worker team：它的 sidecar 在別的機器上，"
                f"第一階段不搬（design D-8；見 §3.2.1 的 [worker-action]）"
            )
        )


def _args_of(job: JobRecord) -> dict:
    return job.args if isinstance(job.args, dict) else {}


def has_fixed_sentence(job: JobRecord, wake_text: str) -> bool:
    """Whether the job's sentence is the PMO's configured wake sentence."""
    return _args_of(job).get("text") == wake_text


def is_auto_job(job: JobRecord) -> bool:
    """True for the sidecar's auto-reopen job (`auto: true`).

    The sidecar schedules these while an agent is `waiting-quota`, and they
    open the agent again when they fire — the one kind `pmo close` deletes
    (#357 ruling (d)). The sidecar sends `auto` as a job field, a fake or an
    older sidecar may put it in `args`, so both shapes are read.
    """
    return bool(job.extra.get("auto", _args_of(job).get("auto", False)))


def is_own_job(job: JobRecord, wake_text: str) -> bool:
    """§3.6's definition of the PMO's own poke job.

    `action` is `poke`, `args.text` equals the currently configured wake
    sentence, and `auto` is not true. `args.id` is deliberately not used: a job
    left under an older PMO id is still the PMO's own.
    """
    if job.action != "poke" or not has_fixed_sentence(job, wake_text):
        return False
    return not is_auto_job(job)


def job_targets_agent(job: JobRecord, agent_id: str) -> bool:
    """Whether the job pokes `agent_id` (`args.id`)."""
    return _args_of(job).get("id") == agent_id


def every_text(job: JobRecord) -> str:
    """`every=600s`, or `at=‹時刻›` for a one-off job (`every` is `None`, #370 高-2).

    One spelling for every place a job is printed (`pmo status`, `pmo close`,
    `pmo wake show`, tick's orphan warning), so a job reads the same way
    wherever the PMO meets it.
    """
    if job.every is None:
        return f"at={job.at or '-'}"
    return f"every={job.every:g}s"


def jobs_aimed_at(jobs: list[JobRecord] | tuple[JobRecord, ...], agent_id: str) -> tuple[JobRecord, ...]:
    """The non-auto jobs aimed at `agent_id`: listed, never deleted (ruling (d))."""
    return tuple(
        job for job in jobs if not is_auto_job(job) and job_targets_agent(job, agent_id)
    )


def auto_jobs_aimed_at(
    jobs: list[JobRecord] | tuple[JobRecord, ...], agent_id: str
) -> tuple[JobRecord, ...]:
    """The sidecar's auto-reopen jobs for `agent_id` (what close deletes)."""
    return tuple(job for job in jobs if is_auto_job(job) and job_targets_agent(job, agent_id))


def _code_of(error: SidecarError) -> str:
    """One known word for a sidecar failure (D-12: no message text)."""
    return sidecar_code(error)


# --------------------------------------------------------------------------- #
# `pmo reply`
# --------------------------------------------------------------------------- #


def reply(ctx: Ctx, *, team: Team, body: str, urgency: int | None = None) -> str:
    """Send the PMO's words to a team PM through aicomm; returns the message id.

    The identity gate is the caller's (`assert_pmo_identity`, before anything
    leaves): this function only speaks to the aicomm port. `AicommTimeout`
    travels up untouched — the caller has to say「送出結果不明」rather than
    「送不出去」.
    """
    port = ctx.deps.aicomm
    if port is None:
        raise PortError("aicomm 埠沒有接上")
    return port.send(
        channel=team.channel, to=team.pm, body=body, urgency=urgency
    )


# --------------------------------------------------------------------------- #
# `pmo poke`
# --------------------------------------------------------------------------- #


def poke(ctx: Ctx, *, team: Team, text: str, interrupt: bool = False) -> int:
    """One poke at the team PM's agent; returns the sidecar's response code.

    `pmo.ports.SidecarPort.poke` answers with the HTTP status the sidecar
    returned (a 204 stays 204); the port's contract is an `int`, so it is
    returned as is — no silent 200 for a port that answers with nothing.
    """
    _local_team(team)
    return _sidecar(ctx, team.sidecar).poke(team.pm, text=text, interrupt=interrupt)


# --------------------------------------------------------------------------- #
# `pmo status`
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TeamStatus:
    """One team PM's line of `pmo status`.

    `attach` is the sidecar's `attach.local` verbatim when the team PM is an
    interactive one (`None` for `-p` and opencode seats); it is printed as it
    came, never parsed (#356 裁定 §3).
    """

    key: str
    pm: str
    state: str
    exit_reason: str | None = None
    session: str | None = None
    attach: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {"key": self.key, "pm": self.pm, "state": self.state}
        known["exit"] = self.exit_reason
        known["session"] = self.session
        known["attach"] = self.attach
        if self.note is not None:
            known["note"] = self.note
        return known


@dataclass(frozen=True)
class StatusReport:
    """What `pmo status` found: one line per team, the PMO's own jobs, failures.

    `team_jobs` is filled only for `status ‹team›`: the jobs aimed at that team
    PM that are not the PMO's own, which the command only lists (裁定 (d) keeps
    `close` from deleting any poke job, and `status` is read-only anyway).
    """

    teams: tuple[TeamStatus, ...]
    jobs: tuple[JobRecord, ...] = ()
    team_jobs: tuple[JobRecord, ...] = ()
    jobs_note: str | None = None
    failures: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {
            "teams": [team.to_dict() for team in self.teams],
            "jobs": [job.to_dict() for job in self.jobs],
        }
        if self.team_jobs:
            known["team_jobs"] = [job.to_dict() for job in self.team_jobs]
        if self.jobs_note is not None:
            known["jobs_note"] = self.jobs_note
        return known


def status(ctx: Ctx, key: str | None = None) -> StatusReport:
    """Read every team PM's state／exit／session and the PMO's own jobs.

    Every team's seat is read on **that team's** sidecar (its `sidecar`
    address); the PMO's own wake jobs are read on `pmo_sidecar`, and a named
    team's aimed jobs on that team's sidecar — the two addresses may differ,
    and each answer is cached for the call so a shared sidecar is asked once
    (#356, design §3.1). Addresses are compared in one form (#371): a trailing
    `/` names the same machine, so it is neither asked twice nor reported as
    two failures. A team whose PM is not on the sidecar is
    `state=absent` (that is the fact, not a failure: the agent may simply not
    be open). A sidecar that refuses or is unreachable is a failure
    (`failures`, exit 2), and the other teams are still read — §2.3's
    「部分失敗」shape. Worker teams are not queried at all (D-8): the tick's
    `[worker-action]` hand-off covers them.
    """
    teams = ctx.config.teams if key is None else (team_or_usage(ctx, key),)
    lines: list[TeamStatus] = []
    failures: list[str] = []

    for team in teams:
        if team.sidecar == "worker":
            lines.append(
                TeamStatus(
                    key=team.key,
                    pm=team.pm,
                    state="worker",
                    note="worker team：sidecar 在別的機器上，不由本機查（D-8）",
                )
            )
            continue
        port = sidecar_port(ctx.deps, team.sidecar)
        if port is None:
            failures.append(f"{team.key}：sidecar 埠沒有接上")
            lines.append(
                TeamStatus(
                    key=team.key, pm=team.pm, state="unknown", note="sidecar 埠沒有接上"
                )
            )
            continue
        try:
            record = port.get_agent(team.pm)
        except SidecarError as error:
            if error.status == 404:
                lines.append(TeamStatus(key=team.key, pm=team.pm, state="absent"))
                continue
            failures.append(f"{team.key}：{_code_of(error)}")
            lines.append(
                TeamStatus(key=team.key, pm=team.pm, state="unknown", note=_code_of(error))
            )
            continue
        lines.append(_status_of(team, record))

    # 每個位址只問一次：`pmo_sidecar` 和某個 team 的 sidecar 可能是同一台。
    # 位址先正規化（`sidecar_address`，一處集中）：結尾多一個 `/` 是同一台，
    # 不能因此問兩次、也不能把同一次失敗報成兩條（#371 低項）。
    job_cache: dict[str, tuple[JobRecord, ...] | str] = {}

    def jobs_at(address: str) -> tuple[JobRecord, ...] | str:
        address = sidecar_address(address)
        if address not in job_cache:
            port = sidecar_port(ctx.deps, address)
            if port is None:
                job_cache[address] = "sidecar 埠沒有接上"
            else:
                try:
                    job_cache[address] = tuple(port.list_jobs())
                except SidecarError as error:
                    job_cache[address] = _code_of(error)
        return job_cache[address]

    jobs: tuple[JobRecord, ...] = ()
    team_jobs: tuple[JobRecord, ...] = ()
    jobs_note: str | None = None
    own_failed = False
    if ctx.config.pmo_sidecar is None:
        jobs_note = "PMO 不在 sidecar（設定沒有 pmo_sidecar），不查自己排的 job"
    else:
        own = jobs_at(ctx.config.pmo_sidecar)
        if isinstance(own, str):
            own_failed = True
            failures.append(f"jobs：{own}")
            jobs_note = f"自己排的 job 讀不到：{own}"
        else:
            jobs = tuple(job for job in own if is_own_job(job, ctx.config.wake_text))

    if key is not None and teams[0].sidecar != "worker":
        team = teams[0]
        aimed = jobs_at(team.sidecar)
        if isinstance(aimed, str):
            # 同一個位址剛剛已經報過一次（自己的 job），不要重複。比對用正規化
            # 後的位址：`…/` 與沒有斜線是同一台（#371 低項）。
            same_as_pmo = ctx.config.pmo_sidecar is not None and (
                sidecar_address(team.sidecar) == sidecar_address(ctx.config.pmo_sidecar)
            )
            if not (own_failed and same_as_pmo):
                failures.append(f"{team.key} 的 job：{aimed}")
        else:
            # 只列不動：瞄準這個 team PM 的 job（裁定 (d) 末段、#357 的建議）
            team_jobs = jobs_aimed_at(aimed, team.pm)

    return StatusReport(
        teams=tuple(lines),
        jobs=jobs,
        team_jobs=team_jobs,
        jobs_note=jobs_note,
        failures=tuple(failures),
    )


def _status_of(team: Team, record: AgentRecord) -> TeamStatus:
    return TeamStatus(
        key=team.key,
        pm=team.pm,
        state=record.state,
        exit_reason=record.exit_reason(),
        session=record.session,
        attach=record.attach,
    )


# --------------------------------------------------------------------------- #
# `pmo close`
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CloseResult:
    """What `pmo close` did: whether the agent is gone, and what it did with jobs.

    `deleted_jobs` are the auto-reopen jobs actually deleted; `missing_jobs` are
    the ones `DELETE /jobs/<id>` said were already gone (a 404, not counted as
    deleted — #357 review L5); `other_jobs` are the non-auto jobs aimed at the
    agent, which the ruling only lists. `job_error` is a failed delete (exit 2);
    `jobs_warn` is a failed `GET /jobs`, which does not change the exit code
    (裁定 (d): 「`GET /jobs` 失敗印 `[warn]`，照樣 close」).
    """

    key: str
    pm: str
    already_gone: bool
    closed: bool
    state: str | None = None
    deleted_jobs: tuple[str, ...] = ()
    missing_jobs: tuple[str, ...] = ()
    other_jobs: tuple[JobRecord, ...] = ()
    job_error: str | None = None
    jobs_warn: str | None = None
    close_in_progress: bool = False


def close(ctx: Ctx, *, team: Team, timeout: float = CLOSE_TIMEOUT_SECONDS) -> CloseResult:
    """Close one team's agent the way the Close SOP does (裁定 (d), §3.2).

    1. `GET /agents/<id>`; not there means it was never open.
    2. `GET /jobs` and delete the sidecar's auto-reopen jobs for this team PM
       **before** closing (otherwise they open the agent again when they fire).
       A `GET /jobs` failure is a `[warn]` and the close goes on; a failed
       delete is also a `[warn]`, but it is a sidecar refusal (exit 2). No poke
       job is ever deleted; the non-auto ones aimed at the agent are returned
       for the caller to list.
    3. A live agent gets the SOP's interrupt poke (「停下來，等指示」). A poke
       that is refused is only a `[warn]`: the close below is what matters.
    4. `POST /agents/<id>/close`, always (even for an `exited` agent: the
       sidecar's close is idempotent and drops the handoff). A 404 means it was
       never open. A 409 `close_in_progress` is never re-sent; the agent is
       polled instead (Close SOP: 「close 回 409 close_in_progress 時不要重送
       close，用 GET /agents/<id> 輪詢到 exited」). A 409 with any other code is
       an ordinary refusal (exit 2), not something to poll.
    5. The team state gets `closed: true` and the state that was seen; a close
       still running in the background gets `close_requested_at` instead (待確認:
       the design names neither field's writer).
    """
    _local_team(team)
    port = _sidecar(ctx, team.sidecar)

    record: AgentRecord | None
    try:
        record = port.get_agent(team.pm)
    except SidecarError as error:
        if error.status != 404:
            raise
        record = None

    already_gone = record is None
    deleted, missing, other, job_error, jobs_warn = _clear_auto_jobs(port, team)

    if record is not None and record.state != "exited":
        try:
            port.poke(team.pm, text=CLOSE_POKE_TEXT, interrupt=True)
        except SidecarError as error:
            ctx.log(f"[warn] 戳「{team.pm}」沒成功（{_code_of(error)}），照樣送 close")

    state: str | None = "exited" if already_gone else None
    close_in_progress = False
    if not already_gone:
        try:
            port.close_agent(team.pm)
        except SidecarError as error:
            if error.status == 404:
                state = "exited"  # 根本沒開：和關掉一樣
            elif error.status == 409 and error.code == "close_in_progress":
                # Only this code is a close already under way; never re-send it.
                close_in_progress = True
            else:
                raise
        if state != "exited":
            state = _await_exit(port, team.pm, timeout)
            if state == "exited":
                close_in_progress = False

    closed = state == "exited"
    if closed:
        _record_close(ctx, team, state=state)
    elif close_in_progress:
        _record_close(ctx, team, state=state, close_requested_at=True)

    return CloseResult(
        key=team.key,
        pm=team.pm,
        already_gone=already_gone,
        closed=closed,
        state=state,
        deleted_jobs=deleted,
        missing_jobs=missing,
        other_jobs=other,
        job_error=job_error,
        jobs_warn=jobs_warn,
        close_in_progress=close_in_progress,
    )


def _await_exit(port, agent_id: str, timeout: float) -> str | None:
    """Poll `GET /agents/<id>` until it is `exited` or gone; `None` on timeout.

    The agent leaving the sidecar (404) counts as closed: after a close the
    sidecar forgets the seat, and the SOP's own polling treats「不在了」as done.
    """
    deadline = _monotonic() + max(0.0, float(timeout))
    while True:
        try:
            record = port.get_agent(agent_id)
        except SidecarError as error:
            if error.status == 404:
                return "exited"
            raise
        if record.state == "exited":
            return "exited"
        if _monotonic() >= deadline:
            return record.state
        _sleep(CLOSE_POLL_SECONDS)


def _clear_auto_jobs(
    port, team: Team
) -> tuple[
    tuple[str, ...], tuple[str, ...], tuple[JobRecord, ...], str | None, str | None
]:
    """Delete the sidecar's auto-reopen jobs for the team PM; list the rest.

    Returns `(deleted, missing, other_jobs, delete_error, list_warn)`: a job the
    delete answered 404 for is `missing`, not deleted (#357 review L5), and the
    failure of `GET /jobs` itself is only a warning (裁定 (d)).

    A delete that fails is recorded and the remaining auto jobs are still
    tried: leaving them in place means they open the agent again when they
    fire, so giving up after the first failure would be the wrong half of the
    ruling (「照樣繼續 close」). The first failure's code is what is reported.
    """
    try:
        jobs = list(port.list_jobs())
    except SidecarError as error:
        return (), (), (), None, _code_of(error)

    other = jobs_aimed_at(jobs, team.pm)
    deleted: list[str] = []
    missing: list[str] = []
    first_error: str | None = None
    for job in auto_jobs_aimed_at(jobs, team.pm):
        try:
            existed = port.delete_job(job.job_id)
        except SidecarError as error:
            if first_error is None:
                first_error = _code_of(error)
            continue
        if existed:
            deleted.append(job.job_id)
        else:
            missing.append(job.job_id)
    return tuple(deleted), tuple(missing), other, first_error, None


def _record_close(
    ctx: Ctx, team: Team, *, state: str | None, close_requested_at: bool = False
) -> None:
    """Write the close into the team state (`closed` is what it reads).

    A close still running in the background writes `close_requested_at` and not
    `closed: true`: the agent may still be running, and claiming it closed
    would be wrong. A failure here is a `[warn]`, not a different exit code:
    the agent is already closed (or the close is on its way), and telling the
    caller「沒關」would be wrong. A new dispatch writes `closed: false` again
    (T-4a), so the flag is per dispatch.

    The read-modify-write runs inside `teamstate.state_lock` (#370 中-2): the
    same file's `ack_at` is written under the same lock by
    `teammsg.process_team_messages`, and two writers outside one lock would
    overwrite each other (the pattern #369 settled on for `send`／`inbox read`).
    """
    try:
        with teamstate.state_lock(ctx.state_dir, team.key):
            st = teamstate.load_state(ctx.state_dir, team.key)
            if close_requested_at:
                st["close_requested_at"] = iso_utc(ctx.deps.now())
            else:
                st["closed"] = True
                if state is not None:
                    st["state"] = state
            teamstate.save_state(ctx.state_dir, team.key, st)
    except (SchemaError, OSError) as error:
        ctx.log(f"[warn] team 狀態寫不進去（已關，但 closed 沒記）：{one_line(error)}")
