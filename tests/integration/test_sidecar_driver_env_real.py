"""Real-binary env_for checks (task 3.6.4; the architect's spike setup).

These start the actual `claude` / `opencode` binaries against a loopback fake
provider and check only *booleans* about the credentials it sees: the key must
be the subscription's and the parent process's key must not leak.

They never run with the default unit suite: `SIDECAR_REAL_AGENT_TESTS=1` is
required, and `tests/integration/` is not in pytest's default testpaths (D6).
They do not touch Drive or agora.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))

from fake_llm_provider import FakeProvider  # noqa: E402

from sidecar.drivers.claude_code import ClaudeCodeDriver  # noqa: E402
from sidecar.drivers.opencode import OpenCodeDriver  # noqa: E402
from sidecar.protocols import Subscription  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SIDECAR_REAL_AGENT_TESTS") != "1",
        reason="real agent binaries are opt-in (SIDECAR_REAL_AGENT_TESTS=1)",
    ),
]

SUBSCRIPTION_KEY = "subscription-key-for-the-spike"
PARENT_KEY = "parent-key-that-must-not-leak"


def _binaries(*names: str) -> list[str]:
    found = [shutil.which(name) for name in names]
    if not all(found):
        pytest.skip(f"missing binary: {', '.join(n for n, f in zip(names, found) if not f)}")
    return [str(f) for f in found]


@pytest.fixture(autouse=True)
def parent_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PARENT_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", PARENT_KEY)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)


def _workdir(tmp_path: Path) -> str:
    work = tmp_path / "work"
    work.mkdir()
    return str(work)


def test_claude_api_key_reaches_the_provider_and_the_parents_does_not(tmp_path):
    claude, = _binaries("claude")
    provider = FakeProvider(SUBSCRIPTION_KEY, PARENT_KEY)
    try:
        driver = ClaudeCodeDriver(command=claude)
        env = driver.env_for(Subscription(
            id="anthropic-direct", provider="anthropic", kind="api-key",
            key=SUBSCRIPTION_KEY, endpoint=provider.base_url,
            models={"default": "claude-sonnet-5-5"},
        ))
        handle = driver.launch(None, env, _workdir(tmp_path))
        try:
            driver.send(handle, "hi")
            seen = provider.wait()
        finally:
            driver.stop(handle, graceful=False, timeout=10)
    finally:
        provider.close()
    assert seen["x_api_key"] is True
    assert seen["saw_subscription_key"] is True
    assert seen["saw_parent_key"] is False


def test_claude_openrouter_style_uses_a_bearer_token(tmp_path):
    claude, = _binaries("claude")
    provider = FakeProvider(SUBSCRIPTION_KEY, PARENT_KEY)
    try:
        driver = ClaudeCodeDriver(command=claude)
        env = driver.env_for(Subscription(
            id="openrouter", provider="openrouter", kind="api-key",
            key=SUBSCRIPTION_KEY, endpoint=provider.base_url,
            models={"default": "acme/some-model"},
        ))
        handle = driver.launch(None, env, _workdir(tmp_path))
        try:
            driver.send(handle, "hi")
            seen = provider.wait()
        finally:
            driver.stop(handle, graceful=False, timeout=10)
    finally:
        provider.close()
    assert seen["authorization"] is True
    assert seen["x_api_key"] is False  # bearer only (LLMGateway decision 7)
    assert seen["saw_subscription_key"] is True
    assert seen["saw_parent_key"] is False


def test_opencode_reaches_the_provider_with_the_subscription_key(tmp_path):
    opencode, = _binaries("opencode")
    provider = FakeProvider(SUBSCRIPTION_KEY, PARENT_KEY)
    try:
        driver = OpenCodeDriver(command=opencode, poll_timeout=30.0, http_timeout=30.0)
        env = driver.env_for(Subscription(
            id="openrouter", provider="openrouter", kind="api-key",
            key=SUBSCRIPTION_KEY, endpoint=provider.base_url,
            models={"default": "acme/some-model"},
        ))
        handle = driver.launch(None, env, _workdir(tmp_path))
        try:
            driver.send(handle, "hi")
            seen = provider.wait(timeout=90.0)
        finally:
            driver.stop(handle, graceful=False, timeout=15)
    finally:
        provider.close()
    assert seen["saw_subscription_key"] is True
    assert seen["saw_parent_key"] is False
