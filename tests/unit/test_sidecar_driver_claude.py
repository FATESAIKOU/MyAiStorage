"""ClaudeCodeDriver over a scripted stream-json fake (task 3.1).

The fake claude is a Python script (tests/fakes/fake_claude_stream.py): it
reads user messages and interrupts on stdin and answers with scripted events.
No real claude, no network, no Drive.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sidecar.agora_store import AgoraProcess
from sidecar.drivers.claude_code import ClaudeCodeDriver, ClaudeDriverError
from sidecar.protocols import (
    DRIVER_IDLE,
    DRIVER_QUOTA_EXHAUSTED,
    DRIVER_RUNNING,
    NoModelError,
    RunningSession,
    Subscription,
    iso,
)

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE = FAKES / "fake_claude_stream.py"
RESUME = "11111111-2222-4333-8444-555555555555"
SECRET = "SUBSCRIPTION-SECRET"


def wait_for(driver, handle, kind, timeout=5.0, window=None):
    deadline = time.monotonic() + timeout
    state = driver.state(handle)
    while time.monotonic() < deadline:
        state = driver.state(handle)
        if state.kind == kind and (window is None or state.window == window):
            return state
        time.sleep(0.02)
    raise AssertionError(f"state stayed {state!r}, wanted {kind!r}")


def wait_until(predicate, timeout=5.0):
    """Poll until `predicate()` is truthy; return its value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.02)


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-credential-must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-credential-must-not-leak")
    return ClaudeCodeDriver(command=[sys.executable, str(FAKE)])


def env_names(fh: Path) -> list[list[str]]:
    return [
        json.loads(line)["env"]
        for line in (fh / "claude-env-names.log").read_text().splitlines()
    ]


def stdin_log(fh: Path) -> list[dict]:
    path = fh / "claude-stdin.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def attach(driver, tmp_path, monkeypatch):
    """Attach to a fake claude process already running with --resume."""
    procs: list[subprocess.Popen] = []

    def _attach(*, scenario: str = "ok", is_api_key: bool = False, env: dict[str, str] | None = None):
        monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", scenario)
        proc = subprocess.Popen(
            [sys.executable, str(FAKE), "--resume", RESUME],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=dict(__import__("os").environ),
        )
        procs.append(proc)
        session = RunningSession(
            session_id="agora:A",
            process=AgoraProcess(proc=proc, agent_session_id=RESUME),
        )
        if env is not None:
            launch_env = env
        elif is_api_key:
            launch_env = {"ANTHROPIC_API_KEY": SECRET}
        else:
            launch_env = {"CLAUDE_CODE_OAUTH_TOKEN": SECRET}
        return driver.launch(session, launch_env, str(tmp_path))

    yield _attach
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- env_for (spike-verified rules) -------------------------------------------


def test_env_for_rules(driver):
    switches = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    # anthropic api-key -> x-api-key
    anthropic = driver.env_for(Subscription(
        id="a", provider="anthropic", kind="api-key", key=SECRET
    ))
    assert anthropic == {"ANTHROPIC_API_KEY": SECRET, **switches}

    # other providers' api-key -> bearer only (LLMGateway decision 7)
    openrouter = driver.env_for(Subscription(
        id="b", provider="openrouter", kind="api-key", key=SECRET,
        endpoint="https://example.invalid", models={"default": "acme/model"},
    ))
    assert openrouter == {
        "ANTHROPIC_AUTH_TOKEN": SECRET,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "ANTHROPIC_MODEL": "acme/model",
        "ANTHROPIC_SMALL_FAST_MODEL": "acme/model",
        **switches,
    }

    # a subscription keeps Claude's own default model
    subscription = driver.env_for(Subscription(
        id="c", provider="anthropic", kind="claude-subscription", key=SECRET
    ))
    assert subscription == {"CLAUDE_CODE_OAUTH_TOKEN": SECRET, **switches}

    # non-anthropic without a default: allow's first concrete entry
    fallback = driver.env_for(Subscription(
        id="d", provider="openrouter", kind="api-key", key="k",
        models={"allow": ["*", "concrete-model"]},
    ))
    assert fallback["ANTHROPIC_MODEL"] == "concrete-model"
    with pytest.raises(NoModelError):
        driver.env_for(Subscription(
            id="e", provider="openrouter", kind="api-key", key="k",
            models={"allow": ["*", "org/*"]},
        ))


