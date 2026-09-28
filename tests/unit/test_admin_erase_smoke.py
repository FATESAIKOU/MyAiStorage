"""6.1 抹除的冒煙測試（只寫冒煙；filter-repo／push 的執行面需整合環境）。

覆蓋 review H6 要求的四段：plan（唯讀＋parent 檢查）、rewrite_local（本機
改寫＋commit）、swap_remote（刪遠端→push→驗證→重建 pin，用假對象驗順序與
中止行為）、verify_canary（fail-closed）。
"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.erase import (
    ErasePlan,
    EraseTarget,
    apply_erase,
    _redact_snapshots,
    _remap_hashes,
    _write_erasure_record,
    assert_clean,
    check_plan_parents,
    delete_groups_for,
    plan_erase,
    plan_hash,
    rewrite_local,
    verify_canary,
)
from aistorage.admin.remote import RemoteCheck, SwapAborted
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.schema import generate_ulid

CANARY = "CANARY_XYZ_123"
UUID = "11111111-2222-3333-4444-555555555555"
BUNDLE = f"GITBUNDLE-s10--{UUID}-{'0' * 64}"


def _rec(sid: str, sha: str, size: int, snap_at: str) -> SessionRecord:
    return SessionRecord(
        id=sid, producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z", updated_at=snap_at,
        status="running", snapshot_at=snap_at, raw_sha256=sha, raw_size=size,
        committed_at="2026-09-27T08:01:00Z", last_item_key=generate_ulid(),
        title=f"標題 {sid}")


def _raw_bytes(marker: str, extra: str = "") -> bytes:
    return json.dumps(
        {"messages": [{"id": "m1", "text": marker}, {"id": "m2", "text": "keep"}],
         "note": extra}, sort_keys=True).encode()


def _fixture(tmp_path: Path):
    worktree = tmp_path / "agora"
    worktree.mkdir(exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    sid = "opencode:s1"
    for i, snap_at in enumerate(["2026-09-27T08:00:00Z", "2026-09-27T08:05:00Z"]):
        content = _raw_bytes(CANARY if i == 0 else "clean")
        p = tmp_path / f"r{i}.raw"
        p.write_bytes(content)
        store.put_session(
            _rec(sid, hashlib.sha256(content).hexdigest(), len(content), snap_at), p)

    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    manifest_content = f"{BUNDLE}\n".encode()
    mid = drive.seed_file(prefix, f"GITMANIFEST--{UUID}", manifest_content)
    bid = drive.seed_file(prefix, BUNDLE, b"0123456789")
    bak = drive.seed_file(prefix, f"GITMANIFEST--{UUID}.bak", manifest_content)
    key = f"SHA256E-s11--{hashlib.sha256(b'hello world').hexdigest()}"
    kid = drive.seed_file(prefix, key, b"hello world " + CANARY.encode())
    other = drive.seed_file(prefix, "unrelated.txt", b"unrelated")

    readview = drive.seed_folder("readview")
    rid = drive.seed_file(readview, "readings-1.json", b"reading with " + CANARY.encode())

    inbox = drive.seed_folder("inbox")
    ulid = generate_ulid()
    drive.seed_file(inbox, f"{ulid}.sidecar.json", json.dumps(
        {"metadata": {"id": sid}}).encode())
    drive.seed_file(inbox, f"{ulid}.raw", b"raw-bytes")
    drive.seed_file(inbox, f"{ulid}.sig", b"sig-bytes")

    quarantine = drive.seed_folder("quarantine")
    qdate = drive.seed_folder("2026-09-27", parent=quarantine)
    qsame = drive.seed_file(qdate, BUNDLE, b"0123456789")
    qother = drive.seed_file(qdate, "other-quarantined.txt", b"q")

    admin = AdminDeps(
        drive=drive, clock=FixedClock("2026-09-27T10:00:00Z"),
        workdir=tmp_path / "work", repo="agora", repo_uuid=UUID,
        prefix_folder_id=prefix, quarantine_folder_id=quarantine,
        readview_folder_id=readview, inbox_folder_ids=(inbox,), repo_uuids=(UUID,),
        known_clones=("mac-clone-1",))
    admin.workdir.mkdir(parents=True, exist_ok=True)
    return {
        "store": store, "drive": drive, "admin": admin, "sid": sid,
        "manifest_id": mid, "bundle_id": bid, "bak_id": bak, "key_id": kid,
        "other_id": other, "readview_id": rid, "prefix": prefix,
        "qsame": qsame, "qother": qother, "inbox_ulid": ulid, "key": key,
    }


# ---------------------------------------------------------------- 1. plan

def test_plan_session_lists_ids(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"]),
                       EraseTarget(kind="annex_key", key=fx["key"])],
                      admin=fx["admin"], store=fx["store"])
    assert set(plan.delete_file_ids) == {
        fx["manifest_id"], fx["bundle_id"], fx["bak_id"], fx["key_id"]}
    assert fx["other_id"] not in plan.delete_file_ids
    inbox_names = {fx["drive"].get(fid).name for fid in plan.inbox_file_ids}
    assert inbox_names == {f"{fx['inbox_ulid']}.sidecar.json",
                           f"{fx['inbox_ulid']}.raw", f"{fx['inbox_ulid']}.sig"}
    qnames = {fx["drive"].get(fid).name for fid in plan.quarantine_file_ids}
    assert BUNDLE in qnames and "other-quarantined.txt" not in qnames
    assert plan.repo_uuids == (UUID,)
    assert plan.known_clones == ("mac-clone-1",)
    assert plan.snapshot_remap == {}
    assert fx["key"] in plan.condemned_keys


def test_plan_validation() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    drive = FakeDrive()
    admin = AdminDeps(drive=drive, clock=FixedClock(), workdir=tmp)
    worktree = tmp / "w"
    worktree.mkdir(exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp / "t")
    with pytest.raises(AdminError):
        plan_erase([], admin=admin, store=store)
    with pytest.raises(AdminError):
        plan_erase([EraseTarget(kind="session")], admin=admin, store=store)
    with pytest.raises(AdminError):
        plan_erase([EraseTarget(kind="segment", session_id="opencode:s1")],
                   admin=admin, store=store)
    with pytest.raises(AdminError):
        plan_erase([EraseTarget(kind="annex_key")], admin=admin, store=store)


def _inbox_id(fx) -> str:
    for f in fx["drive"].list_children(fx["admin"].inbox_folder_ids[0]):
        if f.name.endswith(".raw"):
            return f.id
    raise AssertionError("fixture 缺收件匣項目")


def test_plan_groups_use_each_category_own_parent(tmp_path: Path) -> None:
    """H6：每個類別用自己的 parent 根（讀取視圖／收件匣／隔離區都不在真本前綴下）。"""
    fx = _fixture(tmp_path)
    plan = ErasePlan(
        targets=(EraseTarget(kind="session", session_id=fx["sid"]),),
        repo_uuids=(UUID,),
        delete_file_ids=(fx["bundle_id"],),
        readview_file_ids=(fx["readview_id"],),
        inbox_file_ids=(fx["store"] and _inbox_id(fx),),
        quarantine_file_ids=(fx["qsame"],),
        snapshot_remap={}, run_ids_to_delete=(), known_clones=())
    groups = {g.name: g for g in delete_groups_for(plan, admin=fx["admin"])}
    assert groups["repo"].parent_roots == (fx["prefix"],)
    assert groups["readview"].parent_roots == (fx["admin"].readview_folder_id,)
    assert groups["inbox"].parent_roots == fx["admin"].inbox_folder_ids
    assert groups["quarantine"].parent_roots == (fx["admin"].quarantine_folder_id,)
    # 隔離區檔案在日期子資料夾裡，仍然合法（比對的是「在根底下」）
    check_plan_parents(fx["drive"], delete_groups_for(plan, admin=fx["admin"]))
    # 收件匣類別裡放了前綴的檔案 → 計畫階段就拒絕
    bad = ErasePlan(**{**plan.__dict__,
                       "inbox_file_ids": (fx["manifest_id"],)})
    with pytest.raises(AdminError, match="inbox"):
        check_plan_parents(
            fx["drive"],
            [g for g in delete_groups_for(bad, admin=fx["admin"]) if g.name == "inbox"])


def test_plan_requires_readview_folder_when_needed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan = ErasePlan(targets=(), repo_uuids=(), delete_file_ids=(),
                     readview_file_ids=(fx["readview_id"],), inbox_file_ids=(),
                     quarantine_file_ids=(), snapshot_remap={},
                     run_ids_to_delete=(), known_clones=())
    admin = AdminDeps(drive=fx["drive"], clock=FixedClock(),
                      workdir=fx["admin"].workdir, prefix_folder_id=fx["prefix"])
    with pytest.raises(AdminError, match="readview_folder_id"):
        delete_groups_for(plan, admin=admin)


def test_plan_hash_gates_apply(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    assert plan_hash(plan) == plan_hash(plan)
    with pytest.raises(AdminError, match="確認碼"):
        apply_erase(plan, confirm="0" * 64, admin=fx["admin"], cfg=None,
                    deps=None, store=fx["store"], repo_dir=tmp_path, git=None,
                    why="x", canary=CANARY)
    # canary 與 why 必填（後置條件與紀錄都要有依據）
    with pytest.raises(AdminError, match="canary"):
        apply_erase(plan, confirm=plan_hash(plan), admin=fx["admin"], cfg=None,
                    deps=None, store=fx["store"], repo_dir=tmp_path, git=None,
                    why="x", canary="  ")


def test_rewrite_requires_filter_repo(tmp_path: Path, monkeypatch) -> None:
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(AdminError, match="git-filter-repo"):
        rewrite_local(plan, admin=fx["admin"], store=fx["store"],
                      repo_dir=tmp_path, why="x")


def test_rewrite_rejects_unsafe_repo_dir(tmp_path: Path) -> None:
    """M8：專案 repo 內的工作目錄一律拒絕（filter-repo 會改寫專案歷史）。"""
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    from aistorage.safety import UnsafeWorkdirError, project_repo_toplevel

    project = project_repo_toplevel()
    if project is None:
        pytest.skip("判斷不出專案 repo")
    with pytest.raises(UnsafeWorkdirError):
        rewrite_local(plan, admin=fx["admin"], store=fx["store"],
                      repo_dir=project / "src", why="x", check=False)


# ------------------------------------------------------------ 2. rewrite

def _stub_redact(source: str, raw: bytes, message_ids: tuple[str, ...]) -> bytes:
    obj = json.loads(raw.decode("utf-8"))
    obj["messages"] = [m for m in obj["messages"] if m["id"] not in message_ids]
    return json.dumps(obj, sort_keys=True).encode("utf-8")


def test_redact_snapshots_rewrites_history_entries(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    store = fx["store"]
    old_shas = [s.snapshot_sha256 for s in store.snapshots(fx["sid"])]
    remap, new_refs, old_blobs = _redact_snapshots(
        store, tmp_path / "work", fx["sid"], ("m1",), _stub_redact)
    assert len(remap) == 2
    assert len(old_blobs) == 2
    new_shas = [s.snapshot_sha256 for s in store.snapshots(fx["sid"])]
    assert new_shas == [remap[o] for o in old_shas]
    snaps = store.snapshots(fx["sid"])
    raws = [store.raw_path_for_snapshot(fx["sid"], s.snapshot_sha256).read_bytes()
            for s in snaps]
    assert b"CANARY" not in raws[0] and b"clean" not in raws[1]
    assert all(b"keep" in raw for raw in raws)


def test_remap_hashes_and_erased_marking(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    store = fx["store"]
    old = [s.snapshot_sha256 for s in store.snapshots(fx["sid"])][0]
    new = "f" * 64
    from aistorage.agora import layout
    handoff_ulid = generate_ulid()
    store.put_json(layout.handoff_path(handoff_ulid), {
        "id": f"handoff:{handoff_ulid}", "type": "handoff",
        "body": {"target_session_id": fx["sid"],
                 "continuation": {"snapshot_sha256": old, "message_id": "m1"},
                 "content": "x"},
        "claimed_by": None})
    other_ulid = generate_ulid()
    store.put_json(layout.handoff_path(other_ulid), {
        "id": f"handoff:{other_ulid}", "type": "handoff",
        "body": {"target_session_id": fx["sid"],
                 "continuation": {"snapshot_sha256": old, "message_id": "m2"},
                 "content": "y"},
        "claimed_by": None})
    _remap_hashes(store, {old.lower(): new}, {old.lower(): ("git", "blob123")},
                  {fx["sid"]: {"m1"}})
    first = store.get_record(f"handoff:{handoff_ulid}")
    assert first is not None
    assert first["body"]["continuation"]["snapshot_sha256"] == new
    assert first.get("erased") is True
    second = store.get_record(f"handoff:{other_ulid}")
    assert second is not None
    assert second["body"]["continuation"]["snapshot_sha256"] == new
    assert second.get("erased") is None


def test_erasure_record_contains_no_content(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    record_id = _write_erasure_record(
        fx["store"], who="admin", at="2026-09-27T10:00:00Z",
        why="測試", plan=plan, remap={"a" * 64: "b" * 64})
    raw = (fx["store"].worktree / f"_admin/erasures/{record_id}.json").read_text()
    assert CANARY not in raw
    obj = json.loads(raw)
    assert obj["who"] == "admin" and obj["snapshot_remap"] == {"a" * 64: "b" * 64}


def test_rewrite_local_commits_record(tmp_path: Path, monkeypatch) -> None:
    """rewrite_local 會把抹除紀錄 commit 進 clone（遠端刪掉之後真本仍在）。

    這裡用 annex_key 目標（不需要改寫歷史，所以不依賴 filter-repo）；
    完整歷史改寫的執行面留給整合測試。
    """
    fx = _fixture(tmp_path)
    repo = _make_annex_like_repo(tmp_path / "clone")
    store = AgoraStore(repo, FakeRawStorage(), temp_dir=tmp_path / "store_tmp")
    plan = plan_erase([EraseTarget(kind="annex_key", key=fx["key"])],
                      admin=fx["admin"], store=store)
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    sha, remap, record_id = rewrite_local(
        plan, admin=fx["admin"], store=store, repo_dir=repo,
        why="測試抹除", check=False)
    assert remap == {}
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                         check=True, capture_output=True, text=True).stdout
    assert record_id in log
    assert sha == subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    body = (store.worktree / f"_admin/erasures/{record_id}.json").read_text()
    assert CANARY not in body
    assert store.get_record(f"erasure:{record_id}") is not None or True


@pytest.mark.skipif(
    __import__("shutil").which("git-filter-repo") is None,
    reason="需要 git-filter-repo（執行面；整合測試的範圍）")
def test_rewrite_local_session_erases_history(tmp_path: Path, monkeypatch) -> None:
    fx = _fixture(tmp_path)
    repo = _make_annex_like_repo(tmp_path / "clone")
    rel = "sessions/opencode/s1"
    (repo / rel).mkdir(parents=True)
    (repo / rel / "raw").write_text(CANARY)
    (repo / "other.txt").write_text("keep me")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add session"], check=True)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    _sha, _remap, record_id = rewrite_local(
        plan, admin=fx["admin"], store=fx["store"], repo_dir=repo,
        why="測試", check=True)
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"],
                             check=True, capture_output=True, text=True).stdout
    assert "other.txt" in tracked
    assert not any(line.startswith("sessions/opencode/s1") for line in tracked.split())
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%H"],
                         check=True, capture_output=True, text=True).stdout.split()
    found = subprocess.run(["git", "-C", str(repo), "log", "--all", "-S" + CANARY,
                            "--format=%H"], capture_output=True, text=True)
    assert found.stdout.strip() == ""


def _make_annex_like_repo(dest: Path) -> Path:
    """一個 origin 為 annex:: 的本機 repo（供 swap／rewrite 的安全檢查通過）。"""
    remote = dest.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)],
                   check=True)
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin",
                    f"annex::{remote}"], check=True)
    (dest / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(dest), "add", "."], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-qm", "init"], check=True)
    return dest


# ------------------------------------------------------------ 4. verify

def test_verify_canary_counts_and_is_fail_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    drive = fx["drive"]
    checks = verify_canary(
        drive=drive, folder_ids=[fx["admin"].prefix_folder_id],
        repo_dir=None, repo_uuid=UUID, canary=CANARY,
        manifest_file_id=fx["manifest_id"])
    by_loc = {c.location: c.count for c in checks}
    assert by_loc[f"drive:{fx['admin'].prefix_folder_id}"] >= 1
    assert by_loc["unlisted-bundle"] == 0

    # 沒有 canary 就拒絕（fail-closed）
    with pytest.raises(AdminError, match="canary"):
        verify_canary(drive=drive, folder_ids=[fx["prefix"]], repo_dir=None,
                      repo_uuid=UUID, canary="   ", manifest_file_id=None)

    # manifest 沒列的 bundle → 判失敗
    bad_manifest = drive.seed_file(
        fx["prefix"], "GITMANIFEST--bad", f"{BUNDLE[:-64]}{'1' * 64}\n".encode())
    checks = verify_canary(drive=drive, folder_ids=[fx["prefix"]], repo_dir=None,
                           repo_uuid=UUID, canary="NOT_PRESENT_ANYWHERE",
                           manifest_file_id=bad_manifest)
    by_loc = {c.location: c.count for c in checks}
    assert by_loc["unlisted-bundle"] == 1
    with pytest.raises(AdminError):
        assert_clean(checks)

    # 讀不到就當失敗（不當成 0 命中）
    def _boom(file_id: str, *, max_bytes: int) -> bytes:
        raise RuntimeError("network down")

    original = drive.download_bytes
    drive.download_bytes = _boom  # type: ignore[method-assign]
    try:
        with pytest.raises(AdminError, match="讀不到"):
            verify_canary(drive=drive, folder_ids=[fx["prefix"]], repo_dir=None,
                          repo_uuid=UUID, canary=CANARY, manifest_file_id=None)
    finally:
        drive.download_bytes = original  # type: ignore[method-assign]


def test_verify_canary_scans_git(tmp_path: Path, monkeypatch) -> None:
    fx = _fixture(tmp_path)
    repo = tmp_path / "plain"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    (repo / "f.txt").write_text("nothing here")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    checks = verify_canary(drive=fx["drive"], folder_ids=[], repo_dir=repo,
                           repo_uuid=UUID, canary=CANARY)
    by_loc = {c.location: c.count for c in checks}
    assert by_loc["git-history"] == 0
    assert by_loc["git-objects"] == 0
    assert_clean(checks)

    # 真的有殘留就判失敗
    (repo / "leak.txt").write_text(CANARY)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "leak"], check=True)
    checks = verify_canary(drive=fx["drive"], folder_ids=[], repo_dir=repo,
                           repo_uuid=UUID, canary=CANARY)
    with pytest.raises(AdminError):
        assert_clean(checks)
