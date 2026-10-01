"""Integration: real `claude -p` round-trip through the adapter.

export -> start_native -> `claude --resume <new> -p` -> collect.
Only self-made short prompts, at most 3 `claude -p` calls. Cleans up only
the uuids it created (jsonl + optional sidecar dir).

Run: uv run pytest -q -m integration tests/integration/test_claude_real.py
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from agora.agents import claude as C

pytestmark = pytest.mark.integration

CLAUDE = shutil.which("claude")

P1 = "請用繁體中文列出「把 CSV 轉成 Markdown 表格」的三個步驟。只要文字回答，不要呼叫任何工具、不要寫檔案。"
P2 = "把你剛才的三個步驟濃縮成一句話。只要文字回答，不要呼叫任何工具、不要寫檔案。"
P3 = "你前面在做什麼？用一句話回答。不要呼叫任何工具、不要寫檔案。"


def real_home() -> str:
    return pwd.getpwuid(os.getuid()).pw_dir


@pytest.fixture()
def live(monkeypatch, tmp_path):
    if CLAUDE is None:
        pytest.skip("claude CLI not found")
    home = real_home()
    monkeypatch.setenv("AGORA_CLAUDE_HOME", home)
    proj = Path("/tmp/agora-it-claude/proj")
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=proj, check=True)
    proj = proj.resolve()  # /tmp -> /private/tmp on macOS
    env = {**os.environ, "HOME": home, "AGORA_CLAUDE_HOME": home}
    created: list[str] = []
    yield {"proj": proj, "env": env, "created": created}
    subdir = C.projects_dir() / C.encode_project_dir(proj)
    for sid in created:  # only our own uuids, one by one
        jsonl = subdir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = subdir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)


def run_claude(live, *argv: str) -> str:
    proc = subprocess.run([CLAUDE, *argv], cwd=live["proj"], env=live["env"],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-500:]
    return proc.stdout


def test_native_round_trip(live):
    uuid1 = str(uuid.uuid4())
    live["created"].append(uuid1)
    run_claude(live, "-p", "--session-id", uuid1, P1)
    run_claude(live, "--resume", uuid1, "-p", P2)

    exported = C.ADAPTER.export(uuid1)
    assert exported.session_id == uuid1
    assert exported.dir == str(live["proj"])
    assert exported.title
    assert exported.message_count >= 4  # 2 rounds of user + assistant
    assert exported.agent_version and exported.agent_version[0].isdigit()

    launch = C.ADAPTER.start_native(exported.raw, live["proj"])
    uuid2 = launch.agent_session_id
    live["created"].append(uuid2)
    assert launch.argv == [C.agent_cmd("claude"), "--resume", uuid2]
    new_file = (C.projects_dir() / C.encode_project_dir(live["proj"])
                / f"{uuid2}.jsonl")
    assert new_file.is_file()

    run_claude(live, "--resume", uuid2, "-p", P3)

    got = C.ADAPTER.collect(launch)
    assert got is not None
    assert got.session_id == uuid2
    # Claude writes one line per content block, so a round adds two or more lines.
    assert got.message_count >= exported.message_count + 2
    main = json.loads(got.raw.decode("utf-8"))["main"]
    assert len(main) > len(json.loads(exported.raw.decode("utf-8"))["main"])
    assert {json.loads(line)["sessionId"] for line in main} == {uuid2}
