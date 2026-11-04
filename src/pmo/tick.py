"""`pmo tick`: one round of the PMO's watch (design §2.2, §3.2.1; #358 T5-2).

A round is **not** a loop. Every step looks at the state files and at "now"
and handles only what is already due, so being woken twice is harmless: the
cursor only moves once (`read` advances it), and a watchdog poke is claimed in
the team state before it is sent (ruling 9: 先佔再 poke), so two ticks running
at the same time still poke once.

The skeleton the architect accepted (#358 T5-2 裁定 §1):

- `TickRun` is the one object a step writes to: events go into `run.events`
  (the runner emits them in order after the step, so a step never prints),
  and changes to `tick.json` go into `run.writes` as `TickState → TickState`
  functions. `commit` applies them to a **re-read** `tick.json` inside
  `tick.json.lock`, and no HTTP call happens there (design §3.4): tick
  finishes `GET /agents`／`poke` first, so a `pmo wake set` that ran meanwhile
  is not overwritten by the snapshot this round started with;
- `Step(name, run, fatal)` — only `commit` is fatal, so its failures reach the
  CLI instead of turning into an event line;
- T5-3 adds two `Step(...)` lines (handoffs and the sweep) and T5-4 one (job
  health); the runner, the slots and `commit` are not changed by them.

What a failed step does is decided here (裁定 S-2): only `SchemaError`,
`PortError` (the sidecar's included) and `OSError` are caught. A `PortError`
is the design's「部分失敗」and prints `[warn]` on stderr — including the
`angareion read` failure, whose round still goes on to compare the agents
(§2.3). A `SchemaError`／`OSError` is a damaged state file, the one case that
prints `[要本人處理]`. Anything else is a bug and is raised, not hidden as a
status problem.

The identity gate sits at the very front of `tick` (裁定 S-1): the design's
exit-2「什麼都沒做」must happen before `tick.json`, the ledger or any team
state is read, while a step's failure is not a reason to exit 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Callable, Iterable

from . import handoff, inbox, round as round_module, teammsg, teamstate, tickstate
from ._lock import LockTimeout
from .config import Team
from .errors import SchemaError
from .ports import (
    AgentRecord,
    PortError,
    SidecarError,
    seconds_of,
    sidecar_code,
)
from .runtime import Ctx, assert_pmo_identity, one_line, sidecar_port
from .timeutil import iso_utc, parse_ts

__all__ = ["Step", "TickRun", "tick", "tick_steps"]

#: The watchdog's interval (design §3.6: 看門狗 10 分鐘): the first poke 10
#: minutes after the dispatch, the escalation 10 minutes after the poke.
WATCHDOG_SECONDS = 10 * 60.0

#: The worker-team hand-off's interval (design §3.2.1: `poll` 預設間隔 10 分鐘).
WORKER_POLL_SECONDS = 10 * 60.0

#: How long `commit` waits for `tick.json.lock` (design §3.4: 最多 10 秒).
LOCK_TIMEOUT_SECONDS = 10.0

#: How long tick waits for one team's state lock (design §3.4, architect
#: 2026-11-04 T5-3: 「tick 拿 team 鎖最多等 10 秒，逾時 `[warn]` 跳過該 team」).
#: The lock is only taken around a local read-modify-write, never around HTTP.
TEAM_LOCK_TIMEOUT_SECONDS = 10.0

#: 「現在 − `last_tick_at`」超過想要間隔的幾倍就警告 (design §3.4).
STALE_FACTOR = 3

#: The watchdog's poke sentence, verbatim (`WATCHDOG_POKE_TEXT`, ruling §3):
#: one line, so an interactive (tui) team PM can be given it too. `interrupt`
#: is always false — a poke must not cut the round the team PM is in.
WATCHDOG_POKE_TEXT = (
    "你還沒回「收到 {ticket}」。指示在 Angareion：用 "
    "`angareion read --channel {channel} --since {since} --json` 讀；"
    "讀到先回一則「收到 {ticket}」。"
)

#: The two step names that read as sentences in a failure line.
READ_STEP = "收信"


def _should_be_running(st: dict) -> bool:
    """The design's「應該在跑」: not closed, and a dispatch is recorded.

    `ack_at` may or may not be set (ruling 10: a team PM that ends after its
    「收到」 and before the work is done still needs a person).
    """
    return st.get("closed") is not True and bool(str(st.get("ticket") or "").strip())


def _reopen_at(record: AgentRecord) -> str | None:
    """The sidecar's `reopen_at` off the record (`extra`: it is not a typed field).

    `[waiting-quota]` prints `reopen_at=null` when there is none: that is the
    sidecar saying it will not reopen the seat by itself (ruling 10 (c)).
    """
    value = record.extra.get("reopen_at")
    return value if isinstance(value, str) else None


@dataclass
class TickRun:
    """Everything one round shares; steps write here, the runner prints."""

    ctx: Ctx
    now: datetime
    snapshot: tickstate.TickState
    events: list[str] = field(default_factory=list)
    writes: list[tickstate.Mutation] = field(default_factory=list)
    due: list[datetime] = field(default_factory=list)
    handoffs_seen: list[str] = field(default_factory=list)
    #: Teams whose state lock could not be got this round: one `[warn]` each,
    #: and the later steps skip them (#358 T5-3).
    lock_skipped: set[str] = field(default_factory=set)
    after: tickstate.TickState | None = None
    #: Per round cache of `GET /agents/<pm>`: one answer per (address, pm).
    agent_cache: dict[tuple[str, str], "AgentRecord | SidecarError"] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class Step:
    """One step of a round: its name (for failure lines) and what it does.

    `warn` is the step's own spelling for a `PortError` line (the read keeps
    `pmo inbox read`'s wording); a step without one gets `‹name›：‹error›`.
    """

    name: str
    run: Callable[[TickRun], None]
    fatal: bool = False
    warn: Callable[[PortError], str] | None = None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def tick(ctx: Ctx) -> None:
    """Run one round; the CLI maps the failures it does not catch to codes.

    The identity gate comes before anything is read (裁定 S-1): a wrong seat
    exits 2 without touching `tick.json`, the ledger or any team state.
    """
    assert_pmo_identity(ctx.config)
    run = _new_run(ctx)
    _precheck(run)
    _warn_if_stale(run)
    for step in tick_steps:
        _run_step(run, step)
    _emit_idle(run)


def _new_run(ctx: Ctx) -> TickRun:
    """One round's shared state; the clock is read exactly once."""
    return TickRun(
        ctx=ctx,
        now=ctx.deps.now(),
        snapshot=tickstate.load_or_empty(tickstate.tick_path(ctx.state_dir)),
    )


