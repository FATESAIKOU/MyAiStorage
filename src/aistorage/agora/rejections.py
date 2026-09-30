"""Agora 真本裡的收件匣拒收紀錄（`_committer/rejections/<item_key>.json`）。

依據規格：
- review-g3g M3：apply 產生的拒收必須與 evaluate 的 `reject_decision` **同形狀**，
  否則 evaluate 的拒收快取認不得，每一輪都會重新評估、重新拒收；
  `at` 只在第一次寫入，之後不得被這一輪的時間覆寫，否則
  `deletable_after` 永遠到不了，項目永遠不會被刪除。
- review-g3d M2（REJECT 生命週期）、docs/impl/group4-modules.md 第 4.4 節與
  PM 決定 4：`deletable_after` 以**項目檔案最早的 `created_time`** 為基準
  （Drive 的 metadata，寫入者無法控制），不是「當下 + 24h」。

檔案形狀（evaluate 與 apply 共用，publish 也能讀）：

    {
      "item_key": "...", "code": "...", "at": "<第一次拒收時間>",
      "item_id": "..." | null,
      "inbox_folder_id": "...", "candidate_ids": ["<file id>", ...],
      "entries": [ {"inbox_folder_id", "candidate_ids", "code", "at"}, ... ],
      "detail": "..." | <缺席>
    }

`entries` 是 fast path：同一個收件匣資料夾、同一組候選檔案（file id）只認第一筆，
寫入者換掉檔案（新的 id）才算新的候選。

`detail` 是給寫入端看的補充說明（目前只有預留上限用得到，review-55edd374 M3），
只放 session id 之類本來就公開的欄位，不放任何交接內容或訊息本文；它**不在**
`entries` 裡，也不參與 fast path 比對。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any, Iterable, Sequence

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore
from aistorage.clock import format_rfc3339, parse_rfc3339

#: 孤兒／拒收項目的保留小時數（D2：超過 24 小時即可刪除並永久刪除）。
REJECTION_RETENTION = timedelta(hours=24)


def candidate_ids(sidecars: Sequence[Any], sigs: Sequence[Any]) -> list[str]:
    """候選檔案的 file id（sidecar ＋ sig），排序後作為一次「評估嘗試」的指紋。"""
    return sorted(
        [f.id for f in list(sidecars) + list(sigs) if getattr(f, "id", None)]
    )


def earliest_created_at(files: Iterable[Any]) -> str | None:
    """取一組候選檔案最早的 `created_time`（RFC 3339 UTC，含小數）。

    這是 Drive 的 metadata，寫入者無法控制，所以可當作拒收時間的可信基準。
    檔案不存在或時間無法解析時回傳 None，由呼叫端退回「當下」。
    """
    stamps: list[str] = []
    for f in files:
        created = getattr(f, "created_at", None)
        if created is None:
            continue
        try:
            stamps.append(format_rfc3339(created, include_fraction=True))
        except (ValueError, AttributeError, TypeError):
            continue
    return min(stamps) if stamps else None


def rejection_base_time(files: Iterable[Any], fallback: str) -> str:
    """拒收時間的基準：檔案最早的 `created_time`，拿不到才用呼叫端給的時間。"""
    return earliest_created_at(files) or fallback


def deletable_after_for(files: Iterable[Any], rejected_at: str) -> datetime:
    """`deletable_after` = 基準時間 + 24 小時（docs 4.4／PM 決定 4）。

    基準時間取檔案最早的 `created_time`：一個已經在收件匣躺了三天的壞項目，
    這一輪被拒收時就已經滿 24 小時，不該再等一天。
    """
    base = rejection_base_time(files, rejected_at)
    try:
        return parse_rfc3339(base) + REJECTION_RETENTION
    except ValueError:
        return parse_rfc3339(rejected_at) + REJECTION_RETENTION


def _load_entries(rej_path) -> list[dict[str, Any]]:
    """讀出既有的 entries；舊的單筆格式也視為一筆 entry。"""
    if not rej_path.is_file():
        return []
    try:
        with open(rej_path, encoding="utf-8") as rf:
            data = json.load(rf)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        entries = data.get("entries")
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
        if "inbox_folder_id" in data:
            return [data]
    return []


def record_rejection(
    store: AgoraStore,
    *,
    item_key: str,
    code: str,
    at: str,
    item_id: str | None = None,
    inbox_folder_id: str = "",
    candidates: Sequence[str] = (),
    detail: str | None = None,
) -> dict[str, Any]:
    """寫入（或更新）一個項目的拒收紀錄，回傳寫入的內容。

    `at` 只在第一次寫入：同一組候選檔案重複被拒收時保留第一次的時間，
    讓 `deletable_after` 能夠到期、項目能被刪除。

    `detail` 是給寫入端看的補充說明（目前只有預留上限用得到，
    review-55edd374 M3），隨紀錄發佈到讀取視圖的 `rejections.detail`。它**每一輪
    都更新**：佔住額度的預留清單會變，舊的留著只會誤導。內容只有 session id，
    不帶任何交接內容或訊息本文（`entries` 那個 fast path 完全不含它）。
    """
    rel = layout.rejection_path(item_key)
    rej_path = store.worktree / rel
    existing_entries = _load_entries(rej_path)

    already = any(
        e.get("inbox_folder_id") == inbox_folder_id
        and e.get("candidate_ids") == list(candidates)
        for e in existing_entries
    )
    if not already:
        existing_entries.append({
            "inbox_folder_id": inbox_folder_id,
            "candidate_ids": list(candidates),
            "code": code,
            "at": at,
        })

    # at 取既有紀錄的第一次時間（不得被這一輪覆寫）
    first_at = at
    for e in existing_entries:
        prev = e.get("at")
        if isinstance(prev, str) and prev:
            first_at = prev
            break

    payload: dict[str, Any] = {
        "item_key": item_key,
        "code": code,
        "at": first_at,
        "item_id": item_id,
        "inbox_folder_id": inbox_folder_id,
        "candidate_ids": list(candidates),
        "entries": existing_entries,
    }
    if detail:
        payload["detail"] = detail
    store.put_json(rel, payload)
    return payload
