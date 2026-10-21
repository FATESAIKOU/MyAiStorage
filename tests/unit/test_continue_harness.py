"""Tests for agora continue --harness-file delivery (task 3b.4 / 3b.5)."""

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
from agora.agents.base import Launch
from _round_seam import in_process_start_child

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE_RCLONE = FAKES / "fake_rclone.py"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_OPENCODE = FAKES / "fake_opencode.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
OC_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
CL_SID = "11111111-2222-4333-8444-555555555555"
SECONDS = "0.005"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Headless:
    """One headless continue in a subprocess, modeled after test_continue_headless.py."""

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

    def stderr(self) -> str:
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def stdout(self) -> str:
        return self.out_path.read_text(encoding="utf-8", errors="replace")

    def wait_for_stderr(self, prefix: str, timeout: float = 10.0) -> str:
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

    def terminate(self, timeout: float = 10.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.wait(timeout)

    def wait(self, timeout: float = 10.0) -> int:
        code = self.proc.wait(timeout=timeout)
        self._out.close()
        self._err.close()
        return code


def _recording_wrapper(tmp_path, name, real_fake, log_file):
    """Wrapper that records argv and env to log_file, then becomes the real fake."""
    w = tmp_path / f"wrap_{name}"
    w.write_text(f"""#!/usr/bin/env python3
import sys, os, json

log_path = {repr(str(log_file))}
with open(log_path, "a") as f:
    f.write(json.dumps({{"name": {repr(name)}, "argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")

# Replace this process with the real fake: no middle process that a SIGTERM to the
# headless harness could kill while the fake keeps running as an orphan (#117).
os.execv({repr(sys.executable)}, [{repr(sys.executable)}, {repr(str(real_fake))}] + sys.argv[1:])
""")
    w.chmod(0o755)
    return w


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def paths() -> store.Paths:
    return store.Paths.from_env()


def _with_fakes(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    rclone_wrap = tmp_path / "rclone_wrap"
    rclone_wrap.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    rclone_wrap.chmod(0o755)
    monkeypatch.setenv("AGORA_RCLONE", str(rclone_wrap))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))


@pytest.fixture
def claude_lab(tmp_path, monkeypatch):
    _with_fakes(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    work = tmp_path / "proj"
    work.mkdir()
    proj = home / ".claude" / "projects" / C.encode_project_dir(work)
    proj.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    log_file = tmp_path / "calls.jsonl"
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(_recording_wrapper(tmp_path, "claude", FAKE_CLAUDE, log_file)))
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    return {"home": home, "work": work, "log": log_file}


@pytest.fixture
def opencode_lab(tmp_path, monkeypatch):
    _with_fakes(tmp_path, monkeypatch)
    fh = tmp_path / "fh"
    (fh / "opencode-sessions").mkdir(parents=True)
    shutil.copyfile(OC_FIX / "oc-v2-basic.json", fh / "opencode-sessions" / "default.json")
    log_file = tmp_path / "calls.jsonl"
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(_recording_wrapper(tmp_path, "opencode", FAKE_OPENCODE, log_file)))
    monkeypatch.setattr(cli, "load_agent", lambda name: OC.ADAPTER)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    work = tmp_path / "work"
    work.mkdir()
    return {"work": work, "fh": fh, "log": log_file}


