"""6.2 回滾的冒煙測試（只寫冒煙）。

執行面需要 agora/store.py owner 補上 via="rollback"；
在補上之前，執行測試預期失敗（已列給 PM），其餘全綠。
"""

import hashlib
from pathlib import Path

import pytest

from aistorage.admin import AdminError
from aistorage.admin.rollback import (
    list_rollback_points,
    rollback_session,
)
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.schema import generate_ulid


def _store(tmp_path: Path):
    worktree = tmp_path / "agora"
    worktree.mkdir(exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    sid = "opencode:s1"
    shas = []
    for i, snap_at in enumerate(["2026-09-27T08:00:00Z", "2026-09-27T08:05:00Z"]):
        content = f'{{"v": {i}}}'.encode()
        p = tmp_path / f"r{i}.raw"
        p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        store.put_session(SessionRecord(
            id=sid, producer="profile:mac-opencode",
            created_at="2026-09-27T08:00:00Z", updated_at=snap_at,
            status="running", snapshot_at=snap_at, raw_sha256=sha,
            raw_size=len(content), committed_at="2026-09-27T08:01:00Z",
            last_item_key=generate_ulid(), title="t"), p)
        shas.append(sha)
    return store, sid, shas


def test_list_rollback_points(tmp_path: Path) -> None:
    store, sid, shas = _store(tmp_path)
    points = list_rollback_points(store, sid)
    assert [p.snapshot_sha256 for p in points] == shas
    assert points[-1].is_current is True
    assert points[0].is_current is False
    with pytest.raises(AdminError):
        list_rollback_points(store, "opencode:nope")


def test_rollback_validates_target_and_reason(tmp_path: Path) -> None:
    store, sid, shas = _store(tmp_path)
    clock = FixedClock("2026-09-27T10:00:00Z")
    with pytest.raises(AdminError):
        rollback_session(store=store, session_id=sid,
                         target_snapshot_sha256="0" * 64,
                         reason="x", clock=clock)
    with pytest.raises(AdminError):
        rollback_session(store=store, session_id=sid,
                         target_snapshot_sha256=shas[0],
                         reason="  ", clock=clock)
    with pytest.raises(AdminError):
        rollback_session(store=store, session_id="opencode:nope",
                         target_snapshot_sha256=shas[0],
                         reason="x", clock=clock)


def test_rollback_restores_old_version_as_new_snapshot(tmp_path: Path) -> None:
    """舊版本恢復成新快照（via="rollback"；需 store owner 補允許值）。"""
    store, sid, shas = _store(tmp_path)
    clock = FixedClock("2026-09-27T10:00:00Z")
    result = rollback_session(store=store, session_id=sid,
                              target_snapshot_sha256=shas[0],
                              reason="回到第一版", clock=clock)
    assert result.to_sha256 == shas[0]
    assert result.from_sha256 == shas[1]
    current = store.get_session(sid)
    assert current is not None
    assert current.raw_sha256 == shas[0]
    assert len(store.snapshots(sid)) == 3
    assert store.snapshots(sid)[-1].via == "rollback"
    record = store.get_record("opencode:s1")
    assert record is not None
    import json
    assert (store.worktree / f"_admin/rollbacks/{result.record_id}.json").is_file()
