"""Shared test guard: no test may read the user's real sessions.

Every test gets a fresh HOME and agora state directories, so code that
reaches for ~/.claude or ~/.local/share/opencode finds an empty folder
instead of the user's real sessions (design.md section 7).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent


def _real_env() -> dict:
    """The environment the user's own opencode store lives in."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    env = {**os.environ, "HOME": home, "PWD": str(REPO)}
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        env.pop(var, None)
    return env


def _repo_session_ids() -> set[str]:
    """Ids of the opencode sessions that belong to this repository, and nothing
    else: `session list` is scoped to the project directory. Only ids are kept -
    no titles, no transcripts."""
    proc = subprocess.run(["opencode", "session", "list", "--format", "json"],
                          cwd=str(REPO), env=_real_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        return set()
    return {row["id"] for row in json.loads(proc.stdout or "[]") if row.get("id")}


@pytest.fixture(autouse=True)
def repo_project_stays_clean(request):
    """An integration test must not add a session to this repository.

    opencode files a session under the project of `$PWD`, and `agora continue`
    without `--dir` runs wherever the user is - for a checkout that is the repo,
    i.e. the user's real opencode database. That is not hypothetical: the
    acceptance checklist's `agora continue-session <merge id> --agent opencode`
    had no `--dir`, and it left an injected session titled 「Agora 接續（閱讀版）」
    here. So count before and after, and on a mismatch say which ids appeared,
    delete them one at a time, and fail.
    """
    if "integration" not in request.keywords or shutil.which("opencode") is None:
        yield
        return
    before = _repo_session_ids()
    yield
    added = _repo_session_ids() - before
    if not added:
        return
    for session_id in sorted(added):
        subprocess.run(["opencode", "session", "delete", session_id],
                       cwd=str(REPO), env=_real_env(), capture_output=True)
    pytest.fail("整合測試在 repo 目錄留下 opencode session（已刪掉，請找出是哪條指令"
                f"忘了 --dir）：{', '.join(sorted(added))}")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(home))
    monkeypatch.setenv("AGORA_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    # opencode finds its database through XDG_* too; never let a unit test reach it (OC11).
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    if "integration" not in request.keywords:
        # Unit tests want Drive to have the session by the time the command returns,
        # so uploads run in the foreground instead of in a background process
        # (change local-first-writes, design「測試開關」). Integration tests want the
        # real detached uploader, so they keep the default.
        monkeypatch.setenv("AGORA_UPLOAD", "inline")
        # A unit test that forgets its fake agent fails loudly instead of running the real one.
        monkeypatch.setenv("AGORA_OPENCODE_CMD", os.environ.get("AGORA_OPENCODE_CMD", "/nonexistent/opencode"))
        monkeypatch.setenv("AGORA_CLAUDE_CMD", os.environ.get("AGORA_CLAUDE_CMD", "/nonexistent/claude"))
    return home
