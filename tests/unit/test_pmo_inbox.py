"""收件匣核心測試（#110 T-2；移植自 MyPMO tests/dispatch/test_inbox.py）。

語意原樣搬：合規／不合規解析、read 收信與補發 ack、身分守門、list 只讀、
手動 ack／report、state 檔損毀不推游標、去重帳上限與消毒。改動過的斷言
（結束碼照 design §3.2、例外類別對應）統一列在工作目錄的實作報告裡。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from io import StringIO

import pytest

from pmo import cli, inbox, ledger, teamstate
from pmo.config import parse as parse_config
from pmo.errors import IdentityMismatch, SchemaError, UsageError
from pmo.ports import PortError
from pmo.runtime import Ctx, Deps, assert_pmo_identity

import _pmo_env as E
from _pmo_env import (
    DISPATCH_CHANNEL,
    PMO_IDENTITY,
    SECRETARY_IDENTITY,
    OTHER_IDENTITY_WRONG,
    ULID_1,
    ULID_2,
    VALID_WORK_BODY,
    PmoEnv,
    config_data,
    pmo_env,
    run_cli,
    seed_item,
    seed_team_state,
    set_seat,
)


@pytest.fixture(autouse=True)
def _seat(monkeypatch):
    set_seat(monkeypatch)


def _cfg():
    return parse_config(config_data())


def _ctx(env: PmoEnv) -> Ctx:
    return Ctx(
        deps=Deps(angareion=env.ang),
        config=_cfg(),
        state_dir=env.state_dir,
        out=StringIO(),
        err=StringIO(),
        inp=StringIO(),
    )


# --------------------------------------------------------------------------- #
# 1. 樣板解析測試（合規／不合規／層級缺漏）
# --------------------------------------------------------------------------- #


def test_parse_work_message_compliant():
    parsed = inbox.parse_work_message(E.secretary_msg(), _cfg())
    assert parsed is not None
    assert parsed["valid"] is True
    assert parsed["id"] == ULID_1
    assert parsed["title"] == "修復測試 43 邊界斷言"
    assert parsed["level"] == "c"
    assert "測試 43" in parsed["spec"]
    assert "非作者獨立審查" in parsed["notes"]


def test_parse_work_message_missing_level_defaults_to_b():
    body_no_level = f"""# 工作：快速驗證任務

- 工作 ID：{ULID_1}
- 來源：手機端秘書

## 需求說明
請速檢查。
"""
    parsed = inbox.parse_work_message(E.secretary_msg(content=body_no_level), _cfg())
    assert parsed is not None
    assert parsed["valid"] is True
    assert parsed["level"] == "b"  # 依審查收斂：缺漏層級一律視同 b 級


def test_parse_work_message_non_compliant_variations():
    cfg = _cfg()
    # 非秘書頻道或非秘書發送 -> None
    assert inbox.parse_work_message(E.secretary_msg(channel="other-channel"), cfg) is None
    assert inbox.parse_work_message(E.secretary_msg(sender="other-sender"), cfg) is None

    # 標題不符
    bad_title_body = f"- 工作 ID：{ULID_1}\n## 需求說明\n測試"
    parsed = inbox.parse_work_message(E.secretary_msg(content=bad_title_body), cfg)
    assert parsed is not None and not parsed["valid"]
    assert "標題不符" in parsed["reason"]

    # 缺少內文工作 ID
    no_id_body = "# 工作：測試工作\n## 需求說明\n測試"
    parsed = inbox.parse_work_message(E.secretary_msg(content=no_id_body), cfg)
    assert parsed is not None and not parsed["valid"]
    assert "缺少合法的" in parsed["reason"]

    # front matter id 與內文工作 ID 不一致（中 2 規範）
    mismatch_body = f"""# 工作：測試工作

- 工作 ID：{ULID_2}
## 需求說明
測試
"""
    parsed = inbox.parse_work_message(E.secretary_msg(ulid=ULID_1, content=mismatch_body), cfg)
    assert parsed is not None and not parsed["valid"]
    assert "不一致" in parsed["reason"]

    # 非法 ULID
    bad_ulid_body = """# 工作：測試工作

