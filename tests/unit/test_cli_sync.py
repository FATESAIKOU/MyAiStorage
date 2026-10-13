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

import copy
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

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


def _continue_claude(capsys, lab, agora_id, *extra):
    return run(capsys, "continue", "session", agora_id, "--agent", "claude",
               "--dir", str(lab["work"]), "--sync-period", SECONDS, *extra)


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


def _wait_for(condition, timeout=30.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.05)
    pytest.fail(f"waited {timeout:g}s for {what}")


@pytest.fixture
def clean_subprocesses(monkeypatch):
    """Ensure any subprocess.Popen spawned during detached uploader tests is reaped."""
    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        p = real_popen(*args, **kwargs)
        procs.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    yield
    for p in procs:
        try:
            p.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


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


def test_a_rescue_mid_flight_is_adopted_not_minted_again(env, capsys, monkeypatch):
    """issue #59: the uploader claimed X for rescue (.done-X) but has not staged its
    rescue yet; the foreground write-back must adopt the rescue once it lands
    instead of saving a second one. Exactly one new session either way.

    Deterministic without timing luck: the test holds the uploader inside its
    rescue (gated `stage`) while the foreground resolves, then lets it finish -
    the interleave a slow Drive produces on its own in
    tests/integration/test_e2e_sync.py::test_a_session_deleted_mid_run_is_rescued_exactly_once.
    """
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    agent = cli.load_agent("opencode")
    drive = store.Drive(paths())
    assert store.upload_batch(drive, paths()) == []
    assert on_drive(a).is_dir(), "A confirmed on Drive first"
    # a periodic round's staged update, like the run's own writes
    mirror = paths().mirror / ulid
    hdr, body = h.split_document((mirror / "session.md").read_text(encoding="utf-8"))
    raw = (mirror / h.agora_of(hdr)["raw"]["file"]).read_bytes()
    store.stage(paths(), hdr, body, raw, update=True, kind="continue")
    agent.sessions["ses_a"].append("ZZ autopsy 第三句")
    exported = agent.export("ses_a")
    shutil.rmtree(on_drive(a))                     # another machine deleted A

    gate, release = threading.Event(), threading.Event()
    real_stage = store.stage
    state = {"uploader": None, "errors": []}

    def gated_stage(p, header, body_, raw_bytes, **kwargs):
        if (header["id"].split(":", 1)[1] != ulid
                and threading.current_thread() is state["uploader"]
                and not release.is_set()):
            gate.set()
            assert release.wait(timeout=30), "foreground never started while rescue held"
        return real_stage(p, header, body_, raw_bytes, **kwargs)

    monkeypatch.setattr(store, "stage", gated_stage)

    def uploader():
        try:
            lock = store.hold_upload_lock(paths(), blocking=True)
            try:
                store.upload_batch(store.Drive(paths()), paths())
            finally:
                lock.close()
        except Exception as e:  # noqa: BLE001 - surfaced below
            state["errors"].append(e)

    state["uploader"] = threading.Thread(target=uploader, daemon=True)
    state["uploader"].start()
    assert gate.wait(timeout=30), "uploader never reached its rescue"
    assert (paths().outbox / f".done-{ulid}").is_dir(), "X claimed, rescue in flight"

    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a}}
    run = cli._Run(paths(), record)
    result = {}

    def foreground():
        try:
            result["saved"] = cli._write_one(paths(), run, exported, agent, check_drive=True)
        except Exception as e:  # noqa: BLE001 - surfaced below
            result["error"] = e

    review = threading.Thread(target=foreground, daemon=True)
    review.start()
    time.sleep(2.0)          # let the foreground resolve while the rescue is held
    release.set()            # the uploader lands its rescue; the foreground must adopt it
    review.join(timeout=60)
    state["uploader"].join(timeout=60)
    assert not state["errors"], state["errors"]
    assert "error" not in result, result.get("error")
    assert not review.is_alive() and not state["uploader"].is_alive()

    assert store.upload_batch(drive, paths()) == []
    remote = {e["Name"] for e in
              json.loads(drive._run("lsjson", "gdrive:sessions", "--dirs-only"))}
    new = remote - {ulid}
    assert len(new) == 1, f"exactly one rescue, not {sorted(new)}"
    bid = f"agora:{next(iter(new))}"
    assert [p["id"] for p in h.agora_of(header_of(bid))["parents"]] == [a]
    assert "ZZ autopsy 第三句" in document_of(bid)