def _precheck(run: TickRun) -> None:
    """Stop before any step: the seat, then every state file (design §2.3).

    `tick` already checked the seat before reading `tick.json` (裁定 S-1); the
    gate is repeated here so a future caller of `_precheck` keeps it.
    `round.precheck` reads the ledger and every active team's state, so a
    damaged file stops the round while the cursor has not moved.
    """
    assert_pmo_identity(run.ctx.config)
    round_module.precheck(run.ctx)


def _warn_if_stale(run: TickRun) -> None:
    """`[warn] 上次 tick 在 ‹時刻›` when the wake interval looks broken (§3.4).

    Only when the PMO asked for a wake interval (`wake.every` is not null) and
    a previous round recorded `last_tick_at`: `every` null means the PMO chose
    to turn the wake off, and staying quiet is the design's choice. A
    `last_tick_at` that cannot be read is not a reason to warn (the schema
    only types it as a string).
    """
    every = run.snapshot.wake.every
    last = parse_ts(run.snapshot.last_tick_at)
    if every is None or last is None:
        return
    try:
        interval = seconds_of(every)
    except SidecarError:
        return
    if run.now - last > timedelta(seconds=interval * STALE_FACTOR):
        run.ctx.log(f"[warn] 上次 tick 在 {run.snapshot.last_tick_at}")


