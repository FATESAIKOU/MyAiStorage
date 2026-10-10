"""Periodic write-back while `agora continue` runs (change continue-periodic-sync).

Two harnesses:

* an in-process fake (`SyncAgent`) whose "agent" is a plain `sleep`, with `collect`
  growing the conversation by itself - what lets a test decide exactly how many rounds
  happen and what each one sees;
* the real claude adapter over `fake_claude.py` in a subprocess, for the two things a
  fake cannot show: that nothing of agora's reaches the terminal the agent owns
  (fd 1/2), and that a hard kill leaves a recovery that lands in the same session.

The scripted fake's `wait-sync` watches `<state>/continue.log` for a complete line;
every round ends with exactly one such line, so the agent can be held open across a
write-back without any test-side timing.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h
from agora.agents import claude as C
from agora.agents.base import Exported, Launch

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fakes" / "fake_claude.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
CL_SID = "11111111-2222-4333-8444-555555555555"
SECONDS = "0.005"          # 0.3 s, right at the top of the floor for tests


class SyncAgent:
    """A fake agent whose session grows once per `collect`, and whose process is a
    `sleep` of the test's choosing - so rounds are decided, not raced."""

    name = "opencode"

    def __init__(self):
        self.sessions: dict[str, list[str]] = {"ses_a": ["問題", "回答"]}
        self.launched: list[Launch] = []
        self.collects = 0
        self.speak_rounds = 99      # how many collects answer with new content
        self.sleep = 0.7            # how long the "agent" holds the terminal

    def _exported(self, sid):
        msgs = self.sessions[sid]
        return Exported(session_id=sid, raw=json.dumps({"id": sid, "m": msgs}, ensure_ascii=False).encode(),
                        dir="/tmp", title=msgs[0][:20], created_at="2026-10-01T00:00:00Z",
                        agent_version="9.9", message_count=len(msgs))

    def export(self, sid):
        return self._exported(sid)

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def native(self, turns):
        return json.dumps({"id": "synth", "m": ["\n".join(lines) for _role, lines in turns]},
                          ensure_ascii=False).encode()

    def append_native(self, raw, turns):
        msgs = list(json.loads(raw)["m"])
        if len(msgs) % 2:
            msgs.append(cli.NO_REPLY)
        msgs += ["\n".join(lines) for _role, lines in turns]
        return json.dumps({"id": "appended", "m": msgs}, ensure_ascii=False).encode()

    def start_native(self, raw, workdir):
        new = f"ses_n{len(self.sessions)}"
        self.sessions[new] = list(json.loads(raw)["m"])
        launch = Launch(argv=["sleep", str(self.sleep)], cwd=str(workdir),
                        agent_session_id=new, before_count=len(self.sessions[new]))
        self.launched.append(launch)
        return launch

    def collect(self, launch):
        self.collects += 1
        if self.speak_rounds <= 0:
            return None
        self.speak_rounds -= 1
        msgs = self.sessions[launch.agent_session_id]
        msgs.append(f"ZZ 第 {self.collects} 次發言")
        return self._exported(launch.agent_session_id)


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    agent = SyncAgent()
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    return agent


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def paths():
    return store.Paths.from_env()


def header_of(agora_id):
    return store.Index(paths()).header(agora_id.split(":")[1])


def document_of(agora_id):
    return (paths().mirror / agora_id.split(":")[1] / "session.md").read_text(encoding="utf-8")


def on_drive(agora_id):
    return Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions" / agora_id.split(":")[1]


def log_text():
    try:
        return (paths().state / cli.CONTINUE_LOG).read_text(encoding="utf-8")
    except OSError:
        return ""


def _import(capsys, *extra):
    return run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode", *extra)


def _continue(capsys, agora_id, *extra):
    return run(capsys, "continue", "session", agora_id, "--agent", "opencode", "--dir", "/tmp",
               "--sync-period", SECONDS, *extra)


def _stage_spy(monkeypatch):
    """Record the (id, body) of every version actually staged, in order."""
    staged = []
    real = cli._save

    def spy(paths_, hdr, body, raw, **kwargs):
        staged.append((hdr["id"], body))
        return real(paths_, hdr, body, raw, **kwargs)
    monkeypatch.setattr(cli, "_save", spy)
    return staged


