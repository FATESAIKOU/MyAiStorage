"""The inbox core: parsing, classifying, and the secretary stage (T-2).

Ported from MyPMO's `pmo/inbox.py` (+ the inbox pieces of `pmo/dispatch.py`)
as design `#110` §5.1 says: the template rules, the message classes and the
decision questions move over as they are, with the identity, the secretary's
AI name and the dispatch channel coming from the teams settings instead of the
old hard-coded constants. The group-request branch of the round (`groups`)
stays in MyPMO during the transition (design Q-9 (a)): what moves is only its
**classification**, so a group request is not answered as a simple question
(one `[group-handoff]` line, nothing consumed — the handoff itself is #358).
The round lives in `pmo.round`, the team-message matching in `pmo.teammsg`,
and the decision questions in `pmo.decision` (#362; this module imports only
the two it uses — `latest_decision_answers`, `format_decision_event` — and
does not re-export them). This module keeps the inbox read flow.

The ledger is the same file MyPMO writes (`secretary-inbox.json`); the path,
the compatibility fill, the flock and the atomic, redacted write all live in
`pmo.ledger` (arch review 中-1), so this module never touches the file itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from . import ledger, sanitize
from .decision import format_decision_event, latest_decision_answers
from .errors import SchemaError, SentButNotRecorded, UsageError
from .ports import Message, PortError
from .runtime import Ctx, assert_pmo_identity, one_line
from .timeutil import iso_utc

__all__ = [
    "ACK_PREFIX",
    "was_notified",
    "mark_notified",
    "is_secretary",
    "is_dispatch_channel",
    "item_channel",
    "is_call",
    "is_group_request",
    "should_poke",
    "parse_work_message",
    "extract_section",
    "build_ack_body",
    "build_call_ack_body",
    "build_report_body",
    "send_ack",
    "send_call_ack",
    "send_report",
    "process_messages",
    "one_line_for_event",
    "as_dict",
]

# --------------------------------------------------------------------------- #
# 常數（樣板規則原樣搬；身分與頻道不在此，一律照設定）
# --------------------------------------------------------------------------- #

#: design D6：只有派工頻道內、秘書身分、urgency ≥ 8 才叫醒 PMO。
POKE_MIN_URGENCY = 8

#: 叫人 ack 與 [poke] 各記一份「已處理來信 id」的去重帳，只保留最近 N 筆。
NOTIFIED_MAX = 200

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
TITLE_RE = re.compile(r"^# 工作：\s*(.+)$")

WORK_ID_RE = re.compile(r"^- 工作 ID：\s*([0-9A-HJKMNP-TV-Z]{26})\s*$", re.MULTILINE)
LEVEL_RE = re.compile(r"^- 建議層級：\s*([a-zA-Z0-9]+)\s*$", re.MULTILINE)

# 工作回報狀態（手動可通報進度：進行中、驗收中；預設回報結案：已完成、阻塞、失敗）
REPORT_STATUS_MAP = {
    "完成": "已完成",
    "已完成": "已完成",
    "進行中": "進行中",
    "驗收中": "驗收中",
    "阻塞": "阻塞",
    "失敗": "失敗",
}
VALID_SCHEDULES = ("立即開工", "排入待開工佇列")

# inbox list 顯示層映射（機讀狀態 -> 中文狀態）
STATUS_DISPLAY_MAP = {
    "scheduled": "已收件排程中",
    "已完成": "已完成",
    "進行中": "進行中",
    "驗收中": "驗收中",
    "阻塞": "阻塞",
    "失敗": "失敗",
}

#: team PM 回「收到」ack 的起頭字樣（派工信的第一則回覆就是它）。
ACK_PREFIX = "收到"


def as_dict(msg: Any) -> dict:
    """A message, as the core's dict shape: whatever the port handed over.

    The Angareion port speaks `pmo.ports.Message` records; the core keeps the
    flat dict shape the template rules were written against (and the tests
    build directly), so the record is un-marshalled once, at the boundary.
    Field names follow the current tool's (`id`／`from`／`content`…) because
    they are what the event lines and the ledger carry.
    """
    if isinstance(msg, Mapping):
        return {
            "id": str(msg.get("id") or ""),
            "channel": str(msg.get("channel") or ""),
            "from": str(msg.get("from") or msg.get("sender") or ""),
            "to": msg.get("to"),
            "content": str(msg.get("content") or msg.get("body") or ""),
            "received_at": str(msg.get("received_at") or ""),
            "urgency": msg.get("urgency"),
            "mentions": msg.get("mentions"),
        }
    if isinstance(msg, Message):
        return {
            "id": msg.message_id,
            "channel": msg.channel,
            "from": msg.sender,
            "to": msg.to,
            "content": msg.body,
            "received_at": msg.received_at,
            "urgency": msg.urgency,
            "mentions": None if msg.mentions is None else tuple(msg.mentions),
        }
    raise TypeError(f"not a message: {type(msg).__name__}")


# --------------------------------------------------------------------------- #
# 去重帳（`notified.call`／`notified.poke`）：帳本本身的讀寫在 `pmo.ledger`
# --------------------------------------------------------------------------- #


def _fix_notified(data: dict) -> None:
    """補齊既有的（MyPMO 寫的）舊檔缺的 `notified`；缺了就地補上。

    The compatibility fill itself is `ledger.normalize_state` (one rule for
    both the file and this in-memory helper, arch review 中-1); what is left
    here is the shape check, because a wrong-shaped bucket must stop the round
    instead of silently becoming an empty one (that would ack twice).
    """
    notified = ledger.normalize_state(data)["notified"]
    if not isinstance(notified, dict):
        raise SchemaError("state 檔格式錯誤：notified 必須是物件（dict）")
    for kind in ("call", "poke"):
        if not isinstance(notified[kind], dict):
            raise SchemaError(f"state 檔格式錯誤：notified.{kind} 必須是物件（dict）")


def was_notified(state: dict, kind: str, ref_id: str) -> bool:
    """這則來信（依 id）是否已處理過同類事件（`kind` = `call`／`poke`）。

    沒有 id 的訊息無法辨識來源，一律當成沒處理過（維持原行為，不誤擋）。
    """
    ref_id = str(ref_id or "")
    if not ref_id:
        return False
    _fix_notified(state)
    return ref_id in state["notified"][kind]


def mark_notified(state: dict, kind: str, ref_id: str, now: datetime | None = None) -> None:
    """記下已處理的來信 id；只保留最近 `NOTIFIED_MAX` 筆（dict 保序，砍最舊的）。"""
    ref_id = str(ref_id or "")
    if not ref_id:
        return
    _fix_notified(state)
    bucket: dict = state["notified"][kind]
    dt = now if now is not None else datetime.now(timezone.utc)
    bucket.pop(ref_id, None)
    bucket[ref_id] = iso_utc(dt)
    excess = len(bucket) - NOTIFIED_MAX
    for stale in list(bucket)[: max(excess, 0)]:
        del bucket[stale]


# --------------------------------------------------------------------------- #
# 分類（樣板解析；identity／頻道照設定）
# --------------------------------------------------------------------------- #


def extract_section(content: str, header: str) -> str:
    pattern = rf"^##\s+{re.escape(header)}\s*$(.*?)(?=^##|\Z)"
    m = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def is_secretary(msg: Mapping, config: Any) -> bool:
    return str(as_dict(msg).get("from") or "") == config.secretary_identity


def is_dispatch_channel(msg: Mapping, config: Any) -> bool:
    return str(as_dict(msg).get("channel") or "") == config.dispatch_channel


def item_channel(item: Mapping, config: Any) -> str:
    """收件匣項目要回信的 channel。沒記 channel 的是舊項目，一律來自派工頻道。"""
    return str(item.get("channel") or config.dispatch_channel)


def _looks_like_work(msg: Mapping) -> bool:
    """第一行是「# 工作：」＝看起來是派工（樣板壞了也走不合規回覆）。"""
    lines = [ln.strip() for ln in str(msg.get("content") or "").strip().split("\n") if ln.strip()]
    return bool(lines) and bool(TITLE_RE.match(lines[0]))


