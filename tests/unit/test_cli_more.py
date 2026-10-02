"""Command-layer gaps from code-pm section 2.

Two harnesses: an in-process FakeAgent (argv ["true"]) for logic, and the
real claude adapter plus a script agent in real subprocesses for anything
involving signals, death or races (U-CON-08/10/11/11b/16/17).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C
from agora.agents.base import Exported, Launch, format_reading

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fakes" / "fake_claude.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
CL_SID = "11111111-2222-4333-8444-555555555555"


class FakeAgent:
    """Stores sessions in memory; launching 'talks' by appending a message."""

    name = "opencode"

    def __init__(self):
        self.dir = "/tmp"
        self.sessions: dict[str, list[str]] = {"ses_a": ["把 CSV 轉成 Markdown 表格", "好的，三個步驟"]}
        self.launched: list[Launch] = []
        self.speak = True

    def _exported(self, sid):
        msgs = self.sessions[sid]
        return Exported(session_id=sid, raw=json.dumps({"id": sid, "m": msgs}, ensure_ascii=False).encode(),
                        dir=self.dir, title=msgs[0][:20], created_at="2026-10-01T00:00:00Z",
                        agent_version="9.9", message_count=len(msgs))

    def export(self, sid):
        if sid not in self.sessions:
            from agora.agents.base import AgentError
            raise AgentError(f"沒有這個 session：{sid}")
        return self._exported(sid)

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def native(self, turns):
        msgs = [lines[0] for _role, lines in turns if lines]
        return json.dumps({"id": "native", "m": msgs}, ensure_ascii=False).encode()

    def summarize(self, prompt, workdir):
        return "合併要約", "fake-model"

    def start_native(self, raw, workdir):
        new = f"ses_n{len(self.sessions)}"
        self.sessions[new] = list(json.loads(raw)["m"])
        launch = Launch(argv=["true"], cwd=str(workdir), agent_session_id=new, before_count=len(self.sessions[new]))
        self.launched.append(launch)
        return launch

    def collect(self, launch):
        if self.speak:
            self.sessions[launch.agent_session_id].append("接著做完了")
        exported = self._exported(launch.agent_session_id)
        return exported if exported.message_count > launch.before_count else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    agent = FakeAgent()
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    return agent


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def paths():
    return store.Paths.from_env()


def remote_sessions(tmp_path):
    base = tmp_path / "remote" / "agora" / "sessions"
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


# --- claude-backed harness for subprocess tests --------------------------------

AGENT_SCRIPT = textwrap.dedent("""
    import datetime, json, os, sys, time, uuid
    from agora.agents.claude import encode_project_dir
    sid = sys.argv[sys.argv.index("--resume") + 1]
    mode = os.environ.get("SCRIPT_MODE", "append")
    marker = os.environ.get("SCRIPT_MARKER", "")
    home = os.environ["AGORA_CLAUDE_HOME"]
    def path():
        return os.path.join(home, ".claude", "projects",
                            encode_project_dir(os.getcwd()), sid + ".jsonl")
    def note(text):
        if marker:
            with open(marker, "a") as f:
                f.write(text + "\\n")
    def append(text):
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        user = {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
                "parentUuid": None, "timestamp": now, "cwd": os.getcwd(),
                "message": {"role": "user", "content": text}}
        assistant = {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
                     "parentUuid": user["uuid"], "timestamp": now, "cwd": os.getcwd(),
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "ZZAGENT 收到"}]}}
        with open(path(), "a") as f:
            f.write(json.dumps(user, ensure_ascii=False) + "\\n")
            f.write(json.dumps(assistant, ensure_ascii=False) + "\\n")
    if mode == "record-cwd":
        note(os.getcwd())
    elif mode == "check-pending":
        import fcntl
        state = os.environ["AGORA_STATE_DIR"]
        found = locked = None
        for p in sorted(os.listdir(os.path.join(state, "pending"))):
            if not p.endswith(".json") or p.startswith("."):
                continue
            try:
                rec = json.load(open(os.path.join(state, "pending", p)))
            except (OSError, ValueError):
                continue
            if rec.get("agent_session_id") == sid:
                found = sorted(rec)
                try:
                    with open(os.path.join(state, "pending", p)) as f:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = False
                except BlockingIOError:
                    locked = True
        note(f"found={found} locked={locked}")
        append("ZZAGENT 續寫一句")
    else:
        append("ZZAGENT 續寫一句")
        note(f"pid={os.getpid()}")
        if mode in ("append-sleep-ignore", "append-sleep"):
            if mode == "append-sleep-ignore":
                import signal
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            time.sleep(120)
""")


@pytest.fixture
def claude_env(tmp_path, monkeypatch):
    """Real claude adapter over an isolated AGORA_CLAUDE_HOME + fixture jsonl."""
    home = tmp_path / "chome"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(home))
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    script = tmp_path / "agent_entry.sh"
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT_SCRIPT)
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {agent} \"$@\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(script))
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(fake_home))
    return {"home": home, "marker": fake_home / "marker.log"}


def lay_fixture(home: Path, workdir: Path, sid: str = CL_SID) -> Path:
    subdir = home / ".claude" / "projects" / C.encode_project_dir(workdir)
    subdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", subdir / f"{sid}.jsonl")
    return subdir / f"{sid}.jsonl"


def wait_marker(marker: Path, timeout: float = 60.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        if marker.exists() and marker.read_text():
            return marker.read_text()
        time.sleep(0.5)
    raise TimeoutError(f"agent did not start: {marker}")


def wait_gone(pid: int, timeout: float = 30.0) -> None:
    """Wait until the pid is really dead (it may still hold the pending lock)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    pytest.fail(f"agent {pid} did not exit")


