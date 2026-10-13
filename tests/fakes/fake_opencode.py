#!/usr/bin/env python3
"""A fake opencode for unit tests (test-plan 0.3).

It keeps "sessions" as JSON files under $FAKE_HOME/opencode-sessions/<id>.json,
so tests can see exactly what import was handed. Calls are appended to
$FAKE_HOME/opencode-calls.log and the working directory of every call to
$FAKE_HOME/opencode-cwd.log.

Subcommands / flags:
    --version                 print a fake version
    export <id> / session export <id>    the stored JSON for <id> on stdout,
                              progress on stderr (like the real one); rc=1 +
                              "Session not found" for an id that was never imported
    import <file> / session import <file> [--directory <dir>]
                              store the file under its own session id; with
                              --directory, the session lands in that directory
                              the way opencode 2.x rewrites it
    session delete <id>       forget it
    serve --hostname H --port <p>  the headless server mode: listen on the port;
                              on SIGTERM append one pair to the newest stored
                              session (the handoff agora just imported), then
                              exit 0 - what lets a test see the final write-back
    --session <id>            the TUI: act on <id> per FAKE_AGENT_MODE
    run [--session <id>]      the same, non-interactive
    run -m <model> <text...>  a headless run; -s/--session means "append to <id>"

`--standalone` is accepted and ignored everywhere opencode 2.x takes it.

Sessions are stored as they were imported: a v1 payload (nested `info`/`parts`
rows) stays nested, a v2 payload (`type` rows, `content` inside them) stays flat,
and `say()` appends rows in whichever shape the session already is.

FAKE_AGENT_MODE:
    append      add one user + one assistant message to the session
    noop        change nothing
    crash       exit 2
    sleep:<n>   sleep n seconds first, then behave as append
    ignore-int  ignore SIGINT while sleeping, then append
FAKE_AGENT_SCRIPT=append:第一句,wait-sync,partial,wait-sync,append:第二句,exit
               play that script instead of FAKE_AGENT_MODE (see _script.py): say
               something, wait for a periodic write-back, leave the session caught
               mid-message, say something else, leave. FAKE_SYNC_SIGNAL names the
               file `wait-sync` watches for one more line in. Only for a call that
               names a session - a headless `run` (summarize) is untouched.
FAKE_OPENCODE_PROBE_STDIN=<path>  on export, write whatever is on stdin to that
                         file: what proves our export is not reading the terminal
FAKE_OPENCODE_FAIL=export|import|delete  make that subcommand exit 1
FAKE_OPENCODE_DROP=<n>   import the payload but keep only the first <n> messages
                         (simulates opencode's silent onConflictDoNothing)
FAKE_OPENCODE_DROP_PART=<n>  drop the <n>th part of every message but keep the
                         messages: what a part-id collision looks like
FAKE_OPENCODE_DROP_CONTENT=<n>  the v2 analogue (M2): drop the <n>th content item
                         of every assistant row but keep the rows
FAKE_OPENCODE_SLEEP=<s>  sleep before doing anything (for timeout tests)
FAKE_OPENCODE_LEAVE=<id> import refuses: "Expected a string starting with msg"
FAKE_OPENCODE_EXPORT_SCOPE=project  export only finds sessions of $PWD's project
FAKE_OPENCODE_REQUIRE_V2  how the v1 nested payload is treated: the 2.x spelling
                         (`session import`) refuses it by default with
                         `SchemaError: Missing key at ["info"]["location"]`, like
                         the real 2.x; the 1.x spelling (`import`) accepts it, so
                         the adapter's v1 write path (step 2 replaces it) stays
                         testable. `=1` refuses on both spellings, `=0` on neither.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _script  # the FAKE_AGENT_SCRIPT player, shared with fake_claude

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
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd(),
                         "pwd": os.environ.get("PWD"),
                         "mode": os.environ.get("FAKE_AGENT_MODE", "")}) + "\n")
with open(CWDS, "a") as f:
    f.write(os.getcwd() + "\n")

STORE.mkdir(parents=True, exist_ok=True)


def die(message: str, code: int = 1):
    print(message, file=sys.stderr)
    sys.exit(code)


def path_of(session_id: str) -> Path:
    return STORE / f"{session_id}.json"


def positional(argv: list[str], flags_with_values: tuple[str, ...] = ()) -> list[str]:
    """The arguments that are neither a flag nor a flag's value."""
    out: list[str] = []
    skip = 0
    for arg in argv:
        if skip:
            skip -= 1
            continue
        if arg in flags_with_values:
            skip = 1
            continue
        if arg.startswith("-"):
            continue
        out.append(arg)
    return out


