"""Install check: the agora entry point works from a clean `uv tool install`.

Installs into temp UV_TOOL_DIR/UV_TOOL_BIN_DIR only, then runs the installed
binary against temp AGORA_* dirs with rclone disabled, so nothing touches
Drive or the user's ~/.local/bin. Temp dirs are removed at the end.

Run: uv run pytest -q -m integration tests/integration/test_install.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent.parent
COMMANDS = ["search", "import", "merge", "continue", "delete", "edit", "show"]
USER_BIN = Path.home() / ".local" / "bin"


def listing(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


def test_installed_agora_offline(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv not found")
    tools = tmp_path / "tools"
    bindir = tmp_path / "bin"
    before = listing(USER_BIN)
    try:
        proc = subprocess.run(
            ["uv", "tool", "install", "--editable", str(REPO)], capture_output=True,
            text=True, timeout=300,
            env={**os.environ, "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bindir)})
        assert proc.returncode == 0, proc.stderr[-1000:]
        agora = bindir / "agora"
        assert agora.is_file()

        help_proc = subprocess.run([str(agora), "--help"], capture_output=True,
                                   text=True, timeout=60)
        assert help_proc.returncode == 0
        for cmd in COMMANDS:
            assert cmd in help_proc.stdout, cmd

        offline = {**os.environ,
                   "AGORA_CONFIG": str(tmp_path / "config"),
                   "AGORA_CACHE_DIR": str(tmp_path / "cache"),
                   "AGORA_STATE_DIR": str(tmp_path / "state"),
                   "AGORA_RCLONE": "/usr/bin/false"}  # Drive unreachable by construction
        search = subprocess.run([str(agora), "search", "session", "--no-sync"],
                                capture_output=True, text=True, timeout=120, env=offline)
        assert search.returncode == 0
        show = subprocess.run([str(agora), "show", "session", "agora:01NADA0000000000000000"],
                              capture_output=True, text=True, timeout=120, env=offline)
        assert show.returncode == 1
        assert listing(USER_BIN) == before
    finally:
        shutil.rmtree(tools, ignore_errors=True)
        shutil.rmtree(bindir, ignore_errors=True)


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(__import__("pytest").main([__file__, "-q"]))