- 工作 ID：NOT-A-VALID-ULID-12345
## 需求說明
測試
"""
    parsed = inbox.parse_work_message(
        E.secretary_msg(ulid="NOT-A-VALID-ULID-12345", content=bad_ulid_body), cfg
    )
    assert parsed is not None and not parsed["valid"]
    assert "缺少合法的" in parsed["reason"]


def test_parse_work_message_requires_to_pmo():
    """`to` 沒點名 PMO（含廣播件）不當工作；點名了才解析。"""
    cfg = _cfg()
    assert inbox.parse_work_message(E.secretary_msg(), cfg)["valid"] is True

    broadcast = inbox.as_dict(E.secretary_msg())
    broadcast.pop("to")
    assert inbox.parse_work_message(broadcast, cfg) is None

    other = inbox.as_dict(E.secretary_msg())
    other["to"] = "someone-else-m1"
    assert inbox.parse_work_message(other, cfg) is None


# --------------------------------------------------------------------------- #
# 2. inbox read 收取新工作與寫入狀態
# --------------------------------------------------------------------------- #


def test_inbox_read_new_work(pmo_env):
    msg = E.secretary_msg()
    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert (rc, err) == (0, "")
    assert f"收到新工作 {ULID_1}" in out
    assert f"已向秘書發送 ack：收到工作 {ULID_1}" in out

    # 驗證 secretary-inbox.json 已記錄
    inbox_file = pmo_env.inbox_file()
    assert inbox_file.exists()
    st = json.loads(inbox_file.read_text(encoding="utf-8"))
    assert ULID_1 in st["items"]
    item = st["items"][ULID_1]
    assert item["title"] == "修復測試 43 邊界斷言"
    assert item["level"] == "c"
    assert item["status"] == "scheduled"
    assert item["ack_at"] is not None

    # 驗證假 angareion 紀錄：送出的 ack 帶正確參數與樣板（CLI 旗面隨實作換，語意照驗）
    (send_call,) = pmo_env.sends()
    assert send_call.channel == DISPATCH_CHANNEL
    assert send_call.to == SECRETARY_IDENTITY
    assert send_call.in_reply_to == ULID_1

    ack_body = send_call.body
    assert f"# 收到工作：{ULID_1}" in ack_body
    assert "- 狀態：已收件排程中" in ack_body
    assert f"- 處理者：{PMO_IDENTITY}" in ack_body
    assert "- 建立工單：評估排程中" in ack_body
    assert "- 預估安排：排入待開工佇列" in ack_body


def test_inbox_read_when_nothing_arrived(pmo_env):
    rc, out, err = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert "[inbox] 沒有新信件" in out


def test_inbox_read_backend_failure_partial_warn_exits_0(pmo_env):
    """angareion read 起不來（後端／網路）：design §3.2 的 0＝「部分失敗、已印 [warn]」。"""
    from pmo.fakes import FakeAngareion

    pmo_env.ang = FakeAngareion([E.secretary_msg()])
    pmo_env.ang.errors["read"] = PortError("backend down")
    rc, out, err = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert "[warn] angareion read：" in err
    assert "backend down" in err
    # 該輪什麼都沒做（推進不了游標，也沒有半套項目）
    assert pmo_env.sends() == []
    assert ledger.load_state(pmo_env.state_dir)["items"] == {}


def test_inbox_read_usage_error_exits_1(pmo_env):
    """design §3.2：`inbox read` 的結束碼同 tick，參數錯是 1（不是 read 的 0）。"""
    rc, out, err = run_cli(["inbox", "read", "--no-such-flag"], pmo_env)
    assert rc == 1
    assert "用法錯誤" in err
    assert pmo_env.ang.read_calls == []  # 參數錯時連讀都沒讀


def test_cli_missing_settings_env_exits_2(pmo_env):
    """design §3.4：兩個環境變數沒設就拒絕執行（exit 2），不猜預設路徑。"""
    pmo_env.monkeypatch.delenv("PMO_SKILL_CONFIG", raising=False)
    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 2
    assert "PMO_SKILL_CONFIG" in err


def test_inbox_read_writes_state_before_sending_ack(pmo_env):
    """低 8：ack 送不出去時工作已先入帳（保留去重紀錄，防重複開單）。"""
    from pmo.fakes import FakeAngareion

    pmo_env.ang = FakeAngareion([E.secretary_msg()])
    pmo_env.ang.errors["send"] = PortError("network failure")

    rc, out, err = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert f"收到新工作 {ULID_1}" in out
    assert "發送 ack 失敗" in out

    st = ledger.load_state(pmo_env.state_dir)
    assert ULID_1 in st["items"]
    assert st["items"][ULID_1]["ack_at"] is None


# --------------------------------------------------------------------------- #
# 3. 重複工作（DUP）：只補發 ack，不重開工單
# --------------------------------------------------------------------------- #


def test_inbox_read_duplicate_work(pmo_env):
    # 先預置已收件的狀態（已指派工單 demo#230）
    seed_item(
        pmo_env,
        title="修復測試 43 邊界斷言",
        received_at="2026-10-19T10:15:30Z",
        ack_at="2026-10-19T10:15:35Z",
        issue="demo#230",
        team="demo-w1",
        spec="...",
        notes="...",
    )

    # 再次讀取相同信件
    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[E.secretary_msg()])
    assert rc == 0
    assert f"重複工作 {ULID_1}，補發 ack" in out

    # 驗證狀態檔未被洗掉（保留原工單號）
    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["issue"] == "demo#230"

    # 驗證補發之 ack 帶有原工單號
    (send,) = pmo_env.sends()
    assert "- 建立工單：demo#230" in send.body


# --------------------------------------------------------------------------- #
# 4. 收到不合規訊息不中斷流程、不送 ack
# --------------------------------------------------------------------------- #


def test_inbox_read_non_compliant_ignores_and_does_not_send_ack(pmo_env):
    bad_msg = E.secretary_msg(content="# 工作：樣板壞掉\n\n- 建議層級：c\n")
    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[bad_msg])
    assert rc == 0
    assert "[warn] 收到不合規秘書訊息" in out
    assert "[inbox:unknown_format]" in err

    # 驗證沒有發送任何 ack
    assert pmo_env.sends() == []

    # 收件匣中無任何有效工作
    st = ledger.load_state(pmo_env.state_dir)
    assert len(st["items"]) == 0


# --------------------------------------------------------------------------- #
# 5. 身分不符拒送（design §3.2：不符 → exit 2）
# --------------------------------------------------------------------------- #


def test_identity_mismatch_refuses_to_send(pmo_env, monkeypatch):
    wrong = OTHER_IDENTITY_WRONG  # 另一個合法 AI 名（但非 PMO 本席）

    # inbox read 應拒送並回 exit 2（design §3.2「送信前的身分守門」）
    rc, out, err = run_cli(["inbox", "read"], pmo_env, identity=wrong)
    assert rc == 2
    assert "身分不符拒送" in err
    assert wrong in err

    # inbox ack 應拒送並回 exit 2
    rc, out, err = run_cli(["inbox", "ack", ULID_1], pmo_env, identity=wrong)
    assert rc == 2
    assert "身分不符拒送" in err

    # inbox report 應拒送並回 exit 2
    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "已完成"], pmo_env, identity=wrong
    )
    assert rc == 2
    assert "身分不符拒送" in err

    # 決定題送題也守門（read 會送 ack，所以同樣守門；列入報告的待確認）
    rc, out, err = run_cli(
        ["inbox", "decision-send", "--body", "-"], pmo_env, stdin_text="not-json", identity=wrong
    )
    assert rc == 2
    assert "身分不符拒送" in err

    # 直接呼叫函式驗證拋出 IdentityMismatch
    monkeypatch.setenv("ANGAREION_IDENTITY", wrong)
    with pytest.raises(IdentityMismatch):
        assert_pmo_identity(_cfg())


# --------------------------------------------------------------------------- #
# 6. inbox list 唯讀查詢（不推游標、不送 ack）
# --------------------------------------------------------------------------- #


def test_inbox_list_read_only(pmo_env):
    seed_item(
        pmo_env,
        title="修復測試 43 邊界斷言",
        received_at="2026-10-19T10:15:30Z",
        ack_at="2026-10-19T10:15:35Z",
        issue="demo#230",
        team="demo-w1",
    )

    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 0
    assert "秘書工作共 1 筆" in out
    assert ULID_1 in out
    assert "修復測試 43" in out
    assert "- [NEW]" in out
    assert "狀態: 已收件排程中" in out

    # 驗證 angareion 完全沒有被呼叫（不推游標、不發信）
    assert pmo_env.sends() == []
    assert pmo_env.ang.read_calls == []


def test_inbox_list_json_is_an_array_sorted_by_received_at(pmo_env):
    """`--json` 是**陣列**（每筆是項目本身，本來就帶 `id`），依 `received_at`
    由舊到新，和文字版同一個順序；不含 `notified`（那是內部去重帳）。"""
    E.seed_items(
        pmo_env,
        [
            E.item_data(ULID_2, issue="demo#231", received_at="2026-10-20T09:00:00Z", title="第二筆"),
            E.item_data(ULID_1, issue="demo#230", received_at="2026-10-19T10:15:30Z"),
        ],
    )

    rc, out, err = run_cli(["inbox", "list", "--json"], pmo_env)
    assert (rc, err) == (0, "")
    data = json.loads(out)
    assert isinstance(data, list)
    assert [item["id"] for item in data] == [ULID_1, ULID_2]  # 依 received_at，不是 key
    assert data[0]["issue"] == "demo#230"
    assert "notified" not in data[0]

    # 文字版同一個順序
    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert out.index(ULID_1) < out.index(ULID_2)


def test_inbox_list_sorts_by_received_at_not_by_key(pmo_env):
    """兩筆的 id 順序與 received_at 相反：順序要看 received_at（L-3）。"""
    E.seed_items(
        pmo_env,
        [
            E.item_data(ULID_2, received_at="2026-10-19T09:00:00Z", title="先到"),
            E.item_data(ULID_1, received_at="2026-10-19T11:00:00Z", title="後到"),
        ],
    )

    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 0
    assert out.index(ULID_2) < out.index(ULID_1)


def test_inbox_list_empty(pmo_env):
    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 0
    assert "[inbox] 收件匣是空的（0 筆工作）" in out


#: MyPMO's ledger shape (the file both sides share during the transition, Q-9):
#: `notified` only appears once a叫人 or急件 has been seen, so a real file can
#: be missing it — or carry only one of its two buckets.
LEGACY_ITEM = {
    "id": ULID_1,
    "title": "修復測試 43 邊界斷言",
    "level": "c",
    "sender": SECRETARY_IDENTITY,
    "received_at": "2026-10-19T10:15:30Z",
    "ack_at": "2026-10-19T10:15:35Z",
    "status": "scheduled",
    "schedule": "排入待開工佇列",
    "issue": "demo#230",
    "team": "demo-w1",
    "reported_at": None,
    "spec": "...",
    "notes": "...",
}


def _write_legacy_ledger(pmo_env, ledger: dict) -> None:
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")


def test_inbox_list_reads_legacy_ledger_without_notified(pmo_env):
    """MyPMO 的舊帳本沒有 `notified`：是「還沒記過去重」不是壞檔，照常讀。"""
    _write_legacy_ledger(
        pmo_env,
        {"version": 1, "updated_at": "2026-10-19T10:16:00Z", "items": {ULID_1: LEGACY_ITEM}},
    )

    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert (rc, err) == (0, "")
    assert "秘書工作共 1 筆" in out
    assert "demo#230" in out

    # 寫回時補上兩本去重帳（另一邊之後也讀得到），工作內容原樣
    rc, out, err = run_cli(["inbox", "ack", ULID_1], pmo_env)
    assert (rc, err) == (0, "")
    st = ledger.load_state(pmo_env.state_dir)
    assert st["notified"] == {"call": {}, "poke": {}}
    assert st["items"][ULID_1]["issue"] == "demo#230"


def test_inbox_read_reads_legacy_notified_with_one_bucket(pmo_env):
    """MyPMO 只寫它碰過的那一本（`_notified_bucket`）：缺 `poke` 也照常去重。"""
    call_id = E.ULID_3
    _write_legacy_ledger(
        pmo_env,
        {
            "version": 1,
            "updated_at": "2026-10-19T10:16:00Z",
            "items": {},
            "notified": {"call": {call_id: "2026-10-19T10:15:00Z"}},
        },
    )
    msg = inbox.as_dict(E.secretary_msg(ulid=call_id, channel=E.OTHER_CHANNEL, content="進度如何？"))
    msg["to"] = PMO_IDENTITY

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert (rc, err) == (0, "")
    assert f"叫人 id={call_id} 已 ack 過" in out
    assert pmo_env.sends() == []


# --------------------------------------------------------------------------- #
# 7. inbox ack 手動維運補發指令
# --------------------------------------------------------------------------- #


def test_inbox_ack_manual_send(pmo_env):
    seed_item(pmo_env, title="測試手動補發", ack_at=None, issue=None, team=None)

    rc, out, err = run_cli(
        ["inbox", "ack", ULID_1, "--issue", "demo#230", "--schedule", "立即開工"], pmo_env
    )
    assert rc == 0
    assert f"已發送 ack：收到工作 {ULID_1}" in out

    # 檢查發出的 ack 內容
    (send,) = pmo_env.sends()
    assert "- 建立工單：demo#230" in send.body
    assert "- 預估安排：立即開工" in send.body

    # 檢查 state 已更新
    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["ack_at"] is not None
    assert st["items"][ULID_1]["issue"] == "demo#230"


def test_inbox_ack_preserves_existing_issue_and_schedule(pmo_env):
    seed_item(
        pmo_env,
        title="既有工單安排測試",
        schedule="立即開工",
        issue="demo#230",
        team="demo-w1",
    )

    # 手動 ack 不帶 --issue 與 --schedule，應沿用既有值（低 6）
    rc, out, err = run_cli(["inbox", "ack", ULID_1], pmo_env)
    assert rc == 0
    assert f"已發送 ack：收到工作 {ULID_1}" in out

    (send,) = pmo_env.sends()
    assert "- 建立工單：demo#230" in send.body
    assert "- 預估安排：立即開工" in send.body

    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["ack_at"] is not None
    assert st["items"][ULID_1]["issue"] == "demo#230"
    assert st["items"][ULID_1]["schedule"] == "立即開工"


def test_inbox_ack_unknown_work_id(pmo_env):
    rc, out, err = run_cli(["inbox", "ack", ULID_1], pmo_env)
    assert rc == 1  # design §3.2：查無工作 ID → 1
    assert f"收件匣中找不到工作 ID：{ULID_1}" in err


def test_inbox_ack_bad_ulid(pmo_env):
    rc, out, err = run_cli(["inbox", "ack", "not-a-ulid"], pmo_env)
    assert rc == 1
    assert "不是合法的 26 碼 ULID" in err


def test_inbox_ack_send_failure_reports_and_writes_nothing(pmo_env):
    seed_item(pmo_env, issue="demo#230")
    pmo_env.ang.errors["send"] = PortError("network failure")

    rc, out, err = run_cli(["inbox", "ack", ULID_1], pmo_env)
    assert rc == 2  # design §3.2：送不出去 → 2
    assert "發送 ack 失敗" in err
    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["ack_at"] is None


# --------------------------------------------------------------------------- #
# 8. inbox report 工作回報樣板與狀態限制
# --------------------------------------------------------------------------- #


def test_inbox_report_success(pmo_env):
    seed_item(pmo_env, title="修復測試 43", issue="demo#230", team="demo-w1")

    report_text = """## 執行結果摘要
