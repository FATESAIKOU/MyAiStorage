"""The tests' helpers refuse to run outside an isolated environment (T8)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _run(tmp_path: Path, *extra: tuple[str, str], drop: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Import a test module in a plain interpreter, with only the given settings."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO / "src")}
    for name, value in extra:
        env[name] = value
    for name in drop:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'tests/unit'); import test_tui"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)


def test_importing_a_test_module_outside_pytest_is_refused(tmp_path):
    """T8: no PYTEST_VERSION and nothing isolated: the import stops before any helper
    runs, and it says which settings are loose - never a path or a title."""
    home = tmp_path / "fake_home"
    home.mkdir()
    proc = _run(tmp_path, ("HOME", str(home)))
    assert proc.returncode != 0
    assert "測試的 helper 只能在隔離的環境用" in proc.stderr
    assert "AGORA_CACHE_DIR" in proc.stderr and "AGORA_STATE_DIR" in proc.stderr
    assert str(home) not in proc.stderr, "the message names the settings, not paths"
    assert list(home.iterdir()) == [], "nothing may be touched before the refusal"


def test_importing_a_test_module_with_isolation_is_fine(tmp_path):
    """T8: with all four pointed at a temporary directory (what conftest does), a plain
    interpreter can import the helpers - the guard is about isolation, not about pytest."""
    dirs = {name: str(tmp_path / name) for name in
            ("AGORA_CACHE_DIR", "AGORA_STATE_DIR", "AGORA_CONFIG", "HOME")}
    for path in dirs.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    proc = _run(tmp_path, *dirs.items())
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "測試的 helper" not in proc.stderr
