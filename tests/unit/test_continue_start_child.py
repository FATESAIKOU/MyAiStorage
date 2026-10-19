"""The startup load child: `cli._start_child` and `agora.start_child` (impl-mem-start).

The load-and-launch half of `continue` runs in a fresh interpreter that exits
when it is done, so the session-sized bytes never live in the shell that then
waits for the agent all day. These tests are the boundary: the launch contract
(a new interpreter, its own session, the state on stdin, the answer on fd 1),
the death classes (killed, unparseable, timed out - named errors keep their
class so the CLI's exit code and words do not move), and one REAL child doing
a real load and a real failed load. The fake-agent world is served by the
in-process seam in `test_cli_sync.py`/`test_cli_fork.py`; the RSS acceptance
numbers are in tests/perf/measure_continue_start_rss.py.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora.agents.claude import encode_project_dir
from test_continue_round_child import (  # noqa: F401  (lab and helpers are shared harness)
    _StubProc, _clean_stub, _seed_import, lab, paths, run)


def _state(a, parent, workdir, **over):
    state = {"agent": "claude", "source_id": a, "parent": parent, "fork": False,
             "updates": {}, "add_tags": [], "workdir": str(workdir),
             "headless": False, "headless_port": None, "harness_file": None}
    state.update(over)
    return state


def _payload(**over):
    payload = {"launch": {"argv": ["claude", "--resume", "x"], "cwd": "/tmp",
                          "agent_session_id": "x", "before_count": 1, "env": {}},
               "parent_md5": "0" * 32, "saved_md5": "1" * 32,
               "fork_id": None, "fork_title": None, "fork_md5": None, "saved_fork": None}
    payload.update(over)
    return payload


# --- the launch contract ---------------------------------------------------------

def test_start_child_launches_a_fresh_detached_child(monkeypatch):
    """A NEW interpreter running the module - not `-c`, no PYTHONPATH injection -
    its own session, the state on stdin, the answer from fd 1's pipe. The payload
    is handed back as it came."""
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    _StubProc.returncode, _StubProc.stdout = 0, json.dumps(_payload()).encode()
    state = _state("agora:01TEST00000000000000000000", {"id": "x"}, "/tmp")

    got = cli._start_child(paths(), state)

    assert got == _payload()
    assert _StubProc.args == (sys.executable, "-m", "agora.start_child")
    kw = _StubProc.kwargs
    assert kw.get("start_new_session") is True
    assert kw.get("stdin") == subprocess.PIPE and kw.get("stdout") == subprocess.PIPE
    assert "env" not in kw, "no PYTHONPATH or other env injection (round_child's ruling)"
    assert json.loads(_StubProc.stdin_written) == state


def test_start_child_death_classes_raise_and_do_not_hang(monkeypatch):
    """rc != 0, an unparseable answer and a named load failure. The first two are
    `StartChildDead` (an AgentError: exit 2, the message says the load child); a
    named one is raised as its own class again - that is what keeps InputError at
    exit 1 and the agent's own messages word for word."""
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    state = _state("agora:01TEST00000000000000000000", {"id": "x"}, "/tmp")

    _StubProc.returncode, _StubProc.stdout = 3, b""
    with pytest.raises(cli.StartChildDead) as e:
        cli._start_child(paths(), state)
    assert "rc=3" in str(e.value) and isinstance(e.value, cli.AgentError)

    _StubProc.returncode, _StubProc.stdout = 0, b"not json at all"
    with pytest.raises(cli.StartChildDead) as e:
        cli._start_child(paths(), state)
    assert "狀態壞掉" in str(e.value)

    _StubProc.returncode = 0
    _StubProc.stdout = json.dumps({"err": {"kind": "InputError", "text": "不行"}}).encode()
    with pytest.raises(cli.InputError) as e:
        cli._start_child(paths(), state)
    assert str(e.value) == "不行"

    _StubProc.stdout = json.dumps({"err": {"kind": "UnicodeDecodeError", "text": "壞"}}).encode()
    with pytest.raises(Exception) as e:
        cli._start_child(paths(), state)
    assert type(e.value).__name__ == "UnicodeDecodeError", "the generic handler prints this name"
    assert str(e.value) == "壞"


