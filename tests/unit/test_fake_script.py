"""The scripted mode of the two fake agents (change continue-periodic-sync 1.8).

The fakes themselves, run as the executables they are - no agora anywhere. What is
worth testing here is the timing contract the periodic write-back tests lean on:
the conversation grows when the script says so, `wait-sync` really blocks until the
signal file gains a line (and gives up instead of hanging), `partial` leaves the
session in the shape a write-back has to survive, and with no script at all the two
fakes do exactly what they did before.

`export` reading stdin (design「不弄亂 agent 的畫面」) is the adapter's promise, so
its test lives in test_agent_opencode.py; the probe it reads with is here.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from agora.agents import claude as C
from agora.agents import opencode as oc
from agora.agents.base import format_reading
from agora.agents.claude import encode_project_dir as encode

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CLAUDE_FAKE = FAKES / "fake_claude.py"
OPENCODE_FAKE = FAKES / "fake_opencode.py"
SESSION = "11111111-2222-4333-8444-555555555555"


# --- running a fake ---------------------------------------------------------


def claude_env(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """A claude home with the fixture session in it; the cwd has to match, because
    the fake finds the session the way claude does - through the encoded pwd."""
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = home / ".claude" / "projects" / encode(workdir)
    project.mkdir(parents=True)
    (project / f"{SESSION}.jsonl").write_bytes(
        (FIXTURES / "claude" / "cl-basic.jsonl").read_bytes())
    return workdir, home


def opencode_env(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The fake opencode's store, seeded with oc-basic under a plain name."""
    home = tmp_path / "home"
    store = home / "opencode-sessions"
    store.mkdir(parents=True)
    (store / "seed.json").write_bytes((FIXTURES / "opencode" / "oc-basic.json").read_bytes())
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(home))
    return home, workdir


def run_fake(fake: Path, argv: list[str], cwd: Path, **extra) -> subprocess.CompletedProcess:
    """The fake the way a test uses it: through an sh wrapper, so nothing depends on
    the user's python3 shim (CL3)."""
    wrapper = cwd / f"wrapper-{fake.stem}"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {fake} "$@"\n')
    wrapper.chmod(0o755)
    return subprocess.run([str(wrapper), *argv], cwd=str(cwd), check=False,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, **extra})


def session_file(workdir: Path, home: Path) -> Path:
    return home / ".claude" / "projects" / encode(workdir) / f"{SESSION}.jsonl"


