"""Smoke and security tests for annex module (manifest, replay, fake, git)."""

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
    uuid1 = "01234567-89ab-cdef-0123-456789abcdef"
    b1 = f"GITBUNDLE-s100--{uuid1}-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    b2 = f"GITBUNDLE-s200--{uuid1}-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    b_rem = f"GITBUNDLE-s50--{uuid1}-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    manifest_bytes = f"{b1}\n{b2}\n-{b_rem}\n".encode("utf-8")
    m = parse_manifest(manifest_bytes, repo_uuid=uuid1)
    assert m.active == (b1, b2)
    assert m.removed == frozenset({b_rem})

    # 空 active
    with pytest.raises(MismatchError):
        parse_manifest(f"-{b_rem}\n".encode("utf-8"), repo_uuid="uuid1")

    # 非法行
    with pytest.raises(MismatchError):
        parse_manifest(b"corrupted_line\n", repo_uuid="uuid1")

    # M4: 重複 active
    with pytest.raises(MismatchError):
        parse_manifest(f"{b1}\n{b1}\n".encode("utf-8"), repo_uuid="uuid1")

    # M4: active 與 removed 交集
    with pytest.raises(MismatchError):
        parse_manifest(f"{b1}\n-{b1}\n".encode("utf-8"), repo_uuid="uuid1")


def test_normalize_refs_and_bundle_heads_equality():
    """N1: 驗證同一組 refs 分別造出 ls-remote 形狀與 bundle heads 形狀，正規化後完全相等。"""
    uuid1 = "01234567-89ab-cdef-0123-456789abcdef"
    clean_refs = {
        "refs/heads/main": "867947d6002ab62859d28bace760ae8800f11057",
        "refs/heads/git-annex": "c2c126128c67b6fe8bbb96f18c38e4072505ad79",
    }

    # 造出真實 bundle heads 形狀（帶 namespace、含 HEAD 與 peeled ref）
    raw_bundle_heads = {
        "HEAD": "867947d6002ab62859d28bace760ae8800f11057",
        f"refs/namespaces/git-remote-annex/{uuid1}/refs/heads/main": "867947d6002ab62859d28bace760ae8800f11057",
        f"refs/namespaces/git-remote-annex/{uuid1}/refs/heads/main^{{}}": "867947d6002ab62859d28bace760ae8800f11057",
        f"refs/namespaces/git-remote-annex/{uuid1}/refs/heads/git-annex": "c2c126128c67b6fe8bbb96f18c38e4072505ad79",
    }

    # 造出真實 ls-remote 形狀（不帶 namespace、含 HEAD）
    raw_ls_remote = {
        "HEAD": "867947d6002ab62859d28bace760ae8800f11057",
        "refs/heads/main": "867947d6002ab62859d28bace760ae8800f11057",
        "refs/heads/git-annex": "c2c126128c67b6fe8bbb96f18c38e4072505ad79",
    }

    norm_bundle = normalize_bundle_heads(raw_bundle_heads, repo_uuid=uuid1)
    norm_ls = normalize_ls_remote(raw_ls_remote)

    # 核心斷言：各自正規化後完全相等
    assert norm_bundle == norm_ls == clean_refs

    # 負向：bundle heads 缺少 namespace 前綴應 raise
    with pytest.raises(MismatchError):
        normalize_bundle_heads(raw_ls_remote, repo_uuid=uuid1)

    # 負向：bundle heads uuid 不符應 raise
    with pytest.raises(MismatchError):
        normalize_bundle_heads(raw_bundle_heads, repo_uuid="wrong-uuid-00000000")

    # 負向：ls-remote 包含 namespace 前綴應 raise
    with pytest.raises(MismatchError):
        normalize_ls_remote(raw_bundle_heads)


def _create_test_bundle(repo_dir: Path, out_dir: Path, ref_spec: str, repo_uuid: str) -> Path:
    b_raw = out_dir / f"temp_{ref_spec.replace('..', '_').replace('/', '_')}.bundle"
    # 將 ref_spec 對應之 commit 映射至 git-remote-annex namespace
    ns_ref = f"refs/namespaces/git-remote-annex/{repo_uuid}/refs/heads/main"
    if ".." in ref_spec:
        base, head = ref_spec.split("..", 1)
        sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", head], text=True).strip()
        subprocess.run(["git", "-C", str(repo_dir), "update-ref", ns_ref, sha], check=True)
        bundle_spec = f"{base}..{ns_ref}"
    else:
        sha = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", ref_spec], text=True).strip()
        subprocess.run(["git", "-C", str(repo_dir), "update-ref", ns_ref, sha], check=True)
        bundle_spec = ns_ref

    subprocess.run(
        ["git", "-C", str(repo_dir), "bundle", "create", str(b_raw), bundle_spec],
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
    refs = replay_refs([bundle_path], workdir=workdir, repo_uuid=uuid1)
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
    refs = replay_refs([base_bundle, inc_bundle], workdir=workdir, repo_uuid=uuid1)
    assert "refs/heads/main" in refs

    # 2. H2 核心：使用同一個 workdir，但若僅重放缺基底的 inc_bundle，絕不可因物件殘留而判定成功
    with pytest.raises(MismatchError):
        replay_refs([inc_bundle], workdir=workdir, repo_uuid=uuid1)


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

    # N1: FakeAnnexGit 亦透過 normalize_ls_remote 拒絕 namespace ref
    fake4 = FakeAnnexGit(refs={"refs/namespaces/git-remote-annex/uuid/refs/heads/main": "sha"})
    with pytest.raises(MismatchError):
        fake4.ls_remote()


def test_subprocess_annex_git_n5_n6(tmp_path: Path):
    from aistorage.annex import SubprocessAnnexGit
    from aistorage.errors import ReadError

    # N5: 舊的 clone 方法已徹底移除
    assert not hasattr(SubprocessAnnexGit, "clone")

    # 初始化測試 repo
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    git = SubprocessAnnexGit(tmp_path)

    # N6: 命令失敗時將 stderr 寫至 debug/git-<ts>.log，例外訊息僅包含檔名
    with pytest.raises(ReadError) as exc_info:
        git._run(["git", "checkout", "non_existent_branch"], is_write=False)

    msg = str(exc_info.value)
    assert "Git 命令失敗" in msg
    assert "log=git-" in msg

    debug_dir = tmp_path / "debug"
    assert debug_dir.is_dir()
    log_files = list(debug_dir.glob("git-*.log"))
    assert len(log_files) >= 1
    log_content = log_files[0].read_text(encoding="utf-8")
    assert "non_existent_branch" in log_content
