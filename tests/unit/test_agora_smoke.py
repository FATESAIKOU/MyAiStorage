"""Smoke and security tests for agora layout and store modules."""

import hashlib
import json
from pathlib import Path
import subprocess
import pytest

from aistorage.agora import (
    AgoraStore,
    FakeRawStorage,
    GitRawStorage,
    RawRef,
    SessionRecord,
    SnapshotEntry,
    layout,
)
from aistorage.annex.git import SubprocessAnnexGit
from aistorage.errors import MismatchError, WriteError


def test_layout_enc_dec_smoke():
    raw_str = "ses:2026/09/27@001#test"
    encoded = layout.enc(raw_str)
    assert ":" not in encoded
    assert "/" not in encoded
    assert "@" not in encoded
    assert "#" not in encoded
    assert layout.dec(encoded) == raw_str

    # Safe characters preserved (excluding .)
    assert layout.enc("abc-123_test") == "abc-123_test"

    # M5: 防止路徑穿越
    with pytest.raises(ValueError):
        layout.enc("..")
    with pytest.raises(ValueError):
        layout.enc(".")
    with pytest.raises(ValueError):
        layout.enc("")
    # 斜線轉為百分比編碼，不造成路徑穿越
    assert "/" not in layout.enc("dir/sub")
    assert layout.enc("dir/sub") == "dir%2Fsub"


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
    assert layout.reference_path(ulid) == f"references/{ulid}.json"

    assert layout.continuation_link_path("opencode:ses_02", ulid) == f"links/continuation/opencode%3Ases_02/{ulid}.json"
    assert layout.reference_link_path("opencode:s1", "opencode:s2") == "links/reference/opencode%3As1/opencode%3As2.json"

    assert layout.record_path_for_id(f"handoff:{ulid}") == f"handoffs/{ulid}.json"
    assert layout.record_path_for_id(f"claim:{ulid}") == f"claims/{ulid}.json"
    assert layout.record_path_for_id(f"rewrite:{ulid}") == f"rewrites/{ulid}.json"
    assert layout.record_path_for_id(f"reference:{ulid}") == f"references/{ulid}.json"
    assert layout.record_path_for_id(session_id) == "sessions/opencode/ses_12345/meta.json"

    # M5: 保留型態拒絕與無效 ULID
    with pytest.raises(ValueError):
        layout.record_path_for_id(f"artifact:{ulid}")
    with pytest.raises(ValueError):
        layout.record_path_for_id(f"session:{ulid}")
    with pytest.raises(ValueError):
        layout.handoff_path("invalid_short_ulid")


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
    temp_dir = tmp_path / "external_temp"
    store = AgoraStore(worktree, raw_storage, temp_dir=temp_dir)

    # 1. 寫入 JSON 通用測試 (put_json)
    store.put_json("test/config.json", {"b": 2, "a": 1})
    target_file = worktree / "test/config.json"
    assert target_file.is_file()
    text = target_file.read_text(encoding="utf-8")
    assert text == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert "test/config.json" in store.changed_paths()

    # 2. 寫入 Session (put_session)
    raw_file = tmp_path / "sample.raw"
    raw_content = b'{"messages": ["hello"]}'
    raw_file.write_bytes(raw_content)
    raw_sha = hashlib.sha256(raw_content).hexdigest().lower()

    rec = SessionRecord(
        id="opencode:ses_test",
        producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=raw_sha,
        raw_size=len(raw_content),
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
    assert snaps[0].raw_size == len(raw_content)
    assert snaps[0].item_key == "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    # 檢查 raw_path_for_snapshot
    retrieved_raw = store.raw_path_for_snapshot("opencode:ses_test", snaps[0].snapshot_sha256)
    assert retrieved_raw.is_file()
    assert retrieved_raw.read_bytes() == raw_content
    # M6: 暫存檔案在工作樹外部
    assert not str(retrieved_raw).startswith(str(worktree))

    # H3-c: 測試 raw 雜湊與大小驗證
    bad_rec_sha = SessionRecord(
        id="opencode:ses_bad",
        producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256="0" * 64,
        raw_size=len(raw_content),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    with pytest.raises(MismatchError):
        store.put_session(bad_rec_sha, raw_file)


def test_agora_h3a_multiple_snapshots_committed_per_run(tmp_path: Path):
    """H3-a & PM 決定 3 整合測試：
    同一輪收進同一個 Session 的兩份快照，每份各 commit 一次；
    push 至遠端後，全新的 clone 能夠透過 raw_path_for_snapshot 正確取回兩份快照。
    """
    # 1. 建立遠端裸倉庫 (bare remote)
    remote_dir = tmp_path / "remote.git"
    remote_dir.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote_dir)], check=True, capture_output=True)

    # 2. 建立本地 committer 工作區並 clone
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "clone", str(remote_dir), str(worktree)], check=True, capture_output=True)

    # 配置 git 使用者身分
    git = SubprocessAnnexGit(worktree)
    git._run(["git", "config", "user.name", "AiStorage Committer"], is_write=True)
    git._run(["git", "config", "user.email", "committer@aistorage.local"], is_write=True)

    # 初始空的 commit
    readme = worktree / "README.md"
    readme.write_text("# Agora repo")
    git.add(["README.md"])
    git.commit("initial commit")
    git.push("origin", "main")

    raw_storage = GitRawStorage(worktree)
    store = AgoraStore(worktree, raw_storage, git=git, temp_dir=tmp_path / "committer_tmp")

    # 3. 寫入第 1 份快照 (Snapshot 1)
    raw1 = tmp_path / "snap1.raw"
    raw1_content = b'{"version": 1, "messages": ["first"]}'
    raw1.write_bytes(raw1_content)
    raw1_sha = hashlib.sha256(raw1_content).hexdigest().lower()

    rec1 = SessionRecord(
        id="opencode:ses_multi",
        producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=raw1_sha,
        raw_size=len(raw1_content),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FA1",
    )
    store.put_session(rec1, raw1)

    # 4. 同一輪收進第 2 份快照 (Snapshot 2, 覆寫 raw 路徑)
    raw2 = tmp_path / "snap2.raw"
    raw2_content = b'{"version": 2, "messages": ["first", "second"]}'
    raw2.write_bytes(raw2_content)
    raw2_sha = hashlib.sha256(raw2_content).hexdigest().lower()

    rec2 = SessionRecord(
        id="opencode:ses_multi",
        producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:05:00Z",
        status="running",
        snapshot_at="2026-09-27T08:05:00Z",
        raw_sha256=raw2_sha,
        raw_size=len(raw2_content),
        committed_at="2026-09-27T08:06:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FA2",
    )
    store.put_session(rec2, raw2)

    # 5. 推送至遠端 (push)
    git.push("origin", "main")

    # 6. 建立全新的獨立 clone (模擬讀取視圖或接續點判定從全新環境讀取)
    fresh_clone = tmp_path / "fresh_clone"
    subprocess.run(["git", "clone", "-b", "main", str(remote_dir), str(fresh_clone)], check=True, capture_output=True)

    fresh_raw_storage = GitRawStorage(fresh_clone)
    fresh_git = SubprocessAnnexGit(fresh_clone)
    fresh_store = AgoraStore(fresh_clone, fresh_raw_storage, git=fresh_git, temp_dir=tmp_path / "reader_tmp")

    # 7. 驗證從全新 clone 之中，兩份快照均能成功取出且內容正確！
    out1 = fresh_store.raw_path_for_snapshot("opencode:ses_multi", raw1_sha)
    assert out1.read_bytes() == raw1_content

    out2 = fresh_store.raw_path_for_snapshot("opencode:ses_multi", raw2_sha)
    assert out2.read_bytes() == raw2_content


