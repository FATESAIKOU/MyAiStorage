"""Child-process helpers shared by the real store and the drivers (section 3).

Design D3: the child's environment is built from an allow-list plus exactly
what `env_for` supplied - the sidecar's own `ANTHROPIC_*` / `OPENAI_*`
variables (and anything else credential-shaped) must not leak into the child.
`AGORA_*` carries the sidecar's own plumbing, `XDG_*` is where the agents keep
their databases, `LC_*` is locale; `FAKE_*` exists only so the unit-test fakes
can see their own knobs.

`terminate_process` is the gentle stop from D6: SIGTERM (the agora process
forwards to the agent, writes the last conversation back and releases the
lock), escalation to SIGKILL after `timeout`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

KEEP_ENV = frozenset(
    {"PATH", "HOME", "LANG", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM", "TZ"}
)
KEEP_PREFIXES = ("AGORA_", "XDG_", "LC_", "FAKE_")

#: How long to wait after SIGKILL before giving up on a D-state process.
KILL_WAIT_SECONDS = 5.0


def child_env(env: dict[str, str]) -> dict[str, str]:
    """The complete environment for an agent process: allow-list + `env`."""
    merged = {
        key: value
        for key, value in os.environ.items()
        if key in KEEP_ENV or key.startswith(KEEP_PREFIXES)
    }
    merged.update(env)
    return merged


def terminate_process(
    proc: Any, *, timeout: float, kill_wait: float = KILL_WAIT_SECONDS
) -> int | None:
    """Stop a child gently, then forcibly; returns its exit code (None if stuck).

    The caller's `timeout` is the grace period for the gentle stop - for agora
    that is `close_timeout`, the time the final write-back gets (D6).
    """
    if proc.poll() is not None:
        return proc.returncode
    try:
        proc.send_signal(signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        return proc.wait(timeout=kill_wait)
    except subprocess.TimeoutExpired:
        return None
