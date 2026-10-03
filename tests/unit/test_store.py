"""Storage, write order, outbox and sync (test-plan U-ST)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

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
    return {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
                      "relation": "import", "parents": [],
                      "source": {"agent": "opencode", "session_id": "ses_test", "created_at": "2026-10-01T00:00:00Z"}}}


def T(keyword: str) -> list:
    """A full-text filter, or none for an empty keyword."""
    return [(("text",), "~=", keyword)] if keyword else []


def _save(paths, hdr, body="## user\n把 CSV 轉成 Markdown 表格\n", raw=b'{"x": 1}'):
    folder = store.stage(paths, hdr, body, raw)
    store.upload_batch(store.Drive(paths), paths)
    return hdr["id"].split(":", 1)[1]


def test_raw_uploaded_before_session_md(remote, monkeypatch):  # U-ST-01, U-ST-02, review D1
    paths = store.Paths.from_env()
    batches = []
    real_copy = store._copy_batch
    monkeypatch.setattr(store, "_copy_batch",
                        lambda d, p, names: (batches.append(list(names)), real_copy(d, p, names))[1])
    ulid = _save(paths, _header())
    # raws first, once each: two batches, and no file sent twice (D1)
    assert len(batches) == 2, batches
    assert batches[0][0].split("/")[1].startswith("raw-"), batches
    assert batches[1] == [f"{ulid}/session.md"]
    hdr, _ = h.split_document((remote / "agora" / "sessions" / ulid / "session.md").read_text())
    raw_path = remote / "agora" / "sessions" / ulid / hdr["agora"]["raw"]["file"]
    assert hdr["agora"]["raw"]["file"] == f"raw-{hdr['agora']['raw']['md5'][:12]}.json"
    assert store.md5_file(raw_path) == hdr["agora"]["raw"]["md5"]
    assert not (paths.outbox / ulid).exists()


def test_reimport_replaces_raw_without_dangling_pointer(remote):  # U-ST-03
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = _save(paths, hdr)
    _save(paths, hdr, raw=b'{"x": 2}')
    files = sorted(p.name for p in (remote / "agora" / "sessions" / ulid).iterdir())
    final, _ = h.split_document((remote / "agora" / "sessions" / ulid / "session.md").read_text())
    assert files == sorted([final["agora"]["raw"]["file"], "session.md"])


def test_unfinished_session_is_not_indexed(remote):  # U-ST-04, U-ST-05
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    hdr, _ = h.split_document((remote / "agora" / "sessions" / ulid / "session.md").read_text())
    raw = remote / "agora" / "sessions" / ulid / hdr["agora"]["raw"]["file"]
    raw.write_text("tampered")
    index = store.sync(paths)
    assert index.search(T("CSV")) == []


def test_failed_upload_stays_in_outbox_and_sync_pushes_it(remote, monkeypatch):  # U-ST-06, U-ST-08
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "raw-")
    assert store.upload_batch(store.Drive(paths), paths) == [folder.name]   # it says, not raise
    assert folder.exists() and store.outbox_count(paths) == 1
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    index = store.sync(paths)
    assert store.outbox_count(paths) == 0
    assert [hit[0] for hit in index.search(T("CSV"))] == [hdr["id"].split(":", 1)[1]]


def test_outbox_survives_cache_wipe(remote, monkeypatch):  # U-ST-09
    paths = store.Paths.from_env()
    folder = store.stage(paths, _header(), "## user\nCSV\n", b"{}")
    paths.cache.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.rmtree(paths.cache)
    assert folder.exists()
    store.sync(paths)
    assert store.outbox_count(paths) == 0


def test_deleted_remote_session_stays_and_is_marked(remote):  # U-ST-12, T1 R6
    """Another machine deleted it. We keep the mirror, the row and the search
    entry: whether to drop it or send it back is the user's call."""
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    assert store.sync(paths).search(T("CSV"))
    import shutil
    shutil.rmtree(remote / "agora" / "sessions" / ulid)

    index = store.sync(paths)
    assert [u for u, _, _ in index.search(T("CSV"))] == [ulid]
    assert (paths.mirror / ulid / "session.md").is_file()
    assert index.missing_in_cloud() == [ulid] and not index.cloud_has(ulid)


def test_a_bumped_index_is_filled_from_the_mirror_whatever_was_put_first(remote):
    """M3: after a version bump the index is rebuilt from the mirror, not only when
    it looks empty - `recover_pending` writes a row before anything else looks."""
    paths = store.Paths.from_env()
    ulids = [_save(paths, _header(f"第 {i} 篇")) for i in range(3)]
    store.sync(paths)                        # the mirror is the local truth
    index = store.Index(paths)
    index.db.execute(f"PRAGMA user_version = {store.INDEX_VERSION - 1}")
    index.put(ulids[0], "0" * 32, _header("先寫進去的那筆"), "")   # a row lands first

    assert sorted(store.Index(paths).known()) == sorted(ulids)
    assert sorted(u for u, _, _ in store.Index(paths).search(T("第"))) == sorted(ulids)


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
    assert store.sync(paths).search(T("CSV"))


