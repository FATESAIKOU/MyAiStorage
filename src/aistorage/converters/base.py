"""AiStorage 轉換器基礎協定與資料結構。

依據規格：
- docs/impl/group3-modules.md 第 5 節
- review-g3b.md H2（定義 ConversionError）、M1（snapshot_sha256 計算）
- SessionFacts: 來源 Session 關鍵事實資料結構
- Converter: 轉換器介面協定
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class ConversionError(ValueError):
    """轉換器處理原始紀錄時發現結構不符、角色無效或必要欄位缺失之例外。"""


@dataclass(frozen=True)
class SessionFacts:
    """來源 Session 原始紀錄之關鍵事實摘要。"""

    title: str | None
    created_at: str | None
    updated_at: str | None
    message_ids: tuple[str, ...]          # 閱讀版的順序
    archived_at: str | None               # opencode 的 time.archived（>0 才有值，僅供顯示）
    last_message_at: str | None           # 僅供顯示
    in_progress: bool
    archived_ms: int | None = None        # R3: 毫秒整數時間戳，用於判定「封存之後是否有新訊息」
    last_message_ms: int | None = None    # R3: 毫秒整數時間戳，取自所有訊息時間的最大值


@runtime_checkable
class Converter(Protocol):
    """來源原始紀錄至閱讀版轉換器協定。

    `facts` 與 `child_session_ids` 都接受 keyword-only 的 `session_id`
    （`<source>:<source_session_id>`）：由呼叫端明確提供，轉換器不推測自己的
    Session id（review-g3f L3）。Claude Code 轉換器用它做「子代理不得指向自己」
    的防護；opencode 的子代理 id 直接來自原始紀錄，不使用這個參數。
    """

    source: str                           # 例如 "opencode"、"claude-code"

    def facts(self, raw_path: Path, *, session_id: str | None = None) -> SessionFacts:
        """自原始紀錄提取 SessionFacts（純函式，不碰網路與 git）。"""
        ...

    def convert(
        self,
        raw_path: Path,
        *,
        session_id: str,
        parent_id: str | None = None,
        snapshot_sha256: str | None = None,
    ) -> dict:
        """將原始紀錄轉換為 aistorage.reading/v1 閱讀版字典。

        snapshot_sha256 由轉換器自 raw_path 位元組計算（唯一來源）。
        參數 snapshot_sha256 僅供測試交叉檢查使用，提交流程中不傳此參數。
        轉換結果必須通過 aistorage.reading.validate_reading 驗證。
        """
        ...

    def child_session_ids(self, raw_path: Path, *, session_id: str | None = None) -> tuple[str, ...]:
        """自原始紀錄中提取所有子代理 Session ID 清單。"""
        ...
