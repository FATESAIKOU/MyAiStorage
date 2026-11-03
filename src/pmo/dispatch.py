"""`pmo check` / `pmo send`: the dispatch half of T-4a (design §3.2, §5.2).

The design's table gives the two rows and the `[wake-hint]` rule; the item
list of `check` and the fixed head of the指示信 are not written there. Both
are kept small and explicit here, and the report for #355 names them as
待確認 (the design is the contract; nothing new is invented beyond what the
dispatch has to carry: the ticket, the issue's title and body, and the PMO's
instructions).

`send`'s flow is §2.1's: read the issue through the GitHub port, send the
letter through the Angareion port on the team's channel, read the letter's
back-end time with `read --since` (which never moves the PMO's cursor), then
write `ticket`／`message_id`／`received_at` into the team's state. `check`
only looks at local files (settings and the two state files) and never calls
a port.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import teamstate, tickstate
from .config import Team
from .errors import SchemaError, SentButNotRecorded, UsageError
from .ports import IssueData, PortError, seconds_of
from .runtime import Ctx, one_line
from .timeutil import iso_utc

__all__ = [
    "CheckItem",
    "SendResult",
    "WATCHDOG_SECONDS",
    "build_letter",
    "check",
    "hint_for",
    "parse_ticket",
    "send",
    "tick_path",
]

#: 看門狗的間隔（design §3.6：看門狗 10 分鐘）。`[wake-hint]` 只在
#: `wake.every` **比它長**（或沒有開）時印；剛好 10 分鐘不印。
WATCHDOG_SECONDS = 10 * 60.0

#: `read --since` 的回看窗口：本機時鐘可能比後端快，用 `angareion` 自己的
#: 複寫容許量（docs/angareion.md：`at − 60 秒`）把剛送出的那封包進來，
#: 再用訊息 id 挑出指示信本身。
READ_BACK_SECONDS = 60

#: `‹owner/repo›#‹n›`（design §3.3：`gh issue view <n> --repo <owner/repo>`）。
_TICKET_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")


@dataclass(frozen=True)
class CheckItem:
    """One `pmo check` line: the item, whether it passed, and the detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class SendResult:
    """What `pmo send` reports: the letter's id, its back-end time, the hint."""

    message_id: str
    received_at: str
    wake_hint: str | None


def tick_path(state_dir: Path) -> Path:
    """The tick state file in this state directory (design §3.4)."""
    return Path(state_dir) / tickstate.STATE_FILE_NAME


def parse_ticket(text: str) -> tuple[str, int]:
    """`‹owner/repo›#‹n›` → `(repo, number)`; anything else is a usage error."""
    match = _TICKET_RE.fullmatch(str(text or "").strip())
    if not match:
        raise UsageError(f"工單要寫成 ‹owner/repo›#‹n›，收到：{text!r}")
    return match.group(1), int(match.group(2))


def build_letter(
    *, ticket: str, title: str, issue_body: str, instructions: str, identity: str
) -> str:
    """The dispatch letter: a fixed head, the issue, then the PMO's words.

    待確認（design §3.2 只寫「固定頭＋正文」）：the head names the ticket, the
    issue's title and who dispatched it; the body carries the issue verbatim
    and `--body`'s instructions after it, so the team PM gets both without a
    second lookup. `ticket` stays the bare `repo#n` — the team PM's「收到
    ‹工單›」match (`pmo.teammsg.is_ticket_ack`) looks for exactly that string.
    """
    issue_text = str(issue_body or "").strip() or "（工單沒有內文）"
    instructions_text = str(instructions or "").strip()
    return (
        f"# 派工：{ticket} {title}\n\n"
        f"- 工單：{ticket}\n"
        f"- 派工者：{identity}\n\n"
        f"## 工單內容\n"
        f"{issue_text}\n\n"
        f"## 指示\n"
        f"{instructions_text}\n"
    )


def hint_for(every: str | None) -> str | None:
    """The `[wake-hint]` value: `every` when it is longer than the watchdog.

    `None` (the PMO chose not to schedule, or there is no `tick.json`) is
    `off`; 10 minutes and below give no hint at all (design §3.2`[wake-hint]`).
    """
    if every is None:
        return "off"
    return every if seconds_of(every) > WATCHDOG_SECONDS else None


def _wake_every(ctx: Ctx) -> str | None:
    """`tick.json` 的 `wake.every`；檔不存在視為 `null`，檔壞了也當 `null`。

    待確認：design §3.4 只說 tick／`handoff-close`／`pmo wake` 在檔壞掉時回
    exit 2；`pmo send` 不在其中，所以這裡不因為壞檔而擋下（信已經送出時更
    不能回 2 讓人重送），只在 stderr 說一聲並照「沒有叫醒」提示。
    """
    try:
        state = tickstate.load_or_empty(tick_path(ctx.state_dir))
    except (SchemaError, OSError) as error:
        ctx.log(f"[warn] tick.json 讀不起來：{one_line(error)}（視為叫醒 off）")
        return None
    return state.wake.every


