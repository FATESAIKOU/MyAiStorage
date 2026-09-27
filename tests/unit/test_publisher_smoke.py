"""讀取視圖發佈器（publish/publisher）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

以 FakeDrive 驗證 API 呼叫的順序與數量、manifest 的固定 id、刪除前的 parents
檢查、冪等與失敗時不更新 manifest。範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import MismatchError, ReadError
from aistorage.publish.publisher import (
    SEARCH_INDEX_FORMAT,
    DriveReadViewPublisher,
    load_manifest,
    load_manifest_or_none,
)
from aistorage.publish.rejections import RejectionRow
from aistorage.readview.model import (
    initial_manifest,
    parse_manifest,
    serialize_manifest,
    trusted_ids,
)
from aistorage.readview.naming import index_name, reading_name
from aistorage.schema import generate_ulid

T0 = "2026-09-27T08:00:00.000Z"
T1 = "2026-09-27T09:00:00.000Z"
PRODUCER = "profile:mac-opencode"
MANIFEST_ID = "manifest-file-0001"


# ---------------------------------------------------------------------------
# 測試用的轉換器與索引建立器（search 模組由 impl2 實作，這裡用雙測試替身）
# ---------------------------------------------------------------------------


class FakeConverter:
    """最簡閱讀版轉換器：raw 是 {"texts": [...], "title": ...} 的 JSON。"""

    source = "opencode"
    version = "1"

    def __init__(self, *, fail: bool = False, version: str = "1",
                 fail_only: tuple[str, ...] = ()) -> None:
        self.fail = fail
        self.fail_only = fail_only
        self.version = version

    def facts(self, raw_path: Path, *, session_id: str | None = None):  # pragma: no cover
        raise NotImplementedError

    def convert(
        self,
        raw_path: Path,
        *,
        session_id: str,
        parent_id: str | None = None,
        snapshot_sha256: str | None = None,
    ) -> dict:
        if self.fail or session_id in self.fail_only:
            raise ValueError("轉換失敗（測試注入）")
        data = raw_path.read_bytes()
        payload = json.loads(data.decode("utf-8"))
        return {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": self.source,
            "title": payload.get("title"),
            "parent_id": parent_id,
            "snapshot_sha256": hashlib.sha256(data).hexdigest(),
            "in_progress": False,
            "messages": [
                {
                    "message_id": f"m{i}",
                    "index": i,
                    "role": "user" if i % 2 == 0 else "assistant",
                    "created_at": T0,
                    "completed": True,
                    "reverted": False,
                    "parts": [{"type": "text", "text": t}],
                }
                for i, t in enumerate(payload["texts"])
            ],
        }

    def child_session_ids(self, raw_path: Path, *, session_id: str | None = None):
        return ()


def fake_build_index(path: Path, **kwargs: Any) -> Any:
    """預設走 search.index 的真實 build_index；這裡只留一個可注入的鉤子。"""
    from aistorage.search.index import build_index

    return build_index(path, **kwargs)


# ---------------------------------------------------------------------------
# 真本
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> AgoraStore:
    return AgoraStore(
        worktree=tmp_path / "wt",
        raw_storage=FakeRawStorage(),
        git=None,
        temp_dir=tmp_path / "store_tmp",
    )


def _add_session(
    store: AgoraStore,
    session_id: str,
    texts: tuple[str, ...],
    *,
    snapshot_at: str = T0,
    title: str | None = None,
    parent_id: str | None = None,
) -> str:
    raw = json.dumps({"texts": list(texts), "title": title}, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    raw_path = store._temp_dir / f"seed-{sha}.raw"
    raw_path.write_bytes(raw)
    rec = SessionRecord(
        id=session_id,
        producer=PRODUCER,
        created_at=T0,
        updated_at=snapshot_at,
        status="stopped",
        snapshot_at=snapshot_at,
        raw_sha256=sha,
        raw_size=len(raw),
        committed_at=snapshot_at,
        last_item_key=generate_ulid(),
        stopped_at=snapshot_at,
        title=title,
        parent_id=parent_id,
    )
    store.put_session(rec, raw_path)
    return sha


def _handoff(
    store: AgoraStore,
    target_session_id: str,
    snapshot_sha256: str,
    *,
    author_session_id: str | None = None,
) -> str:
    """寫一張釘住某個快照的交接單，回傳 handoff id。

    author_session_id 放進 body（寫入者提供的業務內容）；不給就是沒有明確作者，
    發佈時由提交流程填成 target_session_id（PM 決定）。
    """
    ulid = generate_ulid()
    handoff_id = f"handoff:{ulid}"
    record: dict[str, Any] = {
        "id": handoff_id,
        "type": "handoff",
        "producer": PRODUCER,
        "created_at": T0,
        "updated_at": T0,
        "case_id": None,
        "provenance": None,
    }
    body: dict[str, Any] = {
        "target_session_id": target_session_id,
        "continuation": {
            "snapshot_sha256": snapshot_sha256,
            "message_id": "m0",
        },
        "content": "交接說明（自編測試內容）",
    }
    if author_session_id is not None:
        body["author_session_id"] = author_session_id
    record["body"] = body
    record["claimed_by"] = None
    record["committed_at"] = T0
    store.put_json(layout.handoff_path(ulid), record)
    return handoff_id


def _drive(tmp_path: Path, *, clock: FixedClock) -> FakeDrive:
    drive = FakeDrive(clock=clock)
    folder = drive.seed_folder("readview")
    drive.seed_file(
        folder,
        "readview-manifest.json",
        serialize_manifest(initial_manifest(published_at=T0)),
        file_id=MANIFEST_ID,
    )
    return drive


def _publisher(
    drive: FakeDrive,
    tmp_path: Path,
    *,
    clock: FixedClock,
    converter: FakeConverter | None = None,
    **kw: Any,
) -> DriveReadViewPublisher:
    return DriveReadViewPublisher(
        drive,
        folder_id=drive._files[MANIFEST_ID].parents[0],
        manifest_file_id=MANIFEST_ID,
        converters={"opencode": converter or FakeConverter()},
        clock=clock,
        workdir=tmp_path / "work",
        index_builder=fake_build_index,
        **kw,
    )


def _write_ops(drive: FakeDrive) -> list[tuple[str, str | None]]:
    return [c for c in drive.calls if c[0] in FakeDrive.WRITE_OPS]


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_first_publish_creates_readings_index_then_updates_manifest(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    s1 = _add_session(store, "opencode:ses_1", ("甲",), title="甲的 Session")
    _add_session(store, "opencode:ses_2", ("乙",))
    pub = _publisher(drive, tmp_path, clock=clock)

    report = pub.publish(store, agora_main_sha="main-1")
    assert report.published and report.generation == 1
    assert report.readings_created == 2 and report.readings_failed == ()

    ops = [op for op, _ in _write_ops(drive)]
    assert ops == ["create", "create", "create", "update_content"]  # 2 readings + index
    assert not [op for op, _ in _write_ops(drive) if op == "delete_permanently"]

    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m.generation == 1 and m.agora_main_sha == "main-1"
    assert m.index is not None
    assert len(m.files) == 3  # index ＋ 2 readings

    folder = drive._files[MANIFEST_ID].parents[0]
    children = drive.list_children(folder)
    assert len(children) == 4  # manifest ＋ 2 readings ＋ index
    names = sorted(f.name for f in children)
    assert reading_name("opencode:ses_1", s1) in names
    index_sha = hashlib.sha256(drive.download_bytes(m.index.id, max_bytes=1 << 20)).hexdigest()
    assert index_name(1, index_sha) in names
    assert index_name(1, index_sha) == drive.get(m.index.id).name

    # manifest 自身的 id 不變（信任錨點）
    assert drive.get(MANIFEST_ID).name == "readview-manifest.json"
    assert trusted_ids(m, MANIFEST_ID) == frozenset({MANIFEST_ID, *m.files})


def test_manifest_update_is_in_place_same_file_id(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    before = drive.get(MANIFEST_ID)
    pub.publish(store, agora_main_sha="main-1")
    after = drive.get(MANIFEST_ID)
    assert after.id == before.id == MANIFEST_ID
    assert after.created_time == before.created_time  # 原地更新，不是新建檔
    assert after.size > 0 and before.size > 0


def test_unchanged_main_and_rejections_is_skipped(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    pub.publish(store, agora_main_sha="main-1")
    drive.calls.clear()
    report = pub.publish(store, agora_main_sha="main-1")
    assert report.skipped and report.generation == 1
    assert _write_ops(drive) == []


def test_new_rejection_triggers_a_new_generation(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    pub.publish(store, agora_main_sha="main-1")
    report = pub.publish(
        store,
        agora_main_sha="main-1",
        run_rejections=[
            RejectionRow(item_key=generate_ulid(), code="orphan", at=T0)
        ],
    )
    assert report.published and report.generation == 2
    # 第二輪只有新的 index，reading 沿用（不重新上傳）
    assert report.readings_created == 0 and report.readings_kept == 1


def test_changed_session_reuploads_only_that_reading(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_2", ("乙",))
    pub = _publisher(drive, tmp_path, clock=clock)

    pub.publish(store, agora_main_sha="main-1")
    first = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    kept = {fid for fid in first.files if "reading" in drive.get(fid).name}

    clock.set_time(T1)
    _add_session(store, "opencode:ses_1", ("甲", "丙"), snapshot_at=T1)
    drive.calls.clear()
    report = pub.publish(store, agora_main_sha="main-2")
    assert report.readings_created == 1 and report.readings_kept == 1
    assert len([op for op, _ in _write_ops(drive) if op == "create"]) == 2  # reading + index

    second = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert second.generation == 2
    # 變動 Session 的舊 reading 退役（先不刪），ses_2 的 reading 沿用
    old_reading = next(fid for fid in kept if drive.get(fid).name.startswith("reading-")
                       and fid not in second.files)
    assert old_reading in report.retired
    assert kept - {old_reading} <= set(second.files)
    assert report.deleted == ()  # 退役滿一個世代才刪


def test_retired_files_deleted_one_generation_later(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    sha = _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    pub.publish(store, agora_main_sha="main-1")
    gen1 = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    old_reading = next(
        fid for fid in gen1.files
        if drive.get(fid).name == reading_name("opencode:ses_1", sha)
    )

    # 每一輪換一個新快照：舊 reading 在 gen2 退役，中間留一個完整世代，gen4 才刪
    reports = {}
    for i in (2, 3, 4):
        clock.set_time(T1)
        _add_session(store, "opencode:ses_1", ("甲",) * i, snapshot_at=T1)
        reports[i] = pub.publish(store, agora_main_sha=f"main-{i}")

    assert old_reading in reports[2].retired
    assert old_reading not in reports[2].deleted  # 退役那一輪不刪
    assert old_reading not in reports[3].deleted  # 中間還留一個完整世代
    assert old_reading in reports[4].deleted

    with pytest.raises(Exception):  # 已被永久刪除
        drive.get(old_reading)
    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m.generation == 4 and old_reading not in m.files


def test_delete_checks_parents_before_removing(tmp_path: Path):
    """刪除前必須確認檔案真的在讀取視圖資料夾裡。"""
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    folder = drive._files[MANIFEST_ID].parents[0]
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    pub.publish(store, agora_main_sha="main-1")
    gen1 = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    victim = gen1.index.id
    assert victim is not None

    # 把要刪的檔案搬到別的資料夾（清單被動過的情境）
    other = drive.seed_folder("elsewhere")
    drive.move(victim, from_parent=folder, to_parent=other)

    reports = {}
    for i in (2, 3, 4):
        clock.set_time(T1)
        _add_session(store, "opencode:ses_1", ("甲",) * i, snapshot_at=T1)
        reports[i] = pub.publish(store, agora_main_sha=f"main-{i}")

    # parents 不符 → 不刪，且記入 delete_failures（不讓整輪中止）
    assert any(f.startswith(victim) for f in reports[4].delete_failures)
    assert victim not in reports[4].deleted
    assert drive.get(victim).id == victim  # 還在別的資料夾，沒被刪


def test_read_error_does_not_update_manifest(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    drive.inject("create", error=ReadError, times=1)
    with pytest.raises(ReadError):
        pub.publish(store, agora_main_sha="main-1")

    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m.is_initial  # manifest 沒被動過，下一輪會重發


def test_manifest_corrupt_aborts_before_any_write(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    drive.update_content(MANIFEST_ID, b"{ broken")
    drive.calls.clear()
    with pytest.raises(MismatchError):
        pub.publish(store, agora_main_sha="main-1")
    assert _write_ops(drive) == []


def test_conversion_failure_publishes_no_reading(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    sha1 = _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_2", ("乙",))
    pub = _publisher(drive, tmp_path, clock=clock,
                     converter=FakeConverter(fail_only=("opencode:ses_1",)))

    report = pub.publish(store, agora_main_sha="main-1")
    assert report.published
    assert report.readings_created == 1  # 只有 ses_2 轉換成功
    # 失敗記錄同時有 session id 與 snapshot sha（知道是哪一版轉不出來）
    assert report.readings_failed == (("opencode:ses_1", sha1),)
    folder = drive._files[MANIFEST_ID].parents[0]
    names = [f.name for f in drive.list_children(folder)]
    assert not any(n == reading_name("opencode:ses_1", sha1) for n in names)
    assert len([n for n in names if n.startswith("reading-")]) == 1  # 只有 ses_2
    assert len([n for n in names if n.startswith("index-")]) == 1


def test_converter_version_change_forces_full_rebuild(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)
    pub.publish(store, agora_main_sha="main-1")

    pub2 = _publisher(drive, tmp_path, clock=clock,
                      converter=FakeConverter(version="2"))
    report = pub2.publish(store, agora_main_sha="main-1")
    assert report.published and report.full_rebuild and report.readings_created == 1
    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m.converter_versions == {"opencode": "2"}


def test_rebuild_epoch_bump_forces_full_rebuild(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)
    pub.publish(store, agora_main_sha="main-1")

    bumped = _publisher(drive, tmp_path, clock=clock, rebuild_epoch=1)
    report = bumped.publish(store, agora_main_sha="main-1")
    assert report.published and report.full_rebuild
    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m.rebuild_epoch == 1


def test_batched_full_rebuild_keeps_old_index_until_done(tmp_path: Path):
    """PM 決定 5：分批上傳的中間輪次不切換 index，已上傳的份數記在 pending。"""
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    s1 = _add_session(store, "opencode:ses_1", ("甲",))
    s2 = _add_session(store, "opencode:ses_2", ("乙",))
    pub = _publisher(drive, tmp_path, clock=clock, max_new_readings=1)

    report = pub.publish(store, agora_main_sha="main-1")
    assert report.readings_created == 1 and report.batch_remaining == 1
    assert report.index_file_id is None

    m1 = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m1.generation == 1 and m1.index is None  # 還沒有 index
    assert [r.snapshot_sha256 for r in m1.pending] == [s1]
    folder = drive._files[MANIFEST_ID].parents[0]
    # pending 也在可信集合內，否則下一輪的清掃會把它隔離
    assert trusted_ids(m1, MANIFEST_ID) == frozenset({MANIFEST_ID, m1.pending[0].file_id})

    report2 = pub.publish(store, agora_main_sha="main-1")
    assert report2.readings_created == 1 and report2.batch_remaining == 0
    m2 = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m2.index is not None and m2.pending == ()
    # 第一輪上傳的那份被沿用（沒有第二個同名的 reading 檔案）
    assert m1.pending[0].file_id in m2.files
    assert len(drive.find_by_name(folder, reading_name("opencode:ses_1", s1))) == 1
    assert len(m2.files) == 3  # index + 兩份 reading


def test_dry_run_performs_no_write(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    pub = _publisher(drive, tmp_path, clock=clock)

    report = pub.publish(store, agora_main_sha="main-1", dry_run=True)
    assert report.status == "planned" and report.dry_run
    assert _write_ops(drive) == []
    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    assert m.is_initial


def test_reading_is_content_addressed_and_never_rewritten(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    sha = _add_session(store, "opencode:ses_1", ("甲", "乙", "丙"))
    pub = _publisher(drive, tmp_path, clock=clock)

    pub.publish(store, agora_main_sha="main-1")
    folder = drive._files[MANIFEST_ID].parents[0]
    reading = drive.find_by_name(folder, reading_name("opencode:ses_1", sha))[0]
    payload = drive.download_bytes(reading.id, max_bytes=1 << 20)
    body = json.loads(payload.decode("utf-8"))
    assert body["snapshot_sha256"] == sha
    assert hashlib.sha256(payload).hexdigest() == reading.sha256
    assert reading.size == len(payload)

    # 同一個快照再發一次：不會產生第二份檔案
    clock.set_time(T1)
    _add_session(store, "opencode:ses_2", ("丁",), snapshot_at=T1)
    pub.publish(store, agora_main_sha="main-2")
    assert len(drive.find_by_name(folder, reading_name("opencode:ses_1", sha))) == 1


def test_index_carries_catalog_for_reader(tmp_path: Path):
    """索引同時是目錄：Session metadata、快照歷史、Link、交接單、拒收原因都在裡面。"""
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    old = _add_session(store, "opencode:ses_1", ("甲",), title="甲的 Session")
    latest = _add_session(
        store, "opencode:ses_1", ("甲", "乙"), snapshot_at=T1, title="甲的 Session"
    )
    _add_session(store, "opencode:ses_2", ("丙",))
    handoff_id = _handoff(
        store, "opencode:ses_1", old, author_session_id="opencode:ses_2"
    )
    ulid = generate_ulid()
    store.put_json(
        layout.continuation_link_path("opencode:ses_2", ulid),
        {
            "from": "opencode:ses_2",
            "to": "opencode:ses_1",
            "continuation": {"snapshot_sha256": old, "message_id": "m0"},
            "handoff_id": f"handoff:{ulid}",
            "claim_id": f"claim:{ulid}",
        },
    )
    pub = _publisher(drive, tmp_path, clock=clock)

    report = pub.publish(
        store,
        agora_main_sha="main-1",
        run_rejections=[RejectionRow(item_key=generate_ulid(), code="orphan", at=T0)],
    )
    assert report.index_file_count == 4  # index ＋ 3 readings（含被釘住的那一份）

    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    index_path = tmp_path / "check.sqlite"
    drive.download(m.index.id, index_path, max_bytes=1 << 20)
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        sessions = con.execute(
            "SELECT session_id, title, status FROM sessions ORDER BY session_id"
        ).fetchall()
        readings = con.execute(
            "SELECT session_id, snapshot_sha256, is_latest FROM readings"
            " ORDER BY session_id, snapshot_sha256"
        ).fetchall()
        snaps = con.execute(
            "SELECT session_id, snapshot_sha256 FROM snapshots ORDER BY session_id,"
            " snapshot_sha256"
        ).fetchall()
        links = con.execute(
            "SELECT kind, from_session_id, to_session_id FROM links"
        ).fetchall()
        handoffs = con.execute(
            "SELECT handoff_id, target_session_id, snapshot_sha256, author_session_id"
            " FROM handoffs"
        ).fetchall()
        rejections = con.execute(
            "SELECT item_key, code, authenticated FROM rejections"
        ).fetchall()
    finally:
        con.close()

    assert meta["format"] == SEARCH_INDEX_FORMAT
    assert meta["agora_main_sha"] == "main-1"
    assert meta["generation"] == "1"
    assert sessions == [
        ("opencode:ses_1", "甲的 Session", "stopped"),
        ("opencode:ses_2", None, "stopped"),
    ]
    # 被釘住的舊快照也在 readings 裡（讀者要能從接續點讀回那一版）
    assert [(r[0], r[2]) for r in readings] == [
        ("opencode:ses_1", 0), ("opencode:ses_1", 1), ("opencode:ses_2", 1)
    ]
    assert {s[1] for s in snaps} >= {old, latest}
    assert len(snaps) == 3
    assert links == [("continuation", "opencode:ses_2", "opencode:ses_1")]
    assert handoffs == [(handoff_id, "opencode:ses_1", old, "opencode:ses_2")]
    assert rejections and rejections[0][1] == "orphan" and rejections[0][2] == 0


def test_handoff_author_session_id_is_the_target_session(tmp_path: Path):
    """PM 決定：作者由提交流程填成 target_session_id（持有者檢查已保證）。

    寫入端若明確提供 author_session_id（body 或 metadata）則以它為準；
    目標與明確作者都缺才是 NULL（讀取端據此排除）。
    """
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    sha = _add_session(store, "opencode:ses_1", ("甲",))
    sub = _add_session(store, "opencode:ses_1", ("甲", "乙"), parent_id="opencode:ses_1")
    h1 = _handoff(store, "opencode:ses_1", sha)                                  # 預設＝target
    h2 = _handoff(store, "opencode:ses_1", sub,
                  author_session_id="opencode:ses_other")                     # 明確作者
    h3 = _handoff(store, "opencode:ses_1", sub)                                # 明確作者無效格式
    pub = _publisher(drive, tmp_path, clock=clock)
    pub.publish(store, agora_main_sha="main-1")

    m = parse_manifest(drive.download_bytes(MANIFEST_ID, max_bytes=1 << 20))
    index_path = tmp_path / "check.sqlite"
    drive.download(m.index.id, index_path, max_bytes=1 << 20)
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        rows = dict(
            (r[0], r[1])
            for r in con.execute("SELECT handoff_id, author_session_id FROM handoffs")
        )
    finally:
        con.close()
    assert rows == {
        h1: "opencode:ses_1",   # 沒有明確作者 → 填 target（PM 決定）
        h2: "opencode:ses_other",  # 明確作者優先
        h3: "opencode:ses_1",   # 明確作者不合法 → 回到 target
    }


def test_handoff_pins_older_snapshot_reading(tmp_path: Path):
    """交接單釘住的快照要真的發佈一份 reading（不能只發最新）。"""
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    store = _store(tmp_path)
    old = _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_1", ("甲", "乙"), snapshot_at=T1)
    _handoff(store, "opencode:ses_1", old)
    pub = _publisher(drive, tmp_path, clock=clock)

    report = pub.publish(store, agora_main_sha="main-1")
    assert report.readings_created == 2
    folder = drive._files[MANIFEST_ID].parents[0]
    assert len(drive.find_by_name(folder, reading_name("opencode:ses_1", old))) == 1


def test_load_manifest_helpers(tmp_path: Path):
    clock = FixedClock(T0)
    drive = _drive(tmp_path, clock=clock)
    assert load_manifest(drive, MANIFEST_ID).is_initial
    assert load_manifest_or_none(drive, MANIFEST_ID) is None  # 尚未發佈過
    assert load_manifest_or_none(drive, None) is None
    assert load_manifest_or_none(drive, "no-such-id") is None

    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    _publisher(drive, tmp_path, clock=clock).publish(store, agora_main_sha="main-1")
    assert load_manifest_or_none(drive, MANIFEST_ID).generation == 1