def _extra_sessions(agora_id):
    return [hdr for _u, hdr, _s in store.Index(paths()).search([]) if hdr["id"] != agora_id]


# --- the spec's scenarios ---------------------------------------------------------

def test_a_round_writes_while_the_agent_is_still_running(env, capsys, monkeypatch):
    """Scenario「執行中看得到進度」: the sleep holds the terminal across two rounds, so
    the first stage is a version the agent was still writing after."""
    _, a, _ = _import(capsys)
    staged = _stage_spy(monkeypatch)
    env.sleep = 0.85

    code, out, _ = _continue(capsys, a)

    assert code == 0 and out == a
    assert len(staged) >= 2, f"a round before the final: {len(staged)}"
    assert "ZZ 第 1 次發言" in staged[0][1], "the first stage is the round's version"
    assert staged[0] != staged[-1], "the final stage is newer than the round"
    assert f"ZZ 第 {env.collects} 次發言" in staged[-1][1]
    assert "定期寫回：已寫回" in log_text()
    assert "定期寫回" not in capsys.readouterr().err   # the log, not the terminal


def test_nothing_new_is_not_written(env, capsys, monkeypatch):
    """Scenario「沒有新內容不寫」: one round answers, the rest have nothing, so exactly
    one version is staged and the later rounds say so in the log.

    The rounds are driven directly, not slept through: under load a fixed sleep can
    end before the second round ever runs, and then "more than one round ran" is a
    wish about the scheduler rather than an assertion about the code."""
    env.speak_rounds = 1
    _, a, _ = _import(capsys)
    staged = _stage_spy(monkeypatch)
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a, "raw_md5": None}}
    run_obj = cli._Run(paths(), record)
    agent = cli.load_agent("opencode")

    for _ in range(3):
        cli._periodic_write(run_obj, agent)

    assert len(staged) == 1, [s[1][:40] for s in staged]
    assert "ZZ 第 1 次發言" in staged[0][1], "the one stage is the answering round"
    rounds = [line for line in log_text().splitlines() if "定期寫回" in line]
    assert len(rounds) == 3, f"three rounds ran: {rounds}"
    assert "已寫回" in rounds[0] and all("沒有新內容" in line for line in rounds[1:]), rounds
    assert (paths().outbox / a.split(":")[1]).exists() is False, "nothing left waiting"


def test_the_final_round_rescues_even_with_nothing_new(env, capsys, monkeypatch):
    """Scenario「最後一次沒有新內容但已被刪」: after the last write the session goes away;
    the quiet agent still has to leave its work somewhere alive."""
    env.speak_rounds = 1
    env.sleep = 0.5
    _, a, _ = _import(capsys)
    staged = _stage_spy(monkeypatch)
    real_round = cli._periodic_write
    deleted = {"done": False}

    def round_then_delete(run_, agent):
        real_round(run_, agent)
        if not deleted["done"]:
            deleted["done"] = True
            shutil.rmtree(on_drive(a))
            # make this machine see it gone too, the way a sync would
            store.sync(paths())
    monkeypatch.setattr(cli, "_periodic_write", round_then_delete)

    code, out, err = _continue(capsys, a)

    assert deleted["done"], "the round has to have happened"
    assert code == 0 and out != a, err
    kept = _extra_sessions(a)
    assert len(kept) == 1 and kept[0]["id"] == out
    assert [p["id"] for p in h.agora_of(kept[0])["parents"]] == [a]
    assert "ZZ 第 1 次發言" in document_of(out), "the same conversation, still alive"


def test_a_deletion_mid_run_moves_later_writes_to_the_rescue(env, capsys, monkeypatch):
    """Scenario「途中被刪」: the uploader's rescue notices, the next round adopts it -
    exactly one new session, and every write after the deletion goes there."""
    env.sleep = 2.6               # room for four rounds even with upload time between
    _, a, _ = _import(capsys)
    staged = _stage_spy(monkeypatch)
    real_round = cli._periodic_write
    rounds = {"n": 0}

    def delete_on_second(run_, agent):
        rounds["n"] += 1
        if rounds["n"] == 2:
            shutil.rmtree(on_drive(a))         # another machine deletes it, online
        real_round(run_, agent)
    monkeypatch.setattr(cli, "_periodic_write", delete_on_second)

    code, out, err = _continue(capsys, a)

    assert rounds["n"] >= 3, "the deletion has to land between two rounds"
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"one rescue, not one per round: {[k['id'] for k in kept]}"
    assert kept[0]["id"] == out, "the final state lives in the rescue"
    assert [p["id"] for p in h.agora_of(kept[0])["parents"]] == [a]
    assert f"ZZ 第 {env.collects} 次發言" in document_of(out), "the newest words are in the rescue"
    assert "另存" in err or "另存" in log_text(), err
    assert not on_drive(a).exists(), "the deleted one is not pushed back"


