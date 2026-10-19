"""The periodic round in a fresh interpreter (ticket impl-mem-subproc, phase 2).

Boundary tests for `cli._round_child`: `collect` + `_write_one` run per round in
`python -m agora.round_child` - a new process whose memory the OS reclaims when it
exits - while `_periodic_write` keeps the outer shell's failure counting, the six
outcome texts, and the exactly-one-line-per-round invariant.

These tests do NOT use the `env` in-process fake-agent fixture: the fake-agent world
is served by the inline seam (test-side DI on the same boundary) in `test_cli_sync.py`.
What lives here is the boundary itself: the wire, the death classes, the timeout and
the group kill, the lock hand-down, the pending repair. Where a stubbed `Popen` drives
a deterministic death it is marked; the success, skip, inner-error, timeout and
SIGTERM paths run the REAL child.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agora import cli, store, round_child
from agora.agents.claude import encode_project_dir

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fakes" / "fake_claude.py"
FAKE_OPENCODE = Path(__file__).resolve().parent.parent / "fakes" / "fake_opencode.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
CL_SID = "11111111-2222-4333-8444-555555555555"
NO_SUCH_SID = "00000000-dead-beef-0000-000000000000"
SECONDS = "0.005"          # 0.3 s, the same floor the periodic-sync suite drives


def paths():
    return store.Paths.from_env()


def log_text():
    try:
        return (paths().state / cli.CONTINUE_LOG).read_text(encoding="utf-8")
    except OSError:
        return ""


def outcome_lines():
    return [line for line in log_text().splitlines() if "定期寫回" in line]


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def _wrapper(tmp_path, name, target):
    w = tmp_path / name
    w.write_text(f"#!/bin/sh\nexec {sys.executable} {target} \"$@\"\n")
    w.chmod(0o755)
    return w


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Fake Drive, the real claude adapter over the fake CLI, and the fixture
    session laid out where `find_jsonl` looks for it. No seam patching: the rounds
    these tests run are real subprocesses."""
    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(_wrapper(tmp_path, "rclone", FAKE_RCLONE)))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    work = tmp_path / "proj"
    work.mkdir()
    proj = home / ".claude" / "projects" / encode_project_dir(work)
    proj.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(_wrapper(tmp_path, "claude", FAKE_CLAUDE)))
    return {"home": home, "work": work, "remote": remote,
            "session": proj / f"{CL_SID}.jsonl", "tmp": tmp_path}


def _seed_import(capsys):
    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    return a


def _grow(session: Path, workdir: str, text: str) -> None:
    """One user+assistant pair in the fake agent's own shape, so the next round
    sees both a fingerprint change and new messages."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    user = {"type": "user", "sessionId": CL_SID, "uuid": str(uuid_mod.uuid4()),
            "parentUuid": None, "timestamp": now, "cwd": workdir,
            "message": {"role": "user", "content": text}}
    assistant = {"type": "assistant", "sessionId": CL_SID, "uuid": str(uuid_mod.uuid4()),
                 "parentUuid": user["uuid"], "timestamp": now, "cwd": workdir,
                 "message": {"role": "assistant", "model": "zz-fake-model",
                             "content": [{"type": "text", "text": "ZZRC 好的"}]}}
    with open(session, "a", encoding="utf-8") as f:
        f.write(json.dumps(user, ensure_ascii=False) + "\n")
        f.write(json.dumps(assistant, ensure_ascii=False) + "\n")


def _record(agora_id, sid, workdir, agent="claude"):
    return {"agora_id": agora_id, "agent": agent, "agent_session_id": sid,
            "dir": str(workdir), "parent": {"id": agora_id, "raw_md5": "0" * 32},
            "parent_headers": [], "title": "輪中程測試", "before_count": 1,
            "started_at": "2026-10-19T00:00:00Z", "header_updates": {},
            "add_tags": [], "saved_md5": "0" * 32}


def _absent(*needles):
    """The pids of anything still alive that matches a command line (a dead
    round child, its rclone/opencode children included). `-ax` matters: an
    orphaned grandchild has no controlling terminal and plain `ps` would not
    list it - the blind scan would make every absence-check trivially true.
    `serve` lines are skipped: old detached `fake_opencode serve` daemons from
    earlier runs of other tickets linger for hours on end; this test's fake
    never serves - a surviving round's fake is always `session export`."""
    out = subprocess.run(["ps", "-ax", "-o", "pid=,command="],
                         capture_output=True, text=True).stdout
    return [ln.strip() for ln in out.splitlines()
            if "serve --hostname" not in ln
            for n in needles if n in ln and "ps -ax" not in ln]


