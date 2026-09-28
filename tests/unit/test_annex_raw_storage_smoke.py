"""Smoke tests for AnnexRawStorage and annex keys coverage in Agora / Integrity (tasks 2.6).

依據 2.6 決策 (docs/decision-log.md)：
1. agora store 的 RawStorage 加入 AnnexRawStorage（annex.largefiles 包含原始紀錄，取回時驗 annex key 雜湊與大小）。
2. AgoraStore 預設改用 AnnexRawStorage。
3. integrity / verify 的 annex key 集合涵蓋原始紀錄物件。
4. git / git-annex 操作一律於 tmp_path 暫存目錄執行，絕不碰專案 repo。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import pytest

from aistorage.agora import AgoraStore, AnnexRawStorage, SessionRecord
from aistorage.annex.fake import FakeAnnexGit
from aistorage.annex.git import get_git_env
from aistorage.drive.fake import FakeDrive
from aistorage.drive.model import DriveFile
from aistorage.errors import MismatchError
from aistorage.integrity import (
    PinState,
    RepoListing,
    verify_annex_coverage,
    verify_after_push,
    verify_clone,
)
from aistorage.schema import generate_ulid


def _make_sample_session_record(
    session_id: str,
    raw_bytes: bytes,
    snapshot_at: str = "2026-09-27T08:00:00Z",
) -> tuple[SessionRecord, Path]:
    sha = hashlib.sha256(raw_bytes).hexdigest().lower()
    rec = SessionRecord(
        id=session_id,
        producer="profile:mac-opencode",
        created_at=snapshot_at,
        updated_at=snapshot_at,
        status="running",
        snapshot_at=snapshot_at,
        raw_sha256=sha,
        raw_size=len(raw_bytes),
        committed_at=snapshot_at,
        last_item_key=generate_ulid(),
        title=f"Session {session_id}",
    )
    return rec, sha


def test_annex_raw_storage_basic_store_and_retrieve(tmp_path: Path):
    """驗證 AnnexRawStorage 寫入產出 SHA256E annex key，取出時能正常讀回內容。"""
    raw_content = b'{"messages": [{"id": "m1", "text": "hello annex"}]}'
    src_file = tmp_path / "source_raw.json"
    src_file.write_bytes(raw_content)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    storage = AnnexRawStorage(worktree)

    target_path = worktree / "sessions/opencode/s1/raw.json"
    raw_ref = storage.store(target_path, src_file)

    assert raw_ref.kind == "annex"
    expected_sha = hashlib.sha256(raw_content).hexdigest().lower()
    expected_key = f"SHA256E-s{len(raw_content)}--{expected_sha}.json"
    assert raw_ref.ref == expected_key
    assert expected_key in storage.keys()

    # 取回原始檔案
    dest = tmp_path / "retrieved.raw"
    storage.retrieve(raw_ref.ref, dest)
    assert dest.is_file()
    assert dest.read_bytes() == raw_content


def test_annex_raw_storage_retrieve_verifies_hash_mismatch(tmp_path: Path):
    """驗證 AnnexRawStorage 取回時嚴格檢查 annex key 雜湊；雜湊不符時拋出 MismatchError。"""
    raw_content = b'{"messages": [{"id": "m1"}]}'
    src_file = tmp_path / "src.json"
    src_file.write_bytes(raw_content)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    storage = AnnexRawStorage(worktree)

    target_path = worktree / "sessions/opencode/s1/raw.json"
    raw_ref = storage.store(target_path, src_file)

    # 刻意篡改內部內容，維持大小相同但雜湊不同
    corrupted_content = b'{"messages": [{"id": "x1"}]}'
    assert len(corrupted_content) == len(raw_content)
    storage._blobs[raw_ref.ref] = corrupted_content

    dest = tmp_path / "corrupted_out.raw"
    with pytest.raises(MismatchError, match="SHA-256.*不符"):
        storage.retrieve(raw_ref.ref, dest)


def test_annex_raw_storage_retrieve_verifies_size_mismatch(tmp_path: Path):
    """驗證 AnnexRawStorage 取回時檢查大小；大小不符時拋出 MismatchError。"""
    raw_content = b'{"messages": [{"id": "m1"}]}'
    src_file = tmp_path / "src.json"
    src_file.write_bytes(raw_content)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    storage = AnnexRawStorage(worktree)

    target_path = worktree / "sessions/opencode/s1/raw.json"
    raw_ref = storage.store(target_path, src_file)

    # 刻意篡改內部內容大小
    storage._blobs[raw_ref.ref] = b'{"truncated"}'

    dest = tmp_path / "truncated_out.raw"
    with pytest.raises(MismatchError, match="大小.*不符"):
        storage.retrieve(raw_ref.ref, dest)


def test_agora_store_defaults_to_annex_raw_storage(tmp_path: Path):
    """驗證 AgoraStore 預設採用 AnnexRawStorage，且快照歷史中紀錄 annex_key。"""
    worktree = tmp_path / "agora"
    worktree.mkdir()
    ext_tmp = tmp_path / "external_temp"

    # AgoraStore 不傳入 raw_storage，應預設為 AnnexRawStorage
    store = AgoraStore(worktree, temp_dir=ext_tmp)
    assert isinstance(store.raw_storage, AnnexRawStorage)

    raw_data = b'{"session_data": [1, 2, 3]}'
    src_file = tmp_path / "session_raw.json"
    src_file.write_bytes(raw_data)

    rec, sha = _make_sample_session_record("opencode:ses_default_annex", raw_data)
    store.put_session(rec, src_file)

    # 驗證快照紀錄了 annex_key，且 git_blob 為 None
    snaps = store.snapshots(rec.id)
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.annex_key is not None
    assert snap.annex_key.startswith(f"SHA256E-s{len(raw_data)}--{sha}")
    assert snap.git_blob is None

    # 驗證透過 store.raw_path_for_snapshot 取回
    retrieved_p = store.raw_path_for_snapshot(rec.id, sha)
    assert retrieved_p.is_file()
    assert retrieved_p.read_bytes() == raw_data
    # 暫存檔應置於工作樹外部
    assert str(retrieved_p).startswith(str(ext_tmp))


def test_agora_store_annex_keys_and_coverage_verification(tmp_path: Path):
    """驗證 AgoraStore.annex_keys 包含所有 raw 物件，且 verify_annex_coverage 可進行覆蓋核對。"""
    worktree = tmp_path / "agora"
    store = AgoraStore(worktree, temp_dir=tmp_path / "tmp")

    raw1 = b'{"data": "session1"}'
    f1 = tmp_path / "f1.json"
    f1.write_bytes(raw1)
    rec1, sha1 = _make_sample_session_record("opencode:s1", raw1)
    store.put_session(rec1, f1)

    raw2 = b'{"data": "session2_larger_payload"}'
    f2 = tmp_path / "f2.json"
    f2.write_bytes(raw2)
    rec2, sha2 = _make_sample_session_record("opencode:s2", raw2)
    store.put_session(rec2, f2)

    all_keys = store.annex_keys()
    assert len(all_keys) == 2
    key1 = f"SHA256E-s{len(raw1)}--{sha1}.json"
    key2 = f"SHA256E-s{len(raw2)}--{sha2}.json"
    assert key1 in all_keys
    assert key2 in all_keys

    # 核對涵蓋性
    verify_annex_coverage(all_keys, [key1, key2])
    verify_annex_coverage(all_keys, [key1])

    # 缺少某 key 應報 MismatchError
    missing_key = "SHA256E-s123--0000000000000000000000000000000000000000000000000000000000000000.json"
    with pytest.raises(MismatchError, match="缺少必要之物件"):
        verify_annex_coverage(all_keys, [key1, missing_key])


def test_annex_raw_storage_fake_annex_git_integration(tmp_path: Path):
    """驗證 AnnexRawStorage 與 FakeAnnexGit 的 local_keys / annex_upload 整合。"""
    worktree = tmp_path / "agora"
    worktree.mkdir()
    fake_git = FakeAnnexGit(workdir=worktree, copy_effect="annex_upload")

    store = AgoraStore(worktree, git=fake_git, temp_dir=tmp_path / "tmp")
    assert isinstance(store.raw_storage, AnnexRawStorage)

    raw_data = b'{"msg": "test with fake git"}'
    f = tmp_path / "raw.json"
    f.write_bytes(raw_data)
    rec, sha = _make_sample_session_record("opencode:s_fake", raw_data)

    store.put_session(rec, f)

    expected_key = f"SHA256E-s{len(raw_data)}--{sha}.json"
    # AnnexRawStorage 寫入時自動登記至 fake_git.local_keys
    assert expected_key in fake_git.local_keys

    # 模擬提交流程第 8 步：git annex copy
    fake_git.copy("origin")

    # 拷貝後 local_keys 進入 annex_keys
    assert expected_key in fake_git.annex_keys
    assert expected_key in fake_git.annex_keys_in("test-uuid")


def test_annex_raw_storage_with_real_git_in_tmp_path(tmp_path: Path):
    """驗證在暫存目錄下的真實 Git 倉庫中，AnnexRawStorage 正確設定 annex.largefiles。"""
    git_dir = tmp_path / "git_test_repo"
    git_dir.mkdir()

    env = get_git_env()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=git_dir, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=git_dir, env=env, check=True)
    subprocess.run(["git", "config", "user.email", "committer@test.local"], cwd=git_dir, env=env, check=True)

    storage = AnnexRawStorage(git_dir)

    # 驗證 git config annex.largefiles 已被配置
    proc = subprocess.run(
        ["git", "config", "annex.largefiles"],
        cwd=git_dir,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert proc.stdout.strip() == "include=*.json"

    raw_data = b'{"msg": "real git test"}'
    src_f = tmp_path / "in.json"
    src_f.write_bytes(raw_data)
    dest_path = git_dir / "sessions/opencode/s_git/raw.json"

    raw_ref = storage.store(dest_path, src_f)
    assert raw_ref.kind == "annex"
    assert dest_path.is_file()

    # 讀回取件
    dest_out = tmp_path / "out.raw"
    storage.retrieve(raw_ref.ref, dest_out)
    assert dest_out.read_bytes() == raw_data


def test_verify_clone_and_verify_after_push_expected_annex_keys(tmp_path: Path):
    """驗證 verify_clone 與 verify_after_push 支援 expected_annex_keys 檢驗。"""
    drive = FakeDrive()
    prefix = drive.seed_folder("repo")
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    m_content = b"GITBUNDLE-s100--01234567-89ab-cdef-0123-456789abcdef-aaaabbbbccccddddeeeeffff0000111122223333444455556666777788889999\n"
    m_sha = hashlib.sha256(m_content).hexdigest().lower()
    drive.seed_file(prefix, f"GITMANIFEST--{uuid}", m_content, sha256=m_sha)

    key_present = "SHA256E-s100--1111111111111111111111111111111111111111111111111111111111111111.json"
    key_missing = "SHA256E-s200--2222222222222222222222222222222222222222222222222222222222222222.json"

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "a" * 40},
        manifest_sha256=m_sha,
        prev_manifest_sha256=None,
        active_bundles=("GITBUNDLE-s100--01234567-89ab-cdef-0123-456789abcdef-aaaabbbbccccddddeeeeffff0000111122223333444455556666777788889999",),
        removed_bundles=frozenset(),
        annex_keys=frozenset({key_present}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    git = FakeAnnexGit(refs={"refs/heads/main": "a" * 40})

    # verify_clone 包含符合之 expected_annex_keys
    verify_clone(
        git,
        state,
        drive=drive,
        prefix_folder_id=prefix,
        expected_annex_keys=frozenset({key_present}),
    )

    # verify_clone 若缺少 expected_annex_keys 拋出 MismatchError
    with pytest.raises(MismatchError, match="缺少必要之物件"):
        verify_clone(
            git,
            state,
            drive=drive,
            prefix_folder_id=prefix,
            expected_annex_keys=frozenset({key_missing}),
        )

    # verify_after_push
    listing_files = tuple(drive.list_children(prefix))
    listing = RepoListing(prefix_folder_id=prefix, files=listing_files, subfolders=())

    with pytest.raises(MismatchError, match="缺少必要之物件"):
        verify_after_push(
            git,
            drive,
            listing,
            state,
            local_refs={"refs/heads/main": "a" * 40},
            push_started_at="2026-09-27T08:00:00Z",
            workdir=tmp_path / "workdir",
            expected_annex_keys=frozenset({key_missing}),
        )
