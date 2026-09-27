"""AiStorage 時鐘協定與實作模組。

依據規格：docs/impl/group3-modules.md 第 1、2.3 節
- Clock: 協定（now_utc 回傳 RFC 3339 UTC 字串）
- SystemClock: 真實系統時鐘
- FixedClock: 單元測試用固定時鐘
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """時鐘介面協定。"""

    def now_utc(self) -> str:
        """取得當前 UTC 時間之 RFC 3339 字串（格式：YYYY-MM-DDTHH:MM:SSZ）。"""
        ...


class SystemClock:
    """真實系統時鐘。"""

    def now_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FixedClock:
    """單元測試用固定時鐘。"""

    def __init__(self, time_str: str = "2026-09-27T08:00:00Z") -> None:
        self._time_str = time_str

    def set_time(self, time_str: str) -> None:
        self._time_str = time_str

    def now_utc(self) -> str:
        return self._time_str
