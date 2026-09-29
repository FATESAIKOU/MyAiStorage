"""讀取介面：新鮮度判定純函式（tasks 4.4）。

規則（design D9、ADR 0007、spec；轉寫自 schemas/searchindex.md 第 6 節）：
- max_lag is None → satisfied=None，不警告，但一定附 snapshot_at。
- status == "stopped" 而且 snapshot_at >= stopped_at（datetime 比較）
  → satisfied=True、stopped_ok=True，不論落後多久。
- status == "reserved"（checkout 預留好、還沒有人開工）：期限之內 → satisfied=True
  （**沒有東西會再變動**，不該用落後時間去警告人）；期限已過 → 警告「預留已過期」
  （review-2bc0785 M2；期限只是顯示與管理用的訊號，提交流程不會自動刪除）。
- 否則 now − snapshot_at <= max_lag → 符合；不符合 → satisfied=False＋warning。
- 交接單、Link、拒收這類非單一 Session 結果，呼叫端以該世代 published_at
  當 snapshot_at 傳入。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aistorage.clock import parse_rfc3339


def _format_lag(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 90:
        return f"{total}s"
    minutes = (total + 30) // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def evaluate_freshness(*, snapshot_at: str | None, status: str | None,
                       stopped_at: str | None, generation: int,
                       published_at: str, now: datetime,
                       max_lag: timedelta | None,
                       reserved_until: str | None = None) -> dict:
    """回傳 Freshness 欄位 dict（由 reader 組成 Freshness dataclass）。"""
    base = {
        "snapshot_at": snapshot_at,
        "generation": generation,
        "published_at": published_at,
        "satisfied": None,
        "warning": None,
        "stopped_ok": False,
    }
    if max_lag is None:
        return base
    if status == "stopped" and snapshot_at and stopped_at:
        try:
            if parse_rfc3339(snapshot_at) >= parse_rfc3339(stopped_at):
                return {**base, "satisfied": True, "stopped_ok": True}
        except ValueError:
            pass
    if status == "reserved" and reserved_until:
        # 預留是零則訊息的空紀錄，本來就沒有東西會再變動 → 用期限判斷，不看落後
        try:
            deadline = parse_rfc3339(reserved_until)
        except ValueError:
            return {**base, "satisfied": False,
                    "warning": "expired: 預留期限格式錯誤，無法判定"}
        if now <= deadline:
            return {**base, "satisfied": True}
        return {**base, "satisfied": False,
                "warning": f"expired: 預留已過期（期限 {reserved_until}），"
                           "還沒有人開工"}
    if not snapshot_at:
        return {**base, "satisfied": False,
                "warning": "stale: 沒有快照時間，無法判定新鮮度"}
    try:
        lag = (now - parse_rfc3339(snapshot_at)).total_seconds()
    except ValueError:
        return {**base, "satisfied": False,
                "warning": "stale: 快照時間格式錯誤，無法判定新鮮度"}
    if lag <= max_lag.total_seconds():
        return {**base, "satisfied": True}
    want = _format_lag(max_lag.total_seconds())
    return {**base, "satisfied": False,
            "warning": f"stale: 快照落後 {_format_lag(lag)}，要求 {want}"}
