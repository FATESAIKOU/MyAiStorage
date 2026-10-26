"""Unit tests for `angareion --version` (#214)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import re
import subprocess

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


def test_version_unknown_on_nonzero_returncode(monkeypatch, capsys):
    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("angareion.cli.subprocess.run", lambda *a, **k: FakeResult())
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("(unknown)")


def test_version_unknown_package_on_not_installed(monkeypatch, capsys):
    import importlib.metadata

    def raise_pnf(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("angareion.cli.importlib.metadata.version", raise_pnf)
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert re.match(r"^angareion unknown \((?:[0-9a-f]{7,}|unknown)\)$", out)


def test_version_unknown_when_installed_outside_git_repo(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "fake@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "fake"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    fake_file = tmp_path / "lib" / "python3.12" / "site-packages" / "angareion" / "cli.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("angareion.cli.__file__", str(fake_file))

    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("(unknown)")


def test_version_prints_repo_sha_when_repo_root_is_git_root(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fake@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "fake"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    fake_file = repo / "src" / "angareion" / "cli.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("angareion.cli.__file__", str(fake_file))

    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith(f"({expected})")
