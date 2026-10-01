#!/usr/bin/env python3
"""A fake claude CLI for unit tests (test-plan 0.3).

--version prints a version number (AGORA agent_version reads it).
--resume <id> appends one user + one assistant line to
$AGORA_CLAUDE_HOME/.claude/projects/<encoded pwd>/<id>.jsonl, unless
FAKE_AGENT_MODE=noop. Every call appends its argv + pwd to
$FAKE_HOME/claude-args.log / claude-cwd.log.
"""

import datetime
import json
import os
import sys
import uuid
from pathlib import Path


def encode(pwd: str) -> str:
    return os.path.abspath(pwd).replace("/", "-").replace(".", "-")


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

if "--resume" in args:
    sid = args[args.index("--resume") + 1]
    path = (Path(os.environ["AGORA_CLAUDE_HOME"]) / ".claude" / "projects"
            / encode(os.getcwd()) / f"{sid}.jsonl")
    if os.environ.get("FAKE_AGENT_MODE", "append") == "noop":
        sys.exit(0)
    if not path.is_file():
        fail(f"no such session: {sid}")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cwd = os.getcwd()
    last_uuid = None
    with open(path) as f:
        for line in f:
            try:
                last_uuid = json.loads(line).get("uuid") or last_uuid
            except ValueError:
                pass
    user = {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
            "parentUuid": last_uuid, "timestamp": now, "cwd": cwd,
            "message": {"role": "user", "content": "ZZSAY 再補一句"}}
    assistant = {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
                 "parentUuid": user["uuid"], "timestamp": now, "cwd": cwd,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "ZZSAY 好的"}]}}
    with open(path, "a") as f:
        f.write(json.dumps(user, ensure_ascii=False) + "\n")
        f.write(json.dumps(assistant, ensure_ascii=False) + "\n")
    print("ZZSAY 好的")
    sys.exit(0)

if "--session-id" in args:
    print("ZZSAY injected")
    sys.exit(0)

fail(f"fake claude does not support {args}")