測試 43 斷言修正完成，非作者審查通過，PR 已代為 merge。

## 待決或後續動作
無，此項已結案。
"""
    rc, out, err = run_cli(
        [
            "inbox",
            "report",
            ULID_1,
            "--status",
            "已完成",
            "--pr",
            "https://example.org/demo-repo/pull/231",
            "--body",
            "-",
        ],
        pmo_env,
        stdin_text=report_text,
    )
    assert rc == 0
    assert f"已向秘書發送工作回報：{ULID_1}（已完成）" in out

    # 檢查發送的 report 樣板（嚴格遵循秘書 §5）
    (send,) = pmo_env.sends()
    body = send.body
    assert f"# 工作回報：{ULID_1}" in body
    assert "- 狀態：已完成" in body
    assert "- 相關工單：demo#230" in body
    assert "- 相關 PR：https://example.org/demo-repo/pull/231" in body
    assert "## 執行結果摘要" in body
    assert "測試 43 斷言修正完成" in body
    assert "## 待決或後續動作" in body
    assert "無，此項已結案。" in body
    assert send.in_reply_to == ULID_1

    # 檢查 state 檔已更新 reported_at 與 status
    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["status"] == "已完成"
    assert st["items"][ULID_1]["reported_at"] is not None
    assert st["items"][ULID_1]["pr"] == "https://example.org/demo-repo/pull/231"


def test_inbox_report_rejects_unsupported_status(pmo_env):
    seed_item(pmo_env)

    # design §3.2：不支援的狀態是輸入問題（查無此用法）→ exit 1
    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "未知狀態", "--body", "進度更新"], pmo_env
    )
    assert rc == 1
    assert "不支援的工作回報狀態" in err

    # 直接呼叫函式驗證拋出 UsageError
    set_seat(pmo_env.monkeypatch)
    ctx = Ctx(
        deps=Deps(angareion=pmo_env.ang),
        config=_cfg(),
        state_dir=pmo_env.state_dir,
        out=StringIO(),
        err=StringIO(),
        inp=StringIO(),
    )
    with pytest.raises(UsageError) as exc_info2:
        inbox.send_report(ctx, ULID_1, status="未知狀態")
    assert "不支援的工作回報狀態" in str(exc_info2.value)


def test_inbox_report_supports_in_progress_and_verification_status(pmo_env):
    seed_item(pmo_env, issue="demo#230", team="demo-w1")

    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "進行中", "--body", "開發中"], pmo_env
    )
    assert rc == 0
    assert f"已向秘書發送工作回報：{ULID_1}（進行中）" in out

    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["status"] == "進行中"

    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "驗收中", "--body", "審查中"], pmo_env
    )
    assert rc == 0
    assert f"已向秘書發送工作回報：{ULID_1}（驗收中）" in out

    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"][ULID_1]["status"] == "驗收中"


def test_inbox_report_unknown_work_id(pmo_env):
    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "已完成", "--body", "x"], pmo_env
    )
    assert rc == 1  # design §3.2：查無工作 ID → 1
    assert f"收件匣中找不到工作 ID：{ULID_1}" in err


def test_inbox_report_send_failure(pmo_env):
    seed_item(pmo_env)
    pmo_env.ang.errors["send"] = PortError("network failure")
    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "已完成", "--body", "x"], pmo_env
    )
    assert rc == 2  # design §3.2：送不出去 → 2
    assert "發送工作回報失敗" in err


# --------------------------------------------------------------------------- #
# 9. state 檔損毀時的異常處置（design §3.2：狀態檔壞 → exit 2、不推游標）
# --------------------------------------------------------------------------- #


def test_inbox_corrupted_state_file(pmo_env):
    pmo_env.inbox_file().parent.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text("NOT_VALID_JSON{", encoding="utf-8")

    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 2
    assert "not valid JSON" in err


def test_inbox_read_damaged_state_does_not_advance_cursor(pmo_env):
    pmo_env.inbox_file().parent.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text("NOT_VALID_JSON{", encoding="utf-8")

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[E.secretary_msg()])
    assert rc == 2
    assert "not valid JSON" in err

    # 中 2：確認 angareion read 推進游標的動作完全沒有被執行
    assert pmo_env.ang.read_calls == []


def test_inbox_read_damaged_notified_state_does_not_advance_cursor(pmo_env):
    """去重帳壞掉也要在推進游標前擋下，訊息不能因為 state 錯誤而丟失。"""
    pmo_env.inbox_file().parent.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text('{"version": 1, "items": {}, "notified": []}', encoding="utf-8")

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[E.secretary_msg()])
    assert rc == 2
    assert "the field 'notified' must be an object" in err

    assert pmo_env.ang.read_calls == []


def test_inbox_state_items_shape_invalid_raises_config_error(pmo_env):
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text('{"version": 1, "items": []}', encoding="utf-8")

    with pytest.raises(SchemaError) as exc_info:
        ledger.load_state(pmo_env.state_dir)
    assert "the field 'items' must be an object" in str(exc_info.value)


def test_inbox_state_notified_shape_invalid_raises_config_error(pmo_env):
    """去重帳壞掉時也要在推進游標前擋下，不能靜默重設（否則會重複 ack）。"""
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text('{"version": 1, "items": {}, "notified": []}', encoding="utf-8")

    with pytest.raises(SchemaError) as exc_info:
        ledger.load_state(pmo_env.state_dir)
    assert "the field 'notified' must be an object" in str(exc_info.value)

    pmo_env.inbox_file().write_text(
        '{"version": 1, "items": {}, "notified": {"call": []}}', encoding="utf-8"
    )
    with pytest.raises(SchemaError) as exc_info2:
        ledger.load_state(pmo_env.state_dir)
    assert "the field 'call' must be an object" in str(exc_info2.value)


def test_inbox_secretary_stage_error_lists_ids_and_sends_nothing(pmo_env, monkeypatch):
    """秘書階段出錯時游標已前進、這批不會重送：事件要講明、列出 id、指引
    `angareion read --since` 補讀；沒有 ack 發出去。"""
    msgs = [
        E.secretary_msg(),
        E.secretary_msg(ulid=ULID_2, content=VALID_WORK_BODY.replace(ULID_1, ULID_2)),
    ]
    real_load = ledger.load_state
    calls = {"n": 0}

    def flaky_load(state_dir):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第一次是預檢，之後是 process_messages 鎖內那次
            raise SchemaError("模擬 process 階段的 state 損毀")
        return real_load(state_dir)

    monkeypatch.setattr(ledger, "load_state", flaky_load)

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=msgs)
    assert rc == 0, (out, err)
    assert "秘書階段處理來信時出錯" in out
    assert "可能只處理了一部分" in out
    assert ULID_1 in out
    assert ULID_2 in out
    assert "angareion read --since" in out
    # 這一輪沒有發 ack（處理失敗），也不會寫入半套項目
    assert pmo_env.sends() == []


def test_inbox_secretary_stage_failure_still_runs_the_team_stage(pmo_env, monkeypatch):
    """#362：秘書階段失敗後團隊訊息階段仍要跑完，事件留下、失敗行只列秘書階段的信。"""
    seed_team_state(pmo_env, ticket="demo#45")
    team_msg = {
        "id": "01JMIXED00000000000000002",
        "from": E.TEAM_PM,
        "channel": E.TEAM_CHANNEL,
        "urgency": 5,
        "received_at": "2026-10-19T10:16:00Z",
        "content": "收到 demo#45",
    }
    real_load = ledger.load_state
    calls = {"n": 0}

    def flaky_load(state_dir):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第一次是預檢，之後是 process_messages 鎖內那次
            raise SchemaError("模擬 process 階段的 state 損毀")
        return real_load(state_dir)

    monkeypatch.setattr(ledger, "load_state", flaky_load)

    rc, out, err = run_cli(
        ["inbox", "read"], pmo_env, msgs=[E.secretary_msg(), team_msg]
    )
    assert rc == 0, (out, err)

    # 失敗行只列秘書階段那一則（團隊階段沒有失敗）
    handoff = [line for line in out.splitlines() if line.startswith("[要本人處理]")]
    assert len(handoff) == 1
    assert "秘書階段處理來信時出錯" in handoff[0]
    assert ULID_1 in handoff[0]
    assert team_msg["id"] not in handoff[0]

    # 秘書階段失敗不影響團隊訊息階段：`[msg]` 與「收到」ack 都照樣處理
    assert team_msg["id"] in out
    assert "[msg]" in out
    assert "回了「收到 demo#45」" in out
    assert teamstate.load_state(pmo_env.state_dir, E.TEAM_KEY)["ack_at"] is not None

    # 秘書階段沒有送出任何信
    assert pmo_env.sends() == []