def _read_records(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    lines = [line.strip() for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Unit tests: Adapter start_native
# ---------------------------------------------------------------------------

def test_claude_adapter_start_native_adds_flags_when_harness_file_given(claude_lab, tmp_path):
    hf = tmp_path / "harness.md"
    hf.write_text("# harness\n")
    main = ['{"type": "user", "sessionId": "s", "message": "hi", "cwd": "/tmp/x"}']
    raw = json.dumps({"format": C.FORMAT, "main": main, "aux": {}}).encode()
    # 1. Interactive mode
    l_inter = C.ADAPTER.start_native(raw, claude_lab["work"], harness_file=hf)
    assert "--append-system-prompt-file" in l_inter.argv
    idx = l_inter.argv.index("--append-system-prompt-file")
    assert l_inter.argv[idx + 1] == str(hf.resolve())
    assert "--system-prompt-snapshot" in l_inter.argv
    idx_snap = l_inter.argv.index("--system-prompt-snapshot")
    assert l_inter.argv[idx_snap + 1] == "off"

    # 2. Headless mode
    l_head = C.ADAPTER.start_native(raw, claude_lab["work"], headless=True, harness_file=hf)
    assert "--append-system-prompt-file" in l_head.argv
    assert "--system-prompt-snapshot" in l_head.argv

    # 3. Without harness_file
    l_none = C.ADAPTER.start_native(raw, claude_lab["work"])
    assert "--append-system-prompt-file" not in l_none.argv
    assert "--system-prompt-snapshot" not in l_none.argv


def test_opencode_adapter_start_native_sets_launch_env_when_harness_file_given(opencode_lab, tmp_path):
    hf = tmp_path / "harness.md"
    hf.write_text("# harness\n")
    raw = (OC_FIX / "oc-v2-basic.json").read_bytes()

    # 1. Interactive mode
    l_inter = OC.ADAPTER.start_native(raw, opencode_lab["work"], harness_file=hf)
    assert "OPENCODE_CONFIG_CONTENT" in l_inter.env
    content = json.loads(l_inter.env["OPENCODE_CONFIG_CONTENT"])
    assert content == {"instructions": [str(hf.resolve())]}
    assert Path(content["instructions"][0]).is_absolute()
    # Must not contain provider, credentials, or extra config
    assert list(content.keys()) == ["instructions"]

    # 2. Headless mode
    l_head = OC.ADAPTER.start_native(raw, opencode_lab["work"], headless=True, headless_port=9999, harness_file=hf)
    assert "OPENCODE_CONFIG_CONTENT" in l_head.env
    content_head = json.loads(l_head.env["OPENCODE_CONFIG_CONTENT"])
    assert content_head == {"instructions": [str(hf.resolve())]}

    # 3. Without harness_file
    l_none = OC.ADAPTER.start_native(raw, opencode_lab["work"])
    assert "OPENCODE_CONFIG_CONTENT" not in l_none.env
    assert l_none.env == {}


# ---------------------------------------------------------------------------
# CLI end-to-end tests: agora continue --harness-file
# ---------------------------------------------------------------------------

def test_cli_continue_claude_with_harness_file(claude_lab, tmp_path, capsys):
    hf = tmp_path / "pm-harness.md"
    hf.write_text("# PM HARNESS\n")

    code, agora_id, err = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0, err

    # 1. Interactive continue
    code, _, err = run(capsys, "continue", "session", agora_id, "--agent", "claude",
                       "--dir", str(claude_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", str(hf))
    assert code == 0, err

    records = _read_records(claude_lab["log"])
    assert len(records) >= 1
    argv = records[-1]["argv"]
    assert "--append-system-prompt-file" in argv
    idx = argv.index("--append-system-prompt-file")
    assert argv[idx + 1] == str(hf.resolve())
    assert "--system-prompt-snapshot" in argv
    idx_snap = argv.index("--system-prompt-snapshot")
    assert argv[idx_snap + 1] == "off"

    # 2. Headless continue with --fork
    code, _, err = run(capsys, "continue", "session", agora_id, "--agent", "claude",
                       "--dir", str(claude_lab["work"]), "--sync-period", SECONDS,
                       "--fork", "--headless", "--harness-file", str(hf))
    assert code == 0, err

    records = _read_records(claude_lab["log"])
    argv_head = records[-1]["argv"]
    assert "-p" in argv_head
    assert "--append-system-prompt-file" in argv_head
    assert "--system-prompt-snapshot" in argv_head


def test_cli_continue_opencode_with_harness_file(opencode_lab, tmp_path, capsys):
    hf = tmp_path / "pm-harness.md"
    hf.write_text("# PM HARNESS\n")

    code, agora_id, err = run(capsys, "import", "session", "--external-session-id", "default", "--agent", "opencode")
    assert code == 0, err

    # 1. Interactive continue
    code, _, err = run(capsys, "continue", "session", agora_id, "--agent", "opencode",
                       "--dir", str(opencode_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", str(hf))
    assert code == 0, err

    launched = [r for r in _read_records(opencode_lab["log"]) if "--session" in r["argv"]]
    assert len(launched) >= 1
    env = launched[-1]["env"]
    assert "OPENCODE_CONFIG_CONTENT" in env
    parsed = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert parsed == {"instructions": [str(hf.resolve())]}
    assert Path(parsed["instructions"][0]).is_absolute()
    assert list(parsed.keys()) == ["instructions"]


def test_cli_continue_claude_without_harness_file_does_not_inject(claude_lab, capsys):
    code, cl_id, err = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0, err
    code, _, err = run(capsys, "continue", "session", cl_id, "--agent", "claude",
                       "--dir", str(claude_lab["work"]), "--sync-period", SECONDS)
    assert code == 0, err
    cl_record = _read_records(claude_lab["log"])[-1]
    assert "--append-system-prompt-file" not in cl_record["argv"]
    assert "--system-prompt-snapshot" not in cl_record["argv"]


def test_cli_continue_opencode_without_harness_file_does_not_inject(opencode_lab, capsys):
    code, oc_id, err = run(capsys, "import", "session", "--external-session-id", "default", "--agent", "opencode")
    assert code == 0, err
    code, _, err = run(capsys, "continue", "session", oc_id, "--agent", "opencode",
                       "--dir", str(opencode_lab["work"]), "--sync-period", SECONDS)
    assert code == 0, err
    launched = [r for r in _read_records(opencode_lab["log"]) if "--session" in r["argv"]]
    assert len(launched) >= 1
    assert "OPENCODE_CONFIG_CONTENT" not in launched[-1]["env"]


def test_cli_continue_subsequent_session_does_not_leak_and_environ_unchanged(opencode_lab, tmp_path, capsys):
    hf = tmp_path / "pm-harness.md"
    hf.write_text("# HARNESS\n")

    code, s1, err = run(capsys, "import", "session", "--external-session-id", "default", "--agent", "opencode")
    assert code == 0, err

    environ_before = dict(os.environ)

    # Session 1: with --harness-file
    code, _, err = run(capsys, "continue", "session", s1, "--agent", "opencode",
                       "--dir", str(opencode_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", str(hf))
    assert code == 0, err
    launched1 = [r for r in _read_records(opencode_lab["log"]) if "--session" in r["argv"]]
    assert len(launched1) == 1
    assert "OPENCODE_CONFIG_CONTENT" in launched1[0]["env"]

    # Session 2: without --harness-file in the same CLI process
    code, _, err = run(capsys, "continue", "session", s1, "--agent", "opencode",
                       "--dir", str(opencode_lab["work"]), "--sync-period", SECONDS)
    assert code == 0, err
    launched2 = [r for r in _read_records(opencode_lab["log"]) if "--session" in r["argv"]]
    assert len(launched2) == 2
    assert "OPENCODE_CONFIG_CONTENT" not in launched2[1]["env"]

    environ_after = dict(os.environ)
    assert environ_before == environ_after, "agora process os.environ must not be modified"


def test_cli_continue_harness_file_missing_fails_before_launch(claude_lab, tmp_path, capsys):
    missing_hf = tmp_path / "no-such-harness.md"
    code, cl_id, err = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0, err

    code, _, err = run(capsys, "continue", "session", cl_id, "--agent", "claude",
                       "--dir", str(claude_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", str(missing_hf))
    assert code == 1, "nonexistent --harness-file must be exit 1 (usage/input error)"
    assert "不存在" in err
    assert _read_records(claude_lab["log"]) == [], "no child process should be launched"


def test_cli_continue_harness_file_directory_fails_before_launch(claude_lab, tmp_path, capsys):
    dir_hf = tmp_path / "harness_dir"
    dir_hf.mkdir()
    code, cl_id, err = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0, err

    code, _, err = run(capsys, "continue", "session", cl_id, "--agent", "claude",
                       "--dir", str(claude_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", str(dir_hf))
    assert code == 2, "unreadable --harness-file must be exit 2"
    assert "讀不到" in err or "目錄" in err
    assert _read_records(claude_lab["log"]) == [], "no child process should be launched"


def test_cli_continue_without_harness_file_calls_start_native_without_harness_file_kwarg(claude_lab, monkeypatch, capsys):
    """M2: when --harness-file is omitted, start_native is called with HEAD shape (no harness_file in kwargs)."""
    code, cl_id, err = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0, err

    calls = []
    real_start_native = C.ADAPTER.start_native

    def legacy_start_native(raw, workdir):
        # NOTE: signature intentionally omits harness_file and **kwargs to verify HEAD call shape
        calls.append({"raw": raw, "workdir": workdir})
        return real_start_native(raw, workdir)

    monkeypatch.setattr(C.ADAPTER, "start_native", legacy_start_native)

    code, out, err = run(capsys, "continue", "session", cl_id, "--agent", "claude",
                         "--dir", str(claude_lab["work"]), "--sync-period", SECONDS)
    assert code == 0, err
    assert len(calls) == 1


def test_cli_continue_claude_relative_harness_file_resolves_to_absolute(claude_lab, tmp_path, monkeypatch, capsys):
    """L1: relative --harness-file path must be resolved to absolute in claude argv."""
    code, cl_id, err = run(capsys, "import", "session", "--external-session-id", CL_SID, "--agent", "claude")
    assert code == 0, err

    sub = tmp_path / "sub_cl"
    sub.mkdir()
    rel_hf = sub / "my_harness.md"
    rel_hf.write_text("# RELATIVE HARNESS\n")

    monkeypatch.chdir(sub)
    code, _, err = run(capsys, "continue", "session", cl_id, "--agent", "claude",
                       "--dir", str(claude_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", "my_harness.md")
    assert code == 0, err

    cl_record = _read_records(claude_lab["log"])[-1]
    idx = cl_record["argv"].index("--append-system-prompt-file")
    delivered_path = cl_record["argv"][idx + 1]
    assert delivered_path == str(rel_hf.resolve())
    assert Path(delivered_path).is_absolute()
    assert delivered_path != "my_harness.md"


def test_cli_continue_opencode_relative_harness_file_resolves_to_absolute(opencode_lab, tmp_path, monkeypatch, capsys):
    """L1: relative --harness-file path must be resolved to absolute in opencode config content."""
    code, agora_id, err = run(capsys, "import", "session", "--external-session-id", "default", "--agent", "opencode")
    assert code == 0, err

    sub = tmp_path / "sub_oc"
    sub.mkdir()
    rel_hf = sub / "my_harness.md"
    rel_hf.write_text("# RELATIVE HARNESS\n")

    monkeypatch.chdir(sub)
    code, _, err = run(capsys, "continue", "session", agora_id, "--agent", "opencode",
                       "--dir", str(opencode_lab["work"]), "--sync-period", SECONDS,
                       "--harness-file", "my_harness.md")
    assert code == 0, err

    launched = [r for r in _read_records(opencode_lab["log"]) if "--session" in r["argv"]]
    env = launched[-1]["env"]
    assert "OPENCODE_CONFIG_CONTENT" in env
    parsed = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    delivered_path = parsed["instructions"][0]
    assert delivered_path == str(rel_hf.resolve())
    assert Path(delivered_path).is_absolute()
    assert delivered_path != "my_harness.md"


def test_cli_continue_opencode_headless_with_harness_file(opencode_lab, tmp_path, capsys):
    """L7: opencode --headless with --harness-file sets OPENCODE_CONFIG_CONTENT in Launch.env."""
    hf = tmp_path / "pm-harness.md"
    hf.write_text("# PM HARNESS\n")

    code, agora_id, err = run(capsys, "import", "session", "--external-session-id", "default", "--agent", "opencode")
    assert code == 0, err

    port = _free_port()
    headless = Headless(opencode_lab, tmp_path, agora_id, "opencode",
                        "--headless-port", str(port), "--harness-file", str(hf))
    try:
        assert headless.wait_for_stderr("agora-port ", timeout=10.0) == f"agora-port {port}"
        assert headless.wait_for_stderr("agora-session ", timeout=10.0) == f"agora-session {agora_id}"
        deadline = time.monotonic() + 5.0
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        assert headless.terminate(timeout=10.0) == 0
        # #117: the wrapper now `execv`s the fake, so the child the headless CLI
        # signals IS the server. With a middle process, that one died and the fake
        # `serve` kept the port - and the session - alive as an orphan.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            except OSError:
                break                   # nothing is listening: the server went with the CLI
            if time.monotonic() > deadline:
                raise AssertionError("fake_opencode serve outlived the headless continue (orphan)")
            time.sleep(0.05)
    finally:
        if headless.proc.poll() is None:
            headless.proc.kill()
            headless.proc.wait(timeout=5.0)

    launched = [r for r in _read_records(opencode_lab["log"]) if "serve" in r["argv"]]
    assert len(launched) >= 1
    server_call = launched[-1]
    assert server_call["argv"] == ["serve", "--hostname", "127.0.0.1", "--port", str(port)]
    env = server_call["env"]
    assert "OPENCODE_CONFIG_CONTENT" in env
    parsed = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert parsed == {"instructions": [str(hf.resolve())]}
    assert Path(parsed["instructions"][0]).is_absolute()


