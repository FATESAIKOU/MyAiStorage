"""Export temp dirs under agora's state, and the sweep for SIGKILL leftovers (#116).

An export puts the whole private conversation on disk before it is staged. A
`TemporaryDirectory` cleaned it on every path but SIGKILL; these tests hold the
mechanism to the two things that matter: the directory is under
`<state>/tmp/` (0700), and the next run removes only the ones whose owner is
really gone - a live `agora continue`'s directory is never taken.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import errno
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from agora import cli, private_tmp, round_child, store
from agora.agents.base import AgentError
from agora.private_tmp import PrivateTmpDir

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
SID = "11111111-2222-4333-8444-555555555555"

EXPORT_AND_WAIT = f"""
    import time
    from agora.agents.claude import ClaudeAgent

    exported = ClaudeAgent().export({SID!r})
    print(exported.raw_file, flush=True)
    time.sleep(120)
"""

EXPORT_ONCE = f"""
    from agora.agents.claude import ClaudeAgent

    ClaudeAgent().export({SID!r})
"""

LIVE_RUN = """
    import time
    from agora.private_tmp import PrivateTmpDir

    here = PrivateTmpDir("agora-raw")
    print(here.name, flush=True)
    time.sleep(120)
"""


@pytest.fixture(autouse=True)
def tempdir_inside_the_test(tmp_path, monkeypatch):
    """Anything still asking for the system temp gets one under tmp_path, and the
    test's state dir is the conftest's own temporary one."""
    tmp = tmp_path / "system-tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    return tmp


@pytest.fixture
def claude_session():
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    proj = home / ".claude" / "projects" / "-tmp-h116"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "cl-basic.jsonl", proj / f"{SID}.jsonl")
    return home


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=os.environ.copy())


def _first_line(proc: subprocess.Popen) -> str:
    line = proc.stdout.readline().strip()
    assert line, f"the child said nothing (rc={proc.poll()})"
    return line


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=20)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def test_temp_dirs_are_under_state_are_0700_and_go_away_on_exit():
    base = Path(os.environ["AGORA_STATE_DIR"]) / private_tmp.DIRNAME
    with PrivateTmpDir("agora-raw") as name:
        path = Path(name)
        assert path.parent == base
        assert path.name.startswith(f"agora-raw-{os.getpid()}-")
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE(base.stat().st_mode) == 0o700
        (path / "raw.json").write_bytes(b'{"secret": true}')
    assert not path.exists(), "the normal path still cleans up after itself"


def test_a_sigkilled_export_leaves_a_residue_that_the_next_export_sweeps(claude_session):
    """The real export, in a real child, killed with SIGKILL: the whole raw is still
    there - private, under the state - and the next run's export removes it while
    leaving a second live run's directory alone."""
    dead = _spawn(EXPORT_AND_WAIT)
    live = None
    live_dir = None
    try:
        residue_file = Path(_first_line(dead))
        assert residue_file.is_file()
        residue_dir = residue_file.parent
        assert residue_dir.parent == Path(os.environ["AGORA_STATE_DIR"]) / private_tmp.DIRNAME
        assert f"-{dead.pid}-" in residue_dir.name, residue_dir.name
        assert stat.S_IMODE(residue_dir.stat().st_mode) == 0o700

        live = _spawn(LIVE_RUN)            # another agora run, still exporting
        live_dir = Path(_first_line(live))
        assert live_dir.is_dir()

        dead.kill()                        # SIGKILL: no finalizer can run
        assert dead.wait(timeout=20) == -signal.SIGKILL
        assert residue_file.is_file(), "SIGKILL left nothing behind: the test proves nothing"

        one = subprocess.run([sys.executable, "-c", textwrap.dedent(EXPORT_ONCE)],
                             capture_output=True, text=True, env=os.environ.copy(),
                             timeout=60)
        assert one.returncode == 0, one.stderr[-1000:]
        assert not residue_dir.exists(), "the next export did not clean up the SIGKILL's residue"
        assert live_dir.is_dir(), "a live run's temp dir was swept"
    finally:
        _stop(dead)
        _stop(live)

    private_tmp.sweep()                    # and now that owner is gone too
    assert not live_dir.exists()


def test_sweep_keeps_a_directory_whose_owner_is_still_alive():
    """The whole point: several `agora continue` runs export at once, and a sweep
    (another run starting, or this one's next export) must not take theirs."""
    live = _spawn(LIVE_RUN)
    live_dir = None
    try:
        live_dir = Path(_first_line(live))
        private_tmp.sweep()
        assert live_dir.is_dir(), "a live run's temp dir was swept"
    finally:
        _stop(live)

    private_tmp.sweep()
    assert not live_dir.exists(), "once the owner is gone the directory is not kept for ever"


def test_sweep_removes_a_directory_that_died_before_it_was_published():
    """Between `mkdtemp` and the owner lock there is a `.new-*` name; a process
    killed there leaves one, and only a sweep can know it is not in flight."""
    base = private_tmp.root()
    orphan = base / f"{private_tmp.SETUP_PREFIX}deadbeef"
    orphan.mkdir()
    (orphan / "raw.json").write_bytes(b"half-written")
    private_tmp.sweep()
    assert not orphan.exists()


