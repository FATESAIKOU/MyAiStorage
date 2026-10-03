"""The background uploader (change local-first-writes, tasks 1.1-1.3).

A real child process against the fake rclone: it has to upload, let the lock go, keep
its hands off our terminal, and not inherit the lock a continue is holding.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agora import background, cli, header as h, store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setenv("AGORA_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    # this file is about the detached uploader, so it wants the real thing (the
    # unit-test default is the foreground switch; the two inline tests set it back)
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)
    (tmp_path / "config").mkdir()
    return remote


def _header(title="背景上傳"):
    ulid = h.new_ulid()
    return {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [],
            "case": None,
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                      "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                      "source": {"agent": "opencode", "session_id": "ses_x",
                                 "created_at": "2026-10-01T00:00:00Z"}}}


def _staged(paths) -> str:
    folder = store.stage(paths, _header(), "## user\n把 CSV 轉成 Markdown 表格\n", b'{"x": 1}')
    store.remember(paths, folder)
    return folder.name


def _lock_is_free(paths) -> bool:
    lock = open(paths.state / "upload.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        lock.close()


def _wait_until(what, paths, timeout=30.0) -> None:
    """The child is a separate process, so wait for it rather than sleep hopefully."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if what(paths):
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what.__name__}")


def _outbox_empty(paths) -> bool:
    return not store.outbox_ulids(paths)


def _uploaded(remote: Path):
    def done(paths):
        return _outbox_empty(paths) and _lock_is_free(paths)
    done.__name__ = "the upload to finish"
    return done


def _child_running(paths) -> bool:
    log = paths.state / "upload.log"
    return log.exists() and "背景上傳開始" in log.read_text(encoding="utf-8")


def _child_running_(paths):
    _child_running.__name__ = "the child to start"
    return _child_running(paths)


def test_the_background_uploads_the_outbox_and_lets_the_lock_go(env, tmp_path):
    paths = store.Paths.from_env()
    ulid = _staged(paths)

    assert background.start(paths) == background.STARTED
    _wait_until(_uploaded(env), paths)

    on_drive = env / "agora" / "sessions" / ulid / "session.md"
    assert on_drive.is_file(), "the background should have uploaded it"
    assert _lock_is_free(paths), "the lock must not stay held"


def test_nothing_it_prints_reaches_our_terminal(env, capsys):
    paths = store.Paths.from_env()
    _staged(paths)
    capsys.readouterr()

    background.start(paths)
    _wait_until(_uploaded(env), paths)

    out = capsys.readouterr()
    assert "背景上傳開始" not in out.out and "背景上傳開始" not in out.err
    assert "背景上傳開始" in (paths.state / "upload.log").read_text(encoding="utf-8")


def test_it_does_not_inherit_the_lock_a_continue_is_holding(env, monkeypatch):
    """M3: the child outlives us. A pending lock handed to it would leave `continuing()`
    true for ever, and delete would refuse that session from then on."""
    paths = store.Paths.from_env()
    ulid = _staged(paths)
    _, lock = cli._write_pending(paths, {"agora_id": f"agora:{ulid}", "agent": "opencode",
                                         "agent_session_id": "ses_x", "dir": "/tmp",
                                         "before_count": 0, "parent": {"id": f"agora:{ulid}"}})
    monkeypatch.setenv("FAKE_RCLONE_DELAY", "2")
    background.start(paths)
    _wait_until(_child_running_, paths)

    lock.close()                        # our agora is gone; the agent it started is not
    assert not store.continuing(paths, ulid), "the child is holding our pending lock"


def test_it_is_launched_the_way_the_lock_needs(env, monkeypatch):
    """M3: its own session (so Esc cannot reach it), no keyboard, our environment as it
    is, our stdout untouched, and the fds closed - an inherited pending lock would keep
    `continuing()` true after we are gone."""
    paths = store.Paths.from_env()
    seen = {}
    real = subprocess.Popen

    def spy(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    assert background.start(paths) == background.STARTED

    assert seen["argv"][1:] == ["-m", "agora.background"]
    assert seen["start_new_session"] is True
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["close_fds"] is True
    assert seen["env"] is None                      # inherited as it is, not rebuilt
    assert seen["stdout"] is seen["stderr"]         # both the log file, not our terminal
    assert seen["stdout"] not in (sys.stdout, sys.stderr)


def test_inline_runs_the_same_thing_in_the_foreground(env, monkeypatch):
    """The switch the unit tests lean on: no child, Drive already has it on return."""
    paths = store.Paths.from_env()
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    ulid = _staged(paths)

    assert background.start(paths) == background.FINISHED
    assert (env / "agora" / "sessions" / ulid / "session.md").is_file()
    assert not store.outbox_ulids(paths)
    assert not (paths.state / "upload.log").exists(), "inline writes no log"


def test_inline_says_failed_when_the_upload_did_not_get_through(env, monkeypatch):
    """N1: inline is only a different place to run, not a softer answer - a failure is
    still exit 3 for the caller, with the message it has always used."""
    paths = store.Paths.from_env()
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    ulid = _staged(paths)

    assert background.start(paths) == background.FAILED
    assert ulid in store.outbox_ulids(paths)      # still there for a later try


def test_a_sync_starts_the_uploader_instead_of_uploading(env, monkeypatch):
    """N3: the command that notices a waiting outbox starts the uploader; it does not
    stand there sending it - that wait is what T3 is here to remove."""
    paths = store.Paths.from_env()
    ulid = _staged(paths)
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)
    started, uploaded = [], []
    monkeypatch.setattr(background, "start", lambda paths=None: started.append(1))
    monkeypatch.setattr(store, "push_outbox",
                        lambda *a, **k: uploaded.append(1) or [])

    assert store.sync(paths) is not None
    assert started, "sync should hand the outbox to the uploader"
    assert not uploaded, "sync must not upload in the foreground any more"
    assert ulid in store.outbox_ulids(paths)


