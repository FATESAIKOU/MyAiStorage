#!/usr/bin/env python3
"""A fake opencode for unit tests (test-plan 0.3).

It keeps "sessions" as JSON files under $FAKE_HOME/opencode-sessions/<id>.json,
so tests can see exactly what import was handed. Calls are appended to
$FAKE_HOME/opencode-calls.log and the working directory of every call to
$FAKE_HOME/opencode-cwd.log.

Subcommands / flags:
    --version                 print a fake version
    export <id>               the stored JSON for <id> on stdout, progress on
                              stderr (like the real one); rc=1 + "Session not
                              found" for an id that was never imported
    import <file>             store the file under its own session id
    session delete <id>       forget it
    --session <id>            the TUI: act on <id> per FAKE_AGENT_MODE
    run [--session <id>]      the same, non-interactive
    run -m <model> <text...>  a headless run; -s/--session means "append to <id>"

FAKE_AGENT_MODE:
    append      add one user + one assistant message to the session
    noop        change nothing
    crash       exit 2
    sleep:<n>   sleep <n> seconds first, then behave as append
    ignore-int  ignore SIGINT while sleeping, then append
FAKE_OPENCODE_FAIL=export|import|delete  make that subcommand exit 1
FAKE_OPENCODE_DROP=<n>   import the payload but keep only the first <n> messages
                         (simulates opencode's silent onConflictDoNothing)
FAKE_OPENCODE_DROP_PART=<n>  drop the <n>th part of every message but keep the
                         messages: what a part-id collision looks like
FAKE_OPENCODE_SLEEP=<s>  sleep before doing anything (for timeout tests)
FAKE_OPENCODE_LEAVE=<id> import refuses: "Expected a string starting with msg"
FAKE_OPENCODE_EXPORT_SCOPE=project  export only finds sessions of $PWD's project
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

HOME = Path(os.environ.get("FAKE_HOME", os.environ["HOME"]))
STORE = HOME / "opencode-sessions"
CALLS = HOME / "opencode-calls.log"
CWDS = HOME / "opencode-cwd.log"
VERSION = os.environ.get("FAKE_OPENCODE_VERSION", "9.9.9")

argv = sys.argv[1:]
sleep_for = os.environ.get("FAKE_OPENCODE_SLEEP")
if sleep_for:
    time.sleep(float(sleep_for))

CALLS.parent.mkdir(parents=True, exist_ok=True)
with open(CALLS, "a") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "mode": os.environ.get("FAKE_AGENT_MODE", "")}) + "\n")
with open(CWDS, "a") as f:
    f.write(os.getcwd() + "\n")

STORE.mkdir(parents=True, exist_ok=True)


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    sys.exit(code)


def path_of(session_id: str) -> Path:
    return STORE / f"{session_id}.json"


def load(session_id: str) -> dict:
    target = path_of(session_id)
    if not target.exists():
        die(f"Error: Session not found: {session_id}")
    return json.loads(target.read_text(encoding="utf-8"))


def save(session_id: str, payload: dict):
    path_of(session_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def act(session_id: str):
    mode = os.environ.get("FAKE_AGENT_MODE", "noop")
    head, _, tail = mode.partition(":")
    if head == "sleep":
        time.sleep(float(tail or 1))
        head = "append"
    elif head == "ignore-int":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        time.sleep(float(tail or 1))
        head = "append"
    if head == "crash":
        sys.exit(2)
    if head != "append":
        return
    payload = load(session_id)
    now = int(time.time() * 1000)
    base = len(payload["messages"])
    payload["messages"].extend([
        {"info": {"id": f"msg_FAKE{base:04d}a", "sessionID": session_id, "role": "user",
                  "time": {"created": now}},
         "parts": [{"id": f"prt_FAKE{base:04d}a", "sessionID": session_id,
                    "messageID": f"msg_FAKE{base:04d}a", "type": "text",
                    "text": "ZZAPPEND 使用者又問了一句。"}]},
        {"info": {"id": f"msg_FAKE{base:04d}b", "sessionID": session_id, "role": "assistant",
                  "parentID": f"msg_FAKE{base:04d}a", "time": {"created": now + 1},
                  "providerID": "opencode",
                  "modelID": os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free")},
         "parts": [{"id": f"prt_FAKE{base:04d}b", "sessionID": session_id,
                    "messageID": f"msg_FAKE{base:04d}b", "type": "text",
                    "text": "ZZAPPEND 助手多說了一句。"}]},
    ])
    save(session_id, payload)


def do_export(params: list[str]):
    if os.environ.get("FAKE_OPENCODE_FAIL") == "export":
        die("injected export failure")
    session_id = params[0]
    if os.environ.get("FAKE_OPENCODE_EXPORT_SCOPE") == "project":
        # An opencode that only knows the current project's sessions - what
        # `opencode session list` does, if export ever followed it.
        stored = load(session_id)
        if os.environ.get("PWD", "") != (stored.get("info") or {}).get("directory"):
            die(f"Error: Session not found: {session_id}")
    print(f"Exporting session: {session_id}", file=sys.stderr)
    sys.stdout.write(json.dumps(load(session_id), ensure_ascii=False))


def do_import(params: list[str]):
    if os.environ.get("FAKE_OPENCODE_FAIL") == "import":
        die("injected import failure")
    if os.environ.get("FAKE_OPENCODE_LEAVE"):
        die('Error: Unexpected error\n\nExpected a string starting with "msg", got "m0"')
    payload = json.loads(Path(params[0]).read_text(encoding="utf-8"))
    session_id = str((payload.get("info") or {}).get("id"))
    keep = os.environ.get("FAKE_OPENCODE_DROP")
    if keep:
        # What opencode does when an id already exists: keep the rows that fit,
        # drop the rest, report success.
        payload["messages"] = payload["messages"][: int(keep)]
    lose = os.environ.get("FAKE_OPENCODE_DROP_PART")
    if lose:
        for message in payload["messages"]:
            parts = message.get("parts") or []
            if len(parts) > int(lose):
                del parts[int(lose)]
    save(session_id, payload)
    print(f"Imported session: {session_id}")


def summarize_run(argv: list[str]) -> int:
    """`opencode run <message> [-f file]` with no session: what summarize() does.

    Emits the same event stream as the real one (one JSON object per line), and
    stores a session so `export` can be asked which model answered.
    FAKE_OPENCODE_SUMMARIZE: normal | empty | fail | no-session
    """
    with open(HOME / "opencode-argv.log", "a") as f:
        f.write(json.dumps(argv) + "\n")
    with open(HOME / "opencode-stdin.log", "ab") as f:   # the prompt, if it came in
        f.write(sys.stdin.buffer.read() if not sys.stdin.isatty() else b"")
        f.write(b"\n---8<---\n")
    mode = os.environ.get("FAKE_OPENCODE_SUMMARIZE", "normal")
    if mode == "fail":
        die("injected summarize failure")
    session_id = "ses_fake_summary00000"
    now = int(time.time() * 1000)
    if mode != "no-session":
        save(session_id, {
            "info": {"id": session_id, "title": "summarize", "directory": os.getcwd(),
                     "version": "9.9.9",
                     "model": {"id": os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free"),
                               "providerID": "opencode", "variant": "default"},
                     "time": {"created": now, "updated": now}},
            "messages": [{"info": {"id": "msg_fake0", "sessionID": session_id,
                                   "role": "assistant", "modelID":
                                   os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free"),
                                   "time": {"created": now}}, "parts": []}],
        })
    if mode == "no-session":       # a run that produced no events at all
        return 0
    print(json.dumps({"type": "step_start", "sessionID": session_id,
                      "timestamp": now, "part": {"type": "step-start"}}))
    if mode != "empty":
        print(json.dumps({"type": "text", "sessionID": session_id, "timestamp": now,
                          "part": {"type": "text",
                                   "text": os.environ.get("FAKE_OPENCODE_REPLY",
                                                           "ZZSUMMARY 這是要約")}}))
    print(json.dumps({"type": "step_finish", "sessionID": session_id,
                      "timestamp": now, "part": {"type": "step-finish"}}))
    return 0


def main() -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if argv[:1] == ["export"]:
        do_export(argv[1:])
        return 0
    if argv[:1] == ["import"]:
        do_import(argv[1:])
        return 0
    if argv[:1] == ["session"] and argv[1:2] == ["delete"]:
        if os.environ.get("FAKE_OPENCODE_FAIL") == "delete":
            die("injected delete failure")
        target = path_of(argv[2])
        if target.exists():
            target.unlink()
            print(f"Session {argv[2]} deleted")
        else:
            die(f"Error: Session not found: {argv[2]}")
        return 0

    target = None
    for flag in ("--session", "-s", "--continue"):
        if flag in argv:
            index = argv.index(flag)
            if index + 1 < len(argv):
                target = argv[index + 1]
    if argv[:1] == ["run"]:
        if target:
            act(target)
            return 0
        return summarize_run(argv)
    if target:  # the TUI
        act(target)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())