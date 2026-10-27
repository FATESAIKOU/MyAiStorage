"""A new identity's first read (#224): "to me" starts at now, not at the beginning.

A new identity has no cursor file, so its first `read` used to return every
message in every channel that counts as "to me" — including every broadcast
that was already there, from every team. The rule now:

- a message addressed to me (`to` is my name, or it mentions me) still reads
  from the start: a dispatch is sent before the agent is opened, so it must be
  readable then;
- a plain broadcast (no `to`, no mention of me) reads only from the backend's
  own time at that first read; older broadcasts need `--since`;
- after that first read the cursor moves as usual, and `--channel` / `--mentioned`
  keep reading from the start.

The backend time here is the fake backend's injected clock, the stand-in for
GitHub's response `Date` header (see test_angareion_github.py).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from _angareion_fakes import FakeGit, FakeRegistryClient, backends_json, channels_json, entity
from angareion.backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.messages import Message

TOKEN = "gh" + "p_FAKE_NEW_IDENTITY_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"
GENERAL = entity(channel="general", issue=10)
OTHER = entity(channel="other", issue=7)
# The backend's clock when the identity first reads; the fixture's messages are
# older than this unless a test says otherwise.
NOW = "2026-10-10T12:00:00Z"
OLD = "2026-10-10T11:00:00Z"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated identity with two channels, a fake backend and a pinned backend clock."""
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
            f"origin/main:{CHANNELS_PATH}": channels_json(
                {"general": ("github-main", 10), "other": ("github-main", 7)}
            ),
            f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
        },
        fetch_head_mtime=1000.0,
    )
    store = ChannelStore(clone, git=git, interval=0, warn=lambda _msg: None, now=lambda: 1000.0)
    clock = {"now": NOW}
    backend = FakeBackend(store, clock=lambda: clock["now"])
    backend.add_entity("general", number=10)
    backend.add_entity("other", number=7, repo="owner/repo")
    return {
        "backend": backend,
        "store": store,
        "registry": FakeRegistryClient(),
        "clock": clock,
        "state_dir": state_dir,
    }


def run(env, argv):
    return main(argv, backend=env["backend"], store=env["store"], registry=env["registry"])


def seed(
    env,
    *,
    channel: str = "general",
    entity_number: int = 10,
    msg_id: str = "01AAAA00000000000000000000",
    to: str | None = IDENTITY,
    mentions: list[str] | None = None,
    comment_id: int | None = None,
    created_at: str = OLD,
) -> None:
    target = entity(channel=channel, issue=entity_number)
    message = Message(
        from_=PM, to=to, channel=channel, content=f"message {msg_id}", id=msg_id, mentions=mentions or []
    )
    env["backend"].deliver(target, message, comment_id=comment_id, created_at=created_at)


def read_ids(env, capsys, argv=None):
    assert run(env, argv or ["read", "--json"]) == 0
    return [message["id"] for message in json.loads(capsys.readouterr().out)]


def test_a_message_addressed_to_me_before_the_first_read_is_read(env, capsys):
    """A dispatch is sent before the agent opens; the first read must find it."""
    seed(env, msg_id="01TOME0000000000000000000A", to=IDENTITY, comment_id=1, created_at=OLD)
    assert read_ids(env, capsys) == ["01TOME0000000000000000000A"]


def test_an_old_broadcast_in_another_channel_is_not_read_first(env, capsys):
    """#224: another team's old broadcast must not land in the new inbox."""
    seed(env, channel="other", entity_number=7, msg_id="01OLDBC000000000000000000A", to=None, comment_id=1)

    assert read_ids(env, capsys) == []

    # The cursor did move past it: the next read still has nothing.
    assert read_ids(env, capsys) == []
    assert (env["state_dir"] / f"cursor-{IDENTITY}.json").is_file()


def test_an_old_broadcast_in_my_own_channel_is_not_read_first(env, capsys):
    seed(env, msg_id="01OLDBC000000000000000000B", to=None, comment_id=1, created_at=OLD)
    assert read_ids(env, capsys) == []


