"""Integration: writes that go to Drive in the background (local-first-writes 5.2).

Real Drive (`agora-test/` only), real rclone, real opencode for a couple of
self-made one-line sessions. The four things 5.2 asks for:

* import a few, the background finishes, Drive has all of them;
* delete a few, the background finishes, Drive has none of them;
* a session written on this machine, deleted by *another* machine (its own
  AGORA_CACHE_DIR / AGORA_STATE_DIR), comes back with `push --not-exist-upload`;
* a session being edited here while another machine deletes it is kept - as a new
  Session of its own, not pushed back into the deleted id (design L7 / N5).

Every wait goes through `wait_uploaded()`: nobody holds the upload lock, and both the
outbox and the trash queue are empty. Cleanup purges only the ULIDs this run printed -
nothing outside `agora-test/` is touched, and no id is deleted that we did not create.

Run: uv run pytest -q -m integration tests/integration/test_background_writes.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time

import pytest

from agora import cli, header as h, store

pytestmark = pytest.mark.integration

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
PROJ = Path("/tmp/agora-it-background/p_專案.v2")
MODEL = os.environ.get("AGORA_TEST_MODEL", "opencode/space-bunny-free")
#: One line, so a session is one exchange and the run is short.
PROMPT = "用一句話回答：CSV 的表頭列怎麼寫？不要寫檔案。"
#: A word that is in every session we make, so a search finds exactly ours.
MARK = "表格"


@pytest.fixture()
def env(tmp_path, monkeypatch, capsys):
    if not REAL_CONF.exists():
        pytest.fail("需要 ~/.config/agora/rclone.conf（整合測試不能 skip）")
    if shutil.which("opencode") is None:
        pytest.skip("opencode not found")
    config = tmp_path / "config"
    config.mkdir()
    (config / "rclone.conf").symlink_to(REAL_CONF)
    monkeypatch.setenv("AGORA_CONFIG", str(config))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.delenv("AGORA_RCLONE", raising=False)
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)   # the real detached uploader
    # the adapter has to read the same opencode database the sessions below go into
    monkeypatch.setenv("HOME", str(REAL_HOME))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    if PROJ.exists():
        shutil.rmtree(PROJ)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=PROJ, check=True)
    created = {"ulids": [], "sessions": [], "locks": []}
    here = store.Paths.from_env()
    theirs = store.Paths(config=config, cache=tmp_path / "other" / "cache",
                         state=tmp_path / "other" / "state")
    yield {"proj": PROJ.resolve(), "created": created, "capsys": capsys,
           "here": here, "theirs": theirs, "other": tmp_path / "other"}
    # a test that failed while holding the lock would make every wait here sit out
    # its timeout (review J4)
    for held in created.pop("locks", []):
        held.close()
    # A background uploader still running would put things on Drive *after* the purge
    # below, so both machines are given a chance to finish first (review I3).
    for paths in (here, theirs):
        try:
            wait_uploaded(paths, timeout=60)
        except AssertionError:
            pass
    ulids = set(created["ulids"])
    index = store.Index(here)
    for root in ulids:                       # anything this run rescued (review I3)
        ulids.update(index.children(root))
    drive = store.Drive(here)
    for ulid in sorted(ulids):               # only what this run printed or created
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", drive.folder_id(),
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
    for sid in created["sessions"]:
        subprocess.run(["opencode", "session", "delete", sid], cwd=str(PROJ),
                       env={**os.environ, "PWD": str(PROJ)}, capture_output=True)
    shutil.rmtree(PROJ.parent, ignore_errors=True)


def wait_uploaded(paths, timeout: float = 300.0, settle: int = 2) -> None:
    """Wait until the background uploader is through: nobody holds the upload lock, and
    the outbox and the trash queue are both empty (spec「delete 先在本機」).

    All three, and the lock first. And twice in a row: a uploader that has been started
    but has not taken the lock yet looks exactly like "nobody is working on it" to a
    single look, which is how this helper used to let a test walk away one round early
    (review T3-it I4 - the same shape as R6).

    When the lock is free and something is still waiting, it gives the uploader a nudge
    (`store.kick_uploader`, which is what the next command would do). Waiting is for the
    uploader, not for a command that happens to come along; without the nudge the test
    depends on a background process started earlier happening to take the lock after the
    test let go of it (review J1/J3).
    """
    deadline = time.monotonic() + timeout
    quiet = 0
    nudged = 0.0
    while time.monotonic() < deadline:
        held = store.uploader_is_running(paths)
        waiting = bool(store.outbox_ulids(paths) or store.queued_for_trash(paths))
        if not held and waiting and time.monotonic() - nudged > 5.0:
            store.kick_uploader(paths)
            nudged = time.monotonic()
        done = not held and not waiting
        quiet = quiet + 1 if done else 0
        if quiet >= settle:
            return
        time.sleep(0.5)
    # A uploader that has been started but has not taken the lock yet looks exactly
    # like "nobody is working on it" to a poll like this one (review T3-it I4 / R6),
    # so the message has to be able to say which of the three it was.
    raise AssertionError(
        f"背景上傳器 {timeout:.0f} 秒還沒做完："
        f"鎖={store.uploader_is_running(paths)}"
        f" outbox={sorted(store.outbox_ulids(paths))}"
        f" 垃圾桶佇列={sorted(store.queued_for_trash(paths))}")


def run_cli(env, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = env["capsys"].readouterr()
    return code, out.out.strip(), out.err


def make_session(env, prompt: str = PROMPT) -> str:
    """One real opencode session: a single exchange, in the test's own project."""
    proc = subprocess.run(
        ["opencode", "run", "-m", MODEL, "--format", "json", prompt],
        cwd=str(env["proj"]), env={**os.environ, "PWD": str(env["proj"]), "HOME": str(REAL_HOME)},
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-400:]
    session_id = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("sessionID"):
            session_id = event["sessionID"]
    assert session_id, "no session id in the run output"
    env["created"]["sessions"].append(session_id)
    return session_id