def _names(value: Any) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def is_call(msg: Mapping, config: Any) -> bool:
    """`to` 或 `mentions` 點到 PMO 才算叫人；廣播、自己發的都不算（design D3、D9）。

    派工 channel 內第一行是「# 工作：」的是派工，不是叫人；其餘（簡單提問）是叫人。
    """
    d = as_dict(msg)
    identity = config.pmo_identity
    if str(d.get("from") or "") == identity:
        return False
    if not (identity in _names(d.get("to")) or identity in _names(d.get("mentions"))):
        return False
    return not (is_dispatch_channel(d, config) and _looks_like_work(d))


def should_poke(msg: Mapping, config: Any) -> bool:
    """派工頻道內、from 是秘書身分、urgency ≥ 8（urgency 是寄件者自填，三條件都要）。"""
    if not (is_dispatch_channel(msg, config) and is_secretary(msg, config)):
        return False
    try:
        return int(as_dict(msg).get("urgency")) >= POKE_MIN_URGENCY
    except (TypeError, ValueError):
        return False


#: 群請求的首行（兩邊都收）：MyPMO／harness 的樣板是 `# 建群請求：…`／
#: `# 封存請求：…`（MyPMO `pmo/groups.py:45-46`、`atelier/pmo/pm/v4.md:126`），
#: 工單另外用 `[create]`／`[archive]` 指這兩類，所以兩種寫法都認。
#: 括號寫法的尾字只收單一群名（slug：`[create] grp-demo`，或裸的 `[create]`）：
#: 舊的 `\s.*` 會把 `[create] 一般叫人…` 這種任意句子吞成群請求（#362 收緊）。
#: 字元集與 MyPMO 的群名規則一致（`[A-Za-z0-9_-]`，不含 `.`；#362b 依審查低-1 收齊）。
GROUP_NAME_RE = r"[A-Za-z0-9][A-Za-z0-9_-]*"
GROUP_FIRST_RE = re.compile(
    rf"^(?:# (?:建群|封存)請求：\S*|\[(?:create|archive)\](?:[ \t]+{GROUP_NAME_RE})?)$"
)


