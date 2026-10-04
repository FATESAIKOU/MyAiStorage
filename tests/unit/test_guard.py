"""The tests' helpers refuse to run outside an isolated environment (T8)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TESTS = Path(__file__).resolve().parent.parent
FAKE_RCLONE = TESTS / "fakes" / "fake_rclone.py"


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


def _pytest_env(tmp_path: Path, home: Path) -> dict[str, str]:
    """An environment built from zero, as `test_ime_kitty.py` does, with every directory
    under `tmp_path` so the subprocess cannot reach the user's own.

    Two details are here for `uv run`, not for agora: the interpreter's own bin directory
    goes first on PATH, because `uv` is reached through a version manager's shim that needs
    the very HOME this replaces; and the project's fake rclone comes along, so even a probe
    that tried to upload would talk to the fake.
    """
    wrapper = tmp_path / "fake_rclone_wrapper.sh"
    if not wrapper.exists():
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
        wrapper.chmod(0o755)
    dirs = {"HOME": home, "AGORA_CONFIG": tmp_path / "config",
            "AGORA_CACHE_DIR": tmp_path / "cache", "AGORA_STATE_DIR": tmp_path / "state"}
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        dirs[var] = home.parent / var.lower()
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": str(Path(sys.executable).resolve().parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO / "src"),
        "AGORA_FOLDER_NAME": "agora-test",
        "AGORA_RCLONE": str(wrapper),
        **{name: str(path) for name, path in dirs.items()},
    }
    return env


def _guard_lines(output: str) -> list[str]:
    """Only the guard's own words: pytest and uv print paths of their own, and the
    refusal shows up wherever pytest's terminal writer put it."""
    return [line for line in output.splitlines() if "測試的 helper 只能在隔離的環境用" in line]


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


def test_a_test_file_outside_the_repo_is_refused_under_pytest(tmp_path):
    """The 10-04 case, made into a test: a test file outside the repository, run with
    `uv run pytest`, gets no conftest from pytest and so no isolation at all - yet pytest
    is running, which used to be enough for this module to stay quiet.

    The probe is an ordinary test that writes a marker, standing for the helpers writing
    into a real cache: pytest must stop on the import, so the marker never appears and the
    run fails. (Had the probe been a file with no tests at all, pytest would have failed it
    for a different reason and the exit code would have said nothing.)

    The subprocess gets its environment built from zero (see `_pytest_env`), so `uv run`
    uses this project's venv - the same command the 10-04 probe was run with - while every
    directory it can see is a temporary one. That is what makes the test safe to run: even
    if the guard were removed, the probe could not reach the user's real directories.
    """
    outside = tmp_path / "outside_the_repo"  # a test file kept outside `tests/`, like 10-04
    outside.mkdir()
    marker = tmp_path / "helper_ran"
    (outside / "test_probe_outside.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(TESTS)!r})\n"
        "import _guard  # a test helper: the import itself has to stop\n"
        "from pathlib import Path\n"
        "\n"
        "def test_the_helper_ran():\n"
        f"    Path({str(marker)!r}).write_text('the helper ran')\n",
        encoding="utf-8")
    home = tmp_path / "fake_home"
    proc = subprocess.run(["uv", "run", "pytest", str(outside / "test_probe_outside.py")],
                          cwd=str(REPO), env=_pytest_env(tmp_path, home), capture_output=True,
                          text=True, timeout=300)
    said = "\n".join(_guard_lines(proc.stdout + proc.stderr))
    assert proc.returncode != 0, f"pytest should have stopped:\n{proc.stdout}\n{proc.stderr}"
    assert said, f"the guard said nothing:\n{proc.stdout}\n{proc.stderr}"
    assert "conftest" in said, f"the message has to give the reason: {said}"
    for path in (str(tmp_path), str(outside), str(home), str(REPO)):
        assert path not in said, f"the message names the reason, not a path: {said}"
    assert not marker.exists(), "the import must be refused before the next line runs"
    assert list(home.iterdir()) == [], "nothing may be touched before the refusal"
