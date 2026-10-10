"""Unit tests for Angareion configuration and paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from angareion.config import (
    Config,
    ConfigError,
    get_config_path,
    get_default_token_path,
    get_state_dir,
    load_config,
    save_config,
)


def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("A2A_CONFIG", raising=False)
    monkeypatch.delenv("A2A_STATE_DIR", raising=False)
    monkeypatch.delenv("A2A_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg_path = get_config_path()
    assert str(cfg_path).endswith(".config/angareion/config.json")

    state_dir = get_state_dir()
    assert str(state_dir).endswith(".local/state/angareion")

    token_path = get_default_token_path()
    assert str(token_path).endswith(".config/angareion/token")


def test_config_env_overrides(tmp_path, monkeypatch):
    custom_cfg = tmp_path / "custom" / "my_config.json"
    custom_state = tmp_path / "custom_state"
    custom_token = tmp_path / "custom_token"

    monkeypatch.setenv("A2A_CONFIG", str(custom_cfg))
    monkeypatch.setenv("A2A_STATE_DIR", str(custom_state))
    monkeypatch.setenv("A2A_TOKEN_FILE", str(custom_token))

    assert get_config_path() == custom_cfg
    assert get_state_dir() == custom_state
    assert get_default_token_path() == custom_token


def test_load_and_save_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    token_file = tmp_path / "token"
    token_file.write_text("dummy-token", encoding="utf-8")

    monkeypatch.setenv("A2A_CONFIG", str(cfg_file))
    monkeypatch.setenv("A2A_STATE_DIR", str(state_dir))
    monkeypatch.setenv("A2A_TOKEN_FILE", str(token_file))

    cfg = Config(
        channel_repo="owner-org/repo-channel",
        identity={"repo": "owner-org/repo-worker", "role": "PM", "name": "worker-1"},
        token_file=token_file,
        state_dir=state_dir,
        config_file=cfg_file,
    )
    save_config(cfg)
    assert cfg_file.is_file()

    loaded = load_config(cfg_file)
    assert loaded.channel_repo == "owner-org/repo-channel"
    assert loaded.identity["role"] == "PM"
    assert loaded.token_file == token_file
    assert loaded.state_dir == state_dir


def test_config_validation_failures(tmp_path):
    bad_cfg = tmp_path / "bad.json"
    # Missing channel_repo
    bad_cfg.write_text(json.dumps({"identity": {"repo": "a", "role": "b"}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="channel_repo"):
        load_config(bad_cfg)

    # Missing identity
    bad_cfg.write_text(json.dumps({"channel_repo": "owner/repo"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid identity"):
        load_config(bad_cfg)
