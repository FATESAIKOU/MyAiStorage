"""Tests for mask_secrets / extract_secrets (3.7.3, M3).

Only real secrets are masked: `key` values and `OPENCODE_CONFIG_CONTENT`'s
`apiKey`.  Switch flags like "1", model names, and endpoint URLs are preserved.
"""

from __future__ import annotations

import sys as _s, pathlib as _p
_s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
import _guard  # noqa: E402,F401

import json
import sys
from pathlib import Path

from sidecar.drivers.common import extract_secrets, mask_secrets
from sidecar.drivers.claude_code import ClaudeCodeDriver
from sidecar.drivers.opencode import OpenCodeDriver
from sidecar.protocols import Subscription

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE_CLAUDE = FAKES / "fake_claude_stream.py"
FAKE_SERVE = FAKES / "fake_opencode_serve.py"

S_KEY = "sk-ant-api-03-abc-very-long-key-value-1234567890"


# --- extract_secrets: only real secrets land in the list -----------------------


def test_extract_secrets_does_not_include_switch_flags():
    env = {
        "ANTHROPIC_API_KEY": S_KEY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    secrets = extract_secrets(env)
    assert secrets == (S_KEY,)


def test_extract_secrets_does_not_include_model_names_or_endpoints():
    env = {
        "ANTHROPIC_API_KEY": S_KEY,
        "ANTHROPIC_BASE_URL": "https://api.example.com",
        "ANTHROPIC_MODEL": "gpt-4o",
        "ANTHROPIC_SMALL_FAST_MODEL": "gpt-4o",
    }
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "https://api.example.com" not in secrets
    assert "gpt-4o" not in secrets
    assert len(secrets) == 1


def test_extract_secrets_does_not_mask_short_values():
    """M3: '1' is too short to be masked even if someone passed it by hand."""
    text = "agent exited with code 1 after 12 retries"
    assert mask_secrets(text, ["1"]) == text


def test_extract_secrets_includes_opencode_config_api_key():
    config = {"provider": {"opencode": {"options": {"apiKey": S_KEY, "baseURL": "https://example.invalid"}}}}
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "https://example.invalid" not in secrets


def test_extract_secrets_deduplicates():
    env = {
        "ANTHROPIC_API_KEY": S_KEY,
        "ANTHROPIC_AUTH_TOKEN": S_KEY,
        "CLAUDE_CODE_OAUTH_TOKEN": "alt-token-0123456789abcdef",
    }
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "alt-token-0123456789abcdef" in secrets
    assert len(secrets) == 2


def test_extract_secrets_ignores_unknown_key_suffixes():
    env = {
        "FOO": S_KEY,            # not a recognised secret key
        "FOO_KEY": S_KEY,        # ends with _KEY -> secret
        "BAR": "some-value",
    }
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "some-value" not in secrets
    assert len(secrets) == 1  # S_KEY is deduplicated


# --- mask_secrets: only real secrets are masked --------------------------------


def test_mask_secrets_preserves_non_secret_numbers():
    """M3: 'code 1 after 12 retries' must come through intact."""
    text = "agent exited with code 1 after 12 retries"
    assert mask_secrets(text, []) == text
    assert mask_secrets(text, [""]) == text


def test_mask_secrets_masks_real_keys():
    text = f"error: bad request (key={S_KEY})"
    masked = mask_secrets(text, (S_KEY,))
    assert "<masked>" in masked
    assert S_KEY not in masked


def test_mask_secrets_short_value_not_masked():
    """A value shorter than 4 chars is never masked."""
    assert mask_secrets("code 1 ok", ("1",)) == "code 1 ok"


# --- integration: env_for -> extract_secrets -> mask --------------------------------


def test_claude_env_for_secrets_only_real_keys():
    driver = ClaudeCodeDriver(command="claude")
    env = driver.env_for(Subscription(
        id="a", provider="anthropic", kind="api-key", key=S_KEY
    ))
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "1" not in secrets
    assert "true" not in secrets
    assert len(secrets) == 1


def test_opencode_env_for_secrets_only_api_key_from_config():
    driver = OpenCodeDriver(command="opencode")
    env = driver.env_for(Subscription(
        id="zen", provider="opencode-zen", kind="api-key",
        key=S_KEY, endpoint="https://example.invalid",
        models={"default": "z-model"},
    ))
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "https://example.invalid" not in secrets
    assert "z-model" not in secrets
    assert "1" not in secrets


def test_claude_env_for_secrets_for_oauth():
    driver = ClaudeCodeDriver(command="claude")
    env = driver.env_for(Subscription(
        id="a", provider="anthropic", kind="claude-subscription", key=S_KEY
    ))
    secrets = extract_secrets(env)
    assert S_KEY in secrets
    assert "1" not in secrets


def test_masking_preserves_code_message_and_masks_key():
    """M3 core scenario: stderr tail has 'code 1 after 12 retries' + a key leak."""
    env = {
        "ANTHROPIC_API_KEY": S_KEY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    secrets = extract_secrets(env)
    stderr_tail = f"agent exited with code 1 after 12 retries (token={S_KEY})"
    masked = mask_secrets(stderr_tail, secrets)
    assert "code 1 after 12 retries" in masked  # preserved
    assert "<masked>" in masked
    assert S_KEY not in masked


# --- launch: the handle carries only real secrets ------------------------------


def test_claude_launch_handle_secrets_only_real_keys(tmp_path, monkeypatch):
    """M3 mutation guard: the driver must not put every env value in handle.secrets."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    driver = ClaudeCodeDriver(command=[sys.executable, str(FAKE_CLAUDE)])
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = driver.env_for(Subscription(
        id="a", provider="openrouter", kind="api-key", key=S_KEY,
        endpoint="https://or.invalid", models={"default": "acme/model"},
    ))
    handle = driver.launch(None, env, str(workdir))
    try:
        assert handle.secrets == (S_KEY,)
        assert "1" not in handle.secrets
        assert "https://or.invalid" not in handle.secrets
        assert "acme/model" not in handle.secrets
    finally:
        driver.stop(handle, graceful=False, timeout=5)


def test_opencode_launch_handle_secrets_only_config_api_key(tmp_path, monkeypatch):
    """M3 mutation guard: only OPENCODE_CONFIG_CONTENT's apiKey is a secret."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    driver = OpenCodeDriver(command=[sys.executable, str(FAKE_SERVE)], poll_timeout=5.0)
    workdir = tmp_path / "w"
    workdir.mkdir()
    env = driver.env_for(Subscription(
        id="zen", provider="opencode-zen", kind="api-key", key=S_KEY,
        endpoint="https://example.invalid", models={"default": "z-model"},
    ))
    handle = driver.launch(None, env, str(workdir))
    try:
        assert handle.secrets == (S_KEY,)
        assert "1" not in handle.secrets
        assert "https://example.invalid" not in handle.secrets
        assert "z-model" not in handle.secrets
    finally:
        driver.stop(handle, graceful=False, timeout=5)
