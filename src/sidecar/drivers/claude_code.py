"""Claude Code driver: the stream-json programmatic mode (task 3.1, design D6).

The agent process is either the one `agora continue --headless` started
(`launch` attaches to its stdio) or, for `new` mode, one this driver starts
itself; either way the conversation travels as stream-json lines on stdin and
stdout.

State comes from the event stream:
- a sent user message is `running` until a `result` event ends the round;
- quota exhaustion is `rate_limit_event` with `rate_limit_info.status ==
  "rejected"` (or `errorCode == "credits_required"`); `allowed_warning` is
  not exhaustion; unknown statuses are logged (the event, no secrets) and
  treated as not exhausted;
- `rateLimitType` maps `five_hour` -> `5h`, `seven_day` -> `1w`; anything else
  is `5h` without `resets_at` and a log line;
- the auxiliary signals (`system/api_retry` with `error: rate_limit`, and a
  `result` whose text starts with `You've hit your`) mean exhausted with
  window `5h` and no reset time - for an api-key subscription they are the
  only signal there is;
- `resetsAt` (epoch seconds) becomes RFC 3339 UTC; none means the field is
  left out (LLMGateway applies the window's default length).

The child environment is built by `subproc.child_env` (allow-list + what
`env_for` returned), so the sidecar's own `ANTHROPIC_*` never leaks in.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..protocols import (
    DRIVER_EXITED,
    DRIVER_IDLE,
    DRIVER_QUOTA_EXHAUSTED,
    DRIVER_RUNNING,
    DriverState,
    RunningSession,
    Subscription,
    iso,
)
from ..subproc import child_env, terminate_process
from .common import pick_model, stderr_excerpt

logger = logging.getLogger(__name__)

#: The fixed settings the spike pinned (D6): sidecar owns the swap decision.
HEADLESS_SETTINGS = '{"autoContinueAtUsageLimit": false}'
QUOTA_EVENT_LOG_LIMIT = 500


class ClaudeDriverError(RuntimeError):
    """The claude process could not be started or talked to."""


@dataclass
class ClaudeHandle:
    proc: Any  # Popen, or the AgoraProcess wrapper from the store
    session_id: str | None = None  # the agora Session id, when known
    agent_session_id: str | None = None
    state: DriverState = field(default_factory=lambda: DriverState(kind=DRIVER_IDLE))
    quota: DriverState | None = None  # sticky: only a new handle clears it
    stopped: bool = False
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20))
    secrets: tuple[str, ...] = ()  # env_for values, for masking error tails
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None


class ClaudeCodeDriver:
    """AgentDriver for `claude-code`."""

    name = "claude-code"

    def __init__(
        self,
        *,
        command: str | Sequence[str] = "claude",
        extra_args: Sequence[str] = (),
    ) -> None:
        self._command = [command] if isinstance(command, str) else list(command)
        self._extra_args = list(extra_args)

    # -- AgentDriver ------------------------------------------------------

    def env_for(self, subscription: Subscription) -> dict[str, str]:
        """The subscription as Claude Code's environment (D6, spike-verified).

        - a claude.ai subscription's key goes to `CLAUDE_CODE_OAUTH_TOKEN`;
        - an `anthropic` api-key goes to `ANTHROPIC_API_KEY` (x-api-key);
        - any other provider's api-key goes to `ANTHROPIC_AUTH_TOKEN` with an
          empty `ANTHROPIC_API_KEY`, so Claude sends only `Authorization:
          Bearer` (LLMGateway decision 7);
        - `models.default`, when set, becomes both `ANTHROPIC_MODEL` and
          `ANTHROPIC_SMALL_FAST_MODEL`; a non-Anthropic provider without a
          default falls back to `models.allow`'s first concrete entry or is
          refused (`no_model`); an Anthropic subscription without a default
          keeps Claude's own;
        - the non-essential-traffic and auto-updater switches are ours to set
          (`child_env` drops the parent's `ANTHROPIC_*`).
        """
        provider = subscription.provider or "anthropic"
        env: dict[str, str] = {}
        if subscription.kind == "api-key":
            if provider == "anthropic":
                if subscription.key:
                    env["ANTHROPIC_API_KEY"] = subscription.key
            else:
                if subscription.key:
                    env["ANTHROPIC_AUTH_TOKEN"] = subscription.key
                env["ANTHROPIC_API_KEY"] = ""  # bearer only, never x-api-key
        elif subscription.key:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = subscription.key
        if subscription.endpoint:
            env["ANTHROPIC_BASE_URL"] = subscription.endpoint
        default = (subscription.models or {}).get("default")
        if isinstance(default, str) and default:
            env["ANTHROPIC_MODEL"] = default
            env["ANTHROPIC_SMALL_FAST_MODEL"] = default
        elif provider != "anthropic":
            model = pick_model(subscription)
            env["ANTHROPIC_MODEL"] = model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"
        return env

    def launch(self, session: RunningSession | None, env: dict[str, str], workdir: str) -> ClaudeHandle:
        if session is None:
            # `new` mode (D5): no agora process; this driver starts claude.
            argv = [
                *self._command, "-p",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--settings", HEADLESS_SETTINGS,
                *self._extra_args,
            ]
            try:
                proc = subprocess.Popen(
                    argv, cwd=workdir, env=child_env(env),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                )
            except OSError as e:
                raise ClaudeDriverError(f"cannot start claude: {e}") from e
            handle = ClaudeHandle(proc=proc)
            threading.Thread(target=_drain_stderr, args=(proc, handle), daemon=True).start()
        else:
            handle = ClaudeHandle(
                proc=session.process,
                session_id=session.session_id,
                agent_session_id=getattr(session.process, "agent_session_id", None),
            )
        handle.secrets = tuple(value for value in env.values() if value)
        handle.reader = threading.Thread(target=self._read_events, args=(handle,), daemon=True)
        handle.reader.start()
        return handle

    def send(self, handle: ClaudeHandle, text: str) -> None:
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self._write(handle, message)
        with handle.lock:
            if handle.quota is None and not handle.stopped:
                handle.state = DriverState(kind=DRIVER_RUNNING)

    def interrupt(self, handle: ClaudeHandle) -> None:
        request = {
            "type": "control_request",
            "request_id": str(uuid.uuid4()),
            "request": {"subtype": "interrupt"},
        }
        self._write(handle, request)

    def state(self, handle: ClaudeHandle) -> DriverState:
        with handle.lock:
            if handle.quota is not None:
                return handle.quota
            return handle.state

    def stop(self, handle: ClaudeHandle, *, graceful: bool = True, timeout: float = 30.0) -> None:
        with handle.lock:
            handle.stopped = True
        if graceful:
            code = terminate_process(handle.proc, timeout=timeout)
        else:
            try:
                handle.proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                code = handle.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                code = None
        with handle.lock:
            if handle.quota is None:
                handle.state = DriverState(
                    kind=DRIVER_EXITED, code=code, reason="stopped"
                )

    def external_session_id(self, handle: ClaudeHandle) -> str | None:
        with handle.lock:
            return handle.agent_session_id

    # -- internals --------------------------------------------------------

    def _write(self, handle: ClaudeHandle, message: dict[str, Any]) -> None:
        line = json.dumps(message)
        try:
            with handle.lock:
                if handle.proc.stdin is None:
                    raise ClaudeDriverError("claude has no stdin")
                handle.proc.stdin.write(line + "\n")
                handle.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise ClaudeDriverError(f"cannot write to claude: {e}{self._tail(handle)}") from e

    def _tail(self, handle: ClaudeHandle) -> str:
        """The child's stderr tail, keys masked (D6)."""
        return stderr_excerpt(handle.proc, handle.stderr_tail, handle.secrets)

    def _read_events(self, handle: ClaudeHandle) -> None:
        try:
            for raw in handle.proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    logger.debug("claude: non-JSON stdout line: %.200s", line)
                    continue
                if isinstance(event, dict):
                    self._on_event(handle, event)
        except (OSError, ValueError):
            pass
        finally:
            code = handle.proc.poll() if hasattr(handle.proc, "poll") else None
            with handle.lock:
                if handle.quota is None:
                    handle.state = DriverState(
                        kind=DRIVER_EXITED, code=code, reason="claude exited"
                    )

    def _on_event(self, handle: ClaudeHandle, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                with handle.lock:
                    handle.agent_session_id = sid
            return
        if etype == "rate_limit_event":
            self._on_rate_limit(handle, event)
            return
        if etype == "system" and event.get("subtype") == "api_retry":
            if event.get("error") == "rate_limit":
                self._mark_quota(handle, "5h", None, "api_retry rate_limit")
            return
        if etype == "result":
            text = event.get("result")
            if isinstance(text, str) and text.startswith("You've hit your"):
                self._mark_quota(handle, "5h", None, "result text")
                return
            with handle.lock:
                if handle.quota is None:
                    handle.state = DriverState(kind=DRIVER_IDLE)
            return
        # Everything else (assistant messages, tool events, ...) does not
        # change the state machine.

    def _on_rate_limit(self, handle: ClaudeHandle, event: dict[str, Any]) -> None:
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            info = {}
        status = info.get("status")
        if status == "rejected" or info.get("errorCode") == "credits_required":
            window, resets_at = self._window_and_resets(info)
            self._mark_quota(handle, window, resets_at, f"status={status!r}")
        elif status in ("allowed", "allowed_warning"):
            return  # near the limit is not over it (D6)
        else:
            # Unknown value: never swap on it; keep the raw event (no secrets
            # ride in a rate_limit_info) for the first real exhaustion (Q6).
            logger.warning(
                "claude: unknown rate_limit status %r; treating as not exhausted: %.500s",
                status, json.dumps(event, ensure_ascii=False)[:QUOTA_EVENT_LOG_LIMIT],
            )

    def _window_and_resets(self, info: dict[str, Any]) -> tuple[str, str | None]:
        rate_type = info.get("rateLimitType")
        if rate_type == "five_hour":
            window = "5h"
        elif rate_type == "seven_day":
            window = "1w"
        else:
            logger.warning(
                "claude: unknown rateLimitType %r; using 5h without resets_at",
                rate_type,
            )
            return "5h", None
        resets = info.get("resetsAt")
        if isinstance(resets, (int, float)) and not isinstance(resets, bool):
            return window, iso(float(resets))
        return window, None

    def _mark_quota(self, handle: ClaudeHandle, window: str, resets_at: str | None,
                    reason: str) -> None:
        with handle.lock:
            if handle.quota is None:
                handle.quota = DriverState(
                    kind=DRIVER_QUOTA_EXHAUSTED,
                    window=window,
                    resets_at=resets_at,
                    reason=reason,
                )


def _drain_stderr(proc: Any, handle: ClaudeHandle) -> None:
    """Keep a directly spawned claude's stderr from filling up, tail kept."""
    try:
        for line in proc.stderr:
            text = line.rstrip("\n")
            with handle.lock:
                handle.stderr_tail.append(text)
            logger.debug("claude: %s", text)
    except (OSError, ValueError):
        pass
