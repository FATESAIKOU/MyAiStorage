"""Unit tests for the angareion CLI: five commands, exit codes, stdout/stderr (D1/v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import io
import json
import sys

import pytest

from _angareion_fakes import (
    FakeGit,
    FakeRegistryClient,
    backends_json,
    channels_json,
    entity,
)
from angareion.backend import BackendError, ChannelNotFoundError, Cursor, FakeBackend, Target
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import build_parser, main
from angareion.config import Config, save_config
from angareion.github import HttpResponse
from angareion.messages import Message
from angareion.registry import Action, parse_comment

TOKEN = "ghp_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "MyLinuxPool-impl-2"
PM = "LLMGateway-pm"
GENERAL = entity(channel="general", issue=10)
OTHER = entity(channel="other", backend="github-main", repo="owner/repo", issue=7)


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ApplyingRegistry(FakeRegistryClient):
    """A registry whose comments land in the config copy, like the workflow would."""

    def __init__(self, git: FakeGit, **kwargs):
        super().__init__(**kwargs)
        self.git = git

    def post_comment(self, body: str) -> None:
        super().post_comment(body)
        parsed = parse_comment(body)
        if isinstance(parsed, Action):
            current = json.loads(self.git.files.get(f"origin/main:{CHANNELS_PATH}", '{"channels": {}}'))
            current.setdefault("channels", {})[parsed.channel] = {
                "backend": parsed.backend,
                "issue": parsed.issue,
            }
            self.git.set_file(CHANNELS_PATH, json.dumps(current, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated config/state/token, a fake config copy and a FakeBackend with two channels."""
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
    clock = FakeClock()
    warnings: list[str] = []
    store = ChannelStore(
        clone,
        git=git,
        interval=0,
        warn=warnings.append,
        now=lambda: 1000.0,
        clock=lambda: clock.now,
        sleeper=clock.sleep,
        fetch_head_mtime=lambda: git.fetch_head_mtime,
    )
    backend = FakeBackend(store)
    backend.add_entity("general", number=10)
    backend.add_entity("other", number=7)
    registry = ApplyingRegistry(git)
    return {
        "config": config,
        "git": git,
        "store": store,
        "backend": backend,
        "registry": registry,
        "clock": clock,
        "warnings": warnings,
        "tmp_path": tmp_path,
        "state_dir": state_dir,
        "token_file": token_file,
        "clone": clone,
    }


def run(env, argv, **kwargs):
    return main(
        argv,
        backend=env["backend"],
        store=env["store"],
        registry=env["registry"],
        sleeper=env["clock"].sleep,
        clock=lambda: env["clock"].now,
        **kwargs,
    )


def _seed_message(
    env,
    *,
    channel: str = "general",
    entity_number: int = 10,
    msg_id: str = "01AAAA00000000000000000000",
    at: str = "2026-10-10T12:00:00+00:00",
    content: str = "Task for worker",
    to: str | None = IDENTITY,
    comment_id: int | None = None,
    created_at: str | None = None,
) -> Message:
    target = entity(channel=channel, issue=entity_number)
    message = Message(from_=PM, to=to, channel=channel, content=content, id=msg_id, at=at)
    env["backend"].deliver(target, message, comment_id=comment_id, created_at=created_at)
    return message