def test_an_offline_deletion_is_adopted_once_the_uploader_frees_it(env, capsys, monkeypatch):
    """Scenario「離線期間被刪」: while Drive cannot be listed the rounds stage updates; when
    the line is back the uploader rescues once, and the following round adopts it."""
    env.sleep = 2.6
    _, a, _ = _import(capsys)
    staged = _stage_spy(monkeypatch)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")     # offline: no listing, no rescue
    real_round = cli._periodic_write
    rounds = {"n": 0}

    def online_and_delete_on_second(run_, agent):
        rounds["n"] += 1
        if rounds["n"] == 2:
            monkeypatch.delenv("FAKE_RCLONE_FAIL")
            shutil.rmtree(on_drive(a))
        real_round(run_, agent)
    monkeypatch.setattr(cli, "_periodic_write", online_and_delete_on_second)

    code, out, err = _continue(capsys, a)

    assert rounds["n"] >= 3
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"the uploader and the foreground must agree on one: {[k['id'] for k in kept]}"
    assert kept[0]["id"] == out and [p["id"] for p in h.agora_of(kept[0])["parents"]] == [a]
    assert f"ZZ 第 {env.collects} 次發言" in document_of(out)
    assert not on_drive(a).exists()


def test_a_failed_round_is_counted_and_named(env, capsys, monkeypatch):
    """Scenario「不弄亂畫面」 (the count half): a round whose upload does not get through is
    counted, the agent is not disturbed, and the end says where the log is."""
    env.sleep = 0.5
    _, a, _ = _import(capsys)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")

    code, out, err = _continue(capsys, a)

    assert code == cli.EXIT_IN_OUTBOX
    assert "定期寫回失敗 1 次" in err and str(paths().state / cli.CONTINUE_LOG) in err
    assert "定期寫回：已寫回" in log_text() and "上傳失敗" in log_text()
    assert "接著" not in err, "the round's own words do not reach the terminal"


