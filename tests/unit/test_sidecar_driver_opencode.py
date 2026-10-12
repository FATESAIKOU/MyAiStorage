"""OpenCodeDriver over a fake opencode serve (task 3.2).

The fake is a loopback HTTP server (tests/fakes/fake_opencode_serve.py); the
driver is exercised through its real HTTP calls, so the polling, the status
lookup and the quota mapping are all covered without the real opencode.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import http.client
import json
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sidecar.agora_store import AgoraProcess
from sidecar.drivers.opencode import OpenCodeDriver
from sidecar.protocols import (
    DRIVER_EXITED,
    DRIVER_IDLE,
    DRIVER_QUOTA_EXHAUSTED,
    DRIVER_RUNNING,
    BadRequestError,
    NoModelError,
    RunningSession,
    Subscription,
    iso,
)

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE_SERVE = FAKES / "fake_opencode_serve.py"
SECRET = "OPENCODE-SECRET"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.02)


def wait_state(driver, handle, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.state(handle)
        if state.kind == kind:
            return state
        time.sleep(0.05)
    raise AssertionError(f"state stayed {driver.state(handle)!r}, wanted {kind!r}")


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-credential-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-credential-must-not-leak")
    return OpenCodeDriver(
        command=[sys.executable, str(FAKE_SERVE)], poll_timeout=5.0, http_timeout=5.0
    )


@pytest.fixture
def serve(tmp_path, monkeypatch):
    procs: list[subprocess.Popen] = []

    def _serve(*, delay: float | None = None, no_listen: bool = False):
        port = free_port()
        env = dict(__import__("os").environ)
        if delay:
            env["FAKE_OC_LISTEN_DELAY"] = str(delay)
        if no_listen:
            env["FAKE_OC_NO_LISTEN"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(FAKE_SERVE), "serve",
             "--hostname", "127.0.0.1", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
        procs.append(proc)
        return proc, port

    yield _serve
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def calls(fh: Path) -> list[dict]:
    path = fh / "fake-oc-calls.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def set_status(fh: Path, payload: dict) -> None:
    (fh / "fake-oc-status.json").write_text(json.dumps(payload), encoding="utf-8")


# --- env_for (spike-verified rules) --------------------------------------------


def test_env_for_maps_provider_and_model(driver):
    env = driver.env_for(Subscription(
        id="zen", provider="opencode-zen", kind="api-key",
        key=SECRET, endpoint="https://example.invalid",
        models={"default": "z-model", "allow": ["*"]},
    ))
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config == {
        "provider": {"opencode": {
            "options": {"apiKey": SECRET, "baseURL": "https://example.invalid"},
            "models": {"z-model": {"name": "z-model"}},
        }},
        "model": "opencode/z-model",
        "small_model": "opencode/z-model",
    }
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"


def test_env_for_model_falls_back_to_allow_and_refuses(driver):
    env = driver.env_for(Subscription(
        id="or", provider="openrouter", kind="api-key", key="k",
        models={"allow": ["*", "concrete-model"]},
    ))
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"] == "openrouter/concrete-model"

    with pytest.raises(NoModelError):
        driver.env_for(Subscription(id="x", provider="openrouter",
                                    models={"allow": ["*", "org/*"]}))
    with pytest.raises(BadRequestError):
        driver.env_for(Subscription(id="y", provider="anthropic",
                                    models={"default": "m"}))  # no mapped opencode id


def test_first_response_is_verified_against_the_chosen_model(driver, serve, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_OC_MESSAGE_PROVIDER", "opencode")
    monkeypatch.setenv("FAKE_OC_MESSAGE_MODEL", "wrong-model")
    proc, port = serve()
    env = driver.env_for(Subscription(
        id="zen", provider="opencode-zen", kind="api-key", key=SECRET,
        models={"default": "z-model"},
    ))
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        env, str(tmp_path),
    )
    with caplog.at_level(logging.ERROR):
        driver.send(handle, "hi")
        wait_until(lambda: "model mismatch" in caplog.text)
    driver.stop(handle, graceful=False, timeout=5)
    state = driver.state(handle)
    assert state.kind == DRIVER_EXITED
    assert "model mismatch" in (state.reason or "")


def test_a_matching_first_response_does_not_complain(driver, serve, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_OC_MESSAGE_PROVIDER", "opencode")
    monkeypatch.setenv("FAKE_OC_MESSAGE_MODEL", "z-model")
    proc, port = serve()
    env = driver.env_for(Subscription(
        id="zen", provider="opencode-zen", kind="api-key", key=SECRET,
        models={"default": "z-model"},
    ))
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        env, str(tmp_path),
    )
    with caplog.at_level(logging.ERROR):
        driver.send(handle, "hi")
        wait_until(lambda: [c for c in calls(tmp_path / "fh")
                            if c["path"].endswith("/message")])
        time.sleep(0.2)
    assert "model mismatch" not in caplog.text
    driver.stop(handle, graceful=False, timeout=5)


def test_opencode_errors_carry_the_masked_stderr_tail(tmp_path, monkeypatch, serve, caplog):
    """3.6.3: an HTTP 500 with no readable reason still carries stderr, masked."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("FAKE_OC_STDERR_ECHO", "OPENCODE_CONFIG_CONTENT")
    monkeypatch.setenv("FAKE_OC_MESSAGE_500", "1")
    driver = OpenCodeDriver(command=[sys.executable, str(FAKE_SERVE)],
                            poll_timeout=5.0, http_timeout=5.0)
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = driver.env_for(Subscription(id="zen", provider="opencode-zen", kind="api-key",
                                      key=SECRET, models={"default": "z"}))
    handle = driver.launch(None, env, str(workdir))
    try:
        with caplog.at_level(logging.WARNING):
            driver.send(handle, "hi")  # the failure is logged by the background round
            wait_until(lambda: "HTTP 500" in caplog.text)
        assert "<masked>" in caplog.text  # the config's key never appears raw
        assert SECRET not in caplog.text
    finally:
        driver.stop(handle, graceful=False, timeout=5)


