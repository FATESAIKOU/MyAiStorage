"""review-g7-e2e A-H1／A-H2／A-H3 的真實 git-annex 驗證（暫存目錄內，不碰專案 repo）。

PM 指定要證明的兩件事：

1. **全新 clone 之後能取回舊快照**（A-H3）——提交流程每一輪都是全新 clone，
   annex 物件的內容不會跟著 clone 下來；`AgoraStore.raw_path_for_snapshot`
   必須能透過 `git annex get --key --from <remote>` 取回前一輪的 raw。
2. **git-annex 的 key 與我們記錄的一致**（A-H2）——`snapshots.jsonl` 記的
   `annex_key` 必須等於 `git annex lookupkey` 的輸出，也必須等於
   `git annex find`（pin 的 annex_keys 來源）列出的 key。

附帶證明 A-H1：raw 確實進了 annex（`git cat-file -p HEAD:<raw>` 是 pointer），
而且 `meta.json` 仍留在 git（小檔才會進 bundle）。

遠端用 `type=directory` 的 special remote（本機目錄），所以這支測試不需要
Drive 憑證；真的 Drive 上的一致性由整合測試（`tests/integration/`）負責。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from aistorage.agora import AgoraStore, AnnexRawStorage, SessionRecord
from aistorage.annex.git import SubprocessAnnexGit, get_git_env
from aistorage.errors import ReadError
from aistorage.schema import generate_ulid

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "annex", "version"], capture_output=True).returncode != 0,
    reason="需要 git-annex")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=get_git_env(), timeout=120, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def _seed_repo_with_remote(tmp_path: Path) -> tuple[Path, str]:
    """真的 git-annex repo ＋ `type=directory` special remote；回傳 (workdir, uuid)。"""
    remote_dir = tmp_path / "annex-remote"
    remote_dir.mkdir()
    workdir = tmp_path / "seed"
    workdir.mkdir()
    _git(workdir, "init", "-b", "main", "-q")
    _git(workdir, "config", "user.name", "it")
    _git(workdir, "config", "user.email", "it@t.invalid")
    (workdir / "README.md").write_text("agora\n")
    _git(workdir, "add", ".")
    _git(workdir, "commit", "-qm", "seed")
    _git(workdir, "annex", "init", "aistorage-it", check=False)
    proc = subprocess.run(
        # --with-url：讓 `git push origin main git-annex` 有 URL 可用
        # （與 1.2/整合測試用 rclone special remote 時的做法相同）
        ["git", "-C", str(workdir), "annex", "initremote", "origin", "type=directory",
         "encryption=none", f"directory={remote_dir}", "--with-url"],
        capture_output=True, text=True, env=get_git_env(), timeout=120, check=False)
    assert proc.returncode == 0, f"initremote 失敗: {proc.stderr.strip()[-300:]}"
    uuid = ""
    for line in _git(workdir, "annex", "info", "origin", "--fast").splitlines():
        if line.startswith("uuid:"):
            uuid = line.split(":", 1)[1].strip()
    assert uuid
    return workdir, uuid


def _annex_url(uuid: str, remote_dir: Path) -> str:
    return (f"annex::{uuid}?type=directory&encryption=none"
            f"&directory={remote_dir}")


def _record(sid: str, raw: bytes, snap_at: str) -> SessionRecord:
    return SessionRecord(
        id=sid, producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z", updated_at=snap_at,
        status="running", snapshot_at=snap_at,
        raw_sha256=hashlib.sha256(raw).hexdigest().lower(), raw_size=len(raw),
        committed_at=snap_at, last_item_key=generate_ulid(),
        title=f"it {sid}")


def test_fresh_clone_retrieves_old_snapshots_and_keys_match_git_annex(tmp_path: Path):
    remote_dir = tmp_path / "annex-remote"
    seed, uuid = _seed_repo_with_remote(tmp_path)
    url = _annex_url(uuid, remote_dir)

    # ---- 第一輪：收兩份快照（內容不同），上推 -------------------------------
    git = SubprocessAnnexGit(seed)
    store = AgoraStore(seed, git=git, temp_dir=tmp_path / "tmp1")
    raws = {
        "opencode:s1": b'{"v": 1, "messages": [{"id": "m1", "text": "first"}]}',
        "opencode:s2": b'{"v": 2, "messages": [{"id": "m1", "text": "second"}]}',
    }
    shas: dict[str, str] = {}
    for i, (sid, raw) in enumerate(raws.items()):
        src = tmp_path / f"in-{i}.raw"
        src.write_bytes(raw)
        store.put_session(_record(sid, raw, f"2026-09-27T08:0{i}:00Z"), src)
        shas[sid] = hashlib.sha256(raw).hexdigest().lower()

    # A-H2：我們記錄的 key == git annex lookupkey == git annex find
    for sid, raw in raws.items():
        snap = store.snapshots(sid)[-1]
        lookup = _git(seed, "annex", "lookupkey", f"sessions/opencode/{sid.split(':')[1]}/raw").strip()
        assert snap.annex_key == lookup, f"{sid}: 記錄的 key 與 git-annex 不一致"
        assert not snap.annex_key.endswith(".json"), "沒有副檔名的檔案不該有副檔名 key"
        assert snap.git_blob is None
        # 內容雜湊要對得上 key 裡宣告的雜湊
        assert snap.annex_key == f"SHA256E-s{len(raw)}--{shas[sid]}"

    # A-H1：raw 是 annex pointer，meta.json 仍是普通 git blob
    # （AgoraStore 每寫一份快照就會 commit checkpoint，所以 HEAD 已經有內容）
    raw_pointer = _git(seed, "cat-file", "-p", "HEAD:sessions/opencode/s1/raw")
    assert "annex" in raw_pointer and b"messages" not in raw_pointer.encode()
    meta_blob = _git(seed, "cat-file", "-p", "HEAD:sessions/opencode/s1/meta.json")
    assert "raw_sha256" in meta_blob

    git.copy("origin")
    git.push("origin", ("main", "git-annex"))
    # pin 的 annex_keys 來源（注意：--format 要帶換行，否則多個 key 會黏在一起）
    find_keys = {
        line for line in _git(seed, "annex", "find", f"--in={uuid}",
                              "--format=${key}\n").splitlines() if line.strip()
    }
    assert find_keys == {s.annex_key for s in store.snapshots("opencode:s1")} | {
        s.annex_key for s in store.snapshots("opencode:s2")}

    # ---- 第二輪：全新 clone（提交流程的實際情況）---------------------------
    fresh_dir = tmp_path / "fresh"
    fresh_git = SubprocessAnnexGit.clone_for_commit(url, fresh_dir, max_git_bundles=20)
    fresh = AgoraStore(fresh_dir, git=fresh_git, temp_dir=tmp_path / "tmp2")

    # clone 之下本機沒有物件（這正是 A-H3 的情境）
    for sid, raw in raws.items():
        snap = fresh.snapshots(sid)[-1]
        assert snap.annex_key
        assert not list((fresh_dir / ".git" / "annex" / "objects").rglob(snap.annex_key)), \
            "全新 clone 不該已經有本機物件（否則這個測試沒測到 A-H3）"
        retrieved = fresh.raw_path_for_snapshot(sid, shas[sid])
        assert retrieved.is_file()
        assert retrieved.read_bytes() == raw, f"{sid}: 全新 clone 取不回舊快照"

    # 取出之後再取一次（走本機 cache，不會再打遠端）
    assert fresh.raw_path_for_snapshot("opencode:s1", shas["opencode:s1"]).read_bytes() == raws["opencode:s1"]

    # 取不到的 key 必須 raise（不再靜默 KeyError / except: pass）
    missing = "SHA256E-s3--" + "0" * 64
    with pytest.raises((ReadError, KeyError)):
        fresh.raw_path_for_snapshot("opencode:s1", missing)


def _bare_annex_repo(path: Path) -> Path:
    workdir = path
    workdir.mkdir(parents=True, exist_ok=True)
    _git(workdir, "init", "-b", "main", "-q")
    _git(workdir, "config", "user.name", "it")
    _git(workdir, "config", "user.email", "it@t.invalid")
    (workdir / "README.md").write_text("x\n")
    _git(workdir, "add", ".")
    _git(workdir, "commit", "-qm", "seed")
    _git(workdir, "annex", "init", "aistorage-it", check=False)
    return workdir


def test_store_raises_when_largefiles_rule_does_not_cover_raw(tmp_path: Path):
    """A-H1 的反面：規則涵蓋不到就必須 raise，不准默默留在 git blob。"""
    from aistorage.errors import WriteError

    src = tmp_path / "in.raw"
    src.write_bytes(b"RAW")

    # 規則只涵蓋 objects/**，raw 不在範圍內 → 必須 raise
    bad_repo = _bare_annex_repo(tmp_path / "repo-bad")
    bad = AnnexRawStorage(bad_repo, git=SubprocessAnnexGit(bad_repo),
                          largefiles="include=objects/*/*")
    with pytest.raises(WriteError, match="沒有進 git-annex"):
        bad.store(bad_repo / "sessions/opencode/s1/raw", src)

    # 涵蓋 raw 的規則就成功（各自獨立的 repo：largefiles 對已在 index 的檔案無效，
    # 所以不能用同一個工作樹換規則後重試）
    good_repo = _bare_annex_repo(tmp_path / "repo-good")
    good = AnnexRawStorage(good_repo, git=SubprocessAnnexGit(good_repo))
    ref = good.store(good_repo / "sessions/opencode/s1/raw", src)
    assert ref.kind == "annex" and ref.ref == f"SHA256E-s3--{hashlib.sha256(b'RAW').hexdigest()}"
    dest = tmp_path / "out.raw"
    good.retrieve(ref.ref, dest)
    assert dest.read_bytes() == b"RAW"
