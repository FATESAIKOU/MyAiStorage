"""團隊訊息的 `[msg]` 事件與「收到 ‹工單›」ack 比對測試（#110 T-2）。

移植自 MyPMO tests/dispatch/test_dispatch.py 的 team-message 段（約 1336–1446 行）：
PMO 只留 `inbox read` 一個讀者，團隊訊息全文要印出（不截斷、不壓行），
第一行格式不變，「收到 <ticket>」的比對照舊、前綴碰撞與順帶提及不算 ack。
Watch／sidecar 的人在別張工單，這裡只搬 `inbox read` 的那一半。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from io import StringIO

import pytest

from pmo import teammsg, teamstate
from pmo.config import parse as parse_config
from pmo.runtime import Ctx, Deps

import _pmo_env as E
from _pmo_env import (
    TEAM_CHANNEL,
    TEAM_KEY,
    TEAM_PM,
    pmo_env,
    run_cli,
    seed_team_state,
)

TICKET = "demo#45"
AGENT = TEAM_PM


@pytest.fixture(autouse=True)
def _seat(monkeypatch):
    E.set_seat(monkeypatch)


# --- 對照素材 -------------------------------------------------------------- #

#: 超過 200 字、多行的團隊訊息（舊行為會在 200 字截斷、把多行壓成一行）。
FULL_CONTENT = (
    "第一段：這是一則超過兩百字的團隊回報，用來確認 inbox read 會把內文全文印出來，"
    "而不是像以前那樣只留前兩百字。\n"
    "\n"
    "第二段：PMO 現在只剩這一個讀信者，所以截斷會讓 PMO 看不到後半段，"
    "還得另外補讀一次；補讀時游標已經推過去了，看不到就是看不到。\n"
    "第三段：這一行要原樣出現，包括全形標點、縮排與空白。\n"
    "第四段：再多寫一點，確保壓成一行之後也還是超過兩百字，"
    "這樣改壞驗證（改回 one_line(content, 200)）才會真的轉紅。\n"
    "第五段：收尾。"
)


def _msg(mid, content, **kw):
    """一般 team 訊息的 dict 臉面（與真 angareion read 的 JSON 同欄位名）。"""
    return {
        "id": mid,
        "from": AGENT,
        "channel": TEAM_CHANNEL,
        "urgency": 5,
        "received_at": "2026-10-16T04:00:05Z",
        "content": content,
        **kw,
    }


def _ctx(env) -> Ctx:
    return Ctx(
        deps=Deps(angareion=env.ang),
        config=parse_config(E.config_data()),
        state_dir=env.state_dir,
        out=StringIO(),
        err=StringIO(),
        inp=StringIO(),
    )


def _team_state(env) -> dict:
    return teamstate.load_state(env.state_dir, TEAM_KEY)


# --- 事件全文 -------------------------------------------------------------- #


def test_process_team_messages_prints_full_multiline_content(pmo_env):
    """① 超過 200 字、多行的團隊訊息：事件含完整內文，續行前面兩個空白。"""
    seed_team_state(pmo_env)
    assert len(FULL_CONTENT) > 200, "測試素材本身要超過 200 字才驗得到截斷"

    msg = _msg("01JFULL0000000000000000001", FULL_CONTENT)
    events = teammsg.process_team_messages(_ctx(pmo_env), [msg])

    assert len(events) == 1  # 一則訊息一個事件（多行內文不拆成多個事件）
    lines = events[0].split("\n")
    head = (
        f"[msg] id=01JFULL0000000000000000001 from={AGENT} channel={TEAM_CHANNEL} "
        f"urgency=5 received_at=2026-10-16T04:00:05Z :: 第一段："
    )
    assert lines[0].startswith(head)
    # 第一行之後的每一行都是兩個空白縮排、內容原樣
    assert all(line.startswith("  ") for line in lines[1:])
    # 內文可以原樣拼回來（沒有截斷、沒有壓成一行）
    restored = "\n".join([lines[0].split(" :: ", 1)[1]] + [line[2:] for line in lines[1:]])
    assert restored == FULL_CONTENT
    assert "…" not in events[0]  # 沒有截長符號


def test_msg_event_first_line_format_unchanged(pmo_env):
    """② 第一行格式與改前相同：單行短訊息整行逐字等於舊格式。"""
    seed_team_state(pmo_env)

    msg = _msg("01JSHORT00000000000000001", "進度：測試綠")
    events = teammsg.process_team_messages(_ctx(pmo_env), [msg])

    assert events[0] == (
        f"[msg] id=01JSHORT00000000000000001 from={AGENT} channel={TEAM_CHANNEL} "
        f"urgency=5 received_at=2026-10-16T04:00:05Z :: 進度：測試綠"
    )
    assert "\n" not in events[0]  # 單行訊息不多出續行


def test_process_team_messages_keeps_ack_compare(pmo_env):
    """`[ack]` 事件照舊：內文全文不影響「收到 <ticket>」的比對。"""
    seed_team_state(pmo_env)

    msg = _msg("01JACK0000000000000000001", f"收到 {TICKET}\n第二行補充")
    events = teammsg.process_team_messages(_ctx(pmo_env), [msg])

    assert events[0].startswith(f"[msg] id=01JACK0000000000000000001")
    assert events[1] == f"[ack] {AGENT} 回了「收到 {TICKET}」"
    state = _team_state(pmo_env)
    assert state["ack_at"]
    # `updated_at` 跟 `ack_at` 同一個時刻（MyPMO 的寫法；L-3 的改壞要轉紅）
    assert state["updated_at"] == state["ack_at"]


def test_ack_prefix_collision_does_not_close_watchdog(pmo_env):
    """（審查 B-5）：`demo#23` 不被「收到 demo#230」誤判為已收到。"""
    seed_team_state(pmo_env, ticket="demo#23")

    msg = _msg("01JACKPREFIX00000000000001", "收到 demo#230，立即開工")
    events = teammsg.process_team_messages(_ctx(pmo_env), [msg])

    assert not any(e.startswith("[ack]") for e in events)
    assert not _team_state(pmo_env).get("ack_at")


