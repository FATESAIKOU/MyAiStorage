"""Headless `agora continue` (change sidecar-v0, task 3.3).

The contract the sidecar reads: the fixed stderr lines (`agora-session`, and
`agora-port` for opencode), stdout left clean for the agent, --add-tag as an
append, and a SIGTERM that stops the agent gently, writes the last
conversation back and exits 0.  Fake agents only: no real claude/opencode, no
Drive (fake rclone), isolated HOME/state from conftest.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agora import cli, store
from agora.agents import claude as C
from agora.agents import opencode as OC

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE_RCLONE = FAKES / "fake_rclone.py"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_OPENCODE = FAKES / "fake_opencode.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
OC_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
CL_SID = "11111111-2222-4333-8444-555555555555"
SECONDS = "0.005"


def _wrapper(tmp_path, name, target):
    w = tmp_path / name
    w.write_text(f"#!/bin/sh\nexec {sys.executable} {target} \"$@\"\n")
    w.chmod(0o755)
    return w


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def paths() -> store.Paths:
    return store.Paths.from_env()


def header_of(agora_id: str) -> dict:
    index = store.sync(paths(), throttle=True)
    return index.header(cli._ulid_of(agora_id)) or {}


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


@pytest.fixture
def opencode_lab(tmp_path, monkeypatch):
    _with_fakes(tmp_path, monkeypatch)
    fh = tmp_path / "fh"
    (fh / "opencode-sessions").mkdir(parents=True)
    # a 2.x opencode cannot load a v1 payload, so the lab's own session is the v2
    # fixture; the continue (start_native -> import) is what needs it (review of
    # step 1: the fake models 2.x for `session import`)
    shutil.copyfile(OC_FIX / "oc-v2-basic.json", fh / "opencode-sessions" / "default.json")
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(_wrapper(tmp_path, "opencode", FAKE_OPENCODE)))
    monkeypatch.setattr(cli, "load_agent", lambda name: OC.ADAPTER)
    work = tmp_path / "work"
    work.mkdir()
    return {"work": work, "fh": fh}


def _import_claude(capsys, *extra):
    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude", *extra)
    assert code == 0, err
    return a


class Headless:
    """One headless continue in a subprocess, its stdio parked in files.

    Files (not pipes) so a test can poll for the moment the agent is really
    running before it signals - a pipe would need a reader thread to do it.
    """

    def __init__(self, lab, tmp_path, agora_id, agent, *extra, stdin=subprocess.DEVNULL):
        self.out_path = tmp_path / "headless.out"
        self.err_path = tmp_path / "headless.err"
        self._out = open(self.out_path, "wb")
        self._err = open(self.err_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", agora_id,
             "--agent", agent, "--dir", str(lab["work"]), "--headless",
             "--sync-period", SECONDS, *extra],
            stdout=self._out, stderr=self._err, stdin=stdin,
            env=dict(os.environ), cwd=str(tmp_path), text=True,
        )
        self.stdin = self.proc.stdin

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
        raise AssertionError(f"never saw a stderr line starting with {prefix!r}; "
                             f"stderr was {self.stderr()!r}")

    def wait_for_stdout(self, needle, timeout=30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.stdout():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(f"never saw {needle!r} on stdout; got {self.stdout()!r}")

    def terminate(self, timeout=60.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.wait(timeout)

    def wait(self, timeout=60.0) -> int:
        code = self.proc.wait(timeout=timeout)
        self._out.close()
        self._err.close()
        return code


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- Claude Code: stream-json, stderr lines, SIGTERM (3.3) --------------------


def test_claude_headless_uses_stream_json_and_terminates_gently(
    claude_lab, capsys, tmp_path, monkeypatch
):
    a = _import_claude(capsys)
    monkeypatch.setenv("FAKE_AGENT_SCRIPT",
                       f"append:ZZHL 頭一句,wait-file:{tmp_path / 'never-created'}")
    headless = Headless(claude_lab, tmp_path, a, "claude")

    assert headless.wait_for_stderr("agora-session ") == f"agora-session {a}"
    assert headless.wait_for_stderr("agora-agent-session ").startswith("agora-agent-session ")
    headless.wait_for_stdout("FAKE-WAITING")  # the fake is up and holding the turn

    assert headless.terminate() == 0  # gentle stop: agent, write-back, unlock, exit 0

    out = headless.stdout()
    assert "FAKE-AGENT-START" in out, "the agent's own stdout is what the caller reads"
    assert "[agora]" not in out and "agora-session" not in out

    # The programmatic argv, exactly as the spike pinned it (D6).  The fake's
    # log holds sys.argv[1:], so the wrapper path is not in it.
    argv_log = Path(os.environ["FAKE_HOME"]) / "claude-args.log"
    launched = json.loads(argv_log.read_text(encoding="utf-8").splitlines()[-1])
    assert launched[:6] == [
        "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose",
    ]
    assert launched[6] == "--resume"
    assert len(launched[7]) == 36  # the fresh claude session uuid
    assert launched[8:] == ["--settings", '{"autoContinueAtUsageLimit": false}']

    # The final write-back landed in A: the conversation grew by the script's turn.
    code, shown, err = run(capsys, "show", "session", a)
    assert code == 0, err
    assert "ZZHL 頭一句" in shown


def test_claude_headless_reads_the_callers_stdin(claude_lab, capsys, tmp_path, monkeypatch):
    """The caller's stream-json goes into the agent: the pipe is not agora's."""
    a = _import_claude(capsys)
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", "exit")
    headless = Headless(claude_lab, tmp_path, a, "claude", stdin=subprocess.PIPE)
    headless.wait_for_stderr("agora-session ")
    message = json.dumps({"type": "user", "message": {"role": "user", "content": "讀 channel X"}})
    headless.stdin.write(message + "\n")
    headless.stdin.close()
    assert headless.wait() == 0

    received = (Path(os.environ["FAKE_HOME"]) / "claude-stream.log").read_text(encoding="utf-8")
    assert received.strip() == message


def test_sigterm_in_the_window_before_popen_is_forwarded_not_unbound(
    claude_lab, capsys, tmp_path, monkeypatch
):
    """L3: SIGTERM after the handler is installed but before Popen returns.

    The old handler read `proc` before it existed (UnboundLocalError, no
    write-back, no 0).  The probe patches Popen to raise the signal in exactly
    that window - guarded to fire only while agora's own handler is installed -
    and agora must remember it, forward it to the fresh child, and still end 0.
    """
    a = _import_claude(capsys)
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", f"wait-file:{tmp_path / 'never-created'}")

    real_popen = subprocess.Popen
    sent = {"done": False}

    class EarlyTermPopen:
        def __init__(self, *args, **kwargs):
            handler = signal.getsignal(signal.SIGTERM)
            if not sent["done"] and getattr(handler, "__name__", "") == "_forward_term":
                sent["done"] = True
                os.kill(os.getpid(), signal.SIGTERM)  # the race window: proc is None
            self._proc = real_popen(*args, **kwargs)

        def __enter__(self):
            self._proc.__enter__()
            return self._proc

        def __exit__(self, *exc):
            return self._proc.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._proc, name)

    monkeypatch.setattr(cli.subprocess, "Popen", EarlyTermPopen)
    code = cli.main(["continue", "session", a, "--agent", "claude",
                     "--dir", str(claude_lab["work"]), "--headless",
                     "--sync-period", SECONDS])

    assert sent["done"], "the probe never hit the window; it proved nothing"
    assert code == 0, capsys.readouterr().err


def test_headless_fork_prints_the_new_id_and_adds_tags(claude_lab, capsys, tmp_path, monkeypatch):
    a = _import_claude(capsys, "--header", "tags=[ui, tui]")
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", "append:ZZHF 一句,exit")
    assert header_of(a)["tags"] == ["ui", "tui"]

    headless = Headless(claude_lab, tmp_path, a, "claude", "--fork",
                        "--add-tag", "sidecar:t1")
    fork_line = headless.wait_for_stderr("agora-session ")
    assert headless.wait() == 0
    out = headless.stdout()
    err = headless.stderr()

    fork_id = fork_line.split(" ", 1)[1]
    assert fork_id != a and fork_id.startswith("agora:")
    assert f"[agora] fork 已存成 {fork_id}" in err
    assert "[agora]" not in out  # stdout stays the agent's (no id printed there)
    assert header_of(fork_id)["tags"] == ["ui", "tui", "sidecar:t1"]
    assert header_of(a)["tags"] == ["ui", "tui"]  # A is not touched


def test_import_add_tag_appends_without_duplicates(tmp_path, monkeypatch, capsys):
    """Scenario 重複的 tag: existing tags stay, the same tag lands only once."""
    _with_fakes(tmp_path, monkeypatch)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    proj = home / ".claude" / "projects" / C.encode_project_dir(tmp_path / "w")
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(_wrapper(tmp_path, "claude", FAKE_CLAUDE)))
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)

    a = _import_claude(capsys, "--header", "tags=[ui]",
                       "--add-tag", "sidecar:t1", "--add-tag", "sidecar:t1")
    assert header_of(a)["tags"] == ["ui", "sidecar:t1"]
    # Re-importing with the same tag is not a no-op, and stays single.
    a2 = _import_claude(capsys, "--add-tag", "sidecar:t1")
    assert a2 == a
    assert header_of(a)["tags"] == ["ui", "sidecar:t1"]


# --- opencode: serve mode, agora-port, SIGTERM (3.3) --------------------------


def test_import_of_a_v2_opencode_session_writes_its_body(opencode_lab, capsys):
    """H1 (change agora-opencode-v2, step 1): an import from opencode 2.x must not
    leave an empty reading version behind - the body carries every section, the
    tool call included."""
    shutil.copyfile(OC_FIX / "oc-v2-basic.json",
                    opencode_lab["fh"] / "opencode-sessions" / "v2basic.json")
    code, a, err = run(capsys, "import", "session", "--external-session-id", "v2basic",
                       "--agent", "opencode")
    assert code == 0, err
    code, shown, err = run(capsys, "show", "session", a)
    assert code == 0, err
    assert "## user" in shown and "## assistant" in shown
    assert "先讀 fake-note.txt 再回答。" in shown
    assert "[tool] read" in shown
    assert "第二輪：紙條上寫的是什麼顏色？一句話。" in shown


def test_opencode_headless_serves_the_port_and_terminates_gently(
    opencode_lab, capsys, tmp_path
):
    code, a, err = run(capsys, "import", "session", "--external-session-id", "default",
                       "--agent", "opencode")
    assert code == 0, err
    port = _free_port()

    headless = Headless(opencode_lab, tmp_path, a, "opencode",
                        "--headless-port", str(port))
    assert headless.wait_for_stderr("agora-session ") == f"agora-session {a}"
    assert headless.wait_for_stderr("agora-port ") == f"agora-port {port}"
    assert headless.wait_for_stderr("agora-agent-session ").startswith("agora-agent-session ses_")

    # The fake server really listens on the port the caller allocated.
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)

    assert headless.terminate() == 0
    out = headless.stdout()
    assert "[agora]" not in out and "agora-port" not in out

    calls = (opencode_lab["fh"] / "opencode-calls.log").read_text(encoding="utf-8").splitlines()
    served = [json.loads(line)["argv"] for line in calls
              if json.loads(line)["argv"][:1] == ["serve"]]
    assert served == [["serve", "--hostname", "127.0.0.1", "--port", str(port)]]

    code, shown, err = run(capsys, "show", "session", a)
    assert code == 0, err
    assert "ZZSERVE 伺服器收尾前說了最後一句。" in shown


def test_opencode_headless_requires_a_port(opencode_lab, capsys, tmp_path):
    code, a, err = run(capsys, "import", "session", "--external-session-id", "default",
                       "--agent", "opencode")
    assert code == 0, err

    headless = Headless(opencode_lab, tmp_path, a, "opencode")
    assert headless.wait() == cli.EXIT_INPUT
    assert "--headless-port" in headless.stderr()


# --- forced kill: no final write-back, and the lock is gone (spec scenario) ---


def test_a_hard_kill_leaves_no_lock_behind(claude_lab, capsys, tmp_path, monkeypatch):
    a = _import_claude(capsys)
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_AGENT_SCRIPT", f"append:ZZHK 一句,wait-file:{release}")
    headless = Headless(claude_lab, tmp_path, a, "claude")
    headless.wait_for_stderr("agora-session ")
    headless.wait_for_stdout("FAKE-WAITING")

    headless.proc.kill()             # SIGKILL agora: no final write-back
    assert headless.wait(timeout=30) == -signal.SIGKILL

    release.write_text("go")         # the orphaned fake agent exits by itself
    deadline = time.monotonic() + 10
    completed = None
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [sys.executable, "-m", "agora.cli", "continue", "session", a, "--agent", "claude",
             "--dir", str(claude_lab["work"]), "--sync-period", "0", "--headless"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env=dict(os.environ), cwd=str(tmp_path), timeout=60,
        )
        if "正在接續中" not in completed.stderr:
            break
        time.sleep(0.1)
    assert completed is not None and "正在接續中" not in completed.stderr
    assert completed.returncode in (0, cli.EXIT_IN_OUTBOX), completed.stderr