# --- the wire: the trap that JSON sets ---------------------------------------

def test_fingerprint_survives_the_wire():
    """`_collect_new` skips by comparing its stat tuple against `run.fingerprint`,
    and JSON turns every tuple into a list. state_of -> json -> decode_run must give
    an EQUAL tuple back or every round silently re-reads the whole session."""
    real = (12345, 678901234, ((11, 22), (33, 44)))
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    run_obj.fingerprint = real
    wire = json.loads(json.dumps(round_child.state_of(run_obj)))
    assert isinstance(wire["fingerprint"], list)                 # the trap is real
    mini = round_child.decode_run(wire, paths())
    assert mini.fingerprint == real and isinstance(mini.fingerprint[2][0], tuple)


def test_apply_diff_moves_the_outer_run():
    """A clean payload updates exactly the fields the outer shell owns; a payload
    missing one is a broken return, not a silently skipped field."""
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    payload = {"err": None, "saved": ["agora:01TEST00000000000000000000", True],
               "target": "agora:01TEST00000000000000000001",
               "last_md5": "b" * 32, "fingerprint": [1, 2, []],
               "collect_failed": False, "wrote": True,
               "pending": None, "first_note": "[agora] x"}
    cli._apply_diff(run_obj, payload)
    assert run_obj.last_md5 == "b" * 32 and run_obj.wrote
    assert run_obj.first_note == "[agora] x"
    assert run_obj.target == "agora:01TEST00000000000000000001"
    assert run_obj.fingerprint == (1, 2, ()), run_obj.fingerprint
    with pytest.raises(KeyError):
        cli._apply_diff(run_obj, {"saved": None})                # missing fields = broken


# --- the real child: success, skip, inner error -------------------------------

def test_round_child_writes_through_a_fresh_interpreter(capsys, lab):
    """One real round, in a real subprocess: the session grew, the child collected,
    staged and inline-uploaded it, and the outer run carries the new state."""
    a = _seed_import(capsys)
    ulid = a.split(":")[1]
    _grow(lab["session"], str(lab["work"]), "ZZRC 子行程寫的一句")
    run_obj = cli._Run(paths(), _record(a, CL_SID, lab["work"]))

    cli._periodic_write(run_obj, cli.load_agent("claude"))

    assert run_obj.failures == 0, log_text()
    lines = outcome_lines()
    assert len(lines) == 1 and "已寫回" in lines[0], lines
    assert run_obj.last_md5 and len(run_obj.last_md5) == 32 and run_obj.wrote
    assert isinstance(run_obj.fingerprint, tuple) and run_obj.fingerprint[0] > 0
    doc = (paths().mirror / ulid / "session.md").read_text(encoding="utf-8")
    assert "ZZRC 子行程寫的一句" in doc
    assert not store.waiting_ulids(paths()), "the child's inline upload drained the outbox"
    on_drive = lab["remote"] / "agora" / "sessions" / ulid / "session.md"
    assert "ZZRC 子行程寫的一句" in on_drive.read_text(encoding="utf-8")


def test_round_child_second_round_skips_by_fingerprint(capsys, lab):
    """Nothing moved between rounds: the CHILD says so (its fingerprint came back a
    tuple and matches again), and every round still leaves exactly one line."""
    a = _seed_import(capsys)
    _grow(lab["session"], str(lab["work"]), "ZZRC 第一句")
    run_obj = cli._Run(paths(), _record(a, CL_SID, lab["work"]))
    agent = cli.load_agent("claude")

    cli._periodic_write(run_obj, agent)
    cli._periodic_write(run_obj, agent)
    cli._periodic_write(run_obj, agent)

    assert run_obj.failures == 0, log_text()
    lines = outcome_lines()
    assert len(lines) == 3 and "已寫回" in lines[0]
    assert all("沒有新內容" in line for line in lines[1:]), lines


