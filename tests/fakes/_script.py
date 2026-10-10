#!/usr/bin/env python3
"""A scripted agent: FAKE_AGENT_SCRIPT says what the fake does, one step at a time.

    FAKE_AGENT_SCRIPT=append:第一句,wait-sync,partial,wait-sync,append:第二句,exit

A step is `append:<text>` (the user says it, the agent answers), `partial` (the
session is caught mid-write: a half-written line / a message that never finished),
`wait-sync` (stop and wait for agora to finish a periodic write-back) or `exit`.
The point is timing: a test needs the conversation to grow *between* two write-backs,
and a fake that appends one pair and returns cannot say when that happens.

`wait-sync` waits for the signal file to hold one more complete line than it did
when the step began - FAKE_SYNC_SIGNAL=<path>, which for the periodic write-back is
`<state>/continue.log`. Just after it takes that count it prints WAIT_MARK
(`FAKE-WAITING`) and only then polls, so a caller that has to add the line can wait
for the marker: a line added after it is always one the fake has not counted yet,
which a sleep cannot promise. It gives up after FAKE_SYNC_TIMEOUT seconds (10 by
default) with a reason on stderr rather than hanging: a test whose write-back never
comes should fail, not sit there.

START_MARK and END_MARK go to stdout around everything else, so a test that sends
agora's stdout and stderr to one file can tell that nothing of agora's appeared
while the agent owned the terminal (design「不弄亂 agent 的畫面」).

Without FAKE_AGENT_SCRIPT nothing here runs and the fakes behave exactly as before.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

START_MARK = "FAKE-AGENT-START"
END_MARK = "FAKE-AGENT-END"
ERROR_MARK = "FAKE-AGENT-ERROR"
#: Printed by `wait-sync` once it has taken the line count it is waiting past, and
#: before it polls. A caller that adds the write-back line itself waits for this;
#: adding it any earlier can be counted before the fake even gets here.
WAIT_MARK = "FAKE-WAITING"

DEFAULT_WAIT_SECONDS = 10.0
POLL_SECONDS = 0.02


class ScriptError(RuntimeError):
    """The script asked for something this fake cannot do. Exit 2; never hang."""


def steps(script: str) -> list[str]:
    return [step.strip() for step in script.split(",") if step.strip()]


def run(script: str, append, partial) -> None:
    """Run the script against a session the caller knows how to change.

    `append(text)` and `partial()` are the agent's own two moves - each fake writes
    them in its own format, since the session on disk is the agent's own.
    """
    print(START_MARK, flush=True)
    try:
        for step in steps(script):
            name, _, text = step.partition(":")
            if name == "append":
                append(text.strip() or "ZZSCRIPT 使用者又說了一句")
            elif name == "partial":
                partial()
            elif name == "wait-sync":
                wait_for_sync()
            elif name == "exit":
                break
            else:
                raise ScriptError(f"不認得的步驟：{step}")
    except ScriptError as e:
        print(f"{ERROR_MARK} {e}", file=sys.stderr, flush=True)
        sys.exit(2)
    print(END_MARK, flush=True)


def wait_for_sync() -> None:
    """Block until the signal file has one more complete line than it had just now.

    The count is taken first and only then announced, so a caller that waits for
    WAIT_MARK and writes the line from there cannot land before the count - which is
    the race that made this flaky: a caller sleeping a fixed time could write the
    line while the fake was still starting up, and the wait then had nothing left to
    wait for. Polling with a deadline, never a sleep-and-hope: whole lines only, so a
    half-written one at the end does not count, and a write-back that never comes
    ends the run with a reason on stderr instead of hanging it.
    """
    path = signal_path()
    if path is None:
        raise ScriptError("wait-sync 要有 FAKE_SYNC_SIGNAL 指到一個檔案"
                          "（agora 每做完一次寫回會在那裡加一行）")
    before = lines(path)
    print(WAIT_MARK, flush=True)
    seconds = timeout()
    deadline = time.monotonic() + seconds
    while lines(path) <= before:
        if time.monotonic() >= deadline:
            raise ScriptError(f"等不到寫回：{seconds:g} 秒內 {path} 沒有多一行"
                              f"（還是 {before} 行）")
        time.sleep(POLL_SECONDS)


def signal_path() -> Path | None:
    """The file `wait-sync` watches, or None when the environment names none."""
    path = os.environ.get("FAKE_SYNC_SIGNAL")
    return Path(path) if path else None


def lines(path: Path) -> int:
    """How many complete lines the signal file holds; 0 while there is no file."""
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return 0


def timeout() -> float:
    try:
        return max(0.1, float(os.environ.get("FAKE_SYNC_TIMEOUT", DEFAULT_WAIT_SECONDS)))
    except ValueError:
        return DEFAULT_WAIT_SECONDS