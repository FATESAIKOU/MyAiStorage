"""Unit tests for Angareion configuration, paths, identity and token files (v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from angareion.config import (
    CONFIG_PULL_INTERVAL_DEFAULT,
    Config,
    ConfigError,
    get_config_path,
    get_default_token_path,
    get_state_dir,
    identity_slug,
    load_config,
    save_config,
)


def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    monkeypatch.delenv("ANGAREION_STATE_DIR", raising=False)
    monkeypatch.delenv("ANGAREION_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert str(get_config_path()).endswith(".config/angareion/config.json")
    assert str(get_state_dir()).endswith(".local/state/angareion")
    assert str(get_default_token_path()).endswith(".config/angareion/token")


def test_config_env_overrides(tmp_path, monkeypatch):
    custom_cfg = tmp_path / "custom" / "my_config.json"
    custom_state = tmp_path / "custom_state"
    custom_token = tmp_path / "custom_token"

    monkeypatch.setenv("ANGAREION_CONFIG", str(custom_cfg))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(custom_state))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(custom_token))

    assert get_config_path() == custom_cfg
    assert get_state_dir() == custom_state
    assert get_default_token_path() == custom_token


def test_identity_slug_uses_the_agent_name():
    assert identity_slug("MyLinuxPool-impl-2") == "MyLinuxPool-impl-2"
    assert identity_slug("LLMGateway-pm") == "LLMGateway-pm"
    # Anything unsafe cannot escape the state directory.
    assert "/" not in identity_slug("owner/team×pm")
    assert identity_slug("") == "unknown"


def test_load_and_save_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    token_file = tmp_path / "token"
    token_file.write_text("dummy-token", encoding="utf-8")

    monkeypatch.setenv("ANGAREION_CONFIG", str(cfg_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))

    cfg = Config(
        identity="MyLinuxPool-impl-2",
        config_dir=tmp_path / "clone",
        registry_repo="owner/MyAiStorage",
        registry_issue=77,
        token_file=token_file,
        state_dir=state_dir,
        config_file=cfg_file,
    )
    save_config(cfg)
    assert cfg_file.is_file()

    loaded = load_config(cfg_file)
    assert loaded.identity == "MyLinuxPool-impl-2"
    assert loaded.config_dir == tmp_path / "clone"
    assert loaded.registry_repo == "owner/MyAiStorage"
    assert loaded.registry_issue == 77
    assert loaded.token_file == token_file
    assert loaded.state_dir == state_dir
    assert loaded.config_pull_interval == CONFIG_PULL_INTERVAL_DEFAULT == 900


def test_config_pull_interval_can_be_overridden(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "identity": "MyLinuxPool-impl-2",
                "config_dir": str(tmp_path / "clone"),
                "registry_repo": "owner/MyAiStorage",
                "registry_issue": 77,
                "config_pull_interval": 0,
            }
        ),
        encoding="utf-8",
    )
    assert load_config(cfg_file).config_pull_interval == 0


@pytest.mark.parametrize(
    "missing",
    ["identity", "config_dir", "registry_repo", "registry_issue"],
)
def test_missing_config_keys_are_config_errors(tmp_path, missing):
    data = {
        "identity": "MyLinuxPool-impl-2",
        "config_dir": str(tmp_path / "clone"),
        "registry_repo": "owner/MyAiStorage",
        "registry_issue": 77,
    }
    del data[missing]
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError, match=missing):
        load_config(cfg_file)


def test_config_validation_failures(tmp_path):
    base = {
        "identity": "MyLinuxPool-impl-2",
        "config_dir": str(tmp_path / "clone"),
        "registry_repo": "owner/MyAiStorage",
        "registry_issue": 77,
    }
    cfg_file = tmp_path / "config.json"

    for override, message in (
        ({"identity": "not an agent"}, "identity"),
        ({"config_dir": ""}, "config_dir"),
        ({"registry_repo": "no-slash"}, "registry_repo"),
        ({"registry_repo": "too/many/slashes"}, "registry_repo"),
        ({"registry_issue": 0}, "registry_issue"),
        ({"registry_issue": "77"}, "registry_issue"),
        ({"config_pull_interval": -1}, "config_pull_interval"),
        ({"config_pull_interval": "900"}, "config_pull_interval"),
    ):
        data = dict(base)
        data.update(override)
        cfg_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_config(cfg_file)


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.json")
