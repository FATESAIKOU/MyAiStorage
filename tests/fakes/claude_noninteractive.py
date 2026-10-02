#!/usr/bin/env python3
"""Non-interactive stand-in for the interactive `claude` in e2e tests.

AGORA_CLAUDE_CMD points here, so `agora continue session` spawns this instead
of the TUI. Mapping (fixed self-made prompts only):

  [..., --resume <id>]            -> real `claude --resume <id> -p <P_NATIVE>`
  [..., --session-id <id>, <prompt>] -> real `claude --session-id <id> -p <prompt>`
  [-p, ..., --no-session-persistence, ...] (merge's summary) -> the same argv on the
                                   real claude with --model haiku, stdin passed through

Every received argv is appended (JSON) to $FAKE_HOME/e2e-args.log so the test
can tell native apart from reading-injected launches. HOME is reset to
$AGORA_CLAUDE_HOME for the child, because pytest's sandbox HOME must not leak
into the real Claude session directory.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

P_NATIVE = "你前面在做什麼？用一句話回答。不要呼叫任何工具、不要寫檔案。"
GUARDS = ["--disallowedTools",
          "Bash Read Glob Grep Edit Write WebFetch WebSearch Task",
          "--model", "haiku"]


def main(argv: list[str]) -> int:
    fake_home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-home"))
    fake_home.mkdir(parents=True, exist_ok=True)
    with open(fake_home / "e2e-args.log", "a") as f:
        f.write(json.dumps(argv) + "\n")

    real = shutil.which("claude")
    if real is None:
        print("real claude not on PATH", file=sys.stderr)
        return 1
    if argv[:1] == ["--version"]:
        child = [real, "--version"]
        proc = subprocess.run(child, capture_output=True, text=True, timeout=60)
        sys.stdout.write(proc.stdout)
        return proc.returncode
    if "-p" in argv and "--no-session-persistence" in argv:   # summarize: already headless, no tools
        child = [real, *argv, "--model", "haiku"]
    elif "--resume" in argv:
        sid = argv[argv.index("--resume") + 1]
        child = [real, *GUARDS, "--resume", sid, "-p", P_NATIVE]
    elif "--session-id" in argv:
        sid = argv[argv.index("--session-id") + 1]
        prompt = argv[argv.index("--session-id") + 2]
        child = [real, *GUARDS, "--session-id", sid, "-p", prompt]
    else:
        print(f"wrapper does not support {argv}", file=sys.stderr)
        return 2
    env = {**os.environ, "HOME": os.environ["AGORA_CLAUDE_HOME"]}
    proc = subprocess.run(child, cwd=os.getcwd(), env=env, timeout=300)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