def test_round_child_reports_inner_errors_like_todays_failure(capsys, lab):
    """Class 1: the round body raised INSIDE the child (missing session). The
    payload carries the message; the outer shell counts the failure and keeps the
    collect-failure bookkeeping (fingerprint cleared), so the next round looks
    again - and lands the new words when the file exists."""
    a = _seed_import(capsys)
    run_obj = cli._Run(paths(), _record(a, NO_SUCH_SID, lab["work"]))
    agent = cli.load_agent("claude")

    cli._periodic_write(run_obj, agent)

    assert run_obj.failures == 1
    assert run_obj.collect_failed is True and run_obj.fingerprint is None
    lines = outcome_lines()
    assert len(lines) == 1 and "失敗（第 1 次）：" in lines[0], lines

    _grow(lab["session"], str(lab["work"]), "ZZRC 下一輪救回的")
    ok_run = cli._Run(paths(), _record(a, CL_SID, lab["work"]))
    ok_run.collect_failed = True                       # what the failed round left
    ok_run.fingerprint = None                          # (fresh run, same state)
    cli._periodic_write(ok_run, agent)
    assert "已寫回" in outcome_lines()[-1], log_text()
    assert ok_run.failures == 0, "a forced re-look is not a failure"


# --- death classes (stubbed Popen drives them deterministically) ---------------

class _StubProc:
    """Stands in for the child: one canned exit, no interpreter. Records the
    launch contract; tests set `returncode`/`stdout` (and sometimes a wait that
    ignores TERM) before triggering `cli._round_child`."""
    returncode = 0
    stdout = b""
    args = None
    kwargs: dict = {}
    stdin_written: bytes = b""
    wait_ignores = 0          # first N wait() calls raise TimeoutExpired
    waits = 0

    def __init__(self, argv, **kw):
        type(self).args = tuple(argv)
        type(self).kwargs = dict(kw)
        self.returncode = type(self).returncode
        self._out = type(self).stdout
        self.pid = 4242

    def communicate(self, input=None, timeout=None):
        type(self).stdin_written = input or b""
        return self._out, b""

    def wait(self, timeout=None):
        type(self).waits += 1
        if type(self).waits <= type(self).wait_ignores:
            raise subprocess.TimeoutExpired(cmd="agora.round_child", timeout=timeout)
        return self.returncode


def _clean_stub():
    _StubProc.args = None
    _StubProc.kwargs = {}
    _StubProc.stdin_written = b""
    _StubProc.wait_ignores = 0
    _StubProc.waits = 0
    return _StubProc


def test_round_child_death_classes_apply_nothing_and_count(monkeypatch):
    """rc != 0 and an unparseable payload mean: one failure counted, NO state
    applied, one outcome line, shell alive. (The pending repair after a death is
    proven in its own test below.)"""
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    run_obj.last_md5 = "keep"

    _StubProc.returncode, _StubProc.stdout = 3, b""
    cli._periodic_write(run_obj, None)
    assert run_obj.failures == 1 and run_obj.last_md5 == "keep"
    assert run_obj.collect_failed is True and run_obj.fingerprint is None
    assert "失敗（第 1 次）：子行程異常結束（rc=3）" in outcome_lines()[-1], log_text()

    _StubProc.returncode, _StubProc.stdout = 0, b"not json at all"
    cli._periodic_write(run_obj, None)
    assert run_obj.failures == 2 and "壞" in outcome_lines()[-1], outcome_lines()[-1]

    _StubProc.returncode, _StubProc.stdout = -9, b""
    cli._periodic_write(run_obj, None)
    assert run_obj.failures == 3 and "rc=-9" in outcome_lines()[-1]
    assert run_obj.last_md5 == "keep", "a dead round applies nothing"
    assert len([ln for ln in outcome_lines() if "子行程異常結束" in ln]) == 3


