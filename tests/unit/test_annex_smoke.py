"""Smoke and security tests for annex module (manifest, replay, fake, git)."""

import hashlib
from pathlib import Path
import subprocess
import pytest

from aistorage.annex import (
    BundleName,
    FakeAnnexGit,
    Manifest,
    normalize_refs,
    parse_bundle_name,
    parse_manifest,
    replay_refs,
)
from aistorage.errors import MismatchError, WriteError


def test_parse_bundle_name_smoke():
    raw_name = "GITBUNDLE-s12345--01234567-89ab-cdef-0123-456789abcdef-e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    parsed = parse_bundle_name(raw_name)
    assert parsed is not None
    assert parsed.name == raw_name
    assert parsed.size == 12345
    assert parsed.repo_uuid == "01234567-89ab-cdef-0123-456789abcdef"
    assert parsed.sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    assert parse_bundle_name("invalid_bundle_name") is None


def test_parse_manifest_smoke():
    b1 = "GITBUNDLE-s100--uuid1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    b2 = "GITBUNDLE-s200--uuid1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    b_rem = "GITBUNDLE-s50--uuid1-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    manifest_bytes = f"{b1}\n{b2}\n-{b_rem}\n".encode("utf-8")
    m = parse_manifest(manifest_bytes, repo_uuid="uuid1")
    assert m.active == (b1, b2)
    assert m.removed == frozenset({b_rem})

    # 空 active
    with pytest.raises(MismatchError):
        parse_manifest(f"-{b_rem}\n".encode("utf-8"))

    # 非法行
    with pytest.raises(MismatchError):
        parse_manifest(b"corrupted_line\n")

    # M4: 重複 active
    with pytest.raises(MismatchError):
        parse_manifest(f"{b1}\n{b1}\n".encode("utf-8"))

    # M4: active 與 removed 交集
    with pytest.raises(MismatchError):
        parse_manifest(f"{b1}\n-{b1}\n".encode("utf-8"))


def test_normalize_refs():
    uuid1 = "01234567-89ab-cdef-0123-456789abcdef"
    raw = {
        "HEAD": "sha_head",
        f"refs/namespaces/git-remote-annex/{uuid1}/refs/heads/main": "sha_main",
        f"refs/namespaces/git-remote-annex/{uuid1}/refs/heads/main^{{}}": "sha_peeled",
        f"refs/namespaces/git-remote-annex/{uuid1}/refs/heads/git-annex": "sha_annex",
    }
    normalized = normalize_refs(raw, repo_uuid=uuid1)
    assert "HEAD" not in normalized
    assert "refs/heads/main^{}" not in normalized
    assert normalized["refs/heads/main"] == "sha_main"
    assert normalized["refs/heads/git-annex"] == "sha_annex"


def _create_test_bundle(repo_dir: Path, out_dir: Path, ref_spec: str, repo_uuid: str) -> Path:
    b_raw = out_dir / f"temp_{ref_spec.replace('..', '_').replace('/', '_')}.bundle"
    subprocess.run(
        ["git", "-C", str(repo_dir), "bundle", "create", str(b_raw), ref_spec],
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


def test_replay_refs_smoke(tmp_path: Path):
    repo_dir = tmp_path / "src_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)
    (repo_dir / "file.txt").write_text("hello")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial"], check=True)

    uuid1 = "01234567-89ab-cdef-0123-456789abcdef"
    bundle_path = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid1)

    workdir = tmp_path / "replay_workdir"
    refs = replay_refs([bundle_path], workdir=workdir)
    assert "refs/heads/main" in refs
    assert len(refs["refs/heads/main"]) == 40


def test_h2_replay_workdir_reuse_rejection(tmp_path: Path):
    """H2 驗收測試：先重放完整 manifest，再於同一個 workdir 重放僅含增量 bundle 的清單，必須 raise MismatchError。"""
    repo_dir = tmp_path / "repo_h2"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)

    # Commit 1
    (repo_dir / "c1.txt").write_text("commit 1")
    subprocess.run(["git", "-C", str(repo_dir), "add", "c1.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "c1"], check=True)

    uuid1 = "11111111-2222-3333-4444-555555555555"
    base_bundle = _create_test_bundle(repo_dir, tmp_path, "refs/heads/main", uuid1)

    # Commit 2 (增量)
    (repo_dir / "c2.txt").write_text("commit 2")
    subprocess.run(["git", "-C", str(repo_dir), "add", "c2.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "c2"], check=True)

    inc_bundle = _create_test_bundle(repo_dir, tmp_path, "HEAD~1..refs/heads/main", uuid1)

    workdir = tmp_path / "shared_workdir"

    # 1. 完整重放（base + inc）應成功
    refs = replay_refs([base_bundle, inc_bundle], workdir=workdir)
    assert "refs/heads/main" in refs

    # 2. H2 核心：使用同一個 workdir，但若僅重放缺基底的 inc_bundle，絕不可因物件殘留而判定成功
    with pytest.raises(MismatchError):
        replay_refs([inc_bundle], workdir=workdir)


def test_fake_annex_git_smoke():
    fake = FakeAnnexGit(
        refs={"refs/heads/main": "sha123"},
        annex_keys={"key_annex_1", "key_annex_2"},
        push_effect="apply",
    )
    assert fake.ls_remote() == {"refs/heads/main": "sha123"}
    fake.add(["a.txt", "b.txt"])
    assert fake.added_paths == ["a.txt", "b.txt"]
    sha = fake.commit("msg")
    assert len(sha) == 40
    fake.copy("gdrive", ["key1"])
    assert fake.copied == [("gdrive", ["key1"])]

    # M5: push branches
    fake.push("origin", "main")
    assert fake.pushed == [("origin", ("main",))]
    # push_effect="apply" 更新了 refs/heads/main
    assert fake.refs["refs/heads/main"] == sha

    # M5: annex_keys_in
    assert fake.annex_keys_in("some_uuid") == frozenset({"key_annex_1", "key_annex_2"})

    # M5: push_effect="silent_fail"
    fake2 = FakeAnnexGit(refs={"refs/heads/main": "old_sha"}, push_effect="silent_fail")
    fake2.commit("new commit")
    fake2.push("origin", "main")
    # refs 保持不變
    assert fake2.refs["refs/heads/main"] == "old_sha"

    # M5: inject
    fake3 = FakeAnnexGit()
    fake3.inject("push", WriteError)
    with pytest.raises(WriteError):
        fake3.push("origin", "main")
