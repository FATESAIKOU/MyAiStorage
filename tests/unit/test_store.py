"""Storage, write order, outbox and sync (test-plan U-ST)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agora import header as h
from agora import store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def calls(remote: Path) -> list[list[str]]:
    log = remote.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _header(title="CSV 規劃"):
    ulid = h.new_ulid()
    return {"header": 1, "entity": "agora", "type": "session", "id": f"agora:{ulid}",
            "title": title, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
            "refs": [], "case": None, "note": None, "tags": [],
            "source": {"agent": "opencode", "session_id": "ses_test", "created_at": "2026-10-01T00:00:00Z"},
            "relation": "import", "parents": []}


def _save(paths, hdr, body="## user\n把 CSV 轉成 Markdown 表格\n", raw=b'{"x": 1}'):
    folder = store.stage(paths, hdr, body, raw)
    store.push_one(store.Drive(paths), folder)
    return hdr["id"].split(":", 1)[1]


def test_raw_uploaded_before_session_md(remote):  # U-ST-01, U-ST-02
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    uploads = [c for c in calls(remote) if "copyto" in c]
    assert uploads[0][-1].endswith(".json") and uploads[-1][-1].endswith("session.md")
    hdr, _ = h.split_document((remote / "agora" / "sessions" / ulid / "session.md").read_text())
    raw_path = remote / "agora" / "sessions" / ulid / hdr["raw"]["file"]
    assert hdr["raw"]["file"] == f"raw-{hdr['raw']['md5'][:12]}.json"
    assert store.md5_file(raw_path) == hdr["raw"]["md5"]
    assert not (paths.outbox / ulid).exists()


def test_reimport_replaces_raw_without_dangling_pointer(remote):  # U-ST-03
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = _save(paths, hdr)
    _save(paths, hdr, raw=b'{"x": 2}')
    files = sorted(p.name for p in (remote / "agora" / "sessions" / ulid).iterdir())
    final, _ = h.split_document((remote / "agora" / "sessions" / ulid / "session.md").read_text())
    assert files == sorted([final["raw"]["file"], "session.md"])


def test_unfinished_session_is_not_indexed(remote):  # U-ST-04, U-ST-05
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    hdr, _ = h.split_document((remote / "agora" / "sessions" / ulid / "session.md").read_text())
    raw = remote / "agora" / "sessions" / ulid / hdr["raw"]["file"]
    raw.write_text("tampered")
    index = store.sync(paths)
    assert index.search("CSV", []) == []


def test_failed_upload_stays_in_outbox_and_sync_pushes_it(remote, monkeypatch):  # U-ST-06, U-ST-08
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "raw-")
    with pytest.raises(store.StoreError):
        store.push_one(store.Drive(paths), folder)
    assert folder.exists() and store.outbox_count(paths) == 1
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    index = store.sync(paths)
    assert store.outbox_count(paths) == 0
    assert [hit[0] for hit in index.search("CSV", [])] == [hdr["id"].split(":", 1)[1]]


def test_outbox_survives_cache_wipe(remote, monkeypatch):  # U-ST-09
    paths = store.Paths.from_env()
    folder = store.stage(paths, _header(), "## user\nCSV\n", b"{}")
    paths.cache.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.rmtree(paths.cache)
    assert folder.exists()
    store.sync(paths)
    assert store.outbox_count(paths) == 0


def test_deleted_remote_session_leaves_index(remote):  # U-ST-12
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    assert store.sync(paths).search("CSV", [])
    import shutil
    shutil.rmtree(remote / "agora" / "sessions" / ulid)
    assert store.sync(paths).search("CSV", []) == []
    assert not (paths.mirror / ulid).exists()


def test_mirror_holds_only_session_md_and_one_listing(remote):  # U-ST-13
    paths = store.Paths.from_env()
    for _ in range(3):
        _save(paths, _header())
    before = len(calls(remote))
    store.sync(paths)
    new_calls = calls(remote)[before:]
    assert sum(1 for c in new_calls if "lsjson" in c) == 1
    assert all(p.name == "session.md" for p in paths.mirror.rglob("*") if p.is_file())


def test_sync_throttle(remote, monkeypatch):  # U-ST-14
    paths = store.Paths.from_env()
    monkeypatch.setenv("AGORA_NOW", "1000")
    store.sync(paths, throttle=True)
    n = len(calls(remote))
    monkeypatch.setenv("AGORA_NOW", "1060")
    store.sync(paths, throttle=True)
    assert len(calls(remote)) == n
    monkeypatch.setenv("AGORA_NOW", "1400")
    store.sync(paths, throttle=True)
    assert len(calls(remote)) > n


def test_offline_falls_back_to_local_index(remote, monkeypatch):  # U-ST-15
    paths = store.Paths.from_env()
    _save(paths, _header())
    store.sync(paths)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    assert store.sync(paths).search("CSV", [])


def test_folder_id_created_once(remote):  # U-ST-17
    paths = store.Paths.from_env()
    store.Drive(paths).folder_id()
    assert json.loads((paths.config / "config.json").read_text())["folder_id"] == "agora"
    before = len(calls(remote))
    store.Drive(paths).folder_id()
    assert len(calls(remote)) == before


def test_cjk_and_short_keywords(remote):  # T1, T2
    paths = store.Paths.from_env()
    _save(paths, _header(), body="## user\n表格轉換を変換するテーブル　ＣＳＶ\n")
    index = store.sync(paths)
    for kw in ["表格", "表格轉換", "変換する", "テーブル", "CSV", "ｃｓｖ"]:
        assert index.search(kw, []), kw


def test_search_filters(remote):
    paths = store.Paths.from_env()
    hdr = _header()
    hdr["source"]["agent"] = "claude"
    _save(paths, hdr)
    _save(paths, _header())
    index = store.sync(paths)
    assert len(index.search("CSV", [(("source", "agent"), "claude")])) == 1
    assert len(index.search("", [])) == 2


def test_unknown_ref_entity_is_still_indexed(remote):  # R7
    paths = store.Paths.from_env()
    hdr = _header()
    hdr["refs"] = ["future:thing"]
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    store.push_one(store.Drive(paths), folder)
    assert store.sync(paths).search("CSV", [])