def test_a_broadcast_after_the_first_read_is_read(env, capsys):
    """#224 ③: only the first read is bounded; later broadcasts arrive as usual."""
    seed(env, msg_id="01TOME0000000000000000000A", to=IDENTITY, comment_id=1, created_at=OLD)
    assert read_ids(env, capsys) == ["01TOME0000000000000000000A"]

    env["clock"]["now"] = "2026-10-10T12:00:05Z"
    seed(env, msg_id="01NEWBC000000000000000000A", to=None, comment_id=2, created_at="2026-10-10T12:00:05Z")
    assert read_ids(env, capsys) == ["01NEWBC000000000000000000A"]


def test_a_broadcast_in_the_same_second_as_the_first_read_is_read(env, capsys):
    """The floor is inclusive: a broadcast at the first read's own second is not lost."""
    seed(env, msg_id="01SAMEBC00000000000000000A", to=None, comment_id=1, created_at=NOW)
    assert read_ids(env, capsys) == ["01SAMEBC00000000000000000A"]


def test_since_still_reads_an_old_broadcast(env, capsys):
    """#224 ④: old broadcasts stay reachable through `--since`, and the cursor stays put."""
    seed(env, msg_id="01OLDBC000000000000000000C", to=None, comment_id=1, created_at=OLD)

    ids = read_ids(env, capsys, ["read", "--since", "2026-10-10T10:00:00Z", "--json"])
    assert ids == ["01OLDBC000000000000000000C"]
    assert not (env["state_dir"] / f"cursor-{IDENTITY}.json").exists()


def test_a_mention_of_me_in_an_old_broadcast_is_read(env, capsys):
    """A no-`to` message that mentions me is a call to me, not an anonymous broadcast."""
    seed(
        env,
        msg_id="01OLDMN000000000000000000A",
        to=None,
        mentions=[IDENTITY],
        comment_id=1,
        created_at=OLD,
    )
    assert read_ids(env, capsys) == ["01OLDMN000000000000000000A"]


def test_channel_read_keeps_starting_from_the_beginning(env, capsys):
    """`--channel` is out of scope for #224: its first read still shows everything."""
    seed(env, msg_id="01OLDBC000000000000000000D", to=None, comment_id=1, created_at=OLD)
    seed(env, msg_id="01OTHER0000000000000000000A", to="someone-pm-m1", comment_id=2, created_at=OLD)
    assert read_ids(env, capsys, ["read", "--channel", "general", "--json"]) == [
        "01OLDBC000000000000000000D",
        "01OTHER0000000000000000000A",
    ]


def test_mentioned_read_keeps_starting_from_the_beginning(env, capsys):
    """`--mentioned` is documented as reading from the start; #224 leaves it alone."""
    seed(env, msg_id="01TOME0000000000000000000B", to=IDENTITY, comment_id=1, created_at=OLD)
    seed(env, msg_id="01OLDBC000000000000000000E", to=None, comment_id=2, created_at=OLD)
    assert read_ids(env, capsys, ["read", "--mentioned", "--json"]) == ["01TOME0000000000000000000B"]


def test_a_channel_empty_on_the_first_read_sees_its_later_broadcast(env, capsys):
    """The marker is "a to-me read has happened", not one channel's empty entry.

    #224 must not turn a quiet channel into a permanent first read: the
    broadcast here is old by the backend's clock, but it arrives after the
    first read, so it is new mail.
    """
    seed(env, msg_id="01TOME0000000000000000000C", to=IDENTITY, channel="general", comment_id=1, created_at=OLD)
    assert read_ids(env, capsys) == ["01TOME0000000000000000000C"]

    # "other" had no message at all on that first read, so it recorded nothing;
    # the broadcast is still older than NOW and must be read now.
    seed(env, channel="other", entity_number=7, msg_id="01QUIET00000000000000000A", to=None, comment_id=1, created_at=OLD)
    assert read_ids(env, capsys) == ["01QUIET00000000000000000A"]
