"""Unit tests for Angareion a2a CLI (tasks 2.1-2.4)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from angareion.backend import FakeBackend, RateLimitError
from angareion.cli import build_parser, main
from angareion.config import Config, save_config
from angareion.messages import Message


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Setup isolated test environment for A2A CLI."""
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    token_file = tmp_path / "token"
    token_file.write_text("ghp_FAKE_CLI_TOKEN_ABC123\n", encoding="utf-8")
    token_file.chmod(0o600)

    cfg = Config(
        channel_repo="owner-org/channel-repo",
        identity={"repo": "owner-org/sender", "role": "PM"},
        token_file=token_file,
        state_dir=state_dir,
        config_file=config_file,
    )
    save_config(cfg, config_file)

    monkeypatch.setenv("A2A_CONFIG", str(config_file))
    monkeypatch.setenv("A2A_STATE_DIR", str(state_dir))
    monkeypatch.setenv("A2A_TOKEN_FILE", str(token_file))

    backend = FakeBackend()
    backend.create_channel("general")
    return {"backend": backend, "config": cfg, "tmp_path": tmp_path}


def test_cli_help(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "a2a" in captured.out
    assert "send" in captured.out
    assert "inbox" in captured.out
    assert "ack" in captured.out


def test_cli_no_subcommand(capsys):
    ret = main([])
    assert ret == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err


def test_send_success_and_stdout_separation(cli_env, capsys):
    backend = cli_env["backend"]
    ret = main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=worker",
            "--channel", "general",
            "--urgency", "7",
            "--body", "Work request message",
        ],
        backend=backend,
    )
    assert ret == 0
    captured = capsys.readouterr()
    # stdout MUST be exactly `<id> <channel>\n`
    lines = captured.out.strip().split()
    assert len(lines) == 2
    msg_id, channel = lines[0], lines[1]
    assert len(msg_id) == 26
    assert channel == "general"

    # Backend contains posted message
    msgs = backend.list_messages().messages
    assert len(msgs) == 1
    assert msgs[0].content == "Work request message"
    assert msgs[0].urgency == 7


def test_send_channel_not_found_without_new_flag(cli_env, capsys):
    backend = cli_env["backend"]
    ret = main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=worker",
            "--channel", "nonexistent-topic",
            "--body", "test",
        ],
        backend=backend,
    )
    assert ret == 1  # Input error: channel does not exist
    captured = capsys.readouterr()
    assert "does not exist. Use --new to create it" in captured.err
    assert "general" in captured.err  # lists known channels


def test_send_channel_creation_with_new_flag(cli_env, capsys):
    backend = cli_env["backend"]
    ret = main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=worker",
            "--channel", "brand-new-channel",
            "--new",
            "--body", "First message in brand new channel",
        ],
        backend=backend,
    )
    assert ret == 0
    captured = capsys.readouterr()
    msg_id, channel = captured.out.strip().split()
    assert channel == "brand-new-channel"
    assert backend.find_channel("brand-new-channel") is not None


def test_send_to_group_address_rejected_in_v0(cli_env, capsys):
    backend = cli_env["backend"]
    ret = main(
        [
            "send",
            "--to", "group=all-workers",
            "--channel", "general",
            "--body", "group message",
        ],
        backend=backend,
    )
    assert ret == 1
    captured = capsys.readouterr()
    assert "Group addressing is not supported in v0" in captured.err


def test_send_with_attachments(cli_env, capsys):
    backend = cli_env["backend"]
    tmp_path = cli_env["tmp_path"]
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("file content 123", encoding="utf-8")

    ret = main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=worker",
            "--channel", "general",
            "--attach", f"data={sample_file}",
            "--ref", "spec=https://example.com/spec",
            "--body", "attached text",
        ],
        backend=backend,
    )
    assert ret == 0
    msgs = backend.list_messages().messages
    assert len(msgs) == 1
    atts = msgs[0].attachments
    assert len(atts) == 2
    assert atts[0] == {"name": "data", "inline": "file content 123"}
    assert atts[1] == {"name": "spec", "ref": "https://example.com/spec"}


def test_inbox_lists_unacked_and_json(cli_env, capsys):
    backend = cli_env["backend"]
    # Send message from PM to worker
    main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=team-a",
            "--channel", "general",
            "--body", "Task for worker",
        ],
        backend=backend,
    )
    capsys.readouterr()  # clear send stdout

    # Query inbox as worker team-a
    ret = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 1
    assert data[0]["content"] == "Task for worker"
    assert data[0]["acked"] is False

    # Second inbox call (304 condition)
    ret2 = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret2 == 0
    captured2 = capsys.readouterr()
    data2 = json.loads(captured2.out)
    assert len(data2) == 1


def test_ack_workflow(cli_env, capsys):
    backend = cli_env["backend"]
    main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=team-a",
            "--channel", "general",
            "--body", "Task 1",
        ],
        backend=backend,
    )
    captured = capsys.readouterr()
    msg_id = captured.out.strip().split()[0]

    # Ack the message as receiver
    ret = main(
        ["ack", msg_id, "--note", "Task done"],
        backend=backend,
    )
    assert ret == 0

    # Repeating ack is notice, exit 0, no duplicate
    ret_repeat = main(
        ["ack", msg_id],
        backend=backend,
    )
    assert ret_repeat == 0

    # Acking unknown ID returns 1
    ret_unknown = main(
        ["ack", "01UNKNOWN000000000000000000"],
        backend=backend,
    )
    assert ret_unknown == 1

    # Now inbox should be empty
    ret_inbox = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret_inbox == 0
    captured_inbox = capsys.readouterr()
    data = json.loads(captured_inbox.out)
    assert len(data) == 0

    # With --all it still lists it
    ret_all = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--all", "--json"],
        backend=backend,
    )
    assert ret_all == 0
    captured_all = capsys.readouterr()
    data_all = json.loads(captured_all.out)
    assert len(data_all) == 1
    assert data_all[0]["acked"] is True


def test_cli_error_does_not_leak_secrets(cli_env, capsys):
    backend = cli_env["backend"]
    secret_token = "ghp_FAKE_CLI_TOKEN_ABC123"

    # Injected backend error
    backend.next_error = RateLimitError(f"Secondary rate limit for {secret_token}", retry_after=60)

    ret = main(
        ["send", "--to", "repo=owner-org/worker,role=worker", "--channel", "general", "--body", "test"],
        backend=backend,
    )
    assert ret == 2  # Backend error code 2
    captured = capsys.readouterr()
    assert secret_token not in captured.err
    assert secret_token not in captured.out
    assert "***" in captured.err
