"""同步器的本機狀態（`/work/.aistorage/sync-state.json`）。

每個 Session 記一筆上傳紀錄，讓同步器知道「上傳過什麼、等了多久」。
這是**本機**狀態，壞掉就重建（頂多多等一輪或重傳一次），所以寫入採
「暫存檔 ＋ 原子改名」，不做版本控制。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
from typing import Any

STATE_FORMAT = "aistorage.syncer-state/v1"
DEFAULT_STATE_PATH = Path("/work/.aistorage/sync-state.json")


@dataclass
class SessionSyncRecord:
    """單一 Session 的上傳紀錄。"""

    last_uploaded_sha: str | None = None
    last_item_key: str | None = None
    uploaded_at: str | None = None
    uploaded_generation: int | None = None
    stop_observed_at: str | None = None
    last_seen_sha: str | None = None
    last_synced_at: str | None = None
    too_large: bool = False
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionSyncRecord:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class SyncState:
    """同步器的本機狀態（session_id → 紀錄）。"""

    path: Path = DEFAULT_STATE_PATH
    sessions: dict[str, SessionSyncRecord] = field(default_factory=dict)
    format: str = STATE_FORMAT

    @classmethod
    def load(cls, path: Path | str | None = None) -> SyncState:
        """讀取狀態；檔案不存在或損毀就從空白開始（不因為狀態壞掉而不能同步）。"""
        p = Path(path) if path is not None else DEFAULT_STATE_PATH
        state = cls(path=p)
        if not p.is_file():
            return state
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return state
        if not isinstance(data, dict):
            return state
        raw = data.get("sessions")
        if isinstance(raw, dict):
            for session_id, rec in raw.items():
                if isinstance(rec, dict):
                    state.sessions[str(session_id)] = SessionSyncRecord.from_dict(rec)
        return state

    def save(self) -> None:
        """原子寫入（先寫暫存檔再 rename）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": self.format,
            "sessions": {
                sid: rec.to_dict() for sid, rec in sorted(self.sessions.items())
            },
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)  # 狀態裡有 item_key，別給其他人看
        os.replace(tmp, self.path)

    def record(self, session_id: str) -> SessionSyncRecord:
        """取得（必要時建立）某個 Session 的紀錄。"""
        rec = self.sessions.get(session_id)
        if rec is None:
            rec = SessionSyncRecord()
            self.sessions[session_id] = rec
        return rec

    def pending(self) -> list[str]:
        """上傳了但 Agora 還沒看到的 Session（等待中；`last_item_key` 存在且沒上傳成功紀錄）。"""
        return sorted(
            sid
            for sid, rec in self.sessions.items()
            if rec.last_item_key and rec.last_uploaded_sha and not rec.error_code
        )

    def rejected(self) -> list[str]:
        """被拒收、不自動重傳的 Session。"""
        return sorted(
            sid for sid, rec in self.sessions.items() if rec.error_code == "rejected"
        )

    def too_large(self) -> list[str]:
        """原始紀錄超過上限、沒有上傳的 Session（交給 6.3 回報）。"""
        return sorted(sid for sid, rec in self.sessions.items() if rec.too_large)