def test_sync_period_zero_turns_the_loop_off(env, capsys):
    """Scenario「不自動同步」: only the final write-back runs - no log at all."""
    env.sleep = 0.4
    _, a, _ = _import(capsys)

    code, out, _ = run(capsys, "continue", "session", a, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")

    assert code == 0 and out == a
    assert "ZZ 第 1 次發言" in document_of(a)
    assert "沒有新內容" not in log_text() and not (paths().state / cli.CONTINUE_LOG).exists()


@pytest.mark.parametrize("given", ["-1", "-0.5", "abc", "", "nan", "inf"])
def test_a_bad_period_is_refused_before_the_agent(env, capsys, given):
    """Scenario「不合法的間隔」: exit 1, no agent, and nothing staged."""
    _, a, _ = _import(capsys)
    before = document_of(a)

    code, out, err = _continue(capsys, a, "--sync-period", given)

    assert code == cli.EXIT_INPUT and out == "" and "--sync-period" in err
    assert not env.launched, "the agent was never opened"
    assert document_of(a) == before


def test_the_interval_has_a_floor():
    """A period so small it rounds to zero still waits a tenth of a second, so the loop
    cannot spin."""
    assert cli._sync_interval_seconds(type("A", (), {"sync_period": "0.0001"})()) == cli.SYNC_MINIMUM_SECONDS
    assert cli._sync_interval_seconds(type("A", (), {"sync_period": None})()) == 300.0
    assert cli._sync_interval_seconds(type("A", (), {"sync_period": "0.5"})()) == 30.0


def test_an_exception_waits_for_the_agent_before_it_leaves(env, capsys, monkeypatch, tmp_path):
    """P9a: `subprocess.run` used to kill the child and take the terminal; with Popen we
    decide. An exception in the loop never leaves while the agent is alive: it waits for
    it to finish first, then goes."""
    marker = tmp_path / "agent-finished"
    env.sleep = 0.5
    _, a, _ = _import(capsys)
    launch_argv = ["/bin/sh", "-c", f"sleep 0.5; touch {marker}"]   # the agent's whole life

    def start_native(raw, workdir):
        launch = SyncAgent.start_native(env, raw, workdir)
        launch.argv = launch_argv
        return launch

    monkeypatch.setattr(env, "start_native", start_native)

    def boom(run_, agent):
        raise RuntimeError("the round itself broke")

    monkeypatch.setattr(cli, "_periodic_write", boom)
    code, out, err = _continue(capsys, a)

    assert code == cli.EXIT_ERROR and "非預期" in err
    assert marker.exists(), "agora left before the agent it started had finished"


def test_the_redirect_is_put_back_when_the_agent_is_done(env, capsys, monkeypatch):
    """The interactive mode calls cli.main in its own process (`tui.outside`), so the
    redirect covers the whole process while it lasts. It has to be gone afterwards, or
    the TUI's own painting would land in continue.log (design「不弄亂 agent 的畫面」)."""
    import sys as _sys
    env.sleep = 0.5
    _, a, _ = _import(capsys)
    stdout_before, stderr_before = _sys.stdout, _sys.stderr

    code, out, _ = _continue(capsys, a)

    assert code == 0 and out == a
    assert _sys.stdout is stdout_before and _sys.stderr is stderr_before


# --- a fork's own session, offline -------------------------------------------------

def test_an_offline_fork_keeps_writing_to_itself(claude_lab, capsys, monkeypatch, tmp_path):
    """The fork's session is unconfirmed from the start; rounds write to it and the
    uploader's "Drive never had it" rule keeps every version new (continue-fork F1).

    The agent is scripted, not slept: every wait-sync releases on a round's log line,
    so the run holds the terminal across write-backs however slow the machine is -
    a fixed sleep can end before a round ever runs under load."""
    a = _import_claude(capsys)
    _script(claude_lab, monkeypatch, "append:ZZ 第 1 次發言,wait-sync,wait-sync,exit")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    terminal = tmp_path / "terminal.txt"
    fd = os.open(terminal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", a, "--agent", "claude",
             "--dir", str(claude_lab["work"]), "--sync-period", SECONDS, "--fork"],
            stdout=fd, stderr=fd, stdin=subprocess.DEVNULL, env=dict(os.environ),
            cwd=str(tmp_path))
        assert proc.wait(timeout=120) == cli.EXIT_IN_OUTBOX
    finally:
        os.close(fd)

    text = terminal.read_text(encoding="utf-8")
    ids = [t for t in text.split() if t.startswith("agora:") and len(t) == len("agora:") + 26]
    assert ids, "the fork printed no agora id"
    b = ids[0]
    assert b != a
    assert "FAKE-AGENT-ERROR" not in text, text[-500:]
    assert "ZZ 第 1 次發言" in document_of(b), "a round wrote the fork while offline"
    # N2: stdout's id is the fork itself, not a session rescued later - and no second
    # new session exists anywhere, not even one that never reached Drive. Drive alone
    # cannot say this: while the copies fail, neither B nor a rescue of it is up there.
    hdr_b = header_of(b)
    assert h.agora_of(hdr_b)["relation"] == "fork", "stdout 印的就該是 fork 出來的 B"
    assert [p["id"] for p in h.agora_of(hdr_b)["parents"]] == [a]
    others = _extra_sessions(a)
    assert [hdr["id"] for hdr in others] == [b], "index 裡只有 A 和 B，沒有第二個新 Session"
    assert not [hdr for hdr in others if h.agora_of(hdr).get("relation") == "continue"], \
        "B 沒有被另存成 continue"
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    assert store.upload_batch(store.Drive(paths()), paths()) == []
    kept = sorted(p.name for p in (Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions").iterdir())
    assert kept == sorted({a.split(":")[1], b.split(":")[1]}), "the fork itself, nothing rescued"
    assert "ZZ 第 1 次發言" in on_drive(b).joinpath("session.md").read_text(encoding="utf-8")


# --- the second continue on the same session --------------------------------------

def test_a_second_continue_is_refused_while_the_first_still_holds_the_lock(env, capsys):
    """Scenario「同一台機器再接續一次」: the pending lock is the answer, not the file."""
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "agent_session_id": "ses_x", "dir": "/tmp",
              "before_count": 0, "parent": {"id": a, "raw_md5": None}}
    path, lock = cli._write_pending(paths(), record)
    try:
        code, out, err = _continue(capsys, a)
        assert code == cli.EXIT_INPUT and "正在接續中" in err and out == ""
        assert not env.launched
    finally:
        lock.close()

    code, out, _ = _continue(capsys, a)      # nobody holds it any more: the record is stale
    assert code == 0 and out == a


def test_a_fork_is_not_refused_by_a_live_continue(env, capsys):
    """The same-machine rule is about writing one session; a fork writes another."""
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "agent_session_id": "ses_x", "dir": "/tmp",
              "before_count": 0, "parent": {"id": a, "raw_md5": None}}
    path, lock = cli._write_pending(paths(), record)
    try:
        code, out, _ = _continue(capsys, a, "--fork")
        assert code == 0 and out != a
    finally:
        lock.close()