def test_ack_incidental_mention_does_not_close_watchdog(pmo_env):
    """（審查 B-5）：內文順帶提及工單號（不是起頭的「收到 <ticket>」）不算 ack。"""
    seed_team_state(pmo_env)  # TICKET＝demo#45

    msg = _msg("01JACKMENTION0000000000001", f"已收到 STOP 指示，{TICKET} 的部分我還沒動")
    events = teammsg.process_team_messages(_ctx(pmo_env), [msg])

    assert not any(e.startswith("[ack]") for e in events)
    assert not _team_state(pmo_env).get("ack_at")


def test_inbox_read_one_round_handles_secretary_and_team_message(pmo_env):
    """同一批信：秘書派工照舊入帳發 ack，團隊訊息印全文並寫 `ack_at`（中 3）。"""
    seed_team_state(pmo_env, ticket=TICKET)
    team_msg = _msg("01JMIXED00000000000000001", f"收到 {TICKET}\n第二行補充")

    rc, out, err = run_cli(
        ["inbox", "read"], pmo_env, msgs=[E.secretary_msg(), team_msg]
    )
    assert rc == 0, (out, err)

    assert "收到新工作" in out
    assert "已向秘書發送 ack" in out
    # 秘書的信不是 `[msg]`：唯一一則團隊訊息才印全文
    msg_lines = [line for line in out.splitlines() if line.startswith("[msg]")]
    assert len(msg_lines) == 1
    assert "01JMIXED00000000000000001" in msg_lines[0]
    assert f"[ack] {TEAM_PM} 回了「收到 {TICKET}」" in out
    assert _team_state(pmo_env)["ack_at"]

    # 現行工具的順序：秘書那一輪的事件在前，團隊訊息在後
    lines = out.splitlines()
    assert lines.index(msg_lines[0]) > next(
        i for i, line in enumerate(lines) if line.startswith("[secretary] 收到新工作")
    )


def test_inbox_read_prints_full_team_message(pmo_env):
    """`inbox read`（PMO 的唯一讀者）印出團隊訊息全文。"""
    seed_team_state(pmo_env)
    msg = _msg("01JFULL0000000000000000002", FULL_CONTENT)

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0, (out, err)

    lines = out.splitlines()
    msg_lines = [line for line in lines if line.startswith("[msg]")]
    assert len(msg_lines) == 1  # 多行內文不影響「一則訊息一行 [msg]」
    assert lines[0] == (
        f"[msg] id=01JFULL0000000000000000002 from={TEAM_PM} channel={TEAM_CHANNEL} "
        f"urgency=5 received_at=2026-10-16T04:00:05Z :: 第一段：這是一則超過兩百字的團隊回報，"
        f"用來確認 inbox read 會把內文全文印出來，而不是像以前那樣只留前兩百字。"
    )
    for content_line in FULL_CONTENT.split("\n")[1:]:
        assert f"  {content_line}" in lines  # 續行原樣、兩個空白縮排
    assert "…" not in out