def test_round_child_timeout_escalates_on_the_group(monkeypatch):
    """Timeout is class 3: the text says 逾時, and the kill targets the GROUP
    (start_new_session makes pgid == pid) because the child's own children
    (rclone, `opencode export`) live there - and a child that ignores TERM is
    escalated to SIGKILL. Stubbed: waits ignore the first TERM."""
    killed = []
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    monkeypatch.setattr(cli.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    _StubProc.wait_ignores = 1        # the TERM-wait fails once: escalate
    calls = {"n": 0}

    def timeout_once(self, input=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="agora.round_child", timeout=timeout)
        return self._out, b""
    monkeypatch.setattr(_StubProc, "communicate", timeout_once)

    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    with pytest.raises(Exception) as exc:
        cli._round_child(paths(), run_obj, None)

    assert "逾時" in str(exc.value)
    assert killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)], killed


def test_round_child_timeout_kills_a_real_wedged_group(lab, monkeypatch, tmp_path):
    """The same through a REAL child: the round hangs inside the fake opencode's
    sleep (its own grandchild process); AGORA_ROUND_TIMEOUT=2 fires, and NEITHER
    the round child nor the sleeping fake survives."""
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(_wrapper(tmp_path, "opencode", FAKE_OPENCODE)))
    monkeypatch.setenv("FAKE_OPENCODE_SLEEP", "30")
    monkeypatch.setenv("AGORA_ROUND_TIMEOUT", "2")
    rec = _record("agora:01TEST00000000000000000000", "ses_slow", str(lab["work"]),
                  agent="opencode")
    run_obj = cli._Run(paths(), rec)

    t0 = time.monotonic()
    with pytest.raises(Exception) as exc:
        cli._round_child(paths(), run_obj, None)
    assert "逾時" in str(exc.value)
    assert time.monotonic() - t0 < 30, "the timeout is enforced, not waited out"

    time.sleep(0.5)
    left = _absent("agora.round_child", "fake_opencode")
    assert not left, f"the group kill orphaned: {left}"


# --- the launch contract --------------------------------------------------------

def test_round_child_launches_a_fresh_detached_child(monkeypatch):
    """The guard against the fork trap and the fd hygiene: a NEW interpreter
    running the module (not `-c`, and no PYTHONPATH injection), its own session,
    stderr into continue.log, and exactly the pending lock's fd handed down when
    one exists. The stdin state is the outer run's own."""
    monkeypatch.setattr(cli.subprocess, "Popen", _clean_stub())
    payload = {"err": None, "saved": None, "target": "agora:01TEST00000000000000000000",
               "last_md5": "0" * 32, "fingerprint": None, "collect_failed": False,
               "wrote": False, "pending": None, "first_note": None}
    _StubProc.returncode, _StubProc.stdout = 0, json.dumps(payload).encode()
    record = _record("agora:01TEST00000000000000000000", CL_SID, "/x")
    run_obj = cli._Run(paths(), record)

    got = cli._round_child(paths(), run_obj, None)

    assert got == payload
    assert _StubProc.args == (sys.executable, "-m", "agora.round_child")
    kw = _StubProc.kwargs
    assert kw.get("start_new_session") is True
    assert kw.get("env") is None, "no PYTHONPATH injection (ticket ruling)"
    assert kw.get("pass_fds") == ()
    assert str(kw["stderr"].name).endswith(cli.CONTINUE_LOG)
    state = json.loads(_StubProc.stdin_written)
    assert state["record"]["agora_id"] == record["agora_id"]
    assert state["target"] == record["agora_id"] and state["last_md5"] == "0" * 32

    pending, lock = cli._write_pending(paths(), record)
    run2 = cli._Run(paths(), record, pending=pending, lock=lock)
    cli._round_child(paths(), run2, None, lock_fd=lock.fileno())
    assert _StubProc.kwargs.get("pass_fds") == (lock.fileno(),)
    lock.close()
    pending.unlink()


# --- the death-path bookkeeping the shell owns --------------------------------

def test_pending_is_repaired_after_a_child_died_mid_move(monkeypatch):
    """The child renamed pending/X.json to pending/Y.json and died before returning
    the new name. The outer shell must find the file again by the lock's inode -
    or every later `_move_pending` and the final `unlink()` would blow up."""
    record = _record("agora:01TEST00000000000000000000", CL_SID, "/x")
    pending, lock = cli._write_pending(paths(), record)
    run_obj = cli._Run(paths(), record, pending=pending, lock=lock)
    moved = pending.with_name("01MOVED0000000000000000MOVED.json")
    os.rename(pending, moved)

    def died_mid_move(paths_, run_, agent_, lock_fd_=None):
        raise RuntimeError("子行程異常結束（rc=-9）")
    monkeypatch.setattr(cli, "_round_child", died_mid_move)

    cli._periodic_write(run_obj, None)

    assert run_obj.failures == 1
    assert run_obj.pending == moved, "the shell re-found the renamed record by inode"
    assert moved.exists()
    lock.close()
    moved.unlink()


