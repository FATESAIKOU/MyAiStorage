"""The orphan guard: an agent must not outlive a `kill -9`ed agora (#229).

A SIGKILL or an OOM kill runs no handler, so `agora continue` says nothing and
the agent (fake agents only here) would keep running forever. agora starts a
tiny reaper next to the agent and holds one end of a pipe: the goodbye byte
means "agora left on its own", EOF without it means "agora was killed". These
tests pin the reaper's whole contract: normal leave kills nothing, a killed
agora TERMs the agent and KILLs one that ignores TERM, a reused pid is never
signalled, and a reaper that cannot start is only a warning.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agora import cli, orphan_guard
from agora.agents import claude as C

REPO = Path(__file__).resolve().parent.parent
FAKES = REPO / "fakes"
FAKE_RCLONE = FAKES / "fake_rclone.py"
FAKE_CLAUDE = FAKES / "fake_claude.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
CL_SID = "11111111-2222-4333-8444-555555555555"
SECONDS = "0.005"


# --- helpers ----------------------------------------------------------------


def _wrapper(tmp_path, name, target):
    w = tmp_path / name
    w.write_text(f"#!/bin/sh\nexec {sys.executable} {target} \"$@\"\n")
    w.chmod(0o755)
    return w


def _with_fakes(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(_wrapper(tmp_path, "rclone", FAKE_RCLONE)))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))


@pytest.fixture
def claude_lab(tmp_path, monkeypatch):
    """The real claude adapter over the fake CLI, with the fixture session laid out."""
    _with_fakes(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    work = tmp_path / "proj"
    work.mkdir()
    proj = home / ".claude" / "projects" / C.encode_project_dir(work)
    proj.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(_wrapper(tmp_path, "claude", FAKE_CLAUDE)))
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    return {"home": home, "work": work}


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def _import_claude(capsys):
    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    return a


def _sleeper(*, ignore_term=False) -> subprocess.Popen:
    """A stand-in agent that sleeps until it is signalled (and says it is up)."""
    code = ("import signal, sys, time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "print('ready', flush=True)\n"
              "time.sleep(600)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert proc.stdout.readline().strip() == b"ready"
    return proc


def _reaper(agent_pid: int, *extra, env=None) -> tuple[subprocess.Popen, int]:
    """Start the reaper exactly as agora does; return it and the pipe's write end.

    The test plays agora: it keeps the write end, and killing the "agora" that
    holds it (the test process itself would be the model, so a real little
    process does it) closes the pipe.
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agora.orphan_guard", "--pid", str(agent_pid),
         "--fd", str(read_fd), *extra],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, close_fds=True, pass_fds=(read_fd,), cwd=str(REPO),
        env={**os.environ, **(env or {})},
    )
    os.close(read_fd)
    return proc, write_fd


def _fake_agora(write_fd: int) -> subprocess.Popen:
    """A process holding the pipe's write end and saying nothing: a SIGKILLed agora."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(600)"],
                            pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(write_fd)          # only the fake agora keeps a copy now
    return proc


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _pids_matching(marker: str) -> list[int]:
    out = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True, text=True,
                         timeout=10)
    pids = []
    for line in out.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and marker in command:
            pids.append(int(pid))
    return pids


def _wait_gone(pid: int, timeout=30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if orphan_guard.start_time(pid) is None:
            return True
        time.sleep(0.05)
    return orphan_guard.start_time(pid) is None


# --- ① the normal leave -----------------------------------------------------


def test_a_normal_leave_says_goodbye_and_kills_nothing():
    agent = _sleeper()
    try:
        guard = orphan_guard.start(agent.pid)
        assert guard is not None
        guard.done()
        assert guard.proc.wait(timeout=15) == 0, "the reaper reads b'D' and leaves 0"
        assert agent.poll() is None, "a normal leave must not touch the agent"
    finally:
        _stop(agent)


# --- ② a killed agora -------------------------------------------------------


def test_a_sigkilled_agora_terms_the_agent():
    agent = _sleeper()
    reaper = write_fd = None
    agora = None
    try:
        reaper, write_fd = _reaper(agent.pid, "--grace", "5")
        agora = _fake_agora(write_fd)
        agora.kill()                            # kill -9: no goodbye byte is ever written
        assert agora.wait(timeout=10) == -signal.SIGKILL
        assert agent.wait(timeout=30) == -signal.SIGTERM
        assert reaper.wait(timeout=15) == 0
    finally:
        _stop(agora)
        _stop(reaper)
        _stop(agent)


def test_an_agent_that_ignores_term_is_killed():
    agent = _sleeper(ignore_term=True)
    reaper = write_fd = None
    agora = None
    try:
        reaper, write_fd = _reaper(agent.pid, "--grace", "0.3")
        agora = _fake_agora(write_fd)
        agora.kill()
        assert agora.wait(timeout=10) == -signal.SIGKILL
        assert agent.wait(timeout=30) == -signal.SIGKILL, "TERM was ignored, so KILL it"
        assert reaper.wait(timeout=15) == 0
    finally:
        _stop(agora)
        _stop(reaper)
        _stop(agent)


# --- ③ a reused pid is somebody else ---------------------------------------


def test_a_reused_pid_is_not_signalled():
    """The reaper's start time says "not that process", so it sends nothing."""
    agent = _sleeper()
    reaper = write_fd = None
    agora = None
    try:
        reaper, write_fd = _reaper(agent.pid, "--grace", "0.3",
                                      "--start", "Thu Jan  1 00:00:00 1970")
        agora = _fake_agora(write_fd)
        agora.kill()
        assert agora.wait(timeout=10) == -signal.SIGKILL
        assert reaper.wait(timeout=15) == 0
        assert agent.poll() is None, "a pid that answers with another start time is not ours"
    finally:
        _stop(agora)
        _stop(reaper)
        _stop(agent)


