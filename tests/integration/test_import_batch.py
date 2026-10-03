"""Integration: `agora import session` with several ids at once (T1 R2).

Two self-made opencode sessions in a throwaway git project under /tmp, then one
import command with both ids. Real Drive (agora-test/ only), real opencode with
a free model. Checks: both ids printed on stdout, k/N progress on stderr only,
and a re-run prints the same two ids without rewriting anything.

Cleanup: the Drive sessions this created are purged, the two opencode session
ids are deleted one by one, then the project tree. Nothing outside agora-test/
is touched and nothing is deleted in bulk.

Run: uv run pytest -q -m integration tests/integration/test_import_batch.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
import sys

import pytest

from agora import cli, header as h, store

pytestmark = pytest.mark.integration

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
PROJ = Path("/tmp/agora-it-batch/p_專案.v2")
MODEL = os.environ.get("AGORA_TEST_MODEL", "opencode/space-bunny-free")
PROMPTS = [
    "請用一句話說明：把 CSV 轉成 Markdown 表格，第一步是什麼？不要寫檔案。",
    "請用一句話說明：把 CSV 轉成 Markdown 表格，第二步是什麼？不要寫檔案。",
]


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
    # conftest gives every test a fake HOME; the adapter has to find the same
    # opencode database the sessions below are written to, so use the real one
    # here (integration only, and only our own project's sessions are touched).
    monkeypatch.setenv("HOME", str(REAL_HOME))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    if PROJ.exists():
        shutil.rmtree(PROJ)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=PROJ, check=True)
    proj = PROJ.resolve()      # /tmp -> /private/tmp on macOS
    paths = store.Paths.from_env()
    created = {"ulids": [], "sessions": []}
    yield {"proj": proj, "paths": paths, "created": created, "capsys": capsys}
    # The uploader this command started is still running: it would put the session on
    # Drive *after* the purge below, and the folder would stay there for ever (T7 F4).
    # Empty *twice in a row*, and the lock first: an uploader that has renamed the entry
    # to `.done-` but not finished yet looks exactly like "nothing is waiting" to a
    # single look (review T3-it I4) - that is how the first version of this wait still
    # leaked one. `test_background_writes.wait_uploaded` is the same rule.
    # settle first (the uploader is detached here, T7 F4), then purge - and look again,
    # because a purge that raced the uploader is how the folder used to stay behind
    try:
        settle(paths)
    except AssertionError:
        pass                       # a failing test's leak is reported by its own assert
    drive = store.Drive(paths)
    giveup = time.monotonic() + 120
    while time.monotonic() < giveup:
        for ulid in created["ulids"]:                  # one purge per session
            subprocess.run(["rclone", "--config", str(REAL_CONF),
                            "--drive-root-folder-id", drive.folder_id(),
                            "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
        there = drive.list_sessions() or {}
        if not [u for u in created["ulids"] if u in there]:
            break                                      # gone, and nothing landed after
        time.sleep(1)
    for sid in created["sessions"]:                    # one delete per session, never in bulk
        subprocess.run(["opencode", "session", "delete", sid], cwd=str(proj),
                       env={**os.environ, "PWD": str(proj)}, capture_output=True)
    shutil.rmtree(PROJ.parent, ignore_errors=True)


def make_session(env, prompt: str) -> str:
    """One self-made session; its id comes from the run's own JSON events."""
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
        info = event.get("sessionID") or event.get("sessionId") or (event.get("properties") or {}).get("sessionID")
        if info:
            session_id = info
    assert session_id, "no session id in the run output"
    env["created"]["sessions"].append(session_id)
    return session_id


def settle(paths, timeout: float = 120.0) -> None:
    """Wait until the detached uploader is through; loud when it is not.

    The imports here leave it detached on purpose (this file is about several ids in
    one command, not about the uploader), so a command that returns has not uploaded
    anything yet. Anything the test does next - a re-run, a purge - has to wait for it,
    or it races the uploader and the race is what T7 F4 found (a folder landing after
    the cleanup). Same shape as `test_background_writes.wait_uploaded`, and it raises
    rather than walking away quietly.
    """
    settled, nudged, deadline = 0, 0.0, time.monotonic() + timeout
    while settled < 2 and time.monotonic() < deadline:
        held = store.uploader_is_running(paths)
        waiting = bool(store.outbox_ulids(paths) or store.queued_for_trash(paths))
        if not held and waiting and time.monotonic() - nudged > 5.0:
            store.kick_uploader(paths)     # what the next command would have done
            nudged = time.monotonic()
        settled = settled + 1 if (not held and not waiting) else 0
        time.sleep(0.5)
    assert settled >= 2, (
        f"背景上傳器 {timeout:.0f} 秒還沒做完：鎖={store.uploader_is_running(paths)}"
        f" outbox={sorted(store.outbox_ulids(paths))}"
        f" 佇列={sorted(store.queued_for_trash(paths))}")


def run_cli(env, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = env["capsys"].readouterr()
    return code, out.out.strip(), out.err


def test_import_two_sessions_in_one_command(env):
    first = make_session(env, PROMPTS[0])
    second = make_session(env, PROMPTS[1])

    code, out, err = run_cli(env, "import", "session",
                             "--external-session-id", f"{first},{second}",
                             "--agent", "opencode", "--header", "title=批次匯入")
    assert code == 0, err
    ids = out.split()
    assert len(ids) == 2 and len(set(ids)) == 2 and all(i.startswith("agora:") for i in ids)
    for ulid in ids:
        env["created"]["ulids"].append(ulid.split(":", 1)[1])
    assert "匯入 1/2" in err and "匯入 2/2" in err        # progress on stderr
    settle(env["paths"])   # on Drive before the re-run: otherwise the re-run races it
                           # and can decide "not there" - and make a second session (F4)
    bodies = {}
    for agora_id in ids:
        ulid = agora_id.split(":", 1)[1]
        _hdr, body = h.split_document(
            (env["paths"].mirror / ulid / "session.md").read_text(encoding="utf-8"))
        bodies[ulid] = body
    assert any(PROMPTS[0][:10] in body for body in bodies.values())   # each one landed
    assert any(PROMPTS[1][:10] in body for body in bodies.values())

    # a re-run prints the same two ids and does not create new ones (T1 R4)
    code, again, _ = run_cli(env, "import", "session",
                             "--external-session-id", f"{first},{second}",
                             "--agent", "opencode", "--header", "title=批次匯入")
    # whatever the re-run printed is ours to clean up, old id or not (T7 F4: the rule is
    # "only what this run printed", and a print we ignore is what left sessions behind)
    for agora_id in again.split():
        if agora_id not in ids:
            env["created"]["ulids"].append(agora_id.split(":", 1)[1])
    assert code == 0 and set(again.split()) == set(ids)
    settle(env["paths"])


def test_import_reports_the_one_that_failed_and_keeps_going(env):
    good = make_session(env, PROMPTS[0])
    code, out, err = run_cli(env, "import", "session",
                             "--external-session-id", f"ses_doesnotexist,{good}",
                             "--agent", "opencode")
    assert code != 0
    assert len(out.split()) == 1                        # the one that worked
    env["created"]["ulids"].append(out.split()[0].split(":", 1)[1])
    assert "ses_doesnotexist" in err and "匯入 2/2" in err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
