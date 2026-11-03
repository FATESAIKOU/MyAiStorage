"""`pmo inbox rebuild`：帳本從 Angareion 的過去重建（design §1.2、§3.2；T-3）。

D-5：收件帳本以 Angareion 為準，本機是快取，可以重建。重建的三個依據（§1.2，
全部經 `pmo.ports.AngareionPort.log`，不動游標）：

- 列舉派工信：`log --channel <派工 channel> --to <PMO>`；
- 單一工作的機械 ack 與回報：`log --from <PMO> --thread <工作 ID>`；
- 叫人 ack（拿 `notified.call`）：`log --from <PMO> --grep <# 收到叫人：> 開頭`
  ——它的 `in_reply_to` 是被叫的來信 id、`received_at` 是 ack 的時刻。

能重建／不能重建照 §1.2 的表：`team` 重建不出來（留空並由回報列出）；
`notified.poke` 重建不出來（重建後同一則急件可能再標一次 `[poke]`，只是多
叫醒一次，無副作用）。重建完在帳本上記 `rebuilt_at`（§3.4 新欄位）。

語意照現行工具：同一個工作 ID 再次讀到時「補發 ack、沿用既有的工單與排程、
不重開工單」——所以補發的 ack 要能沿用重建出來的 `issue`／`schedule`（§1.2
的最後一段；驗收測試在 `tests/unit/test_pmo_rebuild.py`）。

樣板的**回解**只吃 PMO 寄出的信的**已知欄位首行**（`# 收到工作：`／
`# 工作回報：`／`# 收到叫人：`），不是反解析全文；佔位字（「評估排程中」、
「無」）照 `build_ack_body`／`build_report_body` 的寫法回 None。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from . import inbox, ledger
from .runtime import Ctx
from .timeutil import iso_utc

__all__ = [
    "CALL_ACK_MARK",
    "PLACEHOLDER_ISSUE",
    "PLACEHOLDER_PR",
    "RebuildReport",
    "collect_call_acks",
    "collect_letters",
    "collect_pmo_letters_on_thread",
    "merge_rebuild",
    "parse_ack_body",
    "parse_call_ack_body",
    "parse_report_body",
    "rebuild",
]

#: 叫人 ack 的第一行（`inbox.build_call_ack_body` 寫出來的樣式）；重建拿它當
#: `angareion log --grep` 的字樣，別的信不吃到。
CALL_ACK_MARK = "# 收到叫人："

#: 樣板佔位字（照 `build_ack_body`／`build_report_body` 的寫法）：出現它等於
#: 「這欄沒有值」，重建回 None。
PLACEHOLDER_ISSUE = "評估排程中"
PLACEHOLDER_PR = "無"

CALL_ACK_TITLE_RE = re.compile(rf"^{re.escape(CALL_ACK_MARK)}\s*(\S+)\s*$", re.MULTILINE)
ACK_TITLE_RE = re.compile(r"^# 收到工作：\s*(\S+)\s*$", re.MULTILINE)
REPORT_TITLE_RE = re.compile(r"^# 工作回報：\s*(\S+)\s*$", re.MULTILINE)
ACK_ISSUE_RE = re.compile(r"^- 建立工單：\s*(.*?)\s*$", re.MULTILINE)
ACK_SCHEDULE_RE = re.compile(r"^- 預估安排：\s*(.*?)\s*$", re.MULTILINE)
REPORT_STATUS_RE = re.compile(r"^- 狀態：\s*(.*?)\s*$", re.MULTILINE)
REPORT_ISSUE_RE = re.compile(r"^- 相關工單：\s*(.*?)\s*$", re.MULTILINE)
REPORT_PR_RE = re.compile(r"^- 相關 PR：\s*(.*?)\s*$", re.MULTILINE)


def _ts(text: object) -> datetime | None:
    """ISO 時刻，能拆才比（拆不開的當「不可定序」，規則寫在使用處）。"""
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _order_key(text: str) -> datetime:
    """挑最新／trim 用的可比時刻；拆不開的視為最早（文案無時刻的先放吃不到）。"""
    return _ts(text) or datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 樣板回解（PMO 寄出的信 → 帳本欄位；只讀純函式）
# --------------------------------------------------------------------------- #


def _value_of(pattern: re.Pattern, body: str) -> str | None:
    match = pattern.search(body)
    return match.group(1).strip() if match else None


def _opt_field(value: str | None, *placeholders: str) -> str | None:
    """樣板的空值（「無」）與給定的佔位字都當「沒有值」回 None。"""
    if value is None or value == "" or value in (PLACEHOLDER_PR, *placeholders):
        return None
    return value


def parse_ack_body(body: str) -> dict | None:
    """機械 ack 的本文 → 工作 ID、建立工單、預估安排。

    不是 ack 的本文回 None。工單是佔位字（「評估排程中」）時回 None，照正路
    `process_messages` 寫進帳本的 `issue: None`；安排不在兩個合法值內的丟掉
    （樣板保證是其中之一，這裡只是不把壞值當真）。
    """
    match = ACK_TITLE_RE.search(body)
    if not match:
        return None
    schedule = _value_of(ACK_SCHEDULE_RE, body)
    return {
        "work_id": match.group(1),
        "issue": _opt_field(_value_of(ACK_ISSUE_RE, body), PLACEHOLDER_ISSUE),
        "schedule": schedule if schedule in inbox.VALID_SCHEDULES else None,
    }


def parse_report_body(body: str) -> dict | None:
    """工作回報的本文 → 工作 ID、狀態、相關工單、相關 PR。

    狀態不是回報樣板的合法值時丟掉（無法確信）；工單與 PR 是「無」回 None。
    """
    match = REPORT_TITLE_RE.search(body)
    if not match:
        return None
    status = _value_of(REPORT_STATUS_RE, body)
    return {
        "work_id": match.group(1),
        "status": status if status in inbox.REPORT_STATUS_MAP.values() else None,
        "issue": _opt_field(_value_of(REPORT_ISSUE_RE, body)),
        "pr": _opt_field(_value_of(REPORT_PR_RE, body)),
    }


def parse_call_ack_body(body: str) -> str | None:
    """叫人機械 ack 的本文 → 被叫那則信的 id（`notified.call` 的 key）。"""
    match = CALL_ACK_TITLE_RE.search(body)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# 從 log 記錄蒐集（純函式：吃 Message／dict，吐候選欄位）
# --------------------------------------------------------------------------- #


def collect_letters(messages: list, config) -> dict[str, dict]:
    """派工信的候選欄位，以工作 ID 為 key；同一工作 ID 重送留最早收到的。"""
    letters: dict[str, dict] = {}
    for msg in messages:
        data = inbox.as_dict(msg)
        work = inbox.parse_work_message(data, config)
        if work is None or not work.get("valid"):
            continue
        ulid = str(work["id"])
        candidate = {
            "id": ulid,
            "title": str(work["title"]),
            "level": str(work["level"]),
            "sender": str(work["sender"]),
            "channel": str(data.get("channel") or ""),
            "received_at": str(work["received_at"] or ""),
            "spec": str(work.get("spec", "")),
            "notes": str(work.get("notes", "")),
        }
        current = letters.get(ulid)
        if current is None:
            letters[ulid] = candidate
            continue
        new_dt, cur_dt = _ts(candidate["received_at"]), _ts(current["received_at"])
        if new_dt is not None and cur_dt is not None and new_dt < cur_dt:
            letters[ulid] = candidate
    return letters


def _keep_latest(entries: list[tuple[str, dict]]) -> dict | None:
    """時刻最新的一份（拆不開時刻的當最早；同時刻後到的贏，log 是照時間序）。"""
    if not entries:
        return None
    best = entries[0]
    for candidate in entries[1:]:
        if _order_key(candidate[0]) >= _order_key(best[0]):
            best = candidate
    return dict(best[1])


def collect_pmo_letters_on_thread(
    messages: list, pmo_identity: str, work_id: str
) -> tuple[dict | None, dict | None]:
    """一個工作的 thread 裡，PMO 的機械 ack 與回報（各留時刻最新的）。

    回的 ack 帶 `ack_at`（那封信的 `received_at`）：現行工具的 `ack_at` 就是
    發 ack 當下。首行工作 ID 與要重建的工作不符的信不吃——log 的 `--thread`
    之外多一道保險，假物件不過濾參數時也守得住。
    """
    acks: list[tuple[str, dict]] = []
    reports: list[tuple[str, dict]] = []
    for msg in messages:
        data = inbox.as_dict(msg)
        if str(data.get("from") or "") != pmo_identity:
            continue
        body = str(data.get("content") or "")
        when = str(data.get("received_at") or "")
        ack = parse_ack_body(body)
        if ack is not None and ack["work_id"] == work_id:
            acks.append((when, dict(ack, ack_at=when or None)))
            continue
        report = parse_report_body(body)
        if report is not None and report["work_id"] == work_id:
            reports.append(
                (when, dict(report, reported_at=when or None))
            )
    return _keep_latest(acks), _keep_latest(reports)


def collect_call_acks(messages: list, pmo_identity: str) -> dict[str, str]:
    """PMO 寄出的叫人 ack → `{來信 id: ack 時刻}`（`notified.call` 的重建源）。

    依據 §1.2：叫人 ack 的 `in_reply_to` 是被叫的來信 id。`in_reply_to` 缺了
    才退回首行那個 id（正路一定兩個都有）；都沒有的吃不下，留給下輪重 ack
    （去重帳少一筆的後果是多 ack 一次，不丟東西）。
    """
    calls: dict[str, str] = {}
    for msg in messages:
        data = inbox.as_dict(msg)
        if str(data.get("from") or "") != pmo_identity:
            continue
        body = str(data.get("content") or "")
        ref = str(data.get("in_reply_to") or "") or (parse_call_ack_body(body) or "")
        if not ref:
            continue
        when = str(data.get("received_at") or "")
        if ref not in calls or _order_key(when) >= _order_key(calls[ref]):
            calls[ref] = when
    return calls


# --------------------------------------------------------------------------- #
# 併帳（在帳本鎖內做；純函式：吃已驗過的帳本狀態 dict）
# --------------------------------------------------------------------------- #


#: 只能由 log 重建的欄位：log 回 None 代表「這次查不到」，不是「值沒了」。
_LOG_ITEM_FIELDS = ("ack_at", "schedule", "issue", "status", "reported_at", "pr")


def _item_of(letter: Mapping, ack: Mapping | None, report: Mapping | None) -> dict:
    """重建出來的一筆項目（欄位照 §3.4；`team` 一律空，§1.2）。

    log 沒說的欄位留 None：更新既有項目時它代表「這次查不到，別洗掉本機的
    值」（`_keep_local`）；新增的項目再由 `merge_rebuild` 補 `status` 的預設值。
    """
    ack_at = ack["ack_at"] if ack else None
    schedule = ack["schedule"] if ack else None
    issue = (ack["issue"] if ack else None) or (report["issue"] if report else None)
    status, reported_at, pr = None, None, None
    if report is not None:
        status = report["status"]
        reported_at = report["reported_at"]
        pr = report["pr"]
    return {
        "id": letter["id"],
        "title": letter["title"],
        "level": letter["level"],
        "sender": letter["sender"],
        "channel": letter["channel"],
        "received_at": letter["received_at"],
        "ack_at": ack_at,
        "schedule": schedule,
        "issue": issue,
        "status": status,
        "reported_at": reported_at,
        "pr": pr,
        # §1.2：team 重建不出來（回報樣板沒有這一欄）——留空，由回報列出。
        "team": None,
        "spec": letter.get("spec", ""),
        "notes": letter.get("notes", ""),
    }


def _keep_local(existing: Mapping, rebuilt: Mapping) -> dict:
    """更新既有項目：log 回 None 的欄位保留既有的值，只覆寫 log 有值的欄位。

    後端的 log 有 limit，也可能根本沒有那封信（§1.2：過去沒帶 `in_reply_to`
    的舊 ack 重建不出來）。整筆重造會把本機的工單與排程洗成 None、狀態退回
    `scheduled`，下一輪 read 同一張工作就會補發 ack 並可能重開工單。
    """
    merged = dict(rebuilt)
    for field in _LOG_ITEM_FIELDS:
        if merged[field] is None:
            merged[field] = existing.get(field)
    return merged


def _trim_calls(call_acks: Mapping[str, str]) -> dict[str, str]:
    """重建出的叫人去重帳，只留最近 `NOTIFIED_MAX` 筆（照 `mark_notified` 的帳）。"""
    ordered = sorted(call_acks.items(), key=lambda entry: (_order_key(entry[1]), entry[0]))
    kept = ordered[-inbox.NOTIFIED_MAX :] if len(ordered) > inbox.NOTIFIED_MAX else ordered
    return {ref: when for ref, when in kept}


def merge_rebuild(
    state: dict,
    letters: Mapping[str, Mapping],
    acks: Mapping[str, Mapping | None],
    reports: Mapping[str, Mapping | None],
    call_acks: Mapping[str, str],
    now: datetime,
    since: str | None = None,
) -> RebuildReport:
    """把重建結果**併**進帳本狀態（就地改 `state`）。

    併、不整本換：「刪掉帳本 → rebuild」就是整本重來；不刪檔時，帳本裡 log
    沒涵蓋到的既有項目留著（後端的 log 有 limit，沒看到的項目當還在快取裡）。
    更新既有項目時同一條規則落到欄位：log 回 None 的欄位（issue／schedule／
    ack_at／status／reported_at／pr）留舊值，只覆寫 log 有值的那幾個——否則
    查不到舊 ack 的項目會被洗回 `scheduled`，下一輪 read 就補發 ack、可能重開
    工單。`--since` 時同樣只把窗口內當成事實：叫人去重帳窗口外的既有紀錄留著，
    否則重建會讓 PMO 對早就 ack 過的叫人再 ack 一次。
    """
    items: dict = state["items"]
    added: list[str] = []
    updated: list[str] = []
    for ulid, letter in letters.items():
        rebuilt = _item_of(letter, acks.get(ulid), reports.get(ulid))
        if ulid in items:
            updated.append(ulid)
            items[ulid] = _keep_local(items[ulid], rebuilt)
        else:
            added.append(ulid)
            if rebuilt["status"] is None:  # 沒回報的新工作＝已收件排程中（照正路）
                rebuilt["status"] = "scheduled"
            items[ulid] = rebuilt
    # §1.2：team 重建不出來，重建到的每一筆都留空——不論原本有沒有值、是不是
    # 這次才新增的（刪掉帳本重來時每一筆都是新的，PMO 照樣要知道它們沒有
    # team），所以列出的就是這次重建到的全部項目。
    team_blank = list(letters)

    notified = ledger.normalize_state(state)["notified"]
    rebuilt_calls = _trim_calls(call_acks)
    if since is None:
        notified["call"] = rebuilt_calls  # 整段重建：log 就是全部
    else:
        merged_calls = dict(notified.get("call") or {})
        merged_calls.update(rebuilt_calls)
        notified["call"] = _trim_calls(merged_calls)
    poke_lost = len(notified.get("poke", {}))
    # §1.2：急件叫醒去重沒有寄信、重建不出來；留空並反映在回報上
    #（後果是同一則急件可能再標一次 [poke]，只多叫醒一次，無副作用）。
    notified["poke"] = {}
    state["rebuilt_at"] = iso_utc(now)
    return RebuildReport(
        added=added,
        updated=updated,
        team_blank=team_blank,
        call_notified=len(notified["call"]),
        poke_lost=poke_lost,
    )


@dataclass(frozen=True)
class RebuildReport:
    """一次重建的差異，給指令印出去。"""

    added: list[str]
    updated: list[str]
    team_blank: list[str]
    call_notified: int
    poke_lost: int


# --------------------------------------------------------------------------- #
# 指令本體（經 Angareion 埠；不動帳本以外的狀態）
# --------------------------------------------------------------------------- #


def rebuild(ctx: Ctx, since: str | None = None, dry_run: bool = False) -> RebuildReport:
    """收件帳本重建：先驗帳本 → 列舉 → 每個工作一個 thread → 叫人帳 → 併帳。

    `--since` 遞給每一次 log（後端做時間下界，§3.3 的 log 合約）；`dry_run`
    只算不寫。log 讀不到／輸出不合格式時 `PortError` 往上丟、帳本壞掉
    `SchemaError` 往上丟（設計 §3.2 的 exit 2，由 cli 的收尾對應）。
    """
    ledger.load_state(ctx.state_dir)  # 先驗帳本：壞了在打 log 之前就擋（exit 2）

    letters = collect_letters(
        ctx.deps.angareion.log(
            channel=ctx.config.dispatch_channel, to=ctx.config.pmo_identity, since=since
        ),
        ctx.config,
    )
    call_acks = collect_call_acks(
        ctx.deps.angareion.log(
            sender=ctx.config.pmo_identity, grep=CALL_ACK_MARK, since=since
        ),
        ctx.config.pmo_identity,
    )

    acks: dict[str, Mapping | None] = {}
    reports: dict[str, Mapping | None] = {}
    for ulid in letters:
        ack, report = collect_pmo_letters_on_thread(
            ctx.deps.angareion.log(sender=ctx.config.pmo_identity, thread=ulid, since=since),
            ctx.config.pmo_identity,
            ulid,
        )
        acks[ulid] = ack
        reports[ulid] = report

    now = ctx.deps.now()
    with ledger.state_lock(ctx.state_dir):
        state = ledger.load_state(ctx.state_dir)  # 鎖內重讀，不蓋掉並行的寫入
        report = merge_rebuild(state, letters, acks, reports, call_acks, now, since=since)
        if not dry_run:
            ledger.save_state(ctx.state_dir, state, now=now)
    return report
