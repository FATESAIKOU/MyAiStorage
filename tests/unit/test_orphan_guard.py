"""The orphan guard: an agent must not outlive a `kill -9`ed agora (#229).

A SIGKILL or an OOM kill runs no handler, so `agora continue` says nothing and
the agent (fake agents only here) would keep running forever. agora starts a
tiny reaper *before* the agent (#422) and holds one end of a pipe: the agent's
own child writes ``P<pid>``, agora adds ``S<lstart>``, the line ``D`` means
"agora left on its own", EOF without it means "agora was killed". These tests
pin the reaper's whole contract: normal leave kills nothing, a killed agora
TERMs the agent and KILLs one that ignores TERM, a reused pid is never
signalled, an agora killed at any moment after the fork leaves no orphan
(contracts C-1..C-14 of docs/design-notes/422-reaper-order.md), and a reaper
that cannot start is only a warning.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import ast
import os
import shutil
import signal
import subprocess
import sys
import threading
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


def _reaper(agent_pid: int, *extra, start: str | None = None, env=None) -> tuple[subprocess.Popen, int]:
    """Start the reaper exactly as agora does; return it and the pipe's write end.

    The test plays agora (and the agent's child): it writes ``P<agent_pid>``
    (and ``S<start>`` when given) and keeps the write end; killing the "agora"
    that holds it (a real little process does it) closes the pipe.
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agora.orphan_guard", "--fd", str(read_fd), *extra],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, close_fds=True, pass_fds=(read_fd,), cwd=str(REPO),
        env={**os.environ, **(env or {})},
    )
    os.close(read_fd)
    os.write(write_fd, b"P%d\n" % agent_pid)
    if start is not None:
        os.write(write_fd, b"S" + os.fsencode(start) + b"\n")
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
    # `-ww`: procps truncates an `-o` column to the output width (80 under pytest,
    # which hands its children COLUMNS=80), and the marker sits at the end of the
    # agent's command line.
    out = subprocess.run(["ps", "-eww", "-o", "pid=,command="], capture_output=True, text=True,
                         timeout=10)
    pids = []
    for line in out.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and marker in command:
            pids.append(int(pid))
    return pids


def _reaper_pids(agora_pid: int) -> list[int]:
    """The reapers (`python -m agora.orphan_guard --fd N`) that are agora's children."""
    out = subprocess.run(["ps", "-eww", "-o", "pid=,ppid=,command="], capture_output=True,
                         text=True, timeout=10)
    pids = []
    for line in out.stdout.splitlines():
        fields = line.split(None, 2)
        if (len(fields) == 3 and fields[1] == str(agora_pid)
                and "agora.orphan_guard --fd " in fields[2] + " "):
            pids.append(int(fields[0]))
    return pids