def cli_child(*argv: str, extra_env: dict | None = None):
    env = {**os.environ, **(extra_env or {})}
    return subprocess.Popen([sys.executable, "-m", "agora.cli", *argv],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=True)


# --- U-HDR-08 -------------------------------------------------------------------

def test_import_source_fields(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    home = tmp_path / "chome"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(home))
    subdir = home / ".claude" / "projects" / "-tmp-my-proj-v2"
    subdir.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", subdir / f"{CL_SID}.jsonl")
    code, out, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0
    hdr = store.Index(paths()).header(out.split(":")[1])
    assert h.agora_of(hdr)["header"] == 2
    assert h.agora_of(hdr)["source"]["dir"] == "/tmp/my-proj.v2"
    assert h.agora_of(hdr)["source"]["host"]
    assert h.agora_of(hdr)["source"]["agent_version"] == "2.1.286"
    assert h.agora_of(hdr)["source"]["created_at"] == "2026-10-02T01:00:00.000Z"
    assert h.agora_of(hdr)["created_at"] != h.agora_of(hdr)["source"]["created_at"]


# --- U-IMP -----------------------------------------------------------------------

def test_reimport_only_merged_child_branches(env, capsys):  # U-IMP-08b
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    env.sessions["ses_a"].append("來源端又改了")
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert c != a
    from agora import header as h
    assert h.agora_of(store.Index(paths()).header(c.split(":")[1]))["parents"][0]["id"] == a


def test_reimport_with_ref_only_updates_in_place(env, capsys):  # C8
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_c"] = ["參照別人的內容"]
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_c",
                   "--agent", "opencode", "--header", f"refs={a}")
    env.sessions["ses_a"].append("來源端又改了")
    _, a2, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert a2 == a


def test_import_same_source_from_cold_cache(env, capsys, tmp_path, monkeypatch):  # U-IMP-09
    _, first, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    other = tmp_path / "m2"
    monkeypatch.setenv("AGORA_CONFIG", str(other / "config"))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(other / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(other / "state"))
    _, second, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert second == first


def test_search_same_source_shows_newest_with_warning(env, capsys):  # U-IMP-10, C10
    from agora import header as h
    base = {"type": "Session", "title": "重複來源", "refs": [], "case": None, "tags": [],
            "agora": {"header": 2, "created_at": "2026-10-01T00:00:00Z", "relation": "import",
                      "parents": [],
                      "source": {"agent": "opencode", "session_id": "ses_dup",
                                 "created_at": "2026-10-01T00:00:00Z"}}}
    old = dict(base, id=f"agora:{h.new_ulid()}")
    old["agora"] = dict(base["agora"], updated_at="2026-10-02T00:00:00Z")
    new = dict(base, id=f"agora:{h.new_ulid()}")
    new["agora"] = dict(base["agora"], updated_at="2026-10-03T00:00:00Z")
    body = "## user\n重複來源表格\n"
    for hdr in (old, new):
        store.push_one(store.Drive(paths()), store.stage(paths(), hdr, body, b"{}"))
    store.sync(paths())
    code, found, err = run(capsys, "search", "session", "--filter", "text~=重複來源", "--no-sync")
    assert code == 0
    assert found.split()[0] == new["id"]
    assert "同一個來源" in err


