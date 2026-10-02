"""Integration: `agora pull session` / `agora push session` against real Drive.

T1 R5/R6/R7, design 5.10. Real Drive (agora-test/ only), real rclone, real
opencode for one self-made session. Scenarios:

* import a session, delete the local raw, `pull session <id>` brings it back;
* edit the local session.md, `push session <id>`, Drive has the local version;
* `pull session opencode:<id>` writes the agent session's full text into the
  reading cache (and a second pull says it is fresh and skips);
* no id at all -> exit 1.

TODO (impl1, change section 3): the "cloud does not have it" scenarios
(`--not-exist-delete` / `--not-exist-upload` / the plain notice) get their own
tests once specs/session-sync/spec.md section 3 lands.

Cleanup: Drive sessions this created are purged one by one, the opencode session
is deleted by id, then the project tree. Nothing outside agora-test/ is touched.

Run: uv run pytest -q -m integration tests/integration/test_pull_push.py
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
PROJ = Path("/tmp/agora-it-pullpush/p_專案.v2")
MODEL = os.environ.get("AGORA_TEST_MODEL", "opencode/space-bunny-free")
PROMPT = "請用一句話說明：把 CSV 轉成 Markdown 表格時，表的分隔列要怎麼寫？不要寫檔案。"


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
    # the adapter has to read the same opencode database the session below goes into
    monkeypatch.setenv("HOME", str(REAL_HOME))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    if PROJ.exists():
        shutil.rmtree(PROJ)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=PROJ, check=True)
    proj = PROJ.resolve()
    paths = store.Paths.from_env()
    created = {"ulids": [], "sessions": []}
    yield {"proj": proj, "paths": paths, "created": created, "capsys": capsys}
    drive = store.Drive(paths)
    for ulid in created["ulids"]:
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", drive.folder_id(),
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
    for sid in created["sessions"]:
        subprocess.run(["opencode", "session", "delete", sid], cwd=str(proj),
                       env={**os.environ, "PWD": str(proj)}, capture_output=True)
    shutil.rmtree(PROJ.parent, ignore_errors=True)


def run_cli(env, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = env["capsys"].readouterr()
    return code, out.out.strip(), out.err


def make_session(env) -> str:
    proc = subprocess.run(
        ["opencode", "run", "-m", MODEL, "--format", "json", PROMPT],
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


def imported(env) -> tuple[str, str]:
    """One imported session: (agora id, opencode session id)."""
    session_id = make_session(env)
    code, out, err = run_cli(env, "import", "session", "--external-session-id", session_id,
                             "--agent", "opencode", "--header", "title=pull push 表格")
    assert code == 0, err
    agora_id = out.split()[0]
    env["created"]["ulids"].append(agora_id.split(":", 1)[1])
    return agora_id, session_id


def test_pull_brings_the_raw_back(env):
    agora_id, _ = imported(env)
    paths = env["paths"]
    hdr = store.Index(paths).header(agora_id.split(":", 1)[1])
    raw_name = h.agora_of(hdr)["raw"]["file"]
    local_raw = paths.mirror / agora_id.split(":", 1)[1] / raw_name
    store.fetch_raw(paths, store.Drive(paths), agora_id.split(":", 1)[1], hdr)
    assert local_raw.is_file()
    local_raw.unlink()                      # only the raw is gone; session.md stays

    code, out, err = run_cli(env, "pull", "session", agora_id)
    assert code == 0, err
    assert local_raw.is_file(), "pull did not bring the raw back"
    assert store.md5_file(local_raw) == h.agora_of(hdr)["raw"]["md5"]
    assert "pull 1/1" in err               # progress on stderr


def test_push_writes_the_local_version_to_drive(env):
    agora_id, _ = imported(env)
    paths = env["paths"]
    ulid = agora_id.split(":", 1)[1]
    local = paths.mirror / ulid / "session.md"
    text = local.read_text(encoding="utf-8")
    local.write_text(text.replace("title: pull push 表格", "title: 本機改過的標題"),
                     encoding="utf-8")
    before = store.Drive(paths).list_one(ulid).get("session.md")

    code, out, err = run_cli(env, "push", "session", agora_id)
    assert code == 0, err
    assert "push 1/1" in err
    remote = store.Drive(paths)
    assert remote.list_one(ulid)["session.md"] != before
    check = local.parent / "check.md"
    remote.download(ulid, "session.md", check)        # what Drive has now
    assert "本機改過的標題" in check.read_text(encoding="utf-8")
    check.unlink()


def test_pull_writes_an_agent_session_into_the_reading_cache(env):
    _agora_id, session_id = imported(env)
    paths = env["paths"]
    cached = paths.reading / "opencode" / f"{session_id}.md"
    assert not cached.exists(), "nothing cached before the first pull"

    code, _, err = run_cli(env, "pull", "session", f"opencode:{session_id}")
    assert code == 0, err
    assert cached.is_file()
    body = cached.read_text(encoding="utf-8")
    assert PROMPT[:12] in body              # the full text, not just a summary
    stamp = cached.stat().st_mtime

    code, _, err = run_cli(env, "pull", "session", f"opencode:{session_id}")
    assert code == 0
    assert cached.stat().st_mtime == stamp, "a fresh cache entry should be skipped"


def test_pull_and_push_need_an_id(env):
    for action in ("pull", "push"):
        code, _, err = run_cli(env, action, "session")
        assert code == cli.EXIT_INPUT, action
        assert "session id" in err, action


def test_push_refuses_an_agent_id(env):
    _agora_id, session_id = imported(env)
    code, _, err = run_cli(env, "push", "session", f"opencode:{session_id}")
    assert code != 0 and "opencode" in err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
