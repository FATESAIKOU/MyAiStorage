"""The decision questions (aientry V2 決定題 design §5; MyPMO#222).

The questions a PMO sends and the answers it reads back: validating the
question list, assembling the one ```pmo-decision block, sending it (urgency
8), and parsing an answer. Everything here is either pure or goes out through
the ports; the inbox round decides **when** to look at an answer (it prints the
`[decision]` line and does nothing else with it — an answer is not an
authorization).

Split out of `pmo.inbox` (#362): the inbox module keeps the read flow, and this
one does not import it back, so the dependency runs one way —
`round → inbox → decision → runtime`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from .runtime import Ctx, assert_pmo_identity

__all__ = [
    "DECISION_FENCE",
    "DECISION_MAX_QUESTIONS",
    "DECISION_URGENCY",
    "build_decision_body",
    "build_decision_send_body",
    "parse_decision_answer",
    "format_decision_event",
    "latest_decision_answers",
    "send_decision",
]

DECISION_FENCE = "pmo-decision"
DECISION_MAX_QUESTIONS = 4
#: design P8：PMO 只對派工頻道＋秘書身分＋urgency ≥ 8 印 [poke]。
DECISION_URGENCY = 8
DECISION_FIRST_RE = re.compile(r"^# 回答決定題[:：]([0-9A-HJKMNP-TV-Z]{26})$")
DECISION_QID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DECISION_ANSWER_RE = re.compile(r"^- ([A-Za-z0-9_-]+)[：:](.*)$")
DECISION_NOTE_RE = re.compile(r"^- 說明[：:](.*)$")
DECISION_OPTION_ID_RE = re.compile(r"^[a-z]+$")


def _normalize_decision_questions(questions: Any) -> list:
    """驗決定題的 questions，回傳正規化後的清單。不合規丟 ValueError。"""
    if not isinstance(questions, list):
        raise ValueError("questions 必須是陣列")
    if not 1 <= len(questions) <= DECISION_MAX_QUESTIONS:
        raise ValueError(
            f"questions 必須 1～{DECISION_MAX_QUESTIONS} 則（收到 {len(questions)} 則）"
        )
    seen: set = set()
    normalized: list = []
    for q in questions:
        if not isinstance(q, dict):
            raise ValueError("questions[] 的每一則都必須是物件")
        qid = q.get("id")
        if not isinstance(qid, str) or not DECISION_QID_RE.match(qid):
            raise ValueError(f"question id 不合法：{qid!r}（只收英數、_、-，不可含空白或冒號）")
        if qid in seen:
            raise ValueError(f"question id 重複：{qid}")
        seen.add(qid)

        multiple = q.get("multiple")
        if not isinstance(multiple, bool):
            raise ValueError(f"question {qid} 的 multiple 必須是布林（true／false）")

        options = q.get("options")
        if not isinstance(options, list) or not options:
            raise ValueError(f"question {qid} 的 options 必須是非空陣列")
        norm_options: list = []
        for opt in options:
            if not isinstance(opt, dict):
                raise ValueError(f"question {qid} 的 options[] 每一項都必須是物件")
            oid = opt.get("id")
            if not isinstance(oid, str) or not DECISION_OPTION_ID_RE.match(oid):
                raise ValueError(f"question {qid} 的 options[].id 必須是小寫字母：{oid!r}")
            label = opt.get("label", "")
            if not isinstance(label, str):
                raise ValueError(f"question {qid} 的 option {oid} 的 label 必須是字串")
            norm_options.append({"id": oid, "label": label})

        item: dict = {"id": qid}
        header = q.get("header")
        if header is not None:
            if not isinstance(header, str):
                raise ValueError(f"question {qid} 的 header 必須是字串")
            item["header"] = header
        question = q.get("question")
        if question is not None:
            if not isinstance(question, str):
                raise ValueError(f"question {qid} 的 question 必須是字串")
            item["question"] = question
        item["multiple"] = multiple
        item["options"] = norm_options
        normalized.append(item)
    return normalized


def build_decision_body(text: str, questions: Any) -> str:
    """組決定題內文：人看的文字＋唯一一個 ```pmo-decision 區塊。不合格式丟 ValueError。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 必須是非空字串（區塊外、人看的文字）")
    if f"```{DECISION_FENCE}" in text:
        raise ValueError(f"text 不可含有 ```{DECISION_FENCE} 區塊（唯一一個由本函式產生）")
    payload = {
        "v": 1,
        "questions": _normalize_decision_questions(questions),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{text.strip()}\n\n```{DECISION_FENCE}\n{encoded}\n```\n"


def parse_decision_answer(body: str) -> dict | None:
    """解析一則決定題作答：合規回 dict，格式不合回 None（當一般訊息）。"""
    lines = [
        ln.strip()
        for ln in str(body or "").replace("\r\n", "\n").strip().split("\n")
        if ln.strip()
    ]
    if not lines:
        return None
    m_first = DECISION_FIRST_RE.match(lines[0])
    if not m_first:
        return None

    answers: dict = {}
    note: str | None = None
    for line in lines[1:]:
        m_note = DECISION_NOTE_RE.match(line)
        if m_note:
            if note is not None:
                return None  # 說明只能一行
            note = m_note.group(1).strip()
            continue
        m_answer = DECISION_ANSWER_RE.match(line)
        if not m_answer:
            return None
        qid = m_answer.group(1)
        if qid in answers:
            return None  # 同一題重複作答，格式不合
        raw_opts = [opt.strip() for opt in m_answer.group(2).split(",")]
        if not raw_opts or any(not DECISION_OPTION_ID_RE.match(opt) for opt in raw_opts):
            return None
        answers[qid] = raw_opts

    if not answers:
        return None  # 只有說明、沒有作答，不算作答
    return {
        "decision_id": m_first.group(1),
        "answers": answers,
        "note": note,
    }


def format_decision_event(msg: Mapping, parsed: dict) -> str:
    """`inbox read` 印給人看的一行（只印、不建單；答案不是授權）。"""
    answers = " ".join(f"{qid}={','.join(opts)}" for qid, opts in parsed["answers"].items())
    note = parsed.get("note")
    note_text = "-" if not note else " ".join(str(note).split())
    return (
        f"[decision] q={parsed['decision_id']} from={msg.get('from')} "
        f"answers={answers} 說明={note_text}"
    )


def latest_decision_answers(messages: Iterable) -> dict:
    """同一題多則作答時以最新一則為準（以 received_at），回傳 `id(msg) -> parsed`。"""
    latest: dict = {}
    for order, raw in enumerate(messages):
        if not isinstance(raw, dict):
            continue
        parsed = parse_decision_answer(str(raw.get("content") or ""))
        if parsed is None:
            continue
        key = (str(raw.get("received_at") or ""), order)
        decision_id = parsed["decision_id"]
        if decision_id not in latest or key >= latest[decision_id][0]:
            latest[decision_id] = (key, raw, parsed)
    return {id(entry[1]): entry[2] for entry in latest.values()}


def build_decision_send_body(body_json: str) -> str:
    """CLI 的 body（JSON：`{"text": ..., "questions": [...]}`）→ 決定題內文。"""
    try:
        payload = json.loads(body_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"body 必須是 JSON（text＋questions）：{e}") from e
    if not isinstance(payload, dict):
        raise ValueError("body JSON 根節點必須是物件（text＋questions）")
    if "text" not in payload or "questions" not in payload:
        raise ValueError("body JSON 缺少 text 或 questions")
    return build_decision_body(payload["text"], payload["questions"])


def send_decision(ctx: Ctx, body: str) -> str:
    """送出一則決定題（urgency 8，走既有的 Angareion）。

    The channel and the recipient come from the settings only: the design's
    command has no `--channel`/`--to`, so the function has no overrides either
    (Sonnet L-2).
    """
    assert_pmo_identity(ctx.config)
    return ctx.deps.angareion.send(
        channel=ctx.config.dispatch_channel,
        to=ctx.config.secretary_identity,
        body=body,
        urgency=DECISION_URGENCY,
    )
