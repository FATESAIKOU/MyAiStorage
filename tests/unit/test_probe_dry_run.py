"""R4 4.9: `probe_drive.py --dry-run` prints the rclone commands and runs nothing."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[2] / "experiments" / "agora-chunks" / "probe_drive.py"


@pytest.mark.parametrize("mode", ["--answers", "--quick", "--full"])
def test_dry_run_prints_the_plan_and_never_calls_rclone(tmp_path, mode):
    marker = tmp_path / "rclone-was-called"
    fake = tmp_path / "rclone"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    out = subprocess.run([sys.executable, str(PROBE), mode, "--dry-run", "--rclone", str(fake),
                          "--config", str(tmp_path / "no-such.conf"), "--json-out", str(tmp_path / "out.json")],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert not marker.exists() and not (tmp_path / "out.json").exists()
    text = out.stdout
    for needed in ("mkdir", "purge", "lsjson", "--dirs-only", "ModTime", "ID #1", "ID #2", "rc=3"):
        assert needed in text, needed
    assert str(tmp_path / "no-such.conf") not in text, "the config path is a placeholder"
    assert ("--files-only" in text) == (mode != "--answers")


def test_without_a_mode_nothing_runs_at_all(tmp_path):
    out = subprocess.run([sys.executable, str(PROBE), "--dry-run"], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 2
