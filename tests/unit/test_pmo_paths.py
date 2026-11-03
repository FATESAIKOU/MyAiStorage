"""Unit tests for `pmo.paths` and the error family (`#110` T-1, design §3.4, D-4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from pathlib import Path

import pytest

from pmo.errors import MissingEnvError, PMOError, SchemaError
from pmo.paths import CONFIG_ENV, STATE_DIR_ENV, get_config_path, get_state_dir


@pytest.fixture(autouse=True)
def _no_settings_env(monkeypatch):
    """The two variables are read from the environment only; start from none."""
    for name in (CONFIG_ENV, STATE_DIR_ENV):
        monkeypatch.delenv(name, raising=False)


def test_the_two_paths_come_from_the_environment(monkeypatch, tmp_path):
    settings = tmp_path / "cfg" / "teams.json"
    state = tmp_path / "state"
    monkeypatch.setenv(CONFIG_ENV, str(settings))
    monkeypatch.setenv(STATE_DIR_ENV, str(state))
    assert get_config_path() == settings
    assert get_state_dir() == state


@pytest.mark.parametrize("name", [CONFIG_ENV, STATE_DIR_ENV])
def test_a_missing_variable_refuses_to_run(monkeypatch, name):
    with pytest.raises(MissingEnvError) as caught:
        get_config_path() if name == CONFIG_ENV else get_state_dir()
    assert name in str(caught.value)
    assert "default path" in str(caught.value)


@pytest.mark.parametrize("name", [CONFIG_ENV, STATE_DIR_ENV])
def test_an_empty_variable_is_not_a_path(monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(MissingEnvError):
        get_config_path() if name == CONFIG_ENV else get_state_dir()


def test_the_value_is_taken_as_written(monkeypatch):
    # No `~` expansion and no guessed parent: the environment is the whole truth.
    monkeypatch.setenv(CONFIG_ENV, "~/pmo/teams.json")
    monkeypatch.setenv(STATE_DIR_ENV, "relative/state")
    assert get_config_path() == Path("~/pmo/teams.json")
    assert get_state_dir() == Path("relative/state")


def test_the_error_family_is_value_error_based():
    # `angareion` uses `ConfigError(ValueError)`; callers map these onto exit codes.
    assert issubclass(PMOError, ValueError)
    assert issubclass(MissingEnvError, PMOError)
    assert issubclass(SchemaError, PMOError)
