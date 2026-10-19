"""`agora continue`: an agent that dies must not look like a clean write-back (#76).

The bug: `_wait_agent` only `proc.wait()`ed and never read `returncode`, so the
exit code came from the write-back alone - a dead agent with nothing new looked
like exit 0. The contract (same shape as #54's `agora-locked`): the write-back
still happens, but any death (`returncode != 0`, except 130/143, -2/-15, and a
SIGTERM agora itself forwarded) prints a fixed stderr line
`agora-agent-failed rc=<code>`; the exit is the fixed `EXIT_AGENT_FAILED` (5) when
the write-back succeeded, otherwise the write-back's own failure code (3).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from agora import cli, store
from agora.agents import claude as C
from agora.agents.base import Exported, Launch
from _round_seam import in_process_round_child, in_process_start_child

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
CL_SID = "11111111-2222-4333-8444-555555555555"
HEADLESS_SYNC = "0.005"


class ExitAgent:
    """In-memory sessions; the launch is `["true"]` so tests can swap its exit."""

    name = "opencode"

    def __init__(self):
        self.sessions: dict[str, list[str]] = {"ses_a": ["把 CSV 轉成 Markdown 表格", "好的，三個步驟"]}
        self.launched: list[Launch] = []
        self.speak = True

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

    def summarize(self, prompt, workdir):
        return json.dumps({"purpose": "合併要約", "decisions": [], "progress": "進行中",
                           "open_questions": []}, ensure_ascii=False), "fake-model"

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


def _env_fixture(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    agent = ExitAgent()
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    monkeypatch.setattr(cli, "_round_child", in_process_round_child)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    return agent


try:
    import pytest

    @pytest.fixture
    def env(tmp_path, monkeypatch):
        return _env_fixture(tmp_path, monkeypatch)
except ImportError:  # pragma: no cover
    pass


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def _die_with(monkeypatch, argv):
    """Run `argv` instead of the fake's `["true"]`, keeping everything else."""
    real = cli.subprocess.Popen

    def spy(call_argv, **kwargs):
        if call_argv == ["true"]:
            call_argv = argv
        return real(call_argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", spy)


def _fail_with(monkeypatch, code: int):
    _die_with(monkeypatch, [sys.executable, "-c", f"import sys; sys.exit({code})"])


def test_agent_fails_with_no_new_content_reports_fixed_code(env, capsys, monkeypatch):
    """#76: the agent dies (exit 3) before saying anything - the old code said 0."""
    env.speak = False
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 3)
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0")
    assert code == cli.EXIT_AGENT_FAILED == 5, err
    assert "agora-agent-failed rc=3" in err
    assert "沒有新內容" in err


def test_agent_fails_but_new_content_is_still_written_back(env, capsys, monkeypatch):
    """#76: what the agent said before dying is not lost; the exit is the fixed 5."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 3)
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0")
    assert code == cli.EXIT_AGENT_FAILED == 5, err
    assert "agora-agent-failed rc=3" in err
    assert out == parent
    code, shown, _ = run(capsys, "show", "session", parent)
    assert code == 0
    assert "接著做完了" in shown


def test_agent_exit_zero_keeps_old_behavior(env, capsys, monkeypatch):
    """#76: a clean agent changes nothing - exit 0 and no fixed line."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0")
    assert code == 0, err
    assert "agora-agent-failed" not in err
    assert out == parent


def test_ctrl_c_convention_codes_are_not_failures(env, capsys, monkeypatch):
    """#76: 130 (Ctrl-C) and 143 (SIGTERM) are a goodbye, not a crash."""
    for conventional in (130, 143):
        _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                           "--agent", "opencode")
        _fail_with(monkeypatch, conventional)
        code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                           "--dir", "/tmp", "--sync-period", "0")
        assert code == 0, (conventional, err)
        assert "agora-agent-failed" not in err, conventional


def test_sigterm_death_is_not_a_failure(env, capsys, monkeypatch):
    """#76: killed by SIGTERM (-15) stays 0 - the headless gentle stop depends on it."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _die_with(monkeypatch, ["sh", "-c", "kill -TERM $$"])
    code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")
    assert code == 0, err
    assert "agora-agent-failed" not in err


def test_sigint_death_is_not_a_failure(env, capsys, monkeypatch):
    """#76: killed by SIGINT (-2) stays 0 - Ctrl-C belongs to the agent (N1)."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _die_with(monkeypatch, ["sh", "-c", "kill -INT $$"])
    code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")
    assert code == 0, err
    assert "agora-agent-failed" not in err


