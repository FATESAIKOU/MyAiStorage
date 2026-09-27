"""6.2 回滾（管理者直接操作；PM 決定 6）。

不做簽章的 rollback 收件匣項目：管理者在 AdminLock 內直接把某個舊版本
恢復成新的快照。旧版本都能取回（snapshots.jsonl＋raw），回滾即追加一份
內容等於舊版本的新快照（`via="rollback"`）。

注意（PM 決定 6）：運作中的 Session，下一次同步會以來源端的內容成為
新版本（來源端比較新就上傳），回滾只維持到下一次同步為止；
要永久移除內容請用抹除（6.1），不要用回滾。
閱讀版在下一次發佈時重建。

需要 agora/store.py  owner 補上 `via="rollback"`
（put_session 的允許值與 SnapshotEntry 的字面量）；在本行未補之前，
執行面的測試會失敗，已列給 PM。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aistorage.admin import AdminError
from aistorage.agora.store import AgoraStore, SessionRecord
from aistorage.clock import Clock, format_rfc3339
from aistorage.schema import generate_ulid


@dataclass(frozen=True)
class RollbackPoint:
    session_id: str
    snapshot_sha256: str
    snapshot_at: str
    committed_at: str
    via: str
    is_current: bool


@dataclass(frozen=True)
class RollbackResult:
    session_id: str
    from_sha256: str
    to_sha256: str
    committed_at: str
    record_id: str


def list_rollback_points(store: AgoraStore, session_id: str) -> list[RollbackPoint]:
    """列出可回滾的快照（CLI 先列出來讓管理者選擇）。"""
    session = store.get_session(session_id)
    if session is None:
        raise AdminError(f"真本沒有這個 Session: {session_id}")
    current = session.raw_sha256.lower()
    return [RollbackPoint(
        session_id=session_id,
        snapshot_sha256=s.snapshot_sha256,
        snapshot_at=s.snapshot_at,
        committed_at=s.committed_at,
        via=s.via,
        is_current=s.snapshot_sha256.lower() == current,
    ) for s in store.snapshots(session_id)]


def rollback_session(*, store: AgoraStore, session_id: str,
                     target_snapshot_sha256: str, reason: str,
                     who: str = "admin", clock: Clock) -> RollbackResult:
    """把舊版本恢復成新的快照（呼叫端負責先進入 AdminLock）。

    target 必須在 snapshots 裡；新快照內容就是那一份舊版本，
    `snapshot_at` 取執行時鐘（這是一次新的快照事件）。
    """
    session = store.get_session(session_id)
    if session is None:
        raise AdminError(f"真本沒有這個 Session: {session_id}")
    known = {s.snapshot_sha256.lower() for s in store.snapshots(session_id)}
    if target_snapshot_sha256.lower() not in known:
        raise AdminError(f"快照不在歷史裡: {target_snapshot_sha256[:12]}…")
    if not reason.strip():
        raise AdminError("回滾必須說明原因")
    raw_path = store.raw_path_for_snapshot(session_id, target_snapshot_sha256)
    raw_bytes = raw_path.read_bytes()
    now = format_rfc3339(clock.now(), include_fraction=True)
    new_rec = SessionRecord(
        id=session.id,
        producer=session.producer,
        created_at=session.created_at,
        updated_at=now,
        status=session.status,
        snapshot_at=now,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest().lower(),
        raw_size=len(raw_bytes),
        committed_at=now,
        last_item_key=generate_ulid(),
        case_id=session.case_id,
        provenance=session.provenance,
        role=session.role,
        role_version=session.role_version,
        stopped_at=session.stopped_at,
        parent_id=session.parent_id,
        in_progress=session.in_progress,
        archived_at=session.archived_at,
        title=session.title,
        extra=dict(session.extra),
    )
    # via="rollback" 需要 agora/store.py owner 補上允許值（見模組 docstring）。
    store.put_session(new_rec, raw_path, via="rollback",  # type: ignore[arg-type]
                      rewrite_id=None)
    record_id = generate_ulid()
    store.put_json(
        f"_admin/rollbacks/{record_id}.json",
        {
            "id": record_id,
            "who": who,
            "at": now,
            "why": reason,
            "session_id": session_id,
            "from_sha256": session.raw_sha256,
            "to_sha256": new_rec.raw_sha256,
        },
    )
    return RollbackResult(
        session_id=session_id,
        from_sha256=session.raw_sha256,
        to_sha256=new_rec.raw_sha256,
        committed_at=now,
        record_id=record_id,
    )
