"""Unit tests for `angareion --version` (#214)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import re

from angareion.cli import main


def test_version_one_line_and_format(capsys):
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert re.match(r"^angareion \S+ \((?:[0-9a-f]{7,}|unknown)\)$", out.strip())


def test_version_unknown_commit(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("angareion.cli.subprocess.run", boom)
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("(unknown)")
    assert re.match(r"^angareion \S+ \(unknown\)$", out)


def test_version_from_package_metadata(monkeypatch, capsys):
    monkeypatch.setattr("angareion.cli.importlib.metadata.version", lambda name: "9.9.9")
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "9.9.9" in out


def test_version_does_not_run_git_on_other_paths(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("subprocess.run called on a non-version path")

    monkeypatch.setattr("angareion.cli.subprocess.run", boom)
    rc = main(["channel", "--help"])
    assert rc == 0