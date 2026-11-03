"""`pmo inbox rebuild` 的測試（#110 T-3；design §1.2、§3.2）。

驗收（工單 #354）：刪掉帳本 → rebuild → 補發的 ack 沿用重建的 issue／schedule、
不重開工單；team 欄留空並列出；負向：log 讀不到／格式不符時的行為。全部用
假 angareion（`FakeAngareion` 的過濾變種）與暫存 state，不打真的後端。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from io import StringIO

import pytest

from pmo import inbox, ledger, rebuild
from pmo.adapters import Angareion
from pmo.ports import Message, PortError
from pmo.runtime import Deps

import _pmo_env as E
from _pmo_env import (
    DISPATCH_CHANNEL,
    PMO_IDENTITY,
    SECRETARY_IDENTITY,
    ULID_1,
    ULID_2,
    VALID_WORK_BODY,
    PmoEnv,
    item_data,
    pmo_env,
    run_cli,
    seed_item,
)

#: 被叫的那則信的 id（`notified.call` 的 key；假資料，任意字串）。
CALL_MSG_ID = "01JX9A1B2C3D4E5F6G7H8J9KBP"

#: log 記錄的時刻（假資料）。
ACK_TIME = "2026-10-19T10:20:00Z"
REPORT_TIME = "2026-10-19T12:30:00Z"
CALL_ACK_TIME = "2026-10-19T11:00:00Z"

ISSUE = "demo#8"
PR = "demo-repo#12"


# --------------------------------------------------------------------------- #
# 假 angareion：log 會照參數過濾（真 CLI 的合約；T-1 假物件只在記錄參數）
# --------------------------------------------------------------------------- #


class LogFilterAngareion(E.FakeAngareion):
    """`log` 會照過濾參數篩 `logged`：--channel/--from/--to/--thread/--grep/--since。

    篩法照`angareion log --help`（2026-11-03）：thread 以 `in_reply_to` 為準、
    since 是含端的下界。真 backend 的 `--channel` 可重複，重建只用單一頻道。
    """

    def log(self, *, channel=None, sender=None, to=None, thread=None, grep=None, since=None):
        self.log_calls.append(
            {"channel": channel, "sender": sender, "to": to, "thread": thread, "grep": grep, "since": since}
        )
        self._maybe_fail("log")
        got = []
        for m in self.logged:
            if channel is not None and m.channel != channel:
                continue
            if sender is not None and m.sender != sender:
                continue
            if to is not None and m.to != to:
                continue
            if thread is not None and m.in_reply_to != thread:
                continue
            if grep is not None and grep.lower() not in m.body.lower():
                continue
            if since is not None and (m.received_at or "") < since:
                continue
            got.append(m)
        return got


# --------------------------------------------------------------------------- #
# 假資料的組法
# --------------------------------------------------------------------------- #


def letter_msg(
    ulid: str = ULID_1, content: str = VALID_WORK_BODY, received_at: str = "2026-10-19T10:15:30Z"
) -> Message:
    return E.secretary_msg(ulid=ulid, content=content, received_at=received_at)


def pmo_msg(body: str, *, received_at: str, ulid: str = "", other: str = None) -> Message:
    """PMO 自己寄出的信（ack／回報／叫人 ack；`in_reply_to` 是工作 ID 或來信 id）。"""
    return Message(
        message_id=other or f"fake-{ulid or 'x'}",
        channel=DISPATCH_CHANNEL,
        sender=PMO_IDENTITY,
        to=SECRETARY_IDENTITY,
        received_at=received_at,
        body=body,
        urgency=5,
        in_reply_to=other or (ulid or None),
    )


def ack_msg(
    ulid: str = ULID_1, issue: str = ISSUE, schedule: str = "立即開工", received_at: str = ACK_TIME
) -> Message:
    body = inbox.build_ack_body(
        ulid, issue=issue, schedule=schedule, identity=PMO_IDENTITY
    )
    return pmo_msg(body, received_at=received_at, ulid=ulid)


def report_msg(
    ulid: str = ULID_1, status: str = "已完成", issue: str = ISSUE, pr: str = PR,
    received_at: str = REPORT_TIME,
) -> Message:
    body = inbox.build_report_body(
        ulid=ulid, status=status, issue=issue, pr=pr, summary="做完了", next_steps="無"
    )
    return pmo_msg(body, received_at=received_at, ulid=ulid)


def call_ack_msg(ref: str = CALL_MSG_ID, received_at: str = CALL_ACK_TIME) -> Message:
    body = inbox.build_call_ack_body(
        ref, identity=PMO_IDENTITY, dispatch_channel=DISPATCH_CHANNEL
    )
    return Message(
        message_id="fake-call-ack",
        channel="other-channel",  # 叫人不一定在派工頻道
        sender=PMO_IDENTITY,
        to="someone-m1",
        received_at=received_at,
        body=body,
        urgency=5,
        in_reply_to=ref,
    )


def log_env(pmo_env: PmoEnv, logged: list) -> PmoEnv:
    pmo_env.ang = LogFilterAngareion(logged=logged)
    return pmo_env


def read_ledger(env: PmoEnv) -> dict:
    return json.loads(env.inbox_file().read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. 重建基本欄位（刪掉帳本 → rebuild）
# --------------------------------------------------------------------------- #


def test_rebuild_reconstructs_the_letter_fields(pmo_env):
    tmp = log_env(pmo_env, [letter_msg()])
    rc, out, err = run_cli(["inbox", "rebuild"], tmp)
    assert (rc, err) == (0, "")
    assert "[rebuild] 重建 1 筆（新增 1、更新 0）" in out
    assert f"[rebuild] 新增：{ULID_1}" in out
    # §1.2：刪掉帳本重來時 team 一樣重建不出來，要留空並列出（不是只有改到舊項目才列）
    assert f"[rebuild] team：1 筆重建不出來，已留空並列出：{ULID_1}" in out

    state = read_ledger(tmp)
    item = state["items"][ULID_1]
    assert item == {
        "id": ULID_1,
        "title": "修復測試 43 邊界斷言",
        "level": "c",
        "sender": SECRETARY_IDENTITY,
        "channel": DISPATCH_CHANNEL,
        "received_at": "2026-10-19T10:15:30Z",
        "ack_at": None,
        "schedule": None,
        "issue": None,
        "status": "scheduled",
        "reported_at": None,
        "pr": None,
        "team": None,  # §1.2：重建不出來
        "spec": "測試 43 的擁有者斷言要是最後一個動作，\n範圍檢查不能被繞過，請協助補齊測試並開 PR。",
        "notes": "請指派給 team 實作，需通過非作者獨立審查。",
    }
    assert state["rebuilt_at"]  # §3.4 的 rebuilt_at


def test_rebuild_picks_up_the_mechanical_ack_and_the_report(pmo_env):
    tmp = log_env(pmo_env, [letter_msg(), ack_msg(), report_msg()])
    rc, out, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0
    item = read_ledger(tmp)["items"][ULID_1]
    assert item["ack_at"] == ACK_TIME
    assert item["schedule"] == "立即開工"
    assert item["issue"] == ISSUE
    assert item["status"] == "已完成"
    assert item["reported_at"] == REPORT_TIME
    assert item["pr"] == PR


def test_rebuild_uses_the_designed_log_seams(pmo_env):
    """§1.2 的兩個查詢落在埠上：列舉是 --channel --to；ack 是 --from --thread。"""
    tmp = log_env(pmo_env, [letter_msg(), ack_msg(), report_msg()])
    run_cli(["inbox", "rebuild"], tmp)
    calls = tmp.ang.log_calls
    assert calls[0] == {
        "channel": DISPATCH_CHANNEL, "sender": None, "to": PMO_IDENTITY,
        "thread": None, "grep": None, "since": None,
    }
    assert calls[1] == {
        "channel": None, "sender": PMO_IDENTITY, "to": None,
        "thread": None, "grep": rebuild.CALL_ACK_MARK, "since": None,
    }
    assert calls[2] == {
        "channel": None, "sender": PMO_IDENTITY, "to": None,
        "thread": ULID_1, "grep": None, "since": None,
    }
    assert [c.get("thread") for c in tmp.ang.log_calls[2:]] == [ULID_1]


def test_two_works_each_take_their_own_thread(pmo_env):
    ulid2 = ULID_2
    body2 = VALID_WORK_BODY.replace(ULID_1, ulid2)
    logged = [
        letter_msg(ULID_1),
        letter_msg(ulid2, content=body2, received_at="2026-10-19T10:16:00Z"),
        ack_msg(ULID_1, issue="demo#1"),
        ack_msg(ulid2, issue="demo#2"),
    ]
    tmp = log_env(pmo_env, logged)
    rc, _, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0
    items = read_ledger(tmp)["items"]
    assert items[ULID_1]["issue"] == "demo#1"
    assert items[ulid2]["issue"] == "demo#2"


def test_duplicate_letter_keeps_the_earliest_received_at(pmo_env, monkeypatch):
    tmp = log_env(
        pmo_env,
        [letter_msg(received_at="2026-10-19T10:15:30Z"), letter_msg(received_at="2026-10-19T09:00:00Z")],
    )
    rc, out, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0
    assert read_ledger(tmp)["items"][ULID_1]["received_at"] == "2026-10-19T09:00:00Z"


# --------------------------------------------------------------------------- #
# 2. 核心驗收：刪掉帳本 → rebuild → 補發的 ack 沿用重建值、不重開工單
# --------------------------------------------------------------------------- #


def test_resent_ack_reuses_rebuilt_issue_and_schedule_without_reopening(pmo_env):
    tmp = log_env(pmo_env, [letter_msg(), ack_msg(issue=ISSUE, schedule="立即開工")])
    rc, _, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0

    # 同一張派工再出現（游標重來／--since 補讀）：走補發 ack 的路，不是新工作
    tmp.ang.pending.append(letter_msg())
    rc2, out, err = run_cli(["inbox", "read"], tmp)
    assert (rc2, err) == (0, "")
    assert f"[secretary] 重複工作 {ULID_1}，補發 ack" in out
    assert "收到新工作" not in out  # 不重開工單

    (resent,) = tmp.sends()
    assert resent.in_reply_to == ULID_1
    assert f"- 建立工單：{ISSUE}" in resent.body
    assert "- 預估安排：立即開工" in resent.body

    state = read_ledger(tmp)
    assert list(state["items"]) == [ULID_1]  # 沒有新的項目被開出來
    assert state["items"][ULID_1]["issue"] == ISSUE
    assert state["items"][ULID_1]["schedule"] == "立即開工"


# --------------------------------------------------------------------------- #
# 2b. 更新既有項目：只覆寫 log 有值的欄位（審查中 1：別把本機的值洗成 None）
# --------------------------------------------------------------------------- #


def test_existing_item_keeps_the_fields_the_log_cannot_see(pmo_env):
    """thread 查不到 ack／回報時，本機的工單、排程、狀態原樣留著。"""
    from _pmo_env import seed_items  # 局部引進，避免檔頭再列一串

    seed_items(pmo_env, [
        item_data(
            ULID_1, issue=ISSUE, schedule="立即開工", status="已完成",
            ack_at=ACK_TIME, reported_at=REPORT_TIME, pr=PR, team="demo2-m1",
        )
    ])
    # §1.2 的例子：過去沒帶 in_reply_to 的舊 ack 重建不出來——thread 只有派工信
    tmp = log_env(pmo_env, [letter_msg()])
    rc, out, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0
    assert f"[rebuild] 更新：{ULID_1}" in out

    item = read_ledger(tmp)["items"][ULID_1]
    assert (item["issue"], item["schedule"]) == (ISSUE, "立即開工")
    assert (item["ack_at"], item["reported_at"], item["pr"]) == (ACK_TIME, REPORT_TIME, PR)
    assert item["status"] == "已完成"  # 不退回 scheduled（下一輪 read 才不會補發 ack）
    # §1.2：team 仍重建不出來——留空並列出
    assert item["team"] is None
    assert f"[rebuild] team：1 筆重建不出來，已留空並列出：{ULID_1}" in out


def test_existing_item_still_takes_the_values_the_log_does_see(pmo_env):
    """log 有值的欄位照樣覆寫既有值：保留是本機值的後備，不是「一律不動」。"""
    seed_item(
        pmo_env, ULID_1,
        issue="demo#old", schedule="排入待開工佇列", status="已完成",
    )
    tmp = log_env(pmo_env, [letter_msg(), ack_msg(issue=ISSUE, schedule="立即開工")])
    rc, _, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0

    item = read_ledger(tmp)["items"][ULID_1]
    assert (item["issue"], item["schedule"], item["ack_at"]) == (ISSUE, "立即開工", ACK_TIME)
    assert item["status"] == "已完成"  # log 沒有回報：狀態不退回 scheduled


# --------------------------------------------------------------------------- #
# 3. 不可重建欄位（team、notified.poke）
# --------------------------------------------------------------------------- #


def test_team_is_emptied_and_listed_only_for_the_items_rebuilt(pmo_env):
    """team 重建不出來：重建到的項目留空並列出；log 沒到的舊項目原樣留著。"""
    from _pmo_env import seed_items, item_data as _item_data  # 局部引進，避免檔頭再列一串
    seed_items(pmo_env, [
        item_data(ULID_1, team="demo2-m1"),
        item_data(ULID_2, team="demo2-m1", issue="demo#old"),
    ])
    tmp = log_env(pmo_env, [letter_msg(), ack_msg()])
    rc, out, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0
    assert "[rebuild] 新增：無" in out
    assert f"[rebuild] 更新：{ULID_1}" in out

    state = read_ledger(tmp)
    assert state["items"][ULID_1]["team"] is None
    assert f"[rebuild] team：1 筆重建不出來，已留空並列出：{ULID_1}" in out
    old = state["items"][ULID_2]  # log 沒涵蓋到的舊項目不被這次重建動到
    assert old["team"] == "demo2-m1"
    assert old["issue"] == "demo#old"


def test_notified_call_is_rebuilt_and_poke_is_reset(pmo_env):
    seed_item(pmo_env)
    seed_ledger = read_ledger(pmo_env)
    seed_ledger["notified"] = {"call": {"old-in": "2026-10-18T00:00:00Z"}, "poke": {"old-poke": "2026-10-18T00:00:00Z"}}
    ledger.save_state(pmo_env.state_dir, seed_ledger)

    tmp = log_env(pmo_env, [letter_msg(), ack_msg(), call_ack_msg()])
    rc, out, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0

    notified = read_ledger(tmp)["notified"]
    assert notified["call"] == {CALL_MSG_ID: CALL_ACK_TIME}
    assert notified["poke"] == {}  # §1.2：重建不出來
    assert "notified.poke：1 筆未重建" in out
    assert "叫人去重帳重建 1 筆" in out


def test_call_ack_without_in_reply_to_still_falls_back_to_the_title_id(pmo_env):
    dataclasses = pytest.importorskip("dataclasses")
    # 把叫人 ack 的 in_reply_to 摘掉（舊版工具的信）：退回首行 id
    dropped = dataclasses.replace(call_ack_msg(), in_reply_to=None)
    tmp = log_env(pmo_env, [letter_msg(), dropped])
    rc, _, _ = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 0
    notified = read_ledger(tmp)["notified"]
    assert notified["call"] == {CALL_MSG_ID: CALL_ACK_TIME}


# --------------------------------------------------------------------------- #
# 4. dry-run 與 --since
# --------------------------------------------------------------------------- #


def test_dry_run_reports_without_writing(pmo_env):
    tmp = log_env(pmo_env, [letter_msg(), ack_msg()])
    rc, out, _ = run_cli(["inbox", "rebuild", "--dry-run"], tmp)
    assert (rc) == 0
    assert "[rebuild] [dry-run] 只算差異，帳本未寫" in out
    assert not tmp.inbox_file().exists()


def test_since_bounds_the_rebuild(pmo_env):
    """--since 遞給每一次 log；log 沒回的舊項目原樣留著。"""
    old = item_data(ULID_2, team="demo2-m1", received_at="2026-10-18T00:00:00Z")
    ledger.save_state(
        pmo_env.state_dir,
        {"version": 1, "updated_at": None, "items": {ULID_2: old}, "notified": {"call": {}, "poke": {}}},
    )
    tmp = log_env(pmo_env, [letter_msg(), ack_msg()])
    rc, _, _ = run_cli(["inbox", "rebuild", "--since", "2026-10-19T00:00:00Z"], tmp)
    assert rc == 0
    # 每一次 log 都帶了 --since（含列舉、叫人帳、thread 查詢）
    assert all(call["since"] == "2026-10-19T00:00:00Z" for call in tmp.ang.log_calls)

    state = read_ledger(tmp)
    assert ULID_1 in state["items"]  # 新的重建到
    assert state["items"][ULID_2]["team"] == "demo2-m1"  # 舊的原樣留著


def test_since_keeps_the_call_dedupes_outside_the_window(pmo_env):
    """窗口外的叫人 ack 看不到：既有去重紀錄留著，不讓 PMO 對舊叫人再 ack 一次。"""
    ledger.save_state(
        pmo_env.state_dir,
        {
            "version": 1, "updated_at": None, "items": {},
            "notified": {"call": {"old-in": "2026-10-18T00:00:00Z"}, "poke": {}},
        },
    )
    tmp = log_env(pmo_env, [letter_msg(), call_ack_msg()])
    rc, _, _ = run_cli(["inbox", "rebuild", "--since", "2026-10-19T00:00:00Z"], tmp)
    assert rc == 0
    assert read_ledger(tmp)["notified"]["call"] == {
        "old-in": "2026-10-18T00:00:00Z",
        CALL_MSG_ID: CALL_ACK_TIME,
    }


# --------------------------------------------------------------------------- #
# 5. 負向：log 讀不到／輸出不合格式
# --------------------------------------------------------------------------- #


def test_log_failure_is_exit_2_and_leaves_the_ledger_alone(pmo_env):
    tmp = log_env(pmo_env, [])
    tmp.ang.errors["log"] = PortError("後端限流")
    rc, out, err = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 2
    assert "[rebuild] 重建失敗" in err
    assert "後端限流" in err
    assert out == ""
    assert not tmp.inbox_file().exists()


def test_malformed_log_output_is_exit_2_and_leaves_the_ledger_alone(pmo_env, angareion_runs):
    """格式不符：走真的 adapter（只換掉 subprocess），`PortError` 收在指令層。"""
    _, reply = angareion_runs
    reply["stdout"] = "not json"
    seed_item(pmo_env, ULID_2, team="demo2-m1")
    before = pmo_env.inbox_file().read_text(encoding="utf-8")
    rc, out, err = run_cli(
        ["inbox", "rebuild"], pmo_env, deps=Deps(angareion=Angareion())
    )
    assert (rc, out) == (2, "")
    assert "不是 JSON" in err
    assert pmo_env.inbox_file().read_text(encoding="utf-8") == before  # 帳本原樣


def test_a_damaged_ledger_fails_before_the_log(pmo_env):
    tmp = log_env(pmo_env, [letter_msg()])
    tmp.state_dir.mkdir(parents=True, exist_ok=True)
    tmp.inbox_file().write_text("{ not json", encoding="utf-8")
    rc, out, err = run_cli(["inbox", "rebuild"], tmp)
    assert rc == 2
    assert tmp.ang.log_calls == []  # 帳本壞了先擋，連 log 都不打


# --------------------------------------------------------------------------- #
# 6. 純函式：樣板回解的吃與不吃
# --------------------------------------------------------------------------- #


def test_parsers_accept_only_their_own_templates():
    assert rebuild.parse_ack_body(VALID_WORK_BODY) is None
    assert rebuild.parse_report_body(VALID_WORK_BODY) is None
    assert rebuild.parse_call_ack_body(VALID_WORK_BODY) is None

    ack = rebuild.parse_ack_body(
        inbox.build_ack_body(ULID_1, identity=PMO_IDENTITY, issue=ISSUE)
    )
    assert ack is not None and ack["work_id"] == ULID_1


def test_ack_issue_placeholder_maps_to_none():
    body = inbox.build_ack_body(ULID_1, identity=PMO_IDENTITY)  # 無 issue
    parsed = rebuild.parse_ack_body(body)
    assert parsed == {"work_id": ULID_1, "issue": None, "schedule": "排入待開工佇列"}
    with_issue = rebuild.parse_ack_body(
        inbox.build_ack_body(ULID_1, identity=PMO_IDENTITY, issue=ISSUE, schedule="立即開工")
    )
    assert with_issue == {"work_id": ULID_1, "issue": ISSUE, "schedule": "立即開工"}


def test_ack_schedule_outside_the_valid_set_is_dropped():
    body = inbox.build_ack_body(
        ULID_1, identity=PMO_IDENTITY, issue=ISSUE, schedule="立即開工"
    ).replace("立即開工", "馬上做")
    parsed = rebuild.parse_ack_body(body)
    assert parsed["schedule"] is None and parsed["issue"] == ISSUE


def test_report_unknown_status_is_dropped_but_the_rest_stays():
    body = inbox.build_report_body(
        ulid=ULID_1, status="已完成", issue=ISSUE, pr=PR, summary="s", next_steps="n"
    ).replace("已完成", "看起來不錯")
    parsed = rebuild.parse_report_body(body)
    assert parsed["status"] is None
    assert parsed["work_id"] == ULID_1 and parsed["issue"] == ISSUE and parsed["pr"] == PR


def test_report_placeholders_map_to_none():
    body = inbox.build_report_body(
        ulid=ULID_1, status="阻塞", issue=None, pr=None, summary="s", next_steps="n"
    )
    parsed = rebuild.parse_report_body(body)
    assert (parsed["status"], parsed["issue"], parsed["pr"]) == ("阻塞", None, None)


# --------------------------------------------------------------------------- #
# 7. 真接線：`adapters.Angareion.log` 的旗面與不合格式的輸出
# --------------------------------------------------------------------------- #


class _Proc:
    def __init__(self, stdout: str = "[]", returncode: int = 0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


@pytest.fixture
def angareion_runs(monkeypatch):
    """記下 `_run` 收到的 argv，回放指定的 stdout。"""
    calls: list[list[str]] = []
    reply: dict = {"stdout": "[]"}

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return _Proc(stdout=reply["stdout"])

    monkeypatch.setattr("pmo.adapters.subprocess.run", _fake_run)
    return calls, reply


def _one_record() -> dict:
    return {
        "id": "01JXL1", "channel": DISPATCH_CHANNEL, "from": SECRETARY_IDENTITY,
        "to": PMO_IDENTITY, "received_at": "2026-10-19T10:15:30Z",
        "content": "hi", "urgency": 5, "in_reply_to": None,
    }


def test_adapter_log_builds_the_cli_flags(angareion_runs):
    calls, reply = angareion_runs
    reply["stdout"] = json.dumps([_one_record()])
    got = Angareion().log(
        channel=DISPATCH_CHANNEL, sender=PMO_IDENTITY, to=PMO_IDENTITY,
        thread=ULID_1, grep=rebuild.CALL_ACK_MARK, since="2026-10-19T00:00:00Z",
    )
    assert got == [
        Message(
            message_id="01JXL1", channel=DISPATCH_CHANNEL, sender=SECRETARY_IDENTITY,
            to=PMO_IDENTITY, received_at="2026-10-19T10:15:30Z", body="hi",
            urgency=5, in_reply_to=None,
        )
    ]
    (argv,) = calls
    assert argv == [
        "angareion", "log",
        "--channel", DISPATCH_CHANNEL,
        "--from", PMO_IDENTITY,
        "--to", PMO_IDENTITY,
        "--thread", ULID_1,
        "--grep", rebuild.CALL_ACK_MARK,
        "--since", "2026-10-19T00:00:00Z",
        "--json",
    ]


def test_adapter_log_refuses_a_non_json_answer(angareion_runs):
    _, reply = angareion_runs
    reply["stdout"] = "not json"
    with pytest.raises(PortError, match="不是 JSON"):
        Angareion().log(thread=ULID_1)


def test_adapter_log_refuses_a_non_array_answer(angareion_runs):
    _, reply = angareion_runs
    reply["stdout"] = json.dumps({"id": "x"})
    with pytest.raises(PortError, match="不是訊息陣列"):
        Angareion().log()


def test_adapter_log_refuses_a_malformed_record(angareion_runs):
    _, reply = angareion_runs
    reply["stdout"] = json.dumps(["just a string"])
    with pytest.raises(PortError, match="每一筆都必須是物件"):
        Angareion().log()
