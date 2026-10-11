"""Unit tests for the angareion CLI: four commands, exit codes, stdout/stderr (D1)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import sys

import pytest

from angareion.backend import BackendError, Cursor, FakeBackend, Target
from angareion.cli import build_parser, main
from angareion.config import Config, save_config
from angareion.github import HttpResponse
from angareion.messages import Message

TOKEN = "ghp_FAKE_CLI_TOKEN_ABC123"
IDENTITY = {"repo": "owner-org/worker", "role": "worker"}


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated config/state/token plus a FakeBackend with channel `general`."""
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    config = Config(
        channel_repo="owner-org/channel-repo",
        identity=IDENTITY,
        token_file=token_file,
        state_dir=state_dir,
        config_file=config_file,
    )
    save_config(config, config_file)

    monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))

    backend = FakeBackend()
    backend.create_channel("general", [])
    return {
        "backend": backend,
        "config": config,
        "tmp_path": tmp_path,
        "state_dir": state_dir,
        "token_file": token_file,
    }


def _seed_message(
    backend: FakeBackend,
    *,
    channel: str = "general",
    msg_id: str = "01AAAA00000000000000000000",
    at: str = "2026-10-10T12:00:00+00:00",
    content: str = "Task for worker",
    to: dict | None = None,
) -> Message:
    message = Message(
        from_={"repo": "owner-org/team", "role": "PM"},
        to=to or IDENTITY,
        channel=channel,
        content=content,
        id=msg_id,
        at=at,
    )
    backend.send(channel, message)
    return message


