"""The real SessionStore: sidecar runs `agora continue --headless` (task 3.0).

Design D3/D5/D6: continue/fork/merge all go through `agora continue …
--headless` (merge first runs `agora merge --agent <by>`), the fixed stderr
lines tell us the agora session id, the agent's own session id and (opencode)
the port, and the caller talks to the agent through the process's stdio or the
port.  Stopping is SIGTERM to that process - agora forwards it, writes the
last conversation back and releases the lock before exiting (D6).

The `agora` command is the one on `PATH` (config `agora_command`, default
`agora`); sidecar never imports the agora package, so it can live in a
checkout that has only `src/sidecar`.

Everything a child does has a timeout: waiting for the stderr lines
(`start_timeout`), the summarizer round trip of `agora merge` (`merge_timeout`)
and the gentle stop that the drivers perform via
`subproc.terminate_process(..., timeout=close_timeout)`.

This module never runs in unit tests except through the fake command injected
into `AgoraSessionStore(command=[...])` (or a fake `agora` on PATH).
"""

from __future__ import annotations

import logging
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import DEFAULT_AGORA_COMMAND
from .protocols import RunningSession, SidecarError
from .subproc import child_env

logger = logging.getLogger(__name__)

#: sidecar's agent kinds -> agora's `--agent` names (D3).
AGORA_AGENT_NAMES = {"claude-code": "claude", "opencode": "opencode"}

START_TIMEOUT = 60.0
MERGE_TIMEOUT = 900.0  # one summarizer round trip can take minutes (D5)
IMPORT_TIMEOUT = 60.0  # local files + a background upload starts; bounded (N2)
STDERR_TAIL = 500  # kept lines, for error messages and debugging
ERROR_TAIL_CHARS = 500


class AgoraSessionError(SidecarError):
    """agora refused or failed to prepare the Session."""

    code = "agora_session_failed"


@dataclass
class AgoraProcess:
    """The `agora continue --headless` process plus what its stderr said.

    `stdout` is the agent's own stream (Claude Code's stream-json); `port` and
    `agent_session_id` come from the fixed stderr lines and are what the
    opencode driver needs.  A background thread keeps draining stderr so the
    child can never block on a full pipe; `stderr_lines` is that pump's live
    deque, so `stderr_tail()` also sees lines printed *after* the start.
    """

    proc: subprocess.Popen
    port: int | None = None
    agent_session_id: str | None = None
    stderr_lines: Sequence[str] = field(default_factory=list)

    @property
    def stdout(self) -> Any:
        return self.proc.stdout

    @property
    def stdin(self) -> Any:
        return self.proc.stdin

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def poll(self) -> int | None:
        return self.proc.poll()

    # Popen-shaped delegations, so `subproc.terminate_process` can take either
    # a raw Popen or this wrapper.
    def send_signal(self, sig: int) -> None:
        self.proc.send_signal(sig)

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)

    def kill(self) -> None:
        self.proc.kill()

    def terminate(self, *, timeout: float) -> int | None:
        """The D6 gentle stop; returns the exit code (None if unkillable)."""
        from .subproc import terminate_process

        return terminate_process(self.proc, timeout=timeout)

    def stderr_tail(self) -> str:
        """The latest lines, read from the live deque at call time (M5)."""
        try:
            lines = list(self.stderr_lines)
        except RuntimeError:  # the pump appended while we were copying
            lines = []
        return "\n".join(lines[-10:])[-ERROR_TAIL_CHARS:]


class _StderrPump:
    """Reads stderr lines; hands early lines to `start` and keeps draining."""

    def __init__(self, stderr: Any) -> None:
        self.lines: deque[str] = deque(maxlen=STDERR_TAIL)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._drain = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(stderr,), daemon=True)
        self._thread.start()

    def _run(self, stderr: Any) -> None:
        try:
            for raw in stderr:
                line = raw.rstrip("\n")
                self.lines.append(line)
                if not self._drain.is_set():
                    self._queue.put(line)
                else:
                    logger.debug("agora: %s", line)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)

    def wait_for(self, prefixes: Sequence[str], timeout: float) -> dict[str, str]:
        """Wait until every prefix has appeared; return prefix -> value.

        Lines arrive in any order (session-continue delta); a prefix is
        captured once.  Raises TimeoutError when the deadline passes first.
        """
        wanted = list(prefixes)
        found: dict[str, str] = {}
        deadline = _monotonic() + timeout
        while len(found) < len(wanted):
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TimeoutError(f"agora never printed {wanted} (saw {list(self.lines)})")
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(
                    f"agora never printed {wanted} (saw {list(self.lines)})"
                ) from None
            if line is None:
                raise TimeoutError(
                    f"agora exited before printing {wanted} (saw {list(self.lines)})"
                )
            for prefix in wanted:
                if prefix not in found and line.startswith(prefix):
                    found[prefix] = line[len(prefix):].strip()
        return found

    def finish(self) -> None:
        self._drain.set()