# --- the update decision and the pending file --------------------------------------

def test_a_write_back_while_a_uploader_compares_it_is_an_update(env, capsys, monkeypatch):
    """`never_confirmed` has no exemption while `.done-X` exists: an uploader comparing
    that very id may have found Drive's copy, and the write cannot tell (design P1)."""
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")     # entries stay in the outbox
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    hdr = header_of(a)
    body = document_of(a).split("\n---\n", 1)[1]
    raw = (paths().mirror / ulid / hdr["agora"]["raw"]["file"]).read_bytes()
    cli._save(paths(), hdr, body, raw, update=False)     # a fresh entry, unmarked
    assert not store._is_update(paths().outbox / ulid)

    cli._save(paths(), hdr, body, raw, update=True)      # unmarked, no .done: still new
    assert not store._is_update(paths().outbox / ulid)

    shutil.move(str(paths().outbox / ulid), str(paths().outbox / f".done-{ulid}"))
    cli._save(paths(), hdr, body, raw, update=True)      # compared right now: an update

    assert store._is_update(paths().outbox / ulid)


def test_the_pending_file_follows_the_rescue_and_the_lock_goes_with_it(env, capsys):
    """design「pending」P4: the name moves to the rescued session so `continuing()` and
    `delete` guard the right one; the flock is on the inode, which a rename keeps."""
    import fcntl
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "agent_session_id": "ses_x", "dir": "/tmp",
              "before_count": 0, "parent": {"id": a, "raw_md5": None}}
    path, lock = cli._write_pending(paths(), record)
    run_ = cli._Run(paths(), record, pending=path, lock=lock)
    try:
        note = cli._follow(run_, "agora:01K6RESCUED0000000000000AA")
        assert note and "另存" in note
        moved = paths().pending / "01K6RESCUED0000000000000AA.json"
        assert moved.exists() and not path.exists()
        assert store.continuing(paths(), "01K6RESCUED0000000000000AA"), "the lock followed"
        assert not store.continuing(paths(), a.split(":")[1]), "and the old id is free"
        with open(moved) as f:                    # the same inode is still locked
            with pytest.raises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        lock.close()


def test_previous_sources_do_not_accumulate_across_rounds(env, capsys, monkeypatch):
    """design P5: one entry per old source, however many rounds write."""
    env.sleep = 0.85
    _, a, _ = _import(capsys)
    env.sessions["ses_launch_old"] = ["舊的", "好"]     # give it something to list once

    code, out, _ = _continue(capsys, a)

    assert code == 0
    hdr = header_of(out)
    assert hdr["agora"].get("previous_sources", []).count("opencode:ses_a") <= 1


# --- the terminal belongs to the agent (subprocess, real claude adapter) -----------

def _wrapper(tmp_path, name, target):
    w = tmp_path / name
    w.write_text(f"#!/bin/sh\nexec {sys.executable} {target} \"$@\"\n")
    w.chmod(0o755)
    return w