def test_folder_id_created_once(remote):  # U-ST-17
    paths = store.Paths.from_env()
    store.Drive(paths).folder_id()
    assert json.loads((paths.config / "config.json").read_text())["folders"] == {"agora": "agora"}
    before = len(calls(remote))
    store.Drive(paths).folder_id()
    assert len(calls(remote)) == before


def test_cjk_and_short_keywords(remote):  # T1, T2
    paths = store.Paths.from_env()
    _save(paths, _header(), body="## user\n表格轉換を変換するテーブル　ＣＳＶ\n")
    index = store.sync(paths)
    for kw in ["表格", "表格轉換", "変換する", "テーブル", "CSV", "ｃｓｖ"]:
        assert index.search(T(kw)), kw


def test_search_filters(remote):
    paths = store.Paths.from_env()
    hdr = _header()
    hdr["agora"]["source"]["agent"] = "claude"
    _save(paths, hdr)
    _save(paths, _header())
    index = store.sync(paths)
    assert len(index.search(T("CSV") + [(("agora", "source", "agent"), "=", "claude")])) == 1
    assert len(index.search(T(""))) == 2


def test_unknown_ref_entity_is_still_indexed(remote):  # R7
    paths = store.Paths.from_env()
    hdr = _header()
    hdr["refs"] = ["future:thing"]
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    store.upload_batch(store.Drive(paths), paths)
    assert store.sync(paths).search(T("CSV"))


def test_test_folder_never_leaks_into_normal_runs(remote, monkeypatch):
    paths = store.Paths.from_env()
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    assert store.Drive(paths).folder_id() == "agora-test"
    monkeypatch.delenv("AGORA_FOLDER_NAME")
    assert store.Drive(paths).folder_id() == "agora"


def test_cold_start_downloads_in_one_batch(remote):  # docs/perf.md
    paths = store.Paths.from_env()
    for _ in range(3):
        _save(paths, _header())
    before = len(calls(remote))
    index = store.sync(paths)
    new_calls = calls(remote)[before:]
    assert sum(1 for c in new_calls if "copy" in c and "--files-from" in c) == 1
    assert not any("copyto" in c and "session.md" in c[-1] for c in new_calls if c[-1].startswith("/"))
    assert len(index.known()) == 3


def test_a_session_without_a_raw_uploads_only_session_md(remote, monkeypatch):  # review D1
    """One file in, one copyto out - the other half of D1's count."""
    paths = store.Paths.from_env()
    folder = store.stage(paths, _header(), "## user\n沒有原始檔\n", None)
    ulid = folder.name
    batches = []
    real_copy = store._copy_batch
    monkeypatch.setattr(store, "_copy_batch",
                        lambda d, p, names: (batches.append(list(names)), real_copy(d, p, names))[1])
    store.upload_batch(store.Drive(paths), paths)
    assert batches == [[f"{ulid}/session.md"]], batches   # one batch, and no raw to send
    assert (remote / "agora" / "sessions" / ulid / "session.md").is_file()


def test_deleting_a_session_drive_already_lost_still_finishes(remote):  # review S1-4
    """Interrupted between the purge and forgetting it locally, the rerun purges a
    folder that is gone. That is a delete that never completes."""
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    import shutil
    shutil.rmtree(remote / "agora" / "sessions" / ulid)   # the purge already happened

    store.delete_session(paths, store.Drive(paths), ulid)   # no StoreError
    assert not (paths.mirror / ulid).exists()
    assert store.Index(paths).header(ulid) is None


def test_a_failed_purge_that_still_finds_the_session_forgets_nothing(remote):  # review S1-4b
    """"Not found" also means a wrong folder id or a token that sees nothing. Only a
    listing without this ULID means it is gone."""
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    store.sync(paths)          # mirrored and indexed: there is a local copy to forget
    monkey = os.environ.get("FAKE_RCLONE_FAIL")
    os.environ["FAKE_RCLONE_FAIL"] = "purge"
    try:
        with pytest.raises(store.StoreError):
            store.delete_session(paths, store.Drive(paths), ulid)
    finally:
        if monkey is None:
            os.environ.pop("FAKE_RCLONE_FAIL")
        else:
            os.environ["FAKE_RCLONE_FAIL"] = monkey
    assert (paths.mirror / ulid / "session.md").is_file()
    assert store.Index(paths).header(ulid) is not None
    assert (remote / "agora" / "sessions" / ulid).is_dir()


def test_a_purge_that_fails_and_a_listing_that_fails_forgets_nothing(remote):  # review S1-4b
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    store.sync(paths)
    monkey = os.environ.get("FAKE_RCLONE_FAIL")
    os.environ["FAKE_RCLONE_FAIL"] = "sessions"      # every call that names the folder
    try:
        with pytest.raises(store.StoreError):
            store.delete_session(paths, store.Drive(paths), ulid)
    finally:
        if monkey is None:
            os.environ.pop("FAKE_RCLONE_FAIL")
        else:
            os.environ["FAKE_RCLONE_FAIL"] = monkey
    assert store.Index(paths).header(ulid) is not None
