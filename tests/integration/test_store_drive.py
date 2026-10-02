"""The store against real Google Drive, only inside agora-test/ (design.md section 7).

Two cache/state directories act as two machines sharing one Drive folder.
Needs ~/.config/agora/rclone.conf (the worker client). Every session this
test creates is purged at the end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agora import header as h
from agora import store

REAL_CONF = Path(os.path.expanduser("~/.config/agora/rclone.conf"))
pytestmark = pytest.mark.integration


def _machine(tmp_path: Path, name: str, monkeypatch) -> store.Paths:
    config = tmp_path / name / "config"
    config.mkdir(parents=True)
    # A symlink, so a token rclone refreshes lands back in the real file.
    (config / "rclone.conf").symlink_to(REAL_CONF)
    return store.Paths(config=config, cache=tmp_path / name / "cache", state=tmp_path / name / "state")


@pytest.fixture
def two_machines(tmp_path, monkeypatch):
    if not REAL_CONF.exists():
        pytest.fail("需要 ~/.config/agora/rclone.conf（整合測試不能 skip）")
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.delenv("AGORA_RCLONE", raising=False)
    a, b = _machine(tmp_path, "a", monkeypatch), _machine(tmp_path, "b", monkeypatch)
    created: list[str] = []
    yield a, b, created
    drive = store.Drive(a)
    for ulid in created:
        subprocess.run(["rclone", "--config", str(REAL_CONF), "--drive-root-folder-id", drive.folder_id(),
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)


def _header(title: str) -> dict:
    ulid = h.new_ulid()
    return {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
                      "relation": "import", "parents": [],
                      "source": {"agent": "opencode", "session_id": f"ses_it{ulid[-6:]}", "created_at": "2026-10-01T00:00:00Z"}}}


def T(keyword: str) -> list:
    return [(("text",), "~=", keyword)]


def test_write_on_one_machine_read_on_another(two_machines):
    a, b, created = two_machines
    hdr = _header("整合測試：表格轉換")
    folder = store.stage(a, hdr, "## user\n把 CSV 轉成 Markdown 表格\n", b'{"it": 1}')
    ulid = folder.name
    created.append(ulid)
    store.push_one(store.Drive(a), folder)

    index_b = store.sync(b)
    hits = index_b.search(T("表格"))
    assert [hit[0] for hit in hits if hit[0] == ulid] == [ulid]
    raw = store.fetch_raw(b, store.Drive(b), ulid, index_b.header(ulid))
    assert raw == b'{"it": 1}'


def test_reimport_swaps_raw_and_other_machine_follows(two_machines):
    a, b, created = two_machines
    hdr = _header("整合測試：重新匯入")
    folder = store.stage(a, hdr, "## user\n第一版\n", b'{"v": 1}')
    ulid = folder.name
    created.append(ulid)
    store.push_one(store.Drive(a), folder)
    store.sync(b)

    folder = store.stage(a, hdr, "## user\n第二版\n", b'{"v": 2}')
    store.push_one(store.Drive(a), folder)
    files = store.Drive(a).list_sessions()[ulid]
    assert sorted(files) == sorted(["session.md", hdr["agora"]["raw"]["file"]])   # the old raw is gone

    stale = store.Index(b).header(ulid)          # b still points at the first raw
    assert store.fetch_raw(b, store.Drive(b), ulid, stale) == b'{"v": 2}'   # N8 refresh


def test_deleted_on_drive_disappears_from_other_mirror(two_machines):
    a, b, created = two_machines
    folder = store.stage(a, _header("整合測試：刪除"), "## user\n要被刪掉\n", b"{}")
    ulid = folder.name
    store.push_one(store.Drive(a), folder)
    assert store.sync(b).header(ulid)
    drive = store.Drive(a)
    subprocess.run(["rclone", "--config", str(REAL_CONF), "--drive-root-folder-id", drive.folder_id(),
                    "purge", f"gdrive:sessions/{ulid}"], check=True, capture_output=True)
    assert store.sync(b).header(ulid) is None
