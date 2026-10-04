"""Tests for IME kitty keyboard protocol disabling in interactive mode (issue #23)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import subprocess
import sys
from pathlib import Path

from agora import cli

REPO = Path(__file__).resolve().parent.parent.parent
FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"

CODE_INTERACTIVE = """
import os, sys, types
import agora, agora.cli as cli
assert "textual" not in sys.modules, "cli 本身不能帶進 Textual"
fake = types.ModuleType("agora.tui")
def fake_main(paths):
    import textual.constants as c
    print(f"DISABLE_KITTY_KEY={int(c.DISABLE_KITTY_KEY)}")
    print(f"ENV={os.environ.get('TEXTUAL_DISABLE_KITTY_KEY')}")
    return 0
fake.main = fake_main
sys.modules["agora.tui"] = fake
agora.tui = fake
sys.stdin.isatty = sys.stdout.isatty = lambda: True
sys.exit(cli.main([]))
"""

CODE_COMMAND = """
import os, sys
import agora.cli as cli
code = cli.main(["search"])
assert "TEXTUAL_DISABLE_KITTY_KEY" not in os.environ, "指令模式不可設定 TEXTUAL_DISABLE_KITTY_KEY"
print(f"EXIT_CODE={code}")
"""


def _make_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    home = tmp_path / "home"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    state = tmp_path / "state"
    for d in (home, config, cache, state):
        d.mkdir(parents=True, exist_ok=True)

    wrapper = tmp_path / "fake_rclone_wrapper.sh"
    if not wrapper.exists():
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
        wrapper.chmod(0o755)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO / "src"),
        "HOME": str(home),
        "AGORA_CONFIG": str(config),
        "AGORA_CACHE_DIR": str(cache),
        "AGORA_STATE_DIR": str(state),
        "AGORA_FOLDER_NAME": "agora-test",
        "AGORA_RCLONE": str(wrapper),
    }
    if extra:
        env.update(extra)
    return env


def test_interactive_mode_disables_kitty_by_default(tmp_path):
    env = _make_env(tmp_path)
    proc = subprocess.run([sys.executable, "-c", CODE_INTERACTIVE],
                          cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "DISABLE_KITTY_KEY=1" in proc.stdout
    assert "ENV=1" in proc.stdout


def test_interactive_mode_respects_user_setting(tmp_path):
    env = _make_env(tmp_path, {"TEXTUAL_DISABLE_KITTY_KEY": "0"})
    proc = subprocess.run([sys.executable, "-c", CODE_INTERACTIVE],
                          cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "DISABLE_KITTY_KEY=0" in proc.stdout
    assert "ENV=0" in proc.stdout


def test_command_mode_does_not_set_disable_kitty(tmp_path):
    env = _make_env(tmp_path)
    proc = subprocess.run([sys.executable, "-c", CODE_COMMAND],
                          cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert f"EXIT_CODE={cli.EXIT_INPUT}" in proc.stdout