def test_help_exits_zero(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "angareion" in captured.out
    for command in ("channel", "send", "wait", "read"):
        assert command in captured.out


def test_no_subcommand_is_usage_error(capsys):
    assert main([]) == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert captured.out == ""


def test_channel_without_action_is_usage_error(capsys):
    assert main(["channel"]) == 1
    assert "channel" in capsys.readouterr().err


def test_bad_option_value_is_usage_error(cli_env, capsys):
    ret = main(
        ["send", "--channel", "general", "--to", "repo=owner-org/worker,role=worker",
         "--urgency", "not-a-number", "--body", "x"],
        backend=cli_env["backend"],
    )
    assert ret == 1
    assert capsys.readouterr().err != ""


def test_channel_create_prints_name_and_number(cli_env, capsys):
    ret = main(
        ["channel", "create", "new-topic", "--member", "repo=owner-org/team,role=PM"],
        backend=cli_env["backend"],
    )
    assert ret == 0
    assert capsys.readouterr().out == "new-topic #2\n"

    channel = cli_env["backend"].create_channel("new-topic", [])
    assert channel.number == 2
    assert channel.members == [{"repo": "owner-org/team", "role": "PM"}]


def test_channel_create_existing_is_idempotent(cli_env, capsys):
    assert main(["channel", "create", "general"], backend=cli_env["backend"]) == 0
    assert capsys.readouterr().out == "general #1\n"


def test_channel_create_rejects_group_member(cli_env, capsys):
    ret = main(
        ["channel", "create", "g", "--member", "group=all-workers"],
        backend=cli_env["backend"],
    )
    assert ret == 1
    assert "group" in capsys.readouterr().err


def test_send_prints_only_the_message_id(cli_env, capsys):
    ret = main(
        ["send", "--channel", "general", "--to", "repo=owner-org/worker,role=worker",
         "--urgency", "7", "--body", "Work request message"],
        backend=cli_env["backend"],
    )
    assert ret == 0
    captured = capsys.readouterr()
    message_id = captured.out.strip()
    assert len(message_id) == 26
    assert captured.err == ""

    messages = cli_env["backend"].read(Target(channel="general"), Cursor()).messages
    assert len(messages) == 1
    assert messages[0].id == message_id
    assert messages[0].content == "Work request message"
    assert messages[0].urgency == 7
    assert messages[0].from_ == IDENTITY
    assert messages[0].to == {"repo": "owner-org/worker", "role": "worker"}


def test_send_urgency_out_of_range_sends_nothing(cli_env, capsys):
    ret = main(
        ["send", "--channel", "general", "--to", "repo=owner-org/worker,role=worker",
         "--urgency", "12", "--body", "no"],
        backend=cli_env["backend"],
    )
    assert ret == 1
    assert "urgency" in capsys.readouterr().err
    assert cli_env["backend"].read(Target(channel="general"), Cursor()).messages == []


def test_send_group_address_rejected(cli_env, capsys):
    ret = main(
        ["send", "--channel", "general", "--to", "group=all-workers", "--body", "hi"],
        backend=cli_env["backend"],
    )
    assert ret == 1
    assert "group addressing is not supported in v0" in capsys.readouterr().err


def test_send_unknown_channel_exits_1(cli_env, capsys):
    ret = main(
        ["send", "--channel", "nowhere", "--to", "repo=owner-org/worker,role=worker", "--body", "hi"],
        backend=cli_env["backend"],
    )
    assert ret == 1
    assert "does not exist" in capsys.readouterr().err


def test_send_with_attachments(cli_env, capsys):
    sample_file = cli_env["tmp_path"] / "sample.txt"
    sample_file.write_text("file content 123", encoding="utf-8")

    ret = main(
        ["send", "--channel", "general", "--to", "repo=owner-org/worker,role=worker",
         "--attach", f"data={sample_file}", "--ref", "spec=https://example.com/spec",
         "--body", "attached text"],
        backend=cli_env["backend"],
    )
    assert ret == 0
    messages = cli_env["backend"].read(Target(channel="general"), Cursor()).messages
    assert messages[0].attachments == [
        {"name": "data", "inline": "file content 123"},
        {"name": "spec", "ref": "https://example.com/spec"},
    ]


def test_read_prints_messages_and_advances_the_cursor(cli_env, capsys):
    _seed_message(cli_env["backend"], msg_id="01AAAA00000000000000000000")

    ret = main(["read", "--channel", "general"], backend=cli_env["backend"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "01AAAA00000000000000000000" in captured.out
    assert "Task for worker" in captured.out
    assert "<owner-org/team×PM>" in captured.out
    assert "channel: general" in captured.out
    assert captured.err == ""

    # The local cursor moved: the same message does not come back.
    cursor_file = cli_env["state_dir"] / "cursor-owner-org_worker__worker.json"
    assert cursor_file.is_file()
    stored = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert stored == {"channel:general": {"at": "2026-10-10T12:00:00+00:00", "id": "01AAAA00000000000000000000"}}

    ret2 = main(["read", "--channel", "general"], backend=cli_env["backend"])
    assert ret2 == 0
    assert capsys.readouterr().out == ""


def test_read_json_output(cli_env, capsys):
    _seed_message(cli_env["backend"], msg_id="01AAAA00000000000000000000")

    ret = main(["read", "--channel", "general", "--json"], backend=cli_env["backend"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "01AAAA00000000000000000000"
    assert data[0]["content"] == "Task for worker"
    assert data[0]["from"] == {"repo": "owner-org/team", "role": "PM"}


def test_read_with_since_does_not_touch_the_cursor(cli_env, capsys):
    _seed_message(cli_env["backend"], msg_id="01AAAA00000000000000000000", at="2026-10-10T12:00:00+00:00")
    _seed_message(cli_env["backend"], msg_id="01BBBB00000000000000000000", at="2026-10-10T12:00:01+00:00")

    ret = main(
        ["read", "--channel", "general", "--since", "2026-10-10T12:00:00+00:00", "--json"],
        backend=cli_env["backend"],
    )
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01BBBB00000000000000000000"]
    assert not (cli_env["state_dir"] / "cursor-owner-org_worker__worker.json").exists()


def test_read_to_me_sees_messages_from_every_channel(cli_env, capsys):
    cli_env["backend"].create_channel("other", [])
    _seed_message(cli_env["backend"], channel="general", msg_id="01AAAA00000000000000000000")
    _seed_message(cli_env["backend"], channel="other", msg_id="01BBBB00000000000000000000")
    _seed_message(cli_env["backend"], channel="general", msg_id="01CCCC00000000000000000000",
                 to={"repo": "owner-org/worker", "role": "team-b"})

    ret = main(["read", "--json"], backend=cli_env["backend"])
    assert ret == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == ["01AAAA00000000000000000000", "01BBBB00000000000000000000"]


def test_read_unknown_channel_exits_1(cli_env, capsys):
    ret = main(["read", "--channel", "nowhere"], backend=cli_env["backend"])
    assert ret == 1
    assert "does not exist" in capsys.readouterr().err


def test_read_keeps_the_cursor_when_stdout_breaks(cli_env, capsys, monkeypatch):
    """`angareion read | head`: stdout closing early must not consume the messages (L2)."""
    _seed_message(cli_env["backend"], msg_id="01AAAA00000000000000000000")
    cursor_file = cli_env["state_dir"] / "cursor-owner-org_worker__worker.json"
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
    ret = main(["read", "--channel", "general"], backend=cli_env["backend"])
    assert ret == 1
    assert "cursor was not advanced" in capsys.readouterr().err
    assert not cursor_file.exists()

    # stdout back: the message is still there for the next read.
    monkeypatch.setattr(sys, "stdout", real_stdout)
    assert main(["read", "--channel", "general", "--json"], backend=cli_env["backend"]) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01AAAA00000000000000000000"]
    assert cursor_file.exists()


def test_wait_timeout_zero_without_new_messages_exits_3(cli_env, capsys):
    ret = main(["wait", "--channel", "general", "--timeout", "0"], backend=cli_env["backend"])
    assert ret == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Timed out" in captured.err


def test_wait_timeout_zero_with_new_message_exits_0(cli_env, capsys):
    _seed_message(cli_env["backend"])
    ret = main(["wait", "--channel", "general", "--timeout", "0"], backend=cli_env["backend"])
    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_wait_blocks_until_a_message_arrives(cli_env, capsys):
    backend = cli_env["backend"]
    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
        if len(sleeps) == 1:
            _seed_message(backend)

    ret = main(
        ["wait", "--channel", "general", "--timeout", "600"],
        backend=backend,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )
    assert ret == 0
    assert sleeps == [15.0]
    assert capsys.readouterr().out == ""
    # Waiting never writes the cursor; the following read still sees the message.
    assert not (cli_env["state_dir"] / "cursor-owner-org_worker__worker.json").exists()
    assert main(["read", "--channel", "general", "--json"], backend=backend) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_wait_negative_timeout_is_usage_error(cli_env, capsys):
    ret = main(["wait", "--channel", "general", "--timeout", "-1"], backend=cli_env["backend"])
    assert ret == 1
    assert "--timeout" in capsys.readouterr().err


def test_missing_channel_repo_exits_2(cli_env, capsys, monkeypatch):
    cli_env["config"].channel_repo = ""
    bad_config = cli_env["tmp_path"] / "bad-config.json"
    bad_config.write_text(
        json.dumps({"identity": {"repo": "owner-org/worker", "role": "worker"}}), encoding="utf-8"
    )
    monkeypatch.setenv("ANGAREION_CONFIG", str(bad_config))

    ret = main(
        ["send", "--channel", "general", "--to", "repo=owner-org/worker,role=worker", "--body", "x"],
        backend=cli_env["backend"],
    )
    assert ret == 2
    captured = capsys.readouterr()
    assert "channel_repo" in captured.err
    assert captured.out == ""


def test_token_permissions_too_open_exits_2_and_never_leaks(cli_env, capsys):
    cli_env["token_file"].chmod(0o644)

    ret = main(
        ["send", "--channel", "general", "--to", "repo=owner-org/worker,role=worker", "--body", "x"],
    )
    assert ret == 2
    captured = capsys.readouterr()
    assert "owner-only" in captured.err
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_readonly_token_0400_works_end_to_end(cli_env, capsys):
    """Worker mounts are 0400 read-only: the CLI must work there."""
    cli_env["token_file"].chmod(0o400)

    class InlineTransport:
        def __init__(self):
            self.requests = []

        def request(self, method, url, headers=None, body=None):
            self.requests.append((method, url))
            if url.endswith("/issues?state=all&per_page=100"):
                return HttpResponse(200, {}, b"[]")
            if method == "POST" and url.endswith("/repos/owner-org/channel-repo/issues"):
                return HttpResponse(201, {}, json.dumps({"number": 3, "title": "fresh", "body": ""}).encode("utf-8"))
            return HttpResponse(404, {}, b'{"message": "Not Found"}')

    transport = InlineTransport()
    ret = main(["channel", "create", "fresh"], transport=transport)
    assert ret == 0
    assert capsys.readouterr().out == "fresh #3\n"


def test_backend_error_masks_the_token(cli_env, capsys):
    cli_env["backend"].next_error = BackendError(f"Secondary rate limit for {TOKEN}")

    ret = main(
        ["wait", "--channel", "general", "--timeout", "0"],
        backend=cli_env["backend"],
    )
    assert ret == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.err
    assert "***" in captured.err
    assert captured.out == ""


def test_channel_create_without_a_name_is_usage_error(cli_env, capsys):
    assert main(["channel", "create"], backend=cli_env["backend"]) == 1
    assert capsys.readouterr().err != ""


def test_read_same_second_messages_reach_the_reader(cli_env, capsys):
    """Two messages in the same second: reading the first must not swallow the second."""
    same_second = "2026-10-10T12:00:00+00:00"
    backend = cli_env["backend"]
    _seed_message(backend, msg_id="01AAAA00000000000000000000", at=same_second)

    assert main(["read", "--channel", "general", "--json"], backend=backend) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01AAAA00000000000000000000"]

    _seed_message(backend, msg_id="01BBBB00000000000000000000", at=same_second)
    assert main(["read", "--channel", "general", "--json"], backend=backend) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == ["01BBBB00000000000000000000"]


def test_read_does_not_return_an_edited_message_again(cli_env, capsys):
    """An edited already-read comment (same id and at) must not come back as new."""
    message = _seed_message(cli_env["backend"], msg_id="01AAAA00000000000000000000")

    assert main(["read", "--channel", "general", "--json"], backend=cli_env["backend"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1

    message.content = "edited after being read"  # the GitHub updated_at moved; id and at did not
    assert main(["read", "--channel", "general", "--json"], backend=cli_env["backend"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_wait_without_timeout_blocks_until_a_message_arrives(cli_env, capsys):
    backend = cli_env["backend"]
    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
        if len(sleeps) == 2:
            _seed_message(backend)

    ret = main(
        ["wait", "--channel", "general"],  # no --timeout: wait forever
        backend=backend,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )
    assert ret == 0
    assert sleeps == [15.0, 15.0]
    assert not (cli_env["state_dir"] / "cursor-owner-org_worker__worker.json").exists()


class MiniGitHub:
    """A tiny stateful GitHub Issues + comments API for one repo; no network."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self._next_issue = 1
        self._next_comment = 1

    def request(self, method, url, headers=None, body=None):
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if method == "GET" and path.endswith("/issues"):
            return HttpResponse(200, {}, json.dumps(list(self.issues.values())).encode("utf-8"))

        if method == "POST" and path.endswith("/issues"):
            payload = json.loads(body.decode("utf-8"))
            number = self._next_issue
            self._next_issue += 1
            issue = {"number": number, "title": payload["title"], "body": payload.get("body", "")}
            self.issues[number] = issue
            return HttpResponse(201, {}, json.dumps(issue).encode("utf-8"))

        if method == "GET" and path.endswith("/issues/comments"):
            comments = list(self.comments)
        elif method == "GET" and "/issues/" in path and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            comments = [c for c in self.comments if c["_issue_number"] == number]
        elif method == "POST" and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/")[0])
            payload = json.loads(body.decode("utf-8"))
            comment = {
                "id": self._next_comment,
                "body": payload["body"],
                "html_url": f"https://github.com/owner-org/channel-repo/issues/{number}#issuecomment-{self._next_comment}",
                "issue_url": f"https://api.github.com/repos/owner-org/channel-repo/issues/{number}",
                "updated_at": "2026-10-10T12:00:00Z",
                "_issue_number": number,
            }
            self._next_comment += 1
            self.comments.append(comment)
            return HttpResponse(201, {}, json.dumps(comment).encode("utf-8"))
        else:
            return HttpResponse(404, {}, b'{"message": "Not Found"}')

        since = params.get("since")
        if since:
            comments = [c for c in comments if c["updated_at"] >= since[0]]
        return HttpResponse(200, {}, json.dumps(comments).encode("utf-8"))


def test_four_commands_over_the_github_backend_without_network(cli_env, capsys):
    """The four commands behave the same over GitHubIssuesBackend (fake transport)."""
    transport = MiniGitHub()

    assert main(
        ["channel", "create", "phase-1", "--member", "repo=owner-org/worker,role=worker"],
        transport=transport,
    ) == 0
    assert capsys.readouterr().out == "phase-1 #1\n"

    assert main(
        ["send", "--channel", "phase-1", "--to", "repo=owner-org/worker,role=worker", "--body", "hello"],
        transport=transport,
    ) == 0
    message_id = capsys.readouterr().out.strip()
    assert len(message_id) == 26

    assert main(["wait", "--channel", "phase-1", "--timeout", "0"], transport=transport) == 0
    capsys.readouterr()

    assert main(["read", "--channel", "phase-1", "--json"], transport=transport) == 0
    data = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in data] == [message_id]

    # Nothing new: read is empty and wait times out (a fresh process has no ETag yet).
    assert main(["read", "--channel", "phase-1", "--json"], transport=transport) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["wait", "--channel", "phase-1", "--timeout", "0"], transport=transport) == 3
    capsys.readouterr()

    # "To me" has its own fresh cursor, so the same message is visible there.
    assert main(["read", "--json"], transport=transport) == 0
    assert [m["id"] for m in json.loads(capsys.readouterr().out)] == [message_id]


def test_backend_http_error_exits_2_without_retry_and_without_the_token(cli_env, capsys):
    class ErrorTransport:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, headers=None, body=None):
            self.calls += 1
            return HttpResponse(403, {"retry-after": "60"}, b'{"message": "secondary rate limit"}')

    transport = ErrorTransport()
    ret = main(["wait", "--channel", "general", "--timeout", "0"], transport=transport)
    assert ret == 2
    captured = capsys.readouterr()
    assert "403" in captured.err
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out
    assert transport.calls == 1  # no automatic retry


def test_non_json_backend_response_exits_2(cli_env, capsys):
    class HtmlTransport:
        def request(self, method, url, headers=None, body=None):
            if url.endswith("/issues?state=all&per_page=100"):
                return HttpResponse(200, {}, json.dumps([{"number": 1, "title": "general", "body": ""}]).encode("utf-8"))
            return HttpResponse(200, {}, b"<html>proxy login page</html>")

    ret = main(["read", "--channel", "general"], transport=HtmlTransport())
    assert ret == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "non-JSON" in captured.err


def test_unexpected_value_error_from_a_backend_is_exit_2(cli_env, capsys):
    """A ValueError out of a backend is a backend error (2), not input (1)."""
    cli_env["backend"].next_error = ValueError("boom from a backend")
    ret = main(["read", "--channel", "general"], backend=cli_env["backend"])
    assert ret == 2
    assert "boom from a backend" in capsys.readouterr().err


def test_transport_timeout_exits_2_without_waiting_out_the_timeout(cli_env, capsys):
    import socket

    class TimeoutTransport:
        def __init__(self, exc):
            self.exc = exc

        def request(self, method, url, headers=None, body=None):
            raise self.exc

    def forbidden_sleep(seconds):
        raise AssertionError("a backend timeout must surface, not sleep past the --timeout")

    clock = {"now": 1000.0}
    for exc in (TimeoutError("timed out"), socket.timeout("timed out")):
        ret = main(
            ["wait", "--channel", "general", "--timeout", "600"],
            transport=TimeoutTransport(exc),
            sleeper=forbidden_sleep,
            clock=lambda: clock["now"],
        )
        assert ret == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "timed out" in captured.err
