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
from agora.agents import claude
from agora.agents.base import Launch
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
    assert killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)], killed  # TERM, then always KILL (#74)


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

    assert killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)], killed  # TERM, then always KILL (#74)
    assert len(calls) == 2 and calls[1] == 5, "the drain after the kill is bounded"


def test_start_child_cancel_catches_base_exception_during_drain_and_kills_proc(monkeypatch):
    """When a second KeyboardInterrupt arrives during post-cancel drain,
    _start_child catches BaseException, finishes reap with proc.kill(),
    and re-raises the original exception."""
    calls = []
    kills = []
    waits = []

    class _Stub:
        pid = 4242

        def communicate(self, input=None, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise KeyboardInterrupt("first cancel")
            raise KeyboardInterrupt("second cancel")

        def kill(self):
            kills.append(True)

        def wait(self, timeout=None):
            waits.append(timeout)
            return -9

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: _Stub())
    monkeypatch.setattr(cli, "_kill_round_group", lambda p: True)

    with pytest.raises(KeyboardInterrupt) as exc:
        cli._start_child(paths(), _state("agora:01TEST00000000000000000000", {"id": "x"}, "/tmp"))

    assert str(exc.value) == "first cancel"
    assert len(kills) == 1, "proc.kill() must be called when drain raises BaseException"
    assert 5 in waits, "wait after kill must be bounded"


# --- a REAL child: success and a named failure -----------------------------------

def test_the_real_child_does_the_load_and_answers_small(lab, capsys):
    """One real `python -m agora.start_child`: it reads the mirror raw, calls
    `start_native` (the new jsonl is on disk) and answers with ids and md5s only -
    the `Launch` shell needs, no bytes."""
    a = _seed_import(capsys)
    ulid = a.split(":")[1]
    parent = store.Index(paths()).header(ulid)
    raw = (paths().mirror / ulid / store.raw_name(parent["agora"]["raw"])).read_bytes()
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


# --- --agent-arg / the `--` tail (#204 PR1) ---------------------------------------
#
# The two forms are one list handed to claude unchanged. What this file owns is the
# shell side: the parser and the split, the blacklist (exit 1 before a pending
# record or any agent half), the refusal for the agents that take no arguments, and
# the regression that a continue without arguments passes no new kwarg at all.

#: Every denied flag in every spelling that applies (`-p`/`-r`/`-c`/`-w` have no `=` form;
#: a flag with no value has no value to put in one). One case each, per the brief.
DENIED_ARG_CASES = [
    "--resume", "-r", "--resume=11111111-2222-4333-8444-555555555555",
    "--session-id", "--session-id=11111111-2222-4333-8444-555555555555",
    "--fork-session",
    "-w", "--worktree", "--worktree=/tmp/elsewhere",
    "--no-session-persistence",
    "--from-pr", "--from-pr=1234",
    "--continue", "-c",
    "-p", "--print",
    "--input-format", "--input-format=stream-json",
    "--output-format", "--output-format=json",
    "--settings", "--settings={}",
    "--append-system-prompt-file", "--append-system-prompt-file=/tmp/harness.md",
    "--system-prompt-snapshot", "--system-prompt-snapshot=off",
]

#: Single-dash tokens longer than one character: merged short flags (`-pc`, `-cp`) and
#: a value joined to a short flag (`-r<id>`). claude may split these its own way (推論)
#: and agora cannot prove which, so the whole shape is refused (review W1).
MERGED_SHORT_CASES = [
    "-pc", "-cp", "-r11111111-2222-4333-8444-555555555555", "-pstream-json", "-cv",
]


@pytest.fixture
def arg_lab(lab, monkeypatch):
    """The fake-agent lab with the fake's own records inside this test's tmp dir.

    (The shared fixture leaves FAKE_HOME unset; these tests only ever assert that
    the fake made NO record, but it must not be the machine-wide default either.)"""
    fake_home = lab["tmp"] / "fake-home"
    monkeypatch.setenv("FAKE_HOME", str(fake_home))
    return {**lab, "fake_home": fake_home}