def test_a_sync_with_nothing_waiting_starts_nothing(env, monkeypatch):
    paths = store.Paths.from_env()
    started = []
    monkeypatch.setattr(background, "start", lambda paths=None: started.append(1))
    store.sync(paths)
    assert not started


def test_a_second_uploader_skips_while_the_lock_is_held(env):
    """M1: one upload at a time. Whoever cannot take the lock leaves the outbox alone -
    the holder looks again when it lets go, so nothing is left for the next command."""
    paths = store.Paths.from_env()
    lock = store.hold_upload_lock(paths)       # we are the uploader that got there first
    ulid = _staged(paths)
    try:
        assert background.run(paths) == 0       # cannot take it, so it returns at once
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    assert ulid in store.outbox_ulids(paths)     # still waiting, and nobody sent it
    assert not (env / "agora" / "sessions" / ulid).exists()


def test_a_session_staged_while_it_runs_still_goes_up(env, monkeypatch):
    """Spec「背景上傳」: staged after the background started, it must not wait for the
    next command - neither the round it is in the middle of nor the one after."""
    paths = store.Paths.from_env()
    monkeypatch.setenv("FAKE_RCLONE_DELAY", "1.5")
    first = _staged(paths)
    background.start(paths)
    _wait_until(_child_running_, paths)
    second = _staged(paths)                     # arrives while the child is working

    _wait_until(_uploaded(env), paths)
    for ulid in (first, second):
        assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_it_looks_again_after_letting_the_lock_go(env, monkeypatch):
    """P1: a session that appears in the instant between the last check and the release
    must still go up in this process, not wait for the next command."""
    paths = store.Paths.from_env()
    first = _staged(paths)
    late = []

    def on_release(paths_):
        late.append(_staged(paths_))      # in the gap: the last check is already past

    monkeypatch.setattr(store, "hold_upload_lock", _release_hook(on_release))
    assert background.run(paths) == 0
    assert late, "the test never reached the release"

    assert not store.outbox_ulids(paths)
    for ulid in (first, late[0]):
        assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def _release_hook(do):
    """A lock that runs `do` the moment it is let go - the gap after the last check,
    which is the one only the look-again can cover. Releasing is closing the file (E2)."""
    real = store.hold_upload_lock
    done = []

    def spy(paths, *args, **kwargs):
        lock = real(paths, *args, **kwargs)
        if lock is None:
            return None
        close = lock.close

        def closing():
            close()
            if not done:
                done.append(True)
                do(store.Paths.from_env())

        lock.close = closing
        return lock

    return spy


PROBE = """
import sys
sys.path.insert(0, {tests!r})
from test_background import _staged
from agora import background, store
_staged(store.Paths.from_env())
print(background.start(store.Paths.from_env()))
"""


def test_an_edit_that_lands_mid_upload_is_still_sent_by_this_process(env, monkeypatch):
    """P2 / spec「上傳中又改了同一個」: the same session, a new version, staged after we
    sent ours. Comparing ULIDs would call that "nothing new" and leave it for the next
    command; the version is what tells the two apart."""
    paths = store.Paths.from_env()
    paths = store.Paths.from_env()
    ulid = _staged(paths)
    header = store.read_entry(paths.outbox / ulid)
    body2 = "## user\n接著聊的那一段\n"
    real = store._listing_with_md5

    def listing(drive):
        first = real(drive)
        if not getattr(listing, "done", False):
            listing.done = True      # our version is on Drive; now the edit lands
            store.stage(paths, header, body2, b'{"x": 2}')
        return first

    monkeypatch.setattr(store, "_listing_with_md5", listing)
    assert background.run(paths) == 0

    assert not store.outbox_ulids(paths), "the new version should have gone up too"
    check = paths.state / "check.md"
    store.Drive(paths).download(ulid, "session.md", check)
    assert "接著聊的那一段" in check.read_text(encoding="utf-8")