def import_sessions(env, count: int = 2) -> list[str]:
    """Import `count` self-made sessions; returns their agora ids."""
    out = []
    for n in range(count):
        sid = make_session(env, f"{PROMPT}（第 {n + 1} 則）")
        code, ids, err = run_cli(env, "import", "session", "--agent", "opencode",
                                 "--external-session-id", sid,
                                 "--header", f"title={MARK} 匯入 {n + 1}")
        assert code == 0, err
        out.extend(ids.split())
    env["created"]["ulids"].extend(ulid_of(i) for i in out)
    return out


def drive_has(paths, ulid: str) -> dict | None:
    """What Drive holds for this session: {file name: md5}, or None."""
    return (store.Drive(paths).list_sessions() or {}).get(ulid)


def on_drive(paths, ulid: str) -> bool:
    return drive_has(paths, ulid) is not None


def same_as_local(paths, ulid: str, title_of=None) -> None:
    """Drive's copy is the local one: the same session.md, and the raw it names.

    "Drive has a folder" is not the same as "Drive has what we wrote" - a half-sent
    session.md, or one whose raw is missing or is an older version, is what the local
    mirror exists to make impossible (review I2).
    """
    local = paths.mirror / ulid
    remote = drive_has(paths, ulid)
    assert remote, f"{ulid} 不在 Drive 上"
    assert remote.get("session.md") == store.md5_file(local / "session.md"), \
        f"{ulid} 的 session.md 和本機的不一樣"
    hdr, _body = h.split_document((local / "session.md").read_text(encoding="utf-8"))
    raw = (h.agora_of(hdr).get("raw") or {})
    if raw.get("file"):
        assert (local / raw["file"]).is_file(), "本機沒有那個原始檔"
        assert remote.get(raw["file"]) == raw["md5"], f"{ulid} 的原始檔和本機的不一樣"
    if title_of:
        assert hdr.get("title") == title_of, f"{ulid} 的標題是 {hdr.get('title')!r}"


def ulid_of(agora_id: str) -> str:
    return agora_id.split(":")[1]