def test_same_process_compares_the_start_time(monkeypatch):
    live = orphan_guard.start_time(os.getpid())
    assert live
    assert orphan_guard.same_process(os.getpid(), live)
    assert not orphan_guard.same_process(os.getpid(), live + " (other)")
    assert not orphan_guard.same_process(os.getpid(), None)


# --- ④ a reaper that cannot start is a warning, not a failed continue -------


def test_a_reaper_that_cannot_start_does_not_fail_continue(claude_lab, capsys, tmp_path,
                                                           monkeypatch):
    a = _import_claude(capsys)
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", "append:ZZOG 一句,exit")

    def boom(*args, **kwargs):
        raise OSError("no more processes today")

    monkeypatch.setattr(orphan_guard, "_spawn", boom)
    code = cli.main(["continue", "session", a, "--agent", "claude",
                     "--dir", str(claude_lab["work"]), "--sync-period", "0"])
    err = capsys.readouterr().err
    assert code in (0, cli.EXIT_IN_OUTBOX), err
    assert "孤兒防護" in err, err
    # The agent itself ran and its turn was written back: only the reaper was lost.
    code, shown, err = run(capsys, "show", "session", a)
    assert code == 0, err
    assert "ZZOG 一句" in shown


# --- the goodbye byte, through the real `agora continue` (review M2) --------


_READER = """
import json, os, sys
fd, out, argv_out = int(sys.argv[1]), sys.argv[2], sys.argv[3]
with open(argv_out, "w", encoding="utf-8") as f:
    json.dump(sys.argv[4:], f, ensure_ascii=False)
data = b""
while True:
    chunk = os.read(fd, 64)
    if not chunk:
        break
    data += chunk
os.close(fd)
with open(out, "wb") as f:
    f.write(data)
"""


