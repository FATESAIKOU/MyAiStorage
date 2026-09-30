"""Acceptance tests for Agora Layout and AgoraStore (Tasks 3.7 - 3.10).

Adheres strictly to:
- docs/impl/group3-modules.md §6
- design D10 (pinned snapshot reads)
- review-g3b.md & review-g3b-recheck.md (H3, M5, M6, M7, M8, R10, R11)
"""

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


# ---------------------------------------------------------------------------
# 1. Layout & Path Safety (M5, R11)
# ---------------------------------------------------------------------------


def test_layout_enc_dec_and_traversal_prevention():
    """M5: 驗證 layout.enc 編碼安全性、可逆性與防止路徑穿越。"""
    # 1. 冒號、斜線、特殊字元轉換為安全編碼
    raw_str = "ses:2026/09/27@001#feature-x"
    encoded = layout.enc(raw_str)
    assert ":" not in encoded
    assert "/" not in encoded
    assert "@" not in encoded
    assert "#" not in encoded
    assert layout.dec(encoded) == raw_str

    # 2. 保留字元 (-_)
    assert layout.enc("valid-name_123") == "valid-name_123"

    # 3. 拒絕路徑穿越字串（. 與 ..）以及空字串
    with pytest.raises(ValueError):
        layout.enc("..")
    with pytest.raises(ValueError):
        layout.enc(".")
    with pytest.raises(ValueError):
        layout.enc("")

    # 4. 斜線在字串中間時轉為百分比編碼，不造成路徑穿越
    enc_slash = layout.enc("foo/bar/baz")
    assert "/" not in enc_slash
    assert layout.dec(enc_slash) == "foo/bar/baz"


def test_layout_record_routing_and_ulid_validation():
    """M5 / R11: 驗證 record_path_for_id 路由規則與 ULID 格式驗證。"""
    valid_ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    # 1. ULID 紀錄型態正確路由
    assert layout.record_path_for_id(f"handoff:{valid_ulid}") == f"handoffs/{valid_ulid}.json"
    assert layout.record_path_for_id(f"claim:{valid_ulid}") == f"claims/{valid_ulid}.json"
    assert layout.record_path_for_id(f"rewrite:{valid_ulid}") == f"rewrites/{valid_ulid}.json"
    assert layout.record_path_for_id(f"reference:{valid_ulid}") == f"references/{valid_ulid}.json"

    # 2. Session 路由至 sessions/<source>/<enc(id)>/meta.json
    assert layout.record_path_for_id("opencode:ses_12345") == "sessions/opencode/ses_12345/meta.json"
    assert layout.record_path_for_id("opencode:ses/sub:01") == "sessions/opencode/ses%2Fsub%3A01/meta.json"

    # 3. 保留型態拒絕：artifact 不進 Agora、session 不能以 ULID 單獨作為 ID
    with pytest.raises(ValueError):
        layout.record_path_for_id(f"artifact:{valid_ulid}")

    with pytest.raises(ValueError):
        layout.record_path_for_id(f"session:{valid_ulid}")

    # 4. 非法 ULID 格式拒絕
    with pytest.raises(ValueError):
        layout.handoff_path("invalid_short_ulid")
    with pytest.raises(ValueError):
        layout.claim_path("01ARZ3NDEKTSV4RRFFQ69G5FA!")
    with pytest.raises(ValueError):
        layout.rewrite_path("")
    with pytest.raises(ValueError):
        layout.reference_path("non_ulid_string")