def test_inbox_team_stage_error_keeps_the_secretary_events(pmo_env, monkeypatch):
    """M-1：團隊訊息階段出錯時，秘書階段**已完成**的動作與事件要留下來，
    錯誤訊息只列團隊階段未處理的那一則（不能把已 ack 的信說成「未處理」）。"""
    seed_team_state(pmo_env, ticket="demo#45")
    team_msg = {
        "id": "01JMIXED00000000000000001",
        "from": E.TEAM_PM,
        "channel": E.TEAM_CHANNEL,
        "urgency": 5,
        "received_at": "2026-10-19T10:16:00Z",
        "content": "收到 demo#45\n第二行補充",
    }
    real_save = teamstate.save_state

    def boom(state_dir, key, data):
        raise SchemaError("team state 寫不起來")

    monkeypatch.setattr(teamstate, "save_state", boom)

    rc, out, err = run_cli(
        ["inbox", "read"], pmo_env, msgs=[E.secretary_msg(), team_msg]
    )
    assert rc == 0, (out, err)

    # 秘書階段的事件照印（ack 已經發出去了，不能消失）
    assert f"收到新工作 {ULID_1}" in out
    assert f"已向秘書發送 ack：收到工作 {ULID_1}" in out
    assert len(pmo_env.sends()) == 1

    # 錯誤訊息只列團隊階段那一則，不能把已處理的派工信算進去
    handoff = [line for line in out.splitlines() if line.startswith("[要本人處理]")]
    assert len(handoff) == 1
    assert "團隊訊息階段處理來信時出錯" in handoff[0]
    assert "01JMIXED00000000000000001" in handoff[0]
    assert ULID_1 not in handoff[0]
    assert "未處理" in handoff[0]


