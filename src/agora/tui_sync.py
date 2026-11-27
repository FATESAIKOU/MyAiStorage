"""The interactive mode's background sync: one child process, its outcome, and when it is told (#491).

Standard library only. It does not import `tui`, `textual`, `cli`, `store` or
`sync_child`: the child is started by its module name, and its two line prefixes are
written out here (a test keeps them equal to `sync_child`'s).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DONE, OFFLINE, FAILED = "done", "offline", "failed"

# 與 sync_child 的兩個常數逐字相同；另寫一份，不 import sync_child（它頂層 import store）。
# 兩邊一致由測試鎖住（測試可以 import 兩個模組來比）。
RESULT = "AGORA-SYNC "
ERROR = "AGORA-SYNC-ERROR "

_DETAIL_MAX = 200


@dataclass(frozen=True)
class SyncOutcome:
    state: str                      # DONE | OFFLINE | FAILED
    detail: str = ""                # FAILED: the line that says why (at most 200 characters); else ""
    lines: tuple[str, ...] = ()     # every output line of the child (line ends removed)


def _cut(text: str) -> str:
    return text[:_DETAIL_MAX]


def _last_nonblank(lines: list[str]) -> str:
    for line in reversed(lines):
        if line.strip():
            return line
    return ""


def parse_outcome(code: int, lines: list[str]) -> SyncOutcome:
    """What a finished sync child says: its exit code and its output lines."""
    kept = tuple(lines)
    results = [line for line in lines if line.startswith(RESULT)]
    if code == 0 and results:
        try:
            body = json.loads(results[-1][len(RESULT):])
        except ValueError:
            body = None
        if isinstance(body, dict):
            return SyncOutcome(OFFLINE if body.get("offline") is True else DONE, lines=kept)
    errors = [line for line in lines if line.startswith(ERROR)]
    if code == 2 and errors:
        return SyncOutcome(FAILED, _cut(errors[-1][len(ERROR):]), kept)
    if code == 130 or code < 0:
        return SyncOutcome(FAILED, _cut(f"同步子行程被中斷（結束碼 {code}）"), kept)
    detail = f"同步子行程結束碼 {code}"
    tail = _last_nonblank(lines)
    if tail:
        detail += f"：{tail}"
    return SyncOutcome(FAILED, _cut(detail), kept)


def child_argv() -> list[str]:
    return [sys.executable, "-m", "agora.sync_child"]


def child_env(config: Path, cache: Path, state: Path) -> dict[str, str]:
    return {**os.environ, "AGORA_CONFIG": str(config), "AGORA_CACHE_DIR": str(cache),
            "AGORA_STATE_DIR": str(state), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


def spawn_child(argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    # start_new_session: the child is not in the TUI's process group, so a ctrl+c in the
    # terminal does not reach it; after the TUI has gone it runs to its end (Q-3).
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, start_new_session=True, env=env)


class BackgroundSync:
    """One background sync child at a time; tells `on_done` once, from its reader thread through `post`."""

    def __init__(self, *, spawn: Callable[[], Any], post: Callable[..., Any],
                 on_done: Callable[[SyncOutcome], None],
                 clock: Callable[[], float] = time.monotonic):
        self._spawn = spawn
        self._post = post
        self._on_done = on_done
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self.started_at: float | None = None

    @property
    def running(self) -> bool:
        return self._running

    def elapsed(self) -> float:
        if not self._running or self.started_at is None:
            return 0.0
        return self._clock() - self.started_at

    def start(self) -> bool:
        """Start the child; False when one is already running. A spawn error is raised to the caller."""
        with self._lock:
            if self._running:
                return False
            proc = self._spawn()           # raises: nothing below runs, running stays False
            self._running = True
            self.started_at = self._clock()
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        return True

    def _read(self, proc: Any) -> None:
        lines: list[str] = []
        try:
            for line in proc.stdout:
                lines.append(line.rstrip("\r\n"))
            code = proc.wait()
        except Exception as e:             # a read that fails is a failed sync, not a dead thread
            code = 2
            lines.append(f"讀不到輸出：{e}")
        try:
            self._post(self._finish, code, lines)
        except Exception:                  # the app is gone; nobody is waiting for the answer
            pass

    def _finish(self, code: int, lines: list[str]) -> None:
        with self._lock:
            self._running = False
        self._on_done(parse_outcome(code, lines))
