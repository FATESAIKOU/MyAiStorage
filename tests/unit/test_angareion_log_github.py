"""Unit tests for `angareion log` against the real GitHub backend (ETag path)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import urllib.parse

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.github import GitHubIssuesBackend
from angareion.log_cache import LogCache
from angareion.messages import Message, serialize_message
from angareion.registry import FakeRegistryClient
from test_angareion_etag import ETagTransport

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
COMMENTS = "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=100"


def _comment(comment_id, *, created_at="2026-10-10T12:00:05Z", content="hi"):
    message = Message(from_=PM, to=IDENTITY, channel="general", content=content, id=f"01AAAA{comment_id:018d}", at=created_at)
    return {
        "id": comment_id,
        "body": serialize_message(message),
        "created_at": created_at,
        "updated_at": created_at,
        "html_url": f"https://github.com/owner/repo/issues/10#issuecomment-{comment_id}",
    }


@pytest.fixture
def gh_env(tmp_path, monkeypatch):
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
    transport = ETagTransport()
    from angareion.state import FileETagCache
    backend = GitHubIssuesBackend(store, token=TOKEN, transport=transport, etag_cache=FileETagCache(state_dir, IDENTITY))
    registry = FakeRegistryClient()
    return {"config": config, "git": git, "store": store, "backend": backend, "registry": registry, "tmp_path": tmp_path, "state_dir": state_dir, "transport": transport}


def run(env, argv, capsys=None):
    rc = main(argv, backend=env["backend"], store=env["store"], registry=env["registry"], transport=env["transport"])
    return rc


def since_url():
    return COMMENTS + "&since=" + urllib.parse.quote("2026-10-10T11:59:05Z", safe="")


def test_fresh_identity_single_page_with_etag(gh_env, capsys):
    transport = gh_env["transport"]
    transport.serve(COMMENTS, [_comment(1)], etag='"e1"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    out = capsys.readouterr().out
    assert "hi" in out


def test_clean_incremental_records_etag(gh_env, capsys):
    transport = gh_env["transport"]
    su = since_url()
    transport.serve(COMMENTS, [_comment(1)], etag=None)
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()

    transport.serve(su, [_comment(1)], etag='"e1"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()
    assert transport.statuses[-1] == 200
    data = LogCache(gh_env["state_dir"], IDENTITY).load("general", {"repo": "owner/repo", "issue": 10})
    assert data["etag"] == {"url": su, "etag": '"e1"'}


def test_second_sends_if_none_match_304(gh_env, capsys):
    transport = gh_env["transport"]
    su = since_url()
    transport.serve(COMMENTS, [_comment(1)], etag=None)
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()

    transport.serve(su, [_comment(1)], etag='"e1"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()

    transport.serve(su, [_comment(1)], etag='"e1"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    captured = capsys.readouterr()
    assert transport.requests[-1]["headers"].get("If-None-Match") == '"e1"'
    assert transport.statuses[-1] == 304
    assert "hi" in captured.out


def test_new_message_not_recorded(gh_env, capsys):
    transport = gh_env["transport"]
    su = since_url()
    transport.serve(COMMENTS, [_comment(1)], etag=None)
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()

    transport.serve(su, [_comment(1), _comment(2, created_at="2026-10-10T12:10:00Z")], etag='"e2"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()
    data = LogCache(gh_env["state_dir"], IDENTITY).load("general", {"repo": "owner/repo", "issue": 10})
    assert data["etag"] is None


def test_full_page_not_recorded(gh_env, capsys):
    transport = gh_env["transport"]
    comments = [_comment(i) for i in range(1, 101)]
    transport.serve(COMMENTS, comments, etag='"e3"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()
    data = LogCache(gh_env["state_dir"], IDENTITY).load("general", {"repo": "owner/repo", "issue": 10})
    assert data["etag"] is None


def test_next_page_not_recorded(gh_env, capsys):
    transport = gh_env["transport"]
    page2 = COMMENTS + "&page=2"
    transport.serve(COMMENTS, [_comment(1)], etag='"e4"', link=f'<{page2}>; rel="next"')
    transport.serve(page2, [_comment(2)], etag='"e5"')
    rc = run(gh_env, ["log", "--channel", "general"], capsys)
    assert rc == 0
    capsys.readouterr()
    data = LogCache(gh_env["state_dir"], IDENTITY).load("general", {"repo": "owner/repo", "issue": 10})
    assert data["etag"] is None