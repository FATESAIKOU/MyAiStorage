"""AgoraSessionStore over the fake agora command (task 3.0).

No real agora, no Drive, no agent: a script stands in for `agora` and prints
the fixed stderr lines; the tests check the argv (secret never on it), the
environment allow-list, merge-then-continue, the opencode port and the gentle
stop.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

from sidecar.agora_store import AgoraSessionError, AgoraSessionStore

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE_AGORA = FAKES / "fake_headless_agora.py"
SECRET = "SECRET-ONLY-IN-ENV"


@pytest.fixture
def store(tmp_path, monkeypatch):
    fh = tmp_path / "fh"
    monkeypatch.setenv("FAKE_HOME", str(fh))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-credential-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-credential-must-not-leak")
    workdir = tmp_path / "work"
    workdir.mkdir()
    built = AgoraSessionStore(
        command=[sys.executable, str(FAKE_AGORA)],
        sync_period=5,
        start_timeout=5.0,
        merge_timeout=10.0,
    )
    return built, fh, str(workdir)


def args_log(fh: Path) -> list[list[str]]:
    return [json.loads(line) for line in (fh / "agora-args.log").read_text().splitlines()]


def env_names(fh: Path) -> list[list[str]]:
    return [
        json.loads(line)["env"]
        for line in (fh / "agora-env-names.log").read_text().splitlines()
    ]


def test_continue_spawns_headless_and_reads_the_fixed_lines(store):
    built, fh, workdir = store
    running = built.start(
        mode="continue", ids=["agora:A"], by="claude-code", agent="claude-code",
        workdir=workdir, tag="sidecar:t1", env={"FAKE_AGENT_KEY": SECRET},
    )

    argv = args_log(fh)[-1]  # the fake logs sys.argv[1:] (no interpreter path)
    assert argv == [
        "continue", "session", "agora:A", "--agent", "claude", "--headless",
        "--add-tag", "sidecar:t1", "--dir", workdir, "--sync-period", "5",
    ]
    assert running.session_id == "agora:A"
    assert running.process.agent_session_id == "ses_fake000000000000"
    assert running.process.port is None

    assert SECRET not in json.dumps(argv)  # the secret never reaches argv
    names = env_names(fh)[-1]
    assert "FAKE_AGENT_KEY" in names  # it did reach the environment
    assert "ANTHROPIC_API_KEY" not in names  # the parent's credential did not
    assert "OPENAI_API_KEY" not in names

    assert running.process.terminate(timeout=5.0) == 0
    assert (fh / "agora-stopped").exists()  # the gentle stop's write-back marker


def test_fork_passes_the_fork_flag(store):
    built, fh, workdir = store
    built.start(mode="fork", ids=["agora:A"], by="claude-code", agent="claude-code",
                workdir=workdir, tag="sidecar:t1", env={})
    argv = args_log(fh)[-1]
    assert argv[argv.index("--fork")] == "--fork"
    assert argv[argv.index("--dir") + 1] == workdir


def test_merge_runs_merge_first_with_the_by_env(store):
    built, fh, workdir = store
    running = built.start(
        mode="merge", ids=["agora:A", "agora:B"], by="opencode", agent="claude-code",
        workdir=workdir, tag="sidecar:t1",
        env={"FAKE_AGENT_KEY": "agent-env"},
        merge_env={"FAKE_BY_KEY": "by-env"},
    )
    calls = args_log(fh)
    assert calls[0] == ["merge", "session", "agora:A", "agora:B", "--agent", "opencode"]
    assert calls[1][:3] == ["continue", "session", "agora:FAKEMERGED000000000000000"]
    assert calls[1][calls[1].index("--agent") + 1] == "claude"
    assert running.session_id == "agora:FAKEMERGED000000000000000"

    merge_names, continue_names = env_names(fh)[:2]
    assert "FAKE_BY_KEY" in merge_names and "FAKE_AGENT_KEY" not in merge_names
    assert "FAKE_AGENT_KEY" in continue_names and "FAKE_BY_KEY" not in continue_names


def test_merge_failure_is_an_error(store, monkeypatch):
    built, fh, workdir = store
    monkeypatch.setenv("FAKE_AGORA_MERGE_FAIL", "1")
    with pytest.raises(AgoraSessionError) as excinfo:
        built.start(mode="merge", ids=["agora:A", "agora:B"], by="opencode",
                    agent="claude-code", workdir=workdir, tag="tag", env={})
    assert "merge" in str(excinfo.value)
    assert len(args_log(fh)) == 1  # nothing continued


def test_opencode_gets_a_free_port_and_the_port_line(store):
    built, fh, workdir = store
    running = built.start(
        mode="continue", ids=["agora:A"], by="opencode", agent="opencode",
        workdir=workdir, tag="sidecar:t1", env={},
    )
    argv = args_log(fh)[-1]
    port = int(argv[argv.index("--headless-port") + 1])
    assert 0 < port <= 65535
    assert running.process.port == port  # the line is the authority
    assert running.process.agent_session_id.startswith("ses_")
    running.process.terminate(timeout=5.0)


def test_a_missing_fixed_line_times_out_and_kills_the_process(tmp_path, monkeypatch):
    fh = tmp_path / "fh"
    monkeypatch.setenv("FAKE_HOME", str(fh))
    monkeypatch.setenv("FAKE_AGORA_NO_AGENT_LINE", "1")
    workdir = tmp_path / "work"
    workdir.mkdir()
    built = AgoraSessionStore(command=[sys.executable, str(FAKE_AGORA)], start_timeout=0.5)
    with pytest.raises(AgoraSessionError) as excinfo:
        built.start(mode="continue", ids=["agora:A"], by="claude-code",
                    agent="claude-code", workdir=str(workdir), tag="tag", env={})
    assert "agora-agent-session" in str(excinfo.value)
    deadline = time.monotonic() + 5
    while not (fh / "agora-stopped").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (fh / "agora-stopped").exists()  # aborted, not left running


def test_an_early_exit_is_an_error(tmp_path, monkeypatch):
    fh = tmp_path / "fh"
    monkeypatch.setenv("FAKE_HOME", str(fh))
    monkeypatch.setenv("FAKE_AGORA_EXIT_EARLY", "1")
    monkeypatch.setenv("FAKE_AGORA_NO_AGENT_LINE", "1")
    workdir = tmp_path / "work"
    workdir.mkdir()
    built = AgoraSessionStore(command=[sys.executable, str(FAKE_AGORA)], start_timeout=5.0)
    with pytest.raises(AgoraSessionError) as excinfo:
        built.start(mode="continue", ids=["agora:A"], by="claude-code",
                    agent="claude-code", workdir=str(workdir), tag="tag", env={})
    assert "exited" in str(excinfo.value)


def test_refusing_sigterm_escalates_to_sigkill(tmp_path, monkeypatch):
    fh = tmp_path / "fh"
    monkeypatch.setenv("FAKE_HOME", str(fh))
    monkeypatch.setenv("FAKE_AGORA_REFUSE_TERM", "1")
    workdir = tmp_path / "work"
    workdir.mkdir()
    built = AgoraSessionStore(command=[sys.executable, str(FAKE_AGORA)], start_timeout=5.0)
    running = built.start(mode="continue", ids=["agora:A"], by="claude-code",
                          agent="claude-code", workdir=str(workdir), tag="tag", env={})
    assert running.process.terminate(timeout=0.3) == -signal.SIGKILL
    assert not (fh / "agora-stopped").exists()  # SIGKILL never writes back (D6)


def test_unknown_agent_and_mode_are_refused(store):
    built, fh, workdir = store
    with pytest.raises(AgoraSessionError):
        built.start(mode="continue", ids=["agora:A"], by="claude-code", agent="other",
                    workdir=workdir, tag="tag", env={})
    with pytest.raises(AgoraSessionError):
        built.start(mode="new", ids=[], by="claude-code", agent="claude-code",
                    workdir=workdir, tag="tag", env={})
    assert not (fh / "agora-args.log").exists()  # nothing was spawned