def flag_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def load(session_id: str) -> dict:
    target = path_of(session_id)
    if not target.exists():
        die(f"Error: Session not found: {session_id}")
    return json.loads(target.read_text(encoding="utf-8"))


def save(session_id: str, payload: dict):
    path_of(session_id).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def is_v2(payload: dict) -> bool:
    """v2 is the flat shape: a row with `type` and no nested `info`."""
    rows = payload.get("messages") or []
    return any(isinstance(row, dict) and isinstance(row.get("type"), str) for row in rows) and \
        not any(isinstance(row, dict) and isinstance(row.get("info"), dict) for row in rows)


def require_v2() -> bool:
    """Whether this call must refuse a v1 payload: yes for the 2.x spelling
    (`session import`), unless the variable is `0`; `1` refuses either spelling."""
    value = os.environ.get("FAKE_OPENCODE_REQUIRE_V2")
    if value == "0":
        return False
    return value == "1" or argv[:2] == ["session", "import"]


def directory_of(payload: dict) -> str | None:
    info = payload.get("info") or {}
    location = info.get("location")
    if isinstance(location, dict):
        return location.get("directory")
    return info.get("directory")


def set_directory(payload: dict, directory: str) -> None:
    info = payload.setdefault("info", {})
    location = info.get("location")
    if isinstance(location, dict):
        location["directory"] = directory
    else:
        info["directory"] = directory


def act(session_id: str):
    script = os.environ.get("FAKE_AGENT_SCRIPT")
    if script:
        act_scripted(session_id, script)   # the script replaces FAKE_AGENT_MODE
        return
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
    say(session_id, "ZZAPPEND 使用者又問了一句。")