# --- the stream-json round trip -----------------------------------------------


def test_send_runs_then_result_returns_idle(driver, attach, tmp_path):
    handle = attach(scenario="ok")
    assert driver.state(handle).kind == DRIVER_IDLE
    assert driver.external_session_id(handle) == RESUME

    driver.send(handle, "hello")
    assert wait_for(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE  # result came

    sent = stdin_log(tmp_path / "fh")[-1]
    assert sent == {"type": "user", "message": {"role": "user", "content": "hello"}}


def test_hold_result_keeps_the_round_running(driver, attach, tmp_path, monkeypatch):
    handle = attach(scenario="hold")
    driver.send(handle, "slow")
    time.sleep(0.2)
    assert driver.state(handle).kind == DRIVER_RUNNING  # no result yet

    handle.proc.stdin.close()  # D6: closing stdin ends the process
    assert wait_for(driver, handle, "exited").kind == "exited"


def test_interrupt_writes_a_control_request(driver, attach, tmp_path):
    handle = attach(scenario="hold")
    driver.send(handle, "working")
    driver.interrupt(handle)
    sent = wait_until(
        lambda: next(
            (line for line in stdin_log(tmp_path / "fh")
             if line.get("type") == "control_request"),
            None,
        )
    )
    assert sent["request"]["subtype"] == "interrupt"
    assert sent["request_id"]
    assert wait_for(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE  # result followed


def test_new_mode_spawns_headless_and_keeps_parent_credentials_out(driver, tmp_path, monkeypatch):
    workdir = tmp_path / "w"
    workdir.mkdir()
    handle = driver.launch(None, {"CLAUDE_CODE_OAUTH_TOKEN": SECRET}, str(workdir))
    try:
        argv_log = tmp_path / "fh" / "claude-args.log"
        wait_until(lambda: argv_log.exists())
        argv = json.loads(argv_log.read_text().splitlines()[-1])
        assert argv[:5] == ["-p", "--input-format", "stream-json", "--output-format",
                            "stream-json"]
        assert "--resume" not in argv
        assert argv[argv.index("--settings") + 1] == '{"autoContinueAtUsageLimit": false}'
        assert SECRET not in json.dumps(argv)

        names_log = tmp_path / "fh" / "claude-env-names.log"
        wait_until(lambda: names_log.exists())
        names = env_names(tmp_path / "fh")[-1]
        assert "CLAUDE_CODE_OAUTH_TOKEN" in names
        assert "ANTHROPIC_API_KEY" not in names  # the sidecar's own key did not leak
        assert "OPENAI_API_KEY" not in names

        driver.send(handle, "hi")
        assert wait_for(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
        assert driver.external_session_id(handle) == "11111111-2222-4333-8444-666666666666"
    finally:
        driver.stop(handle, graceful=False, timeout=5)


def test_stop_kills_the_process_and_reports_exited(driver, attach):
    handle = attach(scenario="hold")
    driver.send(handle, "x")
    driver.stop(handle, graceful=True, timeout=5)
    assert handle.proc.poll() is not None
    assert driver.state(handle).kind == "exited"


def test_claude_errors_carry_the_masked_stderr_tail(driver, tmp_path, monkeypatch):
    """3.6.3: a write against a dead process reports its stderr, keys masked."""
    monkeypatch.setenv("FAKE_CLAUDE_STDERR_ECHO", "CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "die")
    workdir = tmp_path / "w"
    workdir.mkdir()
    handle = driver.launch(None, {"CLAUDE_CODE_OAUTH_TOKEN": SECRET}, str(workdir))
    try:
        wait_until(lambda: handle.proc.poll() is not None)  # it died at startup
        with pytest.raises(ClaudeDriverError) as excinfo:
            driver.send(handle, "hi")
        message = str(excinfo.value)
        assert "fake claude" in message  # the child's stderr really came through
        assert "<masked>" in message
        assert SECRET not in message
    finally:
        driver.stop(handle, graceful=False, timeout=5)


# --- quota events --------------------------------------------------------------


def test_rejected_five_hour_is_quota_with_rfc3339_reset(driver, attach):
    handle = attach(scenario="quota_rejected_five")
    driver.send(handle, "go")
    state = wait_for(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "5h"
    assert state.resets_at == iso(4102444800.0)
    driver.stop(handle, graceful=False, timeout=5)


def test_rejected_seven_day_is_1w(driver, attach):
    handle = attach(scenario="quota_rejected_seven")
    driver.send(handle, "go")
    assert wait_for(driver, handle, DRIVER_QUOTA_EXHAUSTED).window == "1w"
    driver.stop(handle, graceful=False, timeout=5)


def test_unknown_rate_type_is_5h_without_resets(driver, attach, caplog):
    handle = attach(scenario="quota_unknown_type")
    with caplog.at_level(logging.WARNING):
        driver.send(handle, "go")
        state = wait_for(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "5h" and state.resets_at is None
    assert "unknown rateLimitType" in caplog.text
    driver.stop(handle, graceful=False, timeout=5)


def test_unknown_status_is_not_exhaustion_and_is_logged(driver, attach, caplog):
    handle = attach(scenario="quota_unknown_status")
    with caplog.at_level(logging.WARNING):
        driver.send(handle, "go")
        assert wait_for(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
    assert "unknown rate_limit status" in caplog.text
    assert "weird_value" in caplog.text  # the event itself, minus secrets
    driver.stop(handle, graceful=False, timeout=5)


def test_allowed_warning_is_not_exhaustion(driver, attach):
    handle = attach(scenario="quota_allowed_warning")
    driver.send(handle, "go")
    assert wait_for(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
    driver.stop(handle, graceful=False, timeout=5)


def test_credits_required_is_exhaustion(driver, attach):
    handle = attach(scenario="quota_credits")
    driver.send(handle, "go")
    state = wait_for(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "1w"  # rateLimitType wins
    assert state.resets_at == iso(4102444800.0)
    driver.stop(handle, graceful=False, timeout=5)


def test_api_retry_rate_limit_is_exhaustion(driver, attach):
    handle = attach(scenario="quota_api_retry", is_api_key=True)
    driver.send(handle, "go")
    state = wait_for(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "5h" and state.resets_at is None
    driver.stop(handle, graceful=False, timeout=5)


def test_transient_api_retry_does_not_mark_quota(driver, attach):
    """M1: a single 429 followed by a successful round is not quota exhaustion."""
    handle = attach(scenario="quota_api_retry_transient", is_api_key=True)
    driver.send(handle, "go")
    state = wait_for(driver, handle, DRIVER_IDLE)
    assert state.kind == DRIVER_IDLE
    assert handle.quota is None
    driver.stop(handle, graceful=False, timeout=5)


def test_api_retry_other_error_is_not_exhaustion(driver, attach):
    handle = attach(scenario="api_retry_other")
    driver.send(handle, "go")
    assert wait_for(driver, handle, DRIVER_IDLE).kind == DRIVER_IDLE
    driver.stop(handle, graceful=False, timeout=5)


def test_result_text_youve_hit_your_is_exhaustion(driver, attach):
    handle = attach(scenario="quota_hit_your", is_api_key=True)
    driver.send(handle, "go")
    state = wait_for(driver, handle, DRIVER_QUOTA_EXHAUSTED)
    assert state.window == "5h" and state.resets_at is None
    driver.stop(handle, graceful=False, timeout=5)


def test_result_text_coincidence_does_not_mark_quota(driver, attach):
    """M1: text starting with You've hit your but is_error is False is not quota."""
    handle = attach(scenario="result_hit_your_coincidence", is_api_key=True)
    driver.send(handle, "go")
    state = wait_for(driver, handle, DRIVER_IDLE)
    assert state.kind == DRIVER_IDLE
    assert handle.quota is None
    driver.stop(handle, graceful=False, timeout=5)


def test_subscription_ignores_auxiliary_signals(driver, attach):
    """M1: claude-subscription ignores api_retry and result text without rate_limit_event."""
    handle = attach(scenario="quota_api_retry", is_api_key=False)
    driver.send(handle, "go")
    wait_until(lambda: handle.state.kind == DRIVER_IDLE or handle.quota is not None)
    assert handle.quota is None
    driver.stop(handle, graceful=False, timeout=5)
