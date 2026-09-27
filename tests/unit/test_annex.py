"""Unit tests for aistorage.annex module.

Specifications:
- docs/impl/group3-modules.md §3.1 (manifest 與重放) & §8.2 (annex/manifest, replay)
- review-g3a.md (H2 replay 重用 workdir 必須失敗, M4 bundle hash verification and ref normalization)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import pytest

from aistorage.annex import (
    BundleName,
    FakeAnnexGit,
    Manifest,
    normalize_bundle_heads,
    normalize_ls_remote,
    normalize_refs,
    parse_bundle_name,
    parse_manifest,
    replay_refs,
)
from aistorage.errors import MismatchError, WriteError


# ============================================================================
# 1. BundleName & parse_bundle_name Tests
# ============================================================================

def test_parse_bundle_name_valid():
    """驗證標準 GITBUNDLE-s<size>--<uuid>-<sha256> 名稱解析。"""
    uuid_str = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    sha_str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    raw_name = f"GITBUNDLE-s654321--{uuid_str}-{sha_str}"

    b = parse_bundle_name(raw_name)
    assert b is not None
    assert b.name == raw_name
    assert b.size == 654321
    assert b.repo_uuid == uuid_str
    assert b.sha256 == sha_str


def test_parse_bundle_name_invalid_cases():
    """驗證各種格式損毀或非法的 bundle 名稱回傳 None。"""
    # 缺少 -s
    assert parse_bundle_name("GITBUNDLE-654321--uuid-sha") is None
    # 缺少 uuid
    assert parse_bundle_name("GITBUNDLE-s100--sha") is None
    # 大寫 sha256 (M4: 僅允許小寫 hex)
    assert parse_bundle_name("GITBUNDLE-s100--01234567-89ab-cdef-0123-456789abcdef-" + "A" * 64) is None
    # sha 長度不符
    assert parse_bundle_name("GITBUNDLE-s100--01234567-89ab-cdef-0123-456789abcdef-abcd") is None
    # 非 GITBUNDLE 開頭
    assert parse_bundle_name("MYBUNDLE-s100--01234567-89ab-cdef-0123-456789abcdef-" + "a" * 64) is None
    # 一般檔案
    assert parse_bundle_name("README.md") is None
    assert parse_bundle_name("GITMANIFEST--uuid") is None


# ============================================================================
# 2. Manifest & parse_manifest Tests
# ============================================================================

def test_parse_manifest_active_and_removed():
    """驗證正確解析 active 與 removed 行，並依順序保存 active。"""
    uuid = "11111111-2222-3333-4444-555555555555"
    b1 = f"GITBUNDLE-s100--{uuid}-" + "1" * 64
    b2 = f"GITBUNDLE-s200--{uuid}-" + "2" * 64
    b3 = f"GITBUNDLE-s300--{uuid}-" + "3" * 64
    r1 = f"GITBUNDLE-s400--{uuid}-" + "4" * 64
    r2 = f"GITBUNDLE-s500--{uuid}-" + "5" * 64

    # 嚴格的 '-' removed 前綴寫法（不帶多餘空白）
    content = f"{b1}\n{b2}\n-{r1}\n-{r2}\n{b3}\n".encode("utf-8")
    m = parse_manifest(content, repo_uuid=uuid)

    assert m.active == (b1, b2, b3)
    assert m.removed == frozenset({r1, r2})


def test_parse_manifest_empty_active_raises_mismatch():
    """active 清單為空時必須拋出 MismatchError。"""
    uuid = "11111111-2222-3333-4444-555555555555"
    r1 = f"GITBUNDLE-s400--{uuid}-" + "4" * 64
    content = f"-{r1}\n".encode("utf-8")

    with pytest.raises(MismatchError):
        parse_manifest(content, repo_uuid=uuid)

    with pytest.raises(MismatchError):
        parse_manifest(b"\n\n", repo_uuid=uuid)


def test_parse_manifest_wrong_uuid_raises_mismatch():
    """M4: 包含其他 repo_uuid 的 bundle 名稱時必須拋出 MismatchError。"""
    uuid_expected = "11111111-2222-3333-4444-555555555555"
    uuid_other = "99999999-8888-7777-6666-555555555555"
    b_good = f"GITBUNDLE-s100--{uuid_expected}-" + "1" * 64
    b_bad = f"GITBUNDLE-s100--{uuid_other}-" + "2" * 64

    content = f"{b_good}\n{b_bad}\n".encode("utf-8")
    with pytest.raises(MismatchError):
        parse_manifest(content, repo_uuid=uuid_expected)


def test_parse_manifest_duplicate_active_raises_mismatch():
    """M4: active 清單中有重複行時必須拋出 MismatchError。"""
    uuid = "11111111-2222-3333-4444-555555555555"
    b1 = f"GITBUNDLE-s100--{uuid}-" + "1" * 64
    content = f"{b1}\n{b1}\n".encode("utf-8")

    with pytest.raises(MismatchError):
        parse_manifest(content, repo_uuid=uuid)


def test_parse_manifest_intersection_between_active_and_removed_raises():
    """M4: active 與 removed 有交集時必須拋出 MismatchError。"""
    uuid = "11111111-2222-3333-4444-555555555555"
    b1 = f"GITBUNDLE-s100--{uuid}-" + "1" * 64
    content = f"{b1}\n-{b1}\n".encode("utf-8")

    with pytest.raises(MismatchError):
        parse_manifest(content, repo_uuid=uuid)


def test_parse_manifest_invalid_syntax_lines_raises():
    """非法行或亂碼必須拋出 MismatchError。"""
    uuid = "11111111-2222-3333-4444-555555555555"
    b1 = f"GITBUNDLE-s100--{uuid}-" + "1" * 64
    content = f"{b1}\ncorrupted_random_line\n".encode("utf-8")

    with pytest.raises(MismatchError):
        parse_manifest(content, repo_uuid=uuid)


# ============================================================================
# 3. Ref Normalization Tests
# ============================================================================

def test_normalize_bundle_heads_and_ls_remote_equivalence():
    """N1 / M4: bundle heads 與 ls-remote 正規化後必須完全相等。"""
    uuid = "22222222-3333-4444-5555-666666666666"
    commit_main = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    commit_annex = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # git bundle list-heads 輸出形狀：帶 namespace、含 HEAD 與 peeled ref
    bundle_heads_raw = {
        "HEAD": commit_main,
        f"refs/namespaces/git-remote-annex/{uuid}/refs/heads/main": commit_main,
        f"refs/namespaces/git-remote-annex/{uuid}/refs/heads/main^{{}}": commit_main,
        f"refs/namespaces/git-remote-annex/{uuid}/refs/heads/git-annex": commit_annex,
    }

    # git ls-remote 輸出形狀：無 namespace、含 HEAD
    ls_remote_raw = {
        "HEAD": commit_main,
        "refs/heads/main": commit_main,
        "refs/heads/git-annex": commit_annex,
    }

    expected_clean_refs = {
        "refs/heads/main": commit_main,
        "refs/heads/git-annex": commit_annex,
    }

    norm_b = normalize_bundle_heads(bundle_heads_raw, repo_uuid=uuid)
    norm_ls = normalize_ls_remote(ls_remote_raw)
    norm_unified = normalize_refs(ls_remote_raw)

    assert norm_b == expected_clean_refs
    assert norm_ls == expected_clean_refs
    assert norm_unified == expected_clean_refs
    assert norm_b == norm_ls


def test_normalize_bundle_heads_wrong_uuid_raises():
    """bundle heads 的 namespace uuid 與預期不符時拋出 MismatchError。"""
    uuid_expected = "22222222-3333-4444-5555-666666666666"
    uuid_wrong = "99999999-0000-0000-0000-000000000000"
    raw = {
        f"refs/namespaces/git-remote-annex/{uuid_wrong}/refs/heads/main": "a" * 40,
    }
    with pytest.raises(MismatchError):
        normalize_bundle_heads(raw, repo_uuid=uuid_expected)


def test_normalize_ls_remote_with_namespace_prefix_raises():
    """ls-remote 若帶有 namespace 前綴直接拋出 MismatchError。"""
    raw = {
        "refs/namespaces/git-remote-annex/some-uuid/refs/heads/main": "a" * 40,
    }
    with pytest.raises(MismatchError):
        normalize_ls_remote(raw)


# ============================================================================
# 4. Replay Tests & H2 Workdir Isolation Check
# ============================================================================

def _helper_create_bundle(repo_dir: Path, out_dir: Path, ref_spec: str, repo_uuid: str, ns_branch: str = "refs/heads/main") -> tuple[BundleName, Path]:
    """Helper: 在 repo_dir 產生帶有 git-remote-annex namespace 的 bundle。"""
    b_raw = out_dir / f"tmp_{ref_spec.replace('..', '_').replace('/', '_')}.bundle"
    ns_ref = f"refs/namespaces/git-remote-annex/{repo_uuid}/{ns_branch}"

    if ".." in ref_spec:
        base, head = ref_spec.split("..", 1)
        sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", head], text=True).strip()
        subprocess.run(["git", "-C", str(repo_dir), "update-ref", ns_ref, sha], check=True)
        bundle_spec = f"{base}..{ns_ref}"
    else:
        sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", ref_spec], text=True).strip()
        subprocess.run(["git", "-C", str(repo_dir), "update-ref", ns_ref, sha], check=True)
        bundle_spec = ns_ref

    subprocess.run(["git", "-C", str(repo_dir), "bundle", "create", str(b_raw), bundle_spec], check=True, capture_output=True)

    b_bytes = b_raw.read_bytes()
    b_size = len(b_bytes)
    b_sha = hashlib.sha256(b_bytes).hexdigest().lower()
    b_name_str = f"GITBUNDLE-s{b_size}--{repo_uuid}-{b_sha}"
    final_path = out_dir / b_name_str
    b_raw.rename(final_path)

    b_name_obj = BundleName(name=b_name_str, size=b_size, repo_uuid=repo_uuid, sha256=b_sha)
    return b_name_obj, final_path


def test_replay_refs_in_order(tmp_path: Path):
    """驗證依序 unbundle 兩個 bundle，計算出最新的 ref 集合。"""
    repo = tmp_path / "src_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)

    # Commit 1
    (repo / "f1.txt").write_text("v1")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c1"], check=True)
    c1_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    uuid = "33333333-4444-5555-6666-777777777777"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    b1_info, b1_path = _helper_create_bundle(repo, bundles_dir, "refs/heads/main", uuid)

    # Commit 2 (Incremental)
    (repo / "f2.txt").write_text("v2")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c2"], check=True)
    c2_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    b2_info, b2_path = _helper_create_bundle(repo, bundles_dir, f"{c1_sha}..refs/heads/main", uuid)

    workdir = tmp_path / "replay_work"
    refs = replay_refs([b1_path, b2_path], workdir=workdir, repo_uuid=uuid)

    assert "refs/heads/main" in refs
    assert refs["refs/heads/main"] == c2_sha


def test_h2_replay_workdir_reuse_isolation(tmp_path: Path):
    """H2 核心驗證：重用同一個 workdir 時，第二次單獨重放缺基底之增量 bundle 必須失敗。

    若實作有安全漏洞（沿用 workdir 舊物件庫），則第二次重放會在舊 repo 裡找到 Commit 1 而成功；
    正確實作每次必須建立全新 bare repo，因此缺基底必失敗（拋出 MismatchError）。
    """
    repo = tmp_path / "src_repo_h2"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)

    # Commit 1
    (repo / "base.txt").write_text("base")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True)
    c1_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    uuid = "44444444-5555-6666-7777-888888888888"
    bundles_dir = tmp_path / "bundles_h2"
    bundles_dir.mkdir()
    b1_info, b1_path = _helper_create_bundle(repo, bundles_dir, "refs/heads/main", uuid)

    # Commit 2 (Incremental: depends on Commit 1)
    (repo / "inc.txt").write_text("inc")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "inc"], check=True)
    b2_info, b2_path = _helper_create_bundle(repo, bundles_dir, f"{c1_sha}..refs/heads/main", uuid)

    workdir = tmp_path / "shared_workdir"

    # 第一次：完整重放 [b1, b2]，必須成功
    refs1 = replay_refs([b1_path, b2_path], workdir=workdir, repo_uuid=uuid)
    assert "refs/heads/main" in refs1

    # 第二次：使用同一個 workdir 參數，但只傳入缺基底的增量 bundle [b2]
    # 核心斷言：必須拋出 MismatchError，絕不可因為殘留物件而誤判成功！
    with pytest.raises(MismatchError):
        replay_refs([b2_path], workdir=workdir, repo_uuid=uuid)


def test_replay_bundle_sha256_mismatch_raises(tmp_path: Path):
    """M4: bundle 檔案內容被竄改時（雜湊與檔名之 sha256 不符），重放必須拋出 MismatchError。"""
    repo = tmp_path / "src_repo_tamper"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    (repo / "f.txt").write_text("original")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)

    uuid = "55555555-6666-7777-8888-999999999999"
    bundles_dir = tmp_path / "bundles_tamper"
    bundles_dir.mkdir()
    b_info, b_path = _helper_create_bundle(repo, bundles_dir, "refs/heads/main", uuid)

    # 竄改 bundle 內容
    b_path.write_bytes(b_path.read_bytes() + b"\x00\x00corrupted")

    workdir = tmp_path / "tamper_work"
    with pytest.raises(MismatchError):
        replay_refs([b_path], workdir=workdir, repo_uuid=uuid)


# ============================================================================
# 5. Git Clients (FakeAnnexGit, SubprocessAnnexGit)
# ============================================================================

def test_fake_annex_git_push_effects(tmp_path: Path):
    """驗證 FakeAnnexGit 的 push_effect（apply, silent_fail, error）。"""
    initial_refs = {"refs/heads/main": "1" * 40}
    fake_git = FakeAnnexGit(refs=dict(initial_refs), workdir=tmp_path, push_effect="apply")

    assert fake_git.ls_remote("drive") == initial_refs

    # 1. apply 效果：commit 後 push 成功且更新 refs/heads/main
    sha = fake_git.commit("commit msg 2")
    fake_git.push("drive", "main")
    assert fake_git.ls_remote("drive")["refs/heads/main"] == sha

    # 2. silent_fail 效果：push 不拋出例外但 refs 未變更（模擬 D2 靜默失敗）
    fake_silent = FakeAnnexGit(refs=dict(initial_refs), workdir=tmp_path, push_effect="silent_fail")
    fake_silent.commit("new commit")
    fake_silent.push("drive", "main")
    assert fake_silent.ls_remote("drive") == initial_refs

    # 3. error 效果：push 拋出 WriteError
    fake_err = FakeAnnexGit(refs=dict(initial_refs), workdir=tmp_path, push_effect="error")
    with pytest.raises(WriteError):
        fake_err.push("drive", "main")


def test_fake_annex_git_annex_keys(tmp_path: Path):
    """驗證 FakeAnnexGit 對 annex_keys 之紀錄。"""
    keys = frozenset({"SHA256E-s100--abc", "SHA256E-s200--def"})
    fake_git = FakeAnnexGit(workdir=tmp_path, annex_keys=keys)
    assert fake_git.annex_keys_in("some_remote") == keys
