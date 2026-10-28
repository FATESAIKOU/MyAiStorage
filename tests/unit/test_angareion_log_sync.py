"""Unit tests for angareion log enumeration & incremental sync (tasks 2.2/2.4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import BackendError, FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelConfigError, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.log_cache import LogCache, log_cache_path
from angareion.messages import Message
from angareion.registry import FakeRegistryClient

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"


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


def _seed(env, msg_id, comment_id, created_at, content="hi", to=IDENTITY):
    target = entity(channel="general", issue=10)
    message = Message(from_=PM, to=to, channel="general", content=content, id=msg_id, at=created_at)
    env["backend"].deliver(target, message, comment_id=comment_id, created_at=created_at)
    return message


def test_enumerate_returns_tuple_with_parsed_none_for_nonmessage(cli_env):
    # deliver a normal message
    _seed(cli_env, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z")
    ent = cli_env["store"].entity_for("general")
    out = cli_env["backend"].enumerate_conditional(ent)[0]
    assert isinstance(out, list)
    assert len(out) == 1
    cid, created, parsed = out[0]
    assert isinstance(cid, int)
    assert created == "2026-10-10T12:00:05Z"
    assert parsed is not None
    # non-message: append a FakeComment with message=None
    from angareion.backend import FakeComment
    cli_env["backend"]._comments.append(FakeComment(id=2, created_at="2026-10-10T12:00:06Z", message=None))
    out2 = cli_env["backend"].enumerate_conditional(ent)[0]
    # non-message enumerated with parsed=None
    nonmsg = [x for x in out2 if x[0] == 2]
    assert len(nonmsg) == 1
    assert nonmsg[0][2] is None


def test_incremental_since_last_minus_60(cli_env, capsys):
    _seed(cli_env, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z")
    assert run(cli_env, ["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()
    # second seed later
    _seed(cli_env, "01BBBB00000000000000000000", 2, "2026-10-10T12:05:00Z")
    assert run(cli_env, ["log", "--channel", "general", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000", "01BBBB00000000000000000000"]


def test_idempotent_upsert_and_integer_compare(cli_env):
    # seed id 9 then 10; ensure 10 > 9 integer
    _seed(cli_env, "01AAAA00000000000000000000", 9, "2026-10-10T12:00:05Z", content="nine")
    _seed(cli_env, "01BBBB00000000000000000000", 10, "2026-10-10T12:00:06Z", content="ten")
    cache = LogCache(cli_env["state_dir"], IDENTITY)
    # first sync via log
    import subprocess
    # use main
    from angareion.cli import main as _main
    _main(["log", "--channel", "general", "--json"], backend=cli_env["backend"], store=cli_env["store"], registry=cli_env["registry"])
    # edit content of comment 9 (same position) by re-delivering? FakeBackend stores message ref; mutate
    for c in cli_env["backend"]._comments:
        if c.id == 9:
            c.message.content = "nine-edited"
    # force incremental: run log again
    _main(["log", "--channel", "general", "--json"], backend=cli_env["backend"], store=cli_env["store"], registry=cli_env["registry"])
    data_cache = cache.load("general", {"repo": "owner/repo", "issue": 10})
    msgs = data_cache["messages"]
    # content updated
    m9 = [m for m in msgs if m["comment_id"] == "9"][0]
    assert m9["message"]["content"] == "nine-edited"
    # last_comment_id is 10 not 9
    assert data_cache["last_comment_id"] == "10"
    # never-seen old position included: add comment id 8 later
    _seed(cli_env, "01CCCC00000000000000000000", 8, "2026-10-10T12:00:07Z", content="eight-late")
    _main(["log", "--channel", "general", "--json"], backend=cli_env["backend"], store=cli_env["store"], registry=cli_env["registry"])
    data_cache = cache.load("general", {"repo": "owner/repo", "issue": 10})
    ids = [int(m["comment_id"]) for m in data_cache["messages"]]
    assert 8 in ids
    assert data_cache["last_comment_id"] == "10"  # not retreat


def test_304_does_not_change_cache(cli_env, monkeypatch):
    _seed(cli_env, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z")
    from angareion.cli import main as _main
    _main(["log", "--channel", "general", "--json"], backend=cli_env["backend"], store=cli_env["store"], registry=cli_env["registry"])
    f = log_cache_path(cli_env["state_dir"], IDENTITY, "general")
    before = f.read_text()
    # monkeypatch enumerate_conditional to 304
    orig = cli_env["backend"].enumerate_conditional
    cli_env["backend"].enumerate_conditional = lambda *a, **k: ([], {"url": "x", "etag": "y"}, True)
    _main(["log", "--channel", "general", "--json"], backend=cli_env["backend"], store=cli_env["store"], registry=cli_env["registry"])
    after = f.read_text()
    assert before == after
    cli_env["backend"].enumerate_conditional = orig


def test_sync_failure_exits_2_empty_stdout(cli_env, monkeypatch):
    _seed(cli_env, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z")
    # make enumerate_conditional raise
    def boom(*a, **k):
        raise RuntimeError("backend boom")
    cli_env["backend"].enumerate_conditional = boom
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    rc = run(cli_env, ["log", "--channel", "general", "--json"])
    sys.stdout = real
    assert rc == 2
    assert buf.getvalue().strip() == ""


def test_sync_error_message_never_shows_the_token(cli_env, capsys):
    """F5: the sync error goes through `main`'s `mask_token`, like every other command."""
    _seed(cli_env, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z")

    def leaky(*a, **k):
        raise BackendError("HTTP 403 for " + TOKEN)

    cli_env["backend"].enumerate_conditional = leaky
    rc = run(cli_env, ["log", "--channel", "general", "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out.strip() == ""
    assert TOKEN not in captured.err
    assert "***" in captured.err


def test_ensure_fresh_failure_exits_2(cli_env, monkeypatch, capsys):
    """F5: a failed config pull is a backend/config error (exit 2), not a channel-missing exit 1."""
    _seed(cli_env, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z")

    def boom():
        raise ChannelConfigError("could not pull the channel configuration")

    monkeypatch.setattr(cli_env["store"], "ensure_fresh", boom)
    rc = run(cli_env, ["log", "--channel", "general", "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "could not pull" in captured.err