def check(ctx: Ctx, key: str) -> list[CheckItem]:
    """`pmo check ‹team›`: local readiness, one `OK`／`FAIL` per item.

    待確認：design §3.2 只寫「逐項 OK／FAIL」。Items are the things `send`
    needs and can be checked without touching a port: the team is in the
    settings and not retired, its team state file (if any) reads, and
    `tick.json` (if any) reads. `send` itself is the real gate for the issue,
    the identity and the ports.
    """
    team = ctx.config.team(key)
    if team is None:
        return [CheckItem("team", False, f"設定裡沒有 team「{key}」")]

    items = [CheckItem("team", True, f"設定裡有 team「{key}」")]
    items.append(
        CheckItem(
            "retired",
            not team.retired,
            "已退役（retired=true）" if team.retired else "未退役",
        )
    )

    try:
        state = teamstate.load_state(ctx.state_dir, key)
    except (SchemaError, OSError) as error:
        items.append(CheckItem("state", False, f"team 狀態檔讀不起來：{one_line(error)}"))
    else:
        ticket = str(state.get("ticket") or "")
        detail = (
            f"team 狀態檔讀得起來（ticket={ticket}）"
            if ticket
            else "team 狀態檔讀得起來（尚未派工）"
        )
        items.append(CheckItem("state", True, detail))

    path = tick_path(ctx.state_dir)
    try:
        state = tickstate.load_or_empty(path)
    except (SchemaError, OSError) as error:
        items.append(CheckItem("tick", False, f"tick.json 讀不起來：{one_line(error)}"))
    else:
        every = state.wake.every
        if path.exists():
            items.append(
                CheckItem("tick", True, f"tick.json 讀得起來（叫醒 {every or 'off'}）")
            )
        else:
            items.append(CheckItem("tick", True, "tick.json 不存在（叫醒視為 off）"))

    return items


def _backend_time(ctx: Ctx, message_id: str, sent_at: datetime) -> str:
    """The dispatch letter's `received_at`, read back without moving the cursor.

    `read --since` never moves the PMO's cursor (docs/angareion.md), so the
    one-reader rule (`pmo tick`) still holds. The letter is picked by id, not
    by position, because the window may hold other messages.
    """
    since = iso_utc(sent_at - timedelta(seconds=READ_BACK_SECONDS))
    try:
        recent = ctx.deps.angareion.read(since=since)
    except PortError as error:
        raise SentButNotRecorded(
            f"指示信已送出（id={message_id}），但讀不回後端時刻：{one_line(error)}。"
            f"不要直接重送；先確認通道上那封信，再處理 team 狀態",
            message_id=message_id,
        ) from error
    for message in recent:
        if message.message_id == message_id:
            return message.received_at
    raise SentButNotRecorded(
        f"指示信已送出（id={message_id}），但 read --since 找不到它，拿不到後端時刻。"
        f"不要直接重送；先確認通道上那封信，再處理 team 狀態",
        message_id=message_id,
    )


def _record_dispatch(
    ctx: Ctx, team: Team, *, ticket: str, message_id: str, received_at: str
) -> None:
    """Write the dispatch into the team state (design §2.1、§3.4).

    The design's sequence names `ticket`／`message_id`／`received_at`. Two more
    fields ride along because the existing team-message match needs them
    (`pmo.teammsg.process_team_messages`): `closed` must be `false` (it treats a
    missing one as closed and would never write `ack_at`), and a new dispatch
    clears the previous watchdog marks (`ack_at`／`ack_poke_at`／
    `ack_escalated_at`) so the watchdog starts over. Unknown fields are kept
    (Q-9). 待確認：the design does not spell these four fields out.
    """
    try:
        state = teamstate.load_state(ctx.state_dir, team.key)
    except (SchemaError, OSError) as error:
        raise SentButNotRecorded(
            f"指示信已送出（id={message_id}），但 team 狀態讀不起來：{one_line(error)}。"
            f"不要直接重送",
            message_id=message_id,
        ) from error

    state.update(
        {
            "agent": team.pm,
            "ticket": ticket,
            "message_id": message_id,
            "received_at": received_at,
            "closed": False,
            "ack_at": None,
            "ack_poke_at": None,
            "ack_escalated_at": None,
        }
    )
    try:
        teamstate.save_state(ctx.state_dir, team.key, state)
    except (SchemaError, OSError) as error:
        raise SentButNotRecorded(
            f"指示信已送出（id={message_id}），但 team 狀態寫不進去：{one_line(error)}。"
            f"不要直接重送；先修 state 檔",
            message_id=message_id,
        ) from error


def send(
    ctx: Ctx,
    *,
    team: Team,
    repo: str,
    number: int,
    issue: IssueData,
    instructions: str,
) -> SendResult:
    """Send one指示信 to `team` and record it (design §2.1, §3.2).

    The identity gate is the caller's (`assert_pmo_identity`, before anything
    leaves); this function only speaks to the two ports and writes the state.
    """
    ticket = f"{repo}#{number}"
    letter = build_letter(
        ticket=ticket,
        title=issue.title,
        issue_body=issue.body,
        instructions=instructions,
        identity=ctx.config.pmo_identity,
    )
    sent_at = ctx.deps.now()
    message_id = ctx.deps.angareion.send(
        channel=team.channel, to=team.pm, body=letter, urgency=5
    )
    received_at = _backend_time(ctx, message_id, sent_at)
    _record_dispatch(
        ctx, team, ticket=ticket, message_id=message_id, received_at=received_at
    )
    wake_hint = hint_for(_wake_every(ctx))
    return SendResult(
        message_id=message_id, received_at=received_at, wake_hint=wake_hint
    )