# --- the outer loop with the real child, end to end ------------------------------

def test_detached_uploader_spawned_by_the_child_outlives_it(capsys, lab, monkeypatch):
    """Ticket 13: the background uploader started from INSIDE the round child
    must not die with it - the entry lands on Drive after the child is gone."""
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)    # detached, like production
    a = _seed_import(capsys)
    ulid = a.split(":")[1]
    _grow(lab["session"], str(lab["work"]), "ZZRC 背景傳的一句")
    run_obj = cli._Run(paths(), _record(a, CL_SID, lab["work"]))

    cli._periodic_write(run_obj, cli.load_agent("claude"))

    assert run_obj.failures == 0, log_text()
    doc = lab["remote"] / "agora" / "sessions" / ulid / "session.md"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if "ZZRC 背景傳的一句" in doc.read_text(encoding="utf-8"):
                return
        except OSError:
            pass
        time.sleep(0.1)
    pytest.fail(f"the uploader the child spawned never landed the entry: {log_text()}")


def test_mid_round_sigterm_finishes_the_round_then_the_final_write(lab, capsys):
    """Ticket 12: a SIGTERM that lands while a round is in flight - the fake
    rclone's delay makes the window verifiable (a marker is touched the moment an
    UPLOAD starts) - must forward to the agent, REAP the round child (never kill
    it), apply it, and only then run the outer shell's final write: exit 0, one
    outcome line per round, and no two writers in the outbox."""
    a = _seed_import(capsys)
    env = {**os.environ,
           "FAKE_AGENT_SCRIPT": f"append:ZZTERM TERM前的一句,wait-file:{lab['tmp'] / 'never'}",
           "FAKE_SYNC_TIMEOUT": "120",
           "FAKE_RCLONE_DELAY": "2",
           "FAKE_RCLONE_DELAYED": str(lab["tmp"] / "upload-in-flight")}
    out_file = lab["tmp"] / "shell.out"
    marker = Path(env["FAKE_RCLONE_DELAYED"])
    with open(out_file, "wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", a, "--agent", "claude",
             "--dir", str(lab["work"]), "--headless", "--sync-period", SECONDS],
            stdout=sink, stderr=sink, stdin=subprocess.DEVNULL, env=env, cwd=str(lab["tmp"]))
        deadline = time.monotonic() + 150
        while not marker.exists():
            if proc.poll() is not None:
                raise AssertionError(f"agora died early rc={proc.returncode}: {out_file.read_text(errors='replace')}")
            if time.monotonic() > deadline:
                proc.kill()
                raise AssertionError("never saw a round's upload in flight")
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=240)
    assert rc == 0, out_file.read_text(errors="replace")

    rounds = outcome_lines()
    assert rounds and all("失敗" not in line for line in rounds), rounds
    assert rounds[0].count("定期寫回") == 1
    text = out_file.read_text(errors="replace")
    assert "正在寫回" in text or "定期寫回" in text          # the said_writing line (P7)
    on_drive = lab["remote"] / "agora" / "sessions" / a.split(":")[1] / "session.md"
    assert "ZZTERM TERM前的一句" in on_drive.read_text(encoding="utf-8")
    assert not list(store.Paths.from_env().pending.glob("*.json")), "pending cleaned up"
    left = _absent("agora.round_child", "fake_rclone.py", "fake_claude.py")
    assert not left, f"processes outlived the gentle stop: {left}"


# --- fix round #1 (review HIGH): a timeout of 0 would kill every round ---------

@pytest.mark.parametrize("raw,want", [("0", 1800.0), ("-5", 1800.0), ("abc", 1800.0),
                                      ("0.0", 1800.0), ("7", 7.0)])