def _monotonic() -> float:
    return time.monotonic()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _last_agora_id(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("agora:"):
            return line
    return None


class AgoraSessionStore:
    """SessionStore implementation that spawns `agora` (design D3/D5)."""

    def __init__(
        self,
        *,
        command: str | Sequence[str] | None = None,
        sync_period: float | None = None,
        start_timeout: float = START_TIMEOUT,
        merge_timeout: float = MERGE_TIMEOUT,
        import_timeout: float = IMPORT_TIMEOUT,
    ) -> None:
        #: argv prefix; production is the `agora` on PATH (D7's
        #: `agora_command`), tests inject a fake.
        if command is None:
            self._command = [DEFAULT_AGORA_COMMAND]
        elif isinstance(command, str):
            self._command = [command]
        else:
            self._command = list(command)
        self._sync_period = sync_period
        self._start_timeout = start_timeout
        self._merge_timeout = merge_timeout
        self._import_timeout = import_timeout

    def import_external(self, *, external_id: str, agent: str, tag: str) -> str:
        """`agora import session --external-session-id … --add-tag …` (D5/3.5)."""
        agora_agent = AGORA_AGENT_NAMES.get(agent)
        if agora_agent is None:
            raise AgoraSessionError(f"unsupported agent kind {agent!r}")
        argv = [
            *self._command,
            "import", "session",
            "--external-session-id", external_id,
            "--agent", agora_agent,
            "--add-tag", tag,
        ]
        try:
            proc = subprocess.run(
                argv,
                env=child_env({}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._import_timeout,
            )
        except OSError as e:
            raise AgoraSessionError(f"cannot run agora import: {e}") from e
        except subprocess.TimeoutExpired:
            raise AgoraSessionError(
                f"agora import timed out after {self._import_timeout:g}s"
            ) from None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-ERROR_TAIL_CHARS:]
            raise AgoraSessionError(f"agora import failed (rc={proc.returncode}): {tail}")
        agora_id = _last_agora_id(proc.stdout or "")
        if agora_id is None:
            raise AgoraSessionError("agora import printed no Session id")
        return agora_id

    def start(
        self,
        *,
        mode: str,
        ids: Sequence[str],
        by: str,
        agent: str,
        workdir: str,
        tag: str,
        env: dict[str, str],
        merge_env: dict[str, str] | None = None,
    ) -> RunningSession:
        agora_agent = AGORA_AGENT_NAMES.get(agent)
        if agora_agent is None:
            raise AgoraSessionError(f"unsupported agent kind {agent!r}")
        if mode == "merge":
            merged = self._merge(list(ids), by, merge_env if merge_env is not None else env,
                                 workdir)
            continue_ids = [merged]
        elif mode in ("continue", "fork"):
            continue_ids = list(ids)
        else:
            raise AgoraSessionError(f"unsupported session mode {mode!r}")

        port = _free_port() if agent == "opencode" else None
        argv = [
            *self._command,
            "continue", "session", *continue_ids,
            "--agent", agora_agent,
            "--headless",
            "--add-tag", tag,
            "--dir", workdir,
        ]
        if mode == "fork":
            argv.append("--fork")
        if self._sync_period is not None:
            argv += ["--sync-period", str(self._sync_period)]
        if port is not None:
            argv += ["--headless-port", str(port)]

        try:
            proc = subprocess.Popen(
                argv,
                cwd=workdir,
                env=child_env(env),  # the secret travels in the env, never argv (D3)
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            raise AgoraSessionError(f"cannot run agora: {e}") from e

        pump = _StderrPump(proc.stderr)
        wanted = ["agora-session ", "agora-agent-session "]
        if agent == "opencode":
            wanted.append("agora-port ")
        try:
            found = pump.wait_for(wanted, timeout=self._start_timeout)
        except TimeoutError as e:
            self._abort(proc, pump)
            raise AgoraSessionError(str(e)) from None
        pump.finish()

        session_id = found["agora-session "]
        if not session_id:
            self._abort(proc, pump)
            raise AgoraSessionError("agora printed an empty agora-session line")
        process = AgoraProcess(
            proc=proc,
            port=int(found["agora-port "]) if "agora-port " in found else None,
            agent_session_id=found.get("agora-agent-session ") or None,
            stderr_lines=pump.lines,  # live deque (M5): keep seeing new lines
        )
        return RunningSession(session_id=session_id, process=process)

    def _merge(self, ids: list[str], by: str, env: dict[str, str], workdir: str) -> str:
        agora_by = AGORA_AGENT_NAMES.get(by)
        if agora_by is None:
            raise AgoraSessionError(f"unsupported merge agent kind {by!r}")
        argv = [*self._command, "merge", "session", *ids, "--agent", agora_by]
        try:
            proc = subprocess.run(
                argv,
                cwd=workdir,
                env=child_env(env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._merge_timeout,
            )
        except OSError as e:
            raise AgoraSessionError(f"cannot run agora merge: {e}") from e
        except subprocess.TimeoutExpired:
            raise AgoraSessionError(
                f"agora merge timed out after {self._merge_timeout:g}s"
            ) from None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-ERROR_TAIL_CHARS:]
            raise AgoraSessionError(f"agora merge failed (rc={proc.returncode}): {tail}")
        merged = _last_agora_id(proc.stdout or "")
        if merged is None:
            raise AgoraSessionError("agora merge printed no Session id")
        return merged

    def _abort(self, proc: subprocess.Popen, pump: _StderrPump) -> None:
        from .subproc import terminate_process

        logger.warning("agora start failed; stderr tail: %s",
                       "\n".join(pump.lines)[-ERROR_TAIL_CHARS:])
        terminate_process(proc, timeout=5.0)
