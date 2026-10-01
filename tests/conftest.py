"""Shared test guard: no test may read the user's real sessions.

Every test gets a fresh HOME and agora state directories, so code that
reaches for ~/.claude or ~/.local/share/opencode finds an empty folder
instead of the user's real sessions (design.md section 7).
"""

from __future__ import annotations

import os

import pytest


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
        # A unit test that forgets its fake agent fails loudly instead of running the real one.
        monkeypatch.setenv("AGORA_OPENCODE_CMD", os.environ.get("AGORA_OPENCODE_CMD", "/nonexistent/opencode"))
        monkeypatch.setenv("AGORA_CLAUDE_CMD", os.environ.get("AGORA_CLAUDE_CMD", "/nonexistent/claude"))
    return home
