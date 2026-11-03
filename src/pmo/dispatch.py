"""`pmo check` / `pmo send`: the dispatch half of T-4a (design §3.2, §5.2).

The design's table gives the two rows and the `[wake-hint]` rule; the item
list of `check` and the fixed head of the指示信 are the #355 rulings
(REPORT-arch-355-rulings). `check` looks at the settings, the two state files,
the team's channel (through the Angareion port's read-only `log`) and the
team's sidecar (`GET /agents`, #356). `send`'s letter keeps MyPMO's
`build_instruction`
head (ticket, title, scope, acceptance, how to report, the dispatch-time note),
so the team PMs read the same shape they already know; the M2 section is not
carried (it is environment-note content).

`send`'s flow is §2.1's: read the issue through the GitHub port, send the
letter through the Angareion port on the team's channel, read the letter's
back-end time with `log --channel ‹team channel› --since` (read-only: it neither
moves the PMO's cursor nor makes this command a second reader), then write
`ticket`／`message_id`／`received_at`／`dispatched_at` into the team's state
under the team's lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ainame import parse as parse_name

from . import teamstate, tickstate
from .config import Team
from .errors import SchemaError, SentButNotRecorded, UsageError
from .ports import (
    IssueData,
    PortError,
    SidecarError,
    SidecarShapeError,
    seconds_of,
)
from .runtime import Ctx, one_line, sidecar_port
from .timeutil import iso_utc

__all__ = [
    "CheckItem",
    "MAX_LETTER_CHARS",
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

#: `log --since` 的回看窗口：本機時鐘可能比後端快，用 `angareion` 自己的
#: 複寫容許量（docs/angareion.md：`at − 60 秒`）把剛送出的那封包進來，
#: 再用訊息 id 挑出指示信本身。
READ_BACK_SECONDS = 60

#: `dispatched_at`＝`received_at` 往前 60 秒（#355 裁定 3）：這是 T-4b 的
#: `open` 第一則輸入要帶的 `--since`。
DISPATCHED_BACK_SECONDS = 60

#: 指示信的優先度：一般派工用 5；信裡也要求 team PM 只在「要本人決定」的
#: 回報用 8 以上。寫成常數，換掉時只改一處。
DISPATCH_URGENCY = 5

#: angareion 一則訊息的上限（#355 裁定 2）：組好的信超過就拒送，不截斷。
MAX_LETTER_CHARS = 65536

#: `‹owner/repo›#‹n›`（design §3.3：`gh issue view <n> --repo <owner/repo>`）。
_TICKET_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")


@dataclass(frozen=True)
class CheckItem:
    """One `pmo check` line: the item, whether it passed, and the detail.

    `ok` is False only for a FAIL; a `note` line is informational and is
    printed as `--  ‹item›：‹detail›` (the worker-team line), so it can neither
    pass nor fail the check.
    """

    name: str
    ok: bool
    detail: str
    note: bool = False


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
    *,
    ticket: str,
    title: str,
    issue_body: str,
    instructions: str,
    channel: str,
    pmo: str,
    stamp: str,
    life: bool = False,
) -> str:
    """The dispatch letter in MyPMO `build_instruction`'s fixed head.

    (#355 裁定 2.) The head carries the ticket, the title, where scope and
    acceptance are, how to report (`aicomm send --channel ‹ch› --to ‹pmo›`,
    urgency 8 and above for a decision the person must make) and the dispatch
    time (the authority is the back end's `received_at`; the PMO uses its
    previous 60 seconds as `--since`). Then the issue as read from GitHub, then
    the PMO's own words. `life: true` teams get the life-mode reporting lines.
    The current tool's M2 section (how to open an agent) is deliberately not
    carried: it is environment-note content and would go stale in the letter.
    `ticket` stays the bare `repo#n` — the team PM's「收到 ‹工單›」match
    (`pmo.teammsg.is_ticket_ack`) looks for exactly that string.
    """
    issue_text = str(issue_body or "").strip() or "（工單沒有內文）"
    instructions_text = str(instructions or "").strip()
    if life:
        report = (
            "- 回報格式：做到哪、產出位置（工作目錄／檔案路徑）、驗證輸出（若有）、"
            "剩下的問題、要本人決定的事附建議；\n"
            f"  回報用 `aicomm send --channel {channel} --to {pmo} --body -`；\n"
            "  要本人決定的急件用 `--urgency 8` 以上。\n"
        )
    else:
        report = (
            "- 回報格式：做到哪、半成品在哪、測試實際輸出、剩下的問題、"
            "要本人決定的事附建議；\n"
            f"  PR 連結直接寫在 body，用 `aicomm send --channel {channel} --to {pmo} --body -`；\n"
            "  要本人決定的急件用 `--urgency 8` 以上。\n"
        )
    return (
        f"## {ticket} 派工指示\n\n"
        f"- 工單號：{ticket}\n"
        f"- 標題：{title}\n"
        "- 範圍：見下方指示本文的「範圍」一節\n"
        "- 驗收：見下方指示本文的「驗收」一節\n"
        f"{report}"
        f"- 派工時刻：{stamp}（本機時間，僅供對照；權威時刻是本則訊息的後端 received_at，\n"
        "  PMO 會用它的前 60 秒當 `--since`，不用本機時間對時。）\n\n"
        "---\n\n"
        "## 工單本文（PMO 派工時從 GitHub 讀出）\n\n"
        f"標題：{title}\n\n"
        f"{issue_text}\n\n"
        "---\n\n"
        f"{instructions_text}\n"
    )


def hint_for(every: str | None, *, unknown: bool = False) -> str | None:
    """The `[wake-hint]` value: `every` when it is longer than the watchdog.

    `None` (the PMO chose not to schedule, or there is no `tick.json`) is
    `off`; 10 minutes and below give no hint at all (design §3.2`[wake-hint]`).
    `unknown` is a `tick.json` that could not be read: saying `off` there would
    claim something we do not know (#355 裁定 4).
    """
    if unknown:
        return "unknown（tick.json 讀不起來）"
    if every is None:
        return "off"
    return every if seconds_of(every) > WATCHDOG_SECONDS else None


def _wake_every(ctx: Ctx) -> tuple[str | None, bool]:
    """`tick.json` 的 `wake.every`, and whether the file could not be read.

    檔不存在視為 `null`（叫醒沒排），檔壞了回 `(None, True)`：`send` 已經把
    信送出去，不能因為壞檔回 exit 2 讓人重送（design §3.4 的壞檔 exit 2 只
    點名 tick／`handoff-close`／`pmo wake`），所以 stderr 印一行 `[warn]`、
    stdout 的叫醒值印 `unknown`（#355 裁定 4）。
    """
    try:
        state = tickstate.load_or_empty(tick_path(ctx.state_dir))
    except (SchemaError, OSError) as error:
        ctx.log(f"[warn] tick.json 讀不起來：{one_line(error)}")
        return None, True
    return state.wake.every, False


def check(ctx: Ctx, key: str) -> list[CheckItem]:
    """`pmo check ‹team›`: readiness, one `OK`／`FAIL` per item (#355 裁定 1).

    Items, in order: the team is in the settings and not retired; the PM name
    is a legal AI name whose role is `pm`; the team state file (if any) reads;
    `tick.json` (if any) reads; the team's channel is readable through the
    read-only `log --channel --since now` (exit 1 means「還沒落地或名字錯」,
    anything else is a back-end failure); the team's sidecar answers
    `GET /agents` (#356, the #355 ruling's sixth item). A worker team gets one
    informational line instead of the sidecar item: the worker side is the
    transition tool's job (D-8).

    The sidecar item only runs for a local team, on **that team's** address
    (design §3.1). When the deployment wires no sidecar port at all
    (`Deps.sidecar_for is None`) the item FAILs and says it cannot be checked:
    the design's check exists to catch「sidecar 沒在跑」before work starts, and
    a check that silently skips it would pass a broken machine. The design
    does not name that case; #356's report lists it as 待確認.

    An answer that arrived (2xx) but is not a shape the port reads is its own
    FAIL,「回應形狀不符」, not「連不上」(#356 裁定 §3): the first says the
    sidecar is up and talking a shape this build does not know — the two
    versions disagree — while「連不上」says it did not answer at all; only the
    second is a machine to restart.
    """
    team = ctx.config.team(key)
    if team is None:
        return [CheckItem("team", False, f"設定裡沒有 team「{key}」")]

    retired = "已退役（retired=true）" if team.retired else "未退役"
    items = [CheckItem("team", not team.retired, f"設定裡有 team「{key}」，{retired}")]

    name = parse_name(team.pm)
    if name.role == "pm":
        items.append(CheckItem("pm", True, f"PM 名合法且角色是 pm（{team.pm}）"))
    else:
        items.append(
            CheckItem("pm", False, f"PM 名的角色是 {name.role}，不是 pm（{team.pm}）")
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
        tick = tickstate.load_or_empty(path)
    except (SchemaError, OSError) as error:
        items.append(CheckItem("tick", False, f"tick.json 讀不起來：{one_line(error)}"))
    else:
        every = tick.wake.every
        if path.exists():
            items.append(
                CheckItem("tick", True, f"tick.json 讀得起來（叫醒 {every or 'off'}）")
            )
        else:
            items.append(CheckItem("tick", True, "tick.json 不存在（叫醒視為 off）"))

    try:
        ctx.deps.angareion.log(channel=team.channel, since=iso_utc(ctx.deps.now()))
    except PortError as error:
        if error.returncode == 1:
            items.append(
                CheckItem("channel", False, "channel 讀不到（還沒落地或名字錯）")
            )
        else:
            items.append(CheckItem("channel", False, f"後端錯誤：{one_line(error)}"))
    else:
        items.append(CheckItem("channel", True, f"channel 讀得到（{team.channel}）"))

    if team.sidecar == "worker":
        items.append(
            CheckItem(
                "worker team",
                True,
                "worker 側的檢查由過渡工具負責（D-8）",
                note=True,
            )
        )
    else:
        port = sidecar_port(ctx.deps, team.sidecar)
        if port is None:
            items.append(
                CheckItem("sidecar", False, "sidecar 埠沒有接上，查不到 GET /agents")
            )
        else:
            try:
                port.list_agents()
            except SidecarShapeError as error:
                # 2xx but an unreadable shape: the sidecar is up, so it is not
                #「連不上」— it is the two versions disagreeing (#356 裁定 §3).
                items.append(CheckItem("sidecar", False, _unreadable_answer(error)))
            except SidecarError as error:
                items.append(CheckItem("sidecar", False, _sidecar_failure(error, team.sidecar)))
            else:
                items.append(
                    CheckItem("sidecar", True, f"sidecar 有回應（GET /agents，{team.sidecar}）")
                )
    return items


def _sidecar_failure(error: SidecarError, address: str) -> str:
    """`FAIL sidecar` line for a sidecar that did not answer with a 2xx (#356 裁定 §3).

    Two shapes, two repairs: no status at all is「連不上」and names the address
    to look at; a non-2xx is the sidecar answering with a refusal, so the number
    and the machine code are what to read. Never the response body's text
    (D-12).
    """
    if error.status is None:
        return f"sidecar 連不上（{address}）"
    if error.code:
        return f"sidecar 回 HTTP {error.status} {error.code}"
    return f"sidecar 回 HTTP {error.status}"


def _unreadable_answer(error: SidecarShapeError) -> str:
    """`FAIL sidecar` line for a 2xx answer whose shape is not the one we read.

    The sidecar answered; the shape is not `{"agents": [...]}`. This is the
    version-mismatch case, not a dead machine, so the line says which shape was
    expected and points at the versions (#356 裁定 §3).

    A record inside a correct envelope (`error.record`) is a different repair:
    the wrapper was right, so claiming the outer expected shape would be wrong —
    name the record's own problem instead (#356 修正三 低-2).
    """
    if error.record:
        return (
            f"sidecar 有回應，但某筆 agent 記錄缺 id／state（{error}）；"
            "可能是 sidecar 與 pmo 的版本不一致"
        )
    return (
        "sidecar 有回應，但回應形狀不符（預期 `{\"agents\": [...]}`）；"
        "可能是 sidecar 與 pmo 的版本不一致"
    )


def _backend_time(
    ctx: Ctx, team: Team, message_id: str, sent_at: datetime
) -> str:
    """The dispatch letter's `received_at`, read back without moving the cursor.

    `log --channel` is read-only: it lists the team's channel without moving
    the PMO's cursor and without making this command a second reader (#355
    高-1). The channel matters: the letter's `to` is the team PM, so a bare
    `read` (「寄給我的」) would never see it. The letter is picked by id, not
    by position, because the window may hold other messages.
    """
    since = iso_utc(sent_at - timedelta(seconds=READ_BACK_SECONDS))
    try:
        recent = ctx.deps.angareion.log(channel=team.channel, since=since)
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
        f"指示信已送出（id={message_id}），但 log --channel --since 找不到它，"
        f"拿不到後端時刻。不要直接重送；先確認通道上那封信，再處理 team 狀態",
        message_id=message_id,
    )


def _dispatched_at(received_at: str, message_id: str) -> str:
    """`received_at` 往前 60 秒 (#355 裁定 3): the `--since` `open` will use."""
    try:
        moment = datetime.fromisoformat(received_at)
    except (TypeError, ValueError) as error:
        raise SentButNotRecorded(
            f"指示信已送出（id={message_id}），但後端時刻看不懂（{received_at!r}），"
            f"算不出 dispatched_at。不要直接重送；先確認通道上那封信，再處理 team 狀態",
            message_id=message_id,
        ) from error
    if moment.tzinfo is None:
        raise SentButNotRecorded(
            f"指示信已送出（id={message_id}），但後端時刻沒有時區（{received_at!r}），"
            f"算不出 dispatched_at。不要直接重送；先確認通道上那封信，再處理 team 狀態",
            message_id=message_id,
        )
    return iso_utc(moment - timedelta(seconds=DISPATCHED_BACK_SECONDS))


def _record_dispatch(
    ctx: Ctx, team: Team, *, ticket: str, message_id: str, received_at: str
) -> None:
    """Write the dispatch into the team state (design §2.1、§3.4).

    The fields are MyPMO's `pmo/state/README.md` layout, which design §3.4
    names as the authority: `agent`、`team`、`ticket`、`message_id`、
    `received_at`、`dispatched_at`（`received_at` 往前 60 秒）、`updated_at`、
    `closed: false`. `closed` must be `false` because `pmo.teammsg` treats a
    missing one as closed and would never write `ack_at`; a new dispatch clears
    the previous watchdog marks (`ack_at`／`ack_poke_at`／`ack_escalated_at`,
    #352's names) so the watchdog starts over. Unknown fields are kept (Q-9).

    The load and the save happen inside the team's lock, and the state is
    re-read there, so this write cannot overwrite the「收到」`pmo inbox read` is
    writing at the same time (arch review 中-1).
    """
    dispatched_at = _dispatched_at(received_at, message_id)
    with teamstate.state_lock(ctx.state_dir, team.key):
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
                "team": team.key,
                "ticket": ticket,
                "message_id": message_id,
                "received_at": received_at,
                "dispatched_at": dispatched_at,
                "updated_at": iso_utc(ctx.deps.now()),
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
    A letter over angareion's per-message limit is refused before sending
    (`UsageError` → exit 1) with the issue body's length, never truncated.
    """
    ticket = f"{repo}#{number}"
    sent_at = ctx.deps.now()
    letter = build_letter(
        ticket=ticket,
        title=issue.title,
        issue_body=issue.body,
        instructions=instructions,
        channel=team.channel,
        pmo=ctx.config.pmo_identity,
        stamp=iso_utc(sent_at),
        life=team.life,
    )
    if len(letter) > MAX_LETTER_CHARS:
        raise UsageError(
            f"指示信有 {len(letter)} 字元，超過 angareion 一則的上限 {MAX_LETTER_CHARS}；"
            f"其中工單本文 {len(str(issue.body or ''))} 字元（不截斷）。"
            f"請縮短工單或正文再送"
        )
    message_id = ctx.deps.angareion.send(
        channel=team.channel, to=team.pm, body=letter, urgency=DISPATCH_URGENCY
    )
    received_at = _backend_time(ctx, team, message_id, sent_at)
    _record_dispatch(
        ctx, team, ticket=ticket, message_id=message_id, received_at=received_at
    )
    every, unknown = _wake_every(ctx)
    return SendResult(
        message_id=message_id,
        received_at=received_at,
        wake_hint=hint_for(every, unknown=unknown),
    )
