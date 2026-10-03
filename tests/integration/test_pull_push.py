"""Integration: `agora pull session` / `agora push session` against real Drive.

T1 R5/R6/R7, design 5.10. Real Drive (agora-test/ only), real rclone, real
opencode for one self-made session. Scenarios:

* import a session, delete the local raw, `pull session <id>` brings it back;
* edit the local session.md, `push session <id>`, Drive has the local version;
* `pull session opencode:<id>` writes the agent session's full text into the
  reading cache (and a second pull says it is fresh and skips);
* no id at all -> exit 1.

The "cloud does not have it" scenarios (spec session-sync「雲端沒有的 Session
保留在本機並標記」、「只在明確要求時刪除或復活」、「其他指令遇到雲端沒有的
Session」) are at the bottom: another machine's delete is simulated by purging
the folder on Drive directly, never by another agora.

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
    # This file is about pull/push and the CLI, not about the background uploader:
    # in the foreground the upload is done when the command returns, which is what
    # these assertions have always meant. The detached uploader has its own tests
    # (tests/integration/test_background_writes.py, tests/unit/test_background.py).
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
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


def fetch_local_raw(env, agora_id: str):
    """The raw as `agora show --raw` would leave it: here, fetched once by hand."""
    paths, ulid = env["paths"], agora_id.split(":", 1)[1]
    hdr = store.Index(paths).header(ulid)
    store.fetch_raw(paths, store.Drive(paths), ulid, hdr)
    raw = paths.mirror / ulid / h.agora_of(hdr)["raw"]["file"]
    assert raw.is_file()
    return hdr, raw


def test_pull_brings_the_raw_back(env):
    agora_id, _ = imported(env)
    paths = env["paths"]
    hdr, local_raw = fetch_local_raw(env, agora_id)
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


# --- the cloud no longer has it (spec session-sync, change section 3) ----------

def resync(env) -> str:
    """Really list Drive again: `search` syncs, but sync is throttled."""
    (env["paths"].state / "last-sync").unlink(missing_ok=True)
    code, out, err = run_cli(env, "search", "session", "--filter", "cloud=no")
    assert code == 0, err
    return out


def purge_on_drive(env, agora_id: str) -> None:
    """Another machine deleted it: the folder is gone from Drive, nothing else."""
    drive = store.Drive(env["paths"])
    # not check=True: rclone exits 3 for a folder that is not there, and "already gone"
    # is one of the states this helper is used to create (store.delete_session settles
    # it the same way - S1-4b)
    subprocess.run(["rclone", "--config", str(REAL_CONF),
                    "--drive-root-folder-id", drive.folder_id(),
                    "purge", f"gdrive:sessions/{agora_id.split(':', 1)[1]}"],
                   capture_output=True)


def test_a_session_deleted_elsewhere_is_kept_here_and_marked(env):
    agora_id, _ = imported(env)
    ulid = agora_id.split(":", 1)[1]
    purge_on_drive(env, agora_id)                       # another machine's delete
    env["created"]["ulids"].remove(ulid)                # Drive has none left to purge

    resync(env)
    assert store.Index(env["paths"]).header(ulid) is not None, "the mirror must stay"
    assert not store.Index(env["paths"]).cloud_has(ulid)

    code, out, _ = run_cli(env, "search", "session", "--filter", "cloud=no")
    assert code == 0 and agora_id in out and "(雲端沒有)" in out

    code, out, _ = run_cli(env, "search", "session", "--filter", "cloud=yes")
    assert code == 0 and agora_id not in out


def test_continue_refuses_a_session_the_cloud_lost(env):
    agora_id, _ = imported(env)
    ulid = agora_id.split(":", 1)[1]
    purge_on_drive(env, agora_id)
    env["created"]["ulids"].remove(ulid)
    resync(env)

    code, out, err = run_cli(env, "continue", "session", agora_id, "--agent", "opencode",
                             "--dir", str(env["proj"]))
    assert code == cli.EXIT_INPUT, "must not write back what the cloud dropped"
    assert out == "" and "--not-exist-upload" in err and "--not-exist-delete" in err
    assert not list(env["paths"].pending.glob("*.json")), "no agent was started"


def test_not_exist_upload_puts_it_back_and_the_mark_clears(env):
    agora_id, _ = imported(env)
    ulid = agora_id.split(":", 1)[1]
    hdr, local_raw = fetch_local_raw(env, agora_id)     # both files go up (R7)
    raw_name = h.agora_of(hdr)["raw"]["file"]
    purge_on_drive(env, agora_id)
    resync(env)
    assert not store.Index(env["paths"]).cloud_has(ulid)

    code, _, err = run_cli(env, "push", "session", agora_id, "--not-exist-upload")
    assert code == 0, err
    assert ulid in remote_names(env), "it should be back on Drive"
    on_drive = {e["Name"] for e in json.loads(
        store.Drive(env["paths"])._run("lsjson", f"gdrive:sessions/{ulid}", "--files-only") or "[]")}
    assert on_drive == {"session.md", raw_name}, on_drive
    (env["paths"].state / "last-sync").unlink(missing_ok=True)
    run_cli(env, "search", "session", "--filter", "cloud=yes")
    assert store.Index(env["paths"]).cloud_has(ulid), "the mark clears once it is back"


def test_not_exist_delete_drops_the_local_copy(env):
    agora_id, _ = imported(env)
    ulid = agora_id.split(":", 1)[1]
    purge_on_drive(env, agora_id)
    env["created"]["ulids"].remove(ulid)
    resync(env)
    assert store.Index(env["paths"]).header(ulid) is not None

    code, _, err = run_cli(env, "pull", "session", agora_id, "--not-exist-delete")
    assert code == 0, err
    assert store.Index(env["paths"]).header(ulid) is None
    assert not (env["paths"].mirror / ulid).exists()
    code, out, _ = run_cli(env, "search", "session", "--filter", "cloud=no")
    assert agora_id not in out


def test_a_plain_pull_or_push_only_says_it(env):
    agora_id, _ = imported(env)
    ulid = agora_id.split(":", 1)[1]
    purge_on_drive(env, agora_id)
    env["created"]["ulids"].remove(ulid)
    resync(env)

    code, _, err = run_cli(env, "pull", "session", agora_id)
    assert code == 0 and "雲端沒有" in err
    assert store.Index(env["paths"]).header(ulid) is not None     # untouched

    code, _, err = run_cli(env, "push", "session", agora_id)
    assert code == 0 and "雲端沒有" in err
    assert ulid not in remote_names(env)                          # not revived
    assert store.Index(env["paths"]).header(ulid) is not None


def remote_names(env) -> set[str]:
    drive = store.Drive(env["paths"])
    out = drive._run("lsjson", "gdrive:sessions", "--dirs-only")
    return {e["Name"] for e in json.loads(out or "[]")}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
