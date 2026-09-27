"""組交接單時的接續點（docs/impl/group5-7 第 4.2 節、D10）。

接續點＝「被接續 Session 的快照裡，最後一則**已完成**的訊息」。

實作上**不自己重寫判定**：直接用同一個轉換器產生閱讀版，然後在閱讀版的
`messages` 上找「`completed` 為真且 `reverted` 為假」的最後一則。這正是
`agora/apply.py` 的 `_is_last_completed` 讀的那個清單（`message_id`、
`index`、`completed`、`reverted`），所以兩邊一定一致——自己重寫一份就會漂移。

寫交接單的那一次回覆本身還在生成中（assistant 訊息 `time.completed` 還沒寫入，
所以閱讀版裡 `completed=False`），自然不會被算進去（D10）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aistorage.converters.base import Converter


@dataclass(frozen=True)
class ContinuationPoint:
    """接續點。"""

    session_id: str
    snapshot_sha256: str
    message_id: str
    message_at: str | None = None

    def continuation(self) -> dict[str, str]:
        """`build_handoff_item(continuation=…)` 需要的形狀。"""
        return {
            "snapshot_sha256": self.snapshot_sha256,
            "message_id": self.message_id,
        }


def last_completed_message_id(reading: dict) -> tuple[str | None, str | None]:
    """自閱讀版回傳（message id, 時間）；沒有可用訊息就 (None, None)。

    與 `agora/apply.py::_is_last_completed` 用同一份欄位語意：`index` 決定順序，
    `completed` 與 `reverted` 決定這則訊息算不算數。
    """
    messages = reading.get("messages")
    if not isinstance(messages, list):
        return None, None
    indexed: list[tuple[int, str, str | None]] = []
    for position, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        if not m.get("completed", False) or m.get("reverted", False):
            continue
        mid = m.get("message_id")
        if not isinstance(mid, str) or not mid:
            continue
        index = m.get("index")
        order = index if isinstance(index, int) else position
        at = m.get("created_at") or m.get("completed_at")
        indexed.append((order, mid, at if isinstance(at, str) else None))
    if not indexed:
        return None, None
    _, mid, at = max(indexed, key=lambda row: row[0])
    return mid, at


def continuation_point(
    converter: Converter,
    raw_path: Path,
    *,
    session_id: str,
    snapshot_sha256: str,
    parent_id: str | None = None,
) -> ContinuationPoint | None:
    """組出接續點；沒有可用的已完成訊息就回 None（呼叫端要明確拒絕，不要猜）。

    轉換失敗會**往外拋 ConversionError**：那是原始紀錄有問題，呼叫端必須知道，
    不能假裝成「沒有接續點」而寫出一張壞交接單。
    """
    reading = converter.convert(
        raw_path, session_id=session_id, parent_id=parent_id,
        snapshot_sha256=snapshot_sha256,
    )
    mid, at = last_completed_message_id(reading)
    if not mid:
        return None
    return ContinuationPoint(
        session_id=session_id, snapshot_sha256=snapshot_sha256,
        message_id=mid, message_at=at,
    )
