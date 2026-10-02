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

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
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
    drive = store.Drive(paths)
    for ulid in created["ulids"]:                      # one purge per session
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", drive.folder_id(),
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
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
    assert code == 0 and set(again.split()) == set(ids)


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
