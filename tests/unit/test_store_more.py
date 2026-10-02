"""More header/store coverage (code-pm section 2): split errors and U-ST gaps.

Store-level tests over the fake rclone; a second machine is just another
cache/state pair on the same fake remote. Mirrors test_store.py conventions.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from agora import header as h
from agora import store

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def machine2(tmp_path):
    paths = store.Paths.from_env()
    other = tmp_path / "m2"
    return store.Paths(config=paths.config, cache=other / "cache",
                       state=other / "state")


def _header(title="CSV 規劃表格", **kw):
    ulid = kw.pop("ulid", None) or h.new_ulid()
    hdr = {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                     "updated_at": "2026-10-02T00:00:00Z",
                     "relation": "import", "parents": [],
                     "source": {"agent": "opencode", "session_id": "ses_test",
                                "created_at": "2026-10-01T00:00:00Z"}}}
    hdr.update(kw)
    return hdr


def T(keyword: str) -> list:
    """A full-text filter, or none for an empty keyword."""
    return [(("text",), "~=", keyword)] if keyword else []


def _save(paths, hdr, body="## user\n把 CSV 轉成 Markdown 表格\n", raw=b'{"x": 1}'):
    folder = store.stage(paths, hdr, body, raw)
    store.push_one(store.Drive(paths), folder)
    return hdr["id"].split(":", 1)[1]


def calls(remote):
    log = remote.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def gdrive_targets(call):
    """The gdrive: paths in a logged call (Drive._run prefixes --config etc)."""
    return [a for a in call if isinstance(a, str) and a.startswith("gdrive:")]


# --- header split errors (new, C2) --------------------------------------------

def test_split_document_rejects_broken_yaml():
    with pytest.raises(h.HeaderError):
        h.split_document("---\ntitle: [unclosed\n---\nbody\n")


def test_read_entry_rejects_non_utf8(remote):
    paths = store.Paths.from_env()
    folder = store.stage(paths, _header(), "## user\nCSV\n", b"{}")
    (folder / "session.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(h.HeaderError):
        store.read_entry(folder)


# --- U-ST gaps -----------------------------------------------------------------

def test_no_snapshot_has_a_dangling_raw_pointer(remote):  # U-ST-03, snapshots
    paths = store.Paths.from_env()
    hdr = _header()
    _save(paths, hdr, raw=b'{"v": 1}')
    _save(paths, hdr, raw=b'{"v": 2}')
    snaps = sorted((remote.parent / "snapshots").iterdir())
    assert snaps, "fake rclone should snapshot after every call"
    checked = 0
    for snap in snaps:
        for md in (snap / "agora" / "sessions").glob("*/session.md") if (snap / "agora" / "sessions").exists() else []:
            hdr, _ = h.split_document(md.read_text(encoding="utf-8"))
            raw = h.agora_of(hdr).get("raw")
            if not raw:
                continue
            target = md.parent / raw["file"]
            assert target.is_file(), f"{snap.name}: {raw['file']} missing"
            assert hashlib.md5(target.read_bytes()).hexdigest() == raw["md5"]
            checked += 1
    assert checked >= 2


def test_missing_raw_not_indexed_until_added(remote, tmp_path):  # U-ST-04
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV 表格\n", b'{"x": 1}')
    drive = store.Drive(paths)
    drive.upload(folder / "session.md", hdr["id"].split(":", 1)[1], "session.md")
    other = machine2(tmp_path)
    assert store.sync(other).search(T("表格")) == []
    drive.upload(folder / h.agora_of(hdr)["raw"]["file"], hdr["id"].split(":", 1)[1], h.agora_of(hdr)["raw"]["file"])
    assert [hit[0] for hit in store.sync(other).search(T("表格"))] == [hdr["id"].split(":", 1)[1]]


def test_session_md_upload_failure_leaves_only_raw(remote, tmp_path, monkeypatch):  # U-ST-07
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "session.md")
    with pytest.raises(store.StoreError):
        store.push_one(store.Drive(paths), folder)
    ulid = hdr["id"].split(":", 1)[1]
    assert sorted(p.name for p in (remote / "agora" / "sessions" / ulid).iterdir()) == [h.agora_of(hdr)["raw"]["file"]]
    assert store.sync(machine2(tmp_path)).search(T("CSV")) == []


def test_duplicate_drive_folder_errors(remote, monkeypatch):  # U-ST-16
    monkeypatch.setenv("FAKE_RCLONE_DUP", "agora")
    with pytest.raises(store.StoreError):
        store.Drive(store.Paths.from_env()).folder_id()


def test_merge_without_raw_is_indexed(remote, tmp_path):  # U-ST-18, N5
    paths = store.Paths.from_env()
    hdr = _header()
    hdr.pop("source", None)
    folder = store.stage(paths, hdr, "## user\n合併結果表格\n", None)
    store.push_one(store.Drive(paths), folder)
    assert "raw" not in h.agora_of(hdr)
    assert store.sync(machine2(tmp_path)).search(T("表格"))


def test_sync_never_downloads_raw(remote, tmp_path):  # U-ST-19, N11
    paths = store.Paths.from_env()
    _save(paths, _header())
    _save(paths, _header())
    before = len(calls(remote))
    store.sync(machine2(tmp_path))
    downloads = [c for c in calls(remote)[before:]
                 if "copyto" in c and any(t.endswith(".json") for t in gdrive_targets(c))]
    assert downloads == []


def test_missing_sessions_dir_keeps_mirror(remote, capsys):  # U-ST-20, N12
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    assert store.sync(paths).search(T("CSV"))
    import shutil
    shutil.rmtree(remote / "agora" / "sessions")
    index = store.sync(paths)
    assert [hit[0] for hit in index.search(T("CSV"))] == [ulid]
    assert (paths.mirror / ulid / "session.md").is_file()
    assert "先不刪鏡像" in capsys.readouterr().err


def test_failed_listing_keeps_mirror(remote, monkeypatch):  # U-ST-20, N12
    paths = store.Paths.from_env()
    _save(paths, _header())
    assert store.sync(paths).search(T("CSV"))
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    assert store.sync(paths).search(T("CSV"))


def test_fetch_raw_refetches_session_md(remote, tmp_path):  # U-ST-21, N8
    paths = store.Paths.from_env()
    hdr = _header()
    _save(paths, hdr, raw=b'{"v": 1}')
    other = machine2(tmp_path)
    stale = store.sync(other).header(hdr["id"].split(":", 1)[1])
    _save(paths, hdr, body="## user\n第二版表格\n", raw=b'{"v": 2}')
    got = store.fetch_raw(other, store.Drive(other), hdr["id"].split(":", 1)[1], stale)
    assert got == b'{"v": 2}'
    fresh, _ = h.split_document((other.mirror / hdr["id"].split(":", 1)[1] / "session.md").read_text())
    assert h.agora_of(fresh)["raw"]["md5"] == hashlib.md5(b'{"v": 2}').hexdigest()


def test_broken_outbox_entry_quarantined(remote, capsys):  # new, C2/C4
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    (folder / "session.md").write_text("---\ntitle: [unclosed\n---\nbody\n")
    store.sync(paths)
    assert (paths.outbox / ".bad" / hdr["id"].split(":", 1)[1]).is_dir()
    assert "壞了" in capsys.readouterr().err


def test_push_one_lists_only_its_folder(remote):  # new, C7
    paths = store.Paths.from_env()
    _save(paths, _header())
    listings = [c for c in calls(remote)
                if "lsjson" in c and any("gdrive:sessions" in t for t in gdrive_targets(c))]
    assert listings, "push_one should verify with list_one"
    assert all(t != "gdrive:sessions" and t.startswith("gdrive:sessions/")
               for c in listings for t in gdrive_targets(c) if "gdrive:sessions" in t)
