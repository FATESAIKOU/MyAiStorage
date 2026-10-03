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


def _waiting(paths: store.Paths) -> set[str]:
    """Everything this process is responsible for: uploads first, then deletions."""
    return store.outbox_ulids(paths) | store.queued_for_trash(paths)


def upload_once(paths: store.Paths, drive: store.Drive) -> None:
    """One round: whatever is in the outbox, then the trash queue."""
    store.push_outbox(drive, paths)
    process_trash_queue(paths, drive)


def process_trash_queue(paths: store.Paths, drive: store.Drive) -> None:
    """Move the queued sessions to the Drive trash (design「背景刪除」).

    Empty on purpose: change local-first-writes section 3 fills it in. It is here from
    the start so the loop, the lock and the tests are the ones section 3 will use.
    """


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
    print("[agora] 背景上傳開始", flush=True)
    while True:
        lock = _take(paths)
        if lock is None:
            break               # another uploader has it, and it looks again when done
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
    print(f"[agora] 背景上傳結束，剩下 {len(_waiting(paths))} 筆", flush=True)
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
    _trim_log(paths)
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