def _first_line(content: str) -> str:
    for line in str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip():
            return line.strip()
    return ""


def is_group_request(msg: Mapping, config: Any) -> bool:
    """這則信是不是群請求（建群／封存）：派工頻道＋秘書＋點名 PMO＋首行吻合。

    T-2 只做「不吃」：群請求是獨立一類，**不回**機械 ack、不記進叫人去重、不寫
    帳本（design Q-9 (a)：帳本、步驟機、sweep 都在 MyPMO；`[group-handoff]` 這
    一行就是交接）。MyPMO 也在 `is_call` 之前分流，順序照它（MyPMO#233）。
    """
    d = as_dict(msg)
    if not (is_dispatch_channel(d, config) and is_secretary(d, config)):
        return False
    if config.pmo_identity not in _names(d.get("to")):
        return False
    return bool(GROUP_FIRST_RE.match(_first_line(d.get("content"))))


def parse_work_message(msg: Mapping, config: Any) -> dict | None:
    """解析一則派工訊息。

    若非派工頻道、非秘書寄送，或 `to` 沒有點名 PMO（含廣播件 `to: null`），
    回傳 None——廣播件不當工作處理（不開單、不 ack）。
    若為秘書訊息但格式不合規，回傳 {"valid": False, "reason": ..., "id": ...}。
    若合規，回傳完整解析欄位之 dict。
    """
    d = as_dict(msg)
    if str(d.get("channel") or "") != config.dispatch_channel:
        return None
    if str(d.get("from") or "") != config.secretary_identity:
        return None
    if config.pmo_identity not in _names(d.get("to")):
        return None

    msg_id = str(d.get("id") or "").strip()
    content = str(d.get("content") or "").replace("\r\n", "\n")

    lines = [ln.strip() for ln in content.strip().split("\n") if ln.strip()]
    first_line = lines[0] if lines else ""
    m_title = TITLE_RE.match(first_line)
    if not m_title:
        return {
            "valid": False,
            "reason": "標題不符（需為「# 工作：<簡短標題>」）",
            "id": msg_id,
            "raw": content,
        }
    title = m_title.group(1).strip()

    m_id = WORK_ID_RE.search(content)
    if not m_id:
        return {
            "valid": False,
            "reason": "內文缺少合法的「- 工作 ID：<26碼ULID>」",
            "id": msg_id,
            "raw": content,
        }
    body_ulid = m_id.group(1).strip()

    # 中 2 規範：front matter id 與內文工作 ID 必須一致
    if not ULID_RE.match(body_ulid):
        return {
            "valid": False,
            "reason": f"工作 ID 不是合法的 26 碼 ULID：{body_ulid}",
            "id": msg_id,
            "raw": content,
        }
    if msg_id != body_ulid:
        return {
            "valid": False,
            "reason": f"front matter id「{msg_id}」與內文工作 ID「{body_ulid}」不一致",
            "id": msg_id,
            "raw": content,
        }

    m_level = LEVEL_RE.search(content)
    level_raw = m_level.group(1).strip().lower() if m_level else ""
    # 審查收斂：若缺少或破損建議層級，一律視同 b 級處理
    level = level_raw if level_raw in ("b", "c", "d") else "b"

    spec = extract_section(content, "需求說明")
    notes = extract_section(content, "限制與備註")

    return {
        "valid": True,
        "id": body_ulid,
        "title": title,
        "level": level,
        "sender": config.secretary_identity,
        "received_at": str(d.get("received_at") or ""),
        "spec": spec,
        "notes": notes,
        "content": content,
    }