def say(session_id: str, text: str) -> None:
    payload = load(session_id)
    now = int(time.time() * 1000)
    base = len(payload["messages"])
    if is_v2(payload):
        payload["messages"].extend([
            {"id": f"msg_FAKE{base:04d}a", "time": {"created": now}, "text": text,
             "files": [], "type": "user"},
            {"id": f"msg_FAKE{base:04d}b", "time": {"created": now + 1},
             "type": "assistant", "agent": "build",
             "model": {"id": os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free"),
                       "providerID": "fakeup", "variant": "default"},
             "content": [{"type": "text", "text": "ZZAPPEND 助手多說了一句。"}],
             "finish": "stop", "rawFinish": "stop", "cost": 0,
             "tokens": {"input": 0, "output": 0, "reasoning": 0,
                        "cache": {"read": 0, "write": 0}}},
            {"id": f"msg_FAKE{base:04d}c", "time": {"created": now + 2},
             "type": "idle", "outcome": "succeeded"},
        ])
        save(session_id, payload)
        return
    payload["messages"].extend([
        {"info": {"id": f"msg_FAKE{base:04d}a", "sessionID": session_id, "role": "user",
                  "time": {"created": now}},
         "parts": [{"id": f"prt_FAKE{base:04d}a", "sessionID": session_id,
                    "messageID": f"msg_FAKE{base:04d}a", "type": "text",
                    "text": text}]},
        {"info": {"id": f"msg_FAKE{base:04d}b", "sessionID": session_id, "role": "assistant",
                  "parentID": f"msg_FAKE{base:04d}a", "time": {"created": now + 1},
                  "providerID": "opencode",
                  "modelID": os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free")},
         "parts": [{"id": f"prt_FAKE{base:04d}b", "sessionID": session_id,
                    "messageID": f"msg_FAKE{base:04d}b", "type": "text",
                    "text": "ZZAPPEND 助手多說了一句。"}]},
    ])
    save(session_id, payload)


def mid_message(session_id: str) -> None:
    """What a session looks like while a tool is still running: the call is there,
    its state says `running`, there is no output and no `time.completed`.

    The reading version turns it into one `[tool] …` line, so a write-back that lands
    here is a conversation stopped in the middle of a tool call - which the fork path
    has to cope with, and the next write-back overwrites whole (design「讀到一半不能
    寫壞」).
    """
    payload = load(session_id)
    now = int(time.time() * 1000)
    base = len(payload["messages"])
    message_id = f"msg_FAKEPART{base:04d}"
    if is_v2(payload):
        payload["messages"].append({
            "id": message_id, "time": {"created": now}, "type": "assistant",
            "agent": "build",
            "model": {"id": os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free"),
                      "providerID": "fakeup", "variant": "default"},
            "content": [{"type": "tool", "id": f"call_FAKEPART{base:04d}",
                         "name": "bash", "executed": false,
                         "state": {"status": "running",
                                   "input": {"command": "sleep 3600"}},
                         "time": {"created": now}}],
            "finish": "tool-calls", "rawFinish": "tool_calls", "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}}})
        save(session_id, payload)
        return
    previous = next((m["info"]["id"] for m in reversed(payload["messages"])
                     if m.get("info", {}).get("role") == "user"), "")
    payload["messages"].append({
        "info": {"id": message_id, "sessionID": session_id, "role": "assistant",
                 "parentID": previous, "time": {"created": now},
                 "providerID": "opencode",
                 "modelID": os.environ.get("FAKE_OPENCODE_MODEL", "space-bunny-free")},
        "parts": [{"id": f"prt_FAKEPART{base:04d}", "sessionID": session_id,
                   "messageID": message_id, "type": "tool",
                   "callID": f"call_FAKEPART{base:04d}", "tool": "bash",
                   "time": {"start": now},
                   "state": {"status": "running",
                             "input": {"command": "sleep 3600"}}}]})
    save(session_id, payload)


def act_scripted(session_id: str, script: str) -> None:
    _script.run(script,
                lambda text: say(session_id, text),
                lambda: mid_message(session_id))


def do_export(params: list[str]):
    if os.environ.get("FAKE_OPENCODE_FAIL") == "export":
        die("injected export failure")
    probe = os.environ.get("FAKE_OPENCODE_PROBE_STDIN")
    if probe:
        # Read whatever is on stdin - an empty read means our caller gave us
        # /dev/null, which is what an export must get: during a periodic write-back
        # the terminal belongs to the agent, and a read there eats the user's line.
        with open(probe, "a") as f:
            f.write(sys.stdin.read())
    session_id = positional(params)[0]
    if os.environ.get("FAKE_OPENCODE_EXPORT_SCOPE") == "project":
        # An opencode that only knows the current project's sessions - what
        # `opencode session list` does, if export ever followed it.
        stored = load(session_id)
        if os.environ.get("PWD", "") != directory_of(stored):
            die(f"Error: Session not found: {session_id}")
    print(f"Exporting session: {session_id}", file=sys.stderr)
    sys.stdout.write(json.dumps(load(session_id), ensure_ascii=False))


def _missing(at: str) -> None:
    """Die like opencode 2.x does when a required field is absent (2.1).

    Measured 2.0.26 in isolation (`session import --standalone --directory`):
    every absent field is `SchemaError: Missing key at [...]`, one line per
    field, and a bad id prefix is `Expected a string starting with ...`.
    The fake keeps the single-line shape the v1 refusal already uses, so the
    existing assertion (`in stderr`) keeps working.
    """
    die(f"SchemaError: Missing key at {at}")


def _bad_prefix(want: str, at: str, got: object) -> None:
    die(f'SchemaError: Expected a string starting with "{want}" at {at} (got {got!r})')


def validate_v2(payload: dict) -> None:
    """Reject a v2 payload missing a field the real 2.x import insists on.

    The full set was probed field by field against 2.0.26 in an isolated
    XDG+HOME (port moved off 49374 first); see docs/spike/opencode.md §2.1.
    Optional: info.model/outcome/title, info.time.idle, user.files,
    assistant.time.completed/streamed, snapshot/cost/tokens/finish/rawFinish,
    model.variant, tool.executed/ran/completed, state.metadata, empty text.
    Everything else below is required; idle rows are markers but still carry
    id/time/outcome.
    """
    info = payload.get("info")
    if not isinstance(info, dict):
        _missing('["info"]')
    assert isinstance(info, dict)
    for key in ("id", "cost", "location", "projectID", "time", "tokens"):
        if key not in info:
            _missing(f'["info"]["{key}"]')
    sid = info["id"]
    if not (isinstance(sid, str) and sid.startswith("ses")):
        _bad_prefix("ses", '["info"]["id"]', sid)
    location = info["location"]
    if not isinstance(location, dict) or "directory" not in location:
        _missing('["info"]["location"]["directory"]')
    clock = info["time"]
    if not isinstance(clock, dict):
        _missing('["info"]["time"]')
    assert isinstance(clock, dict)
    for key in ("created", "updated"):
        if key not in clock:
            _missing(f'["info"]["time"]["{key}"]')
    tokens = info["tokens"]
    if not isinstance(tokens, dict):
        _missing('["info"]["tokens"]')
    assert isinstance(tokens, dict)
    for key in ("input", "output", "reasoning", "cache"):
        if key not in tokens:
            _missing(f'["info"]["tokens"]["{key}"]')
    cache = tokens["cache"]
    if not isinstance(cache, dict):
        _missing('["info"]["tokens"]["cache"]')
    assert isinstance(cache, dict)
    for key in ("read", "write"):
        if key not in cache:
            _missing(f'["info"]["tokens"]["cache"]["{key}"]')
    messages = payload.get("messages")
    if not isinstance(messages, list):
        _missing('["messages"]')
    assert isinstance(messages, list)
    for n, row in enumerate(messages):
        if not isinstance(row, dict):
            _missing(f'["messages"][{n}]')
        assert isinstance(row, dict)
        for key in ("id", "type", "time"):
            if key not in row:
                _missing(f'["messages"][{n}]["{key}"]')
        mid = row["id"]
        if not (isinstance(mid, str) and mid.startswith("msg_")):
            _bad_prefix("msg_", f'["messages"][{n}]["id"]', mid)
        kind = row["type"]
        if kind not in ("user", "assistant", "idle"):
            die(f"SchemaError: Expected Session.Message.Info at [\"messages\"][{n}]")
        tick = row["time"]
        if not isinstance(tick, dict) or "created" not in tick:
            _missing(f'["messages"][{n}]["time"]["created"]')
        if kind == "user":
            if "text" not in row:
                _missing(f'["messages"][{n}]["text"]')
        elif kind == "assistant":
            # `time.completed` is not a SchemaError when absent (the real import
            # returns 0) but the row is then silently dropped - the read-back
            # count is what notices. The fake refuses out loud instead, so a
            # payload missing it fails here rather than passing the fake and
            # failing the real import's verification.
            if "completed" not in tick:
                _missing(f'["messages"][{n}]["time"]["completed"]')
            for key in ("agent", "model", "content"):
                if key not in row:
                    _missing(f'["messages"][{n}]["{key}"]')
            model = row["model"]
            if not isinstance(model, dict):
                _missing(f'["messages"][{n}]["model"]')
            assert isinstance(model, dict)
            for key in ("id", "providerID"):
                if key not in model:
                    _missing(f'["messages"][{n}]["model"]["{key}"]')
            content = row["content"]
            if not isinstance(content, list):
                _missing(f'["messages"][{n}]["content"]')
            assert isinstance(content, list)
            for k, item in enumerate(content):
                if not isinstance(item, dict) or "type" not in item:
                    _missing(f'["messages"][{n}]["content"][{k}]["type"]')
                assert isinstance(item, dict)
                if item["type"] == "text":
                    if "text" not in item:
                        _missing(f'["messages"][{n}]["content"][{k}]["text"]')
                elif item["type"] == "tool":
                    for key in ("id", "name", "state", "time"):
                        if key not in item:
                            _missing(f'["messages"][{n}]["content"][{k}]["{key}"]')
                    state = item["state"]
                    if not isinstance(state, dict):
                        _missing(f'["messages"][{n}]["content"][{k}]["state"]')
                    assert isinstance(state, dict)
                    for key in ("status", "input", "content"):
                        if key not in state:
                            _missing(f'["messages"][{n}]["content"][{k}]["state"]["{key}"]')
                    inner = state["content"]
                    if not isinstance(inner, list) or not inner:
                        _missing(f'["messages"][{n}]["content"][{k}]["state"]["content"][0]')
                    for j, bit in enumerate(inner):
                        if not isinstance(bit, dict) or "text" not in bit:
                            _missing(f'["messages"][{n}]["content"][{k}]["state"]["content"][{j}]["text"]')
                    tick = item["time"]
                    if not isinstance(tick, dict) or "created" not in tick:
                        _missing(f'["messages"][{n}]["content"][{k}]["time"]["created"]')
        else:  # idle: a marker, but still a row with an outcome
            if "outcome" not in row:
                _missing(f'["messages"][{n}]["outcome"]')


def do_import(params: list[str]):
    if os.environ.get("FAKE_OPENCODE_FAIL") == "import":
        die("injected import failure")
    if os.environ.get("FAKE_OPENCODE_LEAVE"):
        die('Error: Unexpected error\n\nExpected a string starting with "msg", got "m0"')
    payload = json.loads(Path(positional(params, ("--directory",))[0]).read_text(encoding="utf-8"))
    if not is_v2(payload) and require_v2():
        # What opencode 2.0.26 answers to a v1 nested payload (spike 1.2 §A). The
        # refusal follows the syntax: `session import` is 2.x's spelling and 2.x
        # has no v1 payload, while the 1.x spelling (`import`) keeps accepting one
        # so the adapter's v1 write path (step 2 replaces it) stays testable.
        # `FAKE_OPENCODE_REQUIRE_V2=1` refuses on both spellings, `=0` on neither.
        die('SchemaError: Missing key at ["info"]["location"]')
    if is_v2(payload):
        validate_v2(payload)

    directory = flag_value(params, "--directory")
    if directory:
        set_directory(payload, directory)
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
    lose_content = os.environ.get("FAKE_OPENCODE_DROP_CONTENT")
    if lose_content:
        # The v2 analogue of DROP_PART (M2): drop one content item but keep the
        # rows, so the message count still matches and only the second layer
        # notices - what a content-id collision looks like.
        for message in payload["messages"]:
            content = message.get("content") or []
            if len(content) > int(lose_content):
                del content[int(lose_content)]
    save(session_id, payload)
    print(f"Imported session: {session_id}")


def summarize_run(argv: list[str]) -> int:
    """`opencode run <message> [-f file]` with no session: what summarize() does.

    Emits the same event stream as the real one (one JSON object per line), and
    stores a session so `export` can be asked which model answered.
    FAKE_OPENCODE_SUMMARIZE: normal | empty | fail | no-session
    FAKE_OPENCODE_SUMMARIZE_HOLD=1  after the first event, wait for workdir/pending-<id>
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
                      "timestamp": now, "part": {"type": "step-start"}}), flush=True)
    if os.environ.get("FAKE_OPENCODE_SUMMARIZE_HOLD"):
        # Only answer once workdir/pending-<id> is there: if the adapter waits for
        # the run to end before recording it, this never arrives (review V5).
        record = Path.cwd() / f"pending-{session_id}"
        for _ in range(300):
            if record.exists():
                break
            time.sleep(0.05)
        else:
            die("the pending record never appeared")
    if mode != "empty":
        print(json.dumps({"type": "text", "sessionID": session_id, "timestamp": now,
                          "part": {"type": "text",
                                   "text": os.environ.get("FAKE_OPENCODE_REPLY",
                                                           "ZZSUMMARY 這是要約")}}))
    print(json.dumps({"type": "step_finish", "sessionID": session_id,
                      "timestamp": now, "part": {"type": "step-finish"}}))
    return 0


def serve() -> int:
    """`opencode serve`: listen on --port until SIGTERM, then append and stop.

    The append simulates the server writing into its session while it runs, so
    the caller's final write-back has something new to collect. The session is
    the newest one in the store - the handoff `agora continue` just imported.
    """
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
    host = argv[argv.index("--hostname") + 1] if "--hostname" in argv else "127.0.0.1"

    def _stop(signum, frame):
        candidates = sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if candidates:
            say(candidates[-1].stem, "ZZSERVE 伺服器收尾前說了最後一句。")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    with socket.socket() as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(8)
        srv.settimeout(0.2)
        print(f"opencode server listening on http://{host}:{srv.getsockname()[1]}",
              file=sys.stderr, flush=True)
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
            except socket.timeout:
                continue


def main() -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if argv[:1] == ["serve"]:
        return serve()
    if argv[:2] == ["session", "export"]:
        do_export(argv[2:])
        return 0
    if argv[:1] == ["export"]:
        do_export(argv[1:])
        return 0
    if argv[:2] == ["session", "import"]:
        do_import(argv[2:])
        return 0
    if argv[:1] == ["import"]:
        do_import(argv[1:])
        return 0
    if argv[:2] == ["session", "delete"]:
        if os.environ.get("FAKE_OPENCODE_FAIL") == "delete":
            die("injected delete failure")
        target_id = positional(argv[2:])[0]
        target = path_of(target_id)
        if target.exists():
            target.unlink()
            print(f"Session {target_id} deleted")
        else:
            die(f"Error: Session not found: {target_id}")
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