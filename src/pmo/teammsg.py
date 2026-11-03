"""Team messages: the `[msg]` events and the「收到」ack matching (T-2).

The inbox read is the PMO's only reader, so every message the secretary channel
did not claim still has to reach the PMO (D-13): each one prints as a `[msg]`
event, and when it is the team PM's「收到 ‹工單›」the team state's `ack_at` is
written (design §1.1, §2.1) — the watchdog then stays quiet (T-5's contract).

The state files are the ones MyPMO writes (`<key>.json`, Q-9); the reading,
validating and atomic writing live in `pmo.teamstate` (arch review 中-1), so
this module only decides **what** to write.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from . import inbox, teamstate
from .runtime import Ctx
from .timeutil import iso_utc

__all__ = [
    "format_msg_event",
    "is_ticket_ack",
    "process_team_messages",
    "active_teams",
]


def active_teams(config) -> list:
    """沒退掉的 team（watch／status 用；retired 的不連、不看）。"""
    return [team for team in config.teams if not team.retired]


def format_msg_event(msg: Mapping) -> str:
    """`[msg]` 事件：第一行是欄位（與改前同一個形狀），內文全文接在後面。

    內文不壓成一行、不截長（`inbox read` 是 PMO 唯一的讀信者，PMO 要能直接看到
    全文，不必另外補讀）。第一行仍是
    `[msg] id=… from=… channel=… urgency=… received_at=… :: <內文第一行>`，
    watch 給 Monitor 用的第一行格式不變；其餘各行原樣、前面加兩個空白縮排。
    """
    text = str(msg.get("content") or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()  # 尾端換行不留一行空白
    head = (
        f"[msg] id={str(msg.get('id') or '')} from={str(msg.get('from') or '')} "
        f"channel={msg.get('channel')} "
        f"urgency={msg.get('urgency')} received_at={msg.get('received_at')} :: {lines[0]}"
    )
    return "\n".join([head] + [f"  {line}" for line in lines[1:]])


def is_ticket_ack(content: str, ticket: str) -> bool:
    """內文是不是「收到 <ticket>」的 ack。

    起頭錨定＋數字邊界：工單號前綴碰撞（`repo#23` 對上 `repo#230`）
    與順帶提及（「已收到 STOP 指示，repo#45 還沒動」）都不算 ack。
    保守向：漏判只多跑一次看門狗 poke，誤判的代價是永久關掉看門狗。
    """
    text = str(content or "").lstrip()
    if not text.startswith(inbox.ACK_PREFIX):
        return False
    return re.search(re.escape(str(ticket)) + r"(?!\d)", text) is not None


def process_team_messages(ctx: Ctx, messages: Iterable, seen: set | None = None) -> list[str]:
    """處理非秘書常規團隊訊息（比對 ack、產生事件字串）。

    Msgs the dispatch round already claimed (the secretary's work messages on
    the dispatch channel) are skipped, not printed twice; everything else —
    team PM chatter, other channels — is the PMO's to read (D-13).
    """
    events: list[str] = []
    for raw in messages:
        msg = raw if isinstance(raw, dict) else inbox.as_dict(raw)
        msg_id = str(msg.get("id") or "")
        sender = str(msg.get("from") or "")
        channel = str(msg.get("channel") or "")
        # 若為秘書工作訊息，已由 inbox 處理完畢，不走一般團隊 ack 比對
        if channel == ctx.config.dispatch_channel and sender == ctx.config.secretary_identity:
            if seen is not None and msg_id:
                seen.add(msg_id)
            continue
        if sender == ctx.config.pmo_identity:
            continue  # 略過自己（identity）送的
        if seen is not None and msg_id and msg_id in seen:
            continue  # 用 id 去重
        if seen is not None and msg_id:
            seen.add(msg_id)
        content = str(msg.get("content") or "")
        events.append(format_msg_event(msg))
        for team in active_teams(ctx.config):
            if team.pm != sender:
                continue
            key = team.key
            # 讀、比對、寫都在同一把 team 鎖內，並在鎖內重讀：`pmo send` 的
            # `_record_dispatch` 也可能正在改同一個檔（arch review 中-1）。
            with teamstate.state_lock(ctx.state_dir, key):
                st = teamstate.load_state(ctx.state_dir, key)
                ticket = str(st.get("ticket") or "")
                if st.get("closed", True) or not ticket or st.get("ack_at"):
                    continue
                if is_ticket_ack(content, ticket):
                    st["ack_at"] = iso_utc(ctx.deps.now())
                    st["updated_at"] = st["ack_at"]
                    teamstate.save_state(ctx.state_dir, key, st)
                    events.append(f"[ack] {team.pm} 回了「收到 {ticket}」")
    return events
