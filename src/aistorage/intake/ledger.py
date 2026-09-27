"""AiStorage 已處理收件匣清冊 (Ledger) 模組。

依據規格：
- docs/impl/group3-modules.md 第 4.3 節
- design D2、schemas/README.md（清冊格式、留存期 3 個月、防重放、REJECT 亦記錄）
- review-g3d H2（清冊損毀嚴格中止拋出 MismatchError，非規格檔名亦拒絕）
- review-g3d L（at 嚴格格式驗證、公開 append_line 寫入）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from aistorage.agora.store import AgoraStore
from aistorage.clock import parse_rfc3339
from aistorage.errors import MismatchError

DEFAULT_RETENTION_MONTHS = 3
DEFAULT_RETENTION_DAYS = 90

_CROCKFORD_DECODE = {
    c: i for i, c in enumerate("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
}
_LEDGER_FILE_PATTERN = re.compile(r"^(?P<ym>\d{4}-\d{2})\.jsonl$")


def parse_ulid_timestamp_ms(ulid: str) -> int | None:
    """自 26 字元之 ULID 前 10 碼 Crockford Base32 解析出 48-bit UTC 毫秒時間戳。"""
    if not isinstance(ulid, str) or len(ulid) < 10:
        return None
    time_part = ulid[:10].upper()
    val = 0
    for ch in time_part:
        if ch not in _CROCKFORD_DECODE:
            return None
        val = (val << 5) | _CROCKFORD_DECODE[ch]
    return val


def is_item_key_too_old(
    item_key: str,
    now: datetime,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> bool:
    """判定 item_key (ULID) 是否早於留存期間（預設 90 天 / 3 個月）。"""
    ulid_ms = parse_ulid_timestamp_ms(item_key)
    if ulid_ms is None:
        return False
    ulid_dt = datetime.fromtimestamp(ulid_ms / 1000.0, tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    return ulid_dt < cutoff


@dataclass(frozen=True)
class LedgerEntry:
    """清冊單筆記錄。"""

    item_key: str
    item_id: str
    decision: str
    raw_sha256: str | None
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "item_id": self.item_id,
            "decision": self.decision,
            "raw_sha256": self.raw_sha256,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        return cls(
            item_key=data["item_key"],
            item_id=data["item_id"],
            decision=data["decision"],
            raw_sha256=data.get("raw_sha256"),
            at=data["at"],
        )


class Ledger:
    """處理過的 item_key 清冊管理類別。

    存放位置：真本 repo 的 _committer/ledger/<YYYY-MM>.jsonl。
    包含已 ACCEPT 與通過驗章之 REJECT 項目，避免重複評估與重放。
    目前載入所有月份之清冊紀錄；更早的由 is_item_key_too_old 兜底（超過 retention_days 直接拒收）。
    """

    def __init__(
        self,
        store: AgoraStore,
        *,
        retention_months: int = DEFAULT_RETENTION_MONTHS,
    ) -> None:
        self.store = store
        self.retention_months = retention_months
        self.ledger_dir = self.store.worktree / "_committer" / "ledger"
        self._entries: dict[str, LedgerEntry] = {}
        self._loaded: bool = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.ledger_dir.is_dir():
            return

        for p in sorted(self.ledger_dir.iterdir()):
            if p.name.startswith("."):
                continue
            if not p.is_file() or not _LEDGER_FILE_PATTERN.match(p.name):
                raise MismatchError(
                    f"清冊目錄包含非規格檔案: {p.name}（必須符合 YYYY-MM.jsonl 格式）"
                )

            try:
                with open(p, encoding="utf-8") as f:
                    for line_no, line in enumerate(f, start=1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            entry = LedgerEntry.from_dict(data)
                        except Exception as e:
                            raise MismatchError(
                                f"清冊檔案 {p.name} 第 {line_no} 行解析失敗: {e}"
                            ) from e
                        # 若有重複 item_key，保留第一筆以維護不可變性
                        if entry.item_key not in self._entries:
                            self._entries[entry.item_key] = entry
            except (UnicodeDecodeError, OSError) as e:
                raise MismatchError(f"清冊檔案 {p.name} 讀取失敗: {e}") from e

    def contains(self, item_key: str) -> LedgerEntry | None:
        """查詢 item_key 是否曾被處理過。若存在回傳其 LedgerEntry，否則回傳 None。"""
        self._ensure_loaded()
        return self._entries.get(item_key)

    def record(
        self,
        item_key: str,
        *,
        item_id: str,
        decision: str,
        raw_sha256: str | None,
        at: str,
    ) -> None:
        """將處理結果記錄至清冊。

        參數說明：
        - item_key: 收件匣項目的 ULID。
        - item_id: 資源完整 ID（例如 'opencode:ses_12345'）。
        - decision: 決策代碼 code 字串（例如 'ok', 'already', 'stale', 'too_old' 等，非 DecisionKind 列舉名稱）。
        - raw_sha256: raw 檔案之 sha256 雜湊（若無 raw 則為 None）。
        - at: 記錄時間（RFC 3339 格式）。

        H2 & L: at 必須符合 RFC 3339 格式，否則拋出 ValueError。
        L: 透過 store.append_line 公開方法寫入。
        """
        self._ensure_loaded()
        try:
            parse_rfc3339(at)
        except Exception as e:
            raise ValueError(f"無效之 at 時間格式: {repr(at)}（必須符合 RFC 3339）: {e}") from e

        ym = at[:7]
        if not re.match(r"^\d{4}-\d{2}$", ym):
            raise ValueError(f"無效之月份前綴: {repr(ym)}")

        entry = LedgerEntry(
            item_key=item_key,
            item_id=item_id,
            decision=decision,
            raw_sha256=raw_sha256,
            at=at,
        )
        self._entries[item_key] = entry

        rel_path = f"_committer/ledger/{ym}.jsonl"
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        self.store.append_line(rel_path, line)