def _run_step(run: TickRun, step: Step) -> None:
    """Run one step and emit the events it added, in order (裁定 S-2).

    Only external failures are caught: `PortError` is the「部分失敗」shape and
    prints `[warn]` on stderr (the read's own line keeps `cmd_inbox_read`'s
    wording), while a damaged state file prints `[要本人處理]`. `fatal` steps
    (`commit`) raise instead, so the CLI can answer with exit 2.
    """
    before = len(run.events)
    try:
        step.run(run)
    except (SchemaError, PortError, OSError) as error:
        if step.fatal:
            raise
        if isinstance(error, PortError):
            detail = (
                step.warn(error)
                if step.warn is not None
                else f"{step.name}：{one_line(error)}"
            )
            run.ctx.log(f"[warn] {detail}")
        else:
            run.events.append(
                f"[要本人處理] {step.name}出錯（state／磁碟）：{one_line(error)}"
            )
    finally:
        for event in run.events[before:]:
            run.ctx.emit(event)


def _emit_idle(run: TickRun) -> None:
    """`[idle]` when nothing went to stdout, with the next due moment (⑤).

    Only stdout events count: a round whose only output was `[warn]` on stderr
    is still idle (§2.2, ruling 6). The moment is the earliest of the
    watchdogs' deadlines (`run.due`), the hand-offs and the worker polls
    (`run.after`, the committed state) — T5-3's new fields are picked up
    without changing this function.
    """
    if run.events:
        return
    due = _earliest_due(run)
    if due is None:
        run.ctx.emit("[idle]")
    else:
        run.ctx.emit(f"[idle] [next-due={iso_utc(due)}]")


def _earliest_due(run: TickRun) -> datetime | None:
    """The earliest moment something in this round becomes due, if any."""
    moments = list(run.due)
    state = run.after if run.after is not None else run.snapshot
    for entry in state.handoffs.values():
        moment = parse_ts(entry.next_due_at)
        if moment is not None:
            moments.append(moment)
    for poll in state.worker_poll.values():
        moment = parse_ts(poll.next_due_at)
        if moment is not None:
            moments.append(moment)
    return min(moments) if moments else None


# --------------------------------------------------------------------------- #
# ① 收信（round.read_round；群請求 id 順手放進 run.handoffs_seen）
# --------------------------------------------------------------------------- #


def step_read_round(run: TickRun) -> None:
    """One inbox round: the PMO's only `angareion read` (design §2.2).

    `run.handoffs_seen` is passed in so T5-3 records the group requests without
    parsing the event text (ruling 7); T5-2 only fills it. A `PortError` here
    is the back end failing before/at the read: `_run_step` prints `[warn]`
    and the round's agent comparison still runs (§2.3).
    """
    events = round_module.read_round(
        run.ctx,
        handoffs=run.handoffs_seen,
        team_lock_timeout=TEAM_LOCK_TIMEOUT_SECONDS,
        lock_skipped=run.lock_skipped,
    )
    run.events.extend(events)


def _read_warn(error: PortError) -> str:
    """The read step's `[warn]` detail, keeping `pmo inbox read`'s wording."""
    return f"angareion read：{inbox.one_line_for_event(error)}"


# --------------------------------------------------------------------------- #
# ② 比對 agents（GET /agents/<pm>；變化才印、印完寫回 team 狀態）
# --------------------------------------------------------------------------- #


def _each_local_team(run: TickRun) -> Iterable[Team]:
    """The teams whose sidecar this machine can reach (D-8: worker is not one)."""
    return [
        team
        for team in teammsg.active_teams(run.ctx.config)
        if team.sidecar != "worker"
    ]


def _agent_of(run: TickRun, team: Team) -> AgentRecord | SidecarError:
    """`GET /agents/<pm>` once per (address, pm) per round; a failure is a value."""
    key = (team.sidecar, team.pm)
    if key not in run.agent_cache:
        port = sidecar_port(run.ctx.deps, team.sidecar)
        if port is None:
            run.agent_cache[key] = SidecarError(detail="sidecar 埠沒有接上")
        else:
            try:
                run.agent_cache[key] = port.get_agent(team.pm)
            except SidecarError as error:
                run.agent_cache[key] = error
    return run.agent_cache[key]