def test_round_timeout_refuses_values_that_would_kill_every_round(monkeypatch, raw, want):
    """`communicate(timeout=0)` fires at once, so `AGORA_ROUND_TIMEOUT=0` - often
    meant as "no limit" - or a negative typo would kill every round (review #1).
    A value that is not a real deadline falls back to the default HERE; the
    generic reader `env_seconds` itself stays untouched for its other callers."""
    from agora.agents import base
    monkeypatch.setenv("AGORA_ROUND_TIMEOUT", raw)
    assert cli._round_timeout() == want
    assert base.env_seconds("AGORA_ROUND_TIMEOUT", 5.0) == (5.0 if raw == "abc"
                                                            else float(raw))


# --- fix round #2 (review HIGH): a payload with a missing key lands NOTHING -----

def _full_payload(**over):
    """A clean round's answer with every field the shell reads, each one
    different from a fresh `_Run` so a half-apply is visible; `over` replaces
    fields (test #5 puts a wire-shaped fingerprint in)."""
    payload = {"err": None, "saved": None,
               "last_md5": "b" * 32, "fingerprint": [1, 2, []],
               "collect_failed": True, "wrote": True,
               "target": "agora:01TEST00000000000000000001",
               "pending": str(paths().pending / "01TEST0000000000000000000000.json"),
               "first_note": "[agora] x"}
    payload.update(over)
    return payload


#: The seven fields a payload must carry - named here as well as in `cli`, so a
#: rename on either side that breaks the agreement fails loudly.
DIFF_KEYS = ("last_md5", "fingerprint", "collect_failed", "wrote",
             "target", "pending", "first_note")


def _outer_state(run_obj):
    return tuple(getattr(run_obj, f) for f in DIFF_KEYS)


@pytest.mark.parametrize("dropped", list(DIFF_KEYS))
def test_apply_diff_refuses_a_payload_missing_any_key(dropped):
    """The old code assigned field by field, so a payload missing a later key
    had already moved the earlier ones before the KeyError (review #2) - a dead
    round landing half a round. The keys are checked up front: the whole payload
    is refused and NOTHING moves."""
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    payload = _full_payload()
    assert payload["last_md5"] != run_obj.last_md5      # an apply would be detectable
    del payload[dropped]
    before = _outer_state(run_obj)
    with pytest.raises(KeyError):
        cli._apply_diff(run_obj, payload)
    assert _outer_state(run_obj) == before, f"dropping {dropped} still moved state"


# --- fix round #5 (the mutation review could not catch): the tuple on arrival ---

def test_apply_diff_hands_the_outer_run_a_tuple_fingerprint():
    """The wire carries lists and `_collect_new` compares stat marks with `==` -
    a tuple never equals a list - so the decode must happen on ARRIVAL too, not
    only in `decode_run`: an outer shell holding a list fingerprint skips nothing
    (the deleted-decode mutation the first review round sailed through green)."""
    real = (12345, 678901234, ((11, 22), (33, 44)))
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    payload = _full_payload(fingerprint=json.loads(json.dumps(real)))
    assert isinstance(payload["fingerprint"], list)              # what the wire holds
    cli._apply_diff(run_obj, payload)
    assert run_obj.fingerprint == real                           # list would NOT equal this
    assert isinstance(run_obj.fingerprint, tuple)
    assert isinstance(run_obj.fingerprint[2], tuple)
    assert isinstance(run_obj.fingerprint[2][1], tuple)


# --- fix round #3 (review MED): the child answers for errors, never for deaths --

_RIG = """
import runpy, sys
import agora.cli
def _boom(run, agent):
    run.collect_failed = True
    run.fingerprint = None
    raise %(exc)s
agora.cli._collect_new = _boom
sys.argv = ["agora.round_child"]
runpy.run_module("agora.round_child", run_name="__main__")
"""


def _rigged_round_child(monkeypatch, exc):
    """A REAL fresh interpreter for the round, with only the round BODY rigged
    (it raises `exc` where a collect failure would): the child's own main, the
    wire and the process boundary run production code, so the death classes the
    shell sees are the ones a crashed round really produces."""
    real_popen = subprocess.Popen

    def spawn(argv, **kw):
        return real_popen([sys.executable, "-c", _RIG % {"exc": exc}], **kw)
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)


