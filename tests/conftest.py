"""Shared test guard: no test may read the user's real sessions.

Every test gets a fresh HOME and agora state directories, so code that
reaches for ~/.claude or ~/.local/share/opencode finds an empty folder
instead of the user's real sessions (design.md section 7).
"""

from __future__ import annotations

import _guard  # noqa: F401  (T8: refuse to run at all outside an isolated pytest)
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent

# The probe and the integration tests share one definition of "is this opencode
# 2.x?", so the guard and the skips cannot disagree (tasks 1.10/1.11).
sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))
import opencode_noninteractive as _agent  # noqa: E402

# The token `_guard` reads to tell "a pytest run that has this conftest" from "a pytest run
# of a test file kept outside the repository", where pytest loads no conftest and nothing is
# isolated (T8). A test's own subprocess inherits this together with the temporary HOME that
# `isolated_home` sets, so it is under the same isolation; a pytest run of a file somewhere
# else has neither. Set at import time, never in the user's shell. It is a marker of origin,
# not a pass: on its own `_guard` still requires the four directories to point at temporary
# ones, because this token can be exported by hand or ride along in a copied environment.
os.environ["AGORA_TESTS_ISOLATED"] = "1"


def _real_env() -> dict:
    """The environment the user's own opencode store lives in."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    env = {**os.environ, "HOME": home, "PWD": str(REPO)}
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        env.pop(var, None)
    return env


def _opencode_guard_style() -> list[str]:
    """The server context the installed opencode's session commands need."""
    return (_agent.session_export_supported() and ["--standalone"]) or []


def _session_list() -> subprocess.CompletedProcess:
    """The one call the repo-clean guard makes, kept whole so a test can fake it:
    `opencode session list [--standalone] --format json` in this repository."""
    return subprocess.run(["opencode", "session", "list", *_opencode_guard_style(),
                           "--format", "json"],
                          cwd=str(REPO), env=_real_env(), capture_output=True, text=True)


def _session_ids_from(proc: subprocess.CompletedProcess, command: str) -> set[str]:
    """The ids that call reported - or a failure, never an empty set.

    A call that failed says so out loud: returning an empty set here is exactly
    the silent blindness review M5 found (on 2.x the 1.x flags differ, the exit
    code is non-zero, and a guard that shrugs is no guard).
    """
    if proc.returncode != 0:
        pytest.fail(f"{command} 失敗，護欄沒辦法確認 repo 目錄是乾淨的："
                    f"{(proc.stderr or '').strip()[-200:]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        pytest.fail(f"{command} 的輸出不是 JSON：{e}")
    return {row["id"] for row in rows if isinstance(row, dict) and row.get("id")}


def _repo_session_ids() -> set[str]:
    """Ids of the opencode sessions that belong to this repository, and nothing
    else: `session list` is scoped to the project directory. Only ids are kept -
    no titles, no transcripts.

    A 2.x opencode needs a server context of its own for every call, and a private
    one (`--standalone`) is the point of that - without it the command looks for
    the user's managed service. The 1.x spelling of both commands has no such flag.
    """
    return _session_ids_from(_session_list(), "opencode session list")


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
    style = _opencode_guard_style()
    undeleted: list[str] = []
    for session_id in sorted(added):
        proc = subprocess.run(["opencode", "session", "delete", session_id, *style],
                              cwd=str(REPO), env=_real_env(), capture_output=True)
        if proc.returncode != 0:
            undeleted.append(f"{session_id}（{(proc.stderr or b'').decode('utf-8', 'replace').strip()[-120:]}）")
    leftover = f"；這些沒刪掉：{'、'.join(undeleted)}" if undeleted else ""
    pytest.fail("整合測試在 repo 目錄留下 opencode session（已刪掉，請找出是哪條指令"
                f"忘了 --dir）：{', '.join(sorted(added))}{leftover}")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch, request):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(home))
    monkeypatch.setenv("AGORA_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    # Angareion reads nothing but these three (change angareion-v0, tasks 1.2):
    # point them at the temporary directory so no test can reach the real
    # ~/.config/angareion or the real token.
    monkeypatch.setenv("ANGAREION_CONFIG", str(tmp_path / "angareion" / "config.json"))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(tmp_path / "angareion-state"))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(tmp_path / "angareion" / "token"))
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
        monkeypatch.setenv("AGORA_AGY_CMD", os.environ.get("AGORA_AGY_CMD", "/nonexistent/agy"))
        # Belt and braces for agy (#26 PR2b review N8): a test that forgets the
        # agy_home fixture still writes under tmp_path, never the real
        # ~/.gemini/antigravity-cli. Always set, not inherited from the outer
        # shell (review N6): this is a directory tests write to; a test that
        # wants its own path sets it on top.
        monkeypatch.setenv("AGORA_AGY_HOME", str(tmp_path / "agy-home"))
    return home
