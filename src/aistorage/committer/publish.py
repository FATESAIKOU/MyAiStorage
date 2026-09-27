"""AiStorage 讀取視圖發佈協定與空實作模組。

依據規格：
- docs/impl/group3-modules.md 第 7 節
- design D2、D5（發佈讀取視圖與搜尋索引）
- 第 3 組先以 NullPublisher 作為佔位，第 4 組實作完整發佈邏輯
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from aistorage.agora.store import AgoraStore


@runtime_checkable
class ReadViewPublisher(Protocol):
    """讀取視圖發佈器協定。"""

    def publish(self, store: AgoraStore, *, dry_run: bool = False, **kwargs: Any) -> None:
        """發佈讀取視圖與搜尋索引。

        Args:
            store: AgoraStore 執行個體。
            dry_run: 若為 True 則僅評估，不實際更新發佈資料夾與索引。
            kwargs: 額外擴充參數。
        """
        ...


class NullPublisher(ReadViewPublisher):
    """第 3 組空實作發佈器（無操作）。"""

    def publish(self, store: AgoraStore, *, dry_run: bool = False, **kwargs: Any) -> None:
        """空實作，不執行任何外部發佈。"""
        pass