def parsed(path: Path) -> list[dict]:
    """Every line of a claude session, a half-written one kept as ZZHALF."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"type": "ZZHALF", "raw": line})
    return out


def append_lines(signal: Path, count: int, delay: float, gap: float = 0.2) -> threading.Thread:
    """Someone else adding to the signal file, the way agora's continue.log does.

    `delay` has to outlast the fake's own start-up (a python that imports agora takes
    a moment), or the lines land before the fake starts counting and it waits for a
    write-back that has already happened.
    """

    def go():
        time.sleep(delay)
        for n in range(count):
            with open(signal, "a", encoding="utf-8") as f:
                f.write(f"[agora] 第 {n + 1} 次寫回\n")
                f.flush()
            time.sleep(gap)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread


# --- markers, and no script means no script ---------------------------------


def test_claude_brackets_the_run_with_two_markers(tmp_path, monkeypatch):
    workdir, _home = claude_env(tmp_path, monkeypatch)
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"), FAKE_AGENT_SCRIPT="append:ZZ 一句")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["FAKE-AGENT-START", "FAKE-AGENT-END"]


def test_opencode_brackets_the_run_with_two_markers(tmp_path, monkeypatch):
    _home, workdir = opencode_env(tmp_path, monkeypatch)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir,
                    FAKE_AGENT_SCRIPT="append:ZZ 一句")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["FAKE-AGENT-START", "FAKE-AGENT-END"]


def test_without_a_script_claude_still_appends_one_pair(tmp_path, monkeypatch):
    """The old FAKE_AGENT_MODE=append behaviour, unchanged - and no markers, so a test
    that does not ask for a script does not get one."""
    workdir, home = claude_env(tmp_path, monkeypatch)
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"), FAKE_AGENT_MODE="append")
    assert proc.returncode == 0 and proc.stdout.strip() == "ZZSAY 好的"
    added = parsed(session_file(workdir, home))[-2:]
    assert [o["type"] for o in added] == ["user", "assistant"]
    assert added[0]["message"]["content"] == "ZZSAY 再補一句"


def test_without_a_script_claude_noop_is_still_a_noop(tmp_path, monkeypatch):
    workdir, home = claude_env(tmp_path, monkeypatch)
    path = session_file(workdir, home)
    before = path.read_bytes()
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"), FAKE_AGENT_MODE="noop")
    assert proc.returncode == 0 and proc.stdout == ""
    assert path.read_bytes() == before


def test_without_a_script_opencode_still_appends_one_pair(tmp_path, monkeypatch):
    home, workdir = opencode_env(tmp_path, monkeypatch)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir, FAKE_AGENT_MODE="append")
    assert proc.returncode == 0 and proc.stdout == ""
    payload = json.loads((home / "opencode-sessions" / "seed.json").read_text())
    assert [m["info"]["role"] for m in payload["messages"][-2:]] == ["user", "assistant"]
    assert payload["messages"][-2]["parts"][0]["text"] == "ZZAPPEND 使用者又問了一句。"


def test_a_script_overrides_the_mode(tmp_path, monkeypatch):
    """FAKE_AGENT_MODE is the old one-move switch; a script is the whole plan and
    wins, or a test that set both would get the old behaviour."""
    home, workdir = opencode_env(tmp_path, monkeypatch)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir, FAKE_AGENT_MODE="noop",
                    FAKE_AGENT_SCRIPT="append:ZZ 照腳本走")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((home / "opencode-sessions" / "seed.json").read_text())
    assert payload["messages"][-2]["parts"][0]["text"] == "ZZ 照腳本走"


def test_a_script_overrides_the_mode_for_claude_too(tmp_path, monkeypatch):
    workdir, home = claude_env(tmp_path, monkeypatch)
    before = parsed(session_file(workdir, home))
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"), FAKE_AGENT_MODE="noop",
                    FAKE_AGENT_SCRIPT="append:ZZ 照腳本走")
    assert proc.returncode == 0, proc.stderr
    added = parsed(session_file(workdir, home))[len(before):]
    assert [o["message"]["content"] for o in added if o["type"] == "user"] == ["ZZ 照腳本走"]


def test_a_headless_run_is_never_scripted(tmp_path, monkeypatch):
    """summarize() runs `opencode run <material> -f … --format json` with no session
    named; the script must not swallow that - it has to answer with its own events."""
    _home, workdir = opencode_env(tmp_path, monkeypatch)
    material = workdir / "material-1.md"
    material.write_text("ZZ 材料", encoding="utf-8")
    proc = run_fake(OPENCODE_FAKE, ["run", "ZZ 請回答", "-f", str(material), "--format", "json"],
                    workdir, FAKE_AGENT_SCRIPT="append:ZZ 不該走這裡")
    assert proc.returncode == 0
    assert "ZZSUMMARY" in proc.stdout
    assert "FAKE-AGENT-START" not in proc.stdout


# --- append / partial --------------------------------------------------------


def test_claude_says_each_thing_and_chains_it(tmp_path, monkeypatch):
    workdir, home = claude_env(tmp_path, monkeypatch)
    path = session_file(workdir, home)
    before = parsed(path)
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"),
                    FAKE_AGENT_SCRIPT="append:ZZ 第一句,exit,append:ZZ 不該跑到")
    assert proc.returncode == 0, proc.stderr
    added = parsed(path)[len(before):]
    assert [o["type"] for o in added] == ["user", "assistant"]
    assert added[0]["message"]["content"] == "ZZ 第一句"
    assert added[0]["parentUuid"] == before[-1]["uuid"]
    assert added[1]["parentUuid"] == added[0]["uuid"]
    assert "ZZ 不該跑到" not in path.read_text(encoding="utf-8")


def test_claude_partial_leaves_half_a_line(tmp_path, monkeypatch):
    """The reader has to drop it (review S10): a half-written line is not a
    conversation, and keeping it would put a broken object in the next write-back."""
    workdir, home = claude_env(tmp_path, monkeypatch)
    path = session_file(workdir, home)
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"),
                    FAKE_AGENT_SCRIPT="partial,exit")
    assert proc.returncode == 0, proc.stderr
    text = path.read_text(encoding="utf-8")
    assert not text.endswith("\n")            # written, not finished
    with pytest.raises(ValueError):
        json.loads(text.splitlines()[-1])
    whole = C._split_lines(text, "session.jsonl")
    assert len(whole) == 16 and "ZZPARTIAL" not in whole[-1]


def test_opencode_partial_leaves_a_running_tool_call(tmp_path, monkeypatch):
    """opencode's version of "caught mid-write": the call is there, its state says
    running, and there is no output yet - a write-back here is a conversation stopped
    inside a tool call (design「讀到一半不能寫壞」)."""
    home, workdir = opencode_env(tmp_path, monkeypatch)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir,
                    FAKE_AGENT_SCRIPT="partial,exit")
    assert proc.returncode == 0, proc.stderr
    raw = (home / "opencode-sessions" / "seed.json").read_bytes()
    message = json.loads(raw)["messages"][-1]
    assert message["info"]["role"] == "assistant"
    assert "completed" not in message["info"]["time"]
    part = message["parts"][0]
    assert part["type"] == "tool" and part["state"]["status"] == "running"
    assert "output" not in part["state"]
    # and it still reads: one tool line, so a write-back of this state is not empty
    assert "[tool] bash" in format_reading(oc.ADAPTER.turns(raw))


# --- wait-sync ---------------------------------------------------------------


def test_claude_waits_for_a_line_before_it_says_the_next_thing(tmp_path, monkeypatch):
    workdir, home = claude_env(tmp_path, monkeypatch)
    path = session_file(workdir, home)
    signal = tmp_path / "continue.log"
    append_lines(signal, 1, 1.0)
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"),
                    FAKE_AGENT_SCRIPT="append:ZZ 第一句,wait-sync,append:ZZ 第二句,exit",
                    FAKE_SYNC_SIGNAL=str(signal))
    assert proc.returncode == 0, proc.stderr
    text = path.read_text(encoding="utf-8")
    assert text.index("ZZ 第一句") < text.index("ZZ 第二句")
    assert signal.read_text(encoding="utf-8").count("\n") == 1


def test_opencode_waits_for_a_line_too(tmp_path, monkeypatch):
    home, workdir = opencode_env(tmp_path, monkeypatch)
    signal = tmp_path / "continue.log"
    append_lines(signal, 1, 1.0)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir,
                    FAKE_AGENT_SCRIPT="wait-sync,append:ZZ 等到了才說,exit",
                    FAKE_SYNC_SIGNAL=str(signal))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((home / "opencode-sessions" / "seed.json").read_text())
    assert payload["messages"][-2]["parts"][0]["text"] == "ZZ 等到了才說"


def test_each_wait_sync_waits_for_its_own_line(tmp_path, monkeypatch):
    """Two write-backs, two waits: counting from when each wait starts is what makes
    the second one wait for the second line instead of returning at once."""
    home, workdir = opencode_env(tmp_path, monkeypatch)
    signal = tmp_path / "continue.log"
    signal.write_text("[agora] 啟動\n", encoding="utf-8")
    append_lines(signal, 2, 1.0)
    started = time.monotonic()
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir,
                    FAKE_AGENT_SCRIPT="wait-sync,append:ZZ 一,wait-sync,append:ZZ 二,exit",
                    FAKE_SYNC_SIGNAL=str(signal))
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert elapsed >= 1.1          # ~1 s to start, then 0.2 s for the second line
    payload = json.loads((home / "opencode-sessions" / "seed.json").read_text())
    assert payload["messages"][-2]["parts"][0]["text"] == "ZZ 二"


def test_a_wait_sync_that_never_comes_fails_instead_of_hanging(tmp_path, monkeypatch):
    """A test whose write-back never happens should see a failure it can read, not a
    run that sits there: exit 2, the reason on stderr, and no end marker."""
    workdir, home = claude_env(tmp_path, monkeypatch)
    path = session_file(workdir, home)
    before = path.read_bytes()
    started = time.monotonic()
    proc = run_fake(CLAUDE_FAKE, ["--resume", SESSION], workdir,
                    FAKE_HOME=str(tmp_path / "fake"),
                    FAKE_AGENT_SCRIPT="wait-sync,append:ZZ 說不出來,exit",
                    FAKE_SYNC_SIGNAL=str(tmp_path / "continue.log"),
                    FAKE_SYNC_TIMEOUT="1")
    assert proc.returncode == 2
    assert "等不到寫回" in proc.stderr
    assert "1 秒內" in proc.stderr          # the deadline is the one we asked for
    assert "FAKE-AGENT-END" not in proc.stdout
    assert time.monotonic() - started < 8   # and it gave up on it
    assert path.read_bytes() == before


def test_a_wait_sync_without_a_signal_file_is_a_mistake_not_a_hang(tmp_path, monkeypatch):
    _home, workdir = opencode_env(tmp_path, monkeypatch)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir,
                    FAKE_AGENT_SCRIPT="wait-sync,exit")
    assert proc.returncode == 2
    assert "FAKE_SYNC_SIGNAL" in proc.stderr


def test_an_unknown_step_says_so_instead_of_doing_nothing(tmp_path, monkeypatch):
    _home, workdir = opencode_env(tmp_path, monkeypatch)
    proc = run_fake(OPENCODE_FAKE, ["--session", "seed"], workdir,
                    FAKE_AGENT_SCRIPT="append:ZZ 一句,dance")
    assert proc.returncode == 2 and "不認得的步驟" in proc.stderr


# --- the counter itself ------------------------------------------------------


def test_lines_counts_whole_lines_and_survives_no_file(tmp_path):
    sys.path.insert(0, str(FAKES))
    import _script

    missing = tmp_path / "nothing-here.log"
    assert _script.lines(missing) == 0
    present = tmp_path / "continue.log"
    present.write_bytes(b"one\ntwo\nthree\nhalf")
    assert _script.lines(present) == 3       # the unfinished last line does not count


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))