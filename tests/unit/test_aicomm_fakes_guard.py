"""The `run_aicomm` peer guard on its own: only a wrapper that runs the fake passes."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import sys
from pathlib import Path

import pytest

from _aicomm_fakes import _path_peer_is_test_only, run_aicomm


def setup_env(tmp_path, monkeypatch, script=True):
    """A clean room: PATH (empty bin) and HOME (no install bin) in tmp, no leftovers.

    The guard reads PATH, HOME and `FAKE_PEER_SCRIPT` from the environment; each test
    sets what it needs on top of this, and never runs the real `pool-peer`.
    """
    monkeypatch.delenv("FAKE_PEER_SCRIPT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    if script:
        monkeypatch.setenv("FAKE_PEER_SCRIPT", str(tmp_path / "fake-pool-peer"))


def install(bin_dir: Path, body: str) -> Path:
    """A `pool-peer` in `bin_dir`, executable like the fallback family writes one."""
    peer = bin_dir / "pool-peer"
    peer.parent.mkdir(parents=True, exist_ok=True)
    peer.write_text(body, encoding="utf-8")
    peer.chmod(0o755)
    return peer


def test_a_peer_that_only_mentions_the_fake_in_a_comment_is_rejected(tmp_path, monkeypatch):
    # (1) #393 low-3: a decoy whose text contains the fake path inside a comment - what
    # the old substring check read as a pass - must not be accepted.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(bin_dir, f'#!/bin/sh\n# decoy; the fake one would be: "{fake}"\n'
                     'echo decoy runs nobody\nexit 1\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is False


def test_a_wrapper_that_really_execs_the_fake_is_accepted(tmp_path, monkeypatch):
    # (2) the real thing the PATH/HOME-fallback family puts in place: `exec <python>
    # <fake>`.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(bin_dir, f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is True


def test_a_wrapper_that_execs_the_fake_through_env_is_accepted(tmp_path, monkeypatch):
    # (2b) the #392 decoy shape: `exec env VAR=... <python> <fake>` - an exec chain that
    # ends in the fake is a run, not a mention.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(bin_dir, f'#!/bin/sh\nexec env FAKE_PEER_SCENARIO="/no/such" '
                     f'"{sys.executable}" "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is True


def test_a_home_bin_peer_without_the_fake_is_rejected(tmp_path, monkeypatch):
    # (3) the HOME fallback with a peer that is not a fake wrapper: rejected, so a test
    # that forgot its fake fails instead of running whatever that peer is.
    setup_env(tmp_path, monkeypatch)
    install(tmp_path / "home" / ".mylinuxpool" / "bin",
            '#!/bin/sh\necho peerish, no fake though\nexit 1\n')
    assert _path_peer_is_test_only() is False


def test_a_home_bin_peer_mentioning_the_fake_in_a_comment_is_rejected(tmp_path, monkeypatch):
    # (3b) same through the HOME candidate, and again only as a comment.
    setup_env(tmp_path, monkeypatch)
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(tmp_path / "home" / ".mylinuxpool" / "bin",
            f'#!/bin/sh\n# the fake one is at "{fake}"\nexit 1\n')
    assert _path_peer_is_test_only() is False


def test_no_pool_peer_anywhere_is_test_only_vacuously(tmp_path, monkeypatch):
    # (4) nothing on PATH (the fake `angareion` dir only), nothing in the install bin:
    # no candidate can be the real peer, so the guard allows the fallback run.
    setup_env(tmp_path, monkeypatch)
    assert _path_peer_is_test_only() is True


def test_without_the_fake_script_known_the_guard_refuses(tmp_path, monkeypatch):
    # (5) when `FAKE_PEER_SCRIPT` is unset the guard cannot recognise a wrapper at all:
    # refuse, instead of running anything it cannot name.
    setup_env(tmp_path, monkeypatch, script=False)
    assert "FAKE_PEER_SCRIPT" not in os.environ
    assert _path_peer_is_test_only() is False


def test_a_python_shebang_peer_with_an_exec_line_is_rejected(tmp_path, monkeypatch):
    # (6) #396 mid-2: a candidate that is not a shell wrapper at all (`#!/usr/bin/
    # python3`) stays rejected even when one of its lines execs the fake - the shebang
    # check has to hold on its own, not fall out of the line rules.
    setup_env(tmp_path, monkeypatch)
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(tmp_path / "bin",
            f'#!/usr/bin/python3\nexec "{sys.executable}" "{fake}"\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert _path_peer_is_test_only() is False


def test_a_wrapper_that_execs_cat_on_the_fake_is_rejected(tmp_path, monkeypatch):
    # (7) #396 mid-1: passing the fake to something that is not python is a mention in
    # an exec position, not a run - `exec cat <fake>` just reads the script text out.
    setup_env(tmp_path, monkeypatch)
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(tmp_path / "bin", f'#!/bin/sh\nexec cat "{fake}"\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert _path_peer_is_test_only() is False


def test_a_wrapper_that_execs_a_real_peer_with_the_fake_as_argument_is_rejected(
        tmp_path, monkeypatch):
    # (8) #396 mid-1: the real danger - `exec <real pool-peer> <fake> "$@"` runs the
    # actual peer and hands the fake to it as a harmless-looking extra argument; the
    # command exec'd here is not our python running the fake, so reject.
    setup_env(tmp_path, monkeypatch)
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(tmp_path / "bin",
            f'#!/bin/sh\nexec /usr/local/bin/pool-peer "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert _path_peer_is_test_only() is False


def test_a_wrapper_that_execs_the_fake_through_env_with_assignments_is_accepted(
        tmp_path, monkeypatch):
    # (8b) #396 mid-2 (d): the whole accepted `exec env` shape - `env`, then plain
    # `KEY=V` assignments, then the python, then the fake as its argument.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(bin_dir, f'#!/bin/sh\nexec env KEY=V "{sys.executable}" "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is True


def test_a_dash_shebang_wrapper_is_accepted(tmp_path, monkeypatch):
    # (8c) #396 low-4: `dash` after the path must count as a shell shebang, so the
    # fallback family's dash wrappers are not blocked wholesale.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(bin_dir, f'#!/bin/dash\nexec "{sys.executable}" "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is True


def test_an_env_s_flag_shebang_wrapper_is_accepted(tmp_path, monkeypatch):
    # (8d) #396 low-4: env options between the interpreter and the shell (`-S` for
    # splitting the command string) must not hide the bash behind them.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = os.environ["FAKE_PEER_SCRIPT"]
    install(bin_dir, f'#!/usr/bin/env -S bash -eu\nexec "{sys.executable}" "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is True


def test_a_wrapper_with_a_hash_in_the_fake_path_is_accepted(tmp_path, monkeypatch):
    # (8e) #396 low-5: a fake path containing `#` - shlex only drops comments that
    # start a line/token, it keeps `#` inside a token - must not be cut short.
    setup_env(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    fake = str(tmp_path / "a#b" / "fake-pool-peer")
    monkeypatch.setenv("FAKE_PEER_SCRIPT", fake)
    install(bin_dir, f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    assert _path_peer_is_test_only() is True


def test_run_aicomm_refuses_a_path_peer_without_the_fake(tmp_path, monkeypatch, capsys):
    # (9) #396 low-3: the composed guard - with a `pool-peer` on PATH that does not run
    # the fake and `AICOMM_POOL_PEER` unset, `run_aicomm` itself must refuse, asserting
    # before anything like the real peer gets executed.
    setup_env(tmp_path, monkeypatch)
    install(tmp_path / "bin", '#!/bin/sh\necho real peer passed off as a fake\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("AICOMM_SIDECAR", "http://127.0.0.1:9")  # never contacted here
    with pytest.raises(AssertionError):
        run_aicomm(["wake", "demo"], monkeypatch, capsys)
