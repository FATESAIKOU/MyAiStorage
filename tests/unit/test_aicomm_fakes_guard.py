"""The `run_aicomm` sidecar-link guard on its own: only a directory a test owns passes."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _aicomm_fakes import _link_is_test_only, run_aicomm

REPO = Path(__file__).resolve().parent.parent.parent


def setup_env(tmp_path, monkeypatch):
    """A clean room: HOME in tmp and the hook unset, so the default link dir is absent."""
    monkeypatch.delenv("AICOMM_SIDECAR_LINK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_no_default_directory_is_test_only(tmp_path, monkeypatch):
    # `isolated_home` points HOME at a tmp dir: the real ~/.mylinuxpool/sidecar-link is out
    # of reach, so a run without the hook cannot read a real link.
    setup_env(tmp_path, monkeypatch)
    assert _link_is_test_only() is True


def test_a_default_directory_that_exists_is_refused(tmp_path, monkeypatch):
    # (1) a default link dir under HOME and no hook: a test could read it, refuse instead.
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "home" / ".mylinuxpool" / "sidecar-link").mkdir(parents=True)
    assert _link_is_test_only() is False


def test_the_hook_counts_even_with_a_default_directory(tmp_path, monkeypatch):
    # (2) the fixture's shape: the hook names the test's own directory.
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "home" / ".mylinuxpool" / "sidecar-link").mkdir(parents=True)
    monkeypatch.setenv("AICOMM_SIDECAR_LINK", str(tmp_path / "link"))
    assert _link_is_test_only() is True


def test_run_aicomm_refuses_without_a_fake_sidecar(tmp_path, monkeypatch, capsys):
    # (3) the composed guard: no AICOMM_SIDECAR, nothing to run against.
    monkeypatch.delenv("AICOMM_SIDECAR", raising=False)
    monkeypatch.setenv("AICOMM_SIDECAR_LINK", str(tmp_path / "link"))
    with pytest.raises(AssertionError, match="AICOMM_SIDECAR"):
        run_aicomm(["send", "--channel", "c", "--body", "x"], monkeypatch, capsys)


def test_run_aicomm_refuses_a_default_link_it_could_read(tmp_path, monkeypatch, capsys):
    # (4) the composed guard: the default directory exists and there is no hook.
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "home" / ".mylinuxpool" / "sidecar-link").mkdir(parents=True)
    monkeypatch.setenv("AICOMM_SIDECAR", "http://127.0.0.1:9")  # never contacted here
    with pytest.raises(AssertionError, match="sidecar-link"):
        run_aicomm(["send", "--channel", "c", "--body", "x"], monkeypatch, capsys)


# --- C-17: `sidecar_link` must not depend on cli, and must not have an HTTP client --------


def test_c17_importing_sidecar_link_does_not_import_cli():
    script = (
        "import sys\n"
        "import aicomm.sidecar_link\n"
        "print(','.join(sorted(n for n in sys.modules if n.startswith('aicomm'))))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          cwd=str(REPO), env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "aicomm.sidecar_link" in done.stdout
    assert "aicomm.cli" not in done.stdout


def test_c17_sidecar_link_has_no_http_client_and_no_cli():
    source = (REPO / "src" / "aicomm" / "sidecar_link.py").read_text(encoding="utf-8")
    for forbidden in ("urllib", "http.client", "aicomm.cli"):
        assert forbidden not in source, forbidden
