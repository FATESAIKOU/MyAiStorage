"""AiStorage 已處理收件匣清冊 (Ledger) 模組。

依據規格：
- docs/impl/group3-modules.md 第 4.3 節
- design D2、schemas/README.md（清冊格式、留存期 3 個月、防重放、REJECT 亦記錄）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from aistorage.agora.store import AgoraStore

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
    包含已 ACCEPT 與 REJECT 之項目，避免重複評估與重放。
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

        for p in sorted(self.ledger_dir.glob("*.jsonl")):
            m = _LEDGER_FILE_PATTERN.match(p.name)
            if not m:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            data = json.loads(line)
                            entry = LedgerEntry.from_dict(data)
                            self._entries[entry.item_key] = entry
            except Exception:
                pass

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
        """將處理結果記錄至清冊。"""
        self._ensure_loaded()
        entry = LedgerEntry(
            item_key=item_key,
            item_id=item_id,
            decision=decision,
            raw_sha256=raw_sha256,
            at=at,
        )
        self._entries[item_key] = entry

        ym = at[:7] if len(at) >= 7 and "-" in at[:7] else datetime.now(timezone.utc).strftime("%Y-%m")
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        target_file = self.ledger_dir / f"{ym}.jsonl"

        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(line)

        # 登記至 AgoraStore 變更路徑以供提交流程納入 git add
        rel_path = f"_committer/ledger/{ym}.jsonl"
        if hasattr(self.store, "_changed_paths") and rel_path not in self.store._changed_paths:
            self.store._changed_paths.append(rel_path)