def _state_line(team: Team, old: object, new: str) -> str:
    """`[state] ‹team› ‹old›→‹new›`: one team PM's sidecar state changed."""
    return f"[state] {team.key} {old or '-'}→{new}"


def _state_events(team: Team, record: AgentRecord, st: dict) -> list[str]:
    """The ② classification of one changed answer (ruling 1 and 10).

    The printed events are driven by the **state** changing (the design's
    「狀態變化」): the other fields the sidecar answers (`exit`／`session`／
    `reopen_at`) are still written back by `_record_agent`, but on their own
    they do not make a line — that is what keeps the next round quiet.

    - `exited` + `exit.reason` containing `no candidate` → `[no-candidate]`;
    - `exited` after the PMO's own close (`close_requested_at`) → `[state]`
      only (the PMO already knows), any other exit of a team that should be
      running → `[要本人處理]`;
    - `waiting-quota` → `[waiting-quota]` once, plus the person line when the
      sidecar has no `reopen_at` (no automatic reopen);
    - everything else → `[state]`.
    """
    old = st.get("state")
    new = record.state
    if old == new:
        return []
    if new == "exited":
        reason = record.exit_reason() or ""
        if "no candidate" in reason.lower():
            provider = record.provider or team.provider or "-"
            return [f"[no-candidate] {team.key} {provider}"]
        if st.get("close_requested_at"):
            return [_state_line(team, old, new)]
        if _should_be_running(st):
            return [
                f"[要本人處理] {team.pm} 結束（exit={reason or '-'}）；要不要重開由本人決定"
            ]
    if new == "waiting-quota":
        events = [f"[waiting-quota] {team.key}"]
        if _reopen_at(record) is None:
            events.append(
                f"[要本人處理] {team.pm} 全部冷卻而且沒有候選（reopen_at=null）；"
                f"要不要重開由本人決定"
            )
        return events
    return [_state_line(team, old, new)]


def _absent_events(team: Team, st: dict) -> list[str]:
    """The 404 branch: `NEED_REOPEN` while the team should be running (ruling 10).

    The recorded `state` must already be something else:「同一個 team 恢復之前
    不重印」. `state="absent"` is tick's own mark for this 404 — it is not a
    sidecar state, and the next successful lookup overwrites it. An interactive
    team gets `tui=true` (the current tool's line; it takes no `--session`), a
    `-p` team the session to resume (`none` when the state never recorded one).
    """
    old = st.get("state")
    if old == "absent":
        return []
    if _should_be_running(st):
        if team.tui:
            return [f"NEED_REOPEN {team.key} tui=true"]
        session = str(st.get("session") or "none")
        return [f"NEED_REOPEN {team.key} session={session}"]
    if old is None:
        # A team that was never seen and is not supposed to be running has
        # nothing to report; recording `absent` is enough.
        return []
    return [_state_line(team, old, "absent")]


def _write_team_state(run: TickRun, team: Team, fields: dict) -> None:
    """Write the given fields into the team state, inside its lock (ruling 1).

    The file is re-read in the lock, so a `pmo send`／`pmo open`／the「收到」
    comparison that ran meanwhile is not overwritten; nothing is written when
    every field already holds the value (the round would otherwise rewrite the
    file each time for no reason).
    """
    with teamstate.state_lock(
        run.ctx.state_dir, team.key, timeout=TEAM_LOCK_TIMEOUT_SECONDS
    ):
        st = teamstate.load_state(run.ctx.state_dir, team.key)
        if all(st.get(key) == value for key, value in fields.items()):
            return
        st.update(fields)
        st["updated_at"] = iso_utc(run.now)
        teamstate.save_state(run.ctx.state_dir, team.key, st)


def _record_agent(run: TickRun, team: Team, record: AgentRecord) -> None:
    """Write the answer back (`state`／`exit`／`session`／`reopen_at`, ruling 1).

    `session` and `reopen_at` are only written when the sidecar named one; a
    `null` answer does not erase what the state already recorded.
    """
    fields: dict = {"state": record.state, "exit": record.exit}
    if record.session is not None:
        fields["session"] = record.session
    reopen_at = _reopen_at(record)
    if reopen_at is not None:
        fields["reopen_at"] = reopen_at
    _write_team_state(run, team, fields)