def test_a_rescue_completed_before_claim_is_adopted_not_minted(env, capsys, monkeypatch):
    """M-1: the uploader finished between resolve and claim (staged and indexed,
    claim released); the post-claim recheck finds it - no second session.

    Deterministic: `_saved_as` misses once, like a resolve that ran just before
    the rescue landed; the recheck after claim sees it for real.
    """
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    agent = cli.load_agent("opencode")
    drive = store.Drive(paths())
    assert store.upload_batch(drive, paths()) == []
    assert on_drive(a).is_dir(), "A confirmed on Drive first"
    mirror = paths().mirror / ulid
    hdr, body = h.split_document((mirror / "session.md").read_text(encoding="utf-8"))
    raw = (mirror / h.agora_of(hdr)["raw"]["file"]).read_bytes()
    store.stage(paths(), hdr, body, raw, update=True, kind="continue")
    agent.sessions["ses_a"].append("ZZ m1 第四句")
    exported = agent.export("ses_a")
    shutil.rmtree(on_drive(a))                     # another machine deleted A
    # the uploader's rescue, completed before the foreground resolves
    assert store.upload_batch(store.Drive(paths()), paths()) == []
    bid = store.Index(paths()).children(ulid)
    assert len(bid) == 1, f"setup rescued exactly once: {bid}"

    calls = {"n": 0}
    real_saved_as = cli._saved_as

    def miss_once(index, record, target):
        calls["n"] += 1
        if calls["n"] == 1:
            return None            # the resolve ran just before the rescue landed
        return real_saved_as(index, record, target)

    monkeypatch.setattr(cli, "_saved_as", miss_once)
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a}}
    run = cli._Run(paths(), record)
    saved, target, _note = cli._write_one(paths(), run, exported, agent, check_drive=True)
    assert target == f"agora:{bid[0]}", f"adopted the rescue, not a new session: {target}"

    assert store.upload_batch(drive, paths()) == []
    remote = {e["Name"] for e in
              json.loads(drive._run("lsjson", "gdrive:sessions", "--dirs-only"))}
    assert remote - {ulid} == {bid[0]}, f"exactly one rescue: {sorted(remote)}"
    assert "ZZ m1 第四句" in document_of(f"agora:{bid[0]}")


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


