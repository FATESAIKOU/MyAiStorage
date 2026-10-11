"""OpenCodeDriver over a fake opencode serve (task 3.2).

The fake is a loopback HTTP server (tests/fakes/fake_opencode_serve.py); the
driver is exercised through its real HTTP calls, so the polling, the status
lookup and the quota mapping are all covered without the real opencode.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import logging
import socket
import subprocess
import sys
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


# --- attach: status lookup, send, abort ----------------------------------------


def test_attach_send_and_status(driver, serve, tmp_path):
    proc, port = serve()
    session = RunningSession(
        session_id="agora:A",
        process=AgoraProcess(proc=proc, port=port, agent_session_id="ses_A"),
    )
    handle = driver.launch(session, {}, str(tmp_path))
    assert driver.external_session_id(handle) == "ses_A"

    set_status(tmp_path / "fh", {"ses_A": {"type": "busy"}})
    driver.send(handle, "read channel X")
    assert wait_state(driver, handle, DRIVER_RUNNING).kind == DRIVER_RUNNING
    sent = wait_until(lambda: calls(tmp_path / "fh") and [
        c for c in calls(tmp_path / "fh")
        if c["method"] == "POST" and c["path"].endswith("/session/ses_A/message")
    ])
    assert sent[-1]["body"] == {"parts": [{"type": "text", "text": "read channel X"}]}

    set_status(tmp_path / "fh", {"ses_A": {"type": "idle"}})
    assert wait_state(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE

    driver.interrupt(handle)
    aborts = [c for c in calls(tmp_path / "fh")
              if c["method"] == "POST" and c["path"].endswith("/session/ses_A/abort")]
    assert aborts, "abort was not called"
    driver.stop(handle, graceful=False, timeout=5)


def test_status_may_be_the_flat_object(driver, serve, tmp_path):
    proc, port = serve()
    handle = driver.launch(
        RunningSession("agora:A", AgoraProcess(proc=proc, port=port, agent_session_id="ses_A")),
        {}, str(tmp_path),
    )
    set_status(tmp_path / "fh", {"type": "idle"})
    assert wait_state(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
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


def test_retry_with_a_non_quota_reason_stays_running(driver, serve, tmp_path, caplog):
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
    assert state.kind == DRIVER_RUNNING
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
    assert state.kind == DRIVER_RUNNING  # the last known state is kept

    start = time.monotonic()
    driver.send(handle, "x")  # fires in the background
    assert time.monotonic() - start < 1

    start = time.monotonic()
    with pytest.raises(Exception):
        driver.interrupt(handle)  # bounded by http_timeout
    assert time.monotonic() - start < 3
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