def use_machine(env, monkeypatch, which: str) -> store.Paths:
    """Point the environment at one of the two machines in this test.

    `cli.main` reads `AGORA_CACHE_DIR` / `AGORA_STATE_DIR` every time, so this is how
    the second agora on this account is simulated: the same Drive folder, its own
    cache, state, lock and queues. Switching back matters as much as switching over
    (review I1 b).
    """
    paths = env[which]
    monkeypatch.setenv("AGORA_CACHE_DIR", str(paths.cache))
    monkeypatch.setenv("AGORA_STATE_DIR", str(paths.state))
    return paths


def forget_last_sync(paths) -> None:
    """Drop the throttle stamp, so the next command really does list Drive.

    `sync` is throttled for five minutes, and the import a moment ago synced anyway -
    so without this the "cloud does not have it" marker would never move (review I1 c).
    """
    (paths.state / "last-sync").unlink(missing_ok=True)


def hold_our_lock(env, paths):
    """Take this machine's upload lock, so a background uploader cannot start.

    Needed for the rescue scenarios: with the user's own client a single rclone takes
    0.6-0.8 s, so an edit would be on Drive before the other machine could delete it,
    and the scenario would pass or fail by luck (review I1 d).

    The lock is remembered so the fixture can let it go if a test fails while holding it
    (review J4) - otherwise the teardown waits 60 s for an uploader that can never run.
    """
    held = store.hold_upload_lock(paths, blocking=True)
    env["created"].setdefault("locks", []).append(held)
    return held


def test_import_several_and_the_background_puts_them_all_on_drive(env, monkeypatch):
    """5.2: the command is done when the session is safe here; Drive catches up."""
    paths = env["here"]
    held = hold_our_lock(env, paths)     # the background may not start yet (review I1 d)
    ids = import_sessions(env, 2)
    assert store.outbox_ulids(paths), "both are staged here, not on Drive yet"
    held.close()                         # and now the background may
    store.kick_uploader(paths)           # J1: be the command that starts it, not a hope
    wait_uploaded(paths)

    assert store.outbox_ulids(paths) == set()
    assert store.bad_count(paths) == 0, "沒有東西被移到 .bad"
    for agora_id in ids:
        # Drive has what we wrote, not just a folder with that name (review I2)
        same_as_local(paths, ulid_of(agora_id))


def test_delete_several_and_the_background_takes_them_off_drive(env, monkeypatch):
    """5.2: gone from here at once, gone from Drive once the background gets to it."""
    paths = env["here"]
    held = hold_our_lock(env, paths)
    ids = import_sessions(env, 2)
    held.close()
    wait_uploaded(paths)
    for agora_id in ids:
        assert on_drive(paths, ulid_of(agora_id))

    held = hold_our_lock(env, paths)     # so the queue is ours to look at (review I2)
    code, out, err = run_cli(env, "delete", "session", *ids, "--yes")

    assert code == 0 and set(out.split()) == set(ids), err
    # the foreground's half is done before the command returns: not in the list, not
    # searchable, and the outbox no longer holds them
    index = store.Index(paths)
    for agora_id in ids:
        assert index.header(ulid_of(agora_id)) is None
    code, found, _ = run_cli(env, "search", "session", "--filter", f"text~={MARK}")
    assert code == 0 and not any(agora_id in found for agora_id in ids), \
        "刪完就該從搜尋裡消失"
    # read while the lock is still ours: afterwards the uploader may already be purging
    # (review J2)
    assert sorted(store.queued_for_trash(paths)) == sorted(ulid_of(i) for i in ids)
    held.close()

    wait_uploaded(paths)

    assert store.queued_for_trash(paths) == set()
    for agora_id in ids:
        assert not on_drive(paths, ulid_of(agora_id)), f"{agora_id} 還在 Drive 上"