def test_a_reader_of_our_stdout_gets_eof_before_the_background_is_done(env, monkeypatch):
    """P4: a pipe reading a command's stdout has to reach EOF when the command exits,
    even though the background it started is still uploading. `capsys` cannot see this -
    it only catches Python-level writes - so run a real command with a real pipe."""
    monkeypatch.setenv("FAKE_RCLONE_DELAY", "5")      # the upload is still going...
    tests = str(Path(__file__).resolve().parent)
    proc = subprocess.Popen([sys.executable, "-c", PROBE.format(tests=tests)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(timeout=60)            # ...and EOF came anyway
    assert proc.returncode == 0, err
    assert out.strip() == background.STARTED
    _wait_until(_uploaded(env), store.Paths.from_env())   # and it did finish afterwards


def test_a_log_over_a_megabyte_starts_again(env):
    """E9: over the limit the log is emptied rather than trimmed to a tail. It belongs
    to a process nobody is watching, and a tail nobody reads is not worth the code."""
    paths = store.Paths.from_env()
    paths.state.mkdir(parents=True, exist_ok=True)
    log = paths.state / "upload.log"
    log.write_bytes(b"x" * (background.LOG_MAX + 1000) + b"TAIL")

    background._trim_log(paths)
    assert log.read_bytes() == b""

# --- section 3: the trash queue (design「背景刪除」) ----------------------------


def _queued(paths, count: int = 1) -> list[str]:
    """`count` ULIDs on Drive, deleted here: gone locally, waiting for the trash.

    All of them are staged first and uploaded in one round, so a test can queue two
    sessions and then make one of them fail - the uploader that gets them onto Drive
    would otherwise have purged the first one already.
    """
    ulids = []
    for n in range(count):
        folder = store.stage(paths, _header(title=f"排隊{n}"), "## user\n排隊\n", b'{"x": 1}')
        store.remember(paths, folder)
        ulids.append(folder.name)
    assert background.run(paths) == 0, "the setup upload has to finish first"
    paths.trash_queue.mkdir(parents=True, exist_ok=True)
    for ulid in ulids:
        store.forget_local(paths, ulid)
        (paths.trash_queue / ulid).write_text("", encoding="utf-8")
    return ulids


def _purges(env: Path) -> list[str]:
    """The ULIDs the fake was asked to purge, in order (one call per session)."""
    out = []
    for line in (env.parent / "calls.log").read_text(encoding="utf-8").splitlines():
        args = json.loads(line)
        if "purge" in args:                 # the fake logs the raw argv, --config first
            out.append(args[args.index("purge") + 1])
    return out


def test_the_background_purges_each_queued_session_and_forgets_it(env):
    paths = store.Paths.from_env()
    first, second = _queued(paths, 2)

    assert background.run(paths) == 0

    assert store.queued_for_trash(paths) == set()
    on_drive = env / "agora" / "sessions"
    assert not (on_drive / first).exists() and not (on_drive / second).exists()
    # one purge per session, not one per file: the Drive trash gets whole folders
    assert sorted(_purges(env)) == sorted([f"gdrive:sessions/{first}", f"gdrive:sessions/{second}"])


def test_a_trash_that_fails_stays_in_the_queue_for_the_next_command(env, monkeypatch, capsys):
    """spec「背景刪除」: the uploader is started again by the very next command, so a
    failure has to leave the ULID where it is - and has to be said out loud."""
    paths = store.Paths.from_env()
    (ulid,) = _queued(paths)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", ulid)      # only this session's purge

    assert background.run(paths) == 0
    assert store.queued_for_trash(paths) == {ulid}
    assert (env / "agora" / "sessions" / ulid).is_dir(), "still on Drive"
    assert "垃圾桶失敗" in capsys.readouterr().err

    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    assert background.run(paths) == 0
    assert store.queued_for_trash(paths) == set()
    assert not (env / "agora" / "sessions" / ulid).exists()


def test_a_failed_trash_whose_folder_is_gone_counts_as_deleted(env, monkeypatch):
    """S1-4b, kept where it was measured: rclone says "not found" for a wrong folder id
    and for a token that cannot see anything, so a listing has to agree before we
    believe the session is deleted."""
    import shutil
    paths = store.Paths.from_env()
    (ulid,) = _queued(paths)
    shutil.rmtree(env / "agora" / "sessions" / ulid)      # gone before the purge ran
    monkeypatch.setenv("FAKE_RCLONE_FAIL", ulid)

    assert background.run(paths) == 0
    assert store.queued_for_trash(paths) == set()


def test_one_session_that_cannot_be_deleted_does_not_hold_up_the_next(env, monkeypatch):
    paths = store.Paths.from_env()
    stuck, after = _queued(paths, 2)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", stuck)

    assert background.run(paths) == 0
    assert store.queued_for_trash(paths) == {stuck}
    assert not (env / "agora" / "sessions" / after).exists(), "the one behind it still went"