class _RecordingReaper:
    """The reaper's place in a test: a real process that reports what it got.

    It stands in for `orphan_guard._spawn`, so the pipe, the goodbye byte and
    the wait in `Guard.done` are the ones agora really uses; only what happens
    at the far end is replaced by "write down the bytes and the argv".
    """

    def __init__(self, proc: subprocess.Popen, argv: list[str], out: Path, argv_out: Path):
        self.proc = proc
        self.argv = argv
        self.out = out
        self.argv_out = argv_out

    def wait(self, timeout=None):
        return self.proc.wait(timeout=timeout)

    def received(self, timeout=10.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.out.exists():
                return self.out.read_bytes()
            time.sleep(0.01)
        raise AssertionError("the stand-in reaper never reported: agora said no goodbye")


@pytest.fixture
def reaper_log(tmp_path, monkeypatch):
    """Every reaper agora starts is one of these; each is stopped at the end."""
    seen: list[_RecordingReaper] = []

    def fake_spawn(argv, **kwargs):
        fd = int(argv[argv.index("--fd") + 1])
        tag = len(seen)
        out = tmp_path / f"reaper-{tag}.bytes"
        argv_out = tmp_path / f"reaper-{tag}.argv.json"
        proc = subprocess.Popen([sys.executable, "-c", _READER, str(fd), str(out),
                                 str(argv_out), *argv],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, pass_fds=(fd,))
        seen.append(_RecordingReaper(proc, argv, out, argv_out))
        return seen[-1]

    monkeypatch.setattr(orphan_guard, "_spawn", fake_spawn)
    yield seen
    for reaper in seen:
        _stop(reaper.proc)


def test_a_real_continue_says_goodbye_on_its_normal_path(claude_lab, capsys, monkeypatch,
                                                         reaper_log):
    """`cmd_continue`'s own `finally` says goodbye: a normal leave kills nothing."""
    a = _import_claude(capsys)
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", "append:ZZOG 一句,exit")
    code = cli.main(["continue", "session", a, "--agent", "claude",
                     "--dir", str(claude_lab["work"]), "--sync-period", "0"])
    err = capsys.readouterr().err
    assert code in (0, cli.EXIT_IN_OUTBOX), err
    assert len(reaper_log) == 1, f"one agent, one reaper; got {len(reaper_log)}"
    assert reaper_log[0].received() == b"D", (
        f"a normal leave must say goodbye; agora's stderr was {err!r}")


def test_a_real_continue_says_goodbye_when_the_write_back_blows_up(claude_lab, capsys,
                                                                    monkeypatch, reaper_log):
    """The write-back's exception leaves through the same `finally`: goodbye too."""
    a = _import_claude(capsys)
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", "append:ZZOG 一句,exit")
    monkeypatch.delenv("AGORA_DEBUG", raising=False)

    def boom(run, agent):
        raise RuntimeError("模擬寫回失敗")

    monkeypatch.setattr(cli, "_final_write", boom)
    code = cli.main(["continue", "session", a, "--agent", "claude",
                     "--dir", str(claude_lab["work"]), "--sync-period", "0"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_ERROR, err
    assert len(reaper_log) == 1, f"one agent, one reaper; got {len(reaper_log)}"
    assert reaper_log[0].received() == b"D", (
        f"a failed write-back must still say goodbye; agora's stderr was {err!r}")


def test_the_reaper_is_told_the_agents_start_time(reaper_log):
    """agora reads it before the reaper boots (review L2), so a reused pid is caught."""
    agent = _sleeper()
    try:
        recorded = orphan_guard.start_time(agent.pid)
        assert recorded
        guard = orphan_guard.start(agent.pid)
        assert guard is not None
        guard.done()
        argv = reaper_log[0].argv
        assert "--start" in argv, argv
        assert argv[argv.index("--start") + 1] == recorded
        assert reaper_log[0].received() == b"D"
    finally:
        _stop(agent)


# --- the same thing through the real `agora continue` -----------------------


def test_killing_agora_continue_takes_the_agent_with_it(claude_lab, capsys, tmp_path,
                                                        monkeypatch):
    """End to end: a headless continue is SIGKILLed and the fake agent goes too."""
    a = _import_claude(capsys)
    release = tmp_path / "never-created"
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", f"wait-file:{release}")
    monkeypatch.setenv("FAKE_SYNC_TIMEOUT", "60")
    monkeypatch.setenv("AGORA_ORPHAN_GRACE", "0.3")   # the grace is the env's to shorten

    out_path, err_path = tmp_path / "headless.out", tmp_path / "headless.err"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        agora = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", a,
             "--agent", "claude", "--dir", str(claude_lab["work"]), "--headless",
             "--sync-period", SECONDS],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=dict(os.environ),
            cwd=str(tmp_path),
        )
    try:
        deadline = time.monotonic() + 60
        marker = None
        while time.monotonic() < deadline:
            for line in err_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("agora-agent-session "):
                    marker = line.split(" ", 1)[1].strip()
            if marker:
                break
            if agora.poll() is not None:
                raise AssertionError(f"continue died early: {err_path.read_text()!r}")
            time.sleep(0.02)
        assert marker, f"never saw the agent session line: {err_path.read_text()!r}"
        pids = _pids_matching(marker)
        assert len(pids) == 1, f"expected exactly the fake agent, got {pids}"
        agent_pid = pids[0]

        agora.kill()                                  # SIGKILL: no goodbye byte
        assert agora.wait(timeout=30) == -signal.SIGKILL
        assert _wait_gone(agent_pid, timeout=30), (
            "the orphaned agent is still running; stderr was "
            f"{err_path.read_text(encoding='utf-8', errors='replace')!r}")
    finally:
        _stop(agora)
        for pid in _pids_matching(locals().get("marker") or "\0"):
            try:                                      # only this test's own process
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