def _pending(a):
    return paths().pending / f"{a.split(':', 1)[1]}.json"


def _claude_was_not_started(arg_lab, a):
    """Nothing of a refused continue is left behind: no pending, no agent, no new
    session file in the project directory claude would have written to."""
    assert not _pending(a).exists(), "a refused continue wrote a pending record"
    assert not (arg_lab["fake_home"] / "claude-args.log").exists(), "claude was started"
    assert [p.name for p in arg_lab["session"].parent.glob("*.jsonl")] == [arg_lab["session"].name]


def test_the_two_forms_become_one_agent_arg_list_in_the_order_written(monkeypatch, capsys):
    """`--agent-arg=<arg>` (the `=` form only) and the `--` tail are one list, in the
    order written, and argparse really accepts the documented command line."""
    seen = {}

    def spy(args, paths_):
        seen["agent_arg"] = list(args.agent_arg)
        seen["tail"] = list(args.agent_arg_dash)
        return 0

    monkeypatch.setitem(cli.ACTIONS, "continue", spy)
    code = cli.main(["continue", "session", "agora:01TEST00000000000000000000",
                     "--agent", "claude", "--agent-arg=--effort", "--agent-arg=medium",
                     "--", "--permission-mode", "bypassPermissions"])
    assert code == 0, capsys.readouterr().err
    assert seen["agent_arg"] == ["--effort", "medium",
                                 "--permission-mode", "bypassPermissions"]
    assert seen["tail"] == ["--permission-mode", "bypassPermissions"]


@pytest.mark.parametrize("spelling", ["--agent-arg", "--agent-ar", "--agent-a"])
def test_the_agent_arg_flag_requires_the_full_equals_form(capsys, spelling):
    """A bare `--agent-arg` would let argparse eat the next flag (or fail on it with
    its own exit 2); the same goes for argparse's abbreviation of it (`--agent-ar`,
    `--agent-a`). agora says why instead, and nothing was opened (review N1)."""
    code, out, err = run(capsys, "continue", "session", "agora:01TEST00000000000000000000",
                         "--agent", "claude", spelling, "medium")
    assert code == 1, err
    assert "--agent-arg" in err and "=" in err
    assert "agent-ar" in err, "the abbreviation is named, not just refused as a flag"


def test_the_abbreviated_equals_form_still_lands_as_an_agent_arg(monkeypatch, capsys):
    """The other half of N1's现状: `--agent-a=--effort` carries its own value, so it is
    unambiguous and argparse accepts it as `--agent-arg`. Only the bare form is
    refused."""
    seen = {}

    def spy(args, paths_):
        seen["agent_arg"] = list(args.agent_arg)
        return 0

    monkeypatch.setitem(cli.ACTIONS, "continue", spy)
    code = cli.main(["continue", "session", "agora:01TEST00000000000000000000",
                     "--agent", "claude", "--agent-a=--effort"])
    assert code == 0, capsys.readouterr().err
    assert seen["agent_arg"] == ["--effort"]


def test_another_action_refuses_the_pass_through_flag(capsys):
    """Only continue has anything to hand an agent; show must not look like it took
    the flag (#153's rule, the same shape `--add-tag` uses)."""
    code, out, err = run(capsys, "show", "session", "agora:01TEST00000000000000000000",
                         "--agent-arg=--raw")
    assert code == 1, err
    assert "continue" in err


def test_continue_hands_the_arguments_to_claude_after_its_own_flags(arg_lab, capsys):
    """The whole way through: parser → state → the load child → claude's argv, with
    both forms in one run. The fake agent logs the argv it was really run with."""
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--sync-period", "0",
                         "--agent-arg=--effort", "--agent-arg=medium",
                         "--", "--permission-mode", "bypassPermissions")

    assert code == 0, err and out
    argv = json.loads((arg_lab["fake_home"] / "claude-args.log").read_text().splitlines()[-1])
    assert argv[:2] == ["--resume", argv[1]]
    assert argv[2:] == ["--effort", "medium", "--permission-mode", "bypassPermissions"]