# --------------------------------------------------------------------------- #
# 樣板組合
# --------------------------------------------------------------------------- #


def build_ack_body(
    ulid: str,
    identity: str,
    issue: str | None = None,
    schedule: str = "排入待開工佇列",
) -> str:
    """收到工作 ack 樣板（嚴格遵循秘書 §5）。"""
    if schedule not in VALID_SCHEDULES:
        schedule = "排入待開工佇列"
    issue_text = issue or "評估排程中"
    return (
        f"# 收到工作：{ulid}\n\n"
        f"- 狀態：已收件排程中\n"
        f"- 處理者：{identity}\n"
        f"- 建立工單：{issue_text}\n"
        f"- 預估安排：{schedule}\n\n"
        f"PMO 已確認收件並排入工單樹，後續進度將透過本通道持續回報。\n"
    )


def build_call_ack_body(ref_id: str, identity: str, dispatch_channel: str) -> str:
    """派工 channel 外的叫人：機械 ack，只說看到了、這裡不派工、不建單。

    Both the identity and the dispatch channel come from the teams settings
    (design §5.1: the three constants the current tool hard-codes are settings
    here) — no default, so a caller cannot quietly ack as the wrong seat.
    """
    return (
        f"# 收到叫人：{ref_id}\n\n"
        f"- 處理者：{identity}\n"
        f"- 說明：叫人只回答，不會建單或派工；要派工請在 {dispatch_channel} 用工作樣板送。\n\n"
        f"PMO 已看到這則訊息，簡單問答會在下一次被叫醒時回覆。\n"
    )


def build_report_body(
    ulid: str,
    status: str,
    issue: str | None = None,
    pr: str | None = None,
    summary: str = "",
    next_steps: str = "",
) -> str:
    """工作回報樣板（嚴格遵循秘書 §5）。"""
    norm_status = REPORT_STATUS_MAP.get(status, status)
    issue_text = issue or "無"
    pr_text = pr or "無"
    summary_text = summary.strip() or "（無摘要）"
    next_text = next_steps.strip() or "無"

    return (
        f"# 工作回報：{ulid}\n\n"
        f"- 狀態：{norm_status}\n"
        f"- 相關工單：{issue_text}\n"
        f"- 相關 PR：{pr_text}\n\n"
        f"## 執行結果摘要\n"
        f"{summary_text}\n\n"
        f"## 待決或後續動作\n"
        f"{next_text}\n"
    )


# --------------------------------------------------------------------------- #
# 身分檢查與發送（經 Angareion port；本體不做外部存取）
# --------------------------------------------------------------------------- #


def send_ack(
    ctx: Ctx,
    ulid: str,
    issue: str | None = None,
    schedule: str = "排入待開工佇列",
    channel: str | None = None,
) -> str:
    """ack 送回來信的 channel，`in_reply_to` 指向派工訊息（id 即工作 ID）。"""
    assert_pmo_identity(ctx.config)
    body = build_ack_body(
        ulid, issue=issue, schedule=schedule, identity=ctx.config.pmo_identity
    )
    return ctx.deps.angareion.send(
        channel=channel or ctx.config.dispatch_channel,
        to=ctx.config.secretary_identity,
        body=body,
        urgency=5,
        in_reply_to=ulid,
    )


def send_call_ack(ctx: Ctx, msg: Mapping) -> str:
    assert_pmo_identity(ctx.config)
    msg_id = str(msg.get("id") or "")
    body = build_call_ack_body(
        msg_id,
        identity=ctx.config.pmo_identity,
        dispatch_channel=ctx.config.dispatch_channel,
    )
    return ctx.deps.angareion.send(
        channel=str(msg.get("channel") or ""),
        to=ctx.config.secretary_identity,
        body=body,
        urgency=5,
        in_reply_to=msg_id if ULID_RE.match(msg_id) else None,
    )