def test_sigkill_and_sigsegv_are_failures(env, capsys, monkeypatch):
    """#76 follow-up: -9 (SIGKILL/OOM) and -11 must not be swallowed either."""
    for sig, flag in (("KILL", "-9"), ("SEGV", "-11")):
        _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                           "--agent", "opencode")
        _die_with(monkeypatch, ["sh", "-c", f"kill -{sig} $$"])
        code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                           "--dir", "/tmp", "--sync-period", "0")
        assert code == cli.EXIT_AGENT_FAILED == 5, (sig, err)
        assert f"agora-agent-failed rc={flag}" in err, sig


def test_agent_failure_plus_upload_failure_keeps_exit_3_with_signal(
        env, capsys, monkeypatch):
    """#76: the write-back's own failure (3) wins over the agent's code, but the line stays."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 7)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")
    assert code == cli.EXIT_IN_OUTBOX == 3, err
    assert "agora-agent-failed rc=7" in err


def test_final_write_exception_still_prints_signal(env, capsys, monkeypatch):
    """#76 Minor3: `_final_write` raises (main → exit 2), the death is still said."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 7)
    def _boom(*args, **kwargs):
        raise store.StoreError("boom")
    monkeypatch.setattr(cli, "_final_write", _boom)
    code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")
    assert code == cli.EXIT_ERROR == 2, err
    assert "agora-agent-failed rc=7" in err


def test_fork_agent_failure_reports_5(env, capsys, monkeypatch):
    """#76 Minor2: `--fork` + dead agent - B stays, the exit is the fixed 5."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    before_parent = store.Index(store.Paths.from_env()).header(parent.split(":")[1])
    _fail_with(monkeypatch, 7)
    code, child, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                           "--fork", "--dir", "/tmp", "--sync-period", "0")
    assert code == cli.EXIT_AGENT_FAILED == 5, err
    assert "agora-agent-failed rc=7" in err
    assert child != parent and child.startswith("agora:")
    code, shown, _ = run(capsys, "show", "session", child)
    assert code == 0
    assert "接著做完了" in shown
    after_parent = store.Index(store.Paths.from_env()).header(parent.split(":")[1])
    assert after_parent == before_parent


def test_no_new_at_final_but_wrote_before_reports_5(env, capsys, monkeypatch):
    """#76 Minor2: periodic wrote, final has nothing new - the `run.wrote` branch."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    orig_collect = env.collect
    calls = {"n": 0}

    def collect_once(launch):
        calls["n"] += 1
        if calls["n"] >= 2:
            env.speak = False  # nothing beyond what the periodic round already wrote
        return orig_collect(launch)

    monkeypatch.setattr(env, "collect", collect_once)
    _die_with(monkeypatch, ["sh", "-c", "sleep 2; exit 7"])
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0.005")
    assert calls["n"] >= 2, "no periodic round happened; the branch was not hit"
    assert code == cli.EXIT_AGENT_FAILED == 5, err
    assert "agora-agent-failed rc=7" in err
    assert out == parent


# --- Major1: headless + term_forwarded, in real subprocesses -------------------

AGENT_FAIL_FAST = """\
import datetime, json, os, sys, uuid
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Fake Claude)")
    sys.exit(0)
sid = args[args.index("--resume") + 1]
from agora.agents.claude import encode_project_dir
home = os.environ["AGORA_CLAUDE_HOME"]
path = os.path.join(home, ".claude", "projects",
                    encode_project_dir(os.getcwd()), sid + ".jsonl")
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
user = {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
        "parentUuid": None, "timestamp": now, "cwd": os.getcwd(),
        "message": {"role": "user", "content": "ZZHL 死前說了一句"}}
assistant = {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
             "parentUuid": user["uuid"], "timestamp": now, "cwd": os.getcwd(),
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "ZZHL 收到"}]}}
with open(path, "a") as f:
    f.write(json.dumps(user, ensure_ascii=False) + "\\n")
    f.write(json.dumps(assistant, ensure_ascii=False) + "\\n")
sys.exit(7)
"""

AGENT_TERM_EXIT_1 = """\
import datetime, json, os, signal, sys, time, uuid
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Fake Claude)")
    sys.exit(0)
sid = args[args.index("--resume") + 1]
from agora.agents.claude import encode_project_dir
home = os.environ["AGORA_CLAUDE_HOME"]
path = os.path.join(home, ".claude", "projects",
                    encode_project_dir(os.getcwd()), sid + ".jsonl")
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
user = {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
        "parentUuid": None, "timestamp": now, "cwd": os.getcwd(),
        "message": {"role": "user", "content": "ZZHL 轉送前說了一句"}}
assistant = {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
             "parentUuid": user["uuid"], "timestamp": now, "cwd": os.getcwd(),
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "ZZHL 收到"}]}}
with open(path, "a") as f:
    f.write(json.dumps(user, ensure_ascii=False) + "\\n")
    f.write(json.dumps(assistant, ensure_ascii=False) + "\\n")
print("AGENT-READY", flush=True)
def _on_term(signum, frame):
    sys.exit(1)
signal.signal(signal.SIGTERM, _on_term)
while True:
    time.sleep(0.1)
"""


