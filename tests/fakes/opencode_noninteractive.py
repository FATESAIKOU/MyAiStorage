#!/usr/bin/env python3
"""Non-interactive stand-in for the interactive `opencode` in e2e tests.

`agora continue-session --agent opencode` opens the TUI (`opencode --session
<id>`), which no test can drive. AGORA_OPENCODE_CMD points here instead, and
this wrapper translates the TUI form into the headless one:

    [--session <id>]  ->  opencode run -s <id> -m <model> <fixed question>

`--version`, `export <id>`, `import <file>` and `session delete <id>` are passed
straight through, because the adapter uses them for real.

Every received argv is appended (JSON) to $FAKE_HOME/opencode-e2e-args.log, with
the model that answered, so the test can see which session ids agora minted and
which model the free endpoint managed today.

HOME is left alone unless AGORA_REAL_HOME is set: the integration tests want the
sandboxed HOME, so this file never touches the real store. PWD is set to the
process cwd, because opencode resolves its project from $PWD while `opencode
import` uses the working directory - a shell keeps them in step, a subprocess
does not. Every `opencode run` gets OPENCODE_PERMISSION={"*":"deny"}, so a model
cannot read anything even if a prompt asks it to.

This module is also the single definition of which models the tests may use
(`model_chain`, `ask`), imported by tests/integration/test_opencode_real.py and
test_e2e_opencode.py so the order lives in one place.

Prompts are fixed self-made filler; nothing here reads a real transcript.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

#: Tried in order. The first two are opencode's free endpoints; the third is only
#: used when this machine actually has that provider configured.
PRIMARY = "opencode/space-bunny-free"
FALLBACK = "opencode/muse-spark-1.3-contributor-free"
THIRD_PARTY = ("ollama-cloud/deepseek-v4.1-flash", ["--variant", "max"])

QUESTION = "你前面在做什麼？用一句話回答。不要呼叫任何工具、不要寫檔案。"
TIMEOUT = int(os.environ.get("AGORA_E2E_TIMEOUT", "300"))

#: P3: every `opencode run` from a test gets its tools denied by configuration, not
#: by asking nicely in the prompt. Measured: without it the model calls `read` on
#: any absolute path it is given (the baseline run in docs/spike/opencode.md); with
#: it the model does not even attempt the call. Not overridable on purpose.
PERMISSION = '{"*":"deny"}'


def real_opencode() -> str | None:
    return shutil.which("opencode") or os.environ.get("AGORA_REAL_OPENCODE")


@functools.lru_cache(maxsize=None)
def models_of(provider: str) -> frozenset[str]:
    """The model ids this machine has for `provider` (empty if it has none)."""
    real = real_opencode()
    if real is None:
        return frozenset()
    try:
        proc = subprocess.run([real, "models", provider], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return frozenset((proc.stdout or "").split())


def model_chain() -> list[list[str]]:
    """`['-m', model, *extra]` per candidate, in the order they are tried.

    The third is only offered when this machine actually has that provider, so a
    checkout without it does not burn a timeout on it.
    """
    chain = [["-m", PRIMARY], ["-m", FALLBACK]]
    if THIRD_PARTY[0] in models_of(THIRD_PARTY[0].partition("/")[0]):
        chain.append(["-m", THIRD_PARTY[0], *THIRD_PARTY[1]])
    return chain


def ask(args: list[str], *, cwd, timeout: int | None = None,
        env: dict | None = None) -> tuple[subprocess.CompletedProcess | None, str | None,
                                         list[str]]:
    """`opencode run` against each candidate model in turn.

    Returns (process, model, failures). `process` is None only when every
    candidate was silent or errored, and `failures` then says which was tried and
    what each one did - a skip that does not say why is a test nobody trusts.
    """
    real = real_opencode()
    if real is None:
        return None, None, ["opencode 不在 PATH 上"]
    seconds = timeout or TIMEOUT
    child_env = {**(env or os.environ), "OPENCODE_PERMISSION": PERMISSION}
    failures: list[str] = []
    for flags in model_chain():
        model = flags[1]
        try:
            proc = subprocess.run([real, "run", *flags, *args], cwd=str(cwd),
                                  env=child_env, capture_output=True, text=True,
                                  timeout=seconds)
        except subprocess.TimeoutExpired:
            failures.append(f"{model}: 逾時（{seconds} 秒，沒有任何輸出）")
            continue
        if proc.returncode == 0:
            return proc, model, failures
        failures.append(f"{model}: rc={proc.returncode} "
                        f"{(proc.stderr or '').strip()[-120:]}")
    return None, None, failures


def skip_reason(failures: list[str]) -> str:
    return "所有免費模型都沒有回答：" + "；".join(failures)


def main(argv: list[str]) -> int:
    fake_home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-home"))
    fake_home.mkdir(parents=True, exist_ok=True)

    real = real_opencode()
    if real is None:
        print("real opencode not on PATH", file=sys.stderr)
        return 1

    if "--session" in argv:
        # The TUI form: swap in a headless run on the same session id.
        session_id = argv[argv.index("--session") + 1]
        env = {**os.environ, "OPENCODE_PERMISSION": PERMISSION}
        if env.get("AGORA_REAL_HOME"):
            env["HOME"] = env["AGORA_REAL_HOME"]
        env["PWD"] = os.getcwd()   # see the module docstring
        proc, model, failures = ask(["-s", session_id, QUESTION], cwd=os.getcwd(), env=env)
        with open(fake_home / "opencode-e2e-args.log", "a") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "model": model,
                                "failures": failures}) + "\n")
        if proc is None:
            print(skip_reason(failures), file=sys.stderr)
            return 1
        return 0

    with open(fake_home / "opencode-e2e-args.log", "a") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    env = {**os.environ, "OPENCODE_PERMISSION": PERMISSION}
    if env.get("AGORA_REAL_HOME"):
        env["HOME"] = env["AGORA_REAL_HOME"]
    env["PWD"] = os.getcwd()
    proc = subprocess.run([real, *argv], cwd=os.getcwd(), env=env, timeout=TIMEOUT)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))