"""Foundry 讀取介面 7.4 補充測試。

- find 依 annex_key 篩選（任務：能依 annex key 取得收容產出的本體）；
- find／get 的快照時間是世代 published_at（F-M1，新鮮度與 Agora 相同）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.foundry.index import (
    ArtifactRow,
    FoundryIndexMeta,
    build_foundry_index,
)
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.reader.foundry import FoundryReader
from aistorage.schema import generate_ulid

T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"
T2 = "2026-09-28T10:00:00.000Z"


def _reader(drive: FakeDrive, tmp_path: Path, arts: list[ArtifactRow],
            published_at: str = T1) -> FoundryReader:
    index_path = tmp_path / f"f74_{generate_ulid()}.sqlite"
    build_foundry_index(
        index_path, artifacts=arts,
        meta=FoundryIndexMeta(
            generation=1, built_at=T1, foundry_main_sha="main74"),
    )
    index_file = drive.create("root", "index.sqlite", index_path)
    manifest = {
        "format": "aistorage.readview/v1",
        "element": "foundry",
        "generation": 1,
        "published_at": published_at,
        "agora_main_sha": "main74",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {
            "id": index_file.id,
            "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            "size": index_path.stat().st_size,
        },
        "files": [index_file.id],
        "retired": [],
    }
    manifest_file = drive.create(
        "root", "manifest.json",
        (json.dumps(manifest) + "\n").encode("utf-8"))
    cfg = ReaderConfig(
        manifest_file_id=manifest_file.id,
        sa_key_path=tmp_path / "sa.json",
        cache_dir=tmp_path / "cache74",
    )
    return FoundryReader(
        ReadViewClient(drive, cfg, clock=FixedClock(T2)),
        drive=drive, clock=FixedClock(T2))


def test_find_by_annex_key(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.seed_folder("root")
    content = b"report-bytes"
    obj = drive.create("root", "report.pdf", content)
    sha = hashlib.sha256(content).hexdigest()
    key = f"SHA256E-s{len(content)}--{sha}.pdf"
    art = ArtifactRow(
        artifact_id=f"artifact:{generate_ulid()}",
        kind="contained", content_type="application/pdf", name="report.pdf",
        producer="profile:mac-opencode", case_id="case-1",
        produced_by_session_id="opencode:s1",
        created_at=T0, updated_at=T0, size=len(content), sha256=sha,
        annex_key=key, object_file_id=obj.id,
    )
    other = ArtifactRow(
        artifact_id=f"artifact:{generate_ulid()}",
        kind="link", content_type="text/markdown", name="notes.md",
        producer="profile:mac-opencode", case_id="case-1",
        produced_by_session_id="opencode:s1",
        created_at=T0, updated_at=T0, link="https://example.invalid/n.md",
    )
    reader = _reader(drive, tmp_path, [art, other])

    hit = reader.find(annex_key=key).value
    assert len(hit) == 1
    assert hit[0].artifact.artifact_id == art.artifact_id

    assert reader.find(annex_key="SHA256E-s1--" + "0" * 64).value == []

    # 依 key 找到之後能取回本體（雜湊一致）
    got = reader.get(hit[0].artifact.artifact_id)
    assert got.value.data == content


def test_snapshot_time_is_published_at(tmp_path: Path) -> None:
    drive = FakeDrive()
    drive.seed_folder("root")
    art = ArtifactRow(
        artifact_id=f"artifact:{generate_ulid()}",
        kind="link", content_type="text/markdown", name="old.md",
        producer="profile:mac-opencode", case_id=None,
        produced_by_session_id="opencode:s1",
        created_at=T0, updated_at=T0, link="https://example.invalid/o.md",
    )
    reader = _reader(drive, tmp_path, [art])

    found = reader.find(max_lag=timedelta(minutes=90))
    assert found.value[0].freshness.snapshot_at == T1
    assert found.value[0].freshness.satisfied is True
    assert found.freshness.snapshot_at == T1

    strict = reader.find(max_lag=timedelta(minutes=30))
    assert strict.value[0].freshness.satisfied is False
    assert strict.value[0].freshness.warning

    got = reader.get(art.artifact_id)
    assert got.freshness.snapshot_at == T1