def test_the_final_write_adopts_an_indexed_rescue_with_nothing_new(claude_lab, capsys, monkeypatch, tmp_path):
    """The uploader rescued A's last version into B and the agent then said nothing
    more: no periodic round adopts it first, the final write-back has to.

    This is the e2e's shape (tests/integration/test_e2e_sync.py, 途中被刪): what A
    held at the end is the version the uploader saved as B, and stdout has to name B.
    The agent is the scripted fake, held at `wait-file` until the round below really
    rescued: a fixed sleep only guesses that the round wins the race.
    """
    a = _import_claude(capsys)
    ulid = a.split(":")[1]
    release = tmp_path / "rescue-done"
    _script(claude_lab, monkeypatch, f"append:ZZ 第 1 次發言,wait-file:{release},exit")
    real_round = cli._periodic_write
    done = {"rescued": False}

    def round_then_rescue(run_, agent):
        real_round(run_, agent)
        if done["rescued"] or not run_.wrote:
            # Nothing of this run has reached A yet: a rescue staged now would carry
            # the *import's* source, not this run's, and is not the one being tested.
            return
        done["rescued"] = True
        hdr = store.Index(paths()).header(ulid)
        raw = (paths().mirror / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
        body = (paths().mirror / ulid / "session.md").read_text(encoding="utf-8")
        store.stage(paths(), hdr, body, raw, update=True, kind="continue")
        shutil.rmtree(on_drive(a))                     # another machine deleted A
        store.upload_batch(store.Drive(paths()), paths())   # the uploader's L7 rescue, in place
        release.touch()                                # rescued for real: the agent may leave

    monkeypatch.setattr(cli, "_periodic_write", round_then_rescue)
    code, out, err = _continue_claude(capsys, claude_lab, a)

    assert done["rescued"]
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"exactly one rescue: {[k['id'] for k in kept]}"
    b = kept[0]["id"]
    assert code == 0, err
    assert out == b, f"the final write named {out}, the rescue is {b}"
    assert "另存" in err, err
    assert not on_drive(a).exists(), "the deleted one is not pushed back"


def test_the_final_write_adopts_a_rescue_that_is_only_staged(claude_lab, capsys, monkeypatch, tmp_path):
    """The uploader staged the rescue B but has not indexed it yet (or died between the
    two): the final write-back, with nothing new, still has to find it in the outbox.

    A crash between `stage` and `remember` leaves the new session only in the outbox -
    `store.remember` runs right after the stage, but that window is real. The last write
    resolves its target with `check_drive=True` and, before it can call A dead, has to
    see the work that is already saved as B sitting right there. The scripted fake is
    held at `wait-file` until the round below really staged the rescue.
    """
    a = _import_claude(capsys)
    ulid = a.split(":")[1]
    release = tmp_path / "rescue-done"
    _script(claude_lab, monkeypatch, f"append:ZZ 第 1 次發言,wait-file:{release},exit")
    real_round = cli._periodic_write
    done = {"rescued": False, "b": None}

    def round_then_rescue_before_indexing(run_, agent):
        real_round(run_, agent)
        if done["rescued"] or not run_.wrote:
            # Same as above: the rescued entry has to be one this run wrote, or its
            # source is the import's and the three marks cannot match.
            return
        done["rescued"] = True
        hdr = store.Index(paths()).header(ulid)
        raw = (paths().mirror / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
        body = (paths().mirror / ulid / "session.md").read_text(encoding="utf-8")
        store.stage(paths(), hdr, body, raw, update=True, kind="continue")   # the entry being rescued
        shutil.rmtree(on_drive(a))                     # another machine deleted A
        remember = store.remember                     # staged, then the uploader stops
        monkeypatch.setattr(store, "remember", lambda *args, **kwargs: None)
        try:
            done["b"] = store._rescue_deleted(paths(), paths().outbox / ulid, ulid,
                                              lambda _m: None)
        finally:
            monkeypatch.setattr(store, "remember", remember)
        b = done["b"]
        assert b and (paths().outbox / b).is_dir(), "the rescue has to be waiting in the outbox"
        release.touch()                                # staged for real: the agent may leave

    monkeypatch.setattr(cli, "_periodic_write", round_then_rescue_before_indexing)
    code, out, err = _continue_claude(capsys, claude_lab, a)

    b = f"agora:{done['b']}"
    assert done["rescued"] and store.Index(paths()).header(done["b"]) is None, \
        "nothing indexed it before the final write"
    kept = sorted(p.name for p in on_drive(a).parent.iterdir())
    assert kept == [done["b"]], f"exactly one rescue exists: {kept}"
    assert code == 0, err
    assert out == b, f"the final write named {out}, the staged rescue is {b}"
    assert "另存" in err, err
    assert not on_drive(a).exists(), "the deleted one is not pushed back"
    assert on_drive(b).joinpath("session.md").is_file(), "the adopted rescue went up"


def test_a_rescue_the_uploader_remembered_carries_the_rest_of_the_run(claude_lab, capsys, monkeypatch):
    """The e2e's interleaving (sync-8c5d87a-rescue): mid-run the uploader rescues the
    version A has into B - on Drive, remembered, out of the outbox - and the agent then
    says nothing more. The rounds after it and the last write-back still have to move
    to B, not keep resolving to A until the end.

    The rescue runs in a process of its own, the way the detached uploader does: its
    `remember` reaches the index from outside this process. The scripted fake waits
    for four rounds, so several empty rounds and the final write follow the rescue.
    """
    a = _import_claude(capsys)
    ulid = a.split(":")[1]
    _script(claude_lab, monkeypatch, "append:ZZ 第 1 次發言" + ",wait-sync" * 4 + ",exit")
    real_round = cli._periodic_write
    done = {"rescued": False, "tags": [], "pending": []}

    def round_then_rescue(run_, agent):
        real_round(run_, agent)
        done["tags"].append(run_.target)
        done["pending"].append(run_.pending.name if run_.pending else None)
        if done["rescued"] or not run_.wrote:
            return
        done["rescued"] = True
        hdr = store.Index(paths()).header(ulid)
        raw = (paths().mirror / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
        body = (paths().mirror / ulid / "session.md").read_text(encoding="utf-8")
        store.stage(paths(), hdr, body, raw, update=True, kind="continue")   # the third round's entry
        shutil.rmtree(on_drive(a))                     # another machine deleted A
        subprocess.run([sys.executable, "-m", "agora.background"],             # the uploader, its own process
                       env=dict(os.environ), check=True)

    monkeypatch.setattr(cli, "_periodic_write", round_then_rescue)
    code, out, err = _continue_claude(capsys, claude_lab, a)

    assert done["rescued"]
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"exactly one rescue: {[k['id'] for k in kept]}"
    b = kept[0]["id"]
    assert code == 0, err
    assert out == b, f"the final write named {out}, the rescue is {b}"
    assert done["tags"][-1] == b, f"the quiet rounds stay on {done['tags']}, the rescue is {b}"
    assert done["pending"][-1] == f"{b.split(':')[1]}.json", \
        f"the pending record followed the rescue: {done['pending']}"
    log_lines = (paths().state / "continue.log").read_text(encoding="utf-8").splitlines()
    move_idx = next((i for i, l in enumerate(log_lines) if f"之前已另存成 {b}" in l), None)
    assert move_idx is not None, f"move note was logged in continue.log: {log_lines}"
    quiet_after = [l for l in log_lines[move_idx:] if "定期寫回：沒有新內容" in l]
    assert quiet_after, f"there are quiet rounds after rescue in continue.log: {log_lines}"
    assert all(b in l for l in quiet_after), f"quiet round prefixes followed rescue to {b}: {quiet_after}"


def test_final_write_waits_for_inflight_uploader_to_adopt_rescue(claude_lab, capsys, monkeypatch, clean_subprocesses):
    """If the agent exits while the outbox entry for run.target is in flight and the uploader
    is rescuing it into B, the final write must wait for it to settle and adopt B,
    instead of prematurely minting a duplicate rescue or returning A."""
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)
    a = _import_claude(capsys)
    ulid = a.split(":")[1]
    _script(claude_lab, monkeypatch, "append:ZZ 第 1 次發言,wait-sync,exit")
    real_round = cli._periodic_write
    done = {"staged": False}

    def round_then_delay_uploader(run_, agent):
        real_round(run_, agent)
        if done["staged"] or not run_.wrote:
            return
        done["staged"] = True
        hdr = store.Index(paths()).header(ulid)
        raw = (paths().mirror / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
        body = (paths().mirror / ulid / "session.md").read_text(encoding="utf-8")
        store.stage(paths(), hdr, body, raw, update=True, kind="continue")
        import threading
        def delayed_uploader():
            time.sleep(0.15)
            shutil.rmtree(on_drive(a), ignore_errors=True)
            subprocess.run([sys.executable, "-m", "agora.background"], env=dict(os.environ), check=True)
        t = threading.Thread(target=delayed_uploader, daemon=True)
        done["thread"] = t
        t.start()

    monkeypatch.setattr(cli, "_periodic_write", round_then_delay_uploader)
    code, out, err = _continue_claude(capsys, claude_lab, a)
    assert done["staged"]
    done["thread"].join(timeout=30.0)
    _wait_for(lambda: not store.uploader_is_running(paths()), timeout=10.0, what="uploader finished")
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"exactly one rescue: {[k['id'] for k in kept]}"
    b = kept[0]["id"]
    assert code == 0, err
    assert out == b, f"out was {out}, expected {b}"
    assert "另存" in err, err


def test_final_write_does_not_block_full_timeout_on_normal_upload_or_offline(claude_lab, capsys, monkeypatch, clean_subprocesses):
    """Review M1 / item 6: _wait_target_settled does not block for the 15s timeout:
    1. when A's upload is normal, it returns as soon as A leaves waiting_ulids;
    2. when offline (copy fails), the uploader stops and it returns quickly leaving entry in outbox.

    What is measured is the seconds the settle itself sleeps, not the whole
    run's wall clock: under load agent startup and rounds alone can exceed any
    whole-run bound, and the test was reading the scheduler instead of the
    behaviour. A full-timeout block sleeps ~15 s; a legitimate settle sleeps
    ~0 s (normal) or ~2 s (the offline fallback).
    """
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)
    settled_sleeps: list[float] = []
    real_wait = cli._wait_target_settled

    def counting_wait(paths_, run_):
        seen: list[float] = []
        real_sleep = time.sleep

        def spy(seconds):
            seen.append(seconds)
            return real_sleep(seconds)

        with mock.patch.object(time, "sleep", spy):
            real_wait(paths_, run_)
        settled_sleeps.append(sum(seen))
    monkeypatch.setattr(cli, "_wait_target_settled", counting_wait)

    # 1. Normal upload: detached uploader finishes and A leaves outbox well under 15s
    a = _import_claude(capsys)
    _script(claude_lab, monkeypatch, "append:ZZ 正常上傳,wait-sync,exit")
    code, out, err = _continue_claude(capsys, claude_lab, a)
    assert code == 0, err
    assert out == a
    assert settled_sleeps[-1] < 10.0, f"the settle slept {settled_sleeps[-1]}s on a normal upload"
    _wait_for(lambda: not store.uploader_is_running(paths()), timeout=10.0, what="part 1 uploader finished")

    # 2. Offline / copy failure: uploader fails and stops, returns quickly without hanging for 15s
    a2 = _import_claude(capsys)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    _script(claude_lab, monkeypatch, "append:ZZ 離線失敗,wait-sync,exit")
    code, out, err = _continue_claude(capsys, claude_lab, a2)
    assert out == a2
    assert settled_sleeps[-1] < 10.0, f"the settle slept {settled_sleeps[-1]}s while offline"
    assert a2.split(":")[1] in store.outbox_ulids(paths()), "unsent entry remains in outbox"
    _wait_for(lambda: not store.uploader_is_running(paths()), timeout=10.0, what="part 2 uploader finished")
    assert len(settled_sleeps) == 2, f"each part settles once: {settled_sleeps}"


def test_subprocess_mid_run_rescue_interleaving(claude_lab, capsys, monkeypatch, tmp_path, clean_subprocesses):
    """The e2e's interleaving in a subprocess: mid-run the uploader rescues A into B,
    followed by quiet rounds and the final write. Terminal output must name B."""
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    a = _import_claude(capsys)
    ulid = a.split(":")[1]
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)

    _script(claude_lab, monkeypatch,
            f"append:ZZ 第 1 句,wait-sync,append:ZZ 第 2 句,wait-sync,append:ZZ 第 3 句,wait-sync,exit")
    terminal = tmp_path / "terminal.txt"
    fd = os.open(terminal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    env = dict(os.environ)
    env["FAKE_RCLONE_DELAY"] = "0.2"
    proc = subprocess.Popen(
        [sys.executable, "-m", "agora.cli", "continue", "session", a, "--agent", "claude",
         "--dir", str(claude_lab["work"]), "--sync-period", "0.01"],
        stdout=fd, stderr=fd, stdin=subprocess.DEVNULL, env=env,
        cwd=str(tmp_path))
    # Wait for the second sentence to reach Drive
    _wait_for(lambda: on_drive(a).joinpath("session.md").is_file() and
                      "ZZ 第 2 句" in (on_drive(a) / "session.md").read_text(encoding="utf-8"),
              timeout=20.0, what="sentence 2 on drive")
    # Delete A on Drive
    shutil.rmtree(on_drive(a), ignore_errors=True)
    assert proc.wait(timeout=60) == 0
    os.close(fd)
    _wait_for(lambda: not store.uploader_is_running(paths()), timeout=10.0, what="subprocess uploader finished")
    text = terminal.read_text(encoding="utf-8")
    last_line = [l.strip() for l in text.splitlines() if l.strip().startswith("agora:")][-1]
    kept = _extra_sessions(a)
    assert len(kept) == 1, f"extra sessions: {[k['id'] for k in kept]}, text={text}"
    b = kept[0]["id"]
    assert last_line == b, f"printed {last_line}, rescue is {b}, text={text}"


# --- the staged rescue's own rules (review L1, L2) ----------------------------------

def _record(a: str, source_id: str = "ses_a") -> dict:
    """The pending record a continue of A would hold."""
    return {"agora_id": a, "agent": "opencode", "dir": "/tmp",
            "agent_session_id": source_id, "parent": {"id": a, "raw_md5": None}}


def _stage_rescue(a: str, *, new_id: str | None = None, parents: list | None = None,
                  source_id: str = "ses_a") -> str:
    """An uploader's rescue of A as it is between `stage` and `remember`: in the outbox."""
    ulid = a.split(":")[1]
    hdr = copy.deepcopy(store.Index(paths()).header(ulid))
    new = new_id or h.new_ulid()
    hdr["id"] = f"agora:{new}"
    agora = h.agora_of(hdr)
    agora["relation"] = "continue"
    agora["parents"] = [{"id": a}] if parents is None else parents
    agora["source"] = {"agent": "opencode", "session_id": source_id}
    agora.pop("raw", None)
    store.stage(paths(), hdr, "ZZ 另存的話", None)
    return new


def test_the_newest_staged_rescue_wins(env, capsys):
    """design P3: rescues of the same conversation are a chain, and the newest one is
    where the work is - the same rule the index path already applies."""
    _, a, _ = _import(capsys)
    older, newer = sorted([h.new_ulid(), h.new_ulid()])
    _stage_rescue(a, new_id=newer)      # newest first: "first found wins" has to be red too
    _stage_rescue(a, new_id=older)

    found = cli._staged_as(paths(), _record(a), a.split(":")[1])

    assert found is not None and found[0] == newer, \
        f"the newer staged rescue wins: {found and found[0]}"


def test_a_staged_rescue_of_another_target_is_not_adopted(env, capsys):
    """The three marks hold together: the same relation and source out of a *different*
    branch of that agent session is not this target's rescue."""
    _, a, _ = _import(capsys)
    _stage_rescue(a, parents=[{"id": f"agora:{h.new_ulid()}"}])

    found = cli._staged_as(paths(), _record(a), a.split(":")[1])

    assert found is None, "parents have to name the target this run is resolving"


def test_a_staged_rescue_set_aside_as_done_is_adopted(env, capsys):
    """An uploader comparing the rescue has it as `.done-<ULID>` right then - or died
    between the rename and the compare: the resolver reads it where it lies."""
    _, a, _ = _import(capsys)
    b = _stage_rescue(a)
    shutil.move(str(paths().outbox / b), str(paths().outbox / f".done-{b}"))

    found = cli._staged_as(paths(), _record(a), a.split(":")[1])

    assert found is not None and found[0] == b


def test_an_unreadable_outbox_entry_does_not_stop_the_staged_rescue(env, capsys):
    """One broken entry is not the round's end (the uploader's own rule): the resolver
    reads past it and still finds the rescue that is readable."""
    _, a, _ = _import(capsys)
    broken = paths().outbox / "01K6BADBADBADBADBADBADBADD"
    broken.mkdir(parents=True)
    (broken / "session.md").write_text("這不是一份文件", encoding="utf-8")
    b = _stage_rescue(a)

    found = cli._staged_as(paths(), _record(a), a.split(":")[1])

    assert found is not None and found[0] == b, "the readable rescue is still found"


def test_a_periodic_write_adopts_a_staged_rescue(env, capsys):
    """`check_drive=False` must not make the staged rescue invisible: the periodic
    round resolves the target the same way, so it does not read the work as A's."""
    _, a, _ = _import(capsys)
    b = _stage_rescue(a)
    run_obj = cli._Run(paths(), _record(a))

    saved, target, _note = cli._write_one(paths(), run_obj, None,
                                          cli.load_agent("opencode"), check_drive=False)

    assert saved is None, "nothing new: the adopted rescue is not written twice"
    assert target == f"agora:{b}"


def test_a_rescue_with_broken_parents_does_not_break_the_final_write(env, capsys):
    """`h.validate` does not look inside `parents`, so a header can carry a non-dict
    there: it has to read as "not this run's rescue", not as an AttributeError out of
    the final write (review L1). A is gone, so the work still gets a rescue of its own."""
    _, a, _ = _import(capsys)
    broken = _stage_rescue(a, parents=[f"agora:{h.new_ulid()}"])
    shutil.rmtree(on_drive(a))                     # A is gone from Drive
    run_obj = cli._Run(paths(), _record(a))

    saved, target, _note = cli._write_one(paths(), run_obj, None,
                                          cli.load_agent("opencode"), check_drive=True)

    assert target != f"agora:{broken}", "a malformed parents list is not adopted"
    assert saved is not None and saved[0] == target, "the work is still rescued"
    assert [p["id"] for p in h.agora_of(header_of(target))["parents"]] == [a]


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


def test_a_periodic_round_uploads_while_the_agent_is_still_running(env, capsys, monkeypatch, clean_subprocesses):
    """The e2e progress shape, detached: one periodic round over a confirmed session,
    and the new sentence is on Drive without the agent having left. No agent process
    runs here at all - that is the point: the round stages, the background sends,
    and Drive has it on its own."""
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)   # detached, like the e2e
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    assert store.upload_batch(store.Drive(paths()), paths()) == []
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_a", "parent": {"id": a, "raw_md5": "x"}}
    run_obj = cli._Run(paths(), record)

    cli._periodic_write(run_obj, cli.load_agent("opencode"))

    doc = Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions" / ulid / "session.md"
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            if "ZZ 第 1 次發言" in doc.read_text(encoding="utf-8"):
                _wait_for(lambda: not store.uploader_is_running(paths()), timeout=10.0, what="uploader finished")
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("定期寫回一次之後，Drive 上還沒有第一句")


# --- the second continue on the same session --------------------------------------

def test_a_foreground_rescue_clears_the_old_target_out_of_the_outbox(env, capsys, monkeypatch):
    """T1b / R6: the round that rescues X into Y takes X's outbox entry with it -
    otherwise the next upload meets「.update 而 Drive 沒有」again and saves a second
    session Y2 out of the same work.

    The run speaks with a session of its own (ses_b), not X's source (ses_a), so the
    uploader cannot adopt Y as X's rescue the way it does when the two match - this
    is the common first-rescue shape, and without the cleanup it mints Y2."""
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    hdr = header_of(a)
    raw = (paths().mirror / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
    body = document_of(a).split("\n---\n", 1)[1]
    store.stage(paths(), hdr, body, raw, update=True, kind="continue")   # queued .update
    import shutil
    shutil.rmtree(on_drive(a))              # another machine deletes it
    env.sessions["ses_b"] = ["另一段對話", "好"]
    record = {"agora_id": a, "agent": "opencode", "dir": "/tmp",
              "agent_session_id": "ses_b", "parent": {"id": a, "raw_md5": None}}
    run_obj = cli._Run(paths(), record)
    exported = cli.load_agent("opencode").export("ses_b")

    saved, target, _note = cli._write_one(paths(), run_obj, exported,
                                          cli.load_agent("opencode"), check_drive=True)

    assert saved is not None and target != a, "the work moved to a session of its own"
    assert [p["id"] for p in h.agora_of(header_of(target))["parents"]] == [a]
    assert h.agora_of(header_of(target))["source"]["session_id"] == "ses_b"
    assert not (paths().outbox / ulid).exists(), "the rescued entry goes with the rescue"
    for prefix in (".done-", ".old-", ".tmp-"):
        assert not (paths().outbox / f"{prefix}{ulid}").exists(), \
            f"the rescue takes {prefix}{ulid} too"
    left = store.upload_batch(store.Drive(paths()), paths())
    names = sorted(p.name for p in (Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions").iterdir())
    assert left == [] and names == [target.split(":")[1]], f"one rescue, not two: {names}"


def _locked(err: str, agora_id: str) -> None:
    """The #54 shape, as a caller reads it: the fixed line, then the explanation."""
    lines = err.splitlines()
    assert f"agora-locked {agora_id}" in lines, err
    i = lines.index(f"agora-locked {agora_id}")
    assert lines[i + 1].startswith("[agora] ") and "正在接續中" in lines[i + 1], err
    assert "要分岔請用 --fork" in lines[i + 1], err


HOLDER = ("import fcntl, sys, time; f = open(sys.argv[1]);"
          " fcntl.flock(f, fcntl.LOCK_EX); time.sleep(30)")


def _hold_lock(pending_file):
    """A process of its own holding a pending record's flock, like test_tui.py's."""
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(pending_file)])


def _release(holder):
    if holder is not None:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_second_continue_is_refused_while_the_first_still_holds_the_lock(env, capsys):
    """Scenario「同一台機器再接續一次」: the pending lock is the answer, not the file.

    #54: the refusal is its own signal - `EXIT_LOCKED` with `agora-locked <id>` first -
    and not the exit 1 of an input error.
    """
    _, a, _ = _import(capsys)
    record = {"agora_id": a, "agent": "opencode", "agent_session_id": "ses_x", "dir": "/tmp",
              "before_count": 0, "parent": {"id": a, "raw_md5": None}}
    path, lock = cli._write_pending(paths(), record)
    try:
        code, out, err = _continue(capsys, a)
        assert code == cli.EXIT_LOCKED == 4 and out == ""    # 4 is the number the caller sees
        _locked(err, a)
        assert not env.launched
    finally:
        lock.close()

    code, out, _ = _continue(capsys, a)      # nobody holds it any more: the record is stale
    assert code == 0 and out == a


def test_a_lock_held_by_another_process_is_seen_without_any_mocking(env, capsys):
    """#54 scenario with a real lock: `store.continuing` asks the record's own flock, so
    a second continue in another process is refused even though the record is only a file
    to everyone else."""
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    paths().pending.mkdir(parents=True, exist_ok=True)
    pending_file = paths().pending / f"{ulid}.json"
    pending_file.write_text("{}", encoding="utf-8")
    holder = _hold_lock(pending_file)
    try:
        _wait_for(lambda: store.continuing(paths(), ulid), what="the other process to lock it")
        code, out, err = _continue(capsys, a)
        assert code == cli.EXIT_LOCKED == 4 and out == ""    # 4 is the number the caller sees
        _locked(err, a)
        assert not env.launched, "the agent must not be opened"
    finally:
        _release(holder)


def _lock_appears_mid_preparation(env, monkeypatch, target: Path, content, holder: dict):
    """Make another run take `target`'s lock after the early check, mid-preparation.

    The window between the early check and `_write_pending` is real: the agent session
    is prepared there, and the pending record has not been written yet. The first check
    cannot see a lock that arrives in between; the one that counts is inside
    `_write_pending`. The fake agent is the seam - its `start_native` runs exactly there.
    """
    start_native = type(env).start_native

    def hook(self, raw, workdir):
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            target.write_text(content, encoding="utf-8")
        holder["proc"] = _hold_lock(target)       # a process of its own takes the lock
        _wait_for(lambda: _held_by_somebody(target), what=f"the lock on {target.name}")
        return start_native(self, raw, workdir)

    monkeypatch.setattr(type(env), "start_native", hook)


def _held_by_somebody(path: Path) -> bool:
    """Whether another process holds `path`'s flock (ours would fail)."""
    if not path.exists():
        return False
    with open(path) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def test_a_continue_that_loses_the_lock_race_exits_locked(env, capsys, monkeypatch):
    """#54: the other run got its pending record in place while this one prepared.

    The loser writes nothing: the winner's record stays as it was, and no tmp is left
    behind for the winner to trip over.
    """
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    pending_file = paths().pending / f"{ulid}.json"
    holder = {}
    _lock_appears_mid_preparation(env, monkeypatch, pending_file, "{}", holder)
    try:
        code, out, err = _continue(capsys, a)
        assert code == cli.EXIT_LOCKED == 4 and out == ""    # 4 is the number the caller sees
        _locked(err, a)
        assert pending_file.read_text(encoding="utf-8") == "{}", "the winner's record was renamed over"
        assert not list(paths().pending.glob("*.json.tmp")), "the loser leaves no tmp behind"
    finally:
        _release(holder.get("proc"))


def test_two_runs_inside_write_pending_at_once_leave_the_record_alone(env, capsys, monkeypatch):
    """#54: the narrowest window - the other run is inside `_write_pending` itself.

    Its record is under `<ULID>.json.tmp`, about to be renamed into place; the lock on
    that name is the only thing saying so, and opening it must not truncate the record
    that is on its way.
    """
    _, a, _ = _import(capsys)
    ulid = a.split(":")[1]
    tmp_file = paths().pending / f"{ulid}.json.tmp"
    other = json.dumps({"agora_id": "agora:01THEOTHER0000000000000000"}, ensure_ascii=False)
    holder = {}
    _lock_appears_mid_preparation(env, monkeypatch, tmp_file, other, holder)
    try:
        code, out, err = _continue(capsys, a)
        assert code == cli.EXIT_LOCKED == 4 and out == ""    # 4 is the number the caller sees
        _locked(err, a)
        assert tmp_file.read_text(encoding="utf-8") == other, "the record being written was truncated"
    finally:
        _release(holder.get("proc"))


def test_a_plain_input_error_is_still_exit_1(env, capsys):
    """#54 changes one refusal only: "cannot be done" keeps the exit code it had."""
    code, out, err = _continue(capsys, "agora:01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert code == cli.EXIT_INPUT and out == ""
    assert "agora-locked" not in err


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
