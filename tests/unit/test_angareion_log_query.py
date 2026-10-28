"""Unit tests for angareion log query engine (tasks 2.5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import FakeBackend, Cursor
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.registry import FakeRegistryClient
from angareion.messages import Message

TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
ULID = "01AAAA00000000000000000000"


@pytest.fixture
def two_env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    clone = tmp_path / "clone"
    clone.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    config = Config(identity=IDENTITY, config_dir=clone, registry_repo="owner/MyAiStorage", registry_issue=77, token_file=token_file, state_dir=state_dir, config_file=config_file)
    save_config(config, config_file)
    monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))

    git = FakeGit(
        {
            f"origin/main:{CHANNELS_PATH}": channels_json({"general": ("github-main", 10), "other": ("github-main", 7)}),
            f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
        },
        fetch_head_mtime=1000.0,
    )
    store = ChannelStore(clone, git=git, interval=0)
    backend = FakeBackend(store)
    backend.add_entity("general", number=10)
    backend.add_entity("other", number=7, repo="owner/repo")
    registry = FakeRegistryClient()
    return {"config": config, "git": git, "store": store, "backend": backend, "registry": registry, "tmp_path": tmp_path, "state_dir": state_dir}


def run(env, argv):
    return main(argv, backend=env["backend"], store=env["store"], registry=env["registry"])


def seed(env, channel, entity_number, msg_id, comment_id, created_at, content="msg", to=IDENTITY, in_reply_to=None):
    target = entity(channel=channel, issue=entity_number)
    message = Message(from_=PM, to=to, channel=channel, content=content, id=msg_id, at=created_at, in_reply_to=in_reply_to)
    env["backend"].deliver(target, message, comment_id=comment_id, created_at=created_at)
    return message


def caprun(env, argv):
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    rc = run(env, argv)
    sys.stdout = real
    return rc, buf.getvalue()


def test_default_all_channels(two_env, capsys):
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z", content="g")
    seed(two_env, "other", 7, "01BBBB00000000000000000000", 2, "2026-10-10T12:00:01Z", content="o")
    rc, out = caprun(two_env, ["log", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["id"] for m in data] == ["01BBBB00000000000000000000", "01AAAA00000000000000000000"]


def test_filter_intersection(two_env, capsys):
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z", content="blocker", to=IDENTITY)
    seed(two_env, "general", 10, "01CCCC00000000000000000000", 2, "2026-10-10T12:00:06Z", content="other", to="someone-pm-m1")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--from", PM, "--grep", "blocker", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000"]


def test_mentioned_with_channel(two_env, capsys):
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z", content="x", to=None, )  # broadcast
    # mention me via to=IDENTITY? Instead seed with mentions
    msg = Message(from_=PM, to="someone-pm-m1", channel="general", content="hey", id="01BBBB00000000000000000000", at="2026-10-10T12:00:06Z", mentions=[IDENTITY])
    two_env["backend"].deliver(entity(channel="general", issue=10), msg, comment_id=2, created_at="2026-10-10T12:00:06Z")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--mentioned", "--json"])
    assert rc == 0
    data = json.loads(out)
    # broadcast excluded, mentioned included
    assert [m["id"] for m in data] == ["01BBBB00000000000000000000"]


def test_time_boundaries_inclusive(two_env, capsys):
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T03:00:00Z", content="a")
    seed(two_env, "general", 10, "01BBBB00000000000000000000", 2, "2026-10-10T22:00:00Z", content="b")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--since", "2026-10-10T03:00:00Z", "--until", "2026-10-10T22:00:00Z", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000", "01BBBB00000000000000000000"]


def test_grep_literal_case_insensitive_content_only(two_env, capsys):
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z", content="Blocker: disk full")
    # from name contains Blocker but content not
    msg = Message(from_="other-proj-pm-m1", to=IDENTITY, channel="general", content="nothing", id="01BBBB00000000000000000000", at="2026-10-10T12:00:06Z")
    two_env["backend"].deliver(entity(channel="general", issue=10), msg, comment_id=2, created_at="2026-10-10T12:00:06Z")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--grep", "blocker", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000"]


def test_around_window(two_env, capsys):
    for i in range(1, 21):
        seed(two_env, "general", 10, f"01M{i:02d}000000000000000000000", i, f"2026-10-10T12:00:{i:02d}Z", content=f"m{i}")
    # anchor m10 -> id 01M10...
    anchor = "01M10000000000000000000000"
    rc, out = caprun(two_env, ["log", "--channel", "general", "--around", anchor, "--before", "3", "--after", "4", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == [f"m{i}" for i in range(7, 15)]


def test_around_single(two_env, capsys):
    for i in range(1, 6):
        seed(two_env, "general", 10, f"01M{i:02d}000000000000000000000", i, f"2026-10-10T12:00:{i:02d}Z", content=f"m{i}")
    anchor = "01M03000000000000000000000"
    rc, out = caprun(two_env, ["log", "--channel", "general", "--around", anchor, "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == ["m3"]


def test_anchor_filtered_out_exit0(two_env, capsys):
    # m1 A, m2 B, m3 A
    seed(two_env, "general", 10, "01M01000000000000000000000", 1, "2026-10-10T12:00:01Z", content="m1", to=IDENTITY)  # from PM (A-ish)
    # set from by using PM already; to filter from PM
    # We'll just use --from with PM and anchor m2 (B not PM) but m2 from PM too... adjust: make m2 from someone else impossible (from_=PM fixed). 
    # Instead test grep filter excludes anchor
    seed(two_env, "general", 10, "01M02000000000000000000000", 2, "2026-10-10T12:00:02Z", content="other")
    seed(two_env, "general", 10, "01M03000000000000000000000", 3, "2026-10-10T12:00:03Z", content="m3")
    anchor = "01M02000000000000000000000"
    rc, out = caprun(two_env, ["log", "--channel", "general", "--grep", "m", "--around", anchor, "--before", "1", "--after", "1", "--json"])
    # anchor content "other" filtered out; m1/m3? m1 content m1 contains m, m3 contains m
    assert rc == 0
    data = json.loads(out)
    assert "01M02000000000000000000000" not in [m["id"] for m in data]


def test_anchor_not_in_selected_exit1(two_env, capsys):
    seed(two_env, "general", 10, "01M01000000000000000000000", 1, "2026-10-10T12:00:01Z", content="m1")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--around", "01ZZZZ00000000000000000000", "--json"])
    assert rc == 1
    assert out.strip() == ""


def test_thread_closure(two_env, capsys):
    # m5 replies m3, m8 replies m3, m9 replies m5
    seed(two_env, "general", 10, "01M03000000000000000000000", 3, "2026-10-10T12:00:03Z", content="m3")
    seed(two_env, "general", 10, "01M05000000000000000000000", 5, "2026-10-10T12:00:05Z", content="m5", in_reply_to="01M03000000000000000000000")
    seed(two_env, "general", 10, "01M08000000000000000000000", 8, "2026-10-10T12:00:08Z", content="m8", in_reply_to="01M03000000000000000000000")
    seed(two_env, "general", 10, "01M09000000000000000000000", 9, "2026-10-10T12:00:09Z", content="m9", in_reply_to="01M05000000000000000000000")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--thread", "01M05000000000000000000000", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == ["m3", "m5", "m8", "m9"]


def test_thread_anchor_as_root(two_env, capsys):
    seed(two_env, "general", 10, "01M05000000000000000000000", 5, "2026-10-10T12:00:05Z", content="m5", in_reply_to="01ZZZZ00000000000000000000")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--thread", "01M05000000000000000000000", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == ["m5"]


def test_thread_root_in_other_channel(two_env, capsys):
    # m5 in general replies to m1 in other (different channel) -> treated as root
    seed(two_env, "other", 7, "01M01000000000000000000000", 1, "2026-10-10T12:00:01Z", content="other-m1")
    seed(two_env, "general", 10, "01M05000000000000000000000", 5, "2026-10-10T12:00:05Z", content="m5", in_reply_to="01M01000000000000000000000")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--thread", "01M05000000000000000000000", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == ["m5"]


def test_limit_tail(two_env, capsys):
    for i in range(1, 31):
        seed(two_env, "general", 10, f"01M{i:02d}000000000000000000000", i, f"2026-10-10T12:00:{i:02d}Z", content=f"m{i}")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--limit", "5", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == [f"m{i}" for i in range(26, 31)]


def test_limit_cross_channel_global_latest(two_env, capsys):
    # general all earlier, other has latest
    for i in range(1, 4):
        seed(two_env, "general", 10, f"01G{i:02d}00000000000000000000", i, f"2026-10-10T11:00:{i:02d}Z", content=f"g{i}")
    seed(two_env, "other", 7, "01OLATEST00000000000000000", 10, "2026-10-10T12:00:00Z", content="latest")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--channel", "other", "--limit", "1", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == ["latest"]


def test_around_limit_truncates_anchor(two_env, capsys):
    for i in range(1, 8):
        seed(two_env, "general", 10, f"01M{i:02d}000000000000000000000", i, f"2026-10-10T12:00:{i:02d}Z", content=f"m{i}")
    anchor = "01M04000000000000000000000"
    rc, out = caprun(two_env, ["log", "--channel", "general", "--around", anchor, "--before", "3", "--after", "2", "--limit", "2", "--json"])
    assert rc == 0
    data = json.loads(out)
    # window m1..m6 filtered all, limit 2 -> last 2
    assert [m["content"] for m in data] == ["m5", "m6"]


def test_json_multiline(two_env, capsys):
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z", content="line1\nline2\nline3")
    rc, out = caprun(two_env, ["log", "--channel", "general", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert data[0]["content"] == "line1\nline2\nline3"
    # text mode
    rc2, out2 = caprun(two_env, ["log", "--channel", "general"])
    assert "line1" in out2 and "line2" in out2 and "line3" in out2


def test_nonmessage_not_counted_in_around(two_env, capsys):
    # seed 5 messages plus a non-message comment in between
    for i in range(1, 6):
        seed(two_env, "general", 10, f"01M{i:02d}000000000000000000000", i, f"2026-10-10T12:00:{i:02d}Z", content=f"m{i}")
    from angareion.backend import FakeComment
    two_env["backend"]._comments.append(FakeComment(id=99, created_at="2026-10-10T12:00:99Z", message=None))
    # anchor m3, before 1 after 1 -> m2,m3,m4 (non-message not counted)
    anchor = "01M03000000000000000000000"
    rc, out = caprun(two_env, ["log", "--channel", "general", "--around", anchor, "--before", "1", "--after", "1", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"] for m in data] == ["m2", "m3", "m4"]


def test_cursor_guard_sha256_empty_and_nonempty(two_env, capsys, monkeypatch):
    # empty cursor identity? use current env (no cursor file yet)
    state_dir = two_env["state_dir"]
    cursor_file = state_dir / "cursor-mylinuxpool-impl-w1-1.json"
    # empty case
    if cursor_file.exists():
        cursor_file.unlink()

    class RaisingCursorStore:
        def __init__(self, *a, **k):
            pass
        def cursor(self, key):
            return Cursor()
        def save(self, *a, **k):
            raise RuntimeError("should not be called")
        def save_many(self, *a, **k):
            raise RuntimeError("should not be called")

    # monkeypatch CursorStore in cli module
    import angareion.cli as cli_mod
    monkeypatch.setattr(cli_mod, "CursorStore", RaisingCursorStore)

    # run a variety of log commands
    seed(two_env, "general", 10, "01AAAA00000000000000000000", 1, "2026-10-10T12:00:05Z", content="g")
    for argv in (["log", "--channel", "general", "--json"],
                 ["log", "--channel", "general", "--around", "01AAAA00000000000000000000", "--before", "1", "--after", "1", "--json"],
                 ["log", "--channel", "general", "--limit", "1", "--json"]):
        rc, _ = caprun(two_env, argv)
        assert rc == 0

    # cursor file should still not exist (empty case)
    assert not cursor_file.exists()

    # non-empty cursor case: create cursor file manually
    cursor_file.write_text(json.dumps({"channel:general": {"position": "1", "at": "2026-10-10T12:00:05Z", "seen": ["1"]}}))
    before_hash = hashlib.sha256(cursor_file.read_bytes()).hexdigest()
    for argv in (["log", "--channel", "general", "--json"],
                 ["log", "--channel", "general", "--grep", "g", "--json"]):
        rc, _ = caprun(two_env, argv)
        assert rc == 0
    after_hash = hashlib.sha256(cursor_file.read_bytes()).hexdigest()
    assert before_hash == after_hash
