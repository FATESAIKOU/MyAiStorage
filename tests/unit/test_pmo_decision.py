"""決定題：送題與作答解析測試（#110 T-2；移植自 MyPMO tests/dispatch/test_decision.py）。

語意原樣搬：`build_decision_body` 只允許恰好一個 ```pmo-decision 區塊、
`parse_decision_answer` 對格式不合的一律回 None（當一般訊息）、`inbox read`
印出作答但不建單、照既有叫人規則機械 ack；CLI decision-send 照 design §3.2
只有 `--body` 一個旗面，**沒有** `--channel/--to` 覆寫（MyPMO 的對應測試
`test_cli_decision_send_honours_channel_and_to` 因 design 決策被替換成
「不該有這些旗面」的反向測試，列入實作報告）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import re

import pytest

from pmo import decision, ledger
from pmo.config import parse as parse_config

import _pmo_env as E
from _pmo_env import (
    DISPATCH_CHANNEL,
    PMO_IDENTITY,
    SECRETARY_IDENTITY,
    ULID_1,
    ULID_2,
    ULID_3,
    pmo_env,
    run_cli,
)

pytestmark = pytest.mark.usefixtures("_seat")

QUESTIONS = [
    {
        "id": "q1",
        "header": "範圍",
        "question": "先做哪一半？",
        "multiple": False,
        "options": [
            {"id": "a", "label": "只做看與送"},
            {"id": "b", "label": "連決定題一起做"},
        ],
    },
    {
        "id": "q2",
        "header": "審查",
        "question": "誰審？",
        "multiple": True,
        "options": [
            {"id": "a", "label": "PM"},
            {"id": "b", "label": "本人"},
        ],
    },
]

FENCE_RE = re.compile(r"```pmo-decision\n(.*?)\n```", re.DOTALL)


@pytest.fixture(autouse=True)
def _seat(monkeypatch):
    E.set_seat(monkeypatch)


def _cfg():
    return parse_config(E.config_data())


def decision_payload(body: str) -> dict:
    matches = FENCE_RE.findall(body)
    assert len(matches) == 1, f"pmo-decision 區塊必須恰好一個：{body!r}"
    return json.loads(matches[0])


def answer_msg(
    content: str,
    msg_id: str = ULID_3,
    sender: str = SECRETARY_IDENTITY,
    received_at: str = "2026-10-27T10:00:00Z",
    urgency: int = 8,
) -> dict:
    return {
        "id": msg_id,
        "from": sender,
        "to": PMO_IDENTITY,
        "channel": DISPATCH_CHANNEL,
        "urgency": urgency,
        "received_at": received_at,
        "content": content,
    }


def _sends(env) -> list:
    return env.sends()


def _items(env) -> dict:
    return ledger.load_state(env.state_dir)["items"]


# --------------------------------------------------------------------------- #
# 1. 送題：build_decision_body
# --------------------------------------------------------------------------- #


def test_build_decision_body_has_text_and_exactly_one_block():
    body = decision.build_decision_body("請看下面的問題，回覆時照題號作答。", QUESTIONS)

    assert body.startswith("請看下面的問題，回覆時照題號作答。")
    assert body.count("```pmo-decision") == 1
    payload = decision_payload(body)
    assert payload["v"] == 1
    assert [q["id"] for q in payload["questions"]] == ["q1", "q2"]
    assert payload["questions"][0]["header"] == "範圍"
    assert payload["questions"][0]["multiple"] is False
    assert payload["questions"][0]["options"] == [
        {"id": "a", "label": "只做看與送"},
        {"id": "b", "label": "連決定題一起做"},
    ]
    assert payload["questions"][1]["multiple"] is True


def test_build_decision_body_minimal_question():
    body = decision.build_decision_body("只有一題。", [{"id": "q1", "multiple": False, "options": [{"id": "a"}]}])
    payload = decision_payload(body)
    assert payload["questions"] == [
        {"id": "q1", "multiple": False, "options": [{"id": "a", "label": ""}]}
    ]


@pytest.mark.parametrize(
    "questions, match",
    [
        pytest.param([], "1～4", id="zero-questions"),
        pytest.param([QUESTIONS[0]] * 5, "1～4", id="five-questions"),
        pytest.param(
            [QUESTIONS[0], dict(QUESTIONS[1], id="q1")], "重複", id="duplicate-id"
        ),
        pytest.param(
            [dict(QUESTIONS[0], options=[{"id": "A"}])], "小寫字母", id="uppercase-option"
        ),
        pytest.param(
            [dict(QUESTIONS[0], options=[{"id": "a1"}])], "小寫字母", id="option-with-digit"
        ),
        pytest.param(
            [dict(QUESTIONS[0], multiple="true")], "布林", id="multiple-string"
        ),
        pytest.param([dict(QUESTIONS[0], multiple=1)], "布林", id="multiple-int"),
        pytest.param([dict(QUESTIONS[0], options=[])], "options", id="empty-options"),
        pytest.param([dict(QUESTIONS[0], options="a")], "options", id="options-not-list"),
        pytest.param(["q1"], "物件", id="question-not-dict"),
        pytest.param([dict(QUESTIONS[0], options=["a"])], "物件", id="option-not-dict"),
        pytest.param([dict(QUESTIONS[0], id="q:1")], "id", id="id-with-colon"),
        pytest.param([dict(QUESTIONS[0], header=1)], "header", id="header-not-str"),
        pytest.param("not-a-list", "陣列", id="questions-not-list"),
    ],
)
def test_build_decision_body_rejects_non_compliant_questions(questions, match):
    with pytest.raises(ValueError, match=match):
        decision.build_decision_body("請作答。", questions)


@pytest.mark.parametrize(
    "text, match",
    [
        pytest.param("", "text", id="empty-text"),
        pytest.param("   ", "text", id="blank-text"),
        pytest.param(None, "text", id="text-none"),
        pytest.param("看這裡\n```pmo-decision\n{}\n```", "pmo-decision", id="fence-in-text"),
    ],
)
def test_build_decision_body_rejects_bad_text(text, match):
    with pytest.raises(ValueError, match=match):
        decision.build_decision_body(text, QUESTIONS)


# --------------------------------------------------------------------------- #
# 2. 作答：parse_decision_answer
# --------------------------------------------------------------------------- #


def test_parse_decision_answer_single_choice():
    parsed = decision.parse_decision_answer(f"# 回答決定題：{ULID_1}\n- q1：a\n")
    assert parsed == {"decision_id": ULID_1, "answers": {"q1": ["a"]}, "note": None}


def test_parse_decision_answer_multiple_choice_with_note():
    body = f"# 回答決定題：{ULID_1}\n- q1：a,b\n- q2：a\n- 說明：先做看與送那一半\n"
    parsed = decision.parse_decision_answer(body)
    assert parsed is not None
    assert parsed["answers"] == {"q1": ["a", "b"], "q2": ["a"]}
    assert parsed["note"] == "先做看與送那一半"


def test_parse_decision_answer_accepts_half_width_colons():
    body = f"# 回答決定題:{ULID_1}\n- q1:b\n- 說明:ok\n"
    parsed = decision.parse_decision_answer(body)
    assert parsed == {"decision_id": ULID_1, "answers": {"q1": ["b"]}, "note": "ok"}


def test_parse_decision_answer_ignores_blank_lines_and_crlf():
    body = f"\r\n# 回答決定題：{ULID_1}\r\n\r\n- q1：a\r\n\r\n"
    parsed = decision.parse_decision_answer(body)
    assert parsed is not None
    assert parsed["answers"] == {"q1": ["a"]}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param(f"# 回答決定題\n- q1：a", id="no-id"),
        pytest.param("# 回答決定題：not-a-ulid\n- q1：a", id="id-not-ulid"),
        pytest.param(f"# 回答決定題：{ULID_1}x\n- q1：a", id="id-too-long"),
        pytest.param(f"# 工作：{ULID_1}\n- q1：a", id="wrong-title"),
        pytest.param(f"回答決定題：{ULID_1}\n- q1：a", id="missing-hash"),
        pytest.param(f"## 回答決定題：{ULID_1}\n- q1：a", id="double-hash"),
        pytest.param(f"先說明\n# 回答決定題：{ULID_1}\n- q1：a", id="leading-text"),
        pytest.param(f"# 回答決定題：{ULID_1}\n- q1 a", id="no-separator"),
        pytest.param(f"# 回答決定題：{ULID_1}\n- q1：A", id="uppercase-answer"),
        pytest.param(f"# 回答決定題：{ULID_1}\n- q1：", id="empty-answer"),
        pytest.param(f"# 回答決定題：{ULID_1}\n- q1：a,\n", id="trailing-comma"),
        pytest.param(f"# 回答決定題：{ULID_1}\n- q1：a\n- q1：b", id="duplicate-question"),
        pytest.param(f"# 回答決定題：{ULID_1}\n- 說明：只有說明", id="note-only"),
        pytest.param(
            f"# 回答決定題：{ULID_1}\n- q1：a\n- 說明：一\n- 說明：二", id="two-notes"
        ),
        pytest.param(f"# 回答決定題：{ULID_1}\n自由文字", id="stray-line"),
    ],
)
def test_parse_decision_answer_rejects_non_compliant(body):
    assert decision.parse_decision_answer(body) is None


# --------------------------------------------------------------------------- #
# 3. inbox read：印出作答、照既有規則機械 ack、不建單
# --------------------------------------------------------------------------- #


def test_inbox_read_prints_decision_answer_and_acks_without_creating_work(pmo_env):
    msg = answer_msg(f"# 回答決定題：{ULID_1}\n- q1：b\n- 說明：先做看與送\n")
    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])

    assert rc == 0
    assert f"[decision] q={ULID_1} from={SECRETARY_IDENTITY}" in out
    assert "answers=q1=b" in out
    assert "說明=先做看與送" in out
    # 只印、不建單
    assert _items(pmo_env) == {}
    assert "收到新工作" not in out
    # 仍照既有叫人規則機械 ack
    (send,) = _sends(pmo_env)
    assert send.channel == DISPATCH_CHANNEL
    assert send.in_reply_to == ULID_3
    assert send.body.startswith(f"# 收到叫人：{ULID_3}")


def test_inbox_read_keeps_only_latest_answer_per_decision(pmo_env):
    # 故意把新的一則排在前面：順序不該決定勝負，received_at 才是（把最新判斷改壞會紅）
    newer = answer_msg(
        f"# 回答決定題：{ULID_1}\n- q1：b\n",
        msg_id=ULID_3,
        received_at="2026-10-27T10:00:00Z",
    )
    older = answer_msg(
        f"# 回答決定題：{ULID_1}\n- q1：a\n",
        msg_id=ULID_2,
        received_at="2026-10-27T09:00:00Z",
    )
    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[newer, older])

    assert rc == 0
    assert out.count("[decision]") == 1
    assert "answers=q1=b" in out
    assert "answers=q1=a" not in out
    # 兩則都照既有規則各 ack 一次（去重是 per 訊息 id，不是 per 題目）
    assert len(_sends(pmo_env)) == 2


def test_inbox_read_ignores_malformed_answer_as_normal_message(pmo_env):
    msg = answer_msg(f"# 回答決定題：{ULID_1}\n- q1 A\n", msg_id=ULID_3)
    rc, out, _ = run_cli(["inbox", "read"], pmo_env, msgs=[msg])

    assert rc == 0
    assert "[decision]" not in out
    assert _items(pmo_env) == {}


# --------------------------------------------------------------------------- #
# 4. CLI：decision send（design §3.2：只有 --body，沒有 --channel/--to 覆寫）
# --------------------------------------------------------------------------- #


def test_cli_decision_send_uses_angareion_urgency_8(pmo_env):
    payload = json.dumps({"text": "請看下面的問題。", "questions": QUESTIONS}, ensure_ascii=False)

    rc, out, err = run_cli(["inbox", "decision-send", "--body", "-"], pmo_env, stdin_text=payload)

    assert rc == 0, err
    assert "[decision]" in out
    (send,) = _sends(pmo_env)
    assert send.channel == DISPATCH_CHANNEL
    assert send.to == SECRETARY_IDENTITY
    assert send.urgency == 8
    body = send.body
    assert body.startswith("請看下面的問題。")
    payload_sent = decision_payload(body)
    assert [q["id"] for q in payload_sent["questions"]] == ["q1", "q2"]


def test_cli_decision_send_send_failure_exits_2(pmo_env):
    """design §3.2：送不出去是 exit 2（JSON 不合格式才是 1）。"""
    from pmo.ports import PortError

    pmo_env.ang.errors["send"] = PortError("network failure")
    payload = json.dumps({"text": "請作答。", "questions": QUESTIONS}, ensure_ascii=False)

    rc, out, err = run_cli(["inbox", "decision-send", "--body", "-"], pmo_env, stdin_text=payload)

    assert rc == 2
    assert "發送決定題失敗" in err
    assert out == ""
    assert _sends(pmo_env) == []


def test_cli_decision_send_has_no_channel_override_flags(pmo_env):
    """design §3.2 的旗面只有 --body：--channel/--to 是 MyPMO 的舊旗面，不存在。"""
    payload = json.dumps({"text": "請作答。", "questions": QUESTIONS}, ensure_ascii=False)
    rc, out, err = run_cli(
        ["inbox", "decision-send", "--channel", DISPATCH_CHANNEL, "--to", SECRETARY_IDENTITY, "--body", "-"],
        pmo_env,
        stdin_text=payload,
    )
    assert rc == 1
    assert "用法錯誤" in err
    assert _sends(pmo_env) == []


@pytest.mark.parametrize(
    "payload, match",
    [
        pytest.param("not-json", "JSON", id="not-json"),
        pytest.param(json.dumps(["text"]), "物件", id="root-not-object"),
        pytest.param(json.dumps({"text": "x"}), "缺少", id="missing-questions"),
        pytest.param(
            json.dumps({"text": "x", "questions": []}), "1～4", id="zero-questions"
        ),
        pytest.param(
            json.dumps({"text": "x", "questions": [{"id": "q1", "multiple": "no",
                                                    "options": [{"id": "a"}]}]}),
            "布林",
            id="bad-multiple",
        ),
    ],
)
def test_cli_decision_send_rejects_bad_body(pmo_env, payload, match):
    rc, _, err = run_cli(["inbox", "decision-send", "--body", "-"], pmo_env, stdin_text=payload)

    assert rc == 1
    assert match in err
    assert _sends(pmo_env) == []