# --- U-MRG ------------------------------------------------------------------------

def test_merge_of_merge(env, capsys):  # U-MRG-02b, N5
    from agora import header as h
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    env.sessions["ses_c"] = ["第三份", "好"]
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_c", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    _, n, _ = run(capsys, "merge", "session", m, c, "--agent", "opencode")
    parents = h.agora_of(store.Index(paths()).header(n.split(":")[1]))["parents"]
    assert [p["id"] for p in parents] == [m, c]
    assert parents[0]["raw_md5"] is None
    assert parents[1]["raw_md5"] is not None


def test_merge_missing_id_fails_clean(env, capsys, tmp_path):  # U-MRG-03
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    before = remote_sessions(tmp_path)
    code, _, err = run(capsys, "merge", "session", a, "agora:01K6NADA0000000000000000", "--agent", "opencode")
    assert code != 0 and "找不到" in err
    assert remote_sessions(tmp_path) == before
    assert store.outbox_count(paths()) == 0


# --- U-CON (in-process) -------------------------------------------------------------

def test_dir_defaults_and_relative_resolution(env, capsys, tmp_path, monkeypatch):  # U-CON-06, H1
    work = tmp_path / "proj"
    work.mkdir()
    env.dir = str(work)
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    run(capsys, "continue", "session", parent, "--agent", "opencode")
    assert env.launched[-1].cwd == str(work)  # source.dir exists: use it
    env.dir = "/nonexistent-agora-xyz"
    _, parent2, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    monkeypatch.chdir(tmp_path)
    run(capsys, "continue", "session", parent2, "--agent", "opencode", "--dir", "proj")
    assert env.launched[-1].cwd == str(work.resolve())  # C5: relative -> absolute


def test_continue_from_merge_has_null_parent_md5(env, capsys):  # U-CON-18, N5
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    _, child, _ = run(capsys, "continue", "session", m, "--agent", "opencode", "--dir", "/tmp")
    hdr = store.Index(paths()).header(child.split(":")[1])
    from agora import header as h
    assert h.agora_of(hdr)["parents"] == [{"id": m, "raw_md5": None}]


def test_continue_source_is_the_new_session(env, capsys, tmp_path):  # U-CON-19, N6
    work = tmp_path / "proj"
    work.mkdir()
    env.dir = str(work)  # a truthful adapter reports its own directory
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    _, child, _ = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", str(work))
    hdr = store.Index(paths()).header(child.split(":")[1])
    from agora import header as h
    assert h.agora_of(hdr)["source"]["session_id"] == env.launched[-1].agent_session_id
    assert h.agora_of(hdr)["source"]["dir"] == str(work)


def test_continue_without_raw_fails(env, capsys, tmp_path):  # U-CON-14, L2/S1
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    ulid = parent.split(":")[1]
    raws = list((tmp_path / "remote" / "agora" / "sessions" / ulid).glob("raw-*.json"))
    assert len(raws) == 1
    raws[0].write_bytes(b"tampered")
    code, _, _ = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "/tmp")
    assert code != 0


def test_finalize_upload_failure_keeps_outbox_clears_pending(env, capsys, monkeypatch):  # U-CON-13
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    run(capsys, "search", "session", "--filter", "text~=CSV")   # sync first, so only the finalize upload fails
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    code, out, _ = run(capsys, "continue", "session", m, "--agent", "opencode", "--dir", "/tmp")
    assert code == cli.EXIT_IN_OUTBOX
    assert out.startswith("agora:")
    assert store.outbox_count(paths()) == 1
    assert not list(paths().pending.glob("*.json"))


# --- U-CON (real subprocesses) -------------------------------------------------------

def test_pending_visible_and_locked_at_agent_start(env, capsys, tmp_path, claude_env, monkeypatch):  # U-CON-08
    work = tmp_path / "proj"
    work.mkdir()
    lay_fixture(claude_env["home"], work)
    monkeypatch.setenv("SCRIPT_MODE", "check-pending")
    monkeypatch.setenv("SCRIPT_MARKER", str(claude_env["marker"]))
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    code, child, _ = run(capsys, "continue", "session", parent, "--agent", "claude", "--dir", str(work))
    assert code == 0 and child.startswith("agora:")
    marker = claude_env["marker"].read_text()
    assert "locked=True" in marker
    for key in ("agora_id", "agent", "agent_session_id", "dir", "parent"):
        assert key in marker