def test_help_exits_zero(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    for command in ("channel", "send", "wait", "read"):
        assert command in captured.out

    for action in ("create", "delete"):
        with pytest.raises(SystemExit):
            parser.parse_args(["channel", action, "--help"])


def test_no_subcommand_is_usage_error(capsys):
    assert main([]) == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert captured.out == ""


def test_channel_without_action_is_usage_error(capsys):
    assert main(["channel"]) == 1
    err = capsys.readouterr().err
    assert "channel" in err and "delete" in err


def test_bad_option_value_is_usage_error(cli_env, capsys):
    ret = run(cli_env, ["send", "--channel", "general", "--to", PM, "--urgency", "not-a-number", "--body", "x"])
    assert ret == 1
    assert capsys.readouterr().err != ""


def test_send_prints_only_the_message_id(cli_env, capsys):
    ret = run(cli_env, ["send", "--channel", "general", "--to", PM, "--urgency", "7", "--body", "Work request"])
    assert ret == 0
    captured = capsys.readouterr()
    message_id = captured.out.strip()
    assert len(message_id) == 26
    assert captured.err == ""

    messages = cli_env["backend"].read(GENERAL, Cursor()).messages
    assert len(messages) == 1
    assert messages[0].id == message_id
    assert messages[0].content == "Work request"
    assert messages[0].urgency == 7
    assert messages[0].from_ == IDENTITY
    assert messages[0].to == PM


def test_send_without_to_is_a_broadcast(cli_env, capsys):
    assert run(cli_env, ["send", "--channel", "general", "--body", "all hands"]) == 0
    capsys.readouterr()
    message = cli_env["backend"].read(GENERAL, Cursor()).messages[0]
    assert message.to is None


def test_send_without_body_is_usage_error(cli_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ret = run(cli_env, ["send", "--channel", "general", "--to", PM])
    assert ret == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--body" in captured.err
    assert cli_env["backend"].read(GENERAL, Cursor()).messages == []


def test_send_body_dash_reads_stdin(cli_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("body from stdin"))
    assert run(cli_env, ["send", "--channel", "general", "--to", PM, "--body", "-"]) == 0
    capsys.readouterr()
    assert cli_env["backend"].read(GENERAL, Cursor()).messages[0].content == "body from stdin"


def test_send_urgency_out_of_range_sends_nothing(cli_env, capsys):
    ret = run(cli_env, ["send", "--channel", "general", "--to", PM, "--urgency", "12", "--body", "no"])
    assert ret == 1
    assert "urgency" in capsys.readouterr().err
    assert cli_env["backend"].read(GENERAL, Cursor()).messages == []


def test_send_group_address_rejected(cli_env, capsys):
    ret = run(cli_env, ["send", "--channel", "general", "--to", "group=全員", "--body", "hi"])
    assert ret == 1
    assert "agent name" in capsys.readouterr().err


def test_send_non_ascii_channel_rejected(cli_env, capsys):
    ret = run(cli_env, ["send", "--channel", "小明", "--body", "hi"])
    assert ret == 1
    assert "channel" in capsys.readouterr().err


def test_send_unknown_channel_exits_1(cli_env, capsys):
    ret = run(cli_env, ["send", "--channel", "nowhere", "--to", PM, "--body", "hi"])
    assert ret == 1
    assert "does not exist" in capsys.readouterr().err


def test_send_with_attachments(cli_env, capsys):
    sample_file = cli_env["tmp_path"] / "sample.txt"
    sample_file.write_text("file content 123", encoding="utf-8")

    ret = run(
        cli_env,
        ["send", "--channel", "general", "--to", PM, "--attach", f"data={sample_file}",
         "--ref", "spec=https://example.com/spec", "--body", "attached text"],
    )
    assert ret == 0
    messages = cli_env["backend"].read(GENERAL, Cursor()).messages
    assert messages[0].attachments == [
        {"name": "data", "inline": "file content 123"},
        {"name": "spec", "ref": "https://example.com/spec"},
    ]


def test_read_prints_messages_and_advances_the_cursor(cli_env, capsys):
    _seed_message(cli_env, msg_id="01AAAA00000000000000000000", comment_id=1, created_at="2026-10-10T12:00:05Z")

    assert run(cli_env, ["read", "--channel", "general"]) == 0
    captured = capsys.readouterr()
    assert "01AAAA00000000000000000000" in captured.out
    assert "Task for worker" in captured.out
    assert "<LLMGateway-pm>" in captured.out  # the sender is the agent name
    assert "channel: general" in captured.out
    assert "(received 2026-10-10T12:00:05Z)" in captured.out
    assert captured.err == ""

    cursor_file = cli_env["state_dir"] / "cursor-MyLinuxPool-impl-2.json"
    stored = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert stored == {"channel:general": {"position": "1", "at": "2026-10-10T12:00:05Z", "seen": ["1"]}}

    assert run(cli_env, ["read", "--channel", "general"]) == 0
    assert capsys.readouterr().out == ""


def test_read_json_output(cli_env, capsys):
    _seed_message(cli_env, msg_id="01AAAA00000000000000000000", comment_id=1, created_at="2026-10-10T12:00:05Z")

    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "01AAAA00000000000000000000"
    assert data[0]["content"] == "Task for worker"
    assert data[0]["from"] == PM
    assert data[0]["to"] == IDENTITY
    assert data[0]["received_at"] == "2026-10-10T12:00:05Z"


def test_read_to_me_includes_broadcasts_and_excludes_others(cli_env, capsys):
    _seed_message(cli_env, channel="general", entity_number=10, msg_id="01AAAA00000000000000000000", to=IDENTITY)
    _seed_message(cli_env, channel="other", entity_number=7, msg_id="01BBBB00000000000000000000", to=None)
    _seed_message(cli_env, channel="general", entity_number=10, msg_id="01CCCC00000000000000000000", to="someone-pm")

    assert run(cli_env, ["read", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000", "01BBBB00000000000000000000"]


def test_wait_without_channel_sees_a_broadcast(cli_env, capsys):
    """A broadcast counts as "to me": wait without --channel must wake up on it."""
    _seed_message(cli_env, channel="other", entity_number=7, msg_id="01BBBB00000000000000000000", to=None)
    assert run(cli_env, ["wait", "--timeout", "0"]) == 0
    assert capsys.readouterr().out == ""


def test_read_json_marks_a_broadcast_with_a_null_to(cli_env, capsys):
    _seed_message(cli_env, msg_id="01AAAA00000000000000000000", to=None)
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["to"] is None


def test_send_without_to_over_the_github_backend_is_a_broadcast(cli_env, capsys):
    transport = MiniGitHub()
    assert main(["send", "--channel", "general", "--body", "all hands"], store=cli_env["store"], transport=transport) == 0
    capsys.readouterr()
    from angareion.messages import parse_message

    posted = transport.comments[-1]["body"]
    parsed = parse_message(posted, strict=True)
    assert parsed is not None
    assert parsed.to is None


def test_read_with_since_does_not_touch_the_cursor(cli_env, capsys):
    _seed_message(cli_env, comment_id=1, created_at="2026-10-10T12:00:00Z")
    _seed_message(cli_env, msg_id="01BBBB00000000000000000000", comment_id=2, created_at="2026-10-10T12:00:01Z")

    assert run(cli_env, ["read", "--channel", "general", "--since", "2026-10-10T12:00:01Z", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01BBBB00000000000000000000"]
    assert not (cli_env["state_dir"] / "cursor-MyLinuxPool-impl-2.json").exists()


def test_read_since_must_be_rfc3339(cli_env, capsys):
    ret = run(cli_env, ["read", "--channel", "general", "--since", "yesterday"])
    assert ret == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RFC 3339" in captured.err


def test_read_unknown_channel_exits_1(cli_env, capsys):
    assert run(cli_env, ["read", "--channel", "nowhere"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_read_keeps_the_cursor_when_stdout_breaks(cli_env, capsys, monkeypatch):
    """`angareion read | head`: stdout closing early must not consume the messages (L2)."""
    _seed_message(cli_env)
    cursor_file = cli_env["state_dir"] / "cursor-MyLinuxPool-impl-2.json"
    real_stdout = sys.stdout

    class BrokenStdout:
        def write(self, text):
            raise BrokenPipeError()

        def flush(self):
            raise BrokenPipeError()

        def isatty(self):
            return False

        def fileno(self):
            raise OSError("no fileno under pytest capture")

    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    assert run(cli_env, ["read", "--channel", "general"]) == 1
    assert "cursor was not advanced" in capsys.readouterr().err
    assert not cursor_file.exists()

    monkeypatch.setattr(sys, "stdout", real_stdout)
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01AAAA00000000000000000000"]


def test_wait_timeout_zero_without_new_messages_exits_3(cli_env, capsys):
    assert run(cli_env, ["wait", "--channel", "general", "--timeout", "0"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Timed out" in captured.err


def test_wait_timeout_zero_with_new_message_exits_0(cli_env, capsys):
    _seed_message(cli_env)
    assert run(cli_env, ["wait", "--channel", "general", "--timeout", "0"]) == 0
    assert capsys.readouterr().out == ""


def test_wait_blocks_until_a_message_arrives(cli_env, capsys):
    clock = cli_env["clock"]

    def sleeper(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.sleeps) == 1:
            _seed_message(cli_env)

    env = dict(cli_env)
    ret = main(
        ["wait", "--channel", "general", "--timeout", "600"],
        backend=env["backend"],
        store=env["store"],
        registry=env["registry"],
        sleeper=sleeper,
        clock=lambda: clock.now,
    )
    assert ret == 0
    assert clock.sleeps == [15.0]
    assert not (cli_env["state_dir"] / "cursor-MyLinuxPool-impl-2.json").exists()


def test_wait_negative_timeout_is_usage_error(cli_env, capsys):
    assert run(cli_env, ["wait", "--channel", "general", "--timeout", "-1"]) == 1
    assert "--timeout" in capsys.readouterr().err


def test_wait_polling_does_not_fetch(cli_env, capsys):
    """The pull happens once at the start; the polling loop must not fetch."""
    clock = cli_env["clock"]

    def sleeper(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.sleeps) == 2:
            _seed_message(cli_env)

    fetches_before = cli_env["git"].fetch_calls
    ret = main(
        ["wait", "--channel", "general", "--timeout", "600"],
        backend=cli_env["backend"],
        store=cli_env["store"],
        registry=cli_env["registry"],
        sleeper=sleeper,
        clock=lambda: clock.now,
    )
    assert ret == 0
    assert cli_env["git"].fetch_calls == fetches_before


def test_missing_config_exits_2(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("ANGAREION_CONFIG", str(cli_env["tmp_path"] / "nope.json"))
    ret = run(cli_env, ["send", "--channel", "general", "--body", "x"])
    assert ret == 2
    assert "not found" in capsys.readouterr().err


def test_missing_config_copy_exits_2(cli_env, capsys):
    import shutil

    shutil.rmtree(cli_env["clone"])
    ret = run(cli_env, ["send", "--channel", "general", "--body", "x"])
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(cli_env["clone"]) in captured.err


def test_broken_channels_json_exits_2(cli_env, capsys):
    cli_env["git"].set_file(CHANNELS_PATH, "{not json")
    ret = run(cli_env, ["send", "--channel", "general", "--body", "x"])
    assert ret == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_token_permissions_too_open_exits_2_and_never_leaks(cli_env, capsys):
    cli_env["token_file"].chmod(0o644)
    ret = main(["send", "--channel", "general", "--body", "x"], store=cli_env["store"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "owner-only" in captured.err
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_backend_error_masks_the_token(cli_env, capsys):
    cli_env["backend"].next_error = BackendError(f"Secondary rate limit for {TOKEN}")
    ret = run(cli_env, ["wait", "--channel", "general", "--timeout", "0"])
    assert ret == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.err
    assert "***" in captured.err
    assert captured.out == ""


def test_unexpected_value_error_from_a_backend_is_exit_2(cli_env, capsys):
    cli_env["backend"].next_error = ValueError("boom from a backend")
    assert run(cli_env, ["read", "--channel", "general"]) == 2
    assert "boom from a backend" in capsys.readouterr().err


# -- channel create: the three steps (design section 6) ----------------------


def test_channel_create_opens_registers_and_lands(cli_env, capsys):
    ret = run(cli_env, ["channel", "create", "fresh"])
    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out == "fresh\n"  # stdout only carries the channel name

    # Step 1: the backend entity exists with the channel name as its title.
    assert cli_env["backend"].entity_title(11) == "fresh"
    assert cli_env["backend"].entity_state(11) == "open"

    # Step 2: a register comment on the registry issue.
    assert len(cli_env["registry"].posted) == 1
    body = cli_env["registry"].posted[0]
    assert json.loads(body) == {
        "schema": 1,
        "action": "register",
        "channel": "fresh",
        "backend": "github-main",
        "issue": 11,
    }

    # Step 3: the mapping landed in the local copy (the fake registry applied it).
    assert "fresh" in cli_env["store"].channels()


def test_channel_create_waits_for_the_mapping_to_land(cli_env, capsys):
    """The registry does not apply immediately: create polls until it shows up."""
    registry = cli_env["registry"]
    applied: list[str] = []

    def record_only(body: str) -> None:
        registry.posted.append(body)  # the comment is posted, but the rebuild has not run
        applied.append(body)

    registry.post_comment = record_only  # type: ignore[assignment]

    # Nothing lands during the poll; the create gives up after 2 minutes and
    # still succeeds (with a notice).
    ret = run(cli_env, ["channel", "create", "fresh"])
    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out == "fresh\n"
    assert "has not shown up" in captured.err
    assert cli_env["clock"].now == 120.0  # bounded by the 2-minute landing wait

    # The rebuild lands later; the next command sees the channel.
    ApplyingRegistry.post_comment(registry, applied[0])
    assert run(cli_env, ["send", "--channel", "fresh", "--body", "hi"]) == 0
    capsys.readouterr()


def test_channel_create_existing_name_exits_1(cli_env, capsys):
    ret = run(cli_env, ["channel", "create", "general"])
    assert ret == 1
    assert "already exists" in capsys.readouterr().err
    assert cli_env["registry"].posted == []


def test_channel_create_open_failure_exits_2_and_registers_nothing(cli_env, capsys):
    cli_env["backend"].next_create_error = BackendError("network down")
    ret = run(cli_env, ["channel", "create", "fresh"])
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "network down" not in captured.err  # the interface does not repeat backend detail
    assert cli_env["registry"].posted == []


def test_channel_create_verify_failure_exits_2_and_registers_nothing(cli_env, capsys):
    """Review H2: the entity must exist with exactly the channel name as title."""
    original = cli_env["backend"].create_channel

    def create_with_wrong_title(name, backend):
        number = original(name, backend)
        cli_env["backend"].set_entity_title(number, "something-else")
        return number

    cli_env["backend"].create_channel = create_with_wrong_title  # type: ignore[assignment]
    ret = run(cli_env, ["channel", "create", "fresh"])
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not be verified" in captured.err or "verified" in captured.err
    assert cli_env["registry"].posted == []


def test_channel_create_registration_retries_then_succeeds(cli_env, capsys):
    cli_env["registry"].fail_posts_times = 1
    assert run(cli_env, ["channel", "create", "fresh"]) == 0
    capsys.readouterr()
    assert cli_env["clock"].sleeps == [1.0]  # one short retry gap


def test_channel_create_registration_failure_exits_2(cli_env, capsys):
    cli_env["registry"].fail_posts_times = 3
    ret = run(cli_env, ["channel", "create", "fresh"])
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "was not created" in captured.err
    # The entity from step 1 may remain (an orphan); the interface does not mention it.
    assert cli_env["registry"].posted == []
    assert cli_env["clock"].sleeps == [1.0, 1.0]  # 3 attempts, 2 gaps


def test_channel_create_needs_backend_when_several_are_configured(cli_env, capsys):
    cli_env["git"].set_file(
        BACKENDS_PATH,
        backends_json({"github-main": ("github", "owner/repo"), "github-2": ("github", "owner/repo2")}),
    )
    ret = run(cli_env, ["channel", "create", "fresh"])
    assert ret == 1
    assert "--backend" in capsys.readouterr().err
    assert cli_env["registry"].posted == []

    assert run(cli_env, ["channel", "create", "fresh", "--backend", "github-2"]) == 0
    capsys.readouterr()
    assert json.loads(cli_env["registry"].posted[0])["backend"] == "github-2"


def test_channel_create_unknown_backend_exits_1(cli_env, capsys):
    ret = run(cli_env, ["channel", "create", "fresh", "--backend", "nosuch"])
    assert ret == 1
    assert "not configured" in capsys.readouterr().err
    assert cli_env["registry"].posted == []


def test_channel_create_unknown_backend_type_exits_2(cli_env, capsys):
    cli_env["git"].set_file(BACKENDS_PATH, backends_json({"other": ("matrix", "")}))
    ret = run(cli_env, ["channel", "create", "fresh", "--backend", "other"])
    assert ret == 2
    assert "unsupported backend type" in capsys.readouterr().err


# -- channel delete (design section 7) --------------------------------------


def test_channel_delete_closes_and_unregisters(cli_env, capsys):
    ret = run(cli_env, ["channel", "delete", "general"])
    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out == "general\n"
    assert cli_env["backend"].entity_state(10) == "closed"
    assert json.loads(cli_env["registry"].posted[0]) == {
        "schema": 1,
        "action": "unregister",
        "channel": "general",
        "backend": "github-main",
        "issue": 10,
    }


def test_channel_delete_unknown_channel_exits_1(cli_env, capsys):
    ret = run(cli_env, ["channel", "delete", "nowhere"])
    assert ret == 1
    assert "does not exist" in capsys.readouterr().err
    assert cli_env["registry"].posted == []


def test_channel_delete_close_failure_exits_2_without_unregistering(cli_env, capsys):
    cli_env["backend"].next_close_error = BackendError("closing failed")
    ret = run(cli_env, ["channel", "delete", "general"])
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "closing failed" not in captured.err  # no backend detail in the interface
    assert cli_env["registry"].posted == []
    assert cli_env["backend"].entity_state(10) == "open"


def test_channel_delete_404_is_already_closed(cli_env, capsys):
    """The entity was deleted in the backend: closing it is a no-op, and the comment still goes out."""
    cli_env["git"].set_file(CHANNELS_PATH, channels_json({"general": ("github-main", 999)}))
    ret = run(cli_env, ["channel", "delete", "general"])
    assert ret == 0
    assert capsys.readouterr().out == "general\n"
    assert json.loads(cli_env["registry"].posted[0])["action"] == "unregister"


def test_channel_delete_unregister_failure_exits_2(cli_env, capsys):
    cli_env["registry"].fail_posts_times = 1  # delete does not retry
    ret = run(cli_env, ["channel", "delete", "general"])
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not be posted" in captured.err
    assert cli_env["backend"].entity_state(10) == "closed"  # closed, mapping still there
    assert cli_env["clock"].sleeps == []


def test_channel_delete_is_safe_to_rerun_after_a_failed_unregister(cli_env, capsys):
    """Re-running closes an already-closed entity again (idempotent) and retries the comment."""
    cli_env["registry"].fail_posts_times = 1
    assert run(cli_env, ["channel", "delete", "general"]) == 2
    assert cli_env["backend"].entity_state(10) == "closed"

    assert run(cli_env, ["channel", "delete", "general"]) == 0
    assert capsys.readouterr().out == "general\n"
    assert json.loads(cli_env["registry"].posted[-1]) == {
        "schema": 1,
        "action": "unregister",
        "channel": "general",
        "backend": "github-main",
        "issue": 10,
    }


def test_channel_delete_over_the_github_backend_treats_404_as_closed(cli_env, capsys):
    transport = MiniGitHub()  # this backend never saw entity 10: PATCH will 404
    ret = main(
        ["channel", "delete", "general"],
        store=cli_env["store"],
        registry=cli_env["registry"],
        transport=transport,
    )
    assert ret == 0
    assert capsys.readouterr().out == "general\n"
    assert json.loads(cli_env["registry"].posted[-1])["action"] == "unregister"


# -- the pull policy at the CLI level ----------------------------------------


def test_send_pulls_once_when_the_local_copy_is_behind(cli_env, capsys):
    """A teammate just created X: a lookup miss fetches once and then finds it."""
    cli_env["git"].fetch_head_mtime = 0.0  # the copy is stale, so the miss may fetch
    cli_env["git"].on_fetch = lambda: cli_env["git"].set_file(
        CHANNELS_PATH, channels_json({"general": ("github-main", 10), "fresh": ("github-main", 11)})
    )
    cli_env["backend"].add_entity("fresh", number=11)
    ret = run(cli_env, ["send", "--channel", "fresh", "--to", PM, "--body", "hi"])
    assert ret == 0
    capsys.readouterr()
    assert cli_env["git"].fetch_calls >= 1


def test_fetch_failure_warns_and_the_command_continues(cli_env, capsys):
    from angareion.channel_config import GitError

    cli_env["store"].interval = 900  # periodic pull enabled
    cli_env["git"].raise_on_fetch = GitError("git fetch timed out after 20s")
    cli_env["git"].fetch_head_mtime = None
    assert run(cli_env, ["send", "--channel", "general", "--body", "hi"]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.strip()) == 26
    assert any("could not refresh" in warning for warning in cli_env["warnings"])


# -- the GitHub backend end to end (fake transport, no network) --------------


class MiniGitHub:
    """A tiny stateful GitHub Issues + comments API for one repo; no network."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self._next_issue = 1
        self._next_comment = 1

    def request(self, method, url, headers=None, body=None, timeout=None):
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if method == "POST" and path.endswith("/issues"):
            payload = json.loads(body.decode("utf-8"))
            number = self._next_issue
            self._next_issue += 1
            issue = {"number": number, "title": payload["title"], "body": payload.get("body", ""), "state": "open"}
            self.issues[number] = issue
            return HttpResponse(201, {}, json.dumps(issue).encode("utf-8"))

        if method == "GET" and "/issues/" in path and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            comments = [c for c in self.comments if c["_issue_number"] == number]
            since = params.get("since")
            if since:
                comments = [c for c in comments if c["updated_at"] >= since[0]]
            return HttpResponse(200, {}, json.dumps(comments).encode("utf-8"))

        if method == "POST" and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            payload = json.loads(body.decode("utf-8"))
            created_at = f"2026-10-10T12:00:{self._next_comment:02d}Z"
            comment = {
                "id": self._next_comment,
                "body": payload["body"],
                "created_at": created_at,
                "updated_at": created_at,
                "html_url": f"https://github.com/owner/repo/issues/{number}#issuecomment-{self._next_comment}",
                "_issue_number": number,
            }
            self._next_comment += 1
            self.comments.append(comment)
            return HttpResponse(201, {}, json.dumps(comment).encode("utf-8"))

        if method == "GET" and path.endswith(tuple(f"/issues/{n}" for n in self.issues)):
            number = int(path.rsplit("/", 1)[1])
            return HttpResponse(200, {}, json.dumps(self.issues[number]).encode("utf-8"))

        if method == "PATCH" and path.endswith(tuple(f"/issues/{n}" for n in self.issues)):
            number = int(path.rsplit("/", 1)[1])
            self.issues[number]["state"] = "closed"
            return HttpResponse(200, {}, json.dumps(self.issues[number]).encode("utf-8"))

        return HttpResponse(404, {}, b'{"message": "Not Found"}')


def test_five_commands_over_the_github_backend_without_network(cli_env, capsys):
    transport = MiniGitHub()

    # create: opens the entity in the repo, verifies it, posts the register comment.
    assert main(["channel", "create", "fresh"], store=cli_env["store"], registry=cli_env["registry"], transport=transport) == 0
    assert capsys.readouterr().out == "fresh\n"
    assert transport.issues[1]["title"] == "fresh"
    assert json.loads(cli_env["registry"].posted[-1])["action"] == "register"

    # The mapping lands (the fake registry applied it); send/read/wait work.
    assert main(["send", "--channel", "fresh", "--to", PM, "--body", "hello"], store=cli_env["store"], transport=transport) == 0
    message_id = capsys.readouterr().out.strip()
    assert len(message_id) == 26

    assert main(["wait", "--channel", "fresh", "--timeout", "0"], store=cli_env["store"], transport=transport) == 0
    capsys.readouterr()

    assert main(["read", "--channel", "fresh", "--json"], store=cli_env["store"], transport=transport) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == [message_id]

    # Nothing new: read is empty and wait times out.
    assert main(["read", "--channel", "fresh", "--json"], store=cli_env["store"], transport=transport) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["wait", "--channel", "fresh", "--timeout", "0"], store=cli_env["store"], transport=transport) == 3
    capsys.readouterr()

    # delete: closes the entity and unregisters.
    assert main(["channel", "delete", "fresh"], store=cli_env["store"], registry=cli_env["registry"], transport=transport) == 0
    assert capsys.readouterr().out == "fresh\n"
    assert transport.issues[1]["state"] == "closed"
    assert json.loads(cli_env["registry"].posted[-1])["action"] == "unregister"


def test_backend_http_error_exits_2_without_retry_and_without_the_token(cli_env, capsys):
    class ErrorTransport:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, headers=None, body=None, timeout=None):
            self.calls += 1
            return HttpResponse(403, {"retry-after": "60"}, b'{"message": "secondary rate limit"}')

    transport = ErrorTransport()
    ret = main(["wait", "--channel", "general", "--timeout", "0"], store=cli_env["store"], transport=transport)
    assert ret == 2
    captured = capsys.readouterr()
    assert "403" in captured.err
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out
    assert transport.calls == 1  # no automatic retry


def test_non_json_backend_response_exits_2(cli_env, capsys):
    class HtmlTransport:
        def request(self, method, url, headers=None, body=None, timeout=None):
            return HttpResponse(200, {}, b"<html>proxy login page</html>")

    ret = main(["read", "--channel", "general"], store=cli_env["store"], transport=HtmlTransport())
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "non-JSON" in captured.err


def test_transport_timeout_exits_2_without_waiting_out_the_timeout(cli_env, capsys):
    import socket

    class TimeoutTransport:
        def __init__(self, exc):
            self.exc = exc

        def request(self, method, url, headers=None, body=None, timeout=None):
            raise self.exc

    def forbidden_sleep(seconds):
        raise AssertionError("a backend timeout must surface, not sleep past the --timeout")

    for exc in (TimeoutError("timed out"), socket.timeout("timed out")):
        ret = main(
            ["wait", "--channel", "general", "--timeout", "600"],
            store=cli_env["store"],
            transport=TimeoutTransport(exc),
            sleeper=forbidden_sleep,
            clock=lambda: cli_env["clock"].now,
        )
        assert ret == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "timed out" in captured.err


def test_read_same_second_smaller_ulid_is_delivered(cli_env, capsys):
    """The spec scenario: sender ULIDs in the same second decide nothing; the backend does."""
    _seed_message(cli_env, msg_id="01ZZZZ00000000000000000000", comment_id=100, created_at="2026-10-10T03:00:00Z")

    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01ZZZZ00000000000000000000"]

    # A later message in the same second with a smaller sender ULID must arrive.
    _seed_message(cli_env, msg_id="01AAAA00000000000000000000", comment_id=101, created_at="2026-10-10T03:00:00Z")
    assert run(cli_env, ["wait", "--channel", "general", "--timeout", "0"]) == 0
    capsys.readouterr()
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01AAAA00000000000000000000"]


def test_read_delivers_a_message_from_a_slow_senders_clock(cli_env, capsys):
    _seed_message(
        cli_env,
        msg_id="01SLOW00000000000000000000",
        at="2026-10-10T01:00:00+00:00",  # two hours behind
        comment_id=100,
        created_at="2026-10-10T03:00:10Z",
    )
    assert run(cli_env, ["wait", "--channel", "general", "--timeout", "0"]) == 0
    capsys.readouterr()
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01SLOW00000000000000000000"]
    assert data[0]["received_at"] == "2026-10-10T03:00:10Z"


def test_read_delivers_a_replication_lagging_smaller_id(cli_env, capsys):
    _seed_message(cli_env, msg_id="01READ00000000000000000000", comment_id=100, created_at="2026-10-10T12:00:10Z")
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1

    _seed_message(cli_env, msg_id="01LAG000000000000000000000", comment_id=99, created_at="2026-10-10T12:00:05Z")
    assert run(cli_env, ["wait", "--channel", "general", "--timeout", "0"]) == 0
    capsys.readouterr()
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01LAG000000000000000000000"]

    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_old_format_cursor_is_reported_and_read_from_the_start(cli_env, capsys):
    """A pre-review {at, id} cursor cannot be translated: no cursor, plus a notice."""
    _seed_message(cli_env, comment_id=7, created_at="2026-10-10T12:00:00Z")
    cursor_file = cli_env["state_dir"] / "cursor-MyLinuxPool-impl-2.json"
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    cursor_file.write_text(
        json.dumps({"channel:general": {"at": "2026-10-10T12:00:00+00:00", "id": "01AAAA00000000000000000000"}}),
        encoding="utf-8",
    )

    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    captured = capsys.readouterr()
    assert [m["id"] for m in json.loads(captured.out)] == ["01AAAA00000000000000000000"]
    assert "old (at, id) format" in captured.err

    stored = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert stored["channel:general"]["position"] == "7"
    assert run(cli_env, ["read", "--channel", "general", "--json"]) == 0
    captured2 = capsys.readouterr()
    assert json.loads(captured2.out) == []
    assert captured2.err == ""
