"""opencode driver: the server mode (task 3.2, design D6).

The agent process is `opencode serve --hostname 127.0.0.1 --port <port>` -
either the one `agora continue --headless` started (the store read the port
and the `ses_…` session id from agora's fixed stderr lines) or, for `new`
mode, one this driver starts itself (it then creates the session with
`POST /session`).

The `agora-port` line is printed before the server listens, so `launch` polls
the port until it accepts a connection (`poll_timeout`); talking to the agent
is HTTP on 127.0.0.1 with every request carrying a timeout:

- send: `POST /session/{id}/message` (the call returns only when the round is
  done, so it runs in a background thread with a long timeout, D6/M2), body
  `{"parts": [{"type": "text", "text": ...}]}`;
- state: whether that background send is in flight (running) or not (idle);
  `GET /session/status` is only read for `retry` (quota), D6/M2;
- interrupt: `POST /session/{id}/abort`.

Quota exhaustion is `{"type": "retry", "action": {"reason": …}, "next": …}`
with `reason` in the configured whitelist (`quota_reasons`, default only the
observed `free_tier_limit`); other `retry` reasons are logged and the state
stays `running`.  `next` is epoch milliseconds -> RFC 3339 UTC; the window
follows one rule: more than 5 hours plus 5 minutes away is `1w`, otherwise
`5h`; no `next` (or not a time) is `5h` without `resets_at`.

The key and baseURL reach opencode through `OPENCODE_CONFIG_CONTENT` (the
gateway-v0 design lists this mapping as an open question; the architect
verifies it on real opencode - ASSUMPTION reported to the PM).
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..protocols import (
    DRIVER_EXITED,
    DRIVER_IDLE,
    DRIVER_QUOTA_EXHAUSTED,
    DRIVER_RUNNING,
    BadRequestError,
    DriverState,
    RunningSession,
    Subscription,
    iso,
)
from ..subproc import child_env, terminate_process
from .common import extract_secrets, mask_secrets, pick_model, stderr_excerpt

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_REASONS = ("free_tier_limit",)
FIVE_HOURS_PLUS_SLACK = 5 * 3600 + 300  # D6's single window rule
POLL_TIMEOUT = 15.0
HTTP_TIMEOUT = 10.0
STATUS_TIMEOUT = 2.0
MESSAGE_TIMEOUT = 3600.0  # the round call waits for the whole round (D6: cap 1h)

#: LLMGateway's `provider` -> opencode's provider id (D6, verified by the
#: spike for opencode-zen/openrouter; ollama-cloud is still to be confirmed).
OPENCODE_PROVIDER_IDS = {
    "opencode-zen": "opencode",
    "openrouter": "openrouter",
    "ollama-cloud": "ollama-cloud",
}


class OpenCodeDriverError(RuntimeError):
    """opencode could not be started or talked to."""


@dataclass
class OpenCodeHandle:
    proc: Any
    port: int
    session_id: str | None = None  # the agora Session id, when known
    agent_session_id: str | None = None  # the `ses_…` the HTTP calls need
    state: DriverState = field(default_factory=lambda: DriverState(kind=DRIVER_IDLE))
    quota: DriverState | None = None
    stopped: bool = False
    sending: bool = False  # the background POST /message is in flight (M2)
    last_error: str | None = None  # the last round failure, masked (M2)
    expected_provider: str | None = None  # what env_for told opencode to use
    expected_model: str | None = None
    model_mismatch: str | None = None  # the first response used something else
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20))
    secrets: tuple[str, ...] = ()  # real secret values from env, for masking error tails
    lock: threading.Lock = field(default_factory=threading.Lock)


class OpenCodeDriver:
    """AgentDriver for `opencode`."""

    name = "opencode"

    def __init__(
        self,
        *,
        command: str | Sequence[str] = "opencode",
        extra_args: Sequence[str] = (),
        quota_reasons: Sequence[str] = DEFAULT_QUOTA_REASONS,
        poll_timeout: float = POLL_TIMEOUT,
        http_timeout: float = HTTP_TIMEOUT,
        status_timeout: float = STATUS_TIMEOUT,
        message_timeout: float = MESSAGE_TIMEOUT,
    ) -> None:
        self._command = [command] if isinstance(command, str) else list(command)
        self._extra_args = list(extra_args)
        self._quota_reasons = tuple(quota_reasons)
        self._poll_timeout = poll_timeout
        self._http_timeout = http_timeout
        self._status_timeout = status_timeout
        self._message_timeout = min(message_timeout, MESSAGE_TIMEOUT)  # D6: cap 1h

    # -- AgentDriver ------------------------------------------------------

    def env_for(self, subscription: Subscription) -> dict[str, str]:
        """Per D6's verified rules: mapped provider id, model mandatory.

        ASSUMPTION verified by the architect's spike on the real binary:
        key/baseURL ride in `OPENCODE_CONFIG_CONTENT`, the provider id is
        mapped (`opencode-zen` -> `opencode`), `model`/`small_model` are both
        set, the chosen model is declared, and the auto-update/model-fetch
        switches are ours to set (child_env drops the parent's OPENCODE_*).
        """
        provider_id = OPENCODE_PROVIDER_IDS.get(subscription.provider or "")
        if provider_id is None:
            raise BadRequestError(
                f"LLMGateway provider {subscription.provider!r} has no opencode provider id"
            )
        model = pick_model(subscription)
        options: dict[str, str] = {}
        if subscription.key:
            options["apiKey"] = subscription.key
        if subscription.endpoint:
            options["baseURL"] = subscription.endpoint
        config = {
            "provider": {
                provider_id: {
                    "options": options,
                    "models": {model: {"name": model}},
                }
            },
            "model": f"{provider_id}/{model}",
            "small_model": f"{provider_id}/{model}",
        }
        return {
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
        }

    def launch(self, session: RunningSession | None, env: dict[str, str], workdir: str) -> OpenCodeHandle:
        if session is None:
            port = _free_port()
            argv = [
                *self._command, "serve",
                "--hostname", "127.0.0.1", "--port", str(port),
                *self._extra_args,
            ]
            try:
                proc = subprocess.Popen(
                    argv, cwd=workdir, env=child_env(env),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                )
            except OSError as e:
                raise OpenCodeDriverError(f"cannot start opencode: {e}") from e
            handle = OpenCodeHandle(proc=proc, port=port)
            threading.Thread(target=_drain_pipe, args=(proc.stdout, handle, "stdout"), daemon=True).start()
            threading.Thread(target=_drain_pipe, args=(proc.stderr, handle, "stderr"), daemon=True).start()
            self._wait_listening(handle)
            handle.agent_session_id = self._create_session(handle)
        else:
            process = session.process
            port = getattr(process, "port", None)
            if not isinstance(port, int):
                raise OpenCodeDriverError("the session process has no opencode port")
            handle = OpenCodeHandle(
                proc=process,
                port=port,
                session_id=session.session_id,
                agent_session_id=getattr(process, "agent_session_id", None),
            )
            self._wait_listening(handle)
        handle.expected_provider, handle.expected_model = _expected_model(env)
        handle.secrets = extract_secrets(env)
        return handle

    def send(self, handle: OpenCodeHandle, text: str) -> None:
        # The HTTP call blocks until the round is over (D6), so it goes to a
        # background thread with the long message timeout; while that thread
        # runs the driver reports running (M2).
        with handle.lock:
            if handle.stopped:
                raise OpenCodeDriverError("opencode is stopped")
            session_id = handle.agent_session_id
            if handle.quota is None:
                handle.sending = True
                handle.state = DriverState(kind=DRIVER_RUNNING)
        if session_id is None:
            with handle.lock:
                handle.sending = False
            raise OpenCodeDriverError("no opencode session id to send to")
        body = {"parts": [{"type": "text", "text": text}]}
        thread = threading.Thread(
            target=self._send_round,
            args=(handle, session_id, body),
            daemon=True,
        )
        thread.start()

    def interrupt(self, handle: OpenCodeHandle) -> None:
        with handle.lock:
            session_id = handle.agent_session_id
        if session_id is None:
            raise OpenCodeDriverError("no opencode session id to abort")
        self._request(handle, "POST", f"/session/{session_id}/abort", body={})
        with handle.lock:
            handle.sending = False  # the round was aborted
            if handle.quota is None:
                handle.state = DriverState(kind=DRIVER_IDLE)

    def state(self, handle: OpenCodeHandle) -> DriverState:
        with handle.lock:
            if handle.quota is not None:
                return handle.quota
            if handle.sending:
                return DriverState(kind=DRIVER_RUNNING)  # the background round runs
            session_id = handle.agent_session_id
        if handle.proc.poll() is not None:
            code = handle.proc.returncode
            with handle.lock:
                handle.state = DriverState(
                    kind=DRIVER_EXITED,
                    code=code,
                    reason=handle.model_mismatch or "opencode exited",
                )
                return handle.state
        try:
            data = self._request(handle, "GET", "/session/status", timeout=self._status_timeout)
        except OpenCodeDriverError:
            return handle.state  # cannot tell right now: keep the last known
        status = _status_for(data, session_id)
        if status is not None:
            self._apply_status(handle, status)
        return handle.state

    def stop(self, handle: OpenCodeHandle, *, graceful: bool = True, timeout: float = 30.0) -> None:
        with handle.lock:
            handle.stopped = True
            handle.sending = False
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
                    kind=DRIVER_EXITED, code=code,
                    reason=handle.model_mismatch or "stopped",
                )

    def external_session_id(self, handle: OpenCodeHandle) -> str | None:
        with handle.lock:
            return handle.agent_session_id

    # -- internals --------------------------------------------------------

    def _wait_listening(self, handle: OpenCodeHandle) -> None:
        """The `agora-port` line does not mean listening yet (b8ab0f0)."""
        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            if handle.proc.poll() is not None:
                raise OpenCodeDriverError(
                    f"opencode exited before listening (rc={handle.proc.returncode})"
                    f"{self._tail(handle)}"
                )
            try:
                with socket.create_connection(("127.0.0.1", handle.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        terminate_process(handle.proc, timeout=5.0)
        raise OpenCodeDriverError(
            f"opencode never listened on 127.0.0.1:{handle.port} "
            f"within {self._poll_timeout:g}s{self._tail(handle)}"
        )

    def _create_session(self, handle: OpenCodeHandle) -> str:
        data = self._request(handle, "POST", "/session", body={})
        session_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeDriverError("opencode did not answer a session id")
        return session_id

    def _send_round(self, handle: OpenCodeHandle, session_id: str, body: dict[str, Any]) -> None:
        failure: OpenCodeDriverError | None = None
        try:
            try:
                data = self._request(
                    handle, "POST", f"/session/{session_id}/message", body=body,
                    timeout=self._message_timeout,  # long: the round may take an hour (D6)
                )
            except OpenCodeDriverError as e:
                failure = e
            else:
                info_error = None
                if isinstance(data, dict):
                    info = data.get("info")
                    if isinstance(info, dict) and info.get("error"):
                        info_error = str(info["error"])
                if info_error:
                    failure = OpenCodeDriverError(
                        f"round failed: {info_error}{self._tail(handle)}"
                    )
                else:
                    self._verify_first_response(handle, data)
        except Exception as e:
            # A (section-3 review): anything that is not an OpenCodeDriverError
            # (http.client.IncompleteRead is an HTTPException, not an OSError) is
            # still a failed round; it must not escape and leave `sending` stuck.
            failure = OpenCodeDriverError(
                f"round failed: {type(e).__name__}: {e}{self._tail(handle)}"
            )
        finally:
            # Both exits go through here, so the agent never keeps looking
            # running after its background round is over (A).
            self._finish_round(handle, failure)

    def _finish_round(
        self, handle: OpenCodeHandle, failure: OpenCodeDriverError | None
    ) -> None:
        """The round is over either way: clear `sending` and apply the outcome."""
        if failure is not None:
            self._fail_round(handle, failure)
            return
        with handle.lock:
            handle.sending = False
            if handle.quota is None:
                handle.state = DriverState(kind=DRIVER_IDLE)

    def _fail_round(self, handle: OpenCodeHandle, error: OpenCodeDriverError) -> None:
        """A failed round is visible in the state, not just a log line (M2).

        The reason lands in `exit.reason` (via DriverState) and in
        `last_error`; the message is masked, never carrying the key.
        """
        message = mask_secrets(str(error), handle.secrets)
        logger.warning("opencode: sending a message failed: %s", message)
        with handle.lock:
            handle.sending = False
            if handle.quota is not None:
                return  # the quota state already explains the round
            handle.last_error = message
            handle.state = DriverState(kind=DRIVER_EXITED, reason=message)

    def _verify_first_response(self, handle: OpenCodeHandle, data: Any) -> None:
        """The first response must name the provider/model env_for picked (D6).

        A mismatch is logged and carried into `exit.reason`; it is never
        treated as normal operation.
        """
        if handle.expected_model is None or handle.model_mismatch is not None:
            return
        info = None
        if isinstance(data, dict):
            if isinstance(data.get("info"), dict):
                info = data["info"]
            elif "providerID" in data or "modelID" in data:
                info = data
        if not isinstance(info, dict):
            return  # nothing to compare against; not an error by itself
        provider = info.get("providerID")
        model = info.get("modelID")
        if (provider and provider != handle.expected_provider) or (
            model and model != handle.expected_model
        ):
            note = (
                f"model mismatch: opencode answered providerID={provider!r} "
                f"modelID={model!r}, expected "
                f"{handle.expected_provider}/{handle.expected_model}"
            )
            with handle.lock:
                handle.model_mismatch = note
            logger.error("opencode: %s", note)

    def _request(
        self,
        handle: OpenCodeHandle,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"http://127.0.0.1:{handle.port}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._http_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise OpenCodeDriverError(
                f"{method} {path}: HTTP {e.code}{self._tail(handle)}"
            ) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise OpenCodeDriverError(
                f"{method} {path} failed: {e}{self._tail(handle)}"
            ) from e
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def _tail(self, handle: OpenCodeHandle) -> str:
        """The child's stderr tail, keys masked (D6)."""
        return stderr_excerpt(handle.proc, handle.stderr_tail, handle.secrets)

    def _apply_status(self, handle: OpenCodeHandle, status: dict[str, Any]) -> None:
        """Read `/session/status` for `retry` only (D6/M2).

        The round's running/idle state comes from whether the background send
        is in flight (and the send itself), never from this map: the map may be
        empty or not contain this session while a round runs.
        """
        kind = status.get("type")
        if kind != "retry":
            return
        reason = (status.get("action") or {}).get("reason")
        if reason in self._quota_reasons:
            window, resets_at = _window_and_resets(status.get("next"))
            with handle.lock:
                if handle.quota is None:
                    handle.quota = DriverState(
                        kind=DRIVER_QUOTA_EXHAUSTED,
                        window=window,
                        resets_at=resets_at,
                        reason=f"retry {reason!r}",
                    )
        else:
            logger.warning(
                "opencode: retry with non-quota reason %r; staying running",
                reason,
            )


def _status_for(data: Any, session_id: str | None) -> dict[str, Any] | None:
    """The status object for our session; the answer may be keyed or flat."""
    if not isinstance(data, dict):
        return None
    if session_id and isinstance(data.get(session_id), dict):
        return data[session_id]
    if "type" in data:
        return data
    return None


def _window_and_resets(next_ms: Any) -> tuple[str, str | None]:
    """opencode gives one instant in epoch milliseconds (D6's single rule)."""
    if not isinstance(next_ms, (int, float)) or isinstance(next_ms, bool):
        return "5h", None
    seconds = float(next_ms) / 1000.0
    window = "1w" if seconds - time.time() > FIVE_HOURS_PLUS_SLACK else "5h"
    return window, iso(seconds)


def _expected_model(env: dict[str, str]) -> tuple[str | None, str | None]:
    """The provider/model env_for asked for, read back out of its config."""
    raw = env.get("OPENCODE_CONFIG_CONTENT")
    if not raw:
        return None, None
    try:
        config = json.loads(raw)
    except ValueError:
        return None, None
    model = config.get("model") if isinstance(config, dict) else None
    if isinstance(model, str) and "/" in model:
        provider, _, model_id = model.partition("/")
        if provider and model_id:
            return provider, model_id
    return None, None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _drain_pipe(pipe: Any, handle: OpenCodeHandle, name: str) -> None:
    try:
        for line in pipe:
            text = line.rstrip("\n")
            if name == "stderr":
                with handle.lock:
                    handle.stderr_tail.append(text)
            logger.debug("opencode %s: %s", name, text)
    except (OSError, ValueError):
        pass
