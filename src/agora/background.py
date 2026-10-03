"""The background uploader: one detached process, one at a time (change local-first-writes).

Started as `[sys.executable, "-m", "agora.background"]`, never through `cli.main()`:
it must not run `recover_pending`, print reminders, show up in `--help`, or spawn
itself (design M3). It takes the lock at `<state>/upload.lock`, works until the outbox
and the trash queue are empty, and looks once more after letting the lock go: a session
staged while it was running would otherwise wait for the next command (design M1).

Nothing it does reaches the terminal that started it: its output goes to
`<state>/upload.log`, so a pipe reading our stdout still gets its EOF (T2-archive X1).
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

from agora import store

LOG_MAX = 1 << 20            # above this the log is trimmed when the next one starts
LOG_KEEP = 256 << 10         # and this is how much of it is kept

STARTED = "background"       # handed to a detached uploader
FINISHED = "foreground"      # ran right here, and there is nothing left
FAILED = "failed"            # could not start, or (inline) did not get through


def log_path(paths: store.Paths) -> Path:
    return paths.state / "upload.log"


def lock_path(paths: store.Paths) -> Path:
    return paths.state / "upload.lock"


def _trim_log(paths: store.Paths) -> None:
    """Keep the tail: the interesting part of a failure is at the end, and the file
    belongs to a process nobody is watching."""
    path = log_path(paths)
    try:
        if path.stat().st_size <= LOG_MAX:
            return
        with open(path, "rb") as f:
            f.seek(-LOG_KEEP, os.SEEK_END)
            tail = f.read()
    except OSError:
        return
    path.write_bytes(tail)


def _waiting(paths: store.Paths) -> set[tuple[str, str | None]]:
    """What is left, as versions: each entry with the md5 of what we would send.

    Comparing ULIDs is not enough (review P2): a session edited while we were sending it
    stays in the outbox under the same ULID, so "the same set as last time" would read
    as "it is failing, stop" and leave this round's version to the next command, which
    the spec does not allow.
    """
    versions = set()
    for ulid in store.outbox_ulids(paths):
        try:
            versions.add((ulid, store.md5_file(paths.outbox / ulid / "session.md")))
        except OSError:
            versions.add((ulid, None))
    return versions | {(ulid, "trash") for ulid in store.queued_for_trash(paths)}


def upload_once(paths: store.Paths, drive: store.Drive) -> None:
    """One round: whatever is in the outbox, then the trash queue."""
    store.push_outbox(drive, paths)
    process_trash_queue(paths, drive)


def process_trash_queue(paths: store.Paths, drive: store.Drive) -> None:
    """Move the queued sessions to the Drive trash (design「背景刪除」).

    One `purge` per ULID, and the decision is `store.delete_session`'s (S1-4, S1-4b):
    a purge that fails does not mean the folder is gone, so a listing settles it.
    Written out here a second time, those two rules would be two rules.

    A success leaves the queue; a failure stays in it, and the very next command
    starts this loop again (spec). One session that cannot be deleted does not hold
    up the ones behind it.
    """
    for ulid in sorted(store.queued_for_trash(paths)):
        try:
            store.delete_session(paths, drive, ulid)
        except store.StoreError as e:
            store.warn(f"{ulid} 移到 Drive 垃圾桶失敗，還在佇列裡等下一個指令：{e}")
            continue
        (paths.trash_queue / ulid).unlink(missing_ok=True)


def _take(paths: store.Paths):
    """The lock, or None when somebody else is already uploading."""
    lock_path(paths).parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path(paths), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None         # they re-check the outbox when they let go, so ours is theirs
    return lock


def run(paths: store.Paths | None = None) -> int:
    """Upload and delete until there is nothing left, then look once more."""
    paths = paths or store.Paths.from_env()
    print("[agora] 背景上傳開始", file=sys.stderr, flush=True)
    while True:
        lock = _take(paths)
        if lock is None:
            # P8: not a run of our own, so no start/finish pair that reads like one.
            # The holder looks again before it lets go, so ours is its.
            print("[agora] 已經有一個上傳在跑，這一輪不重來", file=sys.stderr, flush=True)
            break
        try:
            seen = None
            while (waiting := _waiting(paths)) and waiting != seen:
                seen = waiting  # nothing new since the last round: it is failing, stop
                upload_once(paths, store.Drive(paths))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        left = _waiting(paths)
        if not left or left == seen:
            break               # empty, or the same ones that just failed: leave them be
    # stderr, like every progress line: stdout carries the ids and nothing else
    print(f"[agora] 背景上傳結束，剩下 {len(_waiting(paths))} 筆", file=sys.stderr, flush=True)
    return 0


def start(paths: store.Paths | None = None) -> str:
    """Hand the outbox to a detached uploader: STARTED, FINISHED or FAILED.

    `AGORA_UPLOAD=inline` runs the very same code in the foreground instead, which is
    what the unit tests use: they assume Drive already has the session when the command
    returns (design「測試開關」). Inline is not a shortcut - the trash queue is handled
    there too, and a failure is FAILED so the caller still answers exit 3 with the
    message it always used (review N1).
    """
    paths = paths or store.Paths.from_env()
    if os.environ.get("AGORA_UPLOAD") == "inline":
        run(paths)
        return FINISHED if not _waiting(paths) else FAILED
    paths.state.mkdir(parents=True, exist_ok=True)
    if not store.uploader_is_running(paths):
        _trim_log(paths)     # P7: rewriting the file under a running uploader loses lines
    try:
        with open(log_path(paths), "ab") as log:
            # env=None: the caller's AGORA_* settings must reach the child, or a test
            # folder would turn into the real one. stdin is DEVNULL so an agent started
            # further down cannot read the user's keys, and the fds are closed so this
            # process does not hand its locks (a continue's pending one) to a process
            # that outlives it - `continuing()` would then never clear (design M3).
            subprocess.Popen([sys.executable, "-m", "agora.background"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True, env=None)
    except OSError:
        return FAILED
    return STARTED


if __name__ == "__main__":
    sys.exit(run())