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
    created = {"ulids": [], "sessions": []}
    yield {"proj": PROJ.resolve(), "created": created, "capsys": capsys,
           "other": tmp_path / "other"}
    drive = store.Drive(store.Paths.from_env())
    for ulid in created["ulids"]:                    # only what this run printed
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", drive.folder_id(),
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
    for sid in created["sessions"]:
        subprocess.run(["opencode", "session", "delete", sid], cwd=str(PROJ),
                       env={**os.environ, "PWD": str(PROJ)}, capture_output=True)
    shutil.rmtree(PROJ.parent, ignore_errors=True)


def wait_uploaded(paths, timeout: float = 300.0) -> None:
    """Wait until the background uploader is through: nobody holds the upload lock, and
    the outbox and the trash queue are both empty (spec「delete 先在本機」).

    All three, and the lock first: a queue that empties just as the next round starts
    would otherwise pass too early, and a lock held by a uploader that is about to fail
    is exactly the case worth waiting for.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not store.uploader_is_running(paths) and not store.outbox_ulids(paths) \
                and not store.queued_for_trash(paths):
            return
        time.sleep(0.5)
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
        _, sid = make_session(env, f"{PROMPT}（第 {n + 1} 則）")
        code, ids, err = run_cli(env, "import", "session", "--agent", "opencode",
                                 "--external-session-id", sid,
                                 "--header", f"title={MARK} 匯入 {n + 1}")
        assert code == 0, err
        out.extend(ids.split())
    env["created"]["ulids"].extend(ulid_of(i) for i in out)
    return out


def ulid_of(agora_id: str) -> str:
    return agora_id.split(":")[1]


def other_machine(env, monkeypatch) -> store.Paths:
    """A second agora on this account: its own cache and state, the same `agora-test/`.

    Same Drive folder, different machine - which is the whole point of the two rescue
    scenarios. Its lock and queues are its own, so `wait_uploaded` is told whose.
    """
    other = env["other"]
    monkeypatch.setenv("AGORA_CACHE_DIR", str(other / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(other / "state"))
    return store.Paths.from_env()


def on_drive(paths, ulid: str) -> bool:
    """Whether Drive has this session's folder right now."""
    return ulid in (store.Drive(paths).list_sessions() or {})


def test_import_several_and_the_background_puts_them_all_on_drive(env, monkeypatch):
    """5.2: the command is done when the session is safe here; Drive catches up."""
    paths = store.Paths.from_env()
    ids = import_sessions(env, 2)
    assert store.outbox_ulids(paths), "they are staged here, not on Drive yet"

    wait_uploaded(paths)

    assert store.outbox_ulids(paths) == set()
    for agora_id in ids:
        assert on_drive(paths, ulid_of(agora_id)), f"{agora_id} 沒有上傳"
        # and the local copy is complete: session.md and the raw it names
        folder = paths.mirror / ulid_of(agora_id)
        assert (folder / "session.md").is_file()
        hdr, _body = h.split_document((folder / "session.md").read_text(encoding="utf-8"))
        raw = h.agora_of(hdr).get("raw") or {}
        assert raw.get("file") and (folder / raw["file"]).is_file(), "原始檔也在本機"


def test_delete_several_and_the_background_takes_them_off_drive(env, monkeypatch):
    """5.2: gone from here at once, gone from Drive once the background gets to it."""
    paths = store.Paths.from_env()
    ids = import_sessions(env, 2)
    wait_uploaded(paths)
    for agora_id in ids:
        assert on_drive(paths, ulid_of(agora_id))

    code, out, err = run_cli(env, "delete", "session", *ids, "--yes")

    assert code == 0 and set(out.split()) == set(ids), err
    # the foreground's half is done before the command returns
    assert store.Index(paths).header(ulid_of(ids[0])) is None
    assert sorted(store.queued_for_trash(paths)) == sorted(ulid_of(i) for i in ids)

    wait_uploaded(paths)

    assert store.queued_for_trash(paths) == set()
    for agora_id in ids:
        assert not on_drive(paths, ulid_of(agora_id)), f"{agora_id} 還在 Drive 上"


def test_a_session_deleted_elsewhere_comes_back_with_an_explicit_push(env, monkeypatch):
    """5.2 / spec「只在明確要求時才刪或復活」: another machine's delete is undone by
    `push --not-exist-upload`, not by anything that happens on its own."""
    paths = store.Paths.from_env()
    (agora_id,) = import_sessions(env, 1)
    ulid = ulid_of(agora_id)
    wait_uploaded(paths)

    theirs = other_machine(env, monkeypatch)
    code, out, err = run_cli(env, "delete", "session", agora_id, "--yes")
    assert code == 0, err
    wait_uploaded(theirs)
    assert not on_drive(theirs, ulid), "另一台刪掉了"

    # this machine finds out on its next sync, and says so rather than forgetting it
    code, found, _ = run_cli(env, "search", "session", "--filter", "cloud=no")
    assert code == 0 and agora_id in found, "本機還在，而且標成雲端沒有"

    code, out, err = run_cli(env, "push", "session", agora_id, "--not-exist-upload")

    assert code == 0, err
    wait_uploaded(paths)
    assert on_drive(paths, ulid), "明講了要傳回去，就真的回去了"
    assert store.Index(paths).missing_in_cloud() == []


def test_an_edit_deleted_elsewhere_is_kept_as_a_session_of_its_own(env, monkeypatch):
    """5.2 / L7 / N5: the edit is not pushed back into an id another machine deleted -
    that would undo their delete behind their back - so it becomes a new Session whose
    parent is the deleted one, and the deleted one stays deleted."""
    paths = store.Paths.from_env()
    (agora_id,) = import_sessions(env, 1)
    ulid = ulid_of(agora_id)
    wait_uploaded(paths)

    # this machine starts a new version of it (that is what `.update` means)
    code, _, err = run_cli(env, "edit", "session", agora_id, "--header", f"title={MARK} 改過")
    assert code == 0, err
    assert store.outbox_ulids(paths) == {ulid}, "還沒上傳的新版本在 outbox"

    theirs = other_machine(env, monkeypatch)
    code, _, err = run_cli(env, "delete", "session", agora_id, "--yes")
    assert code == 0, err
    wait_uploaded(theirs)
    assert not on_drive(theirs, ulid)

    # any command of ours starts the uploader again, and it meets the same situation
    run_cli(env, "search", "session", "--filter", "cloud=no")
    wait_uploaded(paths)

    assert not on_drive(paths, ulid), "被別台刪掉的那一筆沒有被傳回去"
    index = store.Index(paths)
    # what came back is a new id whose parent is the deleted one - one, not two
    new_ids = [u for u in _ulids(index)
               if any(p.get("id") == f"agora:{ulid}"
                      for p in (index.header(u).get("parents") or []))]
    assert len(new_ids) == 1, f"應該剛好另外存成一個新的，現在有 {new_ids}"
    env["created"]["ulids"].append(new_ids[0])
    assert on_drive(paths, new_ids[0]), "新存的那一個有上傳"
    assert store.outbox_ulids(paths) == set()


def _ulids(index) -> list[str]:
    """Every ULID this machine knows about."""
    return [hdr["id"].split(":")[1] for hdr, _snippet in index.search([])]