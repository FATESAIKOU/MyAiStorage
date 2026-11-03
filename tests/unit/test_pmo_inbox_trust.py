"""收件端的信任與回信規則測試（#110 T-2；移植自 MyPMO tests/dispatch/test_inbox_trust.py）。

語意原樣搬：派工只認派工頻道＋秘書身分、ack／回報送回來信的 channel＋in_reply_to、
廣播不算叫人、機械 ack 只對秘書身分、to／mentions 去重帳與 [poke] 規則。
假 angareion 與假身分（demo-…），不送真訊息、不讀真 channel；斷言從 MyPMO 的
「送信 argv／stdin 記錄」換成 FakeAngareion 的 `SentMessage` 欄位（臉面不同、語意相同）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

from pmo import inbox, ledger
from pmo.config import parse as parse_config

import _pmo_env as E
from _pmo_env import (
    DISPATCH_CHANNEL,
    PMO_IDENTITY,
    SECRETARY_IDENTITY,
    OTHER_CHANNEL,
    ULID_1,
    VALID_WORK_BODY,
    config_data,
    pmo_env,
    run_cli,
    secretary_msg,
    seed_item,
)

def _cfg():
    return parse_config(config_data())


def _sends(env) -> list:
    return env.sends()


def _items(env) -> dict:
    return ledger.load_state(env.state_dir)["items"]


def _call_msg(sender=SECRETARY_IDENTITY, reply_id="01JX9A1B2C3D4E5F6G7H8J9K0Q", **kw):
    """派工頻道外的一則叫人（可帶 to／mentions；格式照臉面 Message）。"""
    msg = inbox.as_dict(
        secretary_msg(ulid=reply_id, channel=OTHER_CHANNEL, content="進度如何？")
    )
    msg["from"] = sender
    kw_full = {"urgency": 5}
    # secretary_msg 預設 `to=PMO_IDENTITY`，叫人規則逐項檢查需要預設無點名
    msg["to"] = None
    msg.update(kw_full)
    for key, value in kw.items():
        if value is ...:  # `to=None` 顯式移除欄位
            msg.pop(key, None)
        else:
            msg[key] = value
    return msg


def _read_env(env, msgs):
    """把假信放進 fake angareion（每次 read 都重發，驗證去重要做第二輪時用得著的方法由個別測試自己挑）。"""
    from pmo.fakes import FakeAngareion
    from pmo.runtime import Deps

    env.ang = FakeAngareion(msgs)
    env.deps = Deps(angareion=env.ang)
    return env


# --- 派工只認秘書身分＋派工頻道 ------------------------------------------- #


def test_dispatch_template_in_other_channel_is_rejected(pmo_env):
    msg = secretary_msg(channel=OTHER_CHANNEL, mentions=(PMO_IDENTITY,))
    _read_env(pmo_env, [msg])
    rc, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert _items(pmo_env) == {}                    # 不建單、不進收件匣
    assert "收到新工作" not in out
    sends = _sends(pmo_env)
    assert len(sends) == 1                          # 只有「叫人」ack，沒有派工 ack
    assert sends[0].channel == OTHER_CHANNEL
    assert "已收件排程中" not in sends[0].body
    assert "叫人只回答" in sends[0].body


def test_forged_secretary_from_outside_dispatch_channel_is_ignored_when_broadcast(pmo_env):
    msg = inbox.as_dict(secretary_msg(channel=OTHER_CHANNEL))
    msg.pop("to")                                   # 廣播件：沒有 to 欄位
    _read_env(pmo_env, [msg])
    rc, _, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert _items(pmo_env) == {}
    assert _sends(pmo_env) == []                    # 廣播不算叫人，不 ack


def test_non_secretary_sender_in_dispatch_channel_is_ignored(pmo_env):
    _read_env(pmo_env, [secretary_msg(sender="someone-else-w1")])
    rc, _, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert _items(pmo_env) == {}
    assert _sends(pmo_env) == []


# --- ack／回報送回來信的 channel、帶 in_reply_to ---------------------------- #


def test_ack_goes_to_origin_channel_with_in_reply_to(pmo_env):
    _read_env(pmo_env, [secretary_msg()])
    run_cli(["inbox", "read"], pmo_env)
    (send,) = _sends(pmo_env)
    assert send.channel == DISPATCH_CHANNEL
    assert send.in_reply_to == ULID_1
    assert _items(pmo_env)[ULID_1]["channel"] == DISPATCH_CHANNEL


def test_report_goes_to_item_channel_with_in_reply_to(pmo_env):
    seed_item(pmo_env, channel="dispatch-demo-b")   # 狀態檔記的是來信的 channel
    rc, _, _ = run_cli(
        ["inbox", "report", ULID_1, "--status", "已完成", "--body", "ok"], pmo_env
    )
    assert rc == 0
    (send,) = _sends(pmo_env)
    assert send.channel == "dispatch-demo-b"
    assert send.in_reply_to == ULID_1


def test_report_legacy_item_without_channel_falls_back_to_dispatch_channel(pmo_env):
    seed_item(pmo_env)
    run_cli(["inbox", "report", ULID_1, "--status", "已完成", "--body", "ok"], pmo_env)
    (send,) = _sends(pmo_env)
    assert send.channel == DISPATCH_CHANNEL
    assert send.in_reply_to == ULID_1


def test_manual_ack_legacy_item_without_channel_falls_back_to_dispatch_channel(pmo_env):
    seed_item(pmo_env)
    run_cli(["inbox", "ack", ULID_1], pmo_env)
    (send,) = _sends(pmo_env)
    assert send.channel == DISPATCH_CHANNEL
    assert send.in_reply_to == ULID_1


def test_duplicate_delivery_resends_ack_to_item_channel(pmo_env):
    from _pmo_env import ReplayAngareion
    from pmo.runtime import Deps

    msg = secretary_msg()
    pmo_env.ang = ReplayAngareion([msg])
    seed_item(pmo_env, channel="dispatch-demo-b")
    run_cli(["inbox", "read"], pmo_env)
    (send,) = _sends(pmo_env)
    assert send.channel == "dispatch-demo-b"
    assert send.in_reply_to == ULID_1


def test_item_channel_helper():
    """沒記 channel 的是舊項目，一律回信到派工頻道。"""
    assert inbox.item_channel({"channel": "dispatch-demo-b"}, _cfg()) == "dispatch-demo-b"
    assert inbox.item_channel({}, _cfg()) == DISPATCH_CHANNEL
    assert inbox.item_channel({"channel": None}, _cfg()) == DISPATCH_CHANNEL


def test_manual_ack_uses_item_channel_and_in_reply_to(pmo_env):
    seed_item(pmo_env, channel="dispatch-demo-b")
    run_cli(["inbox", "ack", ULID_1], pmo_env)
    (send,) = _sends(pmo_env)
    assert send.channel == "dispatch-demo-b"
    assert send.in_reply_to == ULID_1


# --- 叫人：只有 to／mentions 點到 PMO；機械 ack 只對秘書身分 ---------------- #


def test_is_call_rules():
    """to／mentions 點名才叫人；廣播、自己、前綴相同的別人都不算（design D3、D9）。"""
    cfg = _cfg()
    assert inbox.is_call(_call_msg(sender="x-pm-w1", to=PMO_IDENTITY), cfg)
    assert inbox.is_call(_call_msg(sender="x-pm-w1", mentions=[PMO_IDENTITY]), cfg)
    assert inbox.is_call(_call_msg(sender="x-pm-w1", mentions=["a-pm-m1", PMO_IDENTITY]), cfg)
    assert not inbox.is_call(_call_msg(sender="x-pm-w1"), cfg)                            # 廣播
    assert not inbox.is_call(_call_msg(sender="x-pm-w1", to="other-pm-m1"), cfg)
    assert not inbox.is_call(_call_msg(sender="x-pm-w1", mentions=["other-pm-m1"]), cfg)
    assert not inbox.is_call(_call_msg(sender="x-pm-w1", mentions=None), cfg)
    assert not inbox.is_call(_call_msg(sender=PMO_IDENTITY, mentions=[PMO_IDENTITY]), cfg)  # 自己不算
    assert not inbox.is_call(_call_msg(sender="x-pm-w1", to="demo-pm-m10"), cfg)        # 前綴相同的別人不算
    assert not inbox.is_call(_call_msg(sender="x-pm-w1", mentions=["xdemo-pm-m1"]), cfg)
    # 派工 channel 內：樣板是派工，不是叫人；非樣板且點到 PMO 才是叫人（S2）
    in_dispatch = dict(_call_msg(sender="x-pm-w1", mentions=[PMO_IDENTITY]), channel=DISPATCH_CHANNEL)
    assert inbox.is_call(in_dispatch, cfg)
    assert not inbox.is_call(dict(in_dispatch, content=VALID_WORK_BODY), cfg)
    assert not inbox.is_call(dict(_call_msg(sender="x-pm-w1"), channel=DISPATCH_CHANNEL), cfg)  # 廣播


def test_mechanical_ack_only_for_secretary_identity(pmo_env):
    _read_env(pmo_env, [_call_msg(sender="someone-pm-w1", mentions=[PMO_IDENTITY])])
    run_cli(["inbox", "read"], pmo_env)
    assert _sends(pmo_env) == []                    # 非秘書身分叫人：不 ack（多 AI channel 不洗版）
    assert _items(pmo_env) == {}


def test_secretary_call_outside_dispatch_gets_ack_in_same_channel_and_no_ticket(pmo_env):
    msg = _call_msg(sender=SECRETARY_IDENTITY, mentions=[PMO_IDENTITY])
    _read_env(pmo_env, [msg])
    rc, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    (send,) = _sends(pmo_env)
    assert send.channel == OTHER_CHANNEL
    assert send.in_reply_to == msg["id"]
    assert "已收件排程中" not in send.body
    assert _items(pmo_env) == {}


def test_secretary_question_inside_dispatch_channel_gets_call_ack_not_invalid(pmo_env):
    msg = dict(_call_msg(sender=SECRETARY_IDENTITY, to=PMO_IDENTITY), channel=DISPATCH_CHANNEL)
    _read_env(pmo_env, [msg])
    rc, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    (send,) = _sends(pmo_env)
    assert send.channel == DISPATCH_CHANNEL
    assert send.in_reply_to == msg["id"]
    assert "已收件排程中" not in send.body
    assert "不合規" not in out
    assert _items(pmo_env) == {}                    # 不建單、不進收件匣


def test_broken_template_in_dispatch_channel_stays_invalid_without_ack(pmo_env):
    msg = dict(
        inbox.as_dict(secretary_msg(content="# 工作：缺工作 ID\n\n- 建議層級：c\n")),
        mentions=[PMO_IDENTITY],
    )
    _read_env(pmo_env, [msg])
    _, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert "不合規" in out
    assert _sends(pmo_env) == []
    assert _items(pmo_env) == {}


def test_non_secretary_question_inside_dispatch_channel_gets_no_ack(pmo_env):
    msg = dict(_call_msg(sender="someone-pm-w1", to=PMO_IDENTITY), channel=DISPATCH_CHANNEL)
    _read_env(pmo_env, [msg])
    run_cli(["inbox", "read"], pmo_env)
    assert _sends(pmo_env) == []


def test_secretary_broadcast_is_not_a_call(pmo_env):
    _read_env(pmo_env, [_call_msg(sender=SECRETARY_IDENTITY)])
    run_cli(["inbox", "read"], pmo_env)
    assert _sends(pmo_env) == []


# --- 去重：同一則叫人只 ack 一次、同一則急件只標一次 [poke] ----------------- #

OTHER_CALL_ID = "01JX9A1B2C3D4E5F6G7H8J9K0R"


def test_same_call_read_twice_acks_once(pmo_env):
    """游標重來（狀態遺失或 `--since` 補讀）再看到同一則叫人，不能再回一次 ack。"""
    from _pmo_env import ReplayAngareion
    from pmo.runtime import Deps

    msg = _call_msg(sender=SECRETARY_IDENTITY, mentions=[PMO_IDENTITY])
    pmo_env.ang = ReplayAngareion([msg])
    rc, _, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert len(_sends(pmo_env)) == 1

    rc, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert len(_sends(pmo_env)) == 1                # 第二次不再送 ack
    assert f"id={msg['id']}" in out
    assert "略過" in out                            # 看得出來是被去重，不是被吃掉

    st = ledger.load_state(pmo_env.state_dir)
    assert msg["id"] in st["notified"]["call"]


def test_same_call_twice_in_one_batch_acks_once(pmo_env):
    from _pmo_env import ReplayAngareion
    from pmo.runtime import Deps

    msg = _call_msg(sender=SECRETARY_IDENTITY, mentions=[PMO_IDENTITY])
    pmo_env.ang = ReplayAngareion([msg, dict(msg)])
    rc, _, _ = run_cli(["inbox", "read"], pmo_env)
    assert rc == 0
    assert len(_sends(pmo_env)) == 1


def test_distinct_calls_each_get_ack(pmo_env):
    """去重只擋同一則；不同 id 的叫人照常各回一次。"""
    msg = _call_msg(sender=SECRETARY_IDENTITY, mentions=[PMO_IDENTITY])
    _read_env(pmo_env, [msg, dict(msg, id=OTHER_CALL_ID)])
    run_cli(["inbox", "read"], pmo_env)
    assert len(_sends(pmo_env)) == 2


def test_failed_call_ack_is_not_recorded_and_is_retried(pmo_env):
    """送不出去的 ack 不記成已回；下一輪要重試（否則叫人永遠沒被回）。"""
    from _pmo_env import ReplayAngareion
    from pmo.ports import PortError
    from pmo.runtime import Deps

    msg = _call_msg(sender=SECRETARY_IDENTITY, mentions=[PMO_IDENTITY])
    pmo_env.ang = ReplayAngareion([msg])
    pmo_env.ang.errors["send"] = PortError("network failure")
    run_cli(["inbox", "read"], pmo_env)
    assert len(_sends(pmo_env)) == 0
    run_cli(["inbox", "read"], pmo_env)
    assert len(_sends(pmo_env)) == 0

    pmo_env.ang.errors.pop("send")                  # 網路恢復
    run_cli(["inbox", "read"], pmo_env)
    assert len(_sends(pmo_env)) == 1                # 第一次失敗，之後重試成功

    st = ledger.load_state(pmo_env.state_dir)
    assert msg["id"] in st["notified"]["call"]


def test_notified_ledger_keeps_only_recent_entries(monkeypatch):
    monkeypatch.setattr(inbox, "NOTIFIED_MAX", 2)
    st = {"version": 1, "updated_at": None, "items": {}}
    for mid in ("01JLEDGER000000000000000001", "01JLEDGER000000000000000002", "01JLEDGER000000000000000003"):
        assert not inbox.was_notified(st, "call", mid)
        inbox.mark_notified(st, "call", mid)
    assert not inbox.was_notified(st, "call", "01JLEDGER000000000000000001")   # 最舊的被裁掉
    assert inbox.was_notified(st, "call", "01JLEDGER000000000000000003")
    assert len(st["notified"]["call"]) == 2


# --- poke：派工頻道內、秘書身分、urgency ≥ 8 ------------------------------- #


def test_should_poke_table():
    base = inbox.as_dict(secretary_msg())
    assert inbox.should_poke(dict(base, urgency=8), _cfg())
    assert inbox.should_poke(dict(base, urgency=9), _cfg())
    assert not inbox.should_poke(dict(base, urgency=7), _cfg())
    assert not inbox.should_poke(dict(base, urgency=9, channel=OTHER_CHANNEL), _cfg())
    assert not inbox.should_poke(dict(base, urgency=9, **{"from": "someone-else-w1"}), _cfg())
    assert not inbox.should_poke(dict(base, urgency="bad"), _cfg())
    assert not inbox.should_poke(dict(base, urgency=None), _cfg())


def test_urgent_secretary_dispatch_emits_poke_event_but_others_do_not(pmo_env):
    urgent = dict(inbox.as_dict(secretary_msg()), urgency=9)
    _read_env(pmo_env, [urgent])
    _, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert "[poke]" in out

    # 派工 channel 外的急件不叫醒模型（第二個暫存環境，各項照測試自己隔離）
    other_dir = pmo_env.tmp_path / "other-urgent-call"
    other_dir.mkdir()
    config_path = other_dir / "teams.json"
    config_path.write_text(json.dumps(config_data(), ensure_ascii=False), encoding="utf-8")
    pmo_env.monkeypatch.setenv("PMO_SKILL_CONFIG", str(config_path))
    pmo_env.monkeypatch.setenv("PMO_SKILL_STATE_DIR", str(other_dir / "state"))
    _read_env(pmo_env, [dict(_call_msg(sender=SECRETARY_IDENTITY, mentions=[PMO_IDENTITY]), urgency=9)])
    _, out2, _ = run_cli(["inbox", "read"], pmo_env)
    assert "[poke]" not in out2


def test_same_urgent_message_pokes_once(pmo_env):
    """同一則急件在補讀時再出現，只標一次 [poke]（M4 接上叫醒後才不會重複叫）。"""
    from _pmo_env import ReplayAngareion
    from pmo.runtime import Deps

    urgent = dict(inbox.as_dict(secretary_msg()), urgency=9)
    pmo_env.ang = ReplayAngareion([urgent])
    _, out1, _ = run_cli(["inbox", "read"], pmo_env)
    assert out1.count("[poke]") == 1

    _, out2, _ = run_cli(["inbox", "read"], pmo_env)
    assert "[poke]" not in out2

    st = ledger.load_state(pmo_env.state_dir)
    assert urgent["id"] in st["notified"]["poke"]


def test_same_urgent_message_twice_in_one_batch_pokes_once(pmo_env):
    from _pmo_env import ReplayAngareion
    from pmo.runtime import Deps

    urgent = dict(inbox.as_dict(secretary_msg()), urgency=9)
    pmo_env.ang = ReplayAngareion([urgent, dict(urgent)])
    _, out, _ = run_cli(["inbox", "read"], pmo_env)
    assert out.count("[poke]") == 1


def test_urgent_call_is_poked_and_acked_once_each(pmo_env):
    """同一則訊息既是急件（[poke]）又是叫人（ack）時，兩個去重帳要分開記。"""
    from _pmo_env import ReplayAngareion
    from pmo.runtime import Deps

    msg = dict(_call_msg(sender=SECRETARY_IDENTITY, to=PMO_IDENTITY), channel=DISPATCH_CHANNEL, urgency=9)
    pmo_env.ang = ReplayAngareion([msg])
    _, out1, _ = run_cli(["inbox", "read"], pmo_env)
    assert out1.count("[poke]") == 1
    assert len(_sends(pmo_env)) == 1

    _, out2, _ = run_cli(["inbox", "read"], pmo_env)
    assert "[poke]" not in out2
    assert len(_sends(pmo_env)) == 1

    st = ledger.load_state(pmo_env.state_dir)
    assert msg["id"] in st["notified"]["poke"]
    assert msg["id"] in st["notified"]["call"]