def test_systemexit_in_the_round_body_dies_like_a_process_not_an_answer(monkeypatch):
    """SystemExit is not the round's err to hand back (review #3): it escapes
    `run_round`, the child exits nonzero with no payload, and the shell counts
    the round as 子行程異常結束 - NOT as an rc=0 answer that applies state."""
    _rigged_round_child(monkeypatch, 'SystemExit("輪身 SystemExit")')
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))
    run_obj.last_md5 = "keep"

    cli._periodic_write(run_obj, None)

    assert run_obj.failures == 1, log_text()
    assert run_obj.last_md5 == "keep", "a dead round applies nothing"
    assert run_obj.collect_failed is True and run_obj.fingerprint is None
    line = outcome_lines()[-1]
    assert "子行程異常結束" in line, line


def test_ordinary_exception_in_the_round_body_still_answers_with_err(monkeypatch):
    """The companion half: a normal Exception in the round body stays the
    payload's `err` field with rc=0 - what the in-process shell caught before,
    it still counts the same way from the child."""
    _rigged_round_child(monkeypatch, 'RuntimeError("輪身普通錯")')
    run_obj = cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))

    cli._periodic_write(run_obj, None)

    assert run_obj.failures == 1, log_text()
    line = outcome_lines()[-1]
    assert "輪身普通錯" in line and "子行程異常結束" not in line, line
    assert run_obj.collect_failed is True, "the child's bookkeeping crossed on the err path"


# --- fix round #4 (review MED): the post-timeout drain cannot hang the shell ----

class _DrainStub:
    """The timeout branch driven without manufacturing an unkillable process:
    the round's own communicate times out; the drain does what `drain` says.
    Records every drain timeout, every wait timeout and every kill()."""
    drain = "returns"
    kills = 0
    waits: list = []
    drain_timeout: float | None = None

    def __init__(self, argv, **kw):
        self.pid = 4343
        self.returncode = None
        type(self).kills = 0
        type(self).waits = []
        type(self).drain_timeout = None

    def communicate(self, input=None, timeout=None):
        if input is not None:                        # the round itself: wedged
            raise subprocess.TimeoutExpired(cmd="agora.round_child", timeout=timeout)
        type(self).drain_timeout = timeout
        if type(self).drain == "times_out":
            raise subprocess.TimeoutExpired(cmd="agora.round_child", timeout=timeout)
        return b"", b""

    def wait(self, timeout=None):
        type(self).waits.append(timeout)
        self.returncode = -9
        return -9

    def kill(self):
        type(self).kills += 1


def _spawn_drain_stub(monkeypatch, drain, killpg):
    _DrainStub.drain = drain
    monkeypatch.setattr(cli.subprocess, "Popen", _DrainStub)
    monkeypatch.setattr(cli.os, "killpg", killpg)
    return cli._Run(paths(), _record("agora:01TEST00000000000000000000", CL_SID, "/x"))


def test_timeout_drain_gets_its_own_deadline_and_a_final_kill(monkeypatch):
    """After the group kill the shell drains the corpse's pipes - and must not
    wait forever for them (review #4): the drain carries its own deadline, and a
    child that still does not close them gets `proc.kill()` with a bounded wait."""
    run_obj = _spawn_drain_stub(monkeypatch, "times_out", lambda pgid, sig: None)

    with pytest.raises(cli.RoundChildDead) as exc:
        cli._round_child(paths(), run_obj, None)
    assert "逾時" in str(exc.value)
    assert _DrainStub.drain_timeout is not None, "the drain waits forever"
    assert _DrainStub.kills == 1, "a corpse that keeps the pipes open is SIGKILLed"
    assert None not in _DrainStub.waits, _DrainStub.waits


def test_timeout_refused_group_kill_still_kills_the_child(monkeypatch):
    """The other branch: `os.killpg` denied (PermissionError) means NOTHING was
    killed - even a drain that comes back clean must end with the child itself
    killed, bounded (review #4)."""
    def refused(pgid, sig):
        raise PermissionError
    run_obj = _spawn_drain_stub(monkeypatch, "returns", refused)

    with pytest.raises(cli.RoundChildDead):
        cli._round_child(paths(), run_obj, None)
    assert _DrainStub.kills == 1, "killpg was refused: the direct kill is mandatory"
    assert None not in _DrainStub.waits, _DrainStub.waits