def test_git_raw_storage_strict_fail_on_hash_object_error(tmp_path: Path):
    """H3-b: git hash-object 失敗時嚴格拋出 WriteError，絕不假裝成功。"""
    non_git_dir = tmp_path / "not_a_git_repo"
    non_git_dir.mkdir()
    storage = GitRawStorage(non_git_dir)
    src_file = tmp_path / "f.txt"
    src_file.write_bytes(b"hello")

    with pytest.raises(WriteError):
        storage.store(non_git_dir / "target", src_file)


def test_agora_store_r10_git_required_for_git_raw_storage(tmp_path: Path):
    """R10: raw_storage 為 GitRawStorage 時，git 參數為必填，若為 None 則建構時拋出 ValueError。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = GitRawStorage(worktree)

    with pytest.raises(ValueError, match="必須提供 git"):
        AgoraStore(worktree, raw_storage, git=None)


def test_agora_layout_r11_path_validations():
    """R11: ledger_path, rejection_path 與 _SOURCE_PATTERN 格式嚴格驗證。"""
    # 1. rejection_path 必須符合 ULID 格式
    valid_ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert layout.rejection_path(valid_ulid) == f"_committer/rejections/{valid_ulid}.json"

    with pytest.raises(ValueError, match="無效之 rejection item_key ULID"):
        layout.rejection_path("invalid_short_key")

    with pytest.raises(ValueError, match="無效之 rejection item_key ULID"):
        layout.rejection_path("01ARZ3NDEKTSV4RRFFQ69G5FA!")

    # 2. ledger_path 必須符合 YYYY-MM
    assert layout.ledger_path("2026-09") == "_committer/ledger/2026-09.jsonl"

    with pytest.raises(ValueError, match="無效之清冊月份格式"):
        layout.ledger_path("202609")

    with pytest.raises(ValueError, match="無效之清冊月份格式"):
        layout.ledger_path("2026-9")

    with pytest.raises(ValueError, match="無效之清冊月份格式"):
        layout.ledger_path("2026-09-01")

    # 3. _SOURCE_PATTERN 開頭不允許 - 或 _
    with pytest.raises(ValueError, match="無效之 Session source"):
        layout.split_session_id("-opencode:ses_123")

    with pytest.raises(ValueError, match="無效之 Session source"):
        layout.session_dir("_opencode", "ses_123")

    # 合法 source
    src, sid = layout.split_session_id("opencode:ses_123")
    assert src == "opencode"
    assert sid == "ses_123"
