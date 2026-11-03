"""Bulk downloads of changed session.md (#364, C-1′～C-15).

Above BULK_DOWNLOAD_MIN still-needed sessions, `sync` fetches every session.md with
one list-style `copy --include "*/session.md"` into `<cache>/download-staging`, then
copies only the files whose md5 matches the listing into the mirror. Below it, the old
`--files-from --no-traverse` run is unchanged. The staging copy is kept between runs,
so a run that was killed resumes instead of fetching everything again.

Nothing here reaches Drive or the user's own files: the fake rclone over a temporary
folder, as in test_cache.py.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: refuse to run outside an isolated pytest)

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agora import header as h
from agora import store

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """A Drive that is a folder, and an rclone that is the fake in tests/fakes."""
    root = tmp_path / "remote"
    root.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(root))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return root


def sessions_on(drive: Path) -> Path:
    return drive / "agora" / "sessions"


def calls(drive: Path) -> list[list[str]]:
    log = drive.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def copy_rows(drive: Path, mark: int = 0) -> list[list[str]]:
    """The `copy` calls after `mark` (`copyto` is another command, so not one)."""
    return [c for c in calls(drive)[mark:] if "copy" in c]


def files_from_calls(drive: Path, mark: int = 0) -> list[list[str]]:
    return [c for c in copy_rows(drive, mark) if "--files-from" in c]


def bulk_calls(drive: Path, mark: int = 0) -> list[list[str]]:
    return [c for c in copy_rows(drive, mark) if "--files-from" not in c]


def copyto_calls(drive: Path, mark: int = 0) -> list[list[str]]:
    return [c for c in calls(drive)[mark:] if "copyto" in c]


def copyto_sources(drive: Path, mark: int = 0) -> list[str]:
    """The remote names the `copyto` calls after `mark` fetched."""
    return [c[c.index("copyto") + 1] for c in copyto_calls(drive, mark)]


def copies_mark(drive: Path) -> int:
    log = drive.parent / "copies.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def copied_since(drive: Path, mark: int) -> list[str]:
    """Every name a copies.log row after `mark` carried, as `<ulid>/session.md`."""
    log = drive.parent / "copies.log"
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [f["name"] for row in rows[mark:] for f in row["files"]]


def _header(title="CSV 規劃"):
    ulid = h.new_ulid()
    return {"type": "Session", "title": title, "tags": [], "refs": [], "case": None,
            "id": f"agora:{ulid}",
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                      "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                      "source": {"agent": "opencode", "session_id": "ses_source",
                                 "created_at": "2026-10-01T00:00:00Z"}}}


def session_on_drive(paths, title="CSV 規劃", body="## user\n把 CSV 轉成 Markdown 表格\n") -> str:
    """One finished session really on the fake Drive, with its raws, and nothing local."""
    hdr = _header(title)
    store.stage(paths, hdr, body, b'{"x": 1}')
    store.upload_batch(store.Drive(paths), paths)
    return hdr["id"].split(":", 1)[1]


def strip_raws(drive: Path, ulid: str) -> None:
    """Leave session.md alone: with its raws gone the session is unfinished (#119)."""
    folder = sessions_on(drive) / ulid
    for path in folder.iterdir():
        if path.name != "session.md":
            path.unlink()


def queue_trash(paths, ulid: str) -> None:
    """A session deleted here whose folder is still on Drive (the trash queue)."""
    paths.trash_queue.mkdir(parents=True, exist_ok=True)
    (paths.trash_queue / ulid).write_text("", encoding="utf-8")


def staged(paths, ulid: str) -> Path:
    return paths.staging / ulid / "session.md"


def mirror_file(paths, ulid: str) -> Path:
    return paths.mirror / ulid / "session.md"


def rebuild(paths) -> store.Index:
    """An index built from the mirror alone, as after a deleted database."""
    for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm"):
        (paths.cache / name).unlink(missing_ok=True)
    return store.Index(paths)


def drop_local_state(paths) -> None:
    """What a killed process leaves behind: no index, no mirror."""
    import shutil
    for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm"):
        (paths.cache / name).unlink(missing_ok=True)
    shutil.rmtree(paths.mirror, ignore_errors=True)


# --- the fake itself (5.1): without this, C-2～C-4′ would pass while nothing is copied


def test_fake_rclone_remote_copy_moves_bytes_and_honours_include(drive):
    """A `copy` from the remote with no `--files-from` is a download, not a silent no-op.

    The old branch treated it as an upload from a local `gdrive:sessions` that does not
    exist: nothing was copied and rc was 0, and only the per-file md5 check in `sync`
    hid it. This is the test that would have been red then (probe output in the report).
    """
    ulid, other = h.new_ulid(), h.new_ulid()
    folder = sessions_on(drive) / ulid
    folder.mkdir(parents=True)
    (folder / "session.md").write_text("one\n", encoding="utf-8")
    (folder / "raw-abc.json").write_text("{}\n", encoding="utf-8")
    (sessions_on(drive) / other).mkdir(parents=True)
    (sessions_on(drive) / other / "session.md").write_text("two\n", encoding="utf-8")
    wrapper = os.environ["AGORA_RCLONE"]

    def run(dest: Path, *extra: str, env: dict | None = None):
        return subprocess.run(
            [wrapper, "--config", str(drive.parent / "conf"), "--ask-password=false",
             "--drive-root-folder-id", "agora", "copy", "gdrive:sessions", str(dest), *extra],
            capture_output=True, text=True, env=env)

    dest = drive.parent / "staging"
    proc = run(dest, "--include", "*/session.md", "--fast-list",
               "--transfers", "16", "--checkers", "16")
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == sorted(
        [f"{ulid}/session.md", f"{other}/session.md"]), "only session.md, and really copied"

    # --checksum: what is already the same is not copied again (the resumable part)
    same = dest / ulid / "session.md"
    before = same.stat().st_mtime_ns
    assert run(dest, "--include", "*/session.md", "--checksum").returncode == 0
    assert same.stat().st_mtime_ns == before

    # FAKE_RCLONE_BULK_PARTIAL: a run killed halfway leaves what it got, and exits 1
    half = drive.parent / "half"
    proc = run(half, "--include", "*/session.md", "--checksum",
               env={**os.environ, "FAKE_RCLONE_BULK_PARTIAL": "1"})
    assert proc.returncode == 1
    assert len([p for p in half.rglob("*") if p.is_file()]) == 1


# --- C-1′, C-13: below the threshold the old run, untouched


def test_below_the_threshold_one_files_from_copy_and_no_include(drive, monkeypatch):
    """C-1′: 3 changed, threshold 10 -> exactly one `copy --no-traverse --files-from`."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 10)
    mark, copies = len(calls(drive)), copies_mark(drive)

    store.sync(paths)

    assert len(files_from_calls(drive, mark)) == 1
    call = files_from_calls(drive, mark)[0]
    assert "--no-traverse" in call and "--include" not in call
    assert not bulk_calls(drive, mark), "the list-style path is not taken below the threshold"
    assert sorted(copied_since(drive, copies)) == sorted(f"{u}/session.md" for u in ulids)
    assert all(mirror_file(paths, u).is_file() for u in ulids)


# --- C-2, C-13: at or above it, one list-style copy and no per-file fallback


def test_at_the_threshold_one_list_style_copy_and_no_copyto(drive, monkeypatch):
    """C-2/C-13: 3 changed, threshold 2 -> one `copy --include ... --checksum ...`, no copyto."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    paths.staging.mkdir(parents=True, exist_ok=True)
    orphan = h.new_ulid()                  # shadow-only: never in this listing
    (paths.staging / orphan).mkdir(parents=True)
    staged(paths, orphan).write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    mark, copies = len(calls(drive)), copies_mark(drive)

    store.sync(paths)

    assert not files_from_calls(drive, mark), "no --files-from on the list-style path"
    assert len(bulk_calls(drive, mark)) == 1
    call = bulk_calls(drive, mark)[0]
    assert call[call.index("--include") + 1] == "*/session.md"
    assert "--fast-list" in call and "--checksum" in call
    assert call[call.index("--transfers") + 1] == "16"
    assert call[call.index("--checkers") + 1] == "16"
    assert not copyto_calls(drive, mark), "the batch really had every file; nothing was patched up"
    assert all(mirror_file(paths, u).is_file() for u in ulids)
    assert sorted(copied_since(drive, copies)) == sorted(f"{u}/session.md" for u in ulids)
    assert not (paths.staging / orphan).exists(), "a complete listing trims the shadow-only folder"


# --- C-3′, G3: the mirror gets only `changed`, and only at the listed md5


def test_only_changed_sessions_reach_the_mirror_while_the_shadow_has_them(drive, monkeypatch):
    """C-3′: an unfinished session and one queued for the trash stay out of the mirror,
    while the list-style copy has their session.md in the shadow (G3, #119)."""
    paths = store.Paths.from_env()
    normal = session_on_drive(paths)
    unfinished = session_on_drive(paths, title="未寫完")
    trashing = session_on_drive(paths, title="要丟的")
    strip_raws(drive, unfinished)
    queue_trash(paths, trashing)
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)

    store.sync(paths, kick=False)         # learns that `unfinished` is not written yet
    session_on_drive(paths, title="新的一")
    session_on_drive(paths, title="又一則")

    store.sync(paths, kick=False)         # a bulk round: the whole remote into the shadow

    assert mirror_file(paths, normal).is_file()
    assert not mirror_file(paths, unfinished).exists(), "an unfinished session is never indexed (G3)"
    assert not mirror_file(paths, trashing).exists(), "queued for the trash: not pulled back"
    assert staged(paths, unfinished).is_file(), "the shadow really has it; only the mirror must not"
    assert staged(paths, trashing).is_file()
    known = store.Index(paths).known()
    assert normal in known and unfinished not in known and trashing not in known


