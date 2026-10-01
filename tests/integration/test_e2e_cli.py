"""End to end through agora.cli.main: import -> search -> continue (native) ->
merge -> continue (injected), with real Drive (agora-test/) and real claude.

Continue spawns interactive claude, so AGORA_CLAUDE_CMD points at
tests/fakes/claude_noninteractive.py, which re-runs the real CLI with -p and
a fixed self-made question. Prompts are self-made filler (CSV table steps);
at most 3 real `claude -p` calls. Everything created (Drive sessions/<ULID>,
own jsonl uuids) is deleted one by one at teardown.

Config pattern follows tests/integration/test_store_drive.py: AGORA_CONFIG is
a temp dir whose rclone.conf symlinks to ~/.config/agora/rclone.conf.

Run: uv run pytest -q -m integration tests/integration/test_e2e_cli.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from agora import cli, store
from agora.agents import claude as C

pytestmark = pytest.mark.integration

# Captured at import time: conftest replaces HOME per test (same pattern as
# test_store_drive.py / test_opencode_real.py).
REAL_HOME = Path(os.environ["HOME"])
REAL_CONF = Path(os.environ["HOME"]) / ".config" / "agora" / "rclone.conf"
WRAPPER = Path(__file__).parent.parent / "fakes" / "claude_noninteractive.py"

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
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(REAL_HOME))
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(WRAPPER))
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
    created_ulids: list[str] = []
    created_uuids: list[str] = []
    yield {"proj": proj, "paths": paths, "ulids": created_ulids,
           "uuids": created_uuids, "fake_home": fake_home, "capsys": capsys}
    drive = store.Drive(paths)
    for ulid in created_ulids:  # Drive sessions, one purge each
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", drive.folder_id(),
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
    subdir = C.projects_dir() / C.encode_project_dir(proj)
    for sid in created_uuids:  # own jsonl uuids, one by one
        jsonl = subdir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = subdir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
    try:
        subdir.rmdir()
    except OSError:
        pass


def run_main(e2e, *argv: str) -> str:
    rc = cli.main(list(argv))
    out = e2e["capsys"].readouterr().out
    assert rc == 0, out[-500:]
    return out


def read_header(e2e, agora_id: str) -> dict:
    ulid = agora_id.split(":", 1)[1]
    hdr = store.sync(e2e["paths"]).header(ulid)
    assert hdr is not None
    return hdr


def test_import_search_continue_merge_continue(e2e):
    proj = e2e["proj"]
    env = {**os.environ, "HOME": str(REAL_HOME),
           "AGORA_CLAUDE_HOME": str(REAL_HOME)}

    # 1. self-made short dialogue, then import it.
    uuid1 = str(uuid.uuid4())
    e2e["uuids"].append(uuid1)
    proc = subprocess.run(["claude", "-p", "--session-id", uuid1, P1],
                          cwd=proj, env=env, capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-500:]
    out = run_main(e2e, "import", "--format", "claude", "--session-id", uuid1,
                   "--header", "title=e2e 表格")
    id1 = out.split()[0]
    assert id1.startswith("agora:")
    e2e["ulids"].append(id1.split(":", 1)[1])

    # 2. search finds it, agora id first.
    out = run_main(e2e, "search", "session", "表格")
    assert any(line.split()[0] == id1 for line in out.splitlines() if line.split())

    # 3. native continue: wrapper receives --resume, saves a new session.
    out = run_main(e2e, "continue-session", id1, "--agent", "claude",
                   "--dir", str(proj))
    id2 = out.split()[0]
    assert id2.startswith("agora:") and id2 != id1
    e2e["ulids"].append(id2.split(":", 1)[1])
    hdr2 = read_header(e2e, id2)
    assert hdr2["relation"] == "continue"
    assert hdr2["parents"][0]["id"] == id1
    uuid2 = hdr2["source"]["session_id"]
    assert uuid2 != uuid1
    uuid.UUID(uuid2)
    e2e["uuids"].append(uuid2)

    # 4. merge with the comma form, then continue off the merge: must inject
    # the reading version (wrapper receives --session-id, not --resume).
    out = run_main(e2e, "merge-session", f"{id1},{id2}")
    idm = out.split()[0]
    assert idm.startswith("agora:")
    e2e["ulids"].append(idm.split(":", 1)[1])
    out = run_main(e2e, "continue-session", idm, "--agent", "claude",
                   "--dir", str(proj))
    id3 = out.split()[0]
    assert id3.startswith("agora:") and id3 not in (id1, id2, idm)
    e2e["ulids"].append(id3.split(":", 1)[1])
    hdr3 = read_header(e2e, id3)
    assert hdr3["relation"] == "continue"
    assert hdr3["parents"][0]["id"] == idm
    uuid3 = hdr3["source"]["session_id"]
    assert uuid3 not in (uuid1, uuid2)
    e2e["uuids"].append(uuid3)

    # The wrapper saw one native launch and one injected launch, in order
    # (--version probes also land in the log; only launches count).
    launches = [json.loads(line)
                for line in (e2e["fake_home"] / "e2e-args.log").read_text().splitlines()
                if "--resume" in line or "--session-id" in line]
    assert len(launches) == 2
    assert "--resume" in launches[0] and "--session-id" not in launches[0]
    assert "--session-id" in launches[1] and "--resume" not in launches[1]
