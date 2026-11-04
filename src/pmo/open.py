"""`pmo open`: start the team PM on the team's sidecar (design §3.2, §5.2 T-4b).

The design's row is:

    pmo open ‹team› [--session ‹agora:…›] [--fresh] [--provider P --model M]

stdout is the agent's state and, for an interactive team, its attach command;
exit 0 on success, 1 for a usage error or a state that is not ready (no `pmo
send` yet, no `workdir` in the settings, `--session` on a tui team, `--fresh`
on a non-tui one), 2 when the sidecar refuses or is unreachable (the error code
is printed). §2.1's sequence is the flow: the team state must already carry the
dispatch, then `POST /agents` (id, agent, workdir, input, plus whatever this
call applies) starts the team PM, and a successful open writes the agent's
state back into the team state (§2.3). `--session`／`--fresh` ride on the
sidecar port (`ports.SidecarPort.open_agent`).

The request's shape is the #356 architect ruling's §2 tree: `agent` comes from
the team settings (default `claude-code`; the sidecar requires it and has no
default), `workdir` from the team settings too (the sidecar requires it; `pmo`
does not create the directory and does not check that it exists — the sidecar's
400 `workdir_not_found` is printed as it is), `tui` only for an interactive
team, `fresh` only with `tui`, `session` only with `--session` (a new open
leaves the key out entirely; `null` is a 400), and `subscription` is never
sent.

The first input is MyPMO's `build_first_input` shape with the opening's place
(Mac／worker) taken out (#373 結構審 §A-1): it names the team channel, the
`--since` (the team state's `dispatched_at`, which `pmo send` wrote as the
dispatch letter's back-end time minus 60 seconds), the ticket to handle and the
PMO to report to; it asks the team PM to answer「收到 ‹ticket›」first (T-5's
watchdog reads that ack, `teammsg.is_ticket_ack`) and says what to do when the
letter is not there. Two forms: one line for an interactive team (the first
input is typed as a single line through tmux, so a newline would split it into
several messages) and the architect's multi-line one for a non-interactive
team. The `--since` is also how a team PM asks again without moving the cursor
(docs/angareion.md, docs/aicomm.md).

Provider／model follow D-10, relaxed by the ruling: the command only takes them
from the flags or the team record and never picks a model itself. A `--model`
without `--provider` is a usage error; a `--provider` alone is legal (the
sidecar picks the model); neither given and the team names none → both keys are
left out and the sidecar picks.

The sidecar's response carries more than the printed fields; only the known
ones (`id`, `state`, `exit`, `session`, `attach.local`,
`input_unconfirmed_at`) are read, and only `state`／`attach` are printed (D-12).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import teamstate
from .config import Team
from .errors import SchemaError, UsageError
from .ports import AgentRecord, SidecarError
from .runtime import Ctx, one_line, sidecar_port

__all__ = [
    "OPEN_INPUT",
    "OPEN_INPUT_TUI",
    "OpenResult",
    "input_for",
    "open_agent",
]

#: The first input for a non-interactive team PM (design §2.1). The line breaks
#: are the architect's own (#373 結構審 §A-1).
#: `{pm}` is the team PM's AI name, `{channel}` the team channel, `{since}` the
#: dispatch's `--since` (`team state`'s `dispatched_at`), `{ticket}` the team
#: state's `ticket`, `{pmo}` the settings' PMO identity and `{report_hint}` the
#: one thing that differs by `team.life`.
OPEN_INPUT = (
    "你是 team PM（{pm}）。照你的 harness 工作。指示在 Angareion：用\n"
    "`angareion read --channel {channel} --since {since} --json` 讀，只處理工單號是"
    " {ticket} 的那幾則，用訊息 id 去重。\n"
    "讀到後先回一則「收到 {ticket}」。讀不到 channel（exit 1）或讀不到這張單的"
    "指示，就等 60 秒再試，最多 5 次，還是不行就回報 PMO 後停下來。\n"
    "做完或有問題，用 `aicomm send --channel {channel} --to {pmo} --body -` 回報"
    "（{report_hint}）。"
)

#: The same input for an interactive team PM (`tui: true`), on one line: the
#: interactive agent's first input is typed as a single line, so a newline would
#: split it into several messages (#373 結構審 §A-1).
OPEN_INPUT_TUI = (
    "你是 team PM（{pm}）。照你的 harness 工作。指示在 Angareion：用 "
    "`angareion read --channel {channel} --since {since} --json` 讀，只處理工單號是"
    " {ticket} 的那幾則，用訊息 id 去重。讀到後先回一則「收到 {ticket}」。"
    "讀不到 channel（exit 1）或讀不到這張單的指示，就等 60 秒再試，最多 5 次，"
    "還是不行就回報 PMO 後停下來。做完或有問題，用"
    " `aicomm send --channel {channel} --to {pmo} --body -` 回報（{report_hint}）。"
)

#: `{report_hint}`：一般 team 請把 PR 連結寫在回報的 body 裡…
REPORT_HINT_PR = "PR 連結直接寫在 body"
#: …生活模式（`life: true`）的 team 產出不在 repo，寫位置就好。
REPORT_HINT_LIFE = "產出位置寫在回報裡"


@dataclass(frozen=True)
class OpenResult:
    """What `pmo open` got back: the seat, its state, the attach command.

    `attach` is the sidecar's `attach.local` verbatim (interactive teams only);
    `None` means「（非互動版，沒有 attach）」and the caller says so.
    `state_error` is set when the agent started but the team state could not
    be written: the caller still prints the state and attach (the agent is up)
    and exits 2, saying that the state write failed — the same「已做、沒記錄」
    shape as a `send` that went out before its state write failed.
    """

    team_key: str
    agent_id: str
    state: str
    exit: dict[str, object] | None
    attach: str | None
    state_error: str | None = None


def input_for(team: Team, since: str, ticket: str, pmo: str) -> str:
    """The first input, with the team, the dispatch and the PMO filled in.

    Interactive teams get the one-line form and non-interactive ones the
    multi-line form (#373 結構審 §A-1); `{report_hint}` follows `team.life`.
    """
    template = OPEN_INPUT_TUI if team.tui else OPEN_INPUT
    return template.format(
        pm=team.pm,
        channel=team.channel,
        since=since,
        ticket=ticket,
        pmo=pmo,
        report_hint=REPORT_HINT_LIFE if team.life else REPORT_HINT_PR,
    )


def _workdir_of(team: Team) -> str:
    """The team's `workdir` from its settings record (#356 裁定 §2).

    The sidecar requires a working directory that exists; this settings file is
    where it comes from now (it used to ride as an unknown `extra` field). A
    missing one is exit 1 with the thing to add, not a guessed path — and the
    directory's existence is still the sidecar's check (400
    `workdir_not_found`), never this command's.
    """
    if team.workdir is not None:
        return team.workdir
    raise UsageError(
        f"team「{team.key}」的設定缺少 workdir（pmo open 要用它交 sidecar 開 agent）；"
        f"在 teams 設定裡這個 team 加上 workdir：絕對路徑"
    )


def _provider_and_model(
    team: Team, provider: str | None, model: str | None
) -> tuple[str | None, str | None]:
    """The sidecar's `provider`／`model`, from the flags or the team (D-10).

    Flags win over the team's defaults; neither present in either place returns
    `(None, None)` and the keys are left out, which is the sidecar's「依政策自己
    挑」. A `--model` needs a `--provider` (a bare model names nothing to run
    it on); a `--provider` alone is legal — the sidecar's matrix has「provider
    given, model omitted」as a new valid shape (#356 裁定 §2).
    """
    if provider is None:
        if model is not None:
            raise UsageError("--model 要配 --provider（單獨的 model 不行；或兩個都不給）")
        return team.provider, team.model
    if not provider.strip():
        raise UsageError("--provider 是空的")
    if model is not None and not model.strip():
        raise UsageError("--model 是空的")
    return provider, model


def _dispatch_of(ctx: Ctx, team: Team) -> tuple[str, str]:
    """The dispatch's `--since` and `ticket` from the team state (§2.1、§3.2).

    A team state without `ticket` means `pmo send` has not run: exit 1 and no
    sidecar call. A `ticket` without `dispatched_at` is a state that predates
    T-4a's field (or was written by hand); the first input cannot be built
    without it, so it is the same「狀態不足」rather than a guessed time. Both go
    into the first input: the ticket is the one the team PM must handle (and
    answer「收到 ‹ticket›」to) and the `--since` is where to read from.
    """
    try:
        state = teamstate.load_state(ctx.state_dir, team.key)
    except (SchemaError, OSError) as error:
        raise SchemaError(f"team 狀態讀不起來：{one_line(error)}") from error
    ticket = str(state.get("ticket") or "").strip()
    if not ticket:
        raise UsageError(
            f"狀態不足：team {team.key} 還沒有派工。先用 pmo send 把工單與指示送給 team PM"
        )
    since = str(state.get("dispatched_at") or "").strip()
    if not since:
        raise UsageError(
            f"狀態不足：team {team.key} 的狀態沒有 dispatched_at（pmo send 會寫）；"
            f"先重跑 pmo send，或修好 team 狀態"
        )
    return since, ticket


def _write_state(ctx: Ctx, team: Team, record: AgentRecord) -> None:
    """Record the just-opened agent's `state`／`exit`／`session` in the team state (§2.3).

    待確認: the design says「成功 → 寫 team 狀態」without naming the fields.
    Only the ones the team-state schema already has are written (`state`,
    `exit`, and `session` when the sidecar answered one); the dispatch fields
    and unknown fields stay as they are (Q-9). `session` is what tick's
    `NEED_REOPEN … session=…` reads later (#373 低-1); a `None` answer does not
    erase a recorded session.

    The read-modify-write runs inside `teamstate.state_lock` and re-reads the
    state there, so it cannot overwrite the「收到」`pmo inbox read` writes at
    the same time (the pattern #369 settled on; #370 中-2).
    """
    with teamstate.state_lock(ctx.state_dir, team.key):
        state = teamstate.load_state(ctx.state_dir, team.key)
        state["state"] = record.state
        state["exit"] = record.exit
        if record.session is not None:
            state["session"] = record.session
        teamstate.save_state(ctx.state_dir, team.key, state)


def open_agent(
    ctx: Ctx,
    *,
    team: Team,
    session: str | None,
    fresh: bool,
    provider: str | None,
    model: str | None,
) -> OpenResult:
    """One `pmo open` (design §3.2): the sidecar call and its result.

    Raises `UsageError` for the exit-1 cases (retired／worker team, `--session`
    with `--fresh`, `--session` on a tui team, `--fresh` on a non-tui team, a
    bare `--model`, no workdir, no dispatch), and `SidecarError` for the exit-2
    sidecar refusals; the caller prints them with known fields only (D-12). A
    state write that fails after the agent started is not raised: the result
    carries `state_error` so the caller can say the agent is already up.

    The tui checks are here, in the command layer, rather than left to the
    sidecar's 400: `tui` with a `session`, and `fresh` without `tui`, are
    shapes this command must never build (#356 裁定 §2).
    """
    if team.retired:
        raise UsageError(f"team {team.key} 已退役（retired=true），不開")
    if team.sidecar == "worker":
        raise UsageError(
            f"team {team.key} 在 worker 上；第一階段 pmo open 只開本機 sidecar 的 team"
            f"（D-8），worker team 的 open 留在 MyPMO"
        )
    if session is not None and fresh:
        raise UsageError("--session 與 --fresh 不能一起給")
    if session is not None and not session.strip():
        raise UsageError("--session 是空的")
    if team.tui and session is not None:
        raise UsageError(
            f"team {team.key} 是互動版（tui: true），不能帶 --session：sidecar 會回 400"
        )
    if fresh and not team.tui:
        raise UsageError(
            f"team {team.key} 不是互動版（tui: false），--fresh 只配互動版"
        )

    workdir = _workdir_of(team)
    chosen_provider, chosen_model = _provider_and_model(team, provider, model)
    since, ticket = _dispatch_of(ctx, team)
    first_input = input_for(team, since, ticket, ctx.config.pmo_identity)

    port = sidecar_port(ctx.deps, team.sidecar)
    if port is None:
        raise SidecarError(detail="sidecar 埠沒有接上（Deps.sidecar_for）")
    record = port.open_agent(
        agent_id=team.pm,
        agent=team.agent,
        workdir=workdir,
        input=first_input,
        provider=chosen_provider,
        model=chosen_model,
        tui=team.tui,
        session=session,
        fresh=fresh,
        agent_args=team.agent_args,
    )

    state_error: str | None = None
    try:
        _write_state(ctx, team, record)
    except (SchemaError, OSError) as error:
        state_error = one_line(error)

    # `attach.local` verbatim (sidecar §2.1: run it as it is, never parsed).
    attach = record.attach
    return OpenResult(
        team_key=team.key,
        agent_id=record.agent_id,
        state=record.state,
        exit=record.exit,
        attach=attach.strip() if isinstance(attach, str) and attach.strip() else None,
        state_error=state_error,
    )