# --- C-4′: the shadow lives on, and is not part of the mirror


def test_the_shadow_stays_after_a_successful_bulk_download(drive, monkeypatch):
    """C-4′ (first half): `<cache>/download-staging` keeps its files, and an index rebuilt
    from the mirror does not see them."""
    paths = store.Paths.from_env()
    session_on_drive(paths)
    session_on_drive(paths)
    shadow_only = session_on_drive(paths, title="要丟的")
    queue_trash(paths, shadow_only)
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)

    store.sync(paths, kick=False)         # the trash queue must still be on Drive here

    assert paths.staging.is_dir()
    assert staged(paths, shadow_only).is_file(), "kept for the next run, not deleted after use"
    assert not mirror_file(paths, shadow_only).exists()
    known = rebuild(paths).known()
    assert known, "the mirror's own sessions rebuild the index"
    assert shadow_only not in known, "rebuild_from_mirror only reads mirror/*/session.md"


# --- C-4′, C-15: trimming the shadow


def test_a_complete_listing_removes_shadow_folders_drive_no_longer_has(drive):
    """C-4′ (second half): a folder in the shadow that is not in a whole listing goes."""
    paths = store.Paths.from_env()
    orphan = h.new_ulid()
    (paths.staging / orphan).mkdir(parents=True)
    staged(paths, orphan).write_text("gone on Drive\n", encoding="utf-8")
    stray = paths.staging / "leftover.tmp"
    stray.write_text("x", encoding="utf-8")
    session_on_drive(paths)

    store.sync(paths)

    assert not (paths.staging / orphan).exists()
    assert not stray.exists()


