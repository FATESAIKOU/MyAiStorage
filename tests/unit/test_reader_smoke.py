"""4.3／4.4 reader 的冒煙測試（實作方撰寫；驗收由測試方另寫）。

只用 FakeDrive（真 Drive 與 SA 憑證只在整合測試用）。
範例內容一律自編，不碰真實 Session。
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import MismatchError, NotFound, ReadError
from aistorage.reader import AgoraReader
from aistorage.reader.__main__ import main as reader_main
from aistorage.reader.client import AccessDenied, ReadViewClient, StaleManifest
from aistorage.reader.config import ReaderConfig
from aistorage.reader.freshness import evaluate_freshness
from aistorage.search.index import (
    HandoffRow,
    IndexEntry,
    IndexMeta,
    LinkRow,
    RejectionRow,
    build_index,
)
from aistorage.search.query import Query

NOW = "2026-09-27T09:10:00.000Z"
SNAP_A = "a" * 64
SNAP_B = "b" * 64
SNAP_C = "c" * 64


def _reading(sid: str, snap: str, messages: list) -> dict:
    return {
        "format": "aistorage.reading/v1",
        "session_id": sid,
        "source": "opencode",
        "title": f"標題 {sid}",
        "parent_id": None,
        "snapshot_sha256": snap,
        "in_progress": False,
        "messages": messages,
    }


def _msg(mid: str, idx: int, text: str, reasoning: str | None = None) -> dict:
    parts = [{"type": "text", "text": text}]
    if reasoning is not None:
        parts.append({"type": "reasoning", "text": reasoning})
    return {
        "message_id": mid, "index": idx, "role": "user" if idx % 2 == 0 else "assistant",
        "created_at": "2026-09-27T08:00:00Z", "completed": True, "reverted": False,
        "parts": parts,
    }


def _fixture(tmp_path: Path, drive_cls: type[FakeDrive] = FakeDrive
             ) -> tuple[FakeDrive, ReaderConfig, FixedClock]:
    """建 index＋reading 檔，餵進 drive（可傳監控子類），回傳 (drive, cfg, clock)。"""
    reading_b = _reading("opencode:s1", SNAP_B,
                         [_msg("m1a", 0, "接續點格式確定"), _msg("m1b", 1, "交接單處理", "思考過程")])
    reading_a = _reading("opencode:s1", SNAP_A, [_msg("m1a", 0, "接續點格式確定")])
    reading_c = _reading("opencode:s2", SNAP_C, [_msg("n1", 0, "hello world")])

    def _ref(fid: str, payload: dict) -> dict:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {"file_id": fid, "raw": raw, "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw)}

    rb, ra, rc = _ref("reading-s1b", reading_b), _ref("reading-s1a", reading_a), \
        _ref("reading-s2c", reading_c)
    entries = [
        IndexEntry(
            metadata={"session_id": "opencode:s1", "source": "opencode",
                      "title": "標題 opencode:s1", "producer": "profile:mac-opencode",
                      "case_id": "case-1", "status": "stopped",
                      "stopped_at": "2026-09-27T08:55:00.000Z", "in_progress": False,
                      "created_at": "2026-09-27T08:00:00Z",
                      "updated_at": "2026-09-27T09:00:00.000Z",
                      "snapshot_at": "2026-09-27T09:00:00.000Z",
                      "raw_sha256": SNAP_B, "raw_size": 10, "parent_id": None,
                      "reading_status": "ok", "reading_error_code": None,
                      "committed_at": "2026-09-27T09:01:00.000Z"},
            snapshots=[
                {"snapshot_sha256": SNAP_A, "snapshot_at": "2026-09-27T08:30:00.000Z",
                 "committed_at": "2026-09-27T08:31:00.000Z", "via": "sync",
                 "file_id": ra["file_id"], "sha256": ra["sha256"], "size": ra["size"]},
                {"snapshot_sha256": SNAP_B, "snapshot_at": "2026-09-27T09:00:00.000Z",
                 "committed_at": "2026-09-27T09:01:00.000Z", "via": "sync",
                 "file_id": rb["file_id"], "sha256": rb["sha256"], "size": rb["size"]},
            ],
            reading=reading_b,
            reading_ref={"snapshot_sha256": SNAP_B, "file_id": rb["file_id"],
                         "sha256": rb["sha256"], "size": rb["size"]},
        ),
        IndexEntry(
            metadata={"session_id": "opencode:s2", "source": "opencode",
                      "title": "標題 opencode:s2", "producer": "profile:mac-opencode",
                      "case_id": None, "status": "running", "stopped_at": None,
                      "in_progress": False, "created_at": "2026-09-26T08:00:00Z",
                      "updated_at": "2026-09-26T08:10:00.000Z",
                      "snapshot_at": "2026-09-26T08:10:00.000Z",
                      "raw_sha256": SNAP_C, "raw_size": 10, "parent_id": None,
                      "reading_status": "ok", "reading_error_code": None,
                      "committed_at": "2026-09-26T08:11:00.000Z"},
            snapshots=[
                {"snapshot_sha256": SNAP_C, "snapshot_at": "2026-09-26T08:10:00.000Z",
                 "committed_at": "2026-09-26T08:11:00.000Z", "via": "sync",
                 "file_id": rc["file_id"], "sha256": rc["sha256"], "size": rc["size"]},
            ],
            reading=reading_c,
            reading_ref={"snapshot_sha256": SNAP_C, "file_id": rc["file_id"],
                         "sha256": rc["sha256"], "size": rc["size"]},
        ),
        IndexEntry(
            metadata={"session_id": "opencode:s-sub", "source": "opencode",
                      "title": "子 Session", "producer": "profile:mac-opencode",
                      "case_id": None, "status": "running", "stopped_at": None,
                      "in_progress": False, "created_at": "2026-09-26T09:00:00Z",
                      "updated_at": "2026-09-26T09:00:00Z",
                      "snapshot_at": "2026-09-26T09:00:00Z",
                      "raw_sha256": SNAP_C, "raw_size": 10,
                      "parent_id": "opencode:s1",
                      "reading_status": "failed", "reading_error_code": "conversion_error",
                      "committed_at": "2026-09-26T09:01:00Z"},
            snapshots=[
                {"snapshot_sha256": SNAP_C, "snapshot_at": "2026-09-26T09:00:00Z",
                 "committed_at": "2026-09-26T09:01:00.000Z", "via": "sync"},
            ],
            reading=None,
            reading_ref=None,
        ),
        # `checkout` 預留好、還沒有人開工的新 Session（review-2bc0785 M2）：
        # 狀態是 reserved，帶期限；`agora find --status reserved` 要能把它挑出來。
        IndexEntry(
            metadata={"session_id": "opencode:s-reserved", "source": "opencode",
                      "title": "接手甲的工作", "producer": "profile:mac-opencode",
                      "case_id": None, "status": "reserved", "stopped_at": None,
                      "in_progress": False, "created_at": "2026-09-27T09:00:00Z",
                      "updated_at": "2026-09-27T09:00:00.000Z",
                      "snapshot_at": "2026-09-27T09:00:00.000Z",
                      "raw_sha256": SNAP_A, "raw_size": 10, "parent_id": None,
                      "reading_status": "ok", "reading_error_code": None,
                      "committed_at": "2026-09-27T09:00:00.000Z",
                      "reserved_until": "2026-09-29T09:00:00.000Z"},
            snapshots=[
                {"snapshot_sha256": SNAP_A, "snapshot_at": "2026-09-27T09:00:00.000Z",
                 "committed_at": "2026-09-27T09:00:00.000Z", "via": "import"},
            ],
            reading=_reading("opencode:s-reserved", SNAP_A, []),
            reading_ref={"snapshot_sha256": SNAP_A, "file_id": ra["file_id"],
                         "sha256": ra["sha256"], "size": ra["size"]},
        ),
    ]
    index_path = tmp_path / "index.sqlite3"
    build_index(
        index_path, entries=entries,
        links=[LinkRow(kind="continuation", from_session_id="opencode:s2",
                       to_session_id="opencode:s1", handoff_id="handoff:H1",
                       claim_id="claim:C1", snapshot_sha256=SNAP_A, message_id="m1a")],
        handoffs=[HandoffRow(
            handoff_id="handoff:H1", target_session_id="opencode:s1",
            snapshot_sha256=SNAP_A, message_id="m1a",
            producer="profile:mac-opencode",
            created_at="2026-09-27T08:35:00.000Z", updated_at="2026-09-27T08:35:00.000Z",
            case_id="case-1", body_json='{"content":"做"}',
            claimed_by_claim_id="claim:C1", claimed_by_session_id="opencode:s2",
            claimed_at="2026-09-27T08:45:00.000Z",
            author_session_id="opencode:s1"),
            HandoffRow(
                handoff_id="handoff:H2", target_session_id="opencode:s2",
                snapshot_sha256=SNAP_C, message_id="n1",
                producer="profile:mac-opencode",
                created_at="2026-09-26T08:20:00.000Z",
                updated_at="2026-09-26T08:20:00.000Z", body_json="{}",
                author_session_id="opencode:s2"),
            HandoffRow(
                handoff_id="handoff:H3", target_session_id="opencode:s2",
                snapshot_sha256=SNAP_C, message_id="n1",
                producer="profile:mac-opencode",
                created_at="2026-09-26T08:25:00.000Z",
                updated_at="2026-09-26T08:25:00.000Z", body_json="{}",
                author_session_id="opencode:s-sub"),
            HandoffRow(
                handoff_id="handoff:H4", target_session_id="opencode:s2",
                snapshot_sha256=SNAP_C, message_id="n1",
                producer="profile:mac-opencode",
                created_at="2026-09-26T08:26:00.000Z",
                updated_at="2026-09-26T08:26:00.000Z", body_json="{}",
                author_session_id="opencode:ghost")],
        rejections=[RejectionRow(item_key="K1", code="orphan",
                                 at="2026-09-27T07:00:00.000Z", authenticated=False)],
        meta=IndexMeta(generation=7, built_at="2026-09-27T09:05:00.000Z",
                       agora_main_sha="abc", converter_versions={"opencode": "1"}),
    )
    # 作者 Session（handoffs.author_session_id 由 schema 提供，直接建表后填值
    # 由 build_index 經 HandoffRow 寫入）。
    index_raw = index_path.read_bytes()
    manifest = {
        "format": "aistorage.readview/v1",
        "element": "agora",
        "generation": 7,
        "published_at": "2026-09-27T09:05:00.000Z",
        "agora_main_sha": "abc",
        "converter_versions": {"opencode": "1"},
        "index": {"id": "index-file-7",
                  "sha256": hashlib.sha256(index_raw).hexdigest(),
                  "size": len(index_raw)},
        "files": ["index-file-7"],
        "retired": [],
    }
    drive: FakeDrive = drive_cls()
    folder = drive.seed_folder("readview")
    drive.seed_file(folder, "manifest.json",
                    json.dumps(manifest).encode("utf-8"), file_id="manifest-1")
    drive.seed_file(folder, "index.sqlite3", index_raw, file_id="index-file-7")
    for r in (rb, ra, rc):
        drive.seed_file(folder, r["file_id"] + ".json", r["raw"], file_id=r["file_id"])
    cfg = ReaderConfig(manifest_file_id="manifest-1",
                       sa_key_path=tmp_path / "sa.json",
                       cache_dir=tmp_path / "cache")
    return drive, cfg, FixedClock(NOW)


class WriteWatchDrive(FakeDrive):
    """任何寫入都爆炸的 FakeDrive（斷言讀取不觸發寫入）。"""

    def create(self, *args, **kwargs):
        raise AssertionError("讀取觸發了寫入: create")

    def update_content(self, *args, **kwargs):
        raise AssertionError("讀取觸發了寫入: update_content")

    def move(self, *args, **kwargs):
        raise AssertionError("讀取觸發了寫入: move")

    def delete_permanently(self, *args, **kwargs):
        raise AssertionError("讀取觸發了寫入: delete_permanently")


def test_reader_happy_paths_are_read_only(tmp_path: Path):
    drive, cfg, clock = _fixture(tmp_path, WriteWatchDrive)
    reader = AgoraReader(ReadViewClient(drive, cfg, clock=clock), clock=clock)

    found = reader.find_sessions(Query(text="接續點"))
    assert [f.hit.session.session_id for f in found.value] == ["opencode:s1"]
    assert found.freshness.generation == 7
    # 逐筆 Freshness（PM 決定 1）：每筆都附快照時間
    assert found.value[0].freshness.snapshot_at == "2026-09-27T09:00:00.000Z"
    assert found.value[0].freshness.satisfied is None  # 未指定 max_lag
    assert found.freshness.snapshot_at == "2026-09-27T09:00:00.000Z"

    view = reader.get_session("opencode:s1").value
    assert view.session.status == "stopped"
    assert len(view.links_in) == 1 and view.links_out == ()
    assert [h.handoff_id for h in view.handoffs_targeting] == ["handoff:H1"]
    assert len(view.snapshots) == 2

    reading = reader.get_reading("opencode:s1").value
    assert [m["message_id"] for m in reading["messages"]] == ["m1a", "m1b"]
    assert all("reasoning" not in [p["type"] for p in m["parts"]]
               for m in reading["messages"])
    full = reader.get_reading("opencode:s1", include_reasoning=True).value
    assert any("reasoning" in [p["type"] for p in m["parts"]] for m in full["messages"])

    cont = reader.get_continuation("handoff:H1").value
    assert [m["message_id"] for m in cont.messages] == ["m1a"]
    assert cont.sibling_links == ()

    assert [h.handoff_id for h in reader.list_open_handoffs().value] == ["handoff:H2"]
    # H3（子 Session 寫的）、H4（作者不明）被排除
    assert [h.handoff_id for h in
            reader.list_open_handoffs(case_id="case-1").value] == []
    assert reader.get_rejection("K1").value.code == "orphan"
    assert reader.get_rejection("missing").value is None

    catalog = reader.catalog(["opencode:s1", "opencode:nope"]).value
    assert set(catalog) == {"opencode:s1"}
    assert catalog["opencode:s1"].raw_sha256 == SNAP_B

    assert reader.wait_for_snapshot(
        "opencode:s1", SNAP_B, timeout=timedelta(seconds=5)).value is True
    assert reader.wait_for_snapshot(
        "opencode:s1", "f" * 64, timeout=timedelta(seconds=0)).value is False


def test_reserved_sessions_are_separate_from_working_ones(tmp_path: Path):
    """M2：預留（還沒有人開工）與真正開工的 Session 在讀取介面分得開。

    `find --status reserved` 只挑出預留；預留帶期限，期限內不算「落後」，
    期限過了才警告（期 1 **不自動刪除**，那要人決定）。
    """
    drive, cfg, clock = _fixture(tmp_path)
    reader = AgoraReader(ReadViewClient(drive, cfg, clock=clock), clock=clock)

    reserved = reader.find_sessions(Query(status="reserved")).value
    assert [f.hit.session.session_id for f in reserved] == ["opencode:s-reserved"]
    row = reserved[0].hit.session
    assert row.status == "reserved"
    assert row.reserved_until == "2026-09-29T09:00:00.000Z"

    # 沒有期限的普通 session：欄位是 None（不影響其他判斷）
    working = reader.get_session("opencode:s1").value.session
    assert working.status == "stopped" and working.reserved_until is None

    # 期限內（now=09-27，期限 09-29）→ 符合，不警告
    ok = reader.get_session("opencode:s-reserved", max_lag=timedelta(minutes=1))
    assert ok.freshness.satisfied is True
    assert ok.freshness.warning is None

    # 期限過了 → 警告「預留已過期」（不是「落後」）
    later = AgoraReader(ReadViewClient(drive, cfg, clock=clock),
                        clock=FixedClock("2026-10-01T00:00:00.000Z"))
    expired = later.get_session("opencode:s-reserved", max_lag=timedelta(minutes=1))
    assert expired.freshness.satisfied is False
    assert "預留已過期" in (expired.freshness.warning or "")


def test_reserved_until_absent_in_older_index_generations(tmp_path: Path):
    """舊世代的 index 沒有 `reserved_until` 欄 → 讀取端當作 None，不報錯。

    欄位放在 sessions 表最後一個，舊 index 照樣能讀（`get_snapshot` 的 annex_key
    也是同樣的相容寫法）。
    """
    import sqlite3 as _sqlite3

    from aistorage.search.query import get_session_row

    index = tmp_path / "old.sqlite3"
    build_index(index, entries=[], meta=IndexMeta(
        generation=1, built_at="2026-09-27T09:00:00.000Z", agora_main_sha="x",
        converter_versions={}))
    con = _sqlite3.connect(str(index))
    try:
        con.execute("ALTER TABLE sessions DROP COLUMN reserved_until")
        con.execute(
            "INSERT INTO sessions (session_id, source, title, producer, case_id,"
            " status, stopped_at, in_progress, created_at, updated_at, snapshot_at,"
            " raw_sha256, raw_size, parent_id, reading_status, reading_error_code,"
            " committed_at) VALUES ('opencode:old','opencode','t','p',NULL,"
            " 'reserved',NULL,0,'2026-09-27T09:00:00Z','2026-09-27T09:00:00Z',"
            "'2026-09-27T09:00:00Z','a',1,NULL,'ok',NULL,'2026-09-27T09:00:00Z')")
        con.commit()
        cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
        assert "reserved_until" not in cols
        row = get_session_row(con, "opencode:old")
    finally:
        con.close()
    assert row is not None and row.status == "reserved"
    assert row.reserved_until is None


def test_freshness_warnings_and_stopped(tmp_path: Path):
    drive, cfg, clock = _fixture(tmp_path)
    reader = AgoraReader(ReadViewClient(drive, cfg, clock=clock), clock=clock)

    stale = reader.get_session("opencode:s2", max_lag=timedelta(minutes=10))
    assert stale.freshness.satisfied is False
    assert stale.freshness.warning is not None and "10m" in stale.freshness.warning

    stopped = reader.get_session("opencode:s1", max_lag=timedelta(minutes=1))
    assert stopped.freshness.satisfied is True
    assert stopped.freshness.stopped_ok is True

    none_lag = reader.get_session("opencode:s2")
    assert none_lag.freshness.satisfied is None
    assert none_lag.freshness.snapshot_at == "2026-09-26T08:10:00.000Z"


def test_freshness_decision_table():
    now = datetime(2026, 9, 27, 9, 10, tzinfo=timezone.utc)
    base = {"generation": 7, "published_at": "2026-09-27T09:05:00.000Z",
            "now": now}
    assert evaluate_freshness(snapshot_at="2026-09-27T09:00:00.000Z",
                              status="running", stopped_at=None,
                              max_lag=None, **base)["satisfied"] is None
    assert evaluate_freshness(snapshot_at="2026-09-27T09:00:00.000Z",
                              status="running", stopped_at=None,
                              max_lag=timedelta(minutes=30), **base)["satisfied"] is True
    old = evaluate_freshness(snapshot_at="2026-09-27T08:00:00.000Z",
                             status="running", stopped_at=None,
                             max_lag=timedelta(minutes=10), **base)
    assert old["satisfied"] is False and old["warning"].startswith("stale:")
    ok = evaluate_freshness(snapshot_at="2026-09-27T09:05:00.000Z",
                            status="stopped", stopped_at="2026-09-27T09:04:00.000Z",
                            max_lag=timedelta(seconds=1), **base)
    assert ok["satisfied"] is True and ok["stopped_ok"] is True
    # 停止中但快照在停止之前 → 照一般規則判（會過期）
    before = evaluate_freshness(snapshot_at="2026-09-27T09:00:00.000Z",
                                status="stopped", stopped_at="2026-09-27T09:04:00.000Z",
                                max_lag=timedelta(minutes=1), **base)
    assert before["satisfied"] is False and not before["stopped_ok"]


def test_access_denied_and_mismatch(tmp_path: Path):
    class DenyDrive(FakeDrive):
        def download_bytes(self, *args, **kwargs):
            raise NotFound("Drive 資源不存在 (HTTP 404): x")

    _, cfg, clock = _fixture(tmp_path)
    with pytest.raises(AccessDenied):
        ReadViewClient(DenyDrive(), cfg, clock=clock).manifest()

    class ForbiddenDrive(FakeDrive):
        def download_bytes(self, *args, **kwargs):
            raise ReadError("Drive 請求失敗 (HTTP 403): x")

    with pytest.raises(AccessDenied):
        ReadViewClient(ForbiddenDrive(), cfg, clock=clock).manifest()

    drive, cfg2, clock2 = _fixture(tmp_path)
    tampered = drive.seed_folder("evil")
    drive.seed_file(tampered, "bad.sqlite3", b"not a database", file_id="bad-index")
    manifest = json.loads(drive.download_bytes("manifest-1", max_bytes=1 << 20))
    manifest["index"] = {"id": "bad-index", "sha256": "0" * 64, "size": 14}
    drive.seed_file(tampered, "m.json", json.dumps(manifest).encode(),
                    file_id="manifest-bad")
    cfg_bad = ReaderConfig(manifest_file_id="manifest-bad",
                           sa_key_path=tmp_path / "sa.json",
                           cache_dir=tmp_path / "cache-bad")
    with pytest.raises(MismatchError):
        ReadViewClient(drive, cfg_bad, clock=clock2).index()


def test_stale_manifest_rejected(tmp_path: Path):
    drive, cfg, clock = _fixture(tmp_path)
    client = ReadViewClient(drive, cfg, clock=clock)
    client.index()  # 世代 7 進快取
    manifest = json.loads(drive.download_bytes("manifest-1", max_bytes=1 << 20))
    manifest["generation"] = 3
    drive.update_content("manifest-1", json.dumps(manifest).encode())
    with pytest.raises(StaleManifest):
        client.manifest()


def test_open_handoffs_requires_author_column(tmp_path: Path):
    """PM 決定 2：index 沒有 author_session_id 欄位時 raise，不默默不篩。

    以 DROP COLUMN 模擬舊世代 index（SQLite 3.35+ 支援）。
    """
    import sqlite3 as _sqlite3
    from aistorage.search.index import build_index as _build
    p = tmp_path / "idx.sqlite3"
    _build(p, entries=[], meta=IndexMeta(
        generation=1, built_at="2026-09-27T09:00:00.000Z", agora_main_sha="x",
        converter_versions={}))
    _con = _sqlite3.connect(str(p))
    try:
        _con.execute("ALTER TABLE handoffs DROP COLUMN author_session_id")
        _con.commit()
    finally:
        _con.close()
    drive = FakeDrive()
    folder = drive.seed_folder("readview")
    raw = p.read_bytes()
    drive.seed_file(folder, "manifest.json", json.dumps({
        "format": "aistorage.readview/v1", "element": "agora", "generation": 1,
        "published_at": "2026-09-27T09:00:00.000Z", "agora_main_sha": "x",
        "converter_versions": {},
        "index": {"id": "idx", "sha256": hashlib.sha256(raw).hexdigest(),
                  "size": len(raw)},
        "files": ["idx"], "retired": [],
    }).encode(), file_id="manifest-1")
    drive.seed_file(folder, "index.sqlite3", raw, file_id="idx")
    cols = [r[1] for r in _sqlite3.connect(str(p)).execute("PRAGMA table_info(handoffs)")]
    assert "author_session_id" not in cols
    cfg = ReaderConfig(manifest_file_id="manifest-1",
                       sa_key_path=tmp_path / "sa.json",
                       cache_dir=tmp_path / "cache")
    reader = AgoraReader(ReadViewClient(drive, cfg, clock=FixedClock(NOW)),
                         clock=FixedClock(NOW))
    with pytest.raises(MismatchError):
        reader.list_open_handoffs()


def test_sa_auth_jwt_and_caching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from aistorage.drive.sa_auth import ServiceAccountToken
    import base64

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("utf-8")
    key_path = tmp_path / "sa.json"
    key_path.write_text(json.dumps({
        "client_email": "reader@test.iam.gserviceaccount.com",
        "private_key": pem,
    }))
    sa = ServiceAccountToken(key_path)
    assert "reader@test" in repr(sa) and pem not in repr(sa)

    assertion = sa.build_assertion(now=1700000000.0)
    header_b64, claims_b64, sig_b64 = assertion.split(".")
    claims = json.loads(base64.urlsafe_b64decode(claims_b64 + "=="))
    assert claims["iss"] == "reader@test.iam.gserviceaccount.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/drive"
    public = key.public_key()
    public.verify(
        base64.urlsafe_b64decode(sig_b64 + "=="),
        f"{header_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(), hashes.SHA256())

    calls: list[str] = []
    sa._request_token = lambda assertion: (calls.append(assertion) or "tok", 3600)
    assert sa.access_token() == "tok"
    assert sa.access_token() == "tok" and len(calls) == 1
    sa.invalidate()
    assert sa.access_token() == "tok" and len(calls) == 2

    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[gdrive]\nservice_account_file = {key_path}\n")
    assert isinstance(ServiceAccountToken.from_rclone_conf(conf), ServiceAccountToken)


def test_reader_config_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "reader.json"
    cfg_path.write_text(json.dumps({
        "manifest_file_id": "manifest-1",
        "sa_key_path": "/secrets/sa.json",
    }))
    cfg = ReaderConfig.load(cfg_path)
    assert cfg.manifest_file_id == "manifest-1"
    assert str(cfg.sa_key_path) == "/secrets/sa.json"
    assert str(cfg.cache_dir).endswith(".cache/aistorage/reader")

    monkeypatch.setenv("AISTORAGE_READER_CONFIG", str(cfg_path))
    monkeypatch.setenv("AISTORAGE_SA_KEY", "/tmp/other.json")
    cfg2 = ReaderConfig.load()
    assert str(cfg2.sa_key_path) == "/tmp/other.json"

    with pytest.raises(FileNotFoundError):
        ReaderConfig.load(tmp_path / "missing.json")


def test_cli_find_json_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture):
    drive, cfg, clock = _fixture(tmp_path)
    cfg_path = tmp_path / "reader.json"
    cfg_path.write_text(json.dumps({
        "manifest_file_id": "manifest-1",
        "sa_key_path": str(tmp_path / "sa.json"),
        "cache_dir": str(tmp_path / "cache"),
    }))
    factory = lambda _cfg: drive  # noqa: E731

    rc = reader_main(["--config", str(cfg_path), "find",
                      "--text-query", "接續點"], drive_factory=factory, clock=clock)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [h["hit"]["session"]["session_id"] for h in out["value"]] == ["opencode:s1"]
    assert out["value"][0]["freshness"]["snapshot_at"] == "2026-09-27T09:00:00.000Z"
    assert out["freshness"]["generation"] == 7

    rc = reader_main(["--config", str(cfg_path), "find",
                      "--cursor", "badcursor"], drive_factory=factory, clock=clock)
    assert rc == 5

    rc = reader_main(["--config", str(cfg_path), "rejection", "K1"],
                     drive_factory=factory, clock=clock)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["value"]["code"] == "orphan"

    rc = reader_main(["--config", str(cfg_path), "wait", "opencode:s1",
                      "--raw-sha256", SNAP_B, "--timeout", "5s"],
                     drive_factory=factory, clock=clock)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["value"] is True


def test_initial_manifest_reads_as_an_empty_view(tmp_path: Path):
    """generation 0（index=None）是合法的初始狀態，讀取端要視為空視圖。

    9.1 e2e 才發現：原本這裡丟 MismatchError，於是 `aistorage_split` 在
    「等待可見」的迴圈第一輪就爆掉——而那時提交流程根本還沒跑。
    """
    from aistorage.drive.fake import FakeDrive
    from aistorage.readview.model import initial_manifest, serialize_manifest
    from aistorage.reader.client import ReadViewClient
    from aistorage.reader.config import ReaderConfig
    from aistorage.search.query import Query

    drive = FakeDrive()
    folder = drive.seed_folder("readview")
    payload = serialize_manifest(
        initial_manifest(element="agora", published_at="2026-09-28T00:00:00Z")
    )
    drive.seed_file(folder, "manifest.json", payload, file_id="manifest-init")
    cfg = ReaderConfig(manifest_file_id="manifest-init", sa_key_path=tmp_path / "sa.json",
                       cache_dir=tmp_path / "cache")
    client = ReadViewClient(drive, cfg, clock=FixedClock("2026-09-28T01:00:00Z"))

    manifest = client.manifest()
    assert manifest["generation"] == 0
    assert manifest["is_initial"] is True
    assert manifest["index"] == {}
    # 初始視圖：空。AgoraReader 的高階介面也要能正常回答「沒有東西」
    reader = AgoraReader(client, clock=FixedClock("2026-09-28T01:00:00Z"))
    assert reader.catalog(["opencode:s1"]).value == {}
    assert reader.get_rejection("k1").value is None
    assert reader.find_sessions(Query(text="任何")).value == []
    with pytest.raises(KeyError):
        reader.get_session("opencode:s1")
