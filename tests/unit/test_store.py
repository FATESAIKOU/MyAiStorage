"""Storage, write order, outbox and sync (test-plan U-ST)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import stat
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
                        lambda d, p, names, **kw: (batches.append(list(names)), real_copy(d, p, names, **kw))[1])
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
                        lambda d, p, names, **kw: (batches.append(list(names)), real_copy(d, p, names, **kw))[1])
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


# --- continue-periodic-sync, the store part (tasks 1.5 / 1.7, P13 / P17) ---------


def test_the_marker_is_in_the_entry_before_it_is_swapped_in(remote, monkeypatch):
    """P13: the uploader reads the outbox whenever it likes, and `stage` used to publish
    a version first and only then be told what it was. An uploader in between saw a
    session with no marker - one it had no reason to look for on Drive - and saved it
    under a new id. So the marker goes into the folder that is about to go in, and the
    old two-step call keeps working for callers that decide afterwards.
    """
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    seen = []
    real = store.mark_update

    def spy(folder, kind=""):
        written = real(folder, kind)          # what the entry looks like once it is written
        seen.append({"folder": folder.name, "entries": sorted(p.name for p in folder.iterdir()),
                     "published": (paths.outbox / ulid).exists()})
        return written

    monkeypatch.setattr(store, "mark_update", spy)
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}", update=True, kind="continue")

    assert [s["folder"] for s in seen] == [f".tmp-{ulid}"], seen   # not the one already there
    assert not any(s["published"] for s in seen), seen             # nothing of this was out yet
    assert ".update" in seen[0]["entries"], seen                   # and it already had it
    assert store._is_update(folder), "swap in whole, marker and all"
    assert store.update_kind(folder) == "continue", "the kind travels with the marker"
    assert not store.never_confirmed(paths, ulid), \
        "Drive has this id (continue-fork 1.1): the entry says so right away"
    assert store.upload_batch(store.Drive(paths), paths) == []


def test_a_version_that_is_not_an_update_carries_no_marker(remote):
    """The other side of the same swap: a whole new folder comes in, so the previous
    write's marker cannot ride along - and `never_confirmed` goes back to saying the
    session is one Drive has never confirmed."""
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    store.stage(paths, hdr, "## user\nCSV\n", b'{"x": 1}', update=True, kind="continue")

    folder = store.stage(paths, hdr, "## user\n第二版\n", b'{"x": 2}')

    assert not store._is_update(folder), "換進去的是整個資料夾，舊的標記不會跟著"
    assert store.update_kind(folder) == ""
    assert store.never_confirmed(paths, ulid)


def test_a_sigterm_between_the_two_renames_keeps_the_old_entry(remote, monkeypatch):
    """#117: the swap is two syscalls - the old entry aside, the new one in. `stage`
    only caught `Exception`, but a round child turns SIGTERM into `SystemExit`
    (`round_child._exit_on_sigterm`), so an interrupt in that window used to leave
    no `outbox/<ULID>` at all: the write-back vanished and both versions sat in
    `.tmp-<ULID>`/`.old-<ULID>`. The old version has to be back in place, with no
    staging leftovers, before the exception leaves.
    """
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    old_version = store.stage(paths, hdr, "## user\n舊版\n", b'{"x": 1}')
    before = (old_version / "session.md").read_text(encoding="utf-8")
    old_raw = h.agora_of(hdr)["raw"]["file"]
    old_raw_bytes = (old_version / old_raw).read_bytes()

    real_rename = Path.rename
    renames = []

    def sigterm_before_the_swap(folder, target):
        renames.append((folder.name, Path(target).name))
        if len(renames) == 2:             # folder -> .old is done; this is tmp -> folder
            raise SystemExit(143)         # what the SIGTERM handler raises
        return real_rename(folder, target)

    monkeypatch.setattr(Path, "rename", sigterm_before_the_swap)
    with pytest.raises(SystemExit):
        store.stage(paths, hdr, "## user\n新版\n", b'{"x": 2}')

    folder = paths.outbox / ulid
    assert folder.is_dir(), "the outbox entry the round was about to update is still there"
    assert (folder / "session.md").read_text(encoding="utf-8") == before, "it is the old version"
    assert (folder / old_raw).read_bytes() == old_raw_bytes, "raw of the old version came with it"
    assert not (paths.outbox / f".tmp-{ulid}").exists(), "no half-written staging folder left"
    assert not (paths.outbox / f".old-{ulid}").exists(), "no old version left aside"


def test_a_sigterm_after_the_swap_keeps_the_new_entry_and_drops_the_old_aside(remote, monkeypatch):
    """#117 review N1: the interrupt can also land AFTER the second rename, while the
    old version is still aside (`.old-<ULID>`). The new version is already in place, so
    the cleanup keeps it and removes the leftover - not the other way round."""
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    store.stage(paths, hdr, "## user\n舊版\n", b'{"x": 1}')

    real_rmtree = store.shutil.rmtree
    seen_old = []

    def sigterm_while_removing_the_old(path, *args, **kwargs):
        if Path(path).name == f".old-{ulid}":
            seen_old.append(path)
            if len(seen_old) == 2:        # the first is before the swap; this one is after it
                raise SystemExit(143)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(store.shutil, "rmtree", sigterm_while_removing_the_old)
    with pytest.raises(SystemExit):
        store.stage(paths, hdr, "## user\n新版\n", b'{"x": 2}')

    folder = paths.outbox / ulid
    assert "新版" in (folder / "session.md").read_text(encoding="utf-8"), "the new version stays"
    assert not (paths.outbox / f".old-{ulid}").exists(), "the old version aside is removed"
    assert not (paths.outbox / f".tmp-{ulid}").exists()


def test_stage_fsyncs_the_files_it_writes_and_the_folders_around_them(remote, monkeypatch):
    """P17: a periodic write-back stages a session every few minutes for hours on end. A
    power cut used to be able to leave a `session.md` that was short but whole-looking -
    and that is the version Drive would be sent. So the bytes, the names pointing at them
    and the rename itself all have to reach the disk.
    """
    paths = store.Paths.from_env()
    seen, synced = [], []
    real_file, real_dir, real_fsync = store._write_file, store._fsync_dir, os.fsync
    monkeypatch.setattr(store, "_write_file",
                        lambda p, data: (seen.append(("file", Path(p))), real_file(p, data))[1])
    monkeypatch.setattr(store, "_fsync_dir", lambda p: (seen.append(("dir", Path(p))), real_dir(p))[1])
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", b'{"x": 1}', update=True, kind="continue")

    tmp = paths.outbox / f".tmp-{folder.name}"
    raw_name = h.agora_of(hdr)["raw"]["file"]
    assert {p.name for kind, p in seen if kind == "file" and p.parent == tmp} == \
        {"session.md", raw_name, store.UPDATE_MARK}, seen
    assert ("dir", tmp) in seen, "the files, and the names pointing at them, before the swap"
    assert ("dir", paths.outbox) in seen, "and the rename that put them in the outbox"
    assert len(synced) == len(seen), "each of those really reached the disk, not just the call"


@pytest.mark.parametrize("raw_as_path", [False, True], ids=["raw-bytes", "raw-file"])
def test_stage_fsyncs_each_written_file_before_the_swap_and_the_outbox_after_it(
        remote, monkeypatch, tmp_path, raw_as_path):
    """#116: the guard the ticket asked for - every fsync, on what, and in which order.

    The test above counts the calls; this one records `os.fstat(fd)` so a call is
    tied to the file or directory that was open, and interleaves the renames with
    the fsyncs. Dropping any one fsync, or moving the outbox's fsync before the
    swap, fails here. Both arms of `_stage_raw` are covered: bytes, and a file the
    stage streams (the two write the raw with different code).
    """
    paths = store.Paths.from_env()
    events = []
    real_fsync, real_rename, real_replace = os.fsync, os.rename, os.replace

    def fsync(fd):
        info = os.fstat(fd)
        events.append(("fsync", info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode)))
        return real_fsync(fd)

    def rename(src, dst, **kwargs):
        events.append(("rename", str(src), str(dst)))
        return real_rename(src, dst, **kwargs)

    def replace(src, dst, **kwargs):
        events.append(("replace", str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", replace)

    source = tmp_path / "source.json"
    source.write_bytes(b'{"x": "private"}')
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", source if raw_as_path else source.read_bytes())
    raw_name = h.agora_of(hdr)["raw"]["file"]
    written = {"raw": (folder / raw_name).stat(), "session.md": (folder / "session.md").stat()}
    directory, outbox = folder.stat(), paths.outbox.stat()

    def synced(info) -> bool:
        return any(e[:3] == ("fsync", info.st_dev, info.st_ino) for e in events)

    for name, info in written.items():
        assert synced(info), f"{name} never reached the disk"
        assert not stat.S_ISDIR(info.st_mode)
    assert {(e[1], e[2]) for e in events if e[0] == "fsync" and not e[3]} == \
        {(info.st_dev, info.st_ino) for info in written.values()}, events
    assert synced(directory) and synced(outbox) and stat.S_ISDIR(directory.st_mode)
    assert {(e[1], e[2]) for e in events if e[0] == "fsync" and e[3]} == \
        {(directory.st_dev, directory.st_ino), (outbox.st_dev, outbox.st_ino)}, events

    def first(kind, *, dst=None, info=None):
        return next(i for i, e in enumerate(events)
                    if e[0] == kind
                    and (dst is None or e[2] == str(dst))
                    and (info is None or (e[1], e[2]) == (info.st_dev, info.st_ino)))

    swap = first("rename", dst=folder)
    for name, info in written.items():
        assert first("fsync", info=info) < swap, f"{name} was not fsynced before the swap"
    assert first("fsync", info=directory) < swap, "the names were not on disk before the swap"
    assert first("fsync", info=outbox) > swap, "the outbox rename was not made durable after it"


def test_rclone_never_inherits_the_terminal_and_never_asks_for_a_password(remote, monkeypatch):
    """P9 / P15: a periodic write-back runs while the agent owns the terminal. rclone must
    not sit there waiting for somebody to type a password - and must not get the keyboard
    in the first place.

    fd 0 is a pipe the test holds open and never writes to, so a child that tried to read
    would block; with the fix the child is given `/dev/null` instead and says so.
    """
    paths = store.Paths.from_env()
    monkeypatch.setenv("FAKE_RCLONE_RECORD_STDIN", "1")
    read_fd, write_fd = os.pipe()
    saved = os.dup(0)
    os.dup2(read_fd, 0)
    try:
        _save(paths, _header())
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_fd, write_fd):
            os.close(fd)

    log = remote.parent / "stdin.log"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert lines, "there was at least one rclone call"
    assert {line["stdin"] for line in lines} == {"/dev/null"}, lines
    assert all("--ask-password=false" in line["args"] for line in lines), lines