def test_layout_links_and_committer_paths():
    """R11: 驗證 links、ledger 與 rejection 路徑格式驗證。"""
    valid_ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    # 1. continuation link 與 reference link
    c_link = layout.continuation_link_path("opencode:ses_child", valid_ulid)
    assert c_link == f"links/continuation/opencode%3Ases_child/{valid_ulid}.json"

    r_link = layout.reference_link_path("opencode:ses_from", "opencode:ses_to")
    assert r_link == "links/reference/opencode%3Ases_from/opencode%3Ases_to.json"

    # 2. rejection_path 驗證 ULID
    assert layout.rejection_path(valid_ulid) == f"_committer/rejections/{valid_ulid}.json"
    with pytest.raises(ValueError, match="無效之 rejection item_key ULID"):
        layout.rejection_path("bad_key_123")

    # 3. ledger_path 驗證 YYYY-MM
    assert layout.ledger_path("2026-09") == "_committer/ledger/2026-09.jsonl"
    with pytest.raises(ValueError, match="無效之清冊月份格式"):
        layout.ledger_path("2026-9")
    with pytest.raises(ValueError, match="無效之清冊月份格式"):
        layout.ledger_path("2026/09")

    # 4. source 格式驗證（不能以 - 或 _ 開頭）
    with pytest.raises(ValueError, match="無效之 Session source"):
        layout.session_dir("-opencode", "ses_001")
    with pytest.raises(ValueError, match="無效之 Session source"):
        layout.split_session_id("_opencode:ses_001")


# ---------------------------------------------------------------------------
# 2. AgoraStore CRUD & Deterministic JSON (M7, M8)
# ---------------------------------------------------------------------------


def test_agora_store_put_json_deterministic(tmp_path: Path):
    """驗證 put_json 的輸出為排序鍵、2 格縮排、結尾換行之確定性格式。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = FakeRawStorage()
    store = AgoraStore(worktree, raw_storage, temp_dir=tmp_path / "tmp")

    obj = {"z": 100, "a": "hello", "nested": {"k2": True, "k1": False}}
    store.put_json("meta/test.json", obj)

    target = worktree / "meta/test.json"
    assert target.is_file()
    expected = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    assert target.read_text(encoding="utf-8") == expected
    assert "meta/test.json" in store.changed_paths()


def test_agora_store_session_crud_and_deduplication(tmp_path: Path):
    """M7 / M8: 測試 Session 存取、Snapshot 追加與相同快照重複寫入之去重。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = FakeRawStorage()
    store = AgoraStore(worktree, raw_storage, temp_dir=tmp_path / "tmp")

    raw_file = tmp_path / "raw1.json"
    raw_bytes = b'{"info": {"id": "s1"}, "messages": []}'
    raw_file.write_bytes(raw_bytes)
    sha = hashlib.sha256(raw_bytes).hexdigest().lower()

    rec = SessionRecord(
        id="opencode:s1",
        producer="profile:mac",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=sha,
        raw_size=len(raw_bytes),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        title="Session Title",
    )

    # 1. 存入 session
    store.put_session(rec, raw_file)

    # 2. 取得 session 與 record
    loaded_rec = store.get_session("opencode:s1")
    assert loaded_rec is not None
    assert loaded_rec.id == "opencode:s1"
    assert loaded_rec.title == "Session Title"

    rec_dict = store.get_record("opencode:s1")
    assert rec_dict is not None
    assert rec_dict["type"] == "session"
    assert rec_dict["id"] == "opencode:s1"

    # 3. 取得 snapshots 清單
    snaps = store.snapshots("opencode:s1")
    assert len(snaps) == 1
    assert snaps[0].snapshot_sha256 == sha
    assert snaps[0].raw_size == len(raw_bytes)

    # 4. 重複存入同一份 snapshot（雜湊相同）-> snapshots.jsonl 不重複追加
    store.put_session(rec, raw_file)
    snaps_after = store.snapshots("opencode:s1")
    assert len(snaps_after) == 1