def test_fault_before_finalize_recovers(env, capsys, tmp_path, claude_env, monkeypatch):  # U-CON-10
    work = tmp_path / "proj"
    work.mkdir()
    lay_fixture(claude_env["home"], work)
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    monkeypatch.setenv("SCRIPT_MODE", "append")
    proc = cli_child("continue", "session", parent, "--agent", "claude", "--dir", str(work),
                     extra_env={"AGORA_TEST_FAULT": "before-finalize"})
    assert proc.wait(timeout=120) == 137
    assert len(list(paths().pending.glob("*.json"))) == 1
    code, _, err = run(capsys, "show", "session", parent)
    assert code == 0 and "補存" in err
    assert not list(paths().pending.glob("*.json"))
    kids = store.Index(paths()).children(parent.split(":")[1])
    assert len(kids) == 1


def test_sigint_kills_agent_not_agora(env, capsys, tmp_path, claude_env, monkeypatch):  # U-CON-11
    work = tmp_path / "proj"
    work.mkdir()
    lay_fixture(claude_env["home"], work)
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    monkeypatch.setenv("SCRIPT_MODE", "append-sleep-ignore")
    monkeypatch.setenv("SCRIPT_MARKER", str(claude_env["marker"]))
    proc = cli_child("continue", "session", parent, "--agent", "claude", "--dir", str(work))
    pid = int(wait_marker(claude_env["marker"]).split("pid=")[1].split()[0])
    time.sleep(1)
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(2)
    assert proc.poll() is None  # agora ignored it
    os.kill(pid, signal.SIGTERM)  # let the agent finish
    out, _ = proc.communicate(timeout=120)
    assert proc.returncode == 0 and out.strip().split()[0].startswith("agora:")


def test_agent_dies_fast_on_sigint(env, capsys, tmp_path, claude_env, monkeypatch):  # U-CON-11b, N1
    work = tmp_path / "proj"
    work.mkdir()
    lay_fixture(claude_env["home"], work)
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    monkeypatch.setenv("SCRIPT_MODE", "append-sleep")
    monkeypatch.setenv("SCRIPT_MARKER", str(claude_env["marker"]))
    proc = cli_child("continue", "session", parent, "--agent", "claude", "--dir", str(work))
    pid = int(wait_marker(claude_env["marker"]).split("pid=")[1].split()[0])
    time.sleep(1)
    os.killpg(proc.pid, signal.SIGINT)
    end = time.time() + 15
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.5)
    else:
        pytest.fail("agent did not die on SIGINT")
    out, _ = proc.communicate(timeout=120)
    assert proc.returncode == 0 and out.strip().split()[0].startswith("agora:")


def test_killed_agora_does_not_finish_early(env, capsys, tmp_path, claude_env, monkeypatch):  # U-CON-16, C1
    work = tmp_path / "proj"
    work.mkdir()
    lay_fixture(claude_env["home"], work)
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    monkeypatch.setenv("SCRIPT_MODE", "append-sleep-ignore")
    monkeypatch.setenv("SCRIPT_MARKER", str(claude_env["marker"]))
    proc = cli_child("continue", "session", parent, "--agent", "claude", "--dir", str(work))
    pid = int(wait_marker(claude_env["marker"]).split("pid=")[1].split()[0])
    proc.kill()  # agora dies, the agent lives on
    assert proc.wait(timeout=60) == -9
    code, _, _ = run(capsys, "show", "session", parent)
    assert code == 0
    assert len(list(paths().pending.glob("*.json"))) == 1  # still protected
    assert store.Index(paths()).children(parent.split(":")[1]) == []
    os.kill(pid, signal.SIGTERM)
    wait_gone(pid)          # the lock frees only once the agent is really gone
    run(capsys, "show", "session", parent)
    assert store.Index(paths()).children(parent.split(":")[1]) != []
    assert not list(paths().pending.glob("*.json"))


def test_concurrent_recovery_makes_one_session(env, capsys, tmp_path, claude_env):  # U-CON-17, C3
    work = tmp_path / "proj"
    work.mkdir()
    lay_fixture(claude_env["home"], work)
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    record = {"agora_id": f"agora:{h.new_ulid()}", "agent": "claude",
              "agent_session_id": CL_SID, "dir": str(work), "before_count": 0,
              "parent": {"id": parent, "raw_md5": None}, "started_at": "2026-10-02T00:00:00Z"}
    _, lock = cli._write_pending(paths(), record)
    lock.close()  # expired: nobody holds it
    procs = [cli_child("show", "session", parent), cli_child("show", "session", parent)]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    assert (tmp_path / "remote" / "agora" / "sessions" / record["agora_id"].split(":")[1]).is_dir()
    assert not list(paths().pending.glob("*.json"))