# --- attach: status lookup, send, abort ----------------------------------------


def test_attach_send_and_status(driver, serve, tmp_path, monkeypatch):
    """M2: running follows the background send, not the status map."""
    monkeypatch.setenv("FAKE_OC_ROUND_SECONDS", "0.6")
    proc, port = serve()
    session = RunningSession(
        session_id="agora:A",
        process=AgoraProcess(proc=proc, port=port, agent_session_id="ses_A"),
    )
    handle = driver.launch(session, {}, str(tmp_path))
    assert driver.external_session_id(handle) == "ses_A"
    assert driver.state(handle).kind == DRIVER_IDLE  # nothing in flight yet

    set_status(tmp_path / "fh", {})  # the map never even names this session
    driver.send(handle, "read channel X")
    assert wait_state(driver, handle, DRIVER_RUNNING).kind == DRIVER_RUNNING
    sent = wait_until(lambda: calls(tmp_path / "fh") and [
        c for c in calls(tmp_path / "fh")
        if c["method"] == "POST" and c["path"].endswith("/session/ses_A/message")
    ])
    assert sent[-1]["body"] == {"parts": [{"type": "text", "text": "read channel X"}]}
    assert wait_state(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE  # the round ended

    driver.interrupt(handle)
    aborts = [c for c in calls(tmp_path / "fh")
              if c["method"] == "POST" and c["path"].endswith("/session/ses_A/abort")]
    assert aborts, "abort was not called"
    driver.stop(handle, graceful=False, timeout=5)


def test_status_is_only_read_for_retry(driver, serve, tmp_path):
    """M2: the status answer's shape does not drive running/idle, only retry."""
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    set_status(tmp_path / "fh", {"type": "busy"})
    time.sleep(0.3)
    assert driver.state(handle).kind == DRIVER_IDLE  # not running: no send in flight

    set_status(tmp_path / "fh", {"type": "retry", "action": {"reason": "free_tier_limit"}})
    assert wait_state(driver, handle, DRIVER_QUOTA_EXHAUSTED).kind == DRIVER_QUOTA_EXHAUSTED
    driver.stop(handle, graceful=False, timeout=5)


# --- the port line does not mean listening -------------------------------------


def test_launch_polls_until_the_port_listens(driver, serve, tmp_path):
    proc, port = serve(delay=0.6)
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    assert handle.port == port  # launch returned only after it accepted
    driver.stop(handle, graceful=False, timeout=5)


def test_launch_gives_up_when_the_port_never_listens(tmp_path, monkeypatch, serve):
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    driver = OpenCodeDriver(command=[sys.executable, str(FAKE_SERVE)], poll_timeout=0.6)
    proc, port = serve(no_listen=True)
    with pytest.raises(Exception) as excinfo:
        driver.launch(
            RunningSession("agora:A", AgoraProcess(proc=proc, port=port,
                                                   agent_session_id="ses_A")),
            {}, str(tmp_path),
        )
    assert "listened" in str(excinfo.value) or "never" in str(excinfo.value)
    wait_until(lambda: proc.poll() is not None)  # the driver killed it


# --- quota: retry + reason whitelist -------------------------------------------


def test_retry_free_tier_limit_far_away_is_1w(driver, serve, tmp_path):
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    next_ms = int((time.time() + 6 * 3600) * 1000)  # beyond 5h + 5m
    set_status(tmp_path / "fh", {"ses_A": {
        "type": "retry", "message": "Free usage exceeded",
        "action": {"reason": "free_tier_limit"}, "next": next_ms,
    }})
    state = wait_state(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "1w"
    assert state.resets_at == iso(next_ms / 1000.0)
    driver.stop(handle, graceful=False, timeout=5)


def test_retry_free_tier_limit_near_is_5h(driver, serve, tmp_path):
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    next_ms = int((time.time() + 3600) * 1000)  # inside 5h
    set_status(tmp_path / "fh", {"ses_A": {
        "type": "retry", "action": {"reason": "free_tier_limit"}, "next": next_ms,
    }})
    assert wait_state(driver, handle, DRIVER_QUOTA_EXHAUSTED).window == "5h"
    driver.stop(handle, graceful=False, timeout=5)


def test_retry_without_next_is_5h_without_resets(driver, serve, tmp_path):
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    set_status(tmp_path / "fh", {"ses_A": {
        "type": "retry", "action": {"reason": "free_tier_limit"},
    }})
    state = wait_state(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "5h" and state.resets_at is None
    driver.stop(handle, graceful=False, timeout=5)


def test_retry_with_a_non_quota_reason_is_ignored(driver, serve, tmp_path, caplog):
    """A retry with an off-whitelist reason is only logged, never swapped on."""
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    set_status(tmp_path / "fh", {"ses_A": {
        "type": "retry", "action": {"reason": "overloaded"}, "next": 123,
    }})
    with caplog.at_level(logging.WARNING):
        time.sleep(0.3)
        state = driver.state(handle)
    assert state.kind == DRIVER_IDLE  # no round in flight; not quota either
    assert handle.quota is None
    assert "non-quota reason" in caplog.text
    driver.stop(handle, graceful=False, timeout=5)


def test_state_send_and_interrupt_are_bounded(tmp_path, monkeypatch, serve):
    """N2: every HTTP call has an internal timeout, so a tick cannot hang."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("FAKE_OC_HANG", "1")  # the fake never answers
    driver = OpenCodeDriver(
        command=[sys.executable, str(FAKE_SERVE)],
        poll_timeout=5.0, http_timeout=0.3, status_timeout=0.3,
    )
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    start = time.monotonic()
    state = driver.state(handle)
    assert time.monotonic() - start < 3  # bounded by status_timeout
    assert state.kind == DRIVER_IDLE  # no round in flight; the map is not trusted

    start = time.monotonic()
    driver.send(handle, "x")  # fires in the background
    assert time.monotonic() - start < 1

    start = time.monotonic()
    with pytest.raises(Exception):
        driver.interrupt(handle)  # bounded by http_timeout
    assert time.monotonic() - start < 3
    driver.stop(handle, graceful=False, timeout=5)


# --- M2: the long message timeout and the visible round failure -----------------


def test_a_long_round_is_not_cut_by_the_short_http_timeout(tmp_path, monkeypatch, serve):
    """M2/D6: a round longer than the 10s general timeout runs to the end."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("FAKE_OC_ROUND_SECONDS", "1.0")
    driver = OpenCodeDriver(
        command=[sys.executable, str(FAKE_SERVE)],
        poll_timeout=5.0, http_timeout=0.3, status_timeout=0.3,
    )
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    driver.send(handle, "a long round")
    assert wait_state(driver, handle, DRIVER_RUNNING).kind == DRIVER_RUNNING
    time.sleep(0.5)  # past http_timeout: the round is still in flight, uncut
    assert driver.state(handle).kind == DRIVER_RUNNING
    assert wait_state(driver, handle, DRIVER_IDLE, timeout=10.0).kind == DRIVER_IDLE
    assert handle.last_error is None
    driver.stop(handle, graceful=False, timeout=5)


def test_info_error_marks_the_round_failed(tmp_path, monkeypatch):
    """M2 spec: HTTP 200 with info.error writes exit.reason, not just a log."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("FAKE_OC_MESSAGE_INFO_ERROR", "provider auth failed")
    driver = OpenCodeDriver(
        command=[sys.executable, str(FAKE_SERVE)], poll_timeout=5.0, http_timeout=5.0,
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = driver.env_for(Subscription(
        id="zen", provider="opencode-zen", kind="api-key", key=SECRET,
        models={"default": "z"},
    ))
    handle = driver.launch(None, env, str(workdir))
    try:
        driver.send(handle, "hi")
        state = wait_state(driver, handle, DRIVER_EXITED)
        assert "provider auth failed" in (state.reason or "")
        assert SECRET not in (state.reason or "")
        assert handle.last_error is not None and "provider auth failed" in handle.last_error
        assert SECRET not in (handle.last_error or "")
    finally:
        driver.stop(handle, graceful=False, timeout=5)


def test_non_2xx_marks_the_round_failed(tmp_path, monkeypatch):
    """M2 spec: an HTTP 500 without a readable reason lands in exit.reason."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("FAKE_OC_STDERR_ECHO", "OPENCODE_CONFIG_CONTENT")
    monkeypatch.setenv("FAKE_OC_MESSAGE_500", "1")
    driver = OpenCodeDriver(
        command=[sys.executable, str(FAKE_SERVE)], poll_timeout=5.0, http_timeout=5.0,
    )
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = driver.env_for(Subscription(id="zen", provider="opencode-zen", kind="api-key",
                                      key=SECRET, models={"default": "z"}))
    handle = driver.launch(None, env, str(workdir))
    try:
        driver.send(handle, "hi")
        state = wait_state(driver, handle, DRIVER_EXITED)
        assert "HTTP 500" in (state.reason or "")
        assert "<masked>" in (state.reason or "")
        assert SECRET not in (state.reason or "")
    finally:
        driver.stop(handle, graceful=False, timeout=5)


def test_an_unexpected_request_error_does_not_leave_the_agent_running(
    driver, serve, tmp_path, monkeypatch
):
    """A (section-3 review): IncompleteRead is an HTTPException, not an OSError.

    The round still ends - idle or exited, never a stuck `running` - and the
    failure is visible in `last_error`.
    """
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    real_request = driver._request

    def broken_request(h, method, path, *, body=None, timeout=None):
        if path.endswith("/message"):
            raise http.client.IncompleteRead(b'{"ok"', 64)
        return real_request(h, method, path, body=body, timeout=timeout)

    monkeypatch.setattr(driver, "_request", broken_request)
    driver.send(handle, "hi")

    wait_until(lambda: driver.state(handle).kind != DRIVER_RUNNING)
    state = driver.state(handle)
    assert state.kind in (DRIVER_IDLE, DRIVER_EXITED)
    assert "IncompleteRead" in (handle.last_error or "")
    driver.stop(handle, graceful=False, timeout=5)


def test_two_interleaved_rounds_stay_running_until_the_last_one(
    driver, serve, tmp_path, monkeypatch
):
    """B (section-3 review): the first of two rounds to finish must not report
    idle while the second is still in flight."""
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    real_request = driver._request
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()
    started = {"n": 0}

    def gated_request(h, method, path, *, body=None, timeout=None):
        if path.endswith("/message"):
            started["n"] += 1
            if started["n"] == 1:
                first_started.set()
                first_release.wait(5)
            else:
                second_started.set()
                second_release.wait(5)
            return {"info": {}}
        return real_request(h, method, path, body=body, timeout=timeout)

    monkeypatch.setattr(driver, "_request", gated_request)
    driver.send(handle, "one")
    assert first_started.wait(2)
    driver.send(handle, "two")
    assert second_started.wait(2)

    first_release.set()
    wait_until(lambda: handle.sending == 1)  # the first round is over ...
    assert driver.state(handle).kind == DRIVER_RUNNING  # ... the second still runs

    second_release.set()
    assert wait_state(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
    assert handle.sending == 0
    driver.stop(handle, graceful=False, timeout=5)


def test_aborted_round_error_does_not_exit_the_agent(
    driver, serve, tmp_path, monkeypatch
):
    """C (section-3 review): the round interrupt aborted ends as a recorded
    error, not an exited agent - even though it comes back with info.error."""
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    real_request = driver._request
    message_started = threading.Event()
    message_release = threading.Event()

    def gated_request(h, method, path, *, body=None, timeout=None):
        if path.endswith("/message"):
            message_started.set()
            message_release.wait(10)
            return {"info": {"error": "aborted by user"}}
        return real_request(h, method, path, body=body, timeout=timeout)

    monkeypatch.setattr(driver, "_request", gated_request)
    driver.send(handle, "a long round")
    assert message_started.wait(2)

    driver.interrupt(handle)
    message_release.set()  # the aborted round now comes back with info.error

    wait_until(lambda: handle.last_error is not None and "aborted by user" in handle.last_error)
    assert driver.state(handle).kind == DRIVER_IDLE
    assert handle.sending == 0
    driver.stop(handle, graceful=False, timeout=5)


def test_a_late_success_from_an_interrupted_round_does_not_report_idle(
    driver, serve, tmp_path, monkeypatch
):
    """Low 1 (section-3 review): the epoch check also covers the success path.

    interrupt clears `sending` and bumps the epoch; the aborted round then
    comes back *successfully*.  That late success must not decrement the count
    of the round that took its place: the state stays running until the second
    round is really over.
    """
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    real_request = driver._request
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()
    finishes = threading.Event()
    started = {"n": 0}

    def gated_request(h, method, path, *, body=None, timeout=None):
        if path.endswith("/message"):
            started["n"] += 1
            if started["n"] == 1:
                first_started.set()
                first_release.wait(10)
            else:
                second_started.set()
                second_release.wait(10)
            return {"info": {}}  # both rounds come back successfully
        return real_request(h, method, path, body=body, timeout=timeout)

    real_finish = driver._finish_round

    def observed_finish(h, failure, epoch):
        try:
            return real_finish(h, failure, epoch)
        finally:
            finishes.set()  # the second round is still gated, so this is round one

    monkeypatch.setattr(driver, "_request", gated_request)
    monkeypatch.setattr(driver, "_finish_round", observed_finish)

    driver.send(handle, "one")
    assert first_started.wait(2)
    driver.interrupt(handle)  # abort it: sending resets, the epoch moves on
    driver.send(handle, "two")
    assert second_started.wait(2)

    first_release.set()  # the interrupted round comes back as a success
    assert finishes.wait(2)  # its bookkeeping ran ...
    assert handle.sending == 1  # ... it did not count the second round out
    assert driver.state(handle).kind == DRIVER_RUNNING  # still running

    second_release.set()
    assert wait_state(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
    assert handle.sending == 0
    driver.stop(handle, graceful=False, timeout=5)


# --- new mode ------------------------------------------------------------------


def test_new_mode_spawns_serve_creates_the_session_and_keeps_creds_out(driver, tmp_path, monkeypatch):
    workdir = tmp_path / "w"
    workdir.mkdir()
    handle = driver.launch(None, {"OPENCODE_CONFIG_CONTENT": json.dumps(
        {"provider": {"opencode-zen": {"options": {"apiKey": SECRET}}}}
    )}, str(workdir))
    try:
        argv_log = tmp_path / "fh" / "fake-oc-args.log"
        wait_until(lambda: argv_log.exists())
        argv = json.loads(argv_log.read_text().splitlines()[-1])
        assert argv[0] == "serve" and argv[argv.index("--hostname") + 1] == "127.0.0.1"
        assert int(argv[argv.index("--port") + 1]) == handle.port
        assert SECRET not in json.dumps(argv)  # the secret travels in the env

        names_log = tmp_path / "fh" / "fake-oc-env-names.log"
        wait_until(lambda: names_log.exists())
        names = json.loads(names_log.read_text().splitlines()[-1])["env"]
        assert "OPENCODE_CONFIG_CONTENT" in names
        assert "ANTHROPIC_API_KEY" not in names  # the sidecar's own key did not leak
        assert "OPENAI_API_KEY" not in names

        assert handle.agent_session_id == "ses_createdfake00001"  # POST /session
        assert wait_until(lambda: [c for c in calls(tmp_path / "fh")
                                   if c["method"] == "POST"
                                   and c["path"].rstrip("/").endswith("/session")])
        driver.send(handle, "hello")
        assert wait_until(lambda: [c for c in calls(tmp_path / "fh")
                                   if c["path"].endswith("/ses_createdfake00001/message")])
    finally:
        driver.stop(handle, graceful=False, timeout=5)