@pytest.fixture
def claude_lab(tmp_path, monkeypatch):
    """The real claude adapter over a fake CLI, with the fixture session laid out."""
    remote = tmp_path / "remote"
    remote.mkdir()
    rclone = _wrapper(tmp_path, "rclone", FAKE_RCLONE)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(rclone))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    work = tmp_path / "proj"
    work.mkdir()
    proj = home / ".claude" / "projects" / C.encode_project_dir(work)
    proj.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(_wrapper(tmp_path, "claude", FAKE_CLAUDE)))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    return {"home": home, "work": work, "remote": remote}


def _script(lab, monkeypatch, steps):
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", steps)
    monkeypatch.setenv("FAKE_SYNC_SIGNAL", str(paths().state / cli.CONTINUE_LOG))


def _import_claude(capsys):
    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    return a


def test_nothing_of_agoras_reaches_the_agents_terminal(claude_lab, capsys, monkeypatch, tmp_path):
    """Scenario「不弄亂畫面」: stdout and stderr share one O_APPEND file, the fake brackets
    its run with markers, and between them there is no agora line - even though a round
    reads a half-written line and another one cannot upload."""
    a = _import_claude(capsys)
    _script(claude_lab, monkeypatch, "append:ZZFD 第一句,wait-sync,partial,wait-sync,wait-sync,exit")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")     # rounds stage, uploads fail
    terminal = tmp_path / "terminal.txt"
    fd = os.open(terminal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", a, "--agent", "claude",
             "--dir", str(claude_lab["work"]), "--sync-period", SECONDS],
            stdout=fd, stderr=fd, stdin=subprocess.DEVNULL, env=dict(os.environ),
            cwd=str(tmp_path))
        assert proc.wait(timeout=120) == cli.EXIT_IN_OUTBOX
    finally:
        os.close(fd)

    text = terminal.read_text(encoding="utf-8")
    start = text.index("FAKE-AGENT-START")
    end = text.index("FAKE-AGENT-END")
    between = text[start + len("FAKE-AGENT-START"):end]
    assert "[agora]" not in between, f"agora wrote to the agent's terminal: {between!r}"
    assert "FAKE-AGENT-ERROR" not in text, text
    assert "最後一行不完整" in log_text(), "the half-written line was read and logged"
    assert text[end:].strip(), "the final words did come back after the agent ended"


def _wait_for(condition, timeout=30.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.05)
    pytest.fail(f"waited {timeout:g}s for {what}")


def test_a_hard_kill_leaves_the_work_and_the_recovery_writes_it_back(claude_lab, capsys, monkeypatch, tmp_path):
    """Scenario「agora 被強制結束」: the first round kills agora; the agent finishes on its
    own, and the next command's recovery lands in the same session. (That the round's
    version is on Drive is the other test's job; here the timing races the fake on
    purpose, and the recovery is what has to be right.)"""
    a = _import_claude(capsys)
    ulid = a.split(":")[1]
    _script(claude_lab, monkeypatch, "append:ZZKILL 第一句,wait-sync,append:ZZKILL 第二句,exit")
    monkeypatch.setenv("FAKE_SYNC_TIMEOUT", "2")   # agora is gone after round 1: do not hang
    terminal = tmp_path / "terminal.txt"
    child_env = {**os.environ, "AGORA_TEST_FAULT": "after-periodic-write"}
    fd = os.open(terminal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", a, "--agent", "claude",
             "--dir", str(claude_lab["work"]), "--sync-period", SECONDS],
            stdout=fd, stderr=fd, stdin=subprocess.DEVNULL, env=child_env, cwd=str(tmp_path))
        assert proc.wait(timeout=120) == 137, "the fault point kills agora itself"
    finally:
        os.close(fd)
    assert "定期寫回" in log_text(), "the round had to happen before the crash point"
    assert (paths().pending / f"{ulid}.json").exists(), "the record is the thing left behind"
    # The fake keeps playing its script; the pending lock frees only once it has gone.
    _wait_for(lambda: not store.continuing(paths(), ulid), what="the agent to exit")

    code, out, err = run(capsys, "search", "session")

    assert code == 0
    assert "補存" in err
    assert not (paths().pending / f"{ulid}.json").exists()
    # Whichever it got to, the work is in the session it was continued as - not saved
    # under a new id because "nobody wrote the pending one back".
    assert "ZZKILL 第一句" in document_of(a)
    assert len(_extra_sessions(a)) == 0, "the recovery did not invent a second session"