def test_an_incomplete_listing_leaves_the_shadow_alone(drive, capsys):
    """C-4′/C-15: `sessions/` not there means "no complete listing" - trim nothing.

    "Whole" in the code is one thing: `list_sessions()` came back (`missing` is False).
    A listing that silently dropped a live folder (a #152 kind of day) is still
    `missing=False`: that folder's staging copy is trimmed this round and fetched again
    next round. The staging copy is a cache, so the cost is one download - the mirror,
    the index and the marks never depend on it.
    """
    paths = store.Paths.from_env()
    orphan = h.new_ulid()
    (paths.staging / orphan).mkdir(parents=True)
    staged(paths, orphan).write_text("keep me\n", encoding="utf-8")

    store.sync(paths)

    assert "Drive 上找不到 sessions/" in capsys.readouterr().err
    assert staged(paths, orphan).is_file(), "a listing that is not whole must not authorise a delete"


def test_trim_does_not_wait_for_another_process_writing_the_shadow(drive):
    """C-15: non-blocking - a held lock skips this round's trim, and the next one trims."""
    paths = store.Paths.from_env()
    session_on_drive(paths)
    store.sync(paths)                     # a complete, quiet first listing
    orphan = h.new_ulid()
    (paths.staging / orphan).mkdir(parents=True)
    staged(paths, orphan).write_text("old\n", encoding="utf-8")

    with open(paths.staging_lock, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store.sync(paths)                # must not wait and must not report anything
        assert staged(paths, orphan).is_file(), "somebody is writing there: leave this round alone"

    store.sync(paths)
    assert not (paths.staging / orphan).exists()


# --- C-5′: a failed list-style run still publishes what the shadow got right


def test_publishing_goes_through_copy_atomic(drive, monkeypatch):
    """中-1 (architect): the mirror write uses the module's one atomic copy (D-13)."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    seen = []
    real = store.copy_atomic

    def spy(src, dst, **kw):
        seen.append(Path(dst))
        return real(src, dst, **kw)

    monkeypatch.setattr(store, "copy_atomic", spy)

    store.sync(paths)

    assert sorted(seen) == sorted(mirror_file(paths, u) for u in ulids), \
        "_publish writes the mirror through copy_atomic, not a second copy of it"


def test_a_local_write_failure_falls_back_to_the_per_file_copy(drive, monkeypatch, capsys):
    """M1: a local write that fails (a full disk) must not abort sync.

    It becomes a StoreError, so `sync` says「批次下載失敗」and the per-file check fetches
    the sessions again - the mirror and the index still end up complete.
    """
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)

    def full_disk(src, dst, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "copy_atomic", full_disk)
    mark = len(calls(drive))

    store.sync(paths)                     # must not raise

    err = capsys.readouterr().err
    assert "批次下載失敗，改成逐一下載" in err
    assert "No space left on device" in err
    assert len(copyto_calls(drive, mark)) == 3, "the per-file check fetched them instead"
    assert set(ulids) <= set(store.Index(paths).known())


def test_a_publish_failure_does_not_hide_the_download_failure(drive, monkeypatch, capsys):
    """M1, C-5′: inside the failed-batch path the download's error is the one reported.

    Some files made it into the shadow before the copy died; writing one of them into
    the mirror fails too. The person still has to see why the batch failed (the rclone
    error), not just the local one.
    """
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    monkeypatch.setenv("FAKE_RCLONE_BULK_PARTIAL", "2")

    def full_disk(src, dst, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "copy_atomic", full_disk)

    store.sync(paths)                     # must not raise

    err = capsys.readouterr().err
    assert "批次下載失敗，改成逐一下載" in err
    assert "No space left on device" not in err, "the download's error is the one to report"
    assert set(ulids) <= set(store.Index(paths).known())


def test_a_failed_bulk_download_falls_back_to_one_copyto_per_session(drive, monkeypatch, capsys):
    """C-5′: the message is unchanged, and every session still lands in mirror and index."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "--include")
    mark = len(calls(drive))

    store.sync(paths)

    assert "批次下載失敗，改成逐一下載" in capsys.readouterr().err
    assert len(copyto_calls(drive, mark)) == 3
    known = store.Index(paths).known()
    for ulid in ulids:
        assert store.md5_file(mirror_file(paths, ulid)) == known[ulid]


def test_a_partial_bulk_download_does_not_fetch_the_files_it_already_has(drive, monkeypatch, capsys):
    """C-5′: files whose md5 already matches are published from the shadow, not copied again."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    monkeypatch.setenv("FAKE_RCLONE_BULK_PARTIAL", "2")
    mark = len(calls(drive))

    store.sync(paths)

    assert "批次下載失敗，改成逐一下載" in capsys.readouterr().err
    got = {p.parent.name for p in paths.staging.glob("*/session.md")}
    assert len(got) == 2, "two made it into the shadow before the failure"
    fetched = copyto_sources(drive, mark)
    assert sorted(fetched) == sorted(f"gdrive:sessions/{u}/session.md" for u in set(ulids) - got)
    assert store.Index(paths).known().keys() >= set(ulids)


# --- C-6: md5, not the shadow's presence, decides what is published


def test_a_session_changed_after_the_listing_is_fetched_again_once(drive, monkeypatch, capsys):
    """C-6: the shadow has bytes that no longer match the listing -> that one session is
    copied again (one copyto), and the index records the listed md5 as it does today."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    target = ulids[0]
    original = (sessions_on(drive) / target / "session.md").read_text(encoding="utf-8")
    listed_md5 = hashlib.md5(original.encode("utf-8")).hexdigest()
    later = original + "\n## user\n別台機器剛改的\n"
    (drive.parent / "clean.json").write_text(json.dumps({
        "after": "lsjson", "folder": f"sessions/{target}",
        "remove": ["session.md"], "add": {"session.md": later}}), encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_CLEAN_AFTER", str(drive.parent / "clean.json"))
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    mark = len(calls(drive))
    at_fallback = {}
    real_download = store.Drive.download

    def spy_download(self, ulid, name, local):
        at_fallback[ulid] = local.exists()   # what the mirror already held before the copyto
        return real_download(self, ulid, name, local)

    monkeypatch.setattr(store.Drive, "download", spy_download)

    store.sync(paths, kick=False)         # the first lsjson after this is the listing itself

    fetched = copyto_sources(drive, mark)
    assert fetched == [f"gdrive:sessions/{target}/session.md"], "only the mismatched one"
    assert at_fallback[target] is False, "a shadow file whose md5 differs must not be published"
    assert mirror_file(paths, target).read_text(encoding="utf-8") == later
    known = store.Index(paths).known()
    assert known[target] == listed_md5, "the index keeps the listed md5, as before"
    assert all(known[u] for u in ulids)


# --- C-7: the mirror's shape, and how a file gets in


def test_the_mirror_keeps_its_shape_and_no_temporary_files_are_left(drive, monkeypatch):
    """C-7′: `<cache>/sessions/<ulid>/session.md`, no `*.tmp`, no shadow inside the mirror."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)

    store.sync(paths)

    assert sorted(p.relative_to(paths.mirror).as_posix()
                  for p in paths.mirror.glob("*/session.md")) == sorted(
        f"{u}/session.md" for u in ulids)
    assert not list(paths.mirror.rglob("*.tmp"))
    assert not (paths.mirror / "download-staging").exists()
    assert not list(paths.mirror.glob(".files-from-*"))


# --- C-8: a folder gone between the listing and the copy


def test_a_session_deleted_between_the_listing_and_the_copy_is_skipped(drive, monkeypatch, capsys):
    """C-8: the missing folder is not in the shadow, its copyto fails, sync goes on."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    gone = ulids[0]
    (drive.parent / "purge.json").write_text(json.dumps({
        "after": "lsjson", "purge": f"sessions/{gone}"}), encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(drive.parent / "purge.json"))
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)

    store.sync(paths, kick=False)         # the listing must be the call that purges the folder

    assert "讀不到，先跳過" in capsys.readouterr().err
    assert not mirror_file(paths, gone).exists()
    known = store.Index(paths).known()
    assert gone not in known
    assert set(ulids[1:]) <= set(known), "the other sessions still went in"


# --- C-9, C-10: the old small paths, and the threshold is a constant


def test_zero_and_one_changed_are_the_old_paths_and_the_threshold_is_a_constant(drive):
    """C-10: 0 changed -> nothing; 1 changed -> one copyto, no copy. C-9: no env knob."""
    paths = store.Paths.from_env()
    session_on_drive(paths)

    mark = len(calls(drive))
    store.sync(paths)                     # first: one changed session -> copyto
    assert len(copyto_calls(drive, mark)) == 1 and not copy_rows(drive, mark)

    mark = len(calls(drive))
    store.sync(paths)                     # hot: nothing changed at all
    assert not copy_rows(drive, mark) and not copyto_calls(drive, mark)

    session_on_drive(paths)
    mark = len(calls(drive))
    store.sync(paths)
    assert len(copyto_calls(drive, mark)) == 1 and not copy_rows(drive, mark)
    assert store.BULK_DOWNLOAD_MIN == 10, "a module constant, not an environment setting"


# --- C-11: resuming from the shadow


def test_a_killed_bulk_download_resumes_instead_of_fetching_everything(drive, monkeypatch):
    """C-11: round 1 was killed after one file; round 2's list-style copy fetches the rest,
    and a later round that starts over finds everything already in the shadow."""
    paths = store.Paths.from_env()
    ulids = [session_on_drive(paths) for _ in range(3)]
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    monkeypatch.setenv("FAKE_RCLONE_BULK_PARTIAL", "1")
    store.sync(paths)                     # the copy dies after one file; the rest goes copyto

    # What a killed process leaves: the shadow, and no index/mirror of its own.
    drop_local_state(paths)
    monkeypatch.delenv("FAKE_RCLONE_BULK_PARTIAL")
    kept = {p.parent.name for p in paths.staging.glob("*/session.md")}
    assert len(kept) == 1
    copies = copies_mark(drive)

    store.sync(paths)

    assert sorted(copied_since(drive, copies)) == sorted(
        f"{u}/session.md" for u in set(ulids) - kept), "only what the shadow lacked (#checksum)"

    # A successful run keeps the shadow too: starting over again copies nothing.
    drop_local_state(paths)
    copies = copies_mark(drive)
    store.sync(paths)
    assert copied_since(drive, copies) == [], "the shadow survived the successful round"


def test_a_stale_shadow_file_with_the_same_size_and_mtime_is_still_refetched(drive, monkeypatch):
    """L1: `--checksum` is what makes the shadow's bytes, not size+mtime, decide a refetch.

    A killed run can leave a staging file with the same length and modtime as the remote
    one but other bytes. Without `--checksum`, rclone's default comparison would keep it,
    the md5 check would refuse to publish it, and that session would come down again one
    call at a time.
    """
    paths = store.Paths.from_env()
    store.Index(paths)                   # do not rebuild from the mirror
    ulids = [session_on_drive(paths) for _ in range(3)]
    target = ulids[0]
    remote_file = sessions_on(drive) / target / "session.md"
    real = remote_file.read_bytes()
    staged(paths, target).parent.mkdir(parents=True, exist_ok=True)
    staged(paths, target).write_bytes(bytes(reversed(real)))    # same length, other bytes
    stat = remote_file.stat()
    os.utime(staged(paths, target), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    mark = len(calls(drive))

    store.sync(paths)

    assert not copyto_calls(drive, mark), "the stale shadow file was replaced, not published"
    assert store.md5_file(mirror_file(paths, target)) == store.md5_file(remote_file)


# --- C-12: one writer of the shadow at a time


def test_the_staging_lock_is_held_during_the_list_style_copy(drive, monkeypatch):
    """C-12: from inside the copy, a non-blocking flock on `download-staging.lock` fails."""
    paths = store.Paths.from_env()
    for _ in range(3):
        session_on_drive(paths)
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    seen = {}
    real = store._publish

    def spy(paths_, ulid, md5):
        with open(paths_.staging_lock, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                seen["held"] = False
            except OSError:
                seen["held"] = True
        return real(paths_, ulid, md5)

    monkeypatch.setattr(store, "_publish", spy)
    store.sync(paths)
    assert seen.get("held") is True, "the list-style copy and its publishing run under the lock"


def test_a_second_sync_waits_for_the_lock_before_its_list_style_copy(drive, monkeypatch):
    """C-12: a sync that wants to bulk-download queues on the lock instead of copying too."""
    paths = store.Paths.from_env()
    for _ in range(3):
        session_on_drive(paths)
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    d = store.Drive(paths)
    real_list = d.list_sessions
    listed, done = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def list_then_signal():
        out = real_list()
        listed.set()
        return out

    d.list_sessions = list_then_signal

    def worker():
        try:
            store.sync(paths, d)
        except BaseException as e:        # noqa: BLE001 - the test reports it
            errors.append(e)
        finally:
            done.set()

    paths.cache.mkdir(parents=True, exist_ok=True)
    with open(paths.staging_lock, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mark = len(calls(drive))
        thread = threading.Thread(target=worker)
        thread.start()
        assert listed.wait(30), "the listing should happen before the lock is needed"
        assert not bulk_calls(drive, mark), "it is waiting for the lock, not running a second copy"
    assert done.wait(60), "the sync should finish once the lock is free"
    thread.join()
    assert not errors
    assert bulk_calls(drive, mark)


# --- C-14: the threshold counts what the mirror still needs, not `changed`


def test_the_threshold_counts_need_not_changed(drive, monkeypatch):
    """C-14: 3 changed but 2 already correct in the mirror -> 1 needed -> `--files-from`,
    and the list-style path is not taken."""
    paths = store.Paths.from_env()
    store.Index(paths)                   # an index that exists and knows nothing yet
    ulids = [session_on_drive(paths) for _ in range(3)]
    for ulid in ulids[:2]:                # already landed, the index does not know it
        mirror_file(paths, ulid).parent.mkdir(parents=True, exist_ok=True)
        mirror_file(paths, ulid).write_text(
            (sessions_on(drive) / ulid / "session.md").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    mark, copies = len(calls(drive)), copies_mark(drive)

    store.sync(paths)

    assert not bulk_calls(drive, mark), "`need` was 1, under the threshold"
    assert len(files_from_calls(drive, mark)) == 1
    assert copied_since(drive, copies) == [f"{ulids[2]}/session.md"]
    assert set(ulids) <= set(store.Index(paths).known())


def test_an_empty_need_does_not_call_rclone_at_all(drive, monkeypatch):
    """C-1′: every `changed` session is already right in the mirror -> no copy, no copyto."""
    paths = store.Paths.from_env()
    store.Index(paths)                    # exists and knows nothing: the mirror is not rescanned
    ulids = [session_on_drive(paths) for _ in range(3)]
    for ulid in ulids:
        mirror_file(paths, ulid).parent.mkdir(parents=True, exist_ok=True)
        mirror_file(paths, ulid).write_text(
            (sessions_on(drive) / ulid / "session.md").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(store, "BULK_DOWNLOAD_MIN", 2)
    mark = len(calls(drive))

    store.sync(paths)

    assert not copy_rows(drive, mark), "nothing to download, so no `copy` run"
    assert not copyto_calls(drive, mark)
    assert set(ulids) <= set(store.Index(paths).known())