def test_agora_store_corrupted_json_raises_mismatch_error(tmp_path: Path):
    """L: 驗證讀取受損的 JSON 紀錄時包裝為 MismatchError，絕不吞掉或丟出原生解析錯誤。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = FakeRawStorage()
    store = AgoraStore(worktree, raw_storage, temp_dir=tmp_path / "tmp")

    # 寫入損毀的 JSON 內容到 references/01ARZ3NDEKTSV4RRFFQ69G5FAV.json
    ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    corrupted_file = worktree / layout.reference_path(ulid)
    corrupted_file.parent.mkdir(parents=True, exist_ok=True)
    corrupted_file.write_text("{broken_json: true", encoding="utf-8")

    with pytest.raises(MismatchError, match="JSON"):
        store.get_record(f"reference:{ulid}")


# ---------------------------------------------------------------------------
# 3. Fail-Closed & Integrity Checks (H3-b, H3-c, R10)
# ---------------------------------------------------------------------------


def test_agora_store_put_session_validates_raw_hash_and_size(tmp_path: Path):
    """H3-c: put_session 嚴格比對 raw_sha256 與 raw_size，不符時拋出 MismatchError。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = FakeRawStorage()
    store = AgoraStore(worktree, raw_storage, temp_dir=tmp_path / "tmp")

    raw_file = tmp_path / "file.raw"
    raw_content = b"sample session content"
    raw_file.write_bytes(raw_content)
    real_sha = hashlib.sha256(raw_content).hexdigest().lower()

    # 1. 雜湊不符
    bad_sha_rec = SessionRecord(
        id="opencode:ses_mismatch",
        producer="profile:mac",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256="f" * 64,  # 假雜湊
        raw_size=len(raw_content),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    with pytest.raises(MismatchError, match="raw_sha256"):
        store.put_session(bad_sha_rec, raw_file)

    # 2. 大小不符
    bad_size_rec = SessionRecord(
        id="opencode:ses_mismatch2",
        producer="profile:mac",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=real_sha,
        raw_size=999999,  # 假大小
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    with pytest.raises(MismatchError, match="raw_size"):
        store.put_session(bad_size_rec, raw_file)


def test_agora_store_retrieve_snapshot_hash_verification(tmp_path: Path):
    """H3-c / M6: raw_path_for_snapshot 取出後必須驗證雜湊，且暫存區在工作樹外。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = FakeRawStorage()
    external_tmp = tmp_path / "ext_tmp"
    store = AgoraStore(worktree, raw_storage, temp_dir=external_tmp)

    raw_file = tmp_path / "data.raw"
    raw_bytes = b"verification payload"
    raw_file.write_bytes(raw_bytes)
    sha = hashlib.sha256(raw_bytes).hexdigest().lower()

    rec = SessionRecord(
        id="opencode:s_verify",
        producer="profile:mac",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=sha,
        raw_size=len(raw_bytes),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    store.put_session(rec, raw_file)

    # 1. 正常取出，驗證暫存路徑在 external_tmp 內（不在 worktree）
    retrieved = store.raw_path_for_snapshot("opencode:s_verify", sha)
    assert retrieved.is_file()
    assert retrieved.read_bytes() == raw_bytes
    assert str(retrieved).startswith(str(external_tmp))
    assert not str(retrieved).startswith(str(worktree))

    # 2. 當底層 RawStorage 取回受損內容時，取出驗證必須拋出 MismatchError
    class CorruptedRawStorage:
        def store(self, path, src):
            return RawRef("git", "corrupted_blob_ref")

        def retrieve(self, ref, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"corrupted bytes from storage")

    wt2 = tmp_path / "wt2"
    wt2.mkdir()
    bad_store = AgoraStore(wt2, CorruptedRawStorage(), temp_dir=tmp_path / "ext_tmp2")
    bad_store.put_session(rec, raw_file)
    with pytest.raises(MismatchError, match="雜湊.*不符"):
        bad_store.raw_path_for_snapshot("opencode:s_verify", sha)


def test_git_raw_storage_fail_closed_on_error(tmp_path: Path):
    """H3-b: GitRawStorage 在 git 指令失敗時拋出 WriteError，絕不回傳捏造之 SHA-1。"""
    non_git_dir = tmp_path / "not_git"
    non_git_dir.mkdir()
    storage = GitRawStorage(non_git_dir)

    src = tmp_path / "input.txt"
    src.write_bytes(b"data")

    with pytest.raises(WriteError):
        storage.store(non_git_dir / "target", src)


def test_agora_store_requires_git_when_using_git_raw_storage(tmp_path: Path):
    """R10: 使用 GitRawStorage 時必須提供 git 實例，若為 None 則於初始化時拋錯。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    raw_storage = GitRawStorage(worktree)

    with pytest.raises(ValueError, match="必須提供 git"):
        AgoraStore(worktree, raw_storage, git=None)


# ---------------------------------------------------------------------------
# 4. Multi-Snapshot Commit & Cold Retrieval (H3-a & D10)
# ---------------------------------------------------------------------------


def test_agora_h3a_multiple_snapshots_retrievable_from_fresh_clone(tmp_path: Path):
    """H3-a & D10:
    同一輪收進同一 Session 的兩份快照，各自 commit 一次；
    push 至遠端後，全新的 clone 可以透過 raw_path_for_snapshot 完整取出每一份舊快照，
    保證「從被釘住的快照讀」在分散式環境下切實可行。
    """
    # 1. 初始化遠端裸倉庫 (bare remote)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)

    # 2. 本地 committer 工作樹
    worktree = tmp_path / "committer_worktree"
    subprocess.run(["git", "clone", str(remote), str(worktree)], check=True, capture_output=True)

    git = SubprocessAnnexGit(worktree)
    git._run(["git", "config", "user.name", "Agora Committer"], is_write=True)
    git._run(["git", "config", "user.email", "committer@aistorage.local"], is_write=True)

    # 建立初始提交
    init_f = worktree / "INIT.md"
    init_f.write_text("initial")
    git.add(["INIT.md"])
    git.commit("initial commit")
    git.push("origin", "main")

    raw_storage = GitRawStorage(worktree)
    store = AgoraStore(worktree, raw_storage, git=git, temp_dir=tmp_path / "c_tmp")

    # 3. 寫入快照 1
    snap1_file = tmp_path / "snap1.json"
    snap1_bytes = b'{"version": 1, "messages": ["first message"]}'
    snap1_file.write_bytes(snap1_bytes)
    sha1 = hashlib.sha256(snap1_bytes).hexdigest().lower()

    rec1 = SessionRecord(
        id="opencode:ses_h3a",
        producer="profile:mac",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=sha1,
        raw_size=len(snap1_bytes),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FA1",
        title="H3-a Test Session",
    )
    store.put_session(rec1, snap1_file)

    # 4. 同一輪收進快照 2（覆寫 sessions/.../raw 路徑）
    snap2_file = tmp_path / "snap2.json"
    snap2_bytes = b'{"version": 2, "messages": ["first message", "second message"]}'
    snap2_file.write_bytes(snap2_bytes)
    sha2 = hashlib.sha256(snap2_bytes).hexdigest().lower()

    rec2 = SessionRecord(
        id="opencode:ses_h3a",
        producer="profile:mac",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:05:00Z",
        status="running",
        snapshot_at="2026-09-27T08:05:00Z",
        raw_sha256=sha2,
        raw_size=len(snap2_bytes),
        committed_at="2026-09-27T08:06:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FA2",
        title="H3-a Test Session (Updated)",
    )
    store.put_session(rec2, snap2_file)

    # 5. 推送至遠端
    git.push("origin", "main")

    # 6. 建立全新的獨立 clone（模擬第 4 組讀取視圖或接續點判定從全新環境讀取）
    reader_worktree = tmp_path / "reader_worktree"
    subprocess.run(["git", "clone", "-b", "main", str(remote), str(reader_worktree)], check=True, capture_output=True)

    reader_git = SubprocessAnnexGit(reader_worktree)
    reader_storage = GitRawStorage(reader_worktree)
    reader_store = AgoraStore(reader_worktree, reader_storage, git=reader_git, temp_dir=tmp_path / "r_tmp")

    # 7. 驗證從全新 clone 中，兩份快照均能成功取出且內容分毫不差！
    retrieved1 = reader_store.raw_path_for_snapshot("opencode:ses_h3a", sha1)
    assert retrieved1.read_bytes() == snap1_bytes

    retrieved2 = reader_store.raw_path_for_snapshot("opencode:ses_h3a", sha2)
    assert retrieved2.read_bytes() == snap2_bytes
