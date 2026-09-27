"""6.1 抹除的冒煙測試（只寫冒煙；執行面的 filter-repo 需整合環境）。"""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.erase import (
    EraseTarget,
    _redact_snapshots,
    _remap_hashes,
    apply_erase,
    assert_clean,
    plan_erase,
    plan_hash,
    verify_remote,
)
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.schema import generate_ulid

CANARY = "CANARY_XYZ_123"
UUID = "11111111-2222-3333-4444-555555555555"


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
    raws = []
    for i, snap_at in enumerate(["2026-09-27T08:00:00Z", "2026-09-27T08:05:00Z"]):
        content = _raw_bytes(CANARY if i == 0 else "clean")
        p = tmp_path / f"r{i}.raw"
        p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        store.put_session(_rec(sid, sha, len(content), snap_at), p)
        raws.append(sha)

    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    manifest_content = f"GITBUNDLE-s10--{UUID}-{'0' * 64}\n".encode()
    mid = drive.seed_file(prefix, f"GITMANIFEST--{UUID}", manifest_content)
    bid = drive.seed_file(prefix, f"GITBUNDLE-s10--{UUID}-{'0' * 64}", b"0123456789")
    bak = drive.seed_file(prefix, f"GITMANIFEST--{UUID}.bak", manifest_content)
    key = f"SHA256E-s11--{hashlib.sha256(b'hello world').hexdigest()}"
    kid = drive.seed_file(prefix, key, b"hello world " + CANARY.encode())
    other = drive.seed_file(prefix, "unrelated.txt", b"unrelated")

    inbox = drive.seed_folder("inbox")
    ulid = generate_ulid()
    drive.seed_file(inbox, f"{ulid}.sidecar.json", json.dumps(
        {"metadata": {"id": sid}}).encode())
    drive.seed_file(inbox, f"{ulid}.raw", b"raw-bytes")
    drive.seed_file(inbox, f"{ulid}.sig", b"sig-bytes")

    quarantine = drive.seed_folder("quarantine")
    qdate = drive.seed_folder("2026-09-27", parent=quarantine)
    qsame = drive.seed_file(qdate, f"GITBUNDLE-s10--{UUID}-{'0' * 64}", b"0123456789")
    qother = drive.seed_file(qdate, "other-quarantined.txt", b"q")

    admin = AdminDeps(
        drive=drive, clock=FixedClock("2026-09-27T10:00:00Z"),
        workdir=tmp_path / "work", repo="agora", repo_uuid=UUID,
        prefix_folder_id=prefix, quarantine_folder_id=quarantine,
        inbox_folder_ids=(inbox,), repo_uuids=(UUID,),
        known_clones=("mac-clone-1",))
    admin.workdir.mkdir(parents=True, exist_ok=True)
    return {
        "store": store, "drive": drive, "admin": admin, "sid": sid,
        "manifest_id": mid, "bundle_id": bid, "bak_id": bak, "key_id": kid,
        "other_id": other, "qsame": qsame, "qother": qother,
        "inbox_ulid": ulid, "key": key,
    }


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
    assert f"GITBUNDLE-s10--{UUID}-{'0' * 64}" in qnames
    assert "other-quarantined.txt" not in qnames
    assert plan.repo_uuids == (UUID,)
    assert plan.known_clones == ("mac-clone-1",)
    assert plan.snapshot_remap == {}


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


def test_plan_hash_gates_apply(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    h1 = plan_hash(plan)
    assert h1 == plan_hash(plan)
    with pytest.raises(AdminError, match="確認碼"):
        apply_erase(plan, confirm="0" * 64, admin=fx["admin"],
                    store=fx["store"], repo_dir=tmp_path)


def test_apply_requires_filter_repo(tmp_path: Path, monkeypatch) -> None:
    fx = _fixture(tmp_path)
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(AdminError, match="git-filter-repo"):
        apply_erase(plan, confirm=plan_hash(plan), admin=fx["admin"],
                    store=fx["store"], repo_dir=tmp_path)


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
    assert len(remap) == 2  # 兩份快照都有 m1
    assert len(old_blobs) == 2  # FakeRawStorage 是 git 模式，舊 blob 可 strip
    new_shas = [s.snapshot_sha256 for s in store.snapshots(fx["sid"])]
    assert new_shas == [remap[o] for o in old_shas]
    # 新 raw 拿掉了 m1（第 0 份連 CANARY 一起消失），m2 保留
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
    assert first.get("erased") is True  # 接續點訊息本身被抹除
    second = store.get_record(f"handoff:{other_ulid}")
    assert second is not None
    assert second["body"]["continuation"]["snapshot_sha256"] == new
    assert second.get("erased") is None  # m2 還在，不標


def test_verify_remote_counts_and_vulns(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    drive = fx["drive"]
    # canary 找得到（只輸出計數）
    checks = verify_remote(
        drive=drive, folder_ids=[fx["admin"].prefix_folder_id],
        repo_dir=None, repo_uuid=UUID, canary=CANARY,
        manifest_text=f"GITBUNDLE-s10--{UUID}-{'0' * 64}\n".encode())
    by_loc = {c.location: c.count for c in checks}
    assert by_loc[f"drive:{fx['admin'].prefix_folder_id}"] >= 1
    assert by_loc["unlisted-bundle"] == 0
    assert_clean([c for c in checks if c.location != f"drive:{fx['admin'].prefix_folder_id}"])

    # 漏洞 1：manifest 沒列的 bundle → 判失敗
    checks = verify_remote(
        drive=drive, folder_ids=[fx["admin"].prefix_folder_id],
        repo_dir=None, repo_uuid=UUID, canary="",
        manifest_text=f"GITBUNDLE-s10--{UUID}-{'1' * 64}\n".encode(),
        check_canary=False)
    by_loc = {c.location: c.count for c in checks}
    assert by_loc["unlisted-bundle"] == 1
    with pytest.raises(AdminError):
        assert_clean(checks)
    # 空 manifest 解析失敗也算失敗（fail-closed：空 active 即 MismatchError）
    with pytest.raises(AdminError):
        verify_remote(drive=drive, folder_ids=[fx["admin"].prefix_folder_id],
                      repo_dir=None, repo_uuid=UUID, canary="",
                      manifest_text=b"", check_canary=False)

    # 漏洞 2：cat-file 失敗直接 raise，不略過
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    (repo / "f.txt").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    with pytest.raises(AdminError, match="cat-file"):
        verify_remote(drive=drive, folder_ids=[], repo_dir=repo, repo_uuid=UUID,
                      canary="", expected_blobs=["0" * 40])
    real_blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", str(repo / "f.txt")],
        check=True, capture_output=True, text=True).stdout.strip()
    checks = verify_remote(drive=drive, folder_ids=[], repo_dir=repo,
                           repo_uuid=UUID, canary="",
                           expected_blobs=[real_blob])
    assert checks == [] or all(c.count == 0 for c in checks)


def test_erasure_record_contains_no_content(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    from aistorage.admin.erase import _write_erasure_record, ErasePlan, EraseTarget
    plan = plan_erase([EraseTarget(kind="session", session_id=fx["sid"])],
                      admin=fx["admin"], store=fx["store"])
    record_id = _write_erasure_record(
        fx["store"], who="admin", at="2026-09-27T10:00:00Z",
        why="測試", plan=plan, remap={"a" * 64: "b" * 64})
    raw = (fx["store"].worktree / f"_admin/erasures/{record_id}.json").read_text()
    assert CANARY not in raw
    obj = json.loads(raw)
    assert obj["who"] == "admin" and obj["snapshot_remap"] == {"a" * 64: "b" * 64}
