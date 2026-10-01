#!/usr/bin/env python3
"""Non-interactive stand-in for the interactive `opencode` in e2e tests.

`agora continue-session --agent opencode` opens the TUI (`opencode --session
<id>`), which no test can drive. AGORA_OPENCODE_CMD points here instead, and
this wrapper translates the TUI form into the headless one:

    [--session <id>]  ->  opencode run -s <id> -m <model> <fixed question>

`--version`, `export <id>`, `import <file>` and `session delete <id>` are passed
straight through, because the adapter uses them for real.

Every received argv is appended (JSON) to $FAKE_HOME/opencode-e2e-args.log so the
test can see which session ids agora minted and when. HOME is reset to
$AGORA_REAL_HOME for every child, because pytest's sandbox HOME must not hide the
real opencode store (the auth lives there) - and because a sandboxed HOME would
make `export` report "Session not found".

Prompts are fixed self-made filler; nothing here reads a real transcript.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

MODEL = os.environ.get("AGORA_E2E_MODEL", "opencode/space-bunny-free")
QUESTION = "你前面在做什麼？用一句話回答。不要呼叫任何工具、不要寫檔案。"
TIMEOUT = int(os.environ.get("AGORA_E2E_TIMEOUT", "300"))


def main(argv: list[str]) -> int:
    fake_home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-home"))
    fake_home.mkdir(parents=True, exist_ok=True)
    with open(fake_home / "opencode-e2e-args.log", "a") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")

    real = shutil.which("opencode") or os.environ.get("AGORA_REAL_OPENCODE")
    if real is None:
        print("real opencode not on PATH", file=sys.stderr)
        return 1

    child = [real]
    if "--session" in argv:
        # The TUI form: swap in a headless run on the same session id.
        session_id = argv[argv.index("--session") + 1]
        child += ["run", "-s", session_id, "-m", MODEL, QUESTION]
    else:
        child += argv

    env = {**os.environ}
    if env.get("AGORA_REAL_HOME"):
        env["HOME"] = env["AGORA_REAL_HOME"]
    # opencode resolves the project from $PWD, not from the process cwd (measured):
    # with cwd=X and a stale $PWD=Y, `opencode run` files the session under Y while
    # `opencode import` files it under X. A shell would have kept them in step.
    env["PWD"] = os.getcwd()
    proc = subprocess.run(child, cwd=os.getcwd(), env=env, timeout=TIMEOUT)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))