def test_inbox_read_damaged_team_state_does_not_advance_cursor(pmo_env):
    # secretary-inbox.json 正常（不存在即可），但 team state 損毀
    pmo_env.team_file().parent.mkdir(parents=True, exist_ok=True)
    pmo_env.team_file().write_text("NOT_VALID_TEAM_JSON{", encoding="utf-8")

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[E.secretary_msg()])
    assert rc == 2
    assert "not valid JSON" in err
    assert f"{E.TEAM_KEY}.json" in err

    # 新問題 2：預檢擋下，angareion read 完全未被執行，游標不推進
    assert pmo_env.ang.read_calls == []


# --------------------------------------------------------------------------- #
# 9b. 群請求：獨立一類、不回 ack、不消耗（M-2／PM 裁定；完整交接是 #358）
# --------------------------------------------------------------------------- #


def _group_msg(content: str, **extra):
    """秘書→PMO、派工頻道的一則群請求（首行是請求樣板）。"""
    return inbox.as_dict(
        E.secretary_msg(channel=DISPATCH_CHANNEL, content=content, **extra)
    )


CREATE_BODY = "# 建群請求：grp-demo\n\n- 工作 ID：{ulid}\n- 群名：grp-demo\n- 成員：demo-pm-mi\n"


def test_group_request_prints_handoff_without_ack(pmo_env):
    """M-2：群請求不回機械 ack、不寫進帳本，只印一行 `[group-handoff] ‹id›`。"""
    body = CREATE_BODY.format(ulid=ULID_1)
    msg = inbox.as_dict(E.secretary_msg(content=body))

    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0, (out, err)
    assert f"[group-handoff] {ULID_1}" in out
    # 不是叫人：沒有叫人 ack、也沒有「不合規」或工作
    assert "叫人" not in out
    assert "不合規" not in out
    assert "收到新工作" not in out
    assert pmo_env.sends() == []
    st = ledger.load_state(pmo_env.state_dir)
    assert st["items"] == {}
    assert st["notified"]["call"] == {}


