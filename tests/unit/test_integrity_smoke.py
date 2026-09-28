"""Smoke and unit tests for integrity module (pin and settle)."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import pytest

from aistorage.annex.manifest import parse_bundle_name
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.drive.model import DriveFile
from aistorage.errors import AbortRun, MismatchError, NotFound, ReadError, WriteError
from aistorage.integrity import (
    Disposition,
    GitPinStore,
    MemoryPinStore,
    PinPending,
    PinState,
    PrefixLevel,
    PushVerification,
    RepoListing,
    SettleAndSweepResult,
    SettleOutcome,
    SweepDecision,
    apply_sweep,
    check_manifest_continuity,
    check_parents,
    collect_removed_bundles,
    gc_removed,
    plan_readview_sweep,
    plan_sweep,
    precheck,
    purge_quarantine,
    resolve_content_checks,
    run_settle_and_sweep,
    settle,
    verify_after_push,
    verify_clone,
)


class DummyAnnexGit:
    """測試用 AnnexGit，僅實作 verify_clone 與 verify_after_push 所需之 ls_remote。"""

    def __init__(self, refs: dict[str, str]):
        self._refs = refs

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        return dict(self._refs)



def _create_test_bundle(repo_dir: Path, out_dir: Path, ref_spec: str, repo_uuid: str) -> Path:
    b_raw = out_dir / f"temp_{ref_spec.replace('..', '_').replace('/', '_')}.bundle"
    ns_ref = f"refs/namespaces/git-remote-annex/{repo_uuid}/refs/heads/main"
    sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", ref_spec], text=True).strip()
    subprocess.run(["git", "-C", str(repo_dir), "update-ref", ns_ref, sha], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "bundle", "create", str(b_raw), ns_ref],
        check=True,
        capture_output=True,
    )
    b_bytes = b_raw.read_bytes()
    b_size = len(b_bytes)
    b_sha = hashlib.sha256(b_bytes).hexdigest().lower()
    bundle_name = f"GITBUNDLE-s{b_size}--{repo_uuid}-{b_sha}"
    final_path = out_dir / bundle_name
    b_raw.rename(final_path)
    return final_path


def test_memory_pin_store_smoke():
    state1 = PinState(
        repo="agora",
        repo_uuid="01234567-89ab-cdef-0123-456789abcdef",
        refs={"refs/heads/main": "a" * 40},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=("b1",),
        removed_bundles=frozenset(),
        annex_keys=frozenset({"k1"}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    store = MemoryPinStore(initial_state=state1)

    s, p = store.load("agora")
    assert s == state1
    assert p is None

    # 未註冊的 repo load 拋出 ReadError
    with pytest.raises(ReadError):
        store.load("unknown_repo")

    # 寫入 pending
    pending1 = PinPending(
        repo="agora",
        base_manifest_sha256="m" * 64,
        refs={"refs/heads/main": "b" * 40},
        annex_keys=frozenset({"k1", "k2"}),
        written_at="2026-09-27T08:05:00Z",
        run_id="run-2",
    )
    store.write_pending(pending1)
    s2, p2 = store.load("agora")
    assert s2 == state1
    assert p2 == pending1

    # drop_pending
    store.drop_pending("agora")
    s3, p3 = store.load("agora")
    assert p3 is None

    # promote
    state2 = PinState(
        repo="agora",
        repo_uuid="01234567-89ab-cdef-0123-456789abcdef",
        refs={"refs/heads/main": "b" * 40},
        manifest_sha256="n" * 64,
        prev_manifest_sha256="m" * 64,
        active_bundles=("b1", "b2"),
        removed_bundles=frozenset(),
        annex_keys=frozenset({"k1", "k2"}),
        promoted_at="2026-09-27T08:10:00Z",
        run_id="run-2",
    )
    store.write_pending(pending1)
    store.promote(state2)
    s4, p4 = store.load("agora")
    assert s4 == state2
    assert p4 is None


def test_git_pin_store_smoke(tmp_path: Path):
    """測試 GitPinStore 於本機 bare repo 之讀寫、轉正與丟棄待定流程。"""
    # 1. 建立裸遠端倉庫 (remote bare repo)
    remote_bare = tmp_path / "pin_remote.git"
    remote_bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote_bare)], check=True, capture_output=True)

    # 2. 建立初始提交 (包含空的 README)
    init_work = tmp_path / "init_work"
    subprocess.run(["git", "clone", str(remote_bare), str(init_work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(init_work), "config", "user.name", "Test Committer"], check=True)
    subprocess.run(["git", "-C", str(init_work), "config", "user.email", "test@test.com"], check=True)
    (init_work / "README.md").write_text("# Pin Repo")
    subprocess.run(["git", "-C", str(init_work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)

    # 3. 測試 GitPinStore
    workdir = tmp_path / "git_pin_work"
    pin_store = GitPinStore(repo_url=str(remote_bare), workdir=workdir)

    # 尚未有 state 時 load 拋出 ReadError
    with pytest.raises(ReadError):
        pin_store.load("agora")

    # 寫入初始 state (promote)
    state1 = PinState(
        repo="agora",
        repo_uuid="01234567-89ab-cdef-0123-456789abcdef",
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256="a" * 64,
        prev_manifest_sha256=None,
        active_bundles=("b1",),
        removed_bundles=frozenset({"old_b"}),
        annex_keys=frozenset({"k1", "k2"}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    pin_store.promote(state1)

    # 驗證 load
    loaded_state, loaded_pending = pin_store.load("agora")
    assert loaded_state == state1
    assert loaded_pending is None

    # 寫入 pending
    pending1 = PinPending(
        repo="agora",
        base_manifest_sha256="a" * 64,
        refs={"refs/heads/main": "2" * 40},
        annex_keys=frozenset({"k1", "k2", "k3"}),
        written_at="2026-09-27T08:05:00Z",
        run_id="run-2",
    )
    pin_store.write_pending(pending1)
    s2, p2 = pin_store.load("agora")
    assert s2 == state1
    assert p2 == pending1

    # 另一個獨立 clone 也能讀取到最新狀態
    other_workdir = tmp_path / "other_pin_work"
    other_store = GitPinStore(repo_url=str(remote_bare), workdir=other_workdir)
    os2, op2 = other_store.load("agora")
    assert os2 == state1
    assert op2 == pending1

    # drop pending
    other_store.drop_pending("agora")
    os3, op3 = other_store.load("agora")
    assert op3 is None


def test_settle_no_pending():
    """當無待定釘選值時，settle 直接回傳 NO_PENDING 與原 state。"""
    state = PinState(
        repo="agora",
        repo_uuid="uuid-01",
        refs={"refs/heads/main": "a" * 40},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=("b1",),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    listing = RepoListing(prefix_folder_id="f0", files=(), subfolders=())
    drive = FakeDrive()
    clock = FixedClock()
    outcome, final_state = settle(state, None, listing, drive, workdir=Path("/tmp"), clock=clock)
    assert outcome == SettleOutcome.NO_PENDING
    assert final_state == state


def test_settle_promoted_and_dropped(tmp_path: Path):
    """測試 settle 成功判定 PROMOTED（遠端等於待定）與 DROPPED（遠端等於正式）。"""
    # 建立一個包含 commit 的本機 git repo 以產出真 bundle
    repo_dir = tmp_path / "src_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)
    (repo_dir / "file.txt").write_text("v1")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v1"], check=True)
    v1_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

    uuid1 = "01234567-89ab-cdef-0123-456789abcdef"
    bundle1 = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid1)
    b1_bytes = bundle1.read_bytes()
    b1_name = bundle1.name
    b1_info = parse_bundle_name(b1_name)
    assert b1_info is not None

    # v2 commit
    (repo_dir / "file.txt").write_text("v2")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v2"], check=True)
    v2_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()
    bundle2 = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid1)
    b2_bytes = bundle2.read_bytes()
    b2_name = bundle2.name
    b2_info = parse_bundle_name(b2_name)
    assert b2_info is not None

    # 1. 測試 PROMOTED：遠端 manifest 包含 bundle2 且 refs == v2，consolidate bundle1
    manifest_v2_bytes = f"{b2_name}\n-{b1_name}\n".encode("utf-8")
    m_v2_sha = hashlib.sha256(manifest_v2_bytes).hexdigest().lower()

    drive = FakeDrive()
    prefix_id = drive.seed_folder("repo_folder")
    m_id = drive.seed_file(prefix_id, f"GITMANIFEST--{uuid1}", manifest_v2_bytes)
    b2_id = drive.seed_file(prefix_id, b2_name, b2_bytes)

    state = PinState(
        repo="agora",
        repo_uuid=uuid1,
        refs={"refs/heads/main": v1_sha},
        manifest_sha256="m_v1_sha_dummy",
        prev_manifest_sha256=None,
        active_bundles=(b1_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset({"k1"}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    pending = PinPending(
        repo="agora",
        base_manifest_sha256="m_v1_sha_dummy",
        refs={"refs/heads/main": v2_sha},
        annex_keys=frozenset({"k1", "k2"}),
        written_at="2026-09-27T08:05:00Z",
        run_id="run-2",
    )

    listing_files = tuple(drive.list_children(prefix_id))
    listing = RepoListing(prefix_folder_id=prefix_id, files=listing_files, subfolders=())
    clock = FixedClock()

    outcome, new_state = settle(
        state,
        pending,
        listing,
        drive,
        workdir=tmp_path / "settle_workdir",
        clock=clock,
    )
    assert outcome == SettleOutcome.PROMOTED
    assert new_state.refs == {"refs/heads/main": v2_sha}
    assert new_state.manifest_sha256 == m_v2_sha
    assert new_state.prev_manifest_sha256 == "m_v1_sha_dummy"
    assert new_state.active_bundles == (b2_name,)
    assert new_state.removed_bundles == frozenset({b1_name})
    assert new_state.annex_keys == frozenset({"k1", "k2"})

    # 2. 測試 DROPPED：遠端 manifest 實際上依然為 v1
    manifest_v1_bytes = f"{b1_name}\n".encode("utf-8")
    m_v1_sha = hashlib.sha256(manifest_v1_bytes).hexdigest().lower()

    drive2 = FakeDrive()
    prefix2_id = drive2.seed_folder("repo2")
    drive2.seed_file(prefix2_id, f"GITMANIFEST--{uuid1}", manifest_v1_bytes)
    drive2.seed_file(prefix2_id, b1_name, b1_bytes)

    state_v1 = PinState(
        repo="agora",
        repo_uuid=uuid1,
        refs={"refs/heads/main": v1_sha},
        manifest_sha256=m_v1_sha,
        prev_manifest_sha256=None,
        active_bundles=(b1_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset({"k1"}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    listing2_files = tuple(drive2.list_children(prefix2_id))
    listing2 = RepoListing(prefix_folder_id=prefix2_id, files=listing2_files, subfolders=())

    pending_v2 = PinPending(
        repo="agora",
        base_manifest_sha256=m_v1_sha,
        refs={"refs/heads/main": v2_sha},
        annex_keys=frozenset({"k1", "k2"}),
        written_at="2026-09-27T08:05:00Z",
        run_id="run-2",
    )

    outcome2, final2 = settle(
        state_v1,
        pending_v2,  # pending 指向 v2，但遠端只有 v1
        listing2,
        drive2,
        workdir=tmp_path / "settle_workdir2",
        clock=clock,
    )
    assert outcome2 == SettleOutcome.DROPPED
    assert final2 == state_v1


def test_settle_bak_recovery(tmp_path: Path):
    """測試主 manifest 缺失但 .bak 內容與重放 refs 均相符時之 BAK_RECOVERY。"""
    repo_dir = tmp_path / "src_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)
    (repo_dir / "file.txt").write_text("bak_content")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "bak commit"], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

    uuid1 = "01234567-89ab-cdef-0123-456789abcdef"
    bundle = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid1)
    b_bytes = bundle.read_bytes()
    b_name = bundle.name

    manifest_bytes = f"{b_name}\n".encode("utf-8")
    m_sha = hashlib.sha256(manifest_bytes).hexdigest().lower()

    drive = FakeDrive()
    prefix_id = drive.seed_folder("repo_bak")
    # 僅有 .bak，無主 manifest
    drive.seed_file(prefix_id, f"GITMANIFEST--{uuid1}.bak", manifest_bytes)
    drive.seed_file(prefix_id, b_name, b_bytes)

    state = PinState(
        repo="agora",
        repo_uuid=uuid1,
        refs={"refs/heads/main": sha},
        manifest_sha256=m_sha,
        prev_manifest_sha256=None,
        active_bundles=(b_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    pending = PinPending(
        repo="agora",
        base_manifest_sha256=m_sha,
        refs={"refs/heads/main": "different_sha" * 4},
        annex_keys=frozenset(),
        written_at="2026-09-27T08:05:00Z",
        run_id="run-2",
    )

    listing_files = tuple(drive.list_children(prefix_id))
    listing = RepoListing(prefix_folder_id=prefix_id, files=listing_files, subfolders=())
    clock = FixedClock()

    outcome, final_state = settle(
        state,
        pending,
        listing,
        drive,
        workdir=tmp_path / "settle_bak_workdir",
        clock=clock,
    )
    assert outcome == SettleOutcome.BAK_RECOVERY
    assert final_state == state


def test_check_parents_smoke():
    """測試 check_parents 逐層同名資料夾偵測與預期 ID 檢查。"""
    drive = FakeDrive()
    root_id = drive.seed_folder("root")
    p1_id = drive.seed_folder("sub1", parent=root_id)
    p2_id = drive.seed_folder("sub2", parent=p1_id)

    # 1. 正常情況：各層級均有預期 ID，且無重複資料夾
    levels = [
        PrefixLevel(parent_id=root_id, name="sub1", expected_id=p1_id),
        PrefixLevel(parent_id=p1_id, name="sub2", expected_id=p2_id),
    ]
    quarantine = check_parents(levels, drive)
    assert quarantine == []

    # 2. 發現同名重複資料夾：另一個 sub1 在 root 下
    p1_dup_id = drive.seed_folder("sub1", parent=root_id)
    quarantine = check_parents(levels, drive)
    assert len(quarantine) == 1
    assert quarantine[0].id == p1_dup_id

    # 3. 預期 ID 不在清單中 -> 拋出 MismatchError
    bad_levels = [
        PrefixLevel(parent_id=root_id, name="sub1", expected_id="non_existent_folder_id"),
    ]
    with pytest.raises(MismatchError):
        check_parents(bad_levels, drive)


def test_plan_sweep_smoke():
    """測試 plan_sweep 純函式對各類檔案與資料夾之判定決策。"""
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    m_sha = "1" * 64
    prev_m_sha = "2" * 64
    wrong_sha = "3" * 64
    b_act_sha = "a" * 64
    b_rem_sha = "b" * 64
    b_unk_sha = "c" * 64
    annex1_sha = "d" * 64
    annex_unk_sha = "e" * 64

    b_act_name = f"GITBUNDLE-s100--{uuid}-{b_act_sha}"
    b_rem_name = f"GITBUNDLE-s200--{uuid}-{b_rem_sha}"
    b_unk_name = f"GITBUNDLE-s300--{uuid}-{b_unk_sha}"
    annex1_name = f"SHA256E-s50--{annex1_sha}"
    annex_unk_name = f"SHA256E-s60--{annex_unk_sha}"

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256=m_sha,
        prev_manifest_sha256=prev_m_sha,
        active_bundles=(b_act_name,),
        removed_bundles=frozenset({b_rem_name}),
        annex_keys=frozenset({annex1_name}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    sub_id = drive.seed_folder("nested_folder", parent=p_id)
    subfolder_file = drive.get(sub_id)

    # 1. 正確主 manifest
    m1_id = drive.seed_file(p_id, f"GITMANIFEST--{uuid}", b"active_bundle\n", sha256=m_sha)
    # 2. 重複主 manifest
    m2_id = drive.seed_file(p_id, f"GITMANIFEST--{uuid}", b"active_bundle\n", sha256=m_sha)
    # 3. 雜湊不符之主 manifest
    m3_id = drive.seed_file(p_id, f"GITMANIFEST--{uuid}", b"wrong\n", sha256=wrong_sha)
    # 4. 正確 .bak（符合 prev_manifest_sha256）
    bak1_id = drive.seed_file(p_id, f"GITMANIFEST--{uuid}.bak", b"prev\n", sha256=prev_m_sha)
    # 5. 雜湊不符之 .bak
    bak2_id = drive.seed_file(p_id, f"GITMANIFEST--{uuid}.bak", b"wrong\n", sha256=wrong_sha)
    # 6. active bundle
    b1_id = drive.seed_file(
        p_id,
        b_act_name,
        b"x" * 100,
        sha256=b_act_sha,
    )
    # 7. active bundle 重複
    b1_dup_id = drive.seed_file(
        p_id,
        b_act_name,
        b"x" * 100,
        sha256=b_act_sha,
    )
    # 8. removed bundle
    b_rem_id = drive.seed_file(
        p_id,
        b_rem_name,
        b"r" * 200,
        sha256=b_rem_sha,
    )
    # 9. unknown bundle
    b_unk_id = drive.seed_file(
        p_id,
        b_unk_name,
        b"u" * 300,
        sha256=b_unk_sha,
    )
    # 10. 正確 annex 物件
    annex1_id = drive.seed_file(
        p_id,
        annex1_name,
        b"k" * 50,
        sha256=annex1_sha,
    )
    # 11. 重複 annex 物件
    annex1_dup_id = drive.seed_file(
        p_id,
        annex1_name,
        b"k" * 50,
        sha256=annex1_sha,
    )
    # 12. 未釘選之 annex 物件
    annex_unk_id = drive.seed_file(
        p_id,
        annex_unk_name,
        b"z" * 60,
        sha256=annex_unk_sha,
    )
    # 13. 未知檔案
    junk_id = drive.seed_file(p_id, "random.txt", b"junk", sha256="f" * 64)
    # 14. 缺少 sha256 之檔案
    no_sha_id = drive.seed_file(p_id, annex1_name, b"k" * 50, sha256=None)

    files = [
        drive.get(m1_id),
        drive.get(m2_id),
        drive.get(m3_id),
        drive.get(bak1_id),
        drive.get(bak2_id),
        drive.get(b1_id),
        drive.get(b1_dup_id),
        drive.get(b_rem_id),
        drive.get(b_unk_id),
        drive.get(annex1_id),
        drive.get(annex1_dup_id),
        drive.get(annex_unk_id),
        drive.get(junk_id),
        drive.get(no_sha_id),
    ]
    listing = RepoListing(prefix_folder_id=p_id, files=tuple(files), subfolders=(subfolder_file,))

    decisions = plan_sweep(listing, state, repo_uuid=uuid)
    dec_by_id = {d.file.id: d for d in decisions}

    assert dec_by_id[sub_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[m1_id].disposition == Disposition.KEEP
    assert dec_by_id[m2_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[m3_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[bak1_id].disposition == Disposition.KEEP
    assert dec_by_id[bak2_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[b1_id].disposition == Disposition.KEEP
    assert dec_by_id[b1_dup_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[b_rem_id].disposition == Disposition.GC
    assert dec_by_id[b_unk_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[annex1_id].disposition == Disposition.KEEP
    assert dec_by_id[annex1_dup_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[annex_unk_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[junk_id].disposition == Disposition.QUARANTINE
    assert dec_by_id[no_sha_id].disposition == Disposition.NEED_CONTENT_CHECK


def test_resolve_content_checks_smoke():
    """測試 resolve_content_checks 對缺失雜湊之檔案下載計算並更新判定。"""
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    annex_content = b"my_annex_content"
    annex_sha = hashlib.sha256(annex_content).hexdigest().lower()
    annex_name = f"SHA256E-s{len(annex_content)}--{annex_sha}"

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset({annex_name}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    fid = drive.seed_file(p_id, annex_name, annex_content, sha256=None)
    f = drive.get(fid)

    decision = SweepDecision(
        file=f,
        disposition=Disposition.NEED_CONTENT_CHECK,
        reason="缺少 sha256",
    )
    cache: dict = {}
    resolved = resolve_content_checks([decision], drive, cache, state, repo_uuid=uuid)
    assert len(resolved) == 1
    assert resolved[0].disposition == Disposition.KEEP
    cache_key = (fid, f.size, f.md5 or f.modified_time)
    assert cache[cache_key] == (annex_sha, len(annex_content))


def test_apply_sweep_smoke():
    """測試 apply_sweep 隔離搬移與 dry_run 行為。"""
    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    q_id = drive.seed_folder("quarantine")

    keep_fid = drive.seed_file(p_id, "keep.txt", b"keep")
    quar_fid = drive.seed_file(p_id, "quar.txt", b"quar")

    decisions = [
        SweepDecision(file=drive.get(keep_fid), disposition=Disposition.KEEP, reason="keep"),
        SweepDecision(file=drive.get(quar_fid), disposition=Disposition.QUARANTINE, reason="quarantine"),
    ]

    # dry_run: 回傳 1，但不實際移動
    count = apply_sweep(decisions, drive, prefix_folder_id=p_id, quarantine_folder_id=q_id, dry_run=True)
    assert count == 1
    assert p_id in drive.get(quar_fid).parents

    # 實際移動
    count2 = apply_sweep(decisions, drive, prefix_folder_id=p_id, quarantine_folder_id=q_id, dry_run=False)
    assert count2 == 1
    quar_parents = drive.get(quar_fid).parents
    assert p_id not in quar_parents
    quar_parent_folder = drive.get(quar_parents[0])
    assert q_id in quar_parent_folder.parents

    # 移動失敗拋出 AbortRun
    drive.inject("move", error=WriteError)
    with pytest.raises(AbortRun) as exc_info:
        apply_sweep(decisions, drive, prefix_folder_id=p_id, quarantine_folder_id=q_id, dry_run=False)
    assert exc_info.value.step == "sweep"
    assert exc_info.value.code == "move_failed"


def test_verify_clone_and_precheck_smoke():
    """測試 clone 成果核對與 push 前預檢。"""
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    git_ok = DummyAnnexGit({"refs/heads/main": "1" * 40})
    git_bad = DummyAnnexGit({"refs/heads/main": "2" * 40})

    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    m_name = f"GITMANIFEST--{uuid}"
    drive.seed_file(p_id, m_name, b"dummy", sha256="m" * 64)

    # 1. verify_clone: refs 相符且 manifest 相符
    verify_clone(git_ok, state, drive=drive, prefix_folder_id=p_id)
    precheck(drive, p_id, m_name, state)

    # 2. refs 不符拋出 MismatchError
    with pytest.raises(MismatchError):
        verify_clone(git_bad, state, drive=drive, prefix_folder_id=p_id)

    # 3. 遠端主 manifest 數量異常（新增第二個同名）
    drive.seed_file(p_id, m_name, b"dummy2", sha256="m" * 64)
    with pytest.raises(MismatchError):
        verify_clone(git_ok, state, drive=drive, prefix_folder_id=p_id)
    with pytest.raises(MismatchError):
        precheck(drive, p_id, m_name, state)


def test_verify_after_push_smoke(tmp_path: Path):
    """測試 push 後遠端驗證 5 條規則與異常防護。"""
    repo_dir = tmp_path / "src_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)

    # commit 1
    (repo_dir / "file.txt").write_text("v1")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v1"], check=True)
    v1_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    b1_path = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid)
    b1_bytes = b1_path.read_bytes()
    b1_name = b1_path.name

    # commit 2
    (repo_dir / "file.txt").write_text("v2")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v2"], check=True)
    v2_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

    b2_path = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid)
    b2_bytes = b2_path.read_bytes()
    b2_name = b2_path.name

    # 設定環境
    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    b1_id = drive.seed_file(p_id, b1_name, b1_bytes, created_time="2026-09-27T08:00:00Z")
    m_old_id = drive.seed_file(p_id, f"GITMANIFEST--{uuid}", f"{b1_name}\n".encode("utf-8"))

    listing_before = RepoListing(
        prefix_folder_id=p_id,
        files=(drive.get(b1_id), drive.get(m_old_id)),
        subfolders=(),
    )

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": v1_sha},
        manifest_sha256="m1" * 32,
        prev_manifest_sha256=None,
        active_bundles=(b1_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    # 模擬 push 發生：上傳 b2，並更新 manifest
    b2_id = drive.seed_file(p_id, b2_name, b2_bytes, created_time="2026-09-27T08:10:00Z")
    manifest_v2_bytes = f"{b1_name}\n{b2_name}\n".encode("utf-8")
    drive.update_content(m_old_id, manifest_v2_bytes)

    local_refs = {"refs/heads/main": v2_sha}
    git = DummyAnnexGit(local_refs)
    push_started_at = "2026-09-27T08:05:00Z"

    # 1. 成功驗證
    res = verify_after_push(
        git,
        drive,
        listing_before,
        state,
        local_refs,
        push_started_at,
        workdir=tmp_path / "verify_work",
    )
    assert res.active == (b1_name, b2_name)
    assert res.removed == frozenset()
    assert res.new_manifest_sha256 == hashlib.sha256(manifest_v2_bytes).hexdigest().lower()

    # 2. 失敗情況：ls_remote 不符
    with pytest.raises(MismatchError):
        verify_after_push(
            DummyAnnexGit({"refs/heads/main": "other" * 4}),
            drive,
            listing_before,
            state,
            local_refs,
            push_started_at,
            workdir=tmp_path / "verify_work",
        )

    # 3. 失敗情況：新增之 bundle created_time 早於 push 開始時間
    late_push_time = "2026-09-27T08:15:00Z"
    with pytest.raises(MismatchError):
        verify_after_push(
            git,
            drive,
            listing_before,
            state,
            local_refs,
            late_push_time,
            workdir=tmp_path / "verify_work",
        )

    # 4. 失敗情況：新增之 bundle 已存在於 listing_before
    listing_before_with_b2 = RepoListing(
        prefix_folder_id=p_id,
        files=(drive.get(b1_id), drive.get(b2_id)),
        subfolders=(),
    )
    with pytest.raises(MismatchError):
        verify_after_push(
            git,
            drive,
            listing_before_with_b2,
            state,
            local_refs,
            push_started_at,
            workdir=tmp_path / "verify_work",
        )


def test_gc_and_purge_quarantine_smoke():
    """測試 collect_removed_bundles、gc_removed 與 purge_quarantine。"""
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=("b1",),
        removed_bundles=frozenset({"rem1", "rem2"}),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    q_id = drive.seed_folder("quarantine")

    f_active = drive.seed_file(p_id, "b1", b"active")
    f_rem1 = drive.seed_file(p_id, "rem1", b"rem1")
    f_rem2 = drive.seed_file(p_id, "rem2", b"rem2")

    listing = RepoListing(
        prefix_folder_id=p_id,
        files=(drive.get(f_active), drive.get(f_rem1), drive.get(f_rem2)),
        subfolders=(),
    )

    # 1. collect_removed_bundles
    collected = collect_removed_bundles(listing, state)
    assert {f.name for f in collected} == {"rem1", "rem2"}

    # 2. gc_removed dry_run
    gc_count = gc_removed(collected, drive, prefix_folder_id=p_id, state=state, dry_run=True)
    assert gc_count == 2
    assert drive.get(f_rem1) is not None

    # 3. max_delete 上限為 1
    gc_count1 = gc_removed(collected, drive, prefix_folder_id=p_id, state=state, max_delete=1, dry_run=False)
    assert gc_count1 == 1
    # rem1 被刪除，rem2 仍在
    with pytest.raises(NotFound):
        drive.get(f_rem1)
    assert drive.get(f_rem2) is not None

    # 4. gc_removed 防呆：檔案名稱不在 state.removed_bundles
    active_df = drive.get(f_active)
    with pytest.raises(MismatchError):
        gc_removed([active_df], drive, prefix_folder_id=p_id, state=state)

    # 5. gc_removed 防呆：parents 不包含 prefix_folder_id
    orphan_id = drive.seed_file(q_id, "rem2", b"rem2")
    with pytest.raises(MismatchError):
        gc_removed([drive.get(orphan_id)], drive, prefix_folder_id=p_id, state=state)

    # 6. purge_quarantine
    # 建立 10 天前檔案與 1 天前檔案
    old_fid = drive.seed_file(q_id, "old.txt", b"old", created_time="2026-09-17T00:00:00Z")
    new_fid = drive.seed_file(q_id, "new.txt", b"new", created_time="2026-09-26T00:00:00Z")

    now = "2026-09-27T12:00:00Z"
    # dry run
    p_dry = purge_quarantine(drive, q_id, older_than_days=7, now=now, dry_run=True)
    assert p_dry == 1
    assert drive.get(old_fid) is not None

    # 實際 purge
    p_real = purge_quarantine(drive, q_id, older_than_days=7, now=now, dry_run=False)
    assert p_real == 1
    with pytest.raises(NotFound):
        drive.get(old_fid)
    assert drive.get(new_fid) is not None


def test_purge_quarantine_dated_subfolders_smoke():
    """測試 purge_quarantine 依日期子資料夾（YYYY-MM-DD）判定逾期刪除 (H3)。"""
    drive = FakeDrive()
    q_id = drive.seed_folder("quarantine")

    # 12 天前的日期子資料夾
    old_folder_id = drive.seed_folder("2026-09-15", parent=q_id)
    drive.seed_file(old_folder_id, "item1.txt", b"item1")

    # 1 天前的日期子資料夾
    new_folder_id = drive.seed_folder("2026-09-26", parent=q_id)
    drive.seed_file(new_folder_id, "item2.txt", b"item2")

    now = "2026-09-27T12:00:00Z"
    # purge 超過 7 天者
    deleted = purge_quarantine(drive, q_id, older_than_days=7, now=now, dry_run=False)
    assert deleted == 1

    # 逾期資料夾已被永久刪除
    with pytest.raises(NotFound):
        drive.get(old_folder_id)

    # 仍在保留期內的資料夾保留
    assert drive.get(new_folder_id) is not None


def test_resolve_content_checks_replan_duplicate():
    """測試 resolve_content_checks 全量重跑 plan_sweep 能正確將重複之 manifest 標為 QUARANTINE (M8)。"""
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    main_name = f"GITMANIFEST--{uuid}"
    manifest_bytes = b"my_manifest_content\n"
    m_sha = hashlib.sha256(manifest_bytes).hexdigest().lower()

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256=m_sha,
        prev_manifest_sha256=None,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    drive = FakeDrive()
    p_id = drive.seed_folder("repo_root")
    # 兩個內容相同的主 manifest，第一個有 sha256，第二個沒有 sha256
    fid1 = drive.seed_file(p_id, main_name, manifest_bytes, sha256=m_sha)
    fid2 = drive.seed_file(p_id, main_name, manifest_bytes, sha256=None)

    listing = RepoListing(
        prefix_folder_id=p_id,
        files=(drive.get(fid1), drive.get(fid2)),
        subfolders=(),
    )

    initial_decisions = plan_sweep(listing, state, repo_uuid=uuid)
    assert len(initial_decisions) == 2
    assert initial_decisions[0].disposition == Disposition.KEEP
    assert initial_decisions[1].disposition == Disposition.NEED_CONTENT_CHECK

    cache: dict[str, str] = {}
    resolved = resolve_content_checks(
        initial_decisions,
        drive,
        cache,
        state,
        repo_uuid=uuid,
        listing=listing,
    )
    assert len(resolved) == 2
    # 全量重新 plan_sweep 後：恰好一個 KEEP，重複者被判定為 QUARANTINE
    assert resolved[0].disposition == Disposition.KEEP
    assert resolved[1].disposition == Disposition.QUARANTINE
    assert "重複" in resolved[1].reason


def test_plan_readview_sweep_smoke():
    """測試 plan_readview_sweep 介面 stub (M9)。"""
    drive = FakeDrive()
    rv_id = drive.seed_folder("readview_root")
    sub_id = drive.seed_folder("unauthorized_sub", parent=rv_id)
    f_trusted = drive.seed_file(rv_id, "trusted.json", b"trusted")
    f_untrusted = drive.seed_file(rv_id, "untrusted.json", b"untrusted")

    listing = RepoListing(
        prefix_folder_id=rv_id,
        files=(drive.get(f_trusted), drive.get(f_untrusted)),
        subfolders=(drive.get(sub_id),),
    )

    decisions = plan_readview_sweep(
        listing,
        trusted_file_ids={f_trusted},
        readview_folder_id=rv_id,
    )
    assert len(decisions) == 3
    dec_map = {d.file.id: d for d in decisions}
    assert dec_map[f_trusted].disposition == Disposition.KEEP
    assert dec_map[f_untrusted].disposition == Disposition.QUARANTINE
    assert dec_map[sub_id].disposition == Disposition.QUARANTINE
    for d in decisions:
        assert d.from_parent == rv_id


@pytest.mark.parametrize("scenario", ["no_pending", "promoted_with_pending", "content_check_needed"])
def test_run_settle_and_sweep_no_moves_on_any_read_error(tmp_path: Path, scenario: str):
    """H4 & R3 性質測試：在 3 種情境下，任何 ReadError 之下絕無任何 WRITE_OPS 發生且不移動任何檔案，promoted 亦不寫入 pins。"""
    repo_dir = tmp_path / f"src_repo_{scenario}"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)
    (repo_dir / "file.txt").write_text("v1")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v1"], check=True)
    v1_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    bundle1 = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid)
    b1_bytes = bundle1.read_bytes()
    b1_name = bundle1.name

    m1_bytes = f"{b1_name}\n".encode("utf-8")
    m1_sha = hashlib.sha256(m1_bytes).hexdigest().lower()

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": v1_sha},
        manifest_sha256=m1_sha,
        prev_manifest_sha256=None,
        active_bundles=(b1_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    # 為 scenario 2 準備 v2 commit, bundle2 與 pending
    (repo_dir / "file2.txt").write_text("v2")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file2.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "v2"], check=True)
    v2_sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()
    bundle2 = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid)
    b2_bytes = bundle2.read_bytes()
    b2_name = bundle2.name
    m2_bytes = f"{b1_name}\n{b2_name}\n".encode("utf-8")

    pending: PinPending | None = None
    if scenario == "promoted_with_pending":
        pending = PinPending(
            repo="agora",
            base_manifest_sha256=m1_sha,
            refs={"refs/heads/main": v2_sha},
            annex_keys=frozenset(),
            written_at="2026-09-27T08:30:00Z",
            run_id="run-2",
        )

    def create_seeded_drive() -> tuple[FakeDrive, str, str, str]:
        d = FakeDrive()
        root_id = d.seed_folder("root")
        p_id = d.seed_folder("repo", parent=root_id)
        q_id = d.seed_folder("quarantine")
        # 上層多餘同名資料夾（應被 check_parents 隔離）
        d.seed_folder("repo", parent=root_id)

        if scenario == "no_pending":
            d.seed_file(p_id, f"GITMANIFEST--{uuid}", m1_bytes)
            d.seed_file(p_id, b1_name, b1_bytes)
            d.seed_file(p_id, "unknown_intruder.txt", b"bad_content")
        elif scenario == "promoted_with_pending":
            d.seed_file(p_id, f"GITMANIFEST--{uuid}", m2_bytes)
            d.seed_file(p_id, b1_name, b1_bytes)
            d.seed_file(p_id, b2_name, b2_bytes)
            d.seed_file(p_id, "unknown_intruder.txt", b"bad_content")
        elif scenario == "content_check_needed":
            d.seed_file(p_id, f"GITMANIFEST--{uuid}", m1_bytes)
            b1_fid = d.seed_file(p_id, b1_name, b1_bytes)
            d.set_checksum(b1_fid, None)  # 觸發 NEED_CONTENT_CHECK 下載路徑
            d.seed_file(p_id, "unknown_intruder.txt", b"bad_content")

        return d, root_id, p_id, q_id

    # 1. 基準執行
    baseline_drive, r_id, pref_id, quar_id = create_seeded_drive()
    levels = [PrefixLevel(parent_id=r_id, name="repo", expected_id=pref_id)]

    settle_reads_count = [0]

    class TrackedPinStore(MemoryPinStore):
        def promote(self, state: PinState) -> None:
            settle_reads_count[0] = sum(1 for op, _ in baseline_drive.calls if op in FakeDrive.READ_OPS)
            super().promote(state)

    baseline_pins = TrackedPinStore(initial_state=state, initial_pending=pending)

    res = run_settle_and_sweep(
        state,
        pending,
        drive=baseline_drive,
        pins=baseline_pins,
        levels=levels,
        prefix_folder_id=pref_id,
        quarantine_folder_id=quar_id,
        repo_uuid=uuid,
        workdir=tmp_path / f"baseline_work_{scenario}",
    )
    assert res.moved_count == 2
    assert any(op in FakeDrive.WRITE_OPS for op, _ in baseline_drive.calls)

    total_reads = sum(1 for op, _ in baseline_drive.calls if op in FakeDrive.READ_OPS)
    assert total_reads >= 3, f"預期至少有 3 次讀取，實際為 {total_reads}"

    # 2. 窮舉測試：對每一次讀取注入 ReadError，驗證絕無 WRITE_OPS，且 settle 階段 pins 不被寫入
    for nth in range(total_reads):
        test_drive, r_id, pref_id, quar_id = create_seeded_drive()
        test_drive.inject_nth_read(nth, error=ReadError)
        test_pins = MemoryPinStore(initial_state=state, initial_pending=pending)
        with pytest.raises(ReadError):
            run_settle_and_sweep(
                state,
                pending,
                drive=test_drive,
                pins=test_pins,
                levels=[PrefixLevel(parent_id=r_id, name="repo", expected_id=pref_id)],
                prefix_folder_id=pref_id,
                quarantine_folder_id=quar_id,
                repo_uuid=uuid,
                workdir=tmp_path / f"work_{scenario}_inject_{nth}",
            )

        # 核心不變量斷言：calls 內不可有任何 WRITE_OPS
        writes = [call for call in test_drive.calls if call[0] in FakeDrive.WRITE_OPS]
        assert writes == [], f"[{scenario}] 在第 {nth} 次讀取注入 ReadError 時違反保證，發生了寫入操作: {writes}"

        # R3 保證：settle 讀取失敗時 pins 不可被 promote 或寫入
        loaded_state, loaded_pending = test_pins.load("agora")
        if scenario == "promoted_with_pending":
            if nth < settle_reads_count[0]:
                assert loaded_state == state, f"[{scenario}] 在第 {nth} 次讀取（settle 階段）注入 ReadError 時 pins 遭提前寫入"
                assert loaded_pending == pending
        else:
            assert loaded_state == state, f"[{scenario}] 在第 {nth} 次讀取注入 ReadError 時 pins 遭寫入"


def test_sweep_composite_cache_key_invalidation(tmp_path: Path):
    """R1: 同一 fid 在內容變更（md5/modified_time 改變）後，不可命中舊快取，必須重新下載並正確隔離。"""
    drive = FakeDrive()
    p_id = drive.seed_folder("repo")
    uuid = "01234567-89ab-cdef-0123-456789abcdef"

    # 正確的 bundle 內容與雜湊
    correct_bytes = b"correct bundle content"
    correct_sha = hashlib.sha256(correct_bytes).hexdigest().lower()
    b_name = f"GITBUNDLE-s{len(correct_bytes)}--{uuid}-{correct_sha}"

    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=(b_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )

    # 第一次：正確內容，但 sha256 為 None
    fid = drive.seed_file(p_id, b_name, correct_bytes, modified_time="2026-09-27T08:00:00Z")
    drive.set_checksum(fid, None)

    listing1 = RepoListing(
        prefix_folder_id=p_id,
        files=(drive.get(fid),),
        subfolders=(),
    )
    decisions1 = plan_sweep(listing1, state, repo_uuid=uuid, prefix_folder_id=p_id)
    assert decisions1[0].disposition == Disposition.NEED_CONTENT_CHECK

    cache: dict = {}
    resolved1 = resolve_content_checks(
        decisions1, drive, cache, state, repo_uuid=uuid, listing=listing1, prefix_folder_id=p_id
    )
    assert resolved1[0].disposition == Disposition.KEEP

    # 第二次：同一 fid 被改寫為損毀內容，同時 modified_time 改變
    tampered_bytes = b"tampered bad content"
    drive.seed_file(p_id, b_name, tampered_bytes, modified_time="2026-09-27T09:00:00Z", file_id=fid)
    drive.set_checksum(fid, None)

    listing2 = RepoListing(
        prefix_folder_id=p_id,
        files=(drive.get(fid),),
        subfolders=(),
    )
    decisions2 = plan_sweep(listing2, state, repo_uuid=uuid, prefix_folder_id=p_id)
    assert decisions2[0].disposition == Disposition.NEED_CONTENT_CHECK

    resolved2 = resolve_content_checks(
        decisions2, drive, cache, state, repo_uuid=uuid, listing=listing2, prefix_folder_id=p_id
    )
    # 必須重新下載並發現雜湊不符，判定為 QUARANTINE（若命中舊 fid 快取則會誤判為 KEEP）
    assert resolved2[0].disposition == Disposition.QUARANTINE


def test_git_pin_store_r2_url_whitelist_and_ssh_isolation(tmp_path: Path):
    """R2: URL 白名單、非 CI 拒絕正式 repo、以及嚴格 SSH 命令隔離。"""
    workdir = tmp_path / "work"

    # 1. 非白名單形式 URL 必須 raise ValueError
    with pytest.raises(ValueError, match="不允許的 pin repo URL 形式"):
        GitPinStore(repo_url="github-pin:owner/repo", workdir=workdir)

    with pytest.raises(ValueError, match="不允許的 pin repo URL 形式"):
        GitPinStore(repo_url="https://github.com/owner/repo.git", workdir=workdir)

    # 2. 非 CI 環境寫入正式 pin repo (MyAiStorage-pin) 必須拋出 PermissionError
    old_ci = os.environ.get("GITHUB_ACTIONS")
    try:
        os.environ["GITHUB_ACTIONS"] = "false"
        with pytest.raises(PermissionError, match="非 CI 環境.*禁止寫入正式 pin repo"):
            GitPinStore(
                repo_url="git@github.com:myorg/MyAiStorage-pin.git",
                workdir=workdir,
                key_path=tmp_path / "dummy_key",
                known_hosts_path=tmp_path / "dummy_kh",
            )
    finally:
        if old_ci is not None:
            os.environ["GITHUB_ACTIONS"] = old_ci
        else:
            os.environ.pop("GITHUB_ACTIONS", None)

    # 3. 沒有提供 key 時，一律設定 -F /dev/null -o IdentityAgent=none，使其無法認證
    store_local = GitPinStore(repo_url=f"file://{tmp_path}", workdir=workdir)
    assert "IdentityAgent=none" in store_local._env.get("GIT_SSH_COMMAND", "")
    assert "-F /dev/null" in store_local._env.get("GIT_SSH_COMMAND", "")


def test_apply_sweep_multiple_quarantine_folders_aborts(tmp_path: Path):
    """L: 隔離資料夾中存在多個同名日期子資料夾時，raise AbortRun 中止。"""
    drive = FakeDrive()
    p_id = drive.seed_folder("repo")
    q_id = drive.seed_folder("quarantine")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    drive.seed_folder(today_str, parent=q_id)
    drive.seed_folder(today_str, parent=q_id)  # 重複同名資料夾

    bad_file = drive.seed_file(p_id, "bad.txt", b"junk")
    decisions = [
        SweepDecision(
            file=drive.get(bad_file),
            disposition=Disposition.QUARANTINE,
            reason="test",
            from_parent=p_id,
        )
    ]
    with pytest.raises(AbortRun, match="存在多個同名的日期子資料夾"):
        apply_sweep(decisions, drive, quarantine_folder_id=q_id)


def test_git_pin_store_missing_annex_keys_fields_raises_read_error(tmp_path: Path):
    """L: 正式釘選值缺少 annex_keys_count 或 annex_keys_sha256 必須拋出 ReadError。"""
    pin_remote = tmp_path / "pin_remote_missing_keys.git"
    init_work = tmp_path / "init_work_missing_keys"
    _init_bare_pin_repo(pin_remote, init_work)

    pin_dir = init_work / ".pin"
    pin_dir.mkdir(parents=True, exist_ok=True)
    state_json = {
        "repo": "agora",
        "repo_uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "refs": {"refs/heads/main": "1" * 40},
        "manifest_sha256": "m" * 64,
        "prev_manifest_sha256": None,
        "active_bundles": [],
        "removed_bundles": [],
        "promoted_at": "2026-09-27T08:00:00Z",
        "run_id": "run-1",
        # 故意不提供 annex_keys_count 與 annex_keys_sha256
    }
    (pin_dir / "agora.json").write_text(json.dumps(state_json), encoding="utf-8")
    (pin_dir / "agora.keys").write_text("key1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(init_work), "add", ".pin/"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "add missing keys pin"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)

    store = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "clone_missing")
    with pytest.raises(ReadError, match="正式釘選值缺少必填欄位"):
        store.load("agora")


def _init_bare_pin_repo(bare_path: Path, init_work: Path) -> None:
    bare_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_path)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare_path), str(init_work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(init_work), "config", "user.name", "Test Committer"], check=True)
    subprocess.run(["git", "-C", str(init_work), "config", "user.email", "test@test.com"], check=True)
    (init_work / "README.md").write_text("# Pin Repo")
    subprocess.run(["git", "-C", str(init_work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)


def test_git_pin_store_h1_h2_strict_schema(tmp_path: Path):
    """測試 GitPinStore 嚴格 schema 檢查與防呆 (H1, H2)。"""
    pin_remote = tmp_path / "pin_remote.git"
    _init_bare_pin_repo(pin_remote, tmp_path / "init_work1")

    clone_dir = tmp_path / "pin_clone"
    store = GitPinStore(repo_url=f"file://{pin_remote}", workdir=clone_dir)

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    state = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256="m" * 64,
        prev_manifest_sha256=None,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset({"key1"}),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    store.promote(state)
    keys_backup = b"key1\n"

    # 1. 成功載入
    loaded_state, loaded_pending = store.load("agora")
    assert loaded_state == state
    assert loaded_pending is None

    init_work = tmp_path / "init_work1"
    subprocess.run(["git", "-C", str(init_work), "pull"], check=True)

    # 2. H2: 刪除 .keys 檔 -> 拋出 ReadError
    subprocess.run(["git", "-C", str(init_work), "rm", ".pin/agora.keys"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "rm keys"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)
    with pytest.raises(ReadError) as exc_info:
        store.load("agora")
    assert "缺少必填之 keys 檔案" in str(exc_info.value)

    # 3. H2: .keys 雜湊與 json 記錄不符 -> 拋出 ReadError
    (init_work / ".pin" / "agora.keys").write_bytes(b"tampered_keys_data\n")
    subprocess.run(["git", "-C", str(init_work), "add", ".pin/agora.keys"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "tamper keys"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)
    with pytest.raises(ReadError) as exc_info:
        store.load("agora")
    assert "不符" in str(exc_info.value)

    # 4. H1: pending.json 格式損毀 -> 拋出 ReadError（絕不視為 None）
    (init_work / ".pin" / "agora.keys").write_bytes(keys_backup)
    (init_work / ".pin" / "agora.pending.json").write_text("{corrupted_json_syntax")
    (init_work / ".pin" / "agora.pending.keys").write_text("")
    subprocess.run(["git", "-C", str(init_work), "add", ".pin/agora*"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "corrupt pending"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)
    with pytest.raises(ReadError) as exc_info:
        store.load("agora")
    assert "解析待定釘選值 pending.json 失敗" in str(exc_info.value)

    # 5. H1: pending.json 缺少必要欄位 -> 拋出 ReadError
    (init_work / ".pin" / "agora.pending.json").write_text(json.dumps({"repo": "agora"}))
    subprocess.run(["git", "-C", str(init_work), "add", ".pin/agora.pending.json"], check=True)
    subprocess.run(["git", "-C", str(init_work), "commit", "-m", "incomplete pending"], check=True)
    subprocess.run(["git", "-C", str(init_work), "push", "origin", "main"], check=True)
    with pytest.raises(ReadError) as exc_info:
        store.load("agora")
    assert "缺少必填欄位" in str(exc_info.value)


def test_git_pin_store_non_fast_forward_rejected(tmp_path: Path):
    """GitPinStore 遇到「同一路徑的內容衝突」仍然拒絕寫入拋出 WriteError (L)。

    遠端只被「別的 repo 條目」推進（ref 落後）時會自動 rebase 後重試——那是多條線
    共用同一個 pin repo 的正常情況，不算衝突。真正的衝突是同一個檔案被別人改過，
    那必須停下來交人工看（review L 的原意）。
    """
    pin_remote = tmp_path / "pin_remote.git"
    _init_bare_pin_repo(pin_remote, tmp_path / "init_work2")

    store1 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "clone1")
    store2 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "clone2")

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    state1 = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "1" * 40},
        manifest_sha256="m1" * 32,
        prev_manifest_sha256=None,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-1",
    )
    store1.promote(state1)

    # store2 同步載入
    store2.load("agora")

    # store1 先寫入新版本並 push（store2 的 ref 從此落後）
    state2 = PinState(
        repo="agora",
        repo_uuid=uuid,
        refs={"refs/heads/main": "2" * 40},
        manifest_sha256="m2" * 32,
        prev_manifest_sha256="m1" * 32,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:01:00Z",
        run_id="run-2",
    )
    store1.promote(state2)

    # 模擬同一路徑的內容衝突：遠端已改過 agora.json，store2 又改一次 → rebase 失敗
    (tmp_path / "clone2" / ".pin" / "agora.json").write_text('{"repo": "agora", "clobbered": true}')
    with pytest.raises(WriteError):
        store2._commit_and_push("conflict", repo="agora")


def test_git_pin_store_refuses_rebase_when_same_repo_files_changed(tmp_path: Path):
    """M1（review-b1039a8／review-cdb4a34）：遠端動到**同一個 repo** → 中止，不 rebase。

    允許 rebase 的唯一情況是遠端新增的檔案全部屬於其他 repo（多條線共用同一個
    pin repo 時的正常情況）。

    管理者剛上鎖會寫 `.pin/<repo>.maintenance`，那屬於同一個 repo：必須停下來，
    而且要以 `AbortRun("maintenance", "active")` 中止（review-cdb4a34 M1）——
    語意要讓 `run()` 走「維護中」的路徑（整輪停止、不刪收件匣、報告寫
    `maintenance=active`），不是一個「不明原因的寫入失敗」。
    """
    pin_remote = tmp_path / "pin_remote_m1.git"
    _init_bare_pin_repo(pin_remote, tmp_path / "init_work_m1")

    store1 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "c1_m1")
    store2 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "c2_m1")

    uuid = "01234567-89ab-cdef-0123-456789abcdef"

    def _state(repo: str, main_sha: str, run_id: str) -> PinState:
        return PinState(
            repo=repo, repo_uuid=uuid,
            refs={"refs/heads/main": main_sha},
            manifest_sha256=(main_sha[:4] + "m1" * 31)[:64],
            prev_manifest_sha256=None, active_bundles=(), removed_bundles=frozenset(),
            annex_keys=frozenset(), promoted_at="2026-09-27T08:00:00Z", run_id=run_id,
        )

    store1.promote(_state("agora", "1" * 40, "run-1"))
    store2.load("agora")

    # 管理者上鎖（寫同一個 repo 的 .maintenance）
    (tmp_path / "c1_m1" / ".pin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "c1_m1" / ".pin" / "agora.maintenance").write_text(
        '{"reason": "erase", "at": "2026-09-27T09:00:00Z", "by": "user"}',
        encoding="utf-8",
    )
    store1._run_git(["add", ".pin/"])
    store1._run_git(["commit", "-qm", "maintenance on: agora"])
    store1._run_git(["push", "-q", "origin", "main"])

    # store2 這時寫自己的 agora → 遠端有同 repo 的維護旗標 → AbortRun 中止
    with pytest.raises(AbortRun) as excinfo:
        store2.promote(_state("agora", "2" * 40, "run-2"))
    assert excinfo.value.step == "maintenance"
    assert excinfo.value.code == "active"


def test_git_pin_store_recovers_stale_ref_by_rebase(tmp_path: Path):
    """遠端只被別的 repo 條目推進時，push 應自動 rebase 後重試成功。

    這是多條線的整合測試共用同一個 pin-test repo 時的必要行為：否則任何一次
    併發 push 都會讓另一條線的 `pins.write_pending` 直接中止。
    """
    pin_remote = tmp_path / "pin_remote.git"
    _init_bare_pin_repo(pin_remote, tmp_path / "init_work3")

    store1 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "clone1")
    store2 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "clone2")

    uuid = "01234567-89ab-cdef-0123-456789abcdef"

    def _state(repo: str, main_sha: str, run_id: str) -> PinState:
        return PinState(
            repo=repo,
            repo_uuid=uuid,
            refs={"refs/heads/main": main_sha},
            manifest_sha256=(main_sha[:4] + "m1" * 31)[:64],
            prev_manifest_sha256=None,
            active_bundles=(),
            removed_bundles=frozenset(),
            annex_keys=frozenset(),
            promoted_at="2026-09-27T08:00:00Z",
            run_id=run_id,
        )

    # store1 寫 agora（store2 這時還沒 clone，ref 落後）
    store1.promote(_state("agora", "1" * 40, "run-1"))
    store2.load("agora")
    # store1 再寫別的 repo（例如另一條線的 admin 測試用不同 repo 名）
    store1.promote(_state("agora-erase-XYZ", "3" * 40, "run-2"))

    # store2 現在寫自己的 agora：遠端只是多了別的 repo 的條目，應該 rebase 後成功
    store2.promote(_state("agora", "2" * 40, "run-3"))

    # 兩邊的內容都在遠端，沒有互相覆蓋
    store3 = GitPinStore(repo_url=f"file://{pin_remote}", workdir=tmp_path / "clone3")
    state_agora, _ = store3.load("agora")
    state_other, _ = store3.load("agora-erase-XYZ")
    assert state_agora.refs["refs/heads/main"] == "2" * 40
    assert state_other.refs["refs/heads/main"] == "3" * 40


