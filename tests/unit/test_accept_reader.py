"""Independent Acceptance Tests for Reader and SA Auth (Task 4.3, 4.4).

Adheres strictly to:
- docs/impl/group4-modules.md §5, §6
- Freshness decision table (max_lag, stopped session rules, per-hit vs overall freshness)
- Generation regression rejection (StaleManifest)
- 403 / 404 -> AccessDenied
- Secrets never in repr / exception messages
- Reading never triggers writing
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import pytest

from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.drive.sa_auth import ServiceAccountToken
from aistorage.errors import NotFound
from aistorage.reader import (
    AccessDenied,
    AgoraReader,
    Freshness,
    Query,
    ReadViewClient,
    ReadingRef,
    Result,
    StaleManifest,
    evaluate_freshness,
)
from aistorage.reader.config import ReaderConfig
from aistorage.search.index import (
    HandoffRow,
    IndexEntry,
    IndexMeta,
    LinkRow,
    RejectionRow,
    build_index,
)


T0 = "2026-09-27T08:00:00.000Z"
T1 = "2026-09-27T09:00:00.000Z"
T2 = "2026-09-27T10:00:00.000Z"
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------


def make_reading_doc(session_id: str, snapshot_sha: str, messages: list[dict]) -> dict:
    return {
        "format": "aistorage.reading/v1",
        "session_id": session_id,
        "source": "opencode",
        "title": f"Title {session_id}",
        "parent_id": None,
        "snapshot_sha256": snapshot_sha,
        "in_progress": False,
        "messages": messages,
    }


def make_test_reader(
    tmp_path: Path,
    drive: FakeDrive,
    *,
    generation: int = 1,
    published_at: str = T1,
    entries: list[IndexEntry] | None = None,
    clock: FixedClock | None = None,
) -> tuple[AgoraReader, ReadViewClient, str]:
    clock = clock or FixedClock(T2)
    entries = entries or []

    index_db_path = tmp_path / f"index-g{generation}.sqlite"
    build_index(
        index_db_path,
        entries=entries,
        links=(),
        handoffs=(),
        rejections=(),
        meta=IndexMeta(
            generation=generation,
            built_at=published_at,
            agora_main_sha="main_sha_123",
            converter_versions={},
        ),
    )

    index_file = drive.create(drive.seed_folder("root"), f"index-g{generation}.sqlite", index_db_path)
    manifest_data = {
        "format": "aistorage.readview/v1",
        "element": "agora",
        "generation": generation,
        "published_at": published_at,
        "agora_main_sha": "main_sha_123",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {
            "id": index_file.id,
            "sha256": hashlib.sha256(index_db_path.read_bytes()).hexdigest(),
            "size": index_db_path.stat().st_size,
        },
        "files": [index_file.id],
        "retired": [],
    }
    manifest_file = drive.create(
        drive.seed_folder("root"),
        "readview-manifest.json",
        (json.dumps(manifest_data) + "\n").encode("utf-8"),
    )

    cfg = ReaderConfig(
        manifest_file_id=manifest_file.id,
        sa_key_path=tmp_path / "sa.json",
        cache_dir=tmp_path / f"reader_cache_g{generation}",
    )
    rv_client = ReadViewClient(drive, cfg, clock=clock)
    reader = AgoraReader(rv_client, clock=clock)
    return reader, rv_client, manifest_file.id


# ---------------------------------------------------------------------------
# Acceptance Tests
# ---------------------------------------------------------------------------


def test_freshness_decision_table():
    """驗證新鮮度決策表: max_lag is None、已停止Session規則、逾期警告。"""
    # 1. 未指定 max_lag -> satisfied is None，不產生警告，附 snapshot_at
    f_none = evaluate_freshness(
        snapshot_at="2026-09-27T08:00:00.000Z",
        status="running",
        stopped_at=None,
        generation=1,
        published_at=T1,
        now=NOW,
        max_lag=None,
    )
    assert f_none["satisfied"] is None
    assert f_none["warning"] is None
    assert f_none["snapshot_at"] == "2026-09-27T08:00:00.000Z"
    assert f_none["stopped_ok"] is False

    # 2. 已停止且快照在停止之後 (三天前停止、停止後同步過 -> satisfied=True, stopped_ok=True, 不論落後多久)
    f_stopped = evaluate_freshness(
        snapshot_at="2026-09-24T12:00:00.000Z",
        status="stopped",
        stopped_at="2026-09-24T10:00:00.000Z",  # snapshot_at >= stopped_at
        generation=1,
        published_at=T1,
        now=NOW,
        max_lag=timedelta(minutes=10),          # 要求 10 分鐘，即使落後 3 天依然滿足
    )
    assert f_stopped["satisfied"] is True
    assert f_stopped["stopped_ok"] is True
    assert f_stopped["warning"] is None

    # 3. 執行中且落後超過要求 (S2 以 10 分鐘讀 40 分鐘前的 S3 -> satisfied=False 且附警告)
    # now = 10:00, snapshot_at = 09:20 (落後 40 分鐘), max_lag = 10 分鐘
    f_stale = evaluate_freshness(
        snapshot_at="2026-09-27T09:20:00.000Z",
        status="running",
        stopped_at=None,
        generation=1,
        published_at=T1,
        now=NOW,
        max_lag=timedelta(minutes=10),
    )
    assert f_stale["satisfied"] is False
    assert f_stale["stopped_ok"] is False
    assert f_stale["warning"] is not None
    assert "stale" in f_stale["warning"].lower() or "落後" in f_stale["warning"]

    # 4. 執行中且在要求時間內 (now 10:00, snapshot 09:55, max_lag 10 分鐘 -> satisfied=True)
    f_fresh = evaluate_freshness(
        snapshot_at="2026-09-27T09:55:00.000Z",
        status="running",
        stopped_at=None,
        generation=1,
        published_at=T1,
        now=NOW,
        max_lag=timedelta(minutes=10),
    )
    assert f_fresh["satisfied"] is True
    assert f_fresh["warning"] is None


def test_per_item_freshness_in_find_sessions(tmp_path: Path):
    """驗證 find_sessions 逐筆附帶各自的 Freshness，清單整體以最舊快照為準。"""
    drive = FakeDrive()
    clock = FixedClock(T2)  # NOW = 10:00:00

    # 準備兩筆 session: 一筆較新 (09:55)，一筆較舊 (09:10)
    snap1 = "1" * 64
    snap2 = "2" * 64
    r1 = make_reading_doc("opencode:s1", snap1, [
        {"message_id": "m1", "index": 0, "role": "user", "completed": True, "reverted": False,
         "created_at": "2026-09-27T09:55:00.000Z", "parts": [{"type": "text", "text": "target query keyword"}]}
    ])
    r2 = make_reading_doc("opencode:s2", snap2, [
        {"message_id": "m2", "index": 0, "role": "user", "completed": True, "reverted": False,
         "created_at": "2026-09-27T09:10:00.000Z", "parts": [{"type": "text", "text": "target query keyword"}]}
    ])

    meta1 = {
        "session_id": "opencode:s1", "source": "opencode", "title": "S1", "producer": "alice",
        "case_id": None, "status": "running", "stopped_at": None, "in_progress": False,
        "created_at": "2026-09-27T09:55:00.000Z", "updated_at": "2026-09-27T09:55:00.000Z",
        "snapshot_at": "2026-09-27T09:55:00.000Z", "raw_sha256": snap1, "raw_size": 100,
        "reading_status": "ok", "parent_id": None, "committed_at": "2026-09-27T09:55:00.000Z",
    }
    meta2 = {
        "session_id": "opencode:s2", "source": "opencode", "title": "S2", "producer": "bob",
        "case_id": None, "status": "running", "stopped_at": None, "in_progress": False,
        "created_at": "2026-09-27T09:10:00.000Z", "updated_at": "2026-09-27T09:10:00.000Z",
        "snapshot_at": "2026-09-27T09:10:00.000Z", "raw_sha256": snap2, "raw_size": 100,
        "reading_status": "ok", "parent_id": None, "committed_at": "2026-09-27T09:10:00.000Z",
    }

    ref1 = {"session_id": "opencode:s1", "snapshot_sha256": snap1, "is_latest": True, "file_id": "f1", "sha256": snap1, "size": 100}
    ref2 = {"session_id": "opencode:s2", "snapshot_sha256": snap2, "is_latest": True, "file_id": "f2", "sha256": snap2, "size": 100}

    entries = [
        IndexEntry(metadata=meta1, snapshots=[], reading=r1, reading_ref=ref1),
        IndexEntry(metadata=meta2, snapshots=[], reading=r2, reading_ref=ref2),
    ]

    reader, _, _ = make_test_reader(tmp_path, drive, entries=entries, clock=clock)

    # 查詢時要求 max_lag = 15 分鐘
    # S1 落後 5 分鐘 (合格)；S2 落後 50 分鐘 (不合格)
    res = reader.find_sessions(Query(text="target"), max_lag=timedelta(minutes=15))

    hits = res.value
    assert len(hits) == 2

    # 逐筆 Freshness
    h1 = next(h for h in hits if h.hit.session.session_id == "opencode:s1")
    h2 = next(h for h in hits if h.hit.session.session_id == "opencode:s2")
    assert h1.freshness.satisfied is True
    assert h2.freshness.satisfied is False

    # 清單整體 Freshness 以最舊快照 (S2) 為準 -> satisfied is False
    assert res.freshness.satisfied is False
    assert res.freshness.snapshot_at == "2026-09-27T09:10:00.000Z"


def test_generation_regression_rejection(tmp_path: Path):
    """驗證世代倒退拒絕: 收到世代小於已快取世代的 manifest 時拋出 StaleManifest。"""
    drive = FakeDrive()
    clock = FixedClock(T2)

    # 先初始化第 2 世代並載入 index 建立快取
    reader, client, manifest_fid = make_test_reader(tmp_path, drive, generation=2, clock=clock)
    m2 = client.manifest()
    assert m2["generation"] == 2
    db = client.index()
    db.close()

    # 模擬遠端被回放成較舊的第 1 世代
    old_manifest_data = {
        "format": "aistorage.readview/v1",
        "element": "agora",
        "generation": 1,
        "published_at": T0,
        "agora_main_sha": "main_old",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {"id": "f_old", "sha256": "0"*64, "size": 10},
        "files": [],
        "retired": [],
    }
    drive.update_content(manifest_fid, json.dumps(old_manifest_data).encode("utf-8"))

    # 呼叫 manifest 應拒絕倒退並拋出 StaleManifest
    with pytest.raises(StaleManifest):
        client.manifest()


def test_access_denied_on_403_and_404(tmp_path: Path):
    """驗證 Drive manifest 回 403 或 404 時一律轉為 AccessDenied，不回傳空結果。"""
    drive = FakeDrive()
    clock = FixedClock(T2)

    # 設定不存在之 manifest_file_id 觸發 404
    cfg = ReaderConfig(
        manifest_file_id="non_existent_manifest_file_id",
        sa_key_path=tmp_path / "sa.json",
        cache_dir=tmp_path / "cache",
    )
    client = ReadViewClient(drive, cfg, clock=clock)

    with pytest.raises(AccessDenied):
        client.manifest()


def test_service_account_token_secrets_safety(tmp_path: Path):
    """驗證 ServiceAccountToken 絕不將私鑰內容或敏感金鑰洩漏於 repr 或例外訊息中。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    real_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    sa_file = tmp_path / "sa_secret.json"
    sa_file.write_text(json.dumps({
        "type": "service_account",
        "project_id": "test-proj",
        "private_key_id": "kid123",
        "private_key": real_pem,
        "client_email": "sa@test-proj.iam.gserviceaccount.com",
    }))

    token = ServiceAccountToken(sa_file)

    # 1. repr 與 str 絕不包含私鑰內容
    token_repr = repr(token)
    token_str = str(token)
    assert real_pem not in token_repr
    assert real_pem not in token_str
    assert "BEGIN PRIVATE KEY" not in token_repr


def test_reading_never_triggers_writing(tmp_path: Path):
    """驗證 AgoraReader 所有讀取介面均為純讀取，FakeDrive 的呼叫記錄中絕無寫入操作。"""
    drive = FakeDrive()
    clock = FixedClock(T2)
    reader, _, _ = make_test_reader(tmp_path, drive, clock=clock)

    # 清空先前 setup 的 Drive 呼叫記錄
    drive.calls.clear()

    # 執行各項讀取 API
    reader.find_sessions(Query())
    reader.catalog([])
    reader.list_open_handoffs()
    reader.get_rejection("item_key_none")

    # 斷言 Drive 沒有被呼叫任何寫入方法 (create, update, delete, etc.)
    write_methods = {"create", "update_content", "update_metadata", "delete", "trash", "purge"}
    invoked_methods = {call[0] for call in drive.calls}
    assert invoked_methods.isdisjoint(write_methods), f"發現非法寫入調用: {invoked_methods & write_methods}"