def test_sweep_removes_a_published_directory_whose_owner_lock_is_also_gone():
    """#116 review A: a cleanup interrupted after `rmtree` removed `.owner.lock`
    leaves a published name with no lock. Under the gate that is conclusive - a
    directory is published only after its lock exists - so it must not stay for
    ever (Linux ext4 does not promise the lock file goes last)."""
    live = _spawn(LIVE_RUN)
    victim = None
    try:
        victim = Path(_first_line(live))
        live.kill()                        # an interrupted cleanup looks like a dead owner
        assert live.wait(timeout=20) == -signal.SIGKILL
        assert (victim / private_tmp.OWNER_LOCK).is_file()
        (victim / private_tmp.OWNER_LOCK).unlink()
        private_tmp.sweep()                # the missing lock must mean "deletion was interrupted"
        assert not victim.exists(), "a published directory without an owner lock stayed for ever"
    finally:
        _stop(live)


def test_sweep_only_touches_names_it_made():
    """#116 r3: `AGORA_STATE_DIR` is user-controlled, so a `tmp/` that already had
    the user's own directories must survive; only our names (and a dead setup's
    `.new-*`) are ours to remove."""
    base = private_tmp.root()
    notes = base / "notes"
    notes.mkdir(mode=0o700)
    (notes / "keep.txt").write_text("user data", encoding="utf-8")
    near = base / "agora-raw-not-a-pid"          # our prefix, not our name
    near.mkdir(mode=0o700)
    (near / "x").write_text("x", encoding="utf-8")
    short = base / f"{private_tmp.SETUP_PREFIX}short"   # not the 8-char setup name
    short.mkdir(mode=0o700)
    ours = base / "agora-raw-99999-deadbeef"     # our shape, no owner lock: dead
    ours.mkdir(mode=0o700)
    (ours / "raw.json").write_text("private", encoding="utf-8")

    private_tmp.sweep()

    assert (notes / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert near.is_dir() and short.is_dir()
    assert not ours.exists(), "a dead leftover of ours was not cleaned"


def test_an_unregistered_prefix_is_refused():
    """A prefix `sweep` does not recognise would leave directories nobody ever
    cleans; refusing at creation keeps `PREFIXES` and the name regex together."""
    with pytest.raises(ValueError, match="PREFIXES"):
        PrivateTmpDir("notes")


def test_the_temp_root_is_forced_to_0700_even_when_it_already_exists():
    """#116 review 3: `mkdir(mode=0o700)` leaves an existing directory alone, so a
    `<state>/tmp` made 0755 earlier would keep exposing the names."""
    base = Path(os.environ["AGORA_STATE_DIR"]) / private_tmp.DIRNAME
    base.mkdir(parents=True)
    os.chmod(base, 0o755)
    with PrivateTmpDir("agora-raw") as name:
        assert Path(name).parent == base
        assert stat.S_IMODE(base.stat().st_mode) == 0o700


@pytest.mark.parametrize("failing", [1, 2], ids=["gate-lock", "owner-lock"])
def test_creating_a_temp_dir_names_the_flock_problem_and_what_to_do(monkeypatch, failing):
    """#116 review B/r3: no flock means no owner can be proved dead. The failure
    must say that, and where to move `AGORA_STATE_DIR`, instead of `[Errno 77]`
    from deep inside a round; and it must not leave half a directory behind.

    Both wrapper points are pinned: `failing=1` is the shared gate lock,
    `failing=2` the owner lock (the sweep is switched off so the count is just
    the constructor's two calls). A fake that fails on every call would only
    ever reach the first one.
    """
    monkeypatch.setattr(private_tmp, "_sweep_once", lambda: None)
    real = private_tmp.fcntl.flock
    calls = []

    def no_locks_at_the_nth(handle, operation):
        calls.append(operation)
        if len(calls) == failing:
            raise OSError(errno.ENOLCK, "No locks available")
        return real(handle, operation)

    monkeypatch.setattr(private_tmp.fcntl, "flock", no_locks_at_the_nth)
    with pytest.raises(AgentError) as caught:
        PrivateTmpDir("agora-raw")
    assert len(calls) == failing, calls
    assert "AGORA_STATE_DIR" in str(caught.value)
    assert "flock" in str(caught.value)
    base = Path(os.environ["AGORA_STATE_DIR"]) / private_tmp.DIRNAME
    assert [entry.name for entry in base.iterdir()] == [private_tmp.GATE_LOCK]


def test_the_round_child_sweeps_before_it_reads_its_state(monkeypatch):
    order = []
    monkeypatch.setattr(private_tmp, "sweep", lambda state=None: order.append("sweep"))
    monkeypatch.setattr(round_child, "_exit_on_sigterm", lambda: None)
    real_loads = json.loads

    def loads(text):
        order.append("loads")
        return real_loads(text)

    monkeypatch.setattr(round_child.json, "loads", loads)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(KeyError):
        round_child.main()
    assert order == ["sweep", "loads"], order


def test_continue_sweeps_before_it_starts_a_run(monkeypatch):
    calls = []
    monkeypatch.setattr(private_tmp, "sweep", lambda state=None: calls.append(state))
    paths = store.Paths.from_env()
    with pytest.raises(cli.InputError):
        cli.cmd_continue(SimpleNamespace(agent=None), paths)
    assert calls == [paths.state]
