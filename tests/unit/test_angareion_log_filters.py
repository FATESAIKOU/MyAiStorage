"""Unit tests for the `angareion log` filter semantics that had no guard (F6).

Each filter is tested on its own, so removing or weakening it in `cmd_log`
turns exactly one of these red: `--from` alone, the `--until` boundary, and
`--to` (direct recipient + broadcast + mentions).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import io
import json
import sys

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.messages import Message
from angareion.registry import FakeRegistryClient

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM_A = "proj-a-pm-m1"
PM_B = "proj-b-pm-m1"
TARGET = "proj-c-impl-m1-1"
SOMEONE_ELSE = "proj-d-pm-m1"
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
    return {
        "config": config,
        "git": git,
        "store": store,
        "backend": backend,
        "registry": registry,
        "tmp_path": tmp_path,
        "state_dir": state_dir,
    }


def seed(
    env,
    comment_id,
    created_at,
    *,
    from_=PM_A,
    to=IDENTITY,
    content="msg",
    mentions=None,
):
    msg_id = f"01M{comment_id:02d}000000000000000000000"
    message = Message(
        from_=from_,
        to=to,
        channel="general",
        content=content,
        id=msg_id,
        at=created_at,
        mentions=list(mentions or []),
    )
    env["backend"].deliver(GENERAL, message, comment_id=comment_id, created_at=created_at)
    return msg_id


def log_json(env, argv):
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    try:
        rc = main(argv, backend=env["backend"], store=env["store"], registry=env["registry"])
    finally:
        sys.stdout = real
    return rc, buf.getvalue()


def test_from_alone_keeps_only_that_sender(cli_env):
    """`--from A` with no other filter: A's mail only (spec「- `--from <AI 名|me>`」)."""
    a = seed(cli_env, 1, "2026-10-10T12:00:01Z", from_=PM_A, content="from-a")
    seed(cli_env, 2, "2026-10-10T12:00:02Z", from_=PM_B, content="from-b")
    seed(cli_env, 3, "2026-10-10T12:00:03Z", from_=PM_B, content="from-b-again")

    rc, out = log_json(cli_env, ["log", "--channel", "general", "--from", PM_A, "--json"])
    assert rc == 0
    assert [m["id"] for m in json.loads(out)] == [a]


def test_until_boundary_includes_the_boundary_and_excludes_one_second_later(cli_env):
    """`--until` is inclusive (`received_at <= T`), so T+1s is out (spec「皆含邊界」)."""
    at_boundary = seed(cli_env, 1, "2026-10-10T12:00:00Z", content="at-boundary")
    seed(cli_env, 2, "2026-10-10T12:00:01Z", content="one-second-later")

    rc, out = log_json(
        cli_env,
        ["log", "--channel", "general", "--until", "2026-10-10T12:00:00Z", "--json"],
    )
    assert rc == 0
    assert [m["id"] for m in json.loads(out)] == [at_boundary]


def test_to_matches_direct_broadcast_and_mentions_but_not_others(cli_env):
    """`--to X` = `to` is X, or no `to` (broadcast), or `mentions` has X (spec「- `--to <AI 名|me>`」)."""
    direct = seed(cli_env, 1, "2026-10-10T12:00:01Z", to=TARGET, content="direct")
    broadcast = seed(cli_env, 2, "2026-10-10T12:00:02Z", to=None, content="broadcast")
    mentioned = seed(
        cli_env,
        3,
        "2026-10-10T12:00:03Z",
        to=SOMEONE_ELSE,
        content="mentioned",
        mentions=[TARGET],
    )
    seed(cli_env, 4, "2026-10-10T12:00:04Z", to=SOMEONE_ELSE, content="elsewhere")

    rc, out = log_json(cli_env, ["log", "--channel", "general", "--to", TARGET, "--json"])
    assert rc == 0
    assert [m["id"] for m in json.loads(out)] == [direct, broadcast, mentioned]
