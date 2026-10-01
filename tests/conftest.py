"""Shared test guard: no test may read the user's real sessions.

Every test gets a fresh HOME and agora state directories, so code that
reaches for ~/.claude or ~/.local/share/opencode finds an empty folder
instead of the user's real sessions (design.md section 7).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(home))
    monkeypatch.setenv("AGORA_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    return home
