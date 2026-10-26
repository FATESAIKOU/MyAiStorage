"""Unit tests for angareion log cleanup (#263)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import os
import sys

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import build_parser, main
from angareion.config import Config, save_config
from angareion.messages import Message
from angareion.registry import FakeRegistryClient

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
GENERAL = entity(channel="general", issue=10)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    clone = tmp_path / "clone"
    clone.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    config = Config(
        identity=IDENTITY,
        config_dir=clone,
        registry_repo="owner/MyAiStorage",
        registry_issue=77,
        token_file=token_file,
        state_dir=state_dir,
        config_file=config_file,
    )
    save_config(config, config_file)

    monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))

    git = FakeGit(
        {
            f"origin/main:{CHANNELS_PATH}": channels_json({"general": ("github-main", 10)}),
            f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
        },
        fetch_head_mtime=1000.0,
    )
    store = ChannelStore(clone, git=git, interval=0)
    backend = FakeBackend(store)
    backend.add_entity("general", number=10)
    registry = FakeRegistryClient()
    return {"config": config, "git": git, "store": store, "backend": backend, "registry": registry, "tmp_path": tmp_path, "state_dir": state_dir}


def run(env, argv):
    return main(argv, backend=env["backend"], store=env["store"], registry=env["registry"])


def test_channel_validation_non_channel_not_found_error_exits_2_no_token(cli_env, monkeypatch, capsys):
    token = TOKEN
    def boom(ch):
        raise RuntimeError("boom " + token + " leaked")
    monkeypatch.setattr(cli_env["store"], "entity_for", boom)

    rc = run(cli_env, ["log", "--channel", "general", "--json"])
    captured = capsys.readouterr()
    assert rc == 2, (rc, captured.err)
    assert token not in captured.err
    assert token not in captured.out