def _note_lock_timeout(run: TickRun, team: Team) -> None:
    """One team's single `[warn]` for a state lock it could not get this round.

    The team goes into `run.lock_skipped`, so the later steps do not wait on the
    same lock again and do not print the same line: 「這一輪跳過它」(architect
    2026-11-04 T5-3).
    """
    if team.key in run.lock_skipped:
        return
    run.lock_skipped.add(team.key)
    run.ctx.log(teamstate.lock_timeout_warning(team.key, TEAM_LOCK_TIMEOUT_SECONDS))


def step_agents(run: TickRun) -> None:
    """Compare every local team PM's seat with the team state (②; §2.3).

    A sidecar failure is a `[warn]` and the other teams are still compared
    (§2.3's「部分失敗」); a 404 is the absent branch. The answer is only
    printed when it differs from the state, then written back, so the next
    round is quiet (ruling 1). A team whose state lock cannot be got within
    `TEAM_LOCK_TIMEOUT_SECONDS` gets a `[warn]` and is skipped this round
    (architect 2026-11-04 T5-3); the next team still runs.
    """
    for team in _each_local_team(run):
        if team.key in run.lock_skipped:
            continue
        try:
            events = _compare_agent(run, team)
        except LockTimeout:
            _note_lock_timeout(run, team)
            continue
        run.events.extend(events)


def _compare_agent(run: TickRun, team: Team) -> list[str]:
    """One team's ② comparison; writes are done before the events are returned.

    Returning the lines only after the write means a team skipped by a lock
    timeout does not print a `[state]` line it could not record (the next round
    would print it again).
    """
    # `_precheck` already read every team state; damage appearing now is
    # the `[要本人處理]` shape `_run_step` gives a `SchemaError`.
    st = teamstate.load_state(run.ctx.state_dir, team.key)
    answer = _agent_of(run, team)
    if isinstance(answer, SidecarError):
        if answer.status == 404:
            events = _absent_events(team, st)
            if events:
                # Recording `absent` is what stops the line from repeating
                # (ruling 1); a team that was never seen and is not supposed
                # to be running gets no event and no file.
                _write_team_state(run, team, {"state": "absent"})
            return events
        run.ctx.log(f"[warn] {team.key}：{sidecar_code(answer)}")
        return []
    events = _state_events(team, answer, st)
    _record_agent(run, team, answer)
    return events


# --------------------------------------------------------------------------- #
# ③ 看門狗（先佔再 poke）與 worker team 的 poll
# --------------------------------------------------------------------------- #


def _claim(run: TickRun, team: Team, field_name: str) -> bool:
    """Claim one watchdog mark before acting (ruling 9: 先佔再 poke).

    The read-modify-write is inside the team's lock, and the field is written
    only while it is still `null`: two ticks that both see a due watchdog
    cannot both win, so only one pokes. A lost claim returns `False` and the
    loser stays quiet.
    """
    with teamstate.state_lock(
        run.ctx.state_dir, team.key, timeout=TEAM_LOCK_TIMEOUT_SECONDS
    ):
        st = teamstate.load_state(run.ctx.state_dir, team.key)
        if st.get(field_name) is not None:
            return False
        st[field_name] = iso_utc(run.now)
        st["updated_at"] = iso_utc(run.now)
        teamstate.save_state(run.ctx.state_dir, team.key, st)
        return True


def _poke_since(st: dict, base: datetime) -> str:
    """The `--since` in the poke sentence: the dispatch's own `dispatched_at`."""
    return str(st.get("dispatched_at") or iso_utc(base))