@pytest.mark.parametrize("arg", DENIED_ARG_CASES)
def test_a_denied_flag_is_refused_before_anything_starts(arg_lab, capsys, arg):
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), f"--agent-arg={arg}")

    assert code == 1, err
    assert arg.split("=", 1)[0] in err
    assert "不能轉給 claude" in err
    _claude_was_not_started(arg_lab, a)


@pytest.mark.parametrize("arg", DENIED_ARG_CASES)
def test_the_dash_tail_goes_through_the_same_blacklist(arg_lab, capsys, arg):
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--", arg)

    assert code == 1, err
    assert arg.split("=", 1)[0] in err
    _claude_was_not_started(arg_lab, a)


def test_a_bare_value_after_the_double_dash_is_refused_for_now(arg_lab, capsys):
    """claude's positional argument is a prompt it speaks at startup (推論, unverified);
    PR1 blocks it rather than changing what a continue says by itself."""
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--", "先讀這個專案的說明")

    assert code == 1, err
    assert "位置參數" in err and "先讀這個專案的說明" in err
    _claude_was_not_started(arg_lab, a)


# --- W1: merged / value-joined short flags ----------------------------------------

@pytest.mark.parametrize("form", ["agent-arg", "dash"])
@pytest.mark.parametrize("arg", MERGED_SHORT_CASES)
def test_a_merged_or_value_joined_short_flag_is_refused(arg_lab, capsys, arg, form):
    """Review W1: an exact-name blacklist never sees `-pc`, `-cp` or `-r<id>`, so the
    blacklist's protections (session id, headless stream) walked straight past it.
    claude would split these its own way (推論) and agora cannot prove which, so the
    whole shape is refused - in both forms, before anything starts."""
    a = _seed_import(capsys)
    argv = ["continue", "session", a, "--agent", "claude", "--dir", str(arg_lab["work"])]
    argv += [f"--agent-arg={arg}"] if form == "agent-arg" else ["--", arg]

    code, out, err = run(capsys, *argv)

    assert code == 1, err
    assert "不支援合併的短旗標" in err and "請分開寫或用長旗標" in err
    assert arg in err, "the refusal names the token it did not understand"
    _claude_was_not_started(arg_lab, a)


def test_a_one_character_short_flag_is_not_a_merged_one(arg_lab, capsys):
    """The other side of W1: `-x` is a single flag, so it keeps going through the
    blacklist and then to claude unchanged - the conservative refusal must not grow
    into "no short flags at all"."""
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--sync-period", "0",
                         "--agent-arg=-v")

    assert code == 0, err and out
    argv = json.loads((arg_lab["fake_home"] / "claude-args.log").read_text().splitlines()[-1])
    assert argv[2:] == ["-v"]


# --- W2: one bare-value check over the merged list --------------------------------

def test_a_bare_value_through_agent_arg_is_refused_too(arg_lab, capsys):
    """Review W2: the two forms are one list and share one check, so `--agent-arg=`
    is not the way around the positional-argument refusal the `--` tail already had."""
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--agent-arg=先讀這個專案的說明")

    assert code == 1, err
    assert "位置參數" in err and "先讀這個專案的說明" in err
    _claude_was_not_started(arg_lab, a)


def test_the_second_value_of_a_multi_value_flag_says_what_to_do(arg_lab, capsys):
    """W2's over-broad side: `--add-dir a b` is still refused (agora cannot know how
    many values the flag takes), but the message now points at the spelling that does
    work instead of calling the user's own second value a positional argument."""
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--", "--add-dir", "/tmp/a", "/tmp/b")

    assert code == 1, err
    assert "多值旗標" in err and "--flag=值" in err
    assert "/tmp/b" in err
    _claude_was_not_started(arg_lab, a)