def test_archive_group_request_is_handed_off_too(pmo_env):
    body = f"# 封存請求：grp-demo\n\n- 工作 ID：{ULID_1}\n- 封存 channel：grp-demo\n"
    rc, out, _ = run_cli(
        ["inbox", "read"], pmo_env, msgs=[inbox.as_dict(E.secretary_msg(content=body))]
    )
    assert rc == 0
    assert f"[group-handoff] {ULID_1}" in out
    assert pmo_env.sends() == []


def test_group_request_bracket_spelling_is_handed_off(pmo_env):
    """工單用 `[create]`／`[archive]` 指這兩類，也照樣分流。"""
    body = f"[create]\n\n- 工作 ID：{ULID_1}\n"
    rc, out, _ = run_cli(
        ["inbox", "read"], pmo_env, msgs=[inbox.as_dict(E.secretary_msg(content=body))]
    )
    assert rc == 0
    assert f"[group-handoff] {ULID_1}" in out
    assert pmo_env.sends() == []


def test_group_request_from_other_channel_stays_a_call(pmo_env):
    """群請求樣板只在派工頻道成立；別的頻道照舊是叫人（MyPMO 的三條件）。"""
    body = CREATE_BODY.format(ulid=ULID_1)
    msg = inbox.as_dict(E.secretary_msg(channel=E.OTHER_CHANNEL, content=body))
    msg["to"] = PMO_IDENTITY
    assert inbox.is_group_request(msg, _cfg()) is False

    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[group-handoff]" not in out
    assert "叫人" in out
    assert len(pmo_env.sends()) == 1


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("# 建群請求：grp-demo", id="create"),
        pytest.param("# 封存請求：grp-demo", id="archive"),
    ],
)
def test_group_request_requires_the_pmo_in_the_to_field(pmo_env, first_line):
    """`to` 沒點名 PMO 的不是群請求（分流條件與 MyPMO `classify_group_request` 一致）。"""
    msg = inbox.as_dict(E.secretary_msg(content=f"{first_line}\n\n- 工作 ID：{ULID_1}\n"))
    msg["to"] = E.OTHER_PM
    assert inbox.is_group_request(msg, _cfg()) is False

    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[group-handoff]" not in out
    assert pmo_env.sends() == []


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("# 建群請求：grp-demo", id="create"),
        pytest.param("# 封存請求：grp-demo", id="archive"),
    ],
)
def test_group_request_requires_the_secretary_as_sender(pmo_env, first_line):
    """寄件人不是秘書的不是群請求（即使首行吻合、頻道與 `to` 都對）。"""
    msg = inbox.as_dict(
        E.secretary_msg(sender=E.TEAM_PM, content=f"{first_line}\n\n- 工作 ID：{ULID_1}\n")
    )
    assert inbox.is_group_request(msg, _cfg()) is False

    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[group-handoff]" not in out
    assert pmo_env.sends() == []


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("[create] grp-demo", id="create"),
        pytest.param("[archive] grp-demo", id="archive"),
    ],
)
def test_bracket_group_request_with_a_name_is_handed_off(pmo_env, first_line):
    """`[create] <名稱>`／`[archive] <名稱>` 這種寫法照樣分流（收緊後仍要認）。"""
    body = f"{first_line}\n\n- 工作 ID：{ULID_1}\n"
    rc, out, _ = run_cli(
        ["inbox", "read"], pmo_env, msgs=[inbox.as_dict(E.secretary_msg(content=body))]
    )
    assert rc == 0
    assert f"[group-handoff] {ULID_1}" in out
    assert pmo_env.sends() == []


