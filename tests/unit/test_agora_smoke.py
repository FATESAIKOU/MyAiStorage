"""Smoke tests for agora layout and store modules."""

import json
from pathlib import Path
import pytest

from aistorage.agora import (
    AgoraStore,
    FakeRawStorage,
    SessionRecord,
    SnapshotEntry,
    layout,
)


def test_layout_enc_dec_smoke():
    raw_str = "ses:2026/09/27@001#test"
    encoded = layout.enc(raw_str)
    assert ":" not in encoded
    assert "/" not in encoded
    assert "@" not in encoded
    assert "#" not in encoded
    assert layout.dec(encoded) == raw_str

    # Safe characters preserved
    assert layout.enc("abc-123_test.xyz") == "abc-123_test.xyz"


def test_layout_paths_smoke():
    session_id = "opencode:ses_12345"
    assert layout.session_dir("opencode", "ses_12345") == "sessions/opencode/ses_12345"
    assert layout.session_meta_path(session_id) == "sessions/opencode/ses_12345/meta.json"
    assert layout.session_raw_path(session_id) == "sessions/opencode/ses_12345/raw"
    assert layout.session_snapshots_path(session_id) == "sessions/opencode/ses_12345/snapshots.jsonl"

    ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert layout.handoff_path(ulid) == f"handoffs/{ulid}.json"
    assert layout.claim_path(ulid) == f"claims/{ulid}.json"
    assert layout.rewrite_path(ulid) == f"rewrites/{ulid}.json"

    assert layout.continuation_link_path("opencode:ses_02", ulid) == f"links/continuation/opencode%3Ases_02/{ulid}.json"
    assert layout.reference_link_path("opencode:s1", "opencode:s2") == "links/reference/opencode%3As1/opencode%3As2.json"

    assert layout.record_path_for_id(f"handoff:{ulid}") == f"handoffs/{ulid}.json"
    assert layout.record_path_for_id(f"claim:{ulid}") == f"claims/{ulid}.json"
    assert layout.record_path_for_id(f"rewrite:{ulid}") == f"rewrites/{ulid}.json"
    assert layout.record_path_for_id(session_id) == "sessions/opencode/ses_12345/meta.json"


def test_session_record_serialization_smoke():
    rec = SessionRecord(
        id="opencode:ses_001",
        producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        raw_size=0,
        committed_at="2026-09-27T08:05:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        title="Test Session",
    )
    d = rec.to_dict()
    assert d["id"] == "opencode:ses_001"
    assert d["status"] == "running"
    assert d["title"] == "Test Session"

    rec2 = SessionRecord.from_dict(d)
    assert rec2.id == rec.id
    assert rec2.producer == rec.producer
    assert rec2.title == rec.title
    assert rec2.status == rec.status


def test_agora_store_crud_smoke(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = FakeRawStorage()
    store = AgoraStore(worktree, raw_storage)

    # 1. 寫入 JSON 通用測試 (put_json)
    store.put_json("test/config.json", {"b": 2, "a": 1})
    target_file = worktree / "test/config.json"
    assert target_file.is_file()
    # 驗證格式：indent=2, sort_keys=True
    text = target_file.read_text(encoding="utf-8")
    assert text == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert "test/config.json" in store.changed_paths()

    # 2. 寫入 Session (put_session)
    raw_file = tmp_path / "sample.raw"
    raw_file.write_bytes(b'{"messages": ["hello"]}')

    rec = SessionRecord(
        id="opencode:ses_test",
        producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256="59048a1c97a514d1c44b36021f153a5c68910a905a5a92d491c10d7b32c6ff6f",
        raw_size=len(raw_file.read_bytes()),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    store.put_session(rec, raw_file)

    # 檢查檔案是否建立
    meta_p = worktree / layout.session_meta_path("opencode:ses_test")
    raw_p = worktree / layout.session_raw_path("opencode:ses_test")
    snaps_p = worktree / layout.session_snapshots_path("opencode:ses_test")
    assert meta_p.is_file()
    assert raw_p.is_file()
    assert snaps_p.is_file()

    # 檢查 get_session 與 get_record
    loaded_rec = store.get_session("opencode:ses_test")
    assert loaded_rec is not None
    assert loaded_rec.id == "opencode:ses_test"
    assert loaded_rec.status == "running"

    loaded_dict = store.get_record("opencode:ses_test")
    assert loaded_dict is not None
    assert loaded_dict["type"] == "session"

    # 檢查 snapshots
    snaps = store.snapshots("opencode:ses_test")
    assert len(snaps) == 1
    assert snaps[0].raw_size == len(raw_file.read_bytes())
    assert snaps[0].item_key == "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    # 檢查 raw_path_for_snapshot
    retrieved_raw = store.raw_path_for_snapshot("opencode:ses_test", snaps[0].snapshot_sha256)
    assert retrieved_raw.is_file()
    assert retrieved_raw.read_bytes() == raw_file.read_bytes()

    # 不存在的 Session 回傳 None
    assert store.get_session("opencode:not_exist") is None
    assert store.get_record("handoff:01NOTEXIST0000000000000000") is None
    assert store.snapshots("opencode:not_exist") == []

    # 檢查 changed_paths 包含 session 相關路徑
    changes = store.changed_paths()
    assert layout.session_meta_path("opencode:ses_test") in changes
    assert layout.session_raw_path("opencode:ses_test") in changes
    assert layout.session_snapshots_path("opencode:ses_test") in changes