def _headless_lab(tmp_path, monkeypatch, script: str):
    """Real claude adapter over a scripted `claude` that dies however we ask."""
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    rclone = tmp_path / "rclone"
    rclone.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    rclone.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(rclone))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    work = tmp_path / "proj"
    work.mkdir(exist_ok=True)
    proj = home / ".claude" / "projects" / C.encode_project_dir(work)
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    agent_py = tmp_path / "agent_under_test.py"
    agent_py.write_text(script)
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {agent_py} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(wrapper))
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    return {"home": home, "work": work}


class _Headless:
    """One headless continue in a subprocess, stdio parked in files (not pipes)."""

    def __init__(self, lab, tmp_path, agora_id):
        self.out_path = tmp_path / "headless.out"
        self.err_path = tmp_path / "headless.err"
        self._out = open(self.out_path, "wb")
        self._err = open(self.err_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", agora_id,
             "--agent", "claude", "--dir", str(lab["work"]), "--headless",
             "--sync-period", HEADLESS_SYNC],
            stdout=self._out, stderr=self._err, stdin=subprocess.DEVNULL,
            env=dict(os.environ), cwd=str(tmp_path), text=True,
        )

    def stderr(self) -> str:
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def stdout(self) -> str:
        return self.out_path.read_text(encoding="utf-8", errors="replace")

    def wait_for_stderr(self, prefix, timeout=30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.stderr().splitlines():
                if line.startswith(prefix):
                    return line
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(f"never saw stderr {prefix!r}; got {self.stderr()!r}")

    def wait_for_stdout(self, needle, timeout=30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.stdout():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(f"never saw stdout {needle!r}; got {self.stdout()!r}")

    def terminate(self, timeout=60.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.wait(timeout)

    def wait(self, timeout=60.0) -> int:
        code = self.proc.wait(timeout=timeout)
        self._out.close()
        self._err.close()
        return code


def test_headless_agent_dies_before_sigterm_reports_5(tmp_path, monkeypatch, capsys):
    """Major1(a): headless, agent already dead (rc=7) when sidecar would signal."""
    lab = _headless_lab(tmp_path, monkeypatch, AGENT_FAIL_FAST)
    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    headless = _Headless(lab, tmp_path, a)
    assert headless.wait(timeout=60) == cli.EXIT_AGENT_FAILED == 5
    assert "agora-agent-failed rc=7" in headless.stderr()
    code, shown, err = run(capsys, "show", "session", a)
    assert code == 0, err
    assert "ZZHL 死前說了一句" in shown


def test_headless_forwarded_sigterm_exit_1_stays_0(tmp_path, monkeypatch, capsys):
    """Major1(b): SIGTERM forwarded, agent ends 1 (non-conventional) - still 0, no line."""
    lab = _headless_lab(tmp_path, monkeypatch, AGENT_TERM_EXIT_1)
    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    headless = _Headless(lab, tmp_path, a)
    assert headless.wait_for_stderr("agora-session ") == f"agora-session {a}"
    headless.wait_for_stdout("AGENT-READY")
    assert headless.terminate() == 0
    assert "agora-agent-failed" not in headless.stderr()
    code, shown, err = run(capsys, "show", "session", a)
    assert code == 0, err
    assert "ZZHL 轉送前說了一句" in shown


def test_forward_sigterm_only_to_live_child():
    """Major1: the poll() guard - a dead child gets nothing, so a late SIGTERM
    cannot whitewash an earlier death into a gentle stop."""

    class _Live:
        def __init__(self):
            self.sent = []
        def poll(self):
            return None
        def send_signal(self, sig):
            self.sent.append(sig)

    class _Zombie:
        """Exited (poll() set) but send would still succeed - without the guard
        this looks forwarded."""
        def __init__(self):
            self.sent = []
        def poll(self):
            return 7
        def send_signal(self, sig):
            self.sent.append(sig)

    live = _Live()
    assert cli._forward_sigterm(live) is True
    assert live.sent == [signal.SIGTERM]

    zombie = _Zombie()
    assert cli._forward_sigterm(zombie) is False
    assert zombie.sent == []