def _poke(run: TickRun, team: Team, st: dict, ticket: str, base: datetime) -> None:
    """Poke a local team PM; a worker team hands the action off instead (D-8).

    The sentence is `WATCHDOG_POKE_TEXT` verbatim, `interrupt` false. A poke
    that cannot be sent is `[要本人處理]` on stdout, not `[warn]` (ruling 3):
    the team PM may already be gone, and that is the PMO's call. The call is
    outside every lock — the claim was already recorded, so a failed poke is
    still「poke 過一次」and escalates on time.
    """
    if team.sidecar == "worker":
        run.events.append(f"[worker-action] {team.key} watchdog-poke")
        return
    text = WATCHDOG_POKE_TEXT.format(
        ticket=ticket, channel=team.channel, since=_poke_since(st, base)
    )
    port = sidecar_port(run.ctx.deps, team.sidecar)
    if port is None:
        run.events.append(f"[要本人處理] {team.pm} 的看門狗 poke 送不到")
        return
    try:
        code = port.poke(team.pm, text=text, interrupt=False)
    except SidecarError:
        run.events.append(f"[要本人處理] {team.pm} 的看門狗 poke 送不到")
        return
    run.events.append(
        f"[watchdog] {team.pm} 派工 10 分鐘沒有「收到」→ poke 一次（{code}）"
    )


def _escalate(run: TickRun, team: Team, ticket: str) -> None:
    """The escalation: `[要本人處理]` with the seat's state (ruling 3, 10).

    The state is the one ② already fetched this round (the per-round cache),
    never a second call; a worker team has no local answer, so the line says
    `state=-` rather than claiming a state it did not read.
    """
    state = "-"
    if team.sidecar != "worker":
        answer = _agent_of(run, team)
        if isinstance(answer, AgentRecord):
            state = answer.state
    run.events.append(
        f"[要本人處理] {team.pm} poke 後 10 分鐘仍沒有回「收到 {ticket}」；"
        f"GET /agents/{team.pm}：state={state}"
    )


def step_watchdog(run: TickRun) -> None:
    """The watchdog: poke after 10 minutes, escalate 10 minutes later (③).

    The clock starts at `received_at`, falling back to `dispatched_at` (ruling
    2); a state with neither is not enough to act on. `ack_at` ends it. When
    the run has been suspended for longer than the interval both marks are
    still made at most once each (the claims), never a burst: the poke first,
    the escalation once its own deadline (poke + 10 minutes) has passed. A team
    whose state lock cannot be got within `TEAM_LOCK_TIMEOUT_SECONDS` gets a
    `[warn]` and is skipped this round (architect 2026-11-04 T5-3).
    """
    for team in teammsg.active_teams(run.ctx.config):
        if team.key in run.lock_skipped:
            continue
        try:
            _watchdog_for(run, team)
        except LockTimeout:
            _note_lock_timeout(run, team)


def _watchdog_for(run: TickRun, team: Team) -> None:
    """One team's ③ decision and its (claimed) poke or escalation."""
    st = teamstate.load_state(run.ctx.state_dir, team.key)
    if not _should_be_running(st) or st.get("ack_at"):
        return
    ticket = str(st.get("ticket") or "").strip()
    base = parse_ts(st.get("received_at") or st.get("dispatched_at"))
    if base is None:
        return
    poke_at = parse_ts(st.get("ack_poke_at"))
    if poke_at is None:
        deadline = base + timedelta(seconds=WATCHDOG_SECONDS)
        if run.now < deadline:
            run.due.append(deadline)
            return
        if _claim(run, team, "ack_poke_at"):
            _poke(run, team, st, ticket, base)
        # Whether this round won the claim or lost it to a parallel tick,
        # the escalation is 10 minutes after that poke.
        escalate_due = run.now + timedelta(seconds=WATCHDOG_SECONDS)
        if st.get("ack_escalated_at") is None:
            run.due.append(escalate_due)
        return
    if st.get("ack_escalated_at"):
        return
    escalate_due = poke_at + timedelta(seconds=WATCHDOG_SECONDS)
    if run.now < escalate_due:
        run.due.append(escalate_due)
        return
    if _claim(run, team, "ack_escalated_at"):
        _escalate(run, team, ticket)


