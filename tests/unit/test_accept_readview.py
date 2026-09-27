"""4.1 讀取視圖（readview＋publish）的驗收測試（C 線撰寫）。

未看實作，只依文件與簽名：
- docs/impl/group4-modules.md 第 2 節（格式）、第 4 節（發佈）、第 8.2 節（案例表）
- design D5、ADR 0008。
失敗＝回報 B 線（impl3），不改 src/。
"""

import hashlib
import json
from pathlib import Path

import pytest

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.converters import get_converter
from aistorage.converters.base import ConversionError
from aistorage.drive.fake import FakeDrive
from aistorage.errors import MismatchError, ReadError
from aistorage.intake.evaluate import Decision, DecisionKind
from aistorage.intake.scan import InboxItem
from aistorage.publish.plan import (
    collect_pinned_snapshots,
    collect_snapshot_targets,
    plan_publish,
)
from aistorage.publish.publisher import DriveReadViewPublisher
from aistorage.publish.rejections import (
    collect_rejections,
    collect_run_rejections,
    collect_true_copy_rejections,
)
from aistorage.readview.model import (
    FileRef,
    Manifest,
    initial_manifest,
    next_manifest,
    parse_manifest,
    serialize_manifest,
    trusted_ids,
)
from aistorage.readview.naming import index_name, manifest_name, reading_name
from aistorage.schema import generate_ulid
from aistorage.search.index import RejectionRow

OP_BASIC = Path("tests/unit/data/converters/opencode/basic.json")
PRODUCER = "profile:mac-opencode"
T0 = "2026-09-27T08:00:00.000Z"


def _export_info() -> str:
    return json.loads(OP_BASIC.read_bytes().decode("utf-8"))["info"]["id"]


def _raw_variant(tmp_path: Path, name: str, title: str) -> tuple[Path, str, int]:
    data = json.loads(OP_BASIC.read_bytes().decode("utf-8"))
    data["info"]["title"] = title
    raw = json.dumps(data, sort_keys=True).encode("utf-8")
    p = tmp_path / name
    p.write_bytes(raw)
    return p, hashlib.sha256(raw).hexdigest().lower(), len(raw)


def _rec(session_id: str, sha: str, size: int, snapshot_at: str,
         updated_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        producer=PRODUCER,
        created_at="2026-09-27T08:00:00Z",
        updated_at=updated_at or snapshot_at,
        status="running",
        snapshot_at=snapshot_at,
        raw_sha256=sha,
        raw_size=size,
        committed_at="2026-09-27T08:01:00Z",
        last_item_key=generate_ulid(),
        title=f"標題 {session_id}",
    )