def test_bracket_group_name_rejects_a_dot(pmo_env):
    """MyPMO 的群名只收 `[A-Za-z0-9_-]`：`[create] grp.demo` 不當群請求（#362b 低-1）。"""
    body = f"[create] grp.demo\n\n- 工作 ID：{ULID_1}\n"
    msg = inbox.as_dict(E.secretary_msg(content=body))
    assert inbox.is_group_request(msg, _cfg()) is False

    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[group-handoff]" not in out
    assert "叫人" in out
    assert len(pmo_env.sends()) == 1


@pytest.mark.parametrize(
    "first_line",
    [
        pytest.param("[create] 這其實是一般叫人", id="create-arbitrary-tail"),
        pytest.param("[archive] 這其實是一般叫人", id="archive-arbitrary-tail"),
        pytest.param("[create] two words", id="create-multi-word"),
    ],
)
def test_bracket_group_request_with_arbitrary_tail_stays_a_call(pmo_env, first_line):
    """`[create]`／`[archive]` 的尾字收緊成單一名稱：任意尾句不再被吞成群請求。"""
    body = f"{first_line}\n\n- 工作 ID：{ULID_1}\n"
    msg = inbox.as_dict(E.secretary_msg(content=body))
    assert inbox.is_group_request(msg, _cfg()) is False

    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[group-handoff]" not in out
    assert "叫人" in out
    assert len(pmo_env.sends()) == 1


def test_group_request_is_not_printed_as_a_team_message(pmo_env):
    """一則群請求只有一個去處（`[group-handoff]`），不再多印 `[msg]`。"""
    body = CREATE_BODY.format(ulid=ULID_1)
    rc, out, _ = run_cli(
        ["inbox", "read"], pmo_env, msgs=[inbox.as_dict(E.secretary_msg(content=body))]
    )
    assert rc == 0
    assert "[msg]" not in out


# --------------------------------------------------------------------------- #
# 9c. ack／report 已送出但帳本寫不進去（Sonnet L-1）
# --------------------------------------------------------------------------- #


def test_inbox_ack_says_sent_when_ledger_write_fails(pmo_env, monkeypatch):
    """信已送出、帳本寫不進去：stdout 不能宣稱「已發送」，stderr 要講清楚
    已送出（含訊息 id）不要重送，結束碼仍是 2（設計：2＝送不出去，這裡要明說不是）。"""
    seed_item(pmo_env, issue="demo#230")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise SchemaError("state 檔寫不起來")

    monkeypatch.setattr(ledger, "save_state", boom)
    rc, out, err = run_cli(["inbox", "ack", ULID_1], pmo_env)
    assert rc == 2
    assert out == ""
    assert "ack 已送出（id=fake-msg-001）" in err
    assert "帳本寫不進去" in err
    assert "不要重送" in err


def test_inbox_report_says_sent_when_ledger_write_fails(pmo_env, monkeypatch):
    seed_item(pmo_env, issue="demo#230")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise SchemaError("state 檔寫不起來")

    monkeypatch.setattr(ledger, "save_state", boom)
    rc, out, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "已完成", "--body", "x"], pmo_env
    )
    assert rc == 2
    assert out == ""
    assert "工作回報已送出（id=fake-msg-001）" in err
    assert "不要重送" in err


# --------------------------------------------------------------------------- #
# 9d. 另外八項沒轉紅的改壞（Sonnet L-3）
# --------------------------------------------------------------------------- #


def test_duplicate_work_ack_keeps_existing_schedule(pmo_env):
    """重複工作的補發 ack 要沿用既有 schedule，不是寫死預設（L-3）。"""
    seed_item(pmo_env, schedule="立即開工")
    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[E.secretary_msg()])
    assert rc == 0
    (send,) = pmo_env.sends()
    assert "- 預估安排：立即開工" in send.body


def test_report_keeps_the_ledger_pr_when_flag_is_omitted(pmo_env):
    """回報沒帶 `--pr` 時沿用帳本裡的 PR（L-3）。"""
    seed_item(pmo_env, issue="demo#230", pr="https://example.org/demo/pull/9")
    rc, _, err = run_cli(
        ["inbox", "report", ULID_1, "--status", "已完成", "--body", "ok"], pmo_env
    )
    assert rc == 0, err
    (send,) = pmo_env.sends()
    assert "- 相關 PR：https://example.org/demo/pull/9" in send.body


def test_team_message_from_the_pmo_itself_is_skipped(pmo_env):
    """PMO 自己寄出的信不印 `[msg]`、不比對 ack（L-3）。"""
    msg = {
        "id": "01JSELF0000000000000000001",
        "from": PMO_IDENTITY,
        "channel": E.TEAM_CHANNEL,
        "urgency": 5,
        "received_at": "2026-10-19T10:16:00Z",
        "content": "我自己寄的",
    }
    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[msg]" not in out
    assert "[inbox] 沒有新信件" in out


def test_closed_team_state_is_not_acked(pmo_env):
    """已 closed 的 team 不再寫 `ack_at`（看門狗語意，L-3）。"""
    E.seed_team_state(pmo_env, ticket="demo#45", closed=True)
    msg = {
        "id": "01JCLOSED00000000000000001",
        "from": E.TEAM_PM,
        "channel": E.TEAM_CHANNEL,
        "urgency": 5,
        "received_at": "2026-10-19T10:16:00Z",
        "content": "收到 demo#45",
    }
    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0
    assert "[msg]" in out
    assert "[ack]" not in out
    assert not teamstate.load_state(pmo_env.state_dir, E.TEAM_KEY).get("ack_at")


def test_duplicate_ack_failure_is_warned(pmo_env):
    """補發 ack 送不出去時要印 `[warn]`（新工作的 ack 失敗已有測試，L-3）。"""
    from pmo.fakes import FakeAngareion

    seed_item(pmo_env, issue="demo#230")
    pmo_env.ang = FakeAngareion([E.secretary_msg()])
    pmo_env.ang.errors["send"] = PortError("network failure")

    rc, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert "重複工作" in out
    assert "補發 ack 失敗" in out


