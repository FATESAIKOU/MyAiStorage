"""Unit tests for the v5 address surface of the CLI (section 2.2).

`parse_address` only knows agent names; `--member` is gone; `--to` is optional.
These tests do not touch config files or a backend.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import argparse

import pytest

from angareion.cli import build_parser, cmd_send, main, parse_address
from angareion.messages import MessageValidationError

PM = "mypmo-pm-m1"
ME = "mylinuxpool-impl-w1-1"


@pytest.mark.parametrize(
    "name",
    [
        PM,
        ME,
        "mylinuxpool-rev-w1-1",
        "myaistorage-arch-m1-2",
        "myaientry-secretary-p1",
        "pm-tools-impl-m1-1",  # the project may contain a role word
    ],
)
def test_parse_address_accepts_ai_names(name):
    assert parse_address(name) == name
    assert parse_address(f"  {name}  ") == name  # half-width spaces are forgiven


@pytest.mark.parametrize("dirt", ["\n", "\t", "\r", " \n", "\n ", " \t "])
def test_only_half_width_spaces_are_forgiven(dirt):
    """R3: a pasted-in newline/tab/CR is refused, not quietly trimmed."""
    with pytest.raises(MessageValidationError) as caught:
        parse_address(f"{dirt}mypmo-pm-m1{dirt}")
    message = str(caught.value)
    assert "step 1" in message
    assert "lowercase" in message


def test_the_empty_address_check_is_about_spaces_only():
    with pytest.raises(MessageValidationError, match="cannot be empty"):
        parse_address("  ")
    with pytest.raises(MessageValidationError, match="step 1"):
        parse_address("\n")


@pytest.mark.parametrize(
    "raw",
    [
        "myrepo-dev-m1",
        "MyRepo-PM-m1",
        "-pm-m1",
        "foo-impl-",
        "foo-impl-w1-1!",
        "group=全員",
        "repo=owner/team,role=pm",
        "owner/team×pm",
        "owner/team:pm",
        '{"repo": "owner/team", "role": "pm"}',
        "",
        "   ",
    ],
)
def test_parse_address_rejects_everything_else(raw):
    with pytest.raises(MessageValidationError):
        parse_address(raw)


def test_a_group_address_is_a_phase_3_feature():
    """"群組位址期 3 才有" - and it is not a usage error about the grammar."""
    with pytest.raises(MessageValidationError) as caught:
        parse_address("@all")
    message = str(caught.value)
    assert "group address" in message
    assert "phase 3" in message


def test_send_to_is_optional_in_the_parser():
    parser = build_parser()
    args = parser.parse_args(["send", "--channel", "general", "--body", "hi"])
    assert args.to is None  # no --to: a broadcast

    args = parser.parse_args(["send", "--channel", "general", "--to", ME, "--body", "hi"])
    assert args.to == ME


def test_channel_create_no_longer_takes_member():
    parser = build_parser()
    args = parser.parse_args(["channel", "create", "general"])
    assert not hasattr(args, "member")

    ret = main(["channel", "create", "general", "--member", "repo=owner/team,role=pm"])
    assert ret == 1  # unknown argument: usage error


def test_send_rejects_a_non_ascii_channel_name_before_touching_the_backend():
    args = argparse.Namespace(channel="小明", to=None, urgency=5, body="hi", attach=[], ref=[])
    config = argparse.Namespace(identity=PM)

    class ExplodingApp:
        def send(self, *arguments, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("nothing may be sent for a bad channel name")

    assert cmd_send(args, config, ExplodingApp(), token="") == 1


def test_send_rejects_a_bad_to_before_touching_the_backend():
    args = argparse.Namespace(channel="general", to="repo=owner/team,role=pm", urgency=5, body="hi", attach=[], ref=[])
    config = argparse.Namespace(identity=PM)

    class ExplodingApp:
        def send(self, *arguments, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("nothing may be sent for a bad address")

    assert cmd_send(args, config, ExplodingApp(), token="") == 1


@pytest.mark.parametrize(
    "to, expected",
    [
        ("mypmo-pm", "team"),          # 缺 team 段
        ("mylinuxpool-pm", "team"),
        ("myaientry-secretary", "team"),
        ("MyPMO-pm-m1", "lowercase"),  # 請用小寫
        ("mylinuxpool-impl-pm", "team"),  # the team segment is a role word
        ("mylinuxpool-impl-w1-0", "step 5"),
        ("@all", "phase 3"),           # 群組位址期 3 才有
    ],
)
def test_send_explains_why_a_to_was_refused(to, expected, capsys):
    """exit 1 and the reason on stderr - nothing reaches the backend."""
    args = argparse.Namespace(channel="general", to=to, urgency=5, body="hi", attach=[], ref=[])
    config = argparse.Namespace(identity=PM)

    class ExplodingApp:
        def send(self, *arguments, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("nothing may be sent for a bad address")

    assert cmd_send(args, config, ExplodingApp(), token="") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    assert "--to" in captured.err


def test_send_without_to_builds_a_broadcast_message():
    sent = {}

    class NoopStore:
        def ensure_fresh(self):
            pass

    class RecordingApp:
        store = NoopStore()

        def send(self, channel, message):
            sent["channel"] = channel
            sent["message"] = message
            return message.id

    args = argparse.Namespace(channel="general", to=None, urgency=5, body="all hands", attach=[], ref=[])
    config = argparse.Namespace(identity=PM)

    assert cmd_send(args, config, RecordingApp(), token="") == 0
    message = sent["message"]
    assert message.to is None
    assert message.from_ == PM
    assert message.channel == "general"
    assert message.content == "all hands"
