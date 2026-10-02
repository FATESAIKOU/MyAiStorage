"""Integration: ClaudeAgent.summarize() against the real claude CLI.

Verifies design 5.3 / review Y3 end to end: a headless run with no tools at
all answers a self-made prompt, reports the model it used, and leaves nothing
behind under ~/.claude/projects (--no-session-persistence). The workdir is a
throwaway git repo under /tmp so nothing lands in a real project.

Run: uv run pytest -q -m integration tests/integration/test_claude_summarize.py
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

from agora.agents import claude as C

pytestmark = pytest.mark.integration

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
PROJ = Path("/tmp/agora-it-summarize/sum_proj")
PROMPT = ("請用兩句繁體中文，把下面這件事說清楚：有人在規劃「把 CSV 轉成 Markdown 表格」"
          "的流程，已經決定先讀檔、組表頭、再輸出資料列。只輸出這兩句話。")


@pytest.fixture
def workdir(monkeypatch):
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found")
    if PROJ.exists():
        shutil.rmtree(PROJ)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=PROJ, check=True)
    proj = PROJ.resolve()
    # conftest gives every test a fake HOME; this one needs the real login (only here, only per test).
    monkeypatch.setenv("HOME", str(REAL_HOME))
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(REAL_HOME))
    yield proj
    _assert_and_clean(proj)


def _session_files(proj: Path) -> list[Path]:
    """Only this project's own folder: exact path, never a listing of others."""
    subdir = C.projects_dir() / C.encode_project_dir(proj)
    return sorted(subdir.glob("*.jsonl")) if subdir.is_dir() else []


def _assert_and_clean(proj: Path) -> None:
    assert _session_files(proj) == [], f"summarize left a session behind: {_session_files(proj)}"
    for leftover in [C.projects_dir() / C.encode_project_dir(proj) / "memory",
                     C.projects_dir() / C.encode_project_dir(proj)]:
        try:
            leftover.rmdir()
        except OSError:
            pass
    shutil.rmtree(PROJ.parent, ignore_errors=True)


def test_summarize_answers_and_leaves_nothing(workdir):
    assert _session_files(workdir) == []          # before: clean
    text, model = C.ADAPTER.summarize(PROMPT, workdir)
    assert text.strip(), "the real CLI returned an empty summary"
    assert len(text) > 10
    assert model and ("claude" in model or "opus" in model or "haiku" in model), model
    assert _session_files(workdir) == []          # after: still clean (Y3)


def test_summarize_rejects_a_missing_workdir(workdir):
    from agora.agents.base import AgentError
    with pytest.raises(AgentError):
        C.ADAPTER.summarize(PROMPT, workdir / "nowhere")
