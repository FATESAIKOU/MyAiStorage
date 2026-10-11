"""Unit tests for Angareion a2a CLI (tasks 2.1-2.4)."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import time
import pytest

from angareion.backend import BackendError, FakeBackend, RateLimitError
from angareion.cli import RateLimiter, build_parser, main
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
    captured_repeat = capsys.readouterr()
    assert "already acknowledged" in captured_repeat.err
    assert len(backend.list_messages().messages) == 2

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


def test_inbox_rebuilds_after_cache_file_deleted(cli_env, capsys):
    """H1: deleting the local cache file must rebuild the inbox, not return [].

    The state file keeps its etag, but with no cached messages the etag must
    not be sent (the backend would answer 304 and the inbox would read empty).
    """
    backend = cli_env["backend"]
    state_dir = cli_env["config"].state_dir
    main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=team-a",
            "--channel", "general",
            "--body", "Task for worker",
        ],
        backend=backend,
    )
    capsys.readouterr()

    ret = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret == 0
    assert len(json.loads(capsys.readouterr().out)) == 1

    # Delete the local cache file; state.json (with the etag) stays.
    caches = list(state_dir.glob("*_cache.json"))
    assert len(caches) == 1
    caches[0].unlink()

    ret2 = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret2 == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["content"] == "Task for worker"


def test_inbox_cache_file_is_per_identity(cli_env, capsys):
    """M1: each identity gets its own cache file named by identity_slug."""
    backend = cli_env["backend"]
    state_dir = cli_env["config"].state_dir
    for role, body in (("team-a", "Task A"), ("team-b", "Task B")):
        main(
            [
                "send",
                "--to", f"repo=owner-org/worker,role={role}",
                "--channel", "general",
                "--body", body,
            ],
            backend=backend,
        )
    capsys.readouterr()

    ret_a = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret_a == 0
    data_a = json.loads(capsys.readouterr().out)
    assert [m["content"] for m in data_a] == ["Task A"]

    ret_b = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-b", "--json"],
        backend=backend,
    )
    assert ret_b == 0
    data_b = json.loads(capsys.readouterr().out)
    assert [m["content"] for m in data_b] == ["Task B"]

    assert (state_dir / "owner-org_worker__team-a_cache.json").is_file()
    assert (state_dir / "owner-org_worker__team-b_cache.json").is_file()
    assert not (state_dir / "inbox_cache.json").exists()


class _FlakyBackend(FakeBackend):
    """FakeBackend whose next post_message calls fail with queued errors."""

    def __init__(self):
        super().__init__()
        self.post_errors: list[Exception] = []
        self.post_calls = 0

    def post_message(self, channel, message):
        self.post_calls += 1
        if self.post_errors:
            raise self.post_errors.pop(0)
        return super().post_message(channel, message)


def test_send_retries_backend_403_with_injected_sleep(cli_env, capsys):
    """M2: a 403 with retry_after=60 is waited out once and retried, not exit 2.

    The sleep is injected, so the test never actually waits 60 seconds.
    """
    backend = _FlakyBackend()
    backend.create_channel("general")
    backend.post_errors.append(RateLimitError("Secondary rate limit", retry_after=60))
    slept: list[float] = []

    ret = main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=worker",
            "--channel", "general",
            "--body", "retry me",
        ],
        backend=backend,
        sleeper=slept.append,
    )
    assert ret == 0
    assert slept == [60.0]
    captured = capsys.readouterr()
    assert "retrying in 60" in captured.err
    assert backend.post_calls == 2
    assert len(backend.list_messages().messages) == 1


def test_send_403_retry_still_failing_exits_2(cli_env, capsys):
    """M2: if the retry after retry-after also fails, exit 2 with nothing posted."""
    backend = _FlakyBackend()
    backend.create_channel("general")
    backend.post_errors.append(RateLimitError("Secondary rate limit", retry_after=60))
    backend.post_errors.append(BackendError("still limited"))
    slept: list[float] = []

    ret = main(
        [
            "send",
            "--to", "repo=owner-org/worker,role=worker",
            "--channel", "general",
            "--body", "retry me",
        ],
        backend=backend,
        sleeper=slept.append,
    )
    assert ret == 2
    assert slept == [60.0]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(backend.list_messages().messages) == 0


def test_rate_limit_file_uses_flock(cli_env, monkeypatch):
    """L2: rate_limit.json read-modify-write holds an exclusive flock."""
    calls: list[int] = []
    real_flock = fcntl.flock

    def spy(fd, op):
        calls.append(op)
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", spy)
    limiter = RateLimiter(cli_env["config"].state_dir)
    limiter.acquire()
    assert fcntl.LOCK_EX in calls
    assert fcntl.LOCK_UN in calls
    assert calls.index(fcntl.LOCK_EX) < calls.index(fcntl.LOCK_UN)


def test_rate_limit_sleep_happens_outside_lock(cli_env, monkeypatch, capsys):
    """L3: acquire must not sleep while holding LOCK_EX, or a concurrent send
    blocks on the lock for the whole wait instead of just the read-modify-write."""
    events: list[str] = []
    real_flock = fcntl.flock

    def spy_flock(fd, op):
        events.append("lock" if op == fcntl.LOCK_EX else "unlock")
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", spy_flock)
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])

    def fake_sleep(s):
        events.append("sleep")
        clock["now"] += s

    state_dir = cli_env["config"].state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "rate_limit.json").write_text(json.dumps([999.0] * 80))
    limiter = RateLimiter(state_dir, sleeper=fake_sleep)
    limiter.acquire(max_wait=60.0)

    assert "sleep" in events
    depth = 0
    for e in events:
        if e == "lock":
            depth += 1
        elif e == "unlock":
            depth -= 1
        elif e == "sleep":
            assert depth == 0, f"slept while holding the lock: {events}"
    capsys.readouterr()
    # The 80 old entries are 60s old after the sleep: out of the 1-minute
    # window but kept by the 1-hour one; the new entry uses the post-sleep
    # clock, not the pre-sleep one.
    assert json.loads((state_dir / "rate_limit.json").read_text()) == [999.0] * 80 + [1059.0]


def test_empty_inbox_leaves_no_cache_file(cli_env, capsys):
    """L3: an empty channel writes no `{}` cache file; the next inbox still
    lists empty (a missing cache always means a full fetch)."""
    backend = cli_env["backend"]
    state_dir = cli_env["config"].state_dir

    ret = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret == 0
    assert json.loads(capsys.readouterr().out) == []
    assert list(state_dir.glob("*_cache.json")) == []

    ret2 = main(
        ["inbox", "--identity", "repo=owner-org/worker,role=team-a", "--json"],
        backend=backend,
    )
    assert ret2 == 0
    assert json.loads(capsys.readouterr().out) == []
    assert list(state_dir.glob("*_cache.json")) == []