@pytest.mark.parametrize("agent_args", [
    ["--effort", "medium"],
    ["--effort", "medium", "--permission-mode", "bypassPermissions"],
    ["--add-dir=/tmp/a", "--allowedTools=Read,Edit"],
])
def test_a_flags_value_in_the_merged_list_is_not_a_bare_value(agent_args):
    """W2's required case: the value right after a flag (`--agent-arg=--effort` then
    `--agent-arg=medium`) is that flag's value, not claude's positional argument -
    whether the flag came from `--agent-arg=` or from the `--` tail."""
    cli._refuse_agent_args("claude", agent_args)   # must not raise


# --- ③b: the refusal really is before the sync and before the lock ----------------

def test_a_refused_continue_never_reaches_the_drive_sync(arg_lab, capsys, monkeypatch):
    """Review ③b: the refusal is before `_sync_for` as well as before the pending
    record - a refused continue does not pull Drive first. (`_sync_for` is made to
    raise, so reaching it would show as the generic error, exit 2.)"""
    a = _seed_import(capsys)
    calls = []

    def boom(paths_, ids):
        calls.append(ids)
        raise AssertionError("a refused continue must not sync")

    monkeypatch.setattr(cli, "_sync_for", boom)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--agent-arg=-p")

    assert code == 1, err
    assert calls == [], "the refusal came after the sync"
    assert "非預期的錯誤" not in err
    _claude_was_not_started(arg_lab, a)


def test_a_refused_continue_is_exit_1_even_on_a_locked_session(arg_lab, capsys, monkeypatch):
    """Review ③b: on a Session this machine is already continuing, the pass-through
    refusal is still agora's own input error (exit 1, the flag's reason), not exit 4
    with `agora-locked` - the lock is a later check. The first run proves the lock
    really would have fired for this session."""
    a = _seed_import(capsys)
    monkeypatch.setattr(store, "continuing", lambda paths_, ulid: True)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]))
    assert code == 4, err
    assert "agora-locked" in err

    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(arg_lab["work"]), "--agent-arg=--resume=x")
    assert code == 1, err
    assert "agora-locked" not in err
    assert "不能轉給 claude" in err
    _claude_was_not_started(arg_lab, a)


@pytest.mark.parametrize("agent_name", ["opencode", "agy"])
def test_another_agent_refuses_the_arguments_instead_of_a_type_error(
        arg_lab, capsys, agent_name):
    """opencode's headless mode is `serve` and agy takes no flags: the refusal is
    agora's own exit 1, not the TypeError an `extra_args` kwarg would be."""
    a = _seed_import(capsys)

    code, out, err = run(capsys, "continue", "session", a, "--agent", agent_name,
                         "--dir", str(arg_lab["work"]), "--agent-arg=--effort")

    assert code == 1, err
    assert "不支援轉傳參數" in err and agent_name in err
    assert "TypeError" not in err and "非預期的錯誤" not in err
    _claude_was_not_started(arg_lab, a)


@pytest.mark.parametrize("agent_name", ["claude", "opencode", "agy"])
def test_a_continue_without_arguments_passes_no_extra_args_kwarg(
        arg_lab, capsys, monkeypatch, agent_name):
    """#204 PR1 regression: `extra_args` is only handed on when there is something to
    hand. The proof is a stub with the OLD, strict signature - an unconditional kwarg
    is a TypeError for opencode and agy (`_load_for_launch`'s docstring, review W2)."""
    a = _seed_import(capsys)
    parent = json.loads(json.dumps(store.Index(paths()).header(a.split(":", 1)[1])))
    parent["agora"]["source"]["agent"] = agent_name
    seen = {}

    class Strict:
        name = agent_name

        def start_native(self, raw, workdir, *, headless=False, headless_port=None,
                         harness_file=None, keep_id=None):
            seen["kwargs"] = {"headless": headless, "headless_port": headless_port,
                              "harness_file": harness_file, "keep_id": keep_id}
            return Launch(argv=["true"], cwd=str(workdir), agent_session_id="ses-strict",
                          before_count=0)

    monkeypatch.setattr(cli, "load_agent", lambda name: Strict())
    cli._load_for_launch(paths(), _state(a, parent, arg_lab["work"]))

    assert seen, "the adapter was never called"
    assert "extra_args" not in seen["kwargs"]