def send_report(
    ctx: Ctx,
    ulid: str,
    status: str,
    issue: str | None = None,
    pr: str | None = None,
    body_text: str = "",
) -> str:
    """向秘書發工作回報；成功時把狀態與回報時刻記回帳本。

    The letter goes out before the ledger is written (the current tool's order);
    if the write then fails, `SentButNotRecorded` carries the message id so the
    caller can say「已送出、帳本沒更新」instead of implying it never went out
    (Sonnet L-1).
    """
    assert_pmo_identity(ctx.config)

    if not ULID_RE.match(ulid):
        raise UsageError(f"工作 ID 不是合法的 26 碼 ULID：{ulid}")

    if status not in REPORT_STATUS_MAP:
        raise UsageError(
            f"不支援的工作回報狀態：{status}（可選：{', '.join(sorted(REPORT_STATUS_MAP))}）"
        )
    norm_status = REPORT_STATUS_MAP[status]

    with ledger.state_lock(ctx.state_dir):
        inbox_state = ledger.load_state(ctx.state_dir)
        item = inbox_state.get("items", {}).get(ulid)
        if not item:
            raise UsageError(f"收件匣中找不到工作 ID：{ulid}")

        # 解析 body_text（若已包含 ## 則分別提取，否則全數當摘要）
        if "## 執行結果摘要" in body_text:
            summary = extract_section(body_text, "執行結果摘要")
            next_steps = extract_section(body_text, "待決或後續動作")
        else:
            summary = body_text.strip()
            next_steps = ""

        body = build_report_body(
            ulid=ulid,
            status=norm_status,
            issue=issue or item.get("issue"),
            pr=pr or item.get("pr"),
            summary=summary,
            next_steps=next_steps,
        )
        res = ctx.deps.angareion.send(
            channel=item_channel(item, ctx.config),
            to=ctx.config.secretary_identity,
            body=body,
            urgency=5,
            in_reply_to=ulid,
        )
        item["status"] = norm_status
        item["reported_at"] = iso_utc(ctx.deps.now())
        if issue:
            item["issue"] = issue
        if pr:
            item["pr"] = pr
        try:
            ledger.save_state(ctx.state_dir, inbox_state, now=ctx.deps.now())
        except SchemaError as error:
            raise SentButNotRecorded(
                f"工作回報已送出（id={res}），但帳本寫不進去：{one_line_for_event(error)}。"
                f"不要重送，先修 state 檔再跑一次 `pmo inbox list`",
                message_id=res,
            ) from error
        return res


# --------------------------------------------------------------------------- #
# 批次處理收信（read 指令與 tick 共用；群請求留在 MyPMO，Q-9）
# --------------------------------------------------------------------------- #


