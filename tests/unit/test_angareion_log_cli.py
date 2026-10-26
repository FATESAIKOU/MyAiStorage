"""Unit tests for `angareion log` CLI input errors (tasks 2.1)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import io
import json
import os
import sys

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import FakeBackend, Cursor
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import build_parser, main
from angareion.config import Config, save_config
from angareion.messages import Message
from angareion.registry import FakeRegistryClient

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
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


def test_help_lists_log(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "log" in out


def test_bad_since_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--since", "yesterday"]) == 1
    assert capsys.readouterr().out == ""


def test_bad_until_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--until", "yesterday"]) == 1
    assert capsys.readouterr().out == ""


def test_since_after_until_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--since", "2026-10-10T12:00:00Z", "--until", "2026-10-10T11:00:00Z"]) == 1
    assert capsys.readouterr().out == ""


def test_non_ulid_around_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--around", "not-a-ulid"]) == 1
    assert capsys.readouterr().out == ""


def test_non_ulid_thread_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--thread", "not-a-ulid"]) == 1
    assert capsys.readouterr().out == ""


def test_around_and_thread_together_exits_1(cli_env, capsys):
    ulid = "01AAAA00000000000000000000"
    assert run(cli_env, ["log", "--channel", "general", "--around", ulid, "--thread", ulid]) == 1
    assert capsys.readouterr().out == ""


def test_isolated_before_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--before", "2"]) == 1
    assert capsys.readouterr().out == ""


def test_isolated_after_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--after", "2"]) == 1
    assert capsys.readouterr().out == ""


def test_negative_before_exits_1(cli_env, capsys):
    ulid = "01AAAA00000000000000000000"
    assert run(cli_env, ["log", "--channel", "general", "--around", ulid, "--before", "-1"]) == 1
    assert capsys.readouterr().out == ""


def test_negative_after_exits_1(cli_env, capsys):
    ulid = "01AAAA00000000000000000000"
    assert run(cli_env, ["log", "--channel", "general", "--around", ulid, "--after", "-1"]) == 1
    assert capsys.readouterr().out == ""


def test_negative_limit_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--limit", "-1"]) == 1
    assert capsys.readouterr().out == ""


def test_unknown_channel_exits_1(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "nowhere"]) == 1
    assert capsys.readouterr().out == ""


def test_log_channel_no_warning(cli_env, capsys):
    assert run(cli_env, ["log", "--channel", "general", "--json"]) == 0
    captured = capsys.readouterr()
    assert "Warning:" not in captured.err
    assert captured.out.strip() == "[]"


def test_duplicate_channel_is_a_union_and_hits_the_backend_once(cli_env, capsys):
    """F4: `--channel X --channel X` is one channel: no duplicate rows, one sync."""
    message = Message(
        from_=PM,
        to=IDENTITY,
        channel="general",
        content="only-once",
        id="01AAAA00000000000000000000",
        at="2026-10-10T12:00:05Z",
    )
    cli_env["backend"].deliver(GENERAL, message, comment_id=1, created_at="2026-10-10T12:00:05Z")

    synced = []
    real = cli_env["backend"].enumerate_conditional

    def counting_enumerate(target, **kwargs):
        synced.append(target.channel)
        return real(target, **kwargs)

    cli_env["backend"].enumerate_conditional = counting_enumerate

    rc = run(cli_env, ["log", "--channel", "general", "--channel", "general", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000"]
    assert synced == ["general"]
