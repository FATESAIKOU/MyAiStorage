"""讀取視圖重建與驗證（tasks 4.5）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

驗證的是「讀取視圖可以從真本重建，且重建結果與已發佈的一致」。
範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import FixedClock
from aistorage.committer.rebuild import (
    RebuildResult,
    clone_true_copy,
    compare_with_published,
    format_diff,
    open_store,
    rebuild_local,
)
from aistorage.drive.fake import FakeDrive
from aistorage.publish.publisher import DriveReadViewPublisher
from aistorage.readview.model import initial_manifest, parse_manifest, serialize_manifest
from aistorage.readview.naming import reading_name
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.schema import generate_ulid

T0 = "2026-09-27T08:00:00.000Z"
T1 = "2026-09-27T09:00:00.000Z"
PRODUCER = "profile:mac-opencode"
MANIFEST_ID = "manifest-file-0001"


class FakeConverter:
    """最簡閱讀版轉換器（自編測試資料）。"""

    source = "opencode"
    version = "1"

    def __init__(self, *, fail_for: tuple[str, ...] = ()) -> None:
        self.fail_for = fail_for

    def convert(self, raw_path: Path, *, session_id: str, parent_id=None,
                snapshot_sha256: str | None = None) -> dict:
        if session_id in self.fail_for:
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


def _store(tmp_path: Path) -> AgoraStore:
    return AgoraStore(
        worktree=tmp_path / "wt",
        raw_storage=FakeRawStorage(),
        git=None,
        temp_dir=tmp_path / "store_tmp",
    )


def _add_session(store: AgoraStore, session_id: str, texts: tuple[str, ...],
                 *, snapshot_at: str = T0, title: str | None = None) -> str:
    raw = json.dumps({"texts": list(texts), "title": title}, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    raw_path = store._temp_dir / f"seed-{sha}.raw"
    raw_path.write_bytes(raw)
    store.put_session(
        SessionRecord(
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
        ),
        raw_path,
    )
    return sha


def _publish(store: AgoraStore, *, drive: FakeDrive, folder: str, manifest_id: str,
             clock: FixedClock, agora_main_sha: str, workdir: Path) -> None:
    DriveReadViewPublisher(
        drive,
        folder_id=folder,
        manifest_file_id=manifest_id,
        converters={"opencode": FakeConverter()},
        clock=clock,
        workdir=workdir,
    ).publish(store, agora_main_sha=agora_main_sha)


def _setup(tmp_path: Path) -> tuple[AgoraStore, ReadViewClient, FakeDrive, str]:
    """建好真本、讀取視圖（用 FakeDrive 發佈）與讀取端用戶端。"""
    clock = FixedClock(T0)
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",), title="甲的 Session")
    _add_session(store, "opencode:ses_2", ("乙",))

    drive = FakeDrive(clock=clock)
    folder = drive.seed_folder("readview")
    drive.seed_file(
        folder,
        "readview-manifest.json",
        serialize_manifest(initial_manifest(published_at=T0)),
        file_id=MANIFEST_ID,
    )
    _publish(store, drive=drive, folder=folder, manifest_id=MANIFEST_ID, clock=clock,
             agora_main_sha="main-1", workdir=tmp_path / "pub-work")

    cfg = ReaderConfig(
        manifest_file_id=MANIFEST_ID,
        sa_key_path=tmp_path / "unused-sa.json",
        cache_dir=tmp_path / "cache",
    )
    client = ReadViewClient(drive, cfg, clock=clock)
    return store, client, drive, folder


def test_rebuild_local_produces_readings_and_index(tmp_path: Path):
    store, _client, _drive, _folder = _setup(tmp_path)
    result = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out")

    assert result.sessions == 2 and result.readings == 2 and result.failures == ()
    assert result.index_path.is_file()
    assert {p.name for p in (tmp_path / "out" / "readings").iterdir()} == {
        reading_name("opencode:ses_1", result.entries[0].snapshot_sha256),
        reading_name("opencode:ses_2", result.entries[1].snapshot_sha256),
    }
    # 每份 reading 的 sha256 都是檔案內容的雜湊（內容定址）
    for e in result.entries:
        assert hashlib.sha256(e.path.read_bytes()).hexdigest() == e.sha256
        assert e.size == e.path.stat().st_size
    assert "sessions" in result.index_tables and "readings" in result.index_tables


def test_rebuild_local_records_conversion_failures(tmp_path: Path):
    store, _client, _drive, _folder = _setup(tmp_path)
    result = rebuild_local(
        store, {"opencode": FakeConverter(fail_for=("opencode:ses_2",))}, tmp_path / "out"
    )
    assert result.readings == 1
    assert [sid for sid, _ in result.failures] == ["opencode:ses_2"]


def test_rebuild_matches_published_readview(tmp_path: Path):
    store, client, _drive, _folder = _setup(tmp_path)
    result = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out")

    diff = compare_with_published(result, client)
    assert diff.ok, format_diff(diff)
    assert diff.compared_readings == 2
    assert diff.counts() == {
        "compared_readings": 2,
        "missing_in_published": 0,
        "missing_locally": 0,
        "mismatched": 0,
        "body_failures": 0,
        "raw_failures": 0,
        "index_tables_differing": 0,
        "rebuild_failures": 0,
    }
    assert diff.published_generation == 1 and diff.agora_main_sha == "main-1"


def test_pinned_snapshot_is_also_rebuilt_and_compared(tmp_path: Path):
    """被交接單釘住的快照也要重建、也要比對。"""
    from aistorage.agora import layout

    clock = FixedClock(T0)
    store = _store(tmp_path)
    old = _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_1", ("甲", "乙"), snapshot_at=T1)
    ulid = generate_ulid()
    store.put_json(
        layout.handoff_path(ulid),
        {
            "id": f"handoff:{ulid}",
            "type": "handoff",
            "producer": PRODUCER,
            "created_at": T0,
            "updated_at": T0,
            "case_id": None,
            "provenance": None,
            "body": {
                "target_session_id": "opencode:ses_1",
                "continuation": {"snapshot_sha256": old, "message_id": "m0"},
                "content": "交接說明（自編測試內容）",
            },
            "claimed_by": None,
            "committed_at": T0,
        },
    )
    drive = FakeDrive(clock=clock)
    folder = drive.seed_folder("readview")
    drive.seed_file(
        folder, "readview-manifest.json",
        serialize_manifest(initial_manifest(published_at=T0)), file_id=MANIFEST_ID,
    )
    _publish(store, drive=drive, folder=folder, manifest_id=MANIFEST_ID, clock=clock,
             agora_main_sha="main-1", workdir=tmp_path / "pub-work")
    client = ReadViewClient(
        drive,
        ReaderConfig(MANIFEST_ID, tmp_path / "sa.json", tmp_path / "cache"),
        clock=clock,
    )

    result = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out")
    assert result.readings == 2
    assert {e.snapshot_sha256 for e in result.entries} == {old, result.entries[0].snapshot_sha256}
    diff = compare_with_published(result, client)
    assert diff.ok, format_diff(diff)


def test_verify_detects_a_drifted_reading(tmp_path: Path):
    """讀取視圖與真本不一致時要報出來（id，不含內容）。"""
    store, client, drive, folder = _setup(tmp_path)
    result = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out")
    assert compare_with_published(result, client).ok

    # 真本多了一個快照但沒發佈 → 本地重建有、讀取視圖沒有
    _add_session(store, "opencode:ses_1", ("甲", "丙"), snapshot_at=T1)
    drifted = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out2")
    diff = compare_with_published(drifted, client)
    assert not diff.ok
    assert [sid for sid, _ in diff.missing_in_published] == ["opencode:ses_1"]
    assert "RESULT=MISMATCH" in format_diff(diff)


def test_verify_detects_a_modified_index(tmp_path: Path):
    """索引內容被動過（改一個標題）也要報出來。"""
    store, client, _drive, _folder = _setup(tmp_path)
    result = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out")
    assert compare_with_published(result, client).ok

    tables = {k: list(v) for k, v in result.index_tables.items()}
    tables["sessions"] = [
        ("被改過的標題",) + tuple(row[1:]) for row in tables["sessions"]
    ]
    tampered = RebuildResult(
        out_dir=result.out_dir,
        index_path=result.index_path,
        index_sha256=result.index_sha256,
        index_tables=tables,
        entries=result.entries,
        failures=result.failures,
    )
    diff = compare_with_published(tampered, client)
    assert "sessions" in diff.index_tables_differing
    assert not diff.ok


def _annex_works() -> bool:
    try:
        proc = subprocess.run(["git", "annex", "version"], capture_output=True, check=False)
    except OSError:
        return False
    return proc.returncode == 0


@pytest.mark.skipif(not _annex_works(), reason="需要 git-annex")
def test_clone_true_copy_and_rebuild_from_it(tmp_path: Path):
    """從本機真本 clone 出來重建（驗證 CLI 的 clone 路徑；唯讀）。"""
    store, _client, _drive, _folder = _setup(tmp_path)

    src = tmp_path / "src-repo"
    shutil.copytree(store.worktree, src)
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
    }
    def _git(*args: str) -> None:
        proc = subprocess.run(["git", "-C", str(src), *args], env=env,
                              capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr.decode()

    _git("init", "-b", "main")
    _git("add", "-A")
    _git("commit", "-m", "seed")
    _git("annex", "init")

    clone_dir = clone_true_copy(str(src), tmp_path / "clone")
    cloned = open_store(clone_dir, temp_dir=tmp_path / "clone_tmp")
    assert sorted(p.name for p in cloned.worktree.joinpath("sessions").glob("opencode/*"))

    result = rebuild_local(cloned, {"opencode": FakeConverter()}, tmp_path / "out3")
    assert result.sessions == 2 and result.readings == 2
    # 與未 clone 的真本重建結果逐位元組相同
    direct = rebuild_local(store, {"opencode": FakeConverter()}, tmp_path / "out4")
    assert {e.snapshot_sha256 for e in result.entries} == {
        e.snapshot_sha256 for e in direct.entries
    }
    assert result.index_sha256 == direct.index_sha256
