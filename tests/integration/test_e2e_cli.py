"""End to end through agora.cli.main: import -> search -> show -> continue
(native) -> merge -> continue (injected), with real Drive (agora-test/) and
real claude.

Continue spawns interactive claude, so AGORA_CLAUDE_CMD points at a sh
wrapper (E1) around tests/fakes/claude_noninteractive.py, which re-runs the
real CLI with -p and a fixed self-made question. Prompts are self-made
filler (CSV table steps); at most 3 real `claude -p` calls. Everything
created (Drive sessions/<ULID>, own jsonl uuids) is deleted one by one at
teardown; ids are re-collected from the wrapper log and our own cache/outbox
(E3) so a mid-test failure still cleans up.

Config pattern follows tests/integration/test_store_drive.py: AGORA_CONFIG is
a temp dir whose rclone.conf symlinks to ~/.config/agora/rclone.conf.

Run: uv run pytest -q -m integration tests/integration/test_e2e_cli.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import uuid
import warnings

import pytest

from agora import cli, store
from agora.agents import claude as C

pytestmark = pytest.mark.integration

# pwd, not HOME: the sandbox replaces HOME per test (E7).
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
WRAPPER_SRC = Path(__file__).parent.parent / "fakes" / "claude_noninteractive.py"
DISALLOW = "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"

P1 = "請用繁體中文列出「把 CSV 轉成 Markdown 表格」的三個步驟。只要文字回答，不要呼叫任何工具、不要寫檔案。"


@pytest.fixture()
def e2e(tmp_path, monkeypatch, capsys):
    if not REAL_CONF.exists():
        pytest.fail("需要 ~/.config/agora/rclone.conf（整合測試不能 skip）")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found")
    config = tmp_path / "config"
    config.mkdir()
    (config / "rclone.conf").symlink_to(REAL_CONF)
    monkeypatch.setenv("AGORA_CONFIG", str(config))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.delenv("AGORA_RCLONE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(REAL_HOME))
    wrapper = tmp_path / "claude-noninteractive"  # E1: no shebang roulette
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {WRAPPER_SRC} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(wrapper))
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(fake_home))

    proj = Path("/tmp/agora-it-e2e/proj")
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=proj, check=True)
    proj = proj.resolve()
    paths = store.Paths.from_env()
    created = {"ulids": [], "uuids": []}  # E3: recorded the moment they exist
    yield {"proj": proj, "paths": paths, "created": created,
           "fake_home": fake_home, "capsys": capsys}
    uuids = set(created["uuids"])
    log = fake_home / "e2e-args.log"  # E3: launches logged before agent start
    if log.exists():
        for line in log.read_text().splitlines():
            argv = json.loads(line)
            for flag in ("--resume", "--session-id"):
                if flag in argv:
                    uuids.add(argv[argv.index(flag) + 1])
    ulids = set(created["ulids"])
    ulids.update(store.Index(paths).known())  # our cache is test-exclusive
    outbox = paths.outbox
    if outbox.is_dir():
        ulids.update(p.parent.name for p in outbox.glob("*/session.md"))
    drive = store.Drive(paths)
    for ulid in sorted(ulids):
        proc = subprocess.run(
            ["rclone", "--config", str(REAL_CONF),
             "--drive-root-folder-id", drive.folder_id(),
             "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
        if proc.returncode != 0:  # E5: cleanup failures must be visible
            warnings.warn(f"purge sessions/{ulid} failed: {proc.stderr.decode()[-200:]}")
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for sid in sorted(uuids):  # own jsonl uuids, one by one (E4: plus side files)
        jsonl = basedir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = basedir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
        for extra in [cfgdir / "session-env" / sid, cfgdir / "file-history" / sid]:
            if extra.is_dir():
                shutil.rmtree(extra)
        for todo in sorted(cfgdir.glob(f"todos/{sid}-*.json")):
            todo.unlink()
    try:
        basedir.rmdir()
    except OSError:
        pass
    for leftover in [basedir / "memory"]:
        try:
            leftover.rmdir()  # claude leaves an empty memory/ dir; drop it too
        except OSError:
            pass
    try:
        basedir.rmdir()
    except OSError:
        pass
    shutil.rmtree(proj, ignore_errors=True)  # E5


def run_main(e2e, *argv: str) -> str:
    rc = cli.main(list(argv))
    out = e2e["capsys"].readouterr().out
    assert rc == 0, out[-500:]
    return out


def read_header(e2e, agora_id: str) -> dict:
    run_main(e2e, "sync")  # E2: read through the CLI + index like a user would
    hdr = store.Index(e2e["paths"]).header(agora_id.split(":", 1)[1])
    assert hdr is not None
    return hdr


def test_import_search_show_continue_merge_continue(e2e):
    proj = e2e["proj"]
    created = e2e["created"]
    env = {**os.environ, "HOME": str(REAL_HOME),
           "AGORA_CLAUDE_HOME": str(REAL_HOME)}

    # 1. self-made short dialogue, then import it.
    uuid1 = str(uuid.uuid4())
    created["uuids"].append(uuid1)
    proc = subprocess.run(
        ["claude", "--disallowedTools", DISALLOW, "--model", "haiku",  # E6
         "-p", "--session-id", uuid1, P1],
        cwd=proj, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-500:]
    out = run_main(e2e, "import", "--format", "claude", "--session-id", uuid1,
                   "--header", "title=e2e 表格")
    id1 = out.split()[0]
    assert id1.startswith("agora:")
    created["ulids"].append(id1.split(":", 1)[1])

    # 2. search finds it, agora id first.
    out = run_main(e2e, "search", "session", "表格")
    assert any(line.split()[0] == id1 for line in out.splitlines() if line.split())

    # 2b. show and show --raw through the CLI (E2).
    out = run_main(e2e, "show", id1)
    assert f"id: {id1}" in out and "e2e 表格" in out
    out = run_main(e2e, "show", id1, "--raw")
    assert '"format": "claude-jsonl/1"' in out

    # 3. native continue: wrapper receives --resume, saves a new session.
    out = run_main(e2e, "continue-session", id1, "--agent", "claude",
                   "--dir", str(proj))
    id2 = out.split()[0]
    assert id2.startswith("agora:") and id2 != id1
    created["ulids"].append(id2.split(":", 1)[1])
    hdr2 = read_header(e2e, id2)
    assert hdr2["relation"] == "continue"
    assert hdr2["parents"][0]["id"] == id1
    uuid2 = hdr2["source"]["session_id"]
    assert uuid2 != uuid1
    uuid.UUID(uuid2)
    created["uuids"].append(uuid2)

    # 4. merge with the user's comma form, then continue off the merge: must
    # inject the reading version (wrapper receives --session-id, not --resume).
    out = run_main(e2e, "merge-session", f"{id1},", id2)  # E2: 'id1,' 'id2'
    idm = out.split()[0]
    assert idm.startswith("agora:")
    created["ulids"].append(idm.split(":", 1)[1])
    out = run_main(e2e, "continue-session", idm, "--agent", "claude",
                   "--dir", str(proj))
    id3 = out.split()[0]
    assert id3.startswith("agora:") and id3 not in (id1, id2, idm)
    created["ulids"].append(id3.split(":", 1)[1])
    hdr3 = read_header(e2e, id3)
    assert hdr3["relation"] == "continue"
    assert hdr3["parents"][0]["id"] == idm
    uuid3 = hdr3["source"]["session_id"]
    assert uuid3 not in (uuid1, uuid2)
    created["uuids"].append(uuid3)

    # The wrapper saw one native launch and one injected launch, in order
    # (--version probes also land in the log; only launches count).
    launches = [json.loads(line)
                for line in (e2e["fake_home"] / "e2e-args.log").read_text().splitlines()
                if "--resume" in line or "--session-id" in line]
    assert len(launches) == 2
    assert "--resume" in launches[0] and "--session-id" not in launches[0]
    assert "--session-id" in launches[1] and "--resume" not in launches[1]