def test_final_write_rescues_when_target_queued_in_outbox_and_deleted_on_drive(env, capsys, monkeypatch):
    """T1 / T4: target has a queued .update in outbox, and Drive lost it.
    The final write-back must check Drive (not short-circuit), rescue into a new ID,
    print the new ID, and not duplicate rescues."""
    env.speak_rounds = 1
    env.sleep = 0.5
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]

    real_round = cli._periodic_write
    deleted = {"done": False}

    def round_then_queue_and_delete(run_, agent):
        real_round(run_, agent)
        if not deleted["done"]:
            deleted["done"] = True
            hdr = store.Index(paths()).header(ulid)
            raw = (paths().mirror / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
            body = (paths().mirror / ulid / "session.md").read_text(encoding="utf-8")
            store.stage(paths(), hdr, body, raw, update=True, kind="continue")
            assert ulid in store.outbox_ulids(paths())
            shutil.rmtree(on_drive(a))
            # store.sync(paths()) is NOT called

    monkeypatch.setattr(cli, "_periodic_write", round_then_queue_and_delete)
    code, out, err = _continue(capsys, a)

    assert deleted["done"]
    assert code == 0
    assert out != a, f"expected rescued id, got {out}"
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"expected exactly 1 rescue session, got {[k['id'] for k in kept]}"
    assert kept[0]["id"] == out
    assert [p["id"] for p in h.agora_of(kept[0])["parents"]] == [a]
    assert "另存" in err


def test_write_one_writes_when_moved_here_even_if_md5_unchanged(env, capsys):
    """T6: when target moved, write must not be skipped even if raw md5 matches last_md5."""
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a, "raw_md5": "x"}}
    run_obj = cli._Run(paths(), record)
    agent = cli.load_agent("opencode")
    exported = agent.export("ses_a")
    md5 = store.hashlib.md5(exported.raw).hexdigest()
    run_obj.last_md5 = md5

    saved, target, _ = cli._write_one(paths(), run_obj, exported, agent, check_drive=False)
    assert saved is None

    child_id = f"agora:{h.new_ulid()}"
    child_hdr = {
        "id": child_id, "type": "session", "title": "child",
        "agora": {"header": h.HEADER_VERSION, "created_at": "2026-10-10T00:00:00Z",
                  "updated_at": "2026-10-10T00:00:00Z", "relation": "continue",
                  "parents": [{"id": a, "raw_md5": "x"}],
                  "source": {"agent": "opencode", "session_id": "ses_a"}},
    }
    store.Index(paths()).put(child_id.split(":")[1], "dummy", child_hdr, "dummy body")
    saved2, target2, _ = cli._write_one(paths(), run_obj, exported, agent, check_drive=False)
    assert saved2 is not None, "write must not be skipped when target moved"
    assert target2 == child_id


def test_periodic_write_counts_exception_failure(env, capsys, monkeypatch):
    """T7: when an exception happens in _periodic_write, run.failures is incremented."""
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a, "raw_md5": "x"}}
    run_obj = cli._Run(paths(), record)
    agent = cli.load_agent("opencode")

    def broken_collect(launch):
        raise RuntimeError("collect failed")

    monkeypatch.setattr(agent, "collect", broken_collect)
    assert run_obj.failures == 0
    cli._periodic_write(run_obj, agent)
    assert run_obj.failures == 1
    assert "失敗（第 1 次）" in log_text()


def test_follow_updates_run_target():
    """T8: _follow updates run.target to the new target ID."""
    record = {"agora_id": "agora:01M4AAA", "agent": "opencode", "dir": "/tmp"}
    run_obj = cli._Run(paths(), record)
    assert run_obj.target == "agora:01M4AAA"
    cli._follow(run_obj, "agora:01M4BBB")
    assert run_obj.target == "agora:01M4BBB"


def test_periodic_write_detects_unuploaded_items_when_uploader_idle(env, capsys, monkeypatch):
    """T5: when outbox has unuploaded items and uploader is idle, foreground notices and counts failure."""
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a, "raw_md5": "x"}}
    run_obj = cli._Run(paths(), record)
    agent = cli.load_agent("opencode")
    (paths().outbox / a.split(":")[1]).mkdir(parents=True, exist_ok=True)
    env.speak_rounds = 0
    cli._periodic_write(run_obj, agent)
    assert run_obj.failures >= 1
    assert "上傳未完成" in log_text()