def test_lowercase_ulid_is_rejected_by_ack_and_report(pmo_env):
    """手動 ack／report 只收 26 碼大寫 ULID（L-3：ULID_RE 不能放寬）。"""
    lower = "01jx9a1b2c3d4e5f6g7h8j9k0m"
    rc, _, err = run_cli(["inbox", "ack", lower], pmo_env)
    assert rc == 1
    assert "不是合法的 26 碼 ULID" in err
    rc, _, err = run_cli(
        ["inbox", "report", lower, "--status", "已完成", "--body", "x"], pmo_env
    )
    assert rc == 1
    assert "不是合法的 26 碼 ULID" in err


# --------------------------------------------------------------------------- #
# 10. 帳本鎖（跨進程排他＋同一行程可重入）
# --------------------------------------------------------------------------- #


def test_inbox_lock_cross_process_and_reentrancy(pmo_env):
    """同一行程可重入、跨進程排他（現行工具的保證）。"""
    import fcntl

    state_dir = pmo_env.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    # 1. 可重入性驗證（同一線程內可重入不死鎖）
    with ledger.state_lock(state_dir):
        with ledger.state_lock(state_dir):
            pass

    # 2. 跨進程鎖排他性驗證
    lock_file = state_dir / "secretary-inbox.json.lock"
    with ledger.state_lock(state_dir):
        assert lock_file.exists()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            with pytest.raises((BlockingIOError, OSError)):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # 3. 釋放鎖後其他進程可取得
    fd = os.open(str(lock_file), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_lock_is_per_state_directory(pmo_env, tmp_path):
    """兩個 state 目錄各自的鎖（arch 中-1）：舊版的模組級 fd 讓第二個目錄根本沒鎖到。"""
    import fcntl

    other = tmp_path / "other-state"
    other.mkdir()
    first = ledger.lock_file(pmo_env.state_dir)
    second = ledger.lock_file(other)

    with ledger.state_lock(pmo_env.state_dir):
        assert first.exists()
        # 第二個目錄的鎖照樣拿得到（不是同一個全域 fd）
        with ledger.state_lock(other):
            assert second.exists()
            fd = os.open(str(second), os.O_RDWR)
            try:
                with pytest.raises((BlockingIOError, OSError)):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    # 放掉之後兩個鎖都可用
    for path in (first, second):
        fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------- #
# 11. 廣播件不開單（審查 B-7／A-3）、寫入消毒（審查 B-4）、磁碟錯誤（B-3）
# --------------------------------------------------------------------------- #


def test_inbox_read_ignores_broadcast_work(pmo_env):
    """廣播件（`to` 沒點名 PMO）不開單、不 ack。"""
    msg = inbox.as_dict(E.secretary_msg())
    msg.pop("to")
    rc, out, err = run_cli(["inbox", "read"], pmo_env, msgs=[msg])
    assert rc == 0, (out, err)
    assert "收到新工作" not in out
    assert ledger.load_state(pmo_env.state_dir)["items"] == {}
    assert pmo_env.sends() == []


def test_write_inbox_state_sanitizes_secret_like_text(pmo_env):
    """state 寫入同力度消毒：秘密樣式不以明文落地（樣式字串在執行期組字，不進原始碼）。"""
    secret = "s" + "k-live-" + "A" * 20 + "123456"
    st = {
        "version": 1,
        "updated_at": None,
        "items": {
            ULID_1: {
                "id": ULID_1,
                "title": "語音派工貼錯金鑰",
                "level": "c",
                "sender": SECRETARY_IDENTITY,
                "channel": DISPATCH_CHANNEL,
                "received_at": "2026-10-19T10:15:30Z",
                "ack_at": None,
                "status": "scheduled",
                "schedule": "排入待開工佇列",
                "issue": None,
                "team": None,
                "reported_at": None,
                "spec": f"請照這串設定 api_key={secret}",
                "notes": f"token: {secret}",
            }
        },
        "notified": {"call": {}, "poke": {}},
    }
    ledger.save_state(pmo_env.state_dir, st)

    raw = pmo_env.inbox_file().read_text(encoding="utf-8")
    assert secret not in raw
    assert "***" in raw
    # 回讀仍是合法 JSON，結構與非秘密欄位不變
    read_back = ledger.load_state(pmo_env.state_dir)
    assert read_back["items"][ULID_1]["id"] == ULID_1
    assert read_back["items"][ULID_1]["status"] == "scheduled"


# --------------------------------------------------------------------------- #
# #383：`pmo --help` 的 inbox 摘要要列出全部子命令
# --------------------------------------------------------------------------- #


def _subparsers_action(parser: object):
    """`parser._subparsers` 是位置參數群組；action 在它的 `_group_actions` 裡。"""
    for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
        if hasattr(action, "choices"):
            return action
    raise AssertionError("找不到 add_subparsers() 的 action")


def _subparser_help(parser: object, name: str) -> str:
    """取父層 `add_parser(name, help=…)` 存下的摘要字串。"""
    for choice in _subparsers_action(parser)._choices_actions:
        if choice.dest == name:
            return choice.help
    raise AssertionError(f"找不到 {name} 子命令的摘要")


def test_inbox_help_summary_lists_every_registered_subcommand():
    """摘要漏一個子命令就紅：名字從實際註冊的 parser 取，不另外抄清單。"""
    parser = cli.build_parser()
    inbox_parser = _subparsers_action(parser).choices["inbox"]
    registered = list(_subparsers_action(inbox_parser).choices)
    summary = _subparser_help(parser, "inbox")

    missing = [name for name in registered if name not in summary]
    assert missing == [], f"inbox 摘要漏了：{missing}；摘要＝{summary!r}"

    # 順序照註冊順序（#383）
    positions = [summary.index(name) for name in registered]
    assert positions == sorted(positions)


def test_write_ledger_wraps_disk_error_as_schema_error(pmo_env, monkeypatch):
    """磁碟錯誤包成 SchemaError（design §3.2：寫不進去 → 2），呼叫端才能退避續跑。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(SchemaError) as exc_info:
        ledger.save_state(pmo_env.state_dir, ledger.load_state(pmo_env.state_dir))
    assert "state 檔寫不起來" in str(exc_info.value)
