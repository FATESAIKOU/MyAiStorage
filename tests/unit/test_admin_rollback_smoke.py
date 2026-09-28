"""6.2 回滾的冒煙測試（只寫冒煙）。

`via="rollback"` 由 agora/store.py 的允許值支援；回滾只寫真本，
「push → 驗證 → 重建 pin → 讀取視圖世代」是 admin.remote.swap_remote
（見 test_admin_swap_smoke.py）。
"""

import hashlib
import json
from pathlib import Path

import pytest

from aistorage.admin import AdminError
from aistorage.admin.rollback import (
    RUNNING_NOTE,
    list_rollback_points,
    rollback_session,
)
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.schema import generate_ulid


def _store(tmp_path: Path, *, running: bool = True):
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
            status="running" if running else "stopped",
            snapshot_at=snap_at, raw_sha256=sha, raw_size=len(content),
            committed_at="2026-09-27T08:01:00Z",
            last_item_key=generate_ulid(), title="t",
            in_progress=running or None), p)
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
    store, sid, shas = _store(tmp_path)
    clock = FixedClock("2026-09-27T10:00:00Z")
    result = rollback_session(store=store, session_id=sid,
                              target_snapshot_sha256=shas[0],
                              reason="回到第一版", clock=clock)
    assert result.to_sha256 == shas[0]
    assert result.from_sha256 == shas[1]
    assert result.was_running is True
    assert result.note == RUNNING_NOTE
    current = store.get_session(sid)
    assert current is not None
    assert current.raw_sha256 == shas[0]
    # 舊版本內容真的回來了（不是只有雜湊）
    raw = store.raw_path_for_snapshot(sid, shas[0]).read_bytes()
    assert raw == b'{"v": 0}'
    # 新快照是「一次新的快照事件」，時間大於所有舊快照（單調性）
    snaps = store.snapshots(sid)
    assert len(snaps) == 3
    assert snaps[-1].via == "rollback"
    assert snaps[-1].snapshot_at > snaps[-2].snapshot_at
    assert snaps[-1].snapshot_sha256 == shas[0]


def test_rollback_record_has_no_content(tmp_path: Path) -> None:
    store, sid, shas = _store(tmp_path)
    clock = FixedClock("2026-09-27T10:00:00Z")
    result = rollback_session(store=store, session_id=sid,
                              target_snapshot_sha256=shas[0],
                              reason="回到第一版", clock=clock)
    body = (store.worktree / f"_admin/rollbacks/{result.record_id}.json").read_text()
    obj = json.loads(body)
    assert obj["who"] == "admin" and obj["why"] == "回到第一版"
    assert obj["to_sha256"] == shas[0] and obj["from_sha256"] == shas[1]
    assert b'{"v": 0}' not in body.encode()   # 紀錄不含內容


def test_rollback_stopped_session_not_flagged_running(tmp_path: Path) -> None:
    store, sid, shas = _store(tmp_path, running=False)
    result = rollback_session(store=store, session_id=sid,
                              target_snapshot_sha256=shas[0],
                              reason="x", clock=FixedClock("2026-09-27T10:00:00Z"))
    assert result.was_running is False
    # 已停止的 Session 沒有「下一次同步蓋過」的問題，說明仍保留在 note
    assert "永久移除" in result.note
