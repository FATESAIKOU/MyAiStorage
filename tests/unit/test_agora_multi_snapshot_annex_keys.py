"""同一輪內同一個 Session 連續兩版：每一版都要變成自己的 annex 物件。

這是 review-cdb4a34 H1 順手抓到的 Agora 資料遺失：提交流程在同一輪收進同一個
Session 的兩份快照時，第 2 份快照的 `snapshots.jsonl` 記到的 `annex_key` 是
**前一版**的 key，那一版的真正內容從來沒有變成 annex 物件（Drive 上也沒有）。
後果是「讀取介面讀回那一版」拿到別的版本的內容——key 的名稱是內容雜湊，內容
卻不是，sweep、key 覆蓋率、隔離區全都抓不到。

重現關鍵是**用提交流程的路徑**：每一輪都是 `clone_for_commit` 出來的全新 clone，
再連續兩次 `apply_session`（不是自己呼叫 `put_session`）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from aistorage.agora import AgoraStore, AnnexRawStorage
from aistorage.agora.apply import apply_session
from aistorage.annex.git import SubprocessAnnexGit, get_git_env
from aistorage.clock import FixedClock
from aistorage.converters.base import Converter, SessionFacts
from aistorage.intake import Decision, DecisionKind, InboxItem
from aistorage.schema import generate_ulid

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "annex", "version"], capture_output=True).returncode != 0,
    reason="需要 git-annex")

PRODUCER = "profile:mac-opencode"
SID = "opencode:ses_multi"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=get_git_env(), timeout=120, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def _seed_repo_with_remote(tmp_path: Path) -> tuple[Path, str]:
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
    return f"annex::{uuid}?type=directory&encryption=none&directory={remote_dir}"


class _Conv(Converter):
    """只給出足夠的事實；不寫任何檔案。"""

    def facts(self, raw_path: Path) -> SessionFacts:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        return SessionFacts(
            status="stopped" if data.get("archived") else "running",
            title=f"t-{len(data.get('messages', []))}",
            in_progress=False,
        )

    def convert(self, raw_path: Path, *, session_id: str, parent_id=None,
                snapshot_sha256=None) -> dict:
        return {"session_id": session_id, "messages": 0}

    def check_continuation(self, *a, **k) -> tuple:  # pragma: no cover - 沒用到
        return (True, None)

    def child_session_ids(self, raw_path: Path) -> tuple:
        return ()


def _raw(marker: str) -> bytes:
    return json.dumps(
        {"messages": [{"message_id": f"m_{marker}", "text": f"revision {marker}"}]},
        sort_keys=True).encode("utf-8")


def _dec(tmp_path: Path, marker: str, snapshot_at: str) -> Decision:
    data = _raw(marker)
    raw_p = tmp_path / f"in-{marker}.raw"
    raw_p.write_bytes(data)
    record = {
        "id": SID, "type": "session", "producer": PRODUCER,
        "created_at": "2026-09-27T08:00:00Z", "updated_at": snapshot_at,
        "case_id": None, "provenance": None,
    }
    sidecar = {
        "session": {"source": "opencode", "source_session_id": SID.split(":", 1)[1],
                    "snapshot_at": snapshot_at, "status": "running",
                    "in_progress": False},
        "raw": {"sha256": hashlib.sha256(data).hexdigest().lower(), "size": len(data)},
        "body": {},
    }
    return Decision(
        kind=DecisionKind.ACCEPT,
        item=InboxItem(item_key=generate_ulid(), inbox_folder_id="inbox-test"),
        code="ok", producer=PRODUCER, record_metadata=record, sidecar=sidecar,
        raw_path=raw_p,
    )


def test_two_revisions_in_one_round_each_get_their_own_annex_key(tmp_path: Path):
    """同一輪兩版 → 兩個快照、兩個 key、兩個物件，內容各自正確。"""
    seed, uuid = _seed_repo_with_remote(tmp_path)
    seed_git = SubprocessAnnexGit(seed)
    seed_git.copy("origin")
    seed_git.push("origin", ("main", "git-annex"))

    # 提交流程的實際情況：每一輪都是全新 clone
    workdir = tmp_path / "fresh"
    git = SubprocessAnnexGit.clone_for_commit(
        _annex_url(uuid, tmp_path / "annex-remote"), workdir, max_git_bundles=1)
    store = AgoraStore(workdir, raw_storage=AnnexRawStorage(workdir, git=git),
                       git=git, temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T09:00:00Z")
    conv = _Conv()

    # 第 1 輪：同一個 Session 兩版（大小相同、只差一個字元，最容易踩到快取）
    r1 = apply_session(store, _dec(tmp_path, "a", "2026-09-27T08:00:00Z"), conv, clock)
    assert r1.ok, r1.code
    r2 = apply_session(store, _dec(tmp_path, "b", "2026-09-27T09:00:00Z"), conv, clock)
    assert r2.ok, r2.code

    snaps = store.snapshots(SID)
    assert len(snaps) == 2, f"兩版都該有快照，實際 {len(snaps)}"
    objects = {p.name for p in (workdir / ".git" / "annex" / "objects").rglob("*")
               if p.is_file()}
    rel = f"sessions/opencode/{SID.split(':')[1]}/raw"
    for snap, marker in zip(snaps, ("a", "b")):
        data = _raw(marker)
        expect = f"SHA256E-s{len(data)}--{hashlib.sha256(data).hexdigest()}"
        assert snap.annex_key == expect, (
            f"{marker} 版的快照記到別的 key：{snap.annex_key} != {expect}")
        assert snap.annex_key in objects, (
            f"{marker} 版的內容沒有變成 annex 物件（{snap.annex_key} 不在物件庫）")
        # key 指向的物件，內容必須真的是這一版
        got = store.raw_path_for_snapshot(SID, snap.snapshot_sha256).read_bytes()
        assert hashlib.sha256(got).hexdigest() == snap.snapshot_sha256, (
            f"{marker} 版讀回來的內容對不上")

    # 樹狀裡的 raw 必須指向**最新**那一版的物件
    head_blob = _git(workdir, "cat-file", "-p", f"HEAD:{rel}").strip()
    assert snaps[-1].annex_key in head_blob, f"樹狀的 raw 還指著舊物件：{head_blob}"
    assert seed.exists()
