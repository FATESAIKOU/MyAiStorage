"""Foundry 發佈器與 rows_from_catalog 的單元測試（review-25a48a9 H5）。

- manifest 固定 id 原地更新、世代單調遞增、退役檔留一個世代再刪；
- index 副檔名與內容一致（.sqlite）；
- ArtifactRow 取 catalog 最上層欄位；contained 缺欄位就 raise；
- link 型直接發佈（不經 object_file_id 解析）；
- 冪等（真本沒動就不發佈）與 dry-run。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import MismatchError
from aistorage.publish.foundry import (
    FoundryReadViewPublisher,
    rows_from_catalog,
)
from aistorage.readview.model import (
    initial_manifest,
    parse_manifest,
    serialize_manifest,
)

T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"

BODY = b"report-bytes-74"
SHA = hashlib.sha256(BODY).hexdigest()
KEY = f"SHA256E-s{len(BODY)}--{SHA}.pdf"


def _catalog_contained(artifact_id: str = "artifact:01ABCDEF2345GHJKLMNPQRS"):
    return {
        "artifact_id": artifact_id,
        "kind": "contained",
        "name": "report.pdf",
        "content_type": "application/pdf",
        "producer": "profile:mac-opencode",
        "case_id": "case-1",
        "produced_by_session_id": "opencode:s1",
        "created_at": T0,
        "updated_at": T0,
        "size": len(BODY),
        "sha256": SHA,
        "annex_key": KEY,
        "object_path": "objects/01ABCDEF2345GHJKLMNPQRS/report.pdf",
        "metadata": {"id": artifact_id, "type": "artifact",
                     "created_at": T0, "updated_at": T0, "case_id": "case-1"},
        "body": {"kind": "contained", "produced_by_session_id": "opencode:s1",
                 "name": "report.pdf"},
    }


def _catalog_link(artifact_id: str = "artifact:01ABCDEF2345GHJKLMNPQRT"):
    return {
        "artifact_id": artifact_id,
        "kind": "link",
        "name": "spec.md",
        "producer": "profile:mac-opencode",
        "produced_by_session_id": "opencode:s1",
        "created_at": T0,
        "updated_at": T0,
        "link": "https://example.invalid/spec.md",
        "repo": "org/repo",
        "path": "spec.md",
        "metadata": {"id": artifact_id, "type": "artifact",
                     "created_at": T0, "updated_at": T0, "case_id": None},
        "body": {"kind": "link", "produced_by_session_id": "opencode:s1",
                 "name": "spec.md"},
    }


class _Store:
    """最小 FoundryStore 介面（list_catalog）。"""

    def __init__(self, catalogs: list[dict]):
        self._catalogs = catalogs

    def list_catalog(self) -> list[dict]:
        return list(self._catalogs)


def _publisher(drive: FakeDrive, tmp_path: Path, manifest_id: str,
               prefix_id: str, readview_id: str, clock=None):
    return FoundryReadViewPublisher(
        drive, folder_id=readview_id, manifest_file_id=manifest_id,
        prefix_folder_id=prefix_id, clock=clock or FixedClock(T1),
        workdir=tmp_path / "pub-work")


def _setup(tmp_path: Path, catalogs: list[dict]):
    drive = FakeDrive()
    prefix = drive.seed_folder("foundry")
    readview = drive.seed_folder("readview")
    manifest_id = drive.create(
        readview, "manifest.json",
        serialize_manifest(initial_manifest(
            element="foundry", published_at=T0))).id
    return drive, prefix, readview, manifest_id


def test_rows_from_catalog_uses_top_level_fields() -> None:
    rows = rows_from_catalog([_catalog_contained(), _catalog_link()])
    assert rows[0].annex_key == KEY
    assert rows[0].sha256 == SHA
    assert rows[0].size == len(BODY)
    assert rows[0].producer == "profile:mac-opencode"
    assert rows[1].kind == "link"
    assert rows[1].link == "https://example.invalid/spec.md"


def test_rows_from_catalog_contained_missing_field_raises() -> None:
    bad = _catalog_contained()
    del bad["annex_key"]
    with pytest.raises(MismatchError, match="缺少欄位"):
        rows_from_catalog([bad])
    bad2 = _catalog_contained()
    del bad2["sha256"]
    with pytest.raises(MismatchError, match="缺少欄位"):
        rows_from_catalog([bad2])


def test_publish_lifecycle_generations_and_retire(tmp_path: Path) -> None:
    drive, prefix, readview, manifest_id = _setup(
        tmp_path, [_catalog_contained(), _catalog_link()])
    drive.seed_file(prefix, KEY, BODY)
    store = _Store([_catalog_contained(), _catalog_link()])
    pub = _publisher(drive, tmp_path, manifest_id, prefix, readview)

    # gen0 → gen1：contained＋link 都發佈
    res1 = pub.publish(store, foundry_main_sha="main-1", allowed_keys={KEY})
    assert res1.report.status == "published"
    assert res1.report.generation == 1
    assert res1.unpublished == ()
    m1 = parse_manifest(drive.download_bytes(manifest_id, max_bytes=1 << 20))
    assert m1.generation == 1 and m1.element == "foundry"
    assert m1.index is not None
    index_v1 = m1.index.id
    assert drive.get(index_v1).name.endswith(".sqlite")

    # 冪等：什麼都沒變 → skipped
    res_idle = pub.publish(store, foundry_main_sha="main-1",
                           allowed_keys={KEY})
    assert res_idle.report.status == "skipped"
    assert res_idle.report.generation == 1

    # 真本前進 → gen2；上一世代的 index 留著（退役一個世代）
    res2 = pub.publish(store, foundry_main_sha="main-2", allowed_keys={KEY})
    assert res2.report.status == "published"
    assert res2.report.generation == 2
    m2 = parse_manifest(drive.download_bytes(manifest_id, max_bytes=1 << 20))
    assert m2.index is not None and m2.index.id != index_v1
    assert drive.get(index_v1).name.endswith(".sqlite")  # 還沒刪
    assert res2.report.retired == (index_v1,)

    # 再一個世代 → gen1 的 index 才被刪除（只刪讀取視圖資料夾內的）
    res3 = pub.publish(store, foundry_main_sha="main-3", allowed_keys={KEY})
    assert res3.report.status == "published"
    assert res3.report.generation == 3
    assert res3.report.deleted == (index_v1,)
    with pytest.raises(Exception):
        drive.get(index_v1)


def test_publish_rejects_wrong_element_manifest(tmp_path: Path) -> None:
    drive = FakeDrive()
    prefix = drive.seed_folder("foundry")
    readview = drive.seed_folder("readview")
    manifest_id = drive.create(
        readview, "manifest.json",
        serialize_manifest(initial_manifest(
            element="agora", published_at=T0))).id
    pub = _publisher(drive, tmp_path, manifest_id, prefix, readview)
    with pytest.raises(MismatchError, match="不是 'foundry'"):
        pub.publish(_Store([]), foundry_main_sha="main-1")


def test_publish_dry_run_writes_nothing(tmp_path: Path) -> None:
    drive, prefix, readview, manifest_id = _setup(tmp_path, [])
    drive.seed_file(prefix, KEY, BODY)
    pub = _publisher(drive, tmp_path, manifest_id, prefix, readview)
    res = pub.publish(_Store([_catalog_contained()]),
                      foundry_main_sha="main-1", dry_run=True)
    assert res.report.status == "planned"
    m = parse_manifest(drive.download_bytes(manifest_id, max_bytes=1 << 20))
    assert m.generation == 0  # manifest 沒動
    assert drive.list_children(readview) != []  # 只有 manifest 本身
    names = [c.name for c in drive.list_children(readview)]
    assert names == ["manifest.json"]


def test_publish_reports_unpublished_without_failing(tmp_path: Path) -> None:
    drive, prefix, readview, manifest_id = _setup(tmp_path, [])
    # 前綴是空的：contained 對不上就不發佈，但 link 照發、整輪不爆
    pub = _publisher(drive, tmp_path, manifest_id, prefix, readview)
    res = pub.publish(_Store([_catalog_contained("artifact:BAD"),
                              _catalog_link()]),
                      foundry_main_sha="main-1", allowed_keys={KEY})
    assert res.report.status == "published"
    assert res.unpublished == (("artifact:BAD", "object_not_found"),)


def test_init_readview_element_foundry(tmp_path: Path) -> None:
    from aistorage.admin.init_readview import init_readview
    drive = FakeDrive()
    folder = drive.seed_folder("readview-foundry")
    result = init_readview(drive, folder, confirm=True,
                           clock=FixedClock(T1), element="foundry")
    assert result.generation == 0
    data = json.loads(drive.download_bytes(
        result.manifest_file_id, max_bytes=1 << 20).decode("utf-8"))
    assert data["element"] == "foundry"
    with pytest.raises(Exception):
        init_readview(drive, folder, confirm=True,
                      clock=FixedClock(T1), element="nope")
