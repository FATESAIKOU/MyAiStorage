"""AiStorage 時鐘協定與實作模組。

依據規格：
- docs/impl/group3-modules.md 第 1、2.3 節
- review-g3a.md M3（提供 datetime 介面、parse_rfc3339、format_rfc3339 與 advance）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


def parse_rfc3339(s: str) -> datetime:
    """解析 RFC 3339 時間字串為帶時區之 UTC datetime。

    支援 Z、毫秒、微秒、奈秒（超過 6 位小數安全截斷）與時區偏移。
    """
    s_clean = s.strip()
    if "." in s_clean:
        base, frac_and_tz = s_clean.split(".", 1)
        if "Z" in frac_and_tz:
            frac, _ = frac_and_tz.split("Z", 1)
            tz = "+00:00"
        elif "+" in frac_and_tz:
            frac, rest = frac_and_tz.split("+", 1)
            tz = "+" + rest
        elif "-" in frac_and_tz:
            frac, rest = frac_and_tz.split("-", 1)
            tz = "-" + rest
        else:
            frac = frac_and_tz
            tz = "+00:00"
        frac_6 = (frac[:6] + "000000")[:6]
        s_clean = f"{base}.{frac_6}{tz}"
    else:
        s_clean = s_clean.replace("Z", "+00:00")

    dt = datetime.fromisoformat(s_clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_rfc3339(dt: datetime, *, include_fraction: bool = False) -> str:
    """格式化 datetime 為標準 RFC 3339 UTC 字串。"""
    utc_dt = dt.astimezone(timezone.utc)
    if include_fraction and utc_dt.microsecond > 0:
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@runtime_checkable
class Clock(Protocol):
    """時鐘介面協定。"""

    def now(self) -> datetime:
        """取得當前 UTC datetime（包含 timezone.utc）。"""
        ...

    def now_utc(self) -> str:
        """取得當前 UTC 時間之 RFC 3339 字串（格式：YYYY-MM-DDTHH:MM:SSZ）。"""
        ...


class SystemClock:
    """真實系統時鐘。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_utc(self) -> str:
        return format_rfc3339(self.now())


class FixedClock:
    """單元測試用可前進時鐘。"""

    def __init__(self, time_val: str | datetime = "2026-09-27T08:00:00Z") -> None:
        if isinstance(time_val, str):
            self._dt = parse_rfc3339(time_val)
        else:
            self._dt = time_val.astimezone(timezone.utc) if time_val.tzinfo else time_val.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._dt

    def now_utc(self) -> str:
        return format_rfc3339(self._dt)

    def advance(self, seconds: float) -> None:
        """M3: 將時鐘向前推進指定秒數。"""
        self._dt += timedelta(seconds=seconds)

    def set_time(self, time_val: str | datetime) -> None:
        if isinstance(time_val, str):
            self._dt = parse_rfc3339(time_val)
        else:
            self._dt = time_val.astimezone(timezone.utc) if time_val.tzinfo else time_val.replace(tzinfo=timezone.utc)
