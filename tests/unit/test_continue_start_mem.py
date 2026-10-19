"""The shell must not keep the loaded session alive while the agent runs (impl-mem-start).

The second slice moved each periodic round to a child, so the shell stopped
growing with the number of rounds. But `cmd_continue` itself still held `own` /
`raw` (and, for a fork, `exported`) in its frame for the whole run: a 100 MB
session left the shell at ~128 MB from launch to the first round. These tests
put a large, recognisable session through the real command and inspect
`cmd_continue`'s own frame at the moment the agent process is started - the
state the shell keeps while it waits. Nothing session-sized may still be there.
This test guards that the shell frame does not retain the big objects; that the
load really happens in the child is guarded by `test_continue_start_child.py`.

The check is by value, not by RSS: direct locals of `cmd_continue` that are
bytes or `Exported`, at least half the fake session's size. That is stable
across allocators, `gc` timing and the machine's memory pressure; the RSS
acceptance numbers are in tests/perf/measure_continue_start_rss.py.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import sys
from pathlib import Path

from agora import cli
from agora.agents.base import Exported
from _round_seam import in_process_start_child
from test_cli_fork import ForkAgent
from test_cli_sync import SyncAgent

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
BIG = 6 << 20


def _env(tmp_path, monkeypatch, agent):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    return agent


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def _frame_named(name: str):
    frame = sys._getframe()
    while frame is not None:
        if frame.f_code.co_name == name:
            return frame
        frame = frame.f_back
    raise AssertionError(f"{name} is not on the stack")


def _session_sized_locals(floor: int) -> dict[str, int]:
    """Direct locals of `cmd_continue` big enough to be the loaded session."""
    hits: dict[str, int] = {}
    for name, value in _frame_named("cmd_continue").f_locals.items():
        if isinstance(value, bytes) and len(value) >= floor:
            hits[name] = len(value)
        elif isinstance(value, Exported) and len(value.raw) >= floor:
            hits[name] = len(value.raw)
    return hits


def _watch_agent_launch(monkeypatch, argv0: str, seen: list[dict[str, int]]) -> None:
    """Inspect `cmd_continue` once the agent process is about to start (startup done)."""
    real = cli.subprocess.Popen

    def spy(argv, **kwargs):
        if isinstance(argv, (list, tuple)) and list(argv[:1]) == [argv0]:
            seen.append(_session_sized_locals(BIG // 2))
        return real(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", spy)


def _import_big(tmp_path, monkeypatch, capsys, agent, text: str) -> str:
    agent.sessions["ses_a"] = [text]
    _env(tmp_path, monkeypatch, agent)
    code, out, err = _run(capsys, "import", "session", "--external-session-id", "ses_a",
                          "--agent", "opencode")
    assert code == 0, err
    return out


def test_continue_releases_the_loaded_bytes_before_the_agent_runs(tmp_path, monkeypatch, capsys):
    agent = SyncAgent()
    agent.sleep = 0.5
    target = _import_big(tmp_path, monkeypatch, capsys, agent, "ZZBIG 開始" + "x" * BIG)
    seen: list[dict[str, int]] = []
    _watch_agent_launch(monkeypatch, "sleep", seen)

    code, out, err = _run(capsys, "continue", "session", target, "--agent", "opencode",
                          "--dir", str(tmp_path), "--sync-period", "0")

    assert code == 0, err
    assert seen, "the agent process was never started"
    assert seen[0] == {}, f"cmd_continue still holds the session-sized values: {seen[0]}"


def test_fork_does_not_keep_the_exported_session_alive_either(tmp_path, monkeypatch, capsys):
    agent = ForkAgent()
    target = _import_big(tmp_path, monkeypatch, capsys, agent, "ZZBIG 開始" + "x" * BIG)
    seen: list[dict[str, int]] = []
    _watch_agent_launch(monkeypatch, "true", seen)

    code, out, err = _run(capsys, "continue", "session", target, "--agent", "opencode",
                          "--fork", "--dir", str(tmp_path))

    assert code == 0, err
    assert seen, "the agent process was never started"
    assert seen[0] == {}, f"cmd_continue still holds the session-sized values: {seen[0]}"
