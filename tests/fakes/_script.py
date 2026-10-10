#!/usr/bin/env python3
"""A scripted agent: FAKE_AGENT_SCRIPT says what the fake does, one step at a time.

    FAKE_AGENT_SCRIPT=append:第一句,wait-sync,partial,wait-sync,append:第二句,exit

A step is `append:<text>` (the user says it, the agent answers), `partial` (the
session is caught mid-write: a half-written line / a message that never finished),
`wait-sync` (stop and wait for agora to finish a periodic write-back) or `exit`.
The point is timing: a test needs the conversation to grow *between* two write-backs,
and a fake that appends one pair and returns cannot say when that happens.

`wait-sync` watches a file and waits for one more complete line in it -
FAKE_SYNC_SIGNAL=<path>, which for the periodic write-back is `<state>/continue.log`
(the wrapper hides the path, so a test sets it from what it handed agora). It gives
up after FAKE_SYNC_TIMEOUT seconds (10 by default) rather than hanging: a test whose
write-back never comes should fail, not sit there.

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


def wait_for_sync() -> int:
    """Block until the signal file has one more line than when this was called.

    Returns the number of lines it waited for (always 1 - the point is that it
    waited at all), so a caller can say so if it wants to.
    """
    path = os.environ.get("FAKE_SYNC_SIGNAL")
    if not path:
        raise ScriptError("wait-sync 要有 FAKE_SYNC_SIGNAL 指到一個檔案"
                          "（agora 每做完一次寫回會在那裡加一行）")
    target = Path(path)
    before = lines(target)
    seconds = timeout()
    deadline = time.monotonic() + seconds
    while lines(target) <= before:
        if time.monotonic() >= deadline:
            raise ScriptError(f"等不到寫回：{seconds:g} 秒內 {path} 沒有變多一行"
                              f"（還是 {before} 行）")
        time.sleep(POLL_SECONDS)
    return 1


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