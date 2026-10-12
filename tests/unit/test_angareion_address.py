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

PM = "LLMGateway-pm"
ME = "MyLinuxPool-impl-2"


@pytest.mark.parametrize("name", [PM, ME, "my-repo-impl-2", "repo_1.2-arch"])
def test_parse_address_accepts_agent_names(name):
    assert parse_address(name) == name
    assert parse_address(f"  {name}  ") == name  # surrounding space is trimmed


@pytest.mark.parametrize(
    "raw",
    [
        "myrepo-dev",
        "MyRepo-PM",
        "-pm",
        "foo-impl-",
        "foo-impl-2!",
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


def test_send_without_to_builds_a_broadcast_message():
    sent = {}

    class RecordingApp:
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
