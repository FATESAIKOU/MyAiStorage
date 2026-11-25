"""CLI-level tests for `angareion log` rebuilding a partly corrupt cache (F3).

The cache file is all-or-nothing: one `messages` entry with a missing or
unparseable field MUST void the whole file, so the next `log` does a full
synchronisation instead of failing (or silently dropping messages) forever.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import hashlib
import json

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.log_cache import LogCache, log_cache_path
from angareion.messages import Message
from angareion.registry import FakeRegistryClient

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
GENERAL = entity(channel="general", issue=10)
# Two hours apart: with `since = last_created - 60s` the first one is outside
# the incremental window, so it is only rebuilt by a full sync (F3's probe).
FIRST_AT = "2026-10-10T10:00:00Z"
SECOND_AT = "2026-10-10T12:00:00Z"


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


def run(env, argv):
    return main(argv, backend=env["backend"], store=env["store"], registry=env["registry"])


def seed_two_hours_apart(env):
    for msg_id, comment_id, created_at, content in (
        ("01AAAA00000000000000000000", 1, FIRST_AT, "first-message"),
        ("01BBBB00000000000000000000", 2, SECOND_AT, "second-message"),
    ):
        message = Message(
            from_=PM, to=IDENTITY, channel="general", content=content, id=msg_id, at=created_at
        )
        env["backend"].deliver(GENERAL, message, comment_id=comment_id, created_at=created_at)


def cache_file(env):
    return log_cache_path(env["state_dir"], IDENTITY, "general")


def assert_full_rebuild(env, capsys):
    """The next `log` succeeds and answers with both messages, as a full sync would."""
    capsys.readouterr()
    rc = run(env, ["log", "--channel", "general", "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    data = json.loads(captured.out)
    assert [m["id"] for m in data] == [
        "01AAAA00000000000000000000",
        "01BBBB00000000000000000000",
    ]
    assert [m["content"] for m in data] == ["first-message", "second-message"]
    rebuilt = json.loads(cache_file(env).read_text(encoding="utf-8"))
    assert [m["comment_id"] for m in rebuilt["messages"]] == ["1", "2"]
    assert [m["message"]["content"] for m in rebuilt["messages"]] == [
        "first-message",
        "second-message",
    ]


def test_missing_message_content_rebuilds_instead_of_exiting_2(cli_env, capsys):
    """F3: deleting `message.content` of the entry outside the window voids the file."""
    seed_two_hours_apart(cli_env)
    assert run(cli_env, ["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()

    path = cache_file(cli_env)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["messages"][0]["created_at"] == FIRST_AT  # the one outside the window
    del data["messages"][0]["message"]["content"]
    path.write_text(json.dumps(data), encoding="utf-8")
    corrupt = hashlib.sha256(path.read_bytes()).hexdigest()

    assert_full_rebuild(cli_env, capsys)
    assert hashlib.sha256(path.read_bytes()).hexdigest() != corrupt  # it was rewritten


def test_unparseable_comment_id_rebuilds_instead_of_dropping_the_message(cli_env, capsys):
    """F3: a `comment_id` that cannot become an int voids the file (no partial trust)."""
    seed_two_hours_apart(cli_env)
    assert run(cli_env, ["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()

    path = cache_file(cli_env)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"][0]["comment_id"] = "not-a-number"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert_full_rebuild(cli_env, capsys)


@pytest.mark.parametrize(
    "bad_state",
    [
        "x",
        ["x"],
        None,
        {"last_created_at": 5, "etag": None},
        {"last_created_at": None, "etag": "not-a-dict"},
        {"last_created_at": None, "etag": {"url": 1, "etag": "e"}},
    ],
    ids=["string", "list", "null", "time-not-str", "etag-not-dict", "etag-url-not-str"],
)
def test_a_bad_pages_entry_rebuilds_instead_of_a_traceback(cli_env, capsys, bad_state):
    """PR-2c review 中-1: each `pages` entry is checked, so a bad one voids the file (no AttributeError in sync)."""
    seed_two_hours_apart(cli_env)
    assert run(cli_env, ["log", "--channel", "general", "--json"]) == 0
    capsys.readouterr()

    path = cache_file(cli_env)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["pages"]) == {"10"}
    assert LogCache(cli_env["state_dir"], IDENTITY).load("general") is not None
    data["pages"]["10"] = bad_state
    path.write_text(json.dumps(data), encoding="utf-8")

    # The etag shapes are not seen by the Fake backend, so refuse them at load too.
    assert LogCache(cli_env["state_dir"], IDENTITY).load("general") is None
    assert_full_rebuild(cli_env, capsys)