def _store_with_history(tmp_path: Path, session_id: str | None = None
                        ) -> tuple[AgoraStore, str, dict[str, str]]:
    """真本：s1 兩份快照（舊的被交接單釘住）＋交接單／認領／Link。回傳 (store, sid, shas)。"""
    worktree = tmp_path / "agora"
    worktree.mkdir(exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    sid = session_id or f"opencode:{_export_info()}"
    p1, sha1, size1 = _raw_variant(tmp_path, "snap1.json", "第一版")
    p2, sha2, size2 = _raw_variant(tmp_path, "snap2.json", "第二版")
    store.put_session(_rec(sid, sha1, size1, "2026-09-27T08:10:00.000Z"), p1)
    store.put_session(_rec(sid, sha2, size2, "2026-09-27T08:20:00.000Z"), p2)

    handoff_ulid = generate_ulid()
    handoff_id = f"handoff:{handoff_ulid}"
    store.put_json(layout.handoff_path(handoff_ulid), {
        "id": handoff_id, "type": "handoff", "producer": PRODUCER,
        "created_at": T0, "updated_at": T0, "case_id": None, "provenance": None,
        "body": {"target_session_id": sid,
                 "continuation": {"snapshot_sha256": sha1, "message_id": "m1"},
                 "content": "接手"},
        "claimed_by": None,
        "committed_at": T0,
    })
    claim_ulid = generate_ulid()
    claim_id = f"claim:{claim_ulid}"
    claimer = "opencode:child-1"
    store.put_json(layout.claim_path(claim_ulid), {
        "id": claim_id, "type": "claim", "producer": PRODUCER,
        "created_at": T0, "updated_at": T0, "case_id": None, "provenance": None,
        "body": {"handoff_id": handoff_id, "claimer_session_id": claimer},
        "committed_at": T0,
    })
    handoff = store.get_record(handoff_id)
    assert handoff is not None
    handoff["claimed_by"] = {"claim_id": claim_id, "session_id": claimer, "at": T0}
    store.put_json(layout.handoff_path(handoff_ulid), handoff)
    store.put_json(layout.continuation_link_path(claimer, handoff_ulid), {
        "from": claimer, "to": sid,
        "continuation": {"snapshot_sha256": sha1, "message_id": "m1"},
        "handoff_id": handoff_id, "claim_id": claim_id,
    })
    return store, sid, {"old": sha1, "new": sha2}


# ---------------------------------------------------------------------------
# readview/model＋naming（§2、§8.2）
# ---------------------------------------------------------------------------

def test_manifest_roundtrip_and_format() -> None:
    m = Manifest(
        format="aistorage.readview/v1", element="agora", generation=3,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="abc",
        converter_versions={"opencode": "1"},
        index=FileRef(id="idx-1", sha256="0" * 64, size=100),
        files=("idx-1", "r-1"), retired=(("old-1", 1),))
    data = serialize_manifest(m)
    assert data.endswith(b"\n")
    assert list(json.loads(data.decode("utf-8")).keys()) == sorted(
        json.loads(data.decode("utf-8")).keys())
    assert parse_manifest(data) == m


def test_manifest_parse_rejects_garbage() -> None:
    with pytest.raises(MismatchError):
        parse_manifest(b"not json at all")
    with pytest.raises(MismatchError):
        parse_manifest(json.dumps({"format": "wrong", "generation": 1}).encode())
    with pytest.raises(MismatchError):
        parse_manifest(json.dumps(
            {"format": "aistorage.readview/v1"}).encode())


def test_trusted_ids_covers_manifest_files_retired() -> None:
    m = Manifest(
        format="aistorage.readview/v1", element="agora", generation=2,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="abc",
        converter_versions={},
        index=FileRef(id="idx-2", sha256="0" * 64, size=10),
        files=("idx-2", "r-2"), retired=(("old-9", 1),))
    assert trusted_ids(m, "manifest-1") == {"manifest-1", "idx-2", "r-2", "old-9"}


def test_next_manifest_generation_and_retire() -> None:
    prev = Manifest(
        format="aistorage.readview/v1", element="agora", generation=5,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="aaa",
        converter_versions={"opencode": "1"},
        index=FileRef(id="idx-5", sha256="0" * 64, size=10),
        files=("idx-5", "r-5"), retired=(("old-1", 3),))
    nxt = next_manifest(
        prev, index=FileRef(id="idx-6", sha256="1" * 64, size=20),
        files=("idx-6", "r-6"), reading_refs=(),
        retire_now=("idx-5", "r-5"), delete_now=("old-1",),
        published_at="2026-09-27T10:00:00.000Z", agora_main_sha="bbb",
        converter_versions={"opencode": "1"})
    assert nxt.generation == 6
    assert nxt.index is not None and nxt.index.id == "idx-6"
    assert set(nxt.files) == {"idx-6", "r-6"}
    assert "old-1" not in nxt.files
    assert all(fid != "old-1" for fid, _ in nxt.retired)
    assert ("idx-5", 6) in nxt.retired and ("r-5", 6) in nxt.retired
    assert nxt.agora_main_sha == "bbb"


def test_initial_manifest_is_generation_zero() -> None:
    m = initial_manifest(element="agora", agora_main_sha="",
                         published_at="2026-09-27T09:00:00.000Z")
    assert m.generation == 0
    assert parse_manifest(serialize_manifest(m)) == m


def test_naming_conventions() -> None:
    sha = "ab" * 32
    assert index_name(7, sha) == f"index-g7-{sha[:12]}.sqlite"
    assert manifest_name() == "readview-manifest.json"
    assert reading_name("opencode:s1", sha) == reading_name("opencode:s1", sha)
    assert reading_name("opencode:s1", sha) != reading_name("opencode:s1", "cd" * 32)
    assert reading_name("opencode:s1", sha) != reading_name("opencode:s2", sha)


# ---------------------------------------------------------------------------
# publish/plan（§4.1、§8.2）
# ---------------------------------------------------------------------------

def test_collect_targets_latest_and_pinned(tmp_path: Path) -> None:
    store, sid, shas = _store_with_history(tmp_path)
    pinned = collect_pinned_snapshots(store)
    assert pinned.get(sid) == {shas["old"]}
    targets = {(t.session_id, t.snapshot_sha256): t
               for t in collect_snapshot_targets(store)}
    assert targets[(sid, shas["old"])].pinned is True
    assert targets[(sid, shas["old"])].is_latest is False
    assert targets[(sid, shas["new"])].pinned is False
    assert targets[(sid, shas["new"])].is_latest is True


def test_plan_first_publish_all_new(tmp_path: Path) -> None:
    store, sid, shas = _store_with_history(tmp_path)
    plan = plan_publish(store, None, None, {"opencode": "1"})
    assert {(t.session_id, t.snapshot_sha256) for t in plan.targets} == {
        (sid, shas["old"]), (sid, shas["new"])}
    assert {r.snapshot_sha256 for r in plan.readings_new} == {shas["old"], shas["new"]}
    assert plan.readings_keep == ()
    assert plan.retire_now == () and plan.delete_now == ()


def test_plan_no_change_all_keep(tmp_path: Path) -> None:
    store, sid, shas = _store_with_history(tmp_path)
    prev_readings = {
        (sid, shas["old"]): FileRef(id="f-old", sha256="0" * 64, size=1),
        (sid, shas["new"]): FileRef(id="f-new", sha256="1" * 64, size=2),
    }
    plan = plan_publish(store, None, prev_readings, {"opencode": "1"})
    assert plan.readings_new == ()
    assert {r.snapshot_sha256 for r in plan.readings_keep} == {shas["old"], shas["new"]}


def test_plan_converter_change_forces_full(tmp_path: Path) -> None:
    store, sid, shas = _store_with_history(tmp_path)
    prev = Manifest(
        format="aistorage.readview/v1", element="agora", generation=1,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="aaa",
        converter_versions={"opencode": "1"},
        index=FileRef(id="idx-1", sha256="0" * 64, size=10),
        files=("idx-1", "f-old", "f-new"), retired=())
    prev_readings = {
        (sid, shas["old"]): FileRef(id="f-old", sha256="0" * 64, size=1),
        (sid, shas["new"]): FileRef(id="f-new", sha256="1" * 64, size=2),
    }
    plan = plan_publish(store, prev, prev_readings, {"opencode": "2"})
    assert plan.full_rebuild is True
    assert {r.snapshot_sha256 for r in plan.readings_new} == {shas["old"], shas["new"]}


def test_plan_batching_holds_manifest_switch(tmp_path: Path) -> None:
    store, sid, shas = _store_with_history(tmp_path)
    plan = plan_publish(store, None, None, {"opencode": "1"}, max_new_readings=1)
    assert len(plan.readings_new) == 1
    assert plan.batch_remaining == 1
    assert plan.switch_index is False


def test_plan_retire_now_and_delete_next_generation(tmp_path: Path) -> None:
    store, sid, shas = _store_with_history(tmp_path)
    prev = Manifest(
        format="aistorage.readview/v1", element="agora", generation=5,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="aaa",
        converter_versions={"opencode": "1"},
        index=FileRef(id="idx-5", sha256="0" * 64, size=10),
        files=("keepfid", "gonefid"), retired=(("oldfid", 3), ("youngfid", 5)))
    prev_readings = {(sid, shas["new"]): FileRef(
        id="keepfid", sha256="2" * 64, size=3)}
    plan = plan_publish(store, prev, prev_readings, {"opencode": "1"})
    assert "gonefid" in plan.retire_now
    assert "keepfid" not in plan.retire_now
    assert "oldfid" in plan.delete_now  # 退役世代 3 < 6-1
    assert "youngfid" not in plan.delete_now  # 退役世代 5，留到下一輪


# ---------------------------------------------------------------------------
# publish/publisher（§4.2、§8.2）
# ---------------------------------------------------------------------------

def _publisher_fixture(tmp_path: Path, folder_name: str = "rv"
                       ) -> tuple[FakeDrive, str, AgoraStore, str, dict]:
    store, sid, shas = _store_with_history(tmp_path)
    drive = FakeDrive()
    folder = drive.seed_folder(folder_name)
    manifest = initial_manifest(element="agora", agora_main_sha="",
                                published_at="2026-09-27T09:00:00.000Z")
    drive.seed_file(folder, "readview-manifest.json",
                    serialize_manifest(manifest), file_id="manifest-1")
    converters = {"opencode": get_converter("opencode")}
    clock = FixedClock("2026-09-27T10:00:00Z")
    return drive, folder, store, sid, {
        "converters": converters, "clock": clock, "shas": shas}


def _publish(drive: FakeDrive, folder: str, store: AgoraStore, ctx: dict,
             tmp_path: Path, **kw) -> object:
    publisher = DriveReadViewPublisher(
        drive, folder_id=folder, manifest_file_id="manifest-1",
        converters=ctx["converters"], clock=ctx["clock"],
        workdir=tmp_path / "work")
    return publisher.publish(store, agora_main_sha="deadbeef", **kw)


def _manifest_in_drive(drive: FakeDrive) -> Manifest:
    from aistorage.readview.model import parse_manifest as _parse
    return _parse(drive.download_bytes("manifest-1", max_bytes=1 << 20))


def test_publish_first_round_end_to_end(tmp_path: Path) -> None:
    """首次發佈：reading＋index＋manifest 原地更新，manifest id 不變。"""
    drive, folder, store, sid, ctx = _publisher_fixture(tmp_path)
    report = _publish(drive, folder, store, ctx, tmp_path)
    assert report.generation == 1
    assert report.readings_created == 2  # 舊（被釘住）＋新
    assert report.index_file_id is not None
    manifest = _manifest_in_drive(drive)
    assert manifest.generation == 1
    assert manifest.index is not None and manifest.index.id == report.index_file_id
    # manifest 是原地更新：同一個 file id，沒有第二份 manifest
    names = [f.name for f in drive.list_children(folder)]
    assert names.count("readview-manifest.json") == 1
    assert drive.get("manifest-1").id == "manifest-1"
    assert report.file_count >= 3


def test_publish_no_change_skipped(tmp_path: Path) -> None:
    drive, folder, store, sid, ctx = _publisher_fixture(tmp_path)
    first = _publish(drive, folder, store, ctx, tmp_path)
    before = drive.snapshot()
    second = _publish(drive, folder, store, ctx, tmp_path)
    assert second.status == "skipped"
    assert drive.snapshot() == before
    assert first.status != "skipped"


def test_publish_read_error_keeps_manifest(tmp_path: Path) -> None:
    """任何一步 ReadError 都不更新 manifest。"""
    store, sid, shas = _store_with_history(tmp_path)
    seed = FakeDrive()
    folder = seed.seed_folder("rv")
    manifest = initial_manifest(element="agora", agora_main_sha="",
                                published_at="2026-09-27T09:00:00.000Z")
    seed.seed_file(folder, "readview-manifest.json",
                   serialize_manifest(manifest), file_id="manifest-1")

    class FailDrive(FakeDrive):
        def create(self, parent_id, name, content, **kwargs):
            raise ReadError("boom-create")

    drive = FailDrive()
    # 把同樣的檔案搬進會失敗的 drive（id 保持一致）
    for f in seed.list_children(folder):
        drive.seed_file(folder, f.name,
                        seed.download_bytes(f.id, max_bytes=1 << 26), file_id=f.id)
    converters = {"opencode": get_converter("opencode")}
    publisher = DriveReadViewPublisher(
        drive, folder_id=folder, manifest_file_id="manifest-1",
        converters=converters, clock=FixedClock("2026-09-27T10:00:00Z"),
        workdir=tmp_path / "work")
    with pytest.raises(ReadError):
        publisher.publish(store, agora_main_sha="deadbeef")
    assert _manifest_in_drive(drive).generation == 0
    assert drive.get("manifest-1").id == "manifest-1"


def test_publish_dry_run_changes_nothing(tmp_path: Path) -> None:
    drive, folder, store, sid, ctx = _publisher_fixture(tmp_path)
    before = drive.snapshot()
    report = _publish(drive, folder, store, ctx, tmp_path, dry_run=True)
    assert report.dry_run is True
    assert drive.snapshot() == before


def test_publish_records_failed_readings(tmp_path: Path) -> None:
    """轉換失敗的快照記入 readings_failed，發佈照常完成。"""

    class BoomConverter:
        source = "opencode"

        def __init__(self, real) -> None:
            self._real = real

        def facts(self, raw_path):
            return self._real.facts(raw_path)

        def convert(self, *args, **kwargs):
            raise ConversionError("boom")

        def child_session_ids(self, raw_path):
            return ()

    drive, folder, store, sid, ctx = _publisher_fixture(tmp_path)
    ctx = {**ctx, "converters": {"opencode": BoomConverter(ctx["converters"]["opencode"])}}
    report = _publish(drive, folder, store, ctx, tmp_path)
    # PM 決定：同時記 session id 與 snapshot sha（(session_id, snapshot_sha256) 對）。
    assert set(report.readings_failed) == {
        (sid, ctx["shas"]["old"]), (sid, ctx["shas"]["new"])}
    assert _manifest_in_drive(drive).generation == 1


def test_publish_delete_guards_parents(tmp_path: Path) -> None:
    """退役滿一個世代才永久刪除；刪除前 get() 確認 parents。

    世代線：prev gen 5 → 新世代 6；retired (victim,3) 刪除，(keeper5,5) 保留；
    files 裡不再被引用的 gonefid 進入 retired。
    """
    from aistorage.search.index import IndexEntry, IndexMeta, build_index
    store, sid, shas = _store_with_history(tmp_path)
    drive = FakeDrive()
    folder = drive.seed_folder("rv")

    old_index_path = tmp_path / "old.sqlite3"
    build_index(
        old_index_path,
        entries=[IndexEntry(
            metadata={"session_id": sid, "source": "opencode", "title": "t",
                      "producer": PRODUCER, "case_id": None, "status": "running",
                      "stopped_at": None, "in_progress": False,
                      "created_at": T0, "updated_at": T0, "snapshot_at": T0,
                      "raw_sha256": shas["old"], "raw_size": 1, "parent_id": None,
                      "reading_status": "ok", "reading_error_code": None,
                      "committed_at": T0},
            snapshots=[{"snapshot_sha256": shas["old"], "snapshot_at": T0,
                        "committed_at": T0, "via": "sync",
                        "file_id": "keep-old", "sha256": shas["old"], "size": 1}],
            reading=None, reading_ref=None)],
        meta=IndexMeta(generation=5, built_at=T0, agora_main_sha="aaa",
                       converter_versions={"opencode": "1"}),
    )
    drive.seed_file(folder, "old.sqlite3", old_index_path.read_bytes(),
                    file_id="idx-5")
    drive.seed_file(folder, "keep-old.json", b"old reading", file_id="keep-old")
    drive.seed_file(folder, "gone.json", b"unreferenced", file_id="gonefid")
    victim = drive.seed_file(folder, "victim.json", b"old reading")
    keeper = drive.seed_file(folder, "keeper.json", b"young reading")
    manifest = Manifest(
        format="aistorage.readview/v1", element="agora", generation=5,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="aaa",
        converter_versions={"opencode": "1"},
        index=FileRef(id="idx-5", sha256="0" * 64, size=10),
        files=("idx-5", "keep-old", "gonefid"), retired=((victim, 3), (keeper, 5)))
    # index FileRef 的雜湊必須對得上（下載驗證用）
    index_raw = old_index_path.read_bytes()
    manifest = Manifest(
        format="aistorage.readview/v1", element="agora", generation=5,
        published_at="2026-09-27T09:00:00.000Z", agora_main_sha="aaa",
        converter_versions={"opencode": "1"},
        index=FileRef(id="idx-5", sha256=hashlib.sha256(index_raw).hexdigest(),
                      size=len(index_raw)),
        files=("idx-5", "keep-old", "gonefid"), retired=((victim, 3), (keeper, 5)))
    drive.seed_file(folder, "readview-manifest.json",
                    serialize_manifest(manifest), file_id="manifest-1")
    converters = {"opencode": get_converter("opencode")}
    publisher = DriveReadViewPublisher(
        drive, folder_id=folder, manifest_file_id="manifest-1",
        converters=converters, clock=FixedClock("2026-09-27T10:00:00Z"),
        workdir=tmp_path / "work")
    report = publisher.publish(store, agora_main_sha="deadbeef")
    assert victim in report.deleted
    assert keeper not in report.deleted
    assert "gonefid" in report.retired
    assert keeper in [f.id for f in drive.list_children(folder)]
    assert victim not in [f.id for f in drive.list_children(folder)]


def test_publish_rejections_end_to_end(tmp_path: Path) -> None:
    drive, folder, store, sid, ctx = _publisher_fixture(tmp_path)
    rows = [RejectionRow(item_key=generate_ulid(), code="bad_signature",
                         at="2026-09-27T07:00:00.000Z",
                         item_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                         authenticated=True)]
    report = _publish(drive, folder, store, ctx, tmp_path, run_rejections=rows)
    assert report.rejections == 1
    manifest = _manifest_in_drive(drive)
    index_id = manifest.index.id
    assert index_id is not None
    index_raw = drive.download_bytes(index_id, max_bytes=1 << 26)
    index_path = tmp_path / "check.sqlite3"
    index_path.write_bytes(index_raw)
    import sqlite3
    con = sqlite3.connect(str(index_path))
    try:
        from aistorage.search.query import get_rejection
        got = get_rejection(con, rows[0].item_key)
    finally:
        con.close()
    assert got is not None and got.code == "bad_signature" and got.authenticated


# ---------------------------------------------------------------------------
# publish/rejections（§4.4）
# ---------------------------------------------------------------------------

def _rejection_decision(item_key: str, code: str, authenticated: bool) -> Decision:
    return Decision(
        kind=DecisionKind.REJECT,
        item=InboxItem(item_key=item_key, inbox_folder_id="inbox-1"),
        code=code,
        authenticated=authenticated,
        producer="profile:mac-opencode" if authenticated else None,
    )


def test_collect_rejections_true_copy_and_run(tmp_path: Path) -> None:
    """§4.4：真本清冊（驗章之後）＋本輪驗章前（authenticated=False）的 REJECT。

    驗章通過的本輪決策不走 run 蒐集（run.py 會先寫進真本清冊，再以真本列入）。
    """
    store, sid, shas = _store_with_history(tmp_path)
    true_key = generate_ulid()
    store.put_json(f"_committer/rejections/{true_key}.json", {
        "code": "stale", "at": "2026-09-27T07:00:00.000Z",
        "item_id": "opencode:s1"})
    run_key = generate_ulid()
    orphan_key = generate_ulid()
    decisions = [_rejection_decision(run_key, "bad_signature", True),
                 _rejection_decision(orphan_key, "orphan", False)]
    assert {r.item_key for r in collect_run_rejections(decisions)} == {orphan_key}
    rows = {r.item_key: r for r in collect_rejections(store, decisions)}
    assert rows[true_key].code == "stale"
    assert rows[true_key].authenticated is True
    assert rows[true_key].item_id == "opencode:s1"
    assert rows[orphan_key].code == "orphan"
    assert rows[orphan_key].authenticated is False
    assert set(rows) == {true_key, orphan_key}
    assert [r.item_key for r in collect_true_copy_rejections(store)] == [true_key]