def test_a_session_deleted_elsewhere_comes_back_with_an_explicit_push(env, monkeypatch):
    """5.2 / spec「只在明確要求時才刪或復活」: another machine's delete is undone by
    `push --not-exist-upload`, and by nothing else."""
    paths = use_machine(env, monkeypatch, "here")
    held = hold_our_lock(env, paths)
    (agora_id,) = import_sessions(env, 1)
    held.close()
    ulid = ulid_of(agora_id)
    wait_uploaded(paths)
    assert on_drive(paths, ulid)

    theirs = use_machine(env, monkeypatch, "theirs")
    code, out, err = run_cli(env, "delete", "session", agora_id, "--yes")
    assert code == 0, err
    wait_uploaded(theirs)
    assert not on_drive(theirs, ulid), "另一台刪掉了"

    # back here: nothing of ours is running, so say so (review J1)
    store.kick_uploader(paths)

    # the next sync has to really list Drive, or the marker never moves
    paths = use_machine(env, monkeypatch, "here")
    forget_last_sync(paths)
    code, found, _ = run_cli(env, "search", "session", "--filter", "cloud=no")
    assert code == 0 and agora_id in found, "本機還在，而且標成雲端沒有"

    forget_last_sync(paths)
    code, out, err = run_cli(env, "push", "session", agora_id, "--not-exist-upload")
    assert code == 0, err
    wait_uploaded(paths)

    # it came back whole: the session.md and the raw its header names (review I2)
    same_as_local(paths, ulid)
    # the marker moves on the next *sync*, not on the push itself
    forget_last_sync(paths)
    code, found, _ = run_cli(env, "search", "session", "--filter", "cloud=no")
    assert code == 0 and agora_id not in found, "回到 Drive 之後就不再是雲端沒有的了"


def test_an_edit_deleted_elsewhere_is_kept_as_a_session_of_its_own(env, monkeypatch):
    """5.2 / L7 / N5: the edit is not pushed back into an id another machine deleted -
    that would undo their delete behind their back - so it becomes a new Session whose
    parent is the deleted one, and the deleted one stays deleted."""
    paths = use_machine(env, monkeypatch, "here")
    (agora_id,) = import_sessions(env, 1)
    ulid = ulid_of(agora_id)
    wait_uploaded(paths)             # the import is on Drive before we start editing
    held = hold_our_lock(env, paths)  # and now it is held, so the edit below cannot
                                     # go up before the other machine deletes it -
                                     # waiting here would deadlock against our own lock
    code, _, err = run_cli(env, "edit", "session", agora_id,
                           "--header", f"title={MARK} 改過")
    assert code == 0, err
    assert store.outbox_ulids(paths) == {ulid}, "還沒上傳的新版本在 outbox"

    theirs = use_machine(env, monkeypatch, "theirs")
    code, _, err = run_cli(env, "delete", "session", agora_id, "--yes")
    assert code == 0, err
    wait_uploaded(theirs)
    assert not on_drive(theirs, ulid), "另一台刪掉了，而我們的改動還沒上傳"

    # now let our uploader meet it: the lock goes, and a command starts it
    held.close()
    paths = use_machine(env, monkeypatch, "here")
    forget_last_sync(paths)
    run_cli(env, "search", "session", "--filter", "cloud=no")
    wait_uploaded(paths)

    assert not on_drive(paths, ulid), "被別台刪掉的那一筆沒有被傳回去"
    # the sync that started the uploader listed Drive before Y was up, so Y carries the
    # 「雲端沒有」marker until the next sync sees it there - and `children` only counts a
    # child Drive has (T1 3.4). One more sync, then read it.
    forget_last_sync(paths)
    run_cli(env, "search", "session", "--filter", "cloud=no")
    index = store.Index(paths)
    new_ids = index.children(ulid)          # parents live under agora.parents (review I1 e)
    assert len(new_ids) == 1, f"應該剛好另外存成一個新的，現在有 {new_ids}"
    env["created"]["ulids"].extend(new_ids)   # and clean it up even if the next line fails
    # what was rescued is that edit, not the version the other machine deleted
    same_as_local(paths, new_ids[0], title_of=f"{MARK} 改過")
    assert store.outbox_ulids(paths) == set()
