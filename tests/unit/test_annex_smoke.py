"""Smoke tests for annex module (manifest, replay, fake)."""

from pathlib import Path
import subprocess
import pytest

from aistorage.annex import (
    BundleName,
    FakeAnnexGit,
    Manifest,
    parse_bundle_name,
    parse_manifest,
    replay_refs,
)
from aistorage.errors import MismatchError


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
    m = parse_manifest(manifest_bytes)
    assert m.active == (b1, b2)
    assert m.removed == frozenset({b_rem})

    # 空 active
    with pytest.raises(MismatchError):
        parse_manifest(f"-{b_rem}\n".encode("utf-8"))

    # 非法行
    with pytest.raises(MismatchError):
        parse_manifest(b"corrupted_line\n")


def test_replay_refs_smoke(tmp_path: Path):
    # 建立一個測試用的 git bundle
    repo_dir = tmp_path / "src_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.com"], check=True)
    (repo_dir / "file.txt").write_text("hello")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial"], check=True)

    bundle_path = tmp_path / "test.bundle"
    subprocess.run(["git", "-C", str(repo_dir), "bundle", "create", str(bundle_path), "HEAD"], check=True)

    workdir = tmp_path / "replay_workdir"
    refs = replay_refs([bundle_path], workdir=workdir)
    assert "HEAD" in refs
    assert len(refs["HEAD"]) == 40


def test_fake_annex_git_smoke():
    fake = FakeAnnexGit(refs={"refs/heads/main": "sha123"})
    assert fake.ls_remote() == {"refs/heads/main": "sha123"}
    fake.add(["a.txt", "b.txt"])
    assert fake.added_paths == ["a.txt", "b.txt"]
    sha = fake.commit("msg")
    assert len(sha) == 40
    fake.copy("gdrive", ["key1"])
    assert fake.copied == [("gdrive", ["key1"])]
    fake.push("origin", "main")
    assert fake.pushed == [("origin", "main")]
