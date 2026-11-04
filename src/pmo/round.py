"""The read round: one `angareion read` and everything it brings (T-2, §2.2).

`pmo inbox read` is design §3.2's「只做 tick 的收信一輪」, and tick's step ① is
the same round (§2.2). So the round lives here, not in the CLI, and the command
only prints what it returns and decides the exit code (arch review 中-2;
#358's tick calls the same function).

Two rules shape it:

- **Nothing is lost** (review M-1): the secretary stage and the team-message
  stage are separate tries. A failure in one keeps the events the other one
  produced (an ack that already went out must not be reported as「未處理」),
  and the `[要本人處理]` line lists only that stage's messages.
- **The cursor moves once**: the port's `read` advances it before anything is
  processed, so a stage failure cannot be retried by reading again; the line
  says so and points at `read --since` (design §2.3).

What a failure means for the exit code is the caller's call: a bad state file
before the read raises (`SchemaError` → exit 2, cursor untouched), a failed
`angareion read` raises `PortError` (the command prints `[warn]` and exits 0,
§3.2), and a stage that failed after the read is an event line (§2.3 `[要本人
處理]`).
"""

from __future__ import annotations

from typing import Iterable

from . import inbox, ledger, teammsg, teamstate
from .errors import SchemaError
from .ports import PortError
from .runtime import Ctx, assert_pmo_identity

__all__ = ["precheck", "read_round", "secretary_candidates", "team_candidates"]

SECRETARY_STAGE = "秘書階段"
TEAM_STAGE = "團隊訊息階段"


def precheck(ctx: Ctx) -> None:
    """Validate the state files **before** the cursor can move (design §2.3).

    The ledger and every active team's state: a damaged file must stop the
    round while `angareion read` still has not run, or the batch is lost
    (中 2 of the current tool's reviews).
    """
    ledger.load_state(ctx.state_dir)
    for team in teammsg.active_teams(ctx.config):
        teamstate.load_state(ctx.state_dir, team.key)


def _claimed_by_secretary(ctx: Ctx, message: object) -> bool:
    """Whether the secretary stage looks at this message (ids for the failure line)."""
    data = inbox.as_dict(message)
    if inbox.is_group_request(data, ctx.config):
        return True  # it does handle them: that is what [group-handoff] is
    if str(data.get("from") or "") == ctx.config.secretary_identity:
        return True
    return inbox.is_call(data, ctx.config)


def _belongs_to_team_stage(ctx: Ctx, message: object) -> bool:
    """The messages the team stage prints (`[msg]`): not the secretary's, not ours."""
    data = inbox.as_dict(message)
    channel = str(data.get("channel") or "")
    sender = str(data.get("from") or "")
    if channel == ctx.config.dispatch_channel and sender == ctx.config.secretary_identity:
        return False
    return sender != ctx.config.pmo_identity


def secretary_candidates(ctx: Ctx, messages: Iterable) -> list[str]:
    """The ids the secretary stage covers, for its failure line (may be partial)."""
    return [
        str(inbox.as_dict(message).get("id") or "")
        for message in messages
        if _claimed_by_secretary(ctx, message)
    ]


def team_candidates(ctx: Ctx, messages: Iterable) -> list[str]:
    """The ids the team stage would have printed, for its failure line."""
    return [
        str(inbox.as_dict(message).get("id") or "")
        for message in messages
        if _belongs_to_team_stage(ctx, message)
    ]


def _stage_error(stage: str, ids: list[str], error: Exception) -> str:
    """The `[要本人處理]` line for one failed stage (design §2.3)."""
    listed = "、".join(ids) if ids else "（讀不到 id）"
    what = (
        "這批信可能只處理了一部分" if stage == SECRETARY_STAGE else "這些信未處理"
    )
    return (
        f"[要本人處理] {stage}處理來信時出錯（state／磁碟），{what}"
        f"（游標已前進、不會重送），id：{listed}，"
        f"請用 angareion read --since <時刻> 補讀：{inbox.one_line_for_event(error)}"
    )


def read_round(
    ctx: Ctx,
    *,
    handoffs: list[str] | None = None,
    team_lock_timeout: float | None = None,
    lock_skipped: set[str] | None = None,
) -> list[str]:
    """One inbox round: 身分守門 → 預檢 → 讀信 → 秘書階段 → 團隊訊息階段。

    Returns the event lines; raises `SchemaError` when a state file is damaged
    before the read (exit 2, nothing done) and `PortError` when `angareion read`
    itself fails (the caller prints `[warn]` and exits 0 — §3.2's「部分失敗」).

    `handoffs` is passed through to `inbox.process_messages`: tick hands in its
    collection list so T5-3 can record the group requests without parsing the
    event text. `None` (every existing caller) keeps the old behaviour.
    `team_lock_timeout` is tick's team-lock wait (#358 T5-3): the team-message
    stage then skips a team whose lock it cannot get, with a `[warn]`; `None`
    keeps `pmo inbox read` waiting as before. `lock_skipped` is tick's
    round-level set of teams already skipped, shared with the later steps.
    """
    assert_pmo_identity(ctx.config)
    precheck(ctx)
    messages = list(ctx.deps.angareion.read())

    events: list[str] = []
    try:
        events.extend(inbox.process_messages(ctx, messages, handoffs=handoffs))
    except (SchemaError, PortError, OSError) as error:
        events.append(_stage_error(SECRETARY_STAGE, secretary_candidates(ctx, messages), error))
    try:
        events.extend(
            teammsg.process_team_messages(
                ctx,
                messages,
                seen=None,
                lock_timeout=team_lock_timeout,
                lock_skipped=lock_skipped,
            )
        )
    except (SchemaError, PortError, OSError) as error:
        events.append(_stage_error(TEAM_STAGE, team_candidates(ctx, messages), error))
    return events