def _with_poll(state: tickstate.TickState, *, key: str, now: datetime) -> tickstate.TickState:
    """One `worker_poll` entry moved forward by the default interval (§3.4).

    The next due is「現在＋間隔」, never a catch-up of the rounds that were
    missed (design §3.4: 不補跑錯過的次數). The entry's unknown fields are
    kept (Q-9).
    """
    existing = state.worker_poll.get(key)
    extra = dict(existing.extra) if existing is not None else {}
    entry = tickstate.WorkerPoll(
        next_due_at=iso_utc(now + timedelta(seconds=WORKER_POLL_SECONDS)),
        last_reported_at=iso_utc(now),
        extra=extra,
    )
    return replace(state, worker_poll={**state.worker_poll, key: entry})


def step_worker_poll(run: TickRun) -> None:
    """Worker teams: the due `[worker-action] poll` hand-off (③; D-8).

    Tick cannot read a worker team's sidecar, so the PMO's transition tool
    does it: this step only says when it is time. The moment lives in
    `tick.json`'s `worker_poll` (`next_due_at`, default interval 10 minutes);
    due → print once and push it forward, not due → stay quiet. A team with no
    dispatch (or already closed) has nothing to compare, so it is skipped.
    """
    for team in teammsg.active_teams(run.ctx.config):
        if team.sidecar != "worker":
            continue
        st = teamstate.load_state(run.ctx.state_dir, team.key)
        if not _should_be_running(st):
            continue
        entry = run.snapshot.worker_poll.get(team.key)
        due = parse_ts(entry.next_due_at) if entry is not None else None
        if due is not None and run.now < due:
            run.due.append(due)
            continue
        run.events.append(f"[worker-action] {team.key} poll")
        run.writes.append(partial(_with_poll, key=team.key, now=run.now))


# --------------------------------------------------------------------------- #
# 交接出去的群請求（插槽 A／B；#358 T5-3、design §3.2.1／§3.4）
# --------------------------------------------------------------------------- #


def step_handoffs(run: TickRun) -> None:
    """插槽 A：把這一輪讀到的群請求 id 記進 `handoffs`（Q-9 (a)）。"""
    handoff.record(run)


def step_group_sweep_due(run: TickRun) -> None:
    """插槽 B：到期的交接提醒 `[group-sweep-due]`（design §3.2.1）。"""
    handoff.sweep(run)


# --------------------------------------------------------------------------- #
# ④ 寫 tick.json（唯一 fatal 的步驟；鎖內重讀、期間不打 HTTP）
# --------------------------------------------------------------------------- #


def step_commit(run: TickRun) -> None:
    """Apply `run.writes` and `last_tick_at` inside `tick.json.lock` (§3.4).

    `tickstate.commit` re-reads the file inside the lock and applies each write
    in order, so a `wake.every` written by `pmo wake set` while this round was
    out on HTTP is kept. No HTTP call happens here. The lock wait is
    `LOCK_TIMEOUT_SECONDS`; a timeout raises `_lock.LockTimeout` and the CLI
    answers with `[warn]` and exit 2 (§3.4).
    """
    run.after = tickstate.commit(
        tickstate.tick_path(run.ctx.state_dir),
        now=run.now,
        writes=tuple(run.writes),
        timeout=LOCK_TIMEOUT_SECONDS,
    )


# --------------------------------------------------------------------------- #
# 步驟順序（T5-3／T5-4 只加一行；runner、commit 不動）
# --------------------------------------------------------------------------- #

tick_steps: tuple[Step, ...] = (
    Step(READ_STEP, step_read_round, warn=_read_warn),
    Step("agents", step_agents),
    Step("watchdog", step_watchdog),
    Step("worker_poll", step_worker_poll),
    # 插槽 A：① 之後記 handoffs（讀 run.handoffs_seen）
    Step("handoffs", step_handoffs),
    # 插槽 B：[group-sweep-due]（到期才印、往後推）
    Step("group_sweep", step_group_sweep_due),
    # ← T5-4 插槽：job 健康（GET /jobs，只碰 pmo_sidecar）
    Step("commit", step_commit, fatal=True),
)