def test_start_child_timeout_kills_the_group(monkeypatch):
    """A wedged load is killed as a GROUP (the child leads its own session, so the
    rclone or `opencode import` grandchildren die with it) and answered as a timeout
    - the shell never waits the whole deadline out on a corpse."""
    killed: list = []
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    calls = {"n": 0}

    def timeout_once(self, input=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="agora.start_child", timeout=timeout)
        return b"", b""
    monkeypatch.setattr(_StubProc, "communicate", timeout_once)

    with pytest.raises(cli.StartChildDead) as e:
        cli._start_child(paths(), _state("agora:01TEST00000000000000000000", {"id": "x"}, "/tmp"))

    assert "逾時" in str(e.value)
    assert killed == [(4242, signal.SIGTERM)], killed


def test_a_cancelled_shell_kills_the_load_child_and_reraises(monkeypatch):
    """Ctrl-C while the shell waits for the load: the child leads its own session,
    so the terminal's SIGINT never reaches it. The shell must kill that group
    itself (the same path a timeout takes) and then let the original exception
    go - the user cancelled, the load stops too, no orphan keeps fetching."""
    killed: list = []
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    calls: list = []

    def cancelled_once(self, input=None, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return b"", b""
    monkeypatch.setattr(_StubProc, "communicate", cancelled_once)

    with pytest.raises(KeyboardInterrupt):
        cli._start_child(paths(), _state("agora:01TEST00000000000000000000", {"id": "x"}, "/tmp"))

    assert killed == [(4242, signal.SIGTERM)], killed
    assert len(calls) == 2 and calls[1] == 5, "the drain after the kill is bounded"


# --- a REAL child: success and a named failure -----------------------------------

def test_the_real_child_does_the_load_and_answers_small(lab, capsys):
    """One real `python -m agora.start_child`: it reads the mirror raw, calls
    `start_native` (the new jsonl is on disk) and answers with ids and md5s only -
    the `Launch` shell needs, no bytes."""
    a = _seed_import(capsys)
    ulid = a.split(":")[1]
    parent = store.Index(paths()).header(ulid)
    raw = (paths().mirror / ulid / parent["agora"]["raw"]["file"]).read_bytes()
    expected = store.hashlib.md5(raw).hexdigest()

    payload = cli._start_child(paths(), _state(a, parent, lab["work"]))

    launch = payload["launch"]
    assert launch["agent_session_id"] and launch["before_count"] > 0
    assert payload["parent_md5"] == expected and payload["saved_md5"] == expected
    assert payload["saved_fork"] is None and payload["fork_id"] is None
    written = (Path(os.environ["AGORA_CLAUDE_HOME"]) / ".claude" / "projects"
               / encode_project_dir(lab["work"]) / f"{launch['agent_session_id']}.jsonl")
    assert written.is_file() and written.stat().st_size > 0, "the child really loaded the session"


def test_the_real_child_answers_a_load_error_in_kind(lab):
    """A load that raises inside the child comes back as its own class and words -
    here a parent with no raw, which is the StoreError `fetch_raw` always gave."""
    with pytest.raises(store.StoreError) as e:
        cli._start_child(paths(), _state("agora:01TEST00000000000000000000", {}, lab["work"]))
    assert "沒有 raw" in str(e.value)


def test_continue_leaves_the_load_to_the_child(lab, capsys, monkeypatch):
    """The shell must never run the load itself: if it did, the whole session
    would be allocated in the process that stays alive (the bug impl-mem-start
    fixes). The real child has its own copy of the module, so poisoning the
    shell's function proves nothing ran in-process."""
    a = _seed_import(capsys)

    def poison(*args, **kwargs):
        raise AssertionError("the shell ran the load itself")
    monkeypatch.setattr(cli, "_load_for_launch", poison)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(lab["work"]), "--sync-period", "0")

    assert code == 0, err and out