def _wait_for(check, timeout: float):
    """Poll `check()` until it is truthy (returned) or `timeout` seconds pass (falsy)."""
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.02)


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
        guard = orphan_guard.arm()
        assert guard is not None
        # the agent's child reports its pid, agora adds the start time, then goodbye
        child = subprocess.Popen(["sleep", "30"], preexec_fn=guard.child_hook)
        guard.started(child.pid)
        guard.done()
        assert guard.proc.wait(timeout=15) == 0, "the reaper reads the D line and leaves 0"
        assert child.poll() is None, "a normal leave must not touch the agent"
        _stop(child)
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
                                   start="Thu Jan  1 00:00:00 1970")
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
    assert reaper_log[0].received().endswith(b"D\n"), (
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
    assert reaper_log[0].received().endswith(b"D\n"), (
        f"a failed write-back must still say goodbye; agora's stderr was {err!r}")


def test_the_reaper_is_told_the_agents_start_time(reaper_log):
    """agora reads it before any wait (review L2), so a reused pid is caught."""
    agent = _sleeper()
    try:
        recorded = orphan_guard.start_time(agent.pid)
        assert recorded
        g = orphan_guard.arm()
        assert g is not None
        g.started(agent.pid)
        g.done()
        argv = reaper_log[0].argv
        assert "--fd" in argv and "--pid" not in argv and "--start" not in argv, argv
        assert set(a for a in argv if a.startswith("--")) <= {"--fd", "--grace"}, argv
        assert reaper_log[0].received() == b"S" + os.fsencode(recorded) + b"\nD\n"
    finally:
        _stop(agent)


# --- the same thing through the real `agora continue` -----------------------


class _Continue:
    """A real `python -m agora.cli continue` with the fake agent; cleans up after itself."""

    def __init__(self, lab, tmp_path, a, *, headless=True, fault=None, release=None, token=None):
        self.out_path, self.err_path = tmp_path / "c.out", tmp_path / "c.err"
        env = dict(os.environ)
        env["FAKE_AGENT_SCRIPT"] = f"wait-file:{release or tmp_path / 'never-created'}"
        env["FAKE_SYNC_TIMEOUT"] = "60"
        env["AGORA_ORPHAN_GRACE"] = "0.3"           # the grace is the env's to shorten
        if fault:
            env["AGORA_TEST_FAULT"] = fault
        argv = [sys.executable, "-m", "agora.cli", "continue", "session", a,
                "--agent", "claude", "--dir", str(lab["work"]), "--sync-period", SECONDS]
        if headless:
            argv.append("--headless")
        if token:                       # a word only this agent's command line carries
            argv.append(f"--agent-arg=--{token}")
        with open(self.out_path, "wb") as out, open(self.err_path, "wb") as err:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                         env=env, cwd=str(tmp_path))

    def stderr(self) -> str:
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def marker(self, timeout=60) -> str:
        """The agent's session id: printed before the agent is spawned (headless)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.stderr().splitlines():
                if line.startswith("agora-agent-session "):
                    return line.split(" ", 1)[1].strip()
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(f"never saw the agent session line: {self.stderr()!r}")

    def cleanup(self, marker=None):
        _stop(self.proc)
        for pid in _pids_matching(marker or "\0"):
            try:                                      # only this test's own process
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_killing_agora_continue_takes_the_agent_with_it(claude_lab, capsys, tmp_path):
    """End to end: a headless continue is SIGKILLed and the fake agent goes too."""
    a = _import_claude(capsys)
    c = _Continue(claude_lab, tmp_path, a)
    marker = None
    try:
        marker = c.marker()
        pids = _wait_for(lambda: (p := _pids_matching(marker)) and len(p) == 1 and p, 60) or \
            _pids_matching(marker)
        assert len(pids) == 1, f"expected exactly the fake agent, got {pids}"
        agent_pid = pids[0]
        # #422: the reaper is armed before the agent exists, so it is already agora's child.
        assert _reaper_pids(c.proc.pid), f"no reaper beside agora; stderr was {c.stderr()!r}"

        c.proc.kill()                                 # SIGKILL: no goodbye line
        assert c.proc.wait(timeout=30) == -signal.SIGKILL
        assert _wait_gone(agent_pid, timeout=30), (
            f"the orphaned agent is still running; stderr was {c.stderr()!r}")
    finally:
        c.cleanup(marker)


# --- #422: the order of things; agora killed at each point ------------------


def _no_agent_within(marker: str, timeout: float) -> bool:
    return bool(_wait_for(lambda: not _pids_matching(marker), timeout))


def _born(tmp_path, marker: str) -> bool:
    """Did the fake agent ever start? (It logs its argv first thing.)"""
    log = tmp_path / "fh" / "claude-args.log"
    return log.exists() and marker in log.read_text(encoding="utf-8")


def test_c1_agora_killed_right_after_popen_leaves_no_orphan(claude_lab, capsys, tmp_path):
    """C-1 (T5, the #415 window): dead between `Popen` and `started()`."""
    a = _import_claude(capsys)
    c = _Continue(claude_lab, tmp_path, a, fault="after-agent-popen")
    marker = None
    try:
        marker = c.marker()
        assert c.proc.wait(timeout=60) == 137, c.stderr()
        assert _no_agent_within(marker, 30), (
            f"the agent outlived agora; stderr was {c.stderr()!r}")
    finally:
        c.cleanup(marker)


def test_c2_agora_killed_in_the_agent_child_before_it_writes_its_pid(claude_lab, capsys, tmp_path):
    """C-2 (T3): dead between fork and the child's `P` line."""
    a = _import_claude(capsys)
    c = _Continue(claude_lab, tmp_path, a, fault="agent-child-before-pid")
    marker = None
    try:
        marker = c.marker()
        assert c.proc.wait(timeout=60) == -signal.SIGKILL, c.stderr()
        assert _no_agent_within(marker, 30), (
            f"the agent outlived agora; stderr was {c.stderr()!r}")
    finally:
        c.cleanup(marker)


def test_c3_the_agents_own_child_writes_its_pid_first(reaper_log):
    """C-3: `P<pid>` is written by the child in `preexec_fn`, before agora says anything."""
    g = orphan_guard.arm()
    assert g is not None
    child = subprocess.Popen(["sleep", "5"], preexec_fn=g.child_hook)
    try:
        os.close(g.fd)                     # no `started()`: only the child spoke
        g._done = True
        assert reaper_log[0].received() == b"P%d\n" % child.pid
    finally:
        _stop(child)


def test_c4_agora_killed_before_the_agent_is_born_kills_nothing(claude_lab, capsys, tmp_path):
    """C-4 (T2, process level): the reaper is armed, there is no agent to collect."""
    a = _import_claude(capsys)
    c = _Continue(claude_lab, tmp_path, a, fault="after-guard-armed")
    marker = None
    try:
        marker = c.marker()
        assert c.proc.wait(timeout=60) == 137, c.stderr()
        time.sleep(3)
        assert not _pids_matching(marker), "no agent should have been born"
        assert not _born(tmp_path, marker), "the agent must never have started"
    finally:
        c.cleanup(marker)


def test_c4_a_real_reaper_with_nothing_written_leaves_0():
    g = orphan_guard.arm()
    assert g is not None
    os.close(g.fd)
    g._done = True
    assert g.proc.wait(timeout=10) == 0


def _feed(data: bytes) -> int:
    r, w = os.pipe()
    os.write(w, data)
    os.close(w)
    return r


@pytest.fixture
def stops(monkeypatch):
    """`stop_orphan` and `start_time` replaced; `stops` lists the stop calls."""
    calls = []
    monkeypatch.setattr(orphan_guard, "stop_orphan",
                        lambda pid, recorded, grace: calls.append((pid, recorded)) or "term")
    monkeypatch.setattr(orphan_guard, "start_time", lambda pid: "X")
    return calls


def test_c4_main_with_only_eof_stops_nothing(stops):
    assert orphan_guard.main(["--fd", str(_feed(b""))]) == 0
    assert stops == []


def test_c5_an_s_line_containing_Dec_is_not_a_goodbye(stops):
    assert orphan_guard.main(["--fd", str(_feed(b"P123\nSMon Dec  1 00:00:00 2025\n"))]) == 0
    assert stops == [(123, "Mon Dec  1 00:00:00 2025")]


@pytest.mark.parametrize("data, stopped", [
    (b"P123\nD\n", False),
    (b"D\n", False),
    (b"P123\nwhat is this\n", True),
    (b"P123\nD", True),                     # no newline: a torn line is not a goodbye
])
def test_c5_the_goodbye_is_a_line_that_is_exactly_D(stops, data, stopped):
    assert orphan_guard.main(["--fd", str(_feed(data))]) == 0
    assert bool(stops) is stopped


def test_c6_without_an_s_line_the_reaper_uses_its_own_reading(stops):
    orphan_guard.main(["--fd", str(_feed(b"P123\n"))])
    assert stops == [(123, "X")]


def test_c6_an_unreadable_start_time_kills_nothing(stops, monkeypatch):
    monkeypatch.setattr(orphan_guard, "start_time", lambda pid: None)
    orphan_guard.main(["--fd", str(_feed(b"P123\n"))])
    assert stops == []


def test_c6_the_s_line_overrides_the_reapers_own_reading(monkeypatch):
    """The reaper read X (the pid's later owner); agora's S says Y: not the same process."""
    killed = []
    monkeypatch.setattr(orphan_guard, "start_time", lambda pid: "X")
    monkeypatch.setattr(orphan_guard, "_children", lambda pid: [])
    monkeypatch.setattr(orphan_guard.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert orphan_guard.main(["--fd", str(_feed(b"P123\nSY\n")), "--grace", "0"]) == 0
    assert killed == [], "S said Y, the pid now answers X: a stranger, never signalled"


def test_c7_an_agent_still_starts_when_the_reaper_is_already_dead(reaper_log, tmp_path):
    g = orphan_guard.arm()
    assert g is not None
    reaper_log[0].proc.kill()
    reaper_log[0].proc.wait(timeout=10)
    wrote = tmp_path / "ran"
    child = subprocess.Popen([sys.executable, "-c", f"open({str(wrote)!r}, 'w').write('x')"],
                             preexec_fn=g.child_hook)
    assert child.wait(timeout=30) == 0, "not -SIGPIPE: the write must not kill the agent"
    assert wrote.exists()
    # and the child is back on the default SIGPIPE: a shell that signals itself dies of it
    probe = subprocess.Popen(["sh", "-c", "kill -s PIPE $$; echo survived"],
                             preexec_fn=g.child_hook, stdout=subprocess.PIPE)
    out, _ = probe.communicate(timeout=30)
    assert probe.returncode == -signal.SIGPIPE and b"survived" not in out
    os.close(g.fd)
    g._done = True


def test_c8_a_normal_continue_tells_the_reaper_p_s_d_in_that_order(claude_lab, capsys, tmp_path,
                                                                   monkeypatch, reaper_log):
    a = _import_claude(capsys)
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", f"wait-file:{release}")
    monkeypatch.setenv("FAKE_SYNC_TIMEOUT", "60")
    seen = {}
    original = orphan_guard.Guard.started

    def started(self, pid):
        seen["pid"], seen["start"] = pid, orphan_guard.start_time(pid)   # the agent is alive
        original(self, pid)
        release.write_text("go")

    monkeypatch.setattr(orphan_guard.Guard, "started", started)
    safety = threading.Timer(30, lambda: release.write_text("go"))
    safety.start()
    try:
        code = cli.main(["continue", "session", a, "--agent", "claude",
                         "--dir", str(claude_lab["work"]), "--sync-period", "0"])
    finally:
        safety.cancel()
    err = capsys.readouterr().err
    assert code in (0, cli.EXIT_IN_OUTBOX), err
    assert seen.get("start"), f"cli never called started(proc.pid); stderr was {err!r}"
    assert reaper_log[0].received() == (
        b"P%d\n" % seen["pid"] + b"S" + os.fsencode(seen["start"]) + b"\nD\n")


def test_c9_an_exception_between_arm_and_popen_still_says_goodbye(claude_lab, capsys, monkeypatch,
                                                                  reaper_log):
    a = _import_claude(capsys)
    real = subprocess.Popen

    def boom(*args, **kwargs):
        if kwargs.get("preexec_fn") is not None:        # the agent's launch, not the reaper's
            raise OSError("cannot launch the agent")
        return real(*args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    code = cli.main(["continue", "session", a, "--agent", "claude",
                     "--dir", str(claude_lab["work"]), "--sync-period", "0"])
    err = capsys.readouterr().err
    assert code != 0, err
    assert len(reaper_log) == 1
    assert reaper_log[0].received() == b"D\n", "no P (nothing was born), but the goodbye"


def test_c11_the_fixed_stderr_lines_keep_their_order(claude_lab, capsys, tmp_path):
    a = _import_claude(capsys)
    c = _Continue(claude_lab, tmp_path, a)
    marker = None
    try:
        marker = c.marker()
        lines = [ln for ln in c.stderr().splitlines() if ln.startswith("agora-")]
        assert [ln.split()[0] for ln in lines[:2]] == ["agora-session", "agora-agent-session"], lines
        assert lines[1].split()[1] == marker
    finally:
        c.cleanup(marker)


def test_c12_an_interactive_continue_is_covered_too(claude_lab, capsys, tmp_path):
    """Same `Popen`, same fix: a plain (not --headless) continue killed after the Popen."""
    a = _import_claude(capsys)
    token = "zz422-interactive"
    c = _Continue(claude_lab, tmp_path, a, headless=False, fault="after-agent-popen", token=token)
    try:
        assert c.proc.wait(timeout=60) == 137, c.stderr()
        # no session line without --headless (and claude gets a new session id): the extra arg is the marker
        assert _no_agent_within(token, 30), (
            f"the agent outlived agora; stderr was {c.stderr()!r}")
    finally:
        c.cleanup(token)


def test_c13_orphan_guard_imports_nothing_from_agora():
    tree = ast.parse((REPO.parent / "src" / "agora" / "orphan_guard.py").read_text())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        assert not any(n == "agora" or n.startswith("agora.") for n in names), names


def test_c14_a_failed_exec_does_not_hold_the_reaper(monkeypatch):
    monkeypatch.setenv("AGORA_ORPHAN_GRACE", "30")
    g = orphan_guard.arm()
    assert g is not None
    with pytest.raises(FileNotFoundError):
        subprocess.Popen(["/nonexistent/agora-422"], preexec_fn=g.child_hook)
    os.close(g.fd)
    g._done = True
    assert g.proc.wait(timeout=5) == 0
