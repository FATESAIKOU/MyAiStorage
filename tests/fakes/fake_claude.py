#!/usr/bin/env python3
"""A fake claude CLI for unit tests (test-plan 0.3).

--version prints a version number (AGORA agent_version reads it).
--resume <id> appends one user + one assistant line to
$AGORA_CLAUDE_HOME/.claude/projects/<encoded pwd>/<id>.jsonl, unless
FAKE_AGENT_MODE=noop. Every call appends its argv + pwd to
$FAKE_HOME/claude-args.log / claude-cwd.log.
--input-format stream-json (headless) also drains stdin into
$FAKE_HOME/claude-stream.log, so a test can prove the pipe is the caller's.

With FAKE_AGENT_SCRIPT set, --resume plays that script instead (say something, wait
for a periodic write-back, leave a half-written line, say something else, leave) -
see _script.py. No script, no change to anything below.
"""

import datetime
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agora.agents.claude import encode_project_dir as encode  # same rule (CL1)

import _script  # the FAKE_AGENT_SCRIPT player, shared with fake_opencode


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


args = sys.argv[1:]
fake_home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-home"))
fake_home.mkdir(parents=True, exist_ok=True)
with open(fake_home / "claude-args.log", "a") as f:
    f.write(json.dumps(args) + "\n")
with open(fake_home / "claude-cwd.log", "a") as f:
    f.write(os.getcwd() + "\n")

if args[:1] == ["--version"]:
    print(os.environ.get("FAKE_CLAUDE_VERSION", "9.9.9 (Fake Claude)"))
    sys.exit(0)

if "-p" in args and "--resume" not in args and "--session-id" not in args:
    # summarize(): the prompt arrives on stdin (Y1), output is JSON on stdout.
    prompt = sys.stdin.read()
    with open(fake_home / "claude-stdin.log", "a") as f:
        f.write(prompt + "\n")
    mode = os.environ.get("FAKE_SUMMARIZE_MODE", "ok")
    if mode == "fail":
        print("fake failure", file=sys.stderr)
        sys.exit(1)
    if mode == "empty":
        print(json.dumps({"result": "", "modelUsage": {}}))
        sys.exit(0)
    if mode == "garbage":
        print("not json at all")
        sys.exit(0)
    sid = str(uuid.uuid4())
    print(json.dumps({"result": "ZZSUM 這是自編的要約", "session_id": sid,
                      "modelUsage": {"zz-model": {"inputTokens": 1}}}))
    sys.exit(0)

if "--resume" in args:
    sid = args[args.index("--resume") + 1]
    if "--input-format" in args and not sys.stdin.isatty():
        # Headless (`agora continue --headless`, design D6): the caller writes
        # stream-json user messages on stdin; consume them (until EOF) and log
        # them, so a test can prove the pipe really belongs to the caller.
        # A terminal is not a stream-json pipe, so leave that case alone.
        with open(fake_home / "claude-stream.log", "a") as f:
            for line in sys.stdin:
                f.write(line)
    path = (Path(os.environ["AGORA_CLAUDE_HOME"]) / ".claude" / "projects"
            / encode(os.getcwd()) / f"{sid}.jsonl")
    script = os.environ.get("FAKE_AGENT_SCRIPT")
    if not script and os.environ.get("FAKE_AGENT_MODE", "append") == "noop":
        sys.exit(0)
    if not path.is_file():
        fail(f"no such session: {sid}")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cwd = os.getcwd()

    def leaf_uuid() -> str | None:
        """The uuid the next line hangs off: the last one on disk that has one."""
        last = None
        with open(path) as f:
            for line in f:
                try:
                    last = json.loads(line).get("uuid") or last
                except ValueError:
                    pass
        return last

    def say(text: str) -> None:
        user = {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
                "parentUuid": leaf_uuid(), "timestamp": now, "cwd": cwd,
                "message": {"role": "user", "content": text}}
        assistant = {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
                     "parentUuid": user["uuid"], "timestamp": now, "cwd": cwd,
                     "message": {"role": "assistant", "model": "zz-fake-model",
                                 "content": [{"type": "text", "text": "ZZSAY 好的"}]}}
        with open(path, "a") as f:
            f.write(json.dumps(user, ensure_ascii=False) + "\n")
            f.write(json.dumps(assistant, ensure_ascii=False) + "\n")

    def half_written() -> None:
        """What a session file looks like while it is being written: a JSON object
        cut in half, with no newline after it. The reader has to drop that line
        (review S10) rather than choke on it or keep half an object."""
        started = json.dumps(
            {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
             "parentUuid": leaf_uuid(), "timestamp": now, "cwd": cwd,
             "message": {"role": "user", "content": "ZZPARTIAL 寫到一半的這一則"}},
            ensure_ascii=False)
        with open(path, "a") as f:
            f.write(started[:len(started) // 2])

    script = os.environ.get("FAKE_AGENT_SCRIPT")
    if script:
        _script.run(script, say, half_written)
        sys.exit(0)
    say("ZZSAY 再補一句")
    print("ZZSAY 好的")
    sys.exit(0)

if "--session-id" in args:
    print("ZZSAY injected")
    sys.exit(0)

fail(f"fake claude does not support {args}")