# --- U-SHW -----------------------------------------------------------------------------

def test_show_format_and_missing(env, capsys):  # U-SHW-01, U-SHW-02
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                   "--agent", "opencode", "--header", "title=規劃表格")
    code, out, _ = run(capsys, "show", "session", a)
    assert code == 0 and "規劃表格" in out and "type: Session" in out
    assert "把 CSV 轉成 Markdown 表格" in out
    code, _, err = run(capsys, "show", "session", "agora:01K6NADA0000000000000000")
    assert code != 0 and "找不到" in err


def test_show_raw_lazy_and_merge_message(env, capsys):  # U-SHW-03
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    code, out, _ = run(capsys, "show", "session", a, "--raw")
    assert code == 0 and '"m": ["把 CSV 轉成 Markdown 表格"' in out
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    code, _, err = run(capsys, "show", "session", m, "--raw")
    assert code != 0 and "agora.parents" in err


# --- U-SRC -------------------------------------------------------------------------------

def test_search_output_format(env, capsys):  # U-SRC-01
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    code, found, _ = run(capsys, "search", "session", "--filter", "text~=CSV", "--no-sync")
    assert code == 0
    parts = found.split()
    assert parts[0] == a and parts[1] == "2026-10-01" and parts[2] == "opencode"


def test_search_special_keywords_no_crash(env, capsys):  # U-SRC-07
    _, _, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    for kw in ["C++", 'a"b', "AND", "NEAR", "*", "表格 OR"]:
        code, _, _ = run(capsys, "search", "session", "--filter", f"text~={kw}", "--no-sync")
        assert code == 0, kw


def test_search_keyword_starting_with_dash(env, capsys):
    _, _, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    code, _, _ = run(capsys, "search", "session", "--filter", "text~=-x", "--no-sync")
    assert code == 0


def test_search_all_aliases(env, capsys, tmp_path, monkeypatch):  # U-SRC-09
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER if name == "claude" else env)
    home = tmp_path / "chome"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(home))
    subdir = home / ".claude" / "projects" / "-tmp-my-proj-v2"
    subdir.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", subdir / f"{CL_SID}.jsonl")
    _, op_id, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode",
                       "--header", "title=規劃", "--header", "case=mybrain:案件/x",
                       "--header", "tags=[csv]", "--header", "refs=mybrain:a.md")
    _, claude_id, _ = run(capsys, "import", "session", "--external-session-id", CL_SID,
                          "--agent", "claude")
    for filt, want in [("agent=claude", {claude_id}),
                       ("agora.relation=import", {op_id, claude_id}),
                       ("case=mybrain:案件/x", {op_id}),
                       ("tags=csv", {op_id}),
                       ("refs=mybrain:a.md", {op_id}),
                       ("title=規劃", {op_id}),
                       ("agora.relation=merge", set())]:
        code, found, _ = run(capsys, "search", "session", "--filter", filt, "--no-sync")
        assert code == 0, filt
        assert {line.split()[0] for line in found.splitlines() if line.split()} == want, filt


def test_index_rebuild_after_delete(env, capsys, monkeypatch):  # U-SRC-10
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    _, before, _ = run(capsys, "search", "session", "--filter", "text~=CSV", "--no-sync")
    (paths().cache / "index.sqlite").unlink()
    monkeypatch.setenv("AGORA_NOW", str(2000000000))  # past the sync throttle
    _, after, _ = run(capsys, "search", "session", "--filter", "text~=CSV")
    assert after == before and after.split()[0] == a


def test_search_ascii_matches_fullwidth(env, capsys):  # U-SRC-12, T2
    env.sessions["ses_u"] = ["ＵＩ介面表格測試"]
    _, _, _ = run(capsys, "import", "session", "--external-session-id", "ses_u", "--agent", "opencode")
    _, found, _ = run(capsys, "search", "session", "--filter", "text~=ui", "--no-sync")
    assert found


def test_merge_row_display(env, capsys):  # U-SRC-13, N6
    from datetime import datetime, timezone
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    _, found, _ = run(capsys, "search", "session", "--filter", "text~=CSV", "--no-sync")
    line = next(line for line in found.splitlines() if line.split()[0] == m)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert line.split()[1] == today and line.split()[2] == "merge"


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(__import__("pytest").main([__file__, "-q"]))