def process_messages(
    ctx: Ctx, messages: Iterable, *, handoffs: list[str] | None = None
) -> list[str]:
    """處理訊息陣列中的秘書工作，回傳產生的事件字串清單。

    叫人 ack 與 [poke] 依來信 id 記在收件匣狀態檔（`notified.call`／`notified.poke`），
    游標遺失或 `--since` 補讀讓同一則再出現時，不會再回第二次機械 ack、也不會再標一次
    叫醒。送失敗的不記，下一輪重試。帳本一律在鎖內重新讀取（不用呼叫端手上的那份）。

    `handoffs` 是 tick（#358 T5-3）的收集清單：給了就把這批 `[group-handoff]`
    的訊息 id 依序放進去，呼叫端不必反解析事件字串。預設 `None` 時行為與
    既有呼叫端完全不變。
    """
    events: list[str] = []
    dirty = False
    messages = [msg if isinstance(msg, dict) else as_dict(msg) for msg in messages]
    decision_latest = latest_decision_answers(messages)
    with ledger.state_lock(ctx.state_dir):
        # 一律於鎖內讀取最新 state，避免使用外部傳入的 stale dict 覆蓋並行更新
        inbox_state = ledger.load_state(ctx.state_dir)
        items = inbox_state["items"]

        for msg in messages:
            msg_id = str(msg.get("id") or "")
            decision = decision_latest.get(id(msg))
            if decision is not None:
                # 決定題只印、不建單、不做任何有副作用的動作（答案不是授權）
                events.append(format_decision_event(msg, decision))
            if should_poke(msg, ctx.config):
                if was_notified(inbox_state, "poke", msg_id):
                    events.append(f"[dedupe] 秘書急件 id={msg_id} 已叫過，略過重複叫醒")
                else:
                    events.append(
                        f"[poke] 秘書急件 id={msg.get('id')} urgency={msg.get('urgency')}：叫醒 PMO"
                    )
                    mark_notified(inbox_state, "poke", msg_id, now=ctx.deps.now())
                    dirty = True
            # 群請求在 is_call **之前**分流（MyPMO#233 的位置）：不回機械 ack、
            # 不記成已處理，只印一行交接（Q-9 (a)；完整交接機制是 #358）
            if is_group_request(msg, ctx.config):
                events.append(f"[group-handoff] {msg_id}")
                if handoffs is not None:
                    handoffs.append(msg_id)
                continue
            if is_call(msg, ctx.config):
                # 叫人只回答：機械 ack 只回秘書身分，不建單、不進收件匣、不派工
                if is_secretary(msg, ctx.config):
                    if was_notified(inbox_state, "call", msg_id):
                        events.append(f"[secretary] 叫人 id={msg_id} 已 ack 過，略過重複 ack")
                    else:
                        events.append(
                            f"[secretary] 叫人 id={msg.get('id')} channel={msg.get('channel')}"
                        )
                        try:
                            send_call_ack(ctx, msg)
                        except (UsageError, SchemaError, PortError) as e:
                            events.append(f"[warn] 發送叫人 ack 失敗：{one_line_for_event(e)}")
                        else:
                            mark_notified(inbox_state, "call", msg_id, now=ctx.deps.now())
                            dirty = True
                continue
            parsed = parse_work_message(msg, ctx.config)
            if parsed is None:
                continue
            if not parsed["valid"]:
                reason = parsed.get("reason")
                events.append(
                    f"[secretary] [warn] 收到不合規秘書訊息 id={parsed.get('id')}：{reason}"
                )
                ctx.log(f"[inbox:unknown_format] 不合規訊息 id={parsed.get('id')}：{reason}")
                continue

            ulid = parsed["id"]
            if ulid in items:
                # 重複送達：只補發 ack，不重開工單，沿用既有 issue 與 schedule
                existing = items[ulid]
                events.append(f"[secretary] 重複工作 {ulid}，補發 ack")
                schedule = existing.get("schedule", "排入待開工佇列")
                try:
                    send_ack(
                        ctx, ulid, issue=existing.get("issue"), schedule=schedule,
                        channel=item_channel(existing, ctx.config),
                    )
                except (UsageError, SchemaError, PortError) as e:
                    events.append(f"[warn] 補發 ack 失敗：{one_line_for_event(e)}")
            else:
                # 新工作：先寫入領帳（保留去重紀錄防重複開單）再發送 ack
                events.append(
                    f"[secretary] 收到新工作 {ulid}：{parsed['title']}（{parsed['level']} 級）"
                )
                items[ulid] = {
                    "id": ulid,
                    "title": parsed["title"],
                    "level": parsed["level"],
                    "sender": parsed["sender"],
                    "channel": ctx.config.dispatch_channel,
                    "received_at": parsed["received_at"] or iso_utc(ctx.deps.now()),
                    "ack_at": None,
                    "status": "scheduled",
                    "schedule": "排入待開工佇列",
                    "issue": None,
                    "team": None,
                    "reported_at": None,
                    "spec": parsed.get("spec", ""),
                    "notes": parsed.get("notes", ""),
                }
                ledger.save_state(ctx.state_dir, inbox_state, now=ctx.deps.now())

                try:
                    send_ack(
                        ctx, ulid, issue=None, schedule="排入待開工佇列",
                        channel=item_channel(items[ulid], ctx.config),
                    )
                except (UsageError, SchemaError, PortError) as e:
                    events.append(f"[warn] 發送 ack 失敗：{one_line_for_event(e)}")
                else:
                    items[ulid]["ack_at"] = iso_utc(ctx.deps.now())
                    ledger.save_state(ctx.state_dir, inbox_state, now=ctx.deps.now())
                    events.append(f"[ack] 已向秘書發送 ack：收到工作 {ulid}")

        if dirty:
            ledger.save_state(ctx.state_dir, inbox_state, now=ctx.deps.now())

    return events


def one_line_for_event(exc: Exception, limit: int = 120) -> str:
    """事件行用的單行錯誤字樣（壓行、截長；字串一律再過 sanitize 才出得去）。"""
    return sanitize.sanitize(one_line(f"{type(exc).__name__}: {exc}", limit))
