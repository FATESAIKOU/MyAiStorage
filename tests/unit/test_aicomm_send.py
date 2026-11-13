"""aicomm send: write to Angareion first, then wake the recipient on the local sidecar (#212)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from aicomm import sidecar_link
from _aicomm_fakes import FAKE_ID, WAKE_TEXT, LinkDir, RawSidecar, run_aicomm
from _aicomm_fakes import aicomm_world as world  # noqa: F401  (the fixture)

BODY = "請看這份草稿 SECRET-BODY-TEXT"


def send(world, monkeypatch, capsys, *extra, to="demo-pm-t1", stdin=None, body=BODY):
    argv = ["send", "--channel", "team-demo"]
    if to:
        argv += ["--to", to]
    argv += list(extra)
    if body is not None:
        argv += ["--body", body]
    return run_aicomm(argv, monkeypatch, capsys, stdin=stdin)


def opt(argv, name):
    """The value of `--name=value`; a bare `--name value` pair is a failure (single token only)."""
    assert name not in argv, f"{name} must be passed as a single {name}=VALUE token: {argv}"
    (hit,) = [a for a in argv if a.startswith(name + "=")]
    return hit[len(name) + 1:]


def agent(world, name="demo-pm-t1", state="idle", **extra):
    world.sidecar.agents[name] = {"id": name, "state": state, **extra}


# --- the command set -------------------------------------------------------------------

@pytest.mark.parametrize("word", ["poke", "status"])
def test_unknown_subcommand_exits_1_and_says_only_send_and_inbox(world, monkeypatch, capsys, word):
    r = run_aicomm([word], monkeypatch, capsys)
    assert r.code == 1
    assert "send" in r.err and "inbox" in r.err
    assert world.calls() == [] and world.sidecar.requests == []


SUFFIX = "aicomm 只有 send、inbox 與 log"


@pytest.mark.parametrize("argv", [
    ["send", "--channel", "c"],                                   # missing --body
    ["send", "--channel", "c", "--body", "x", "--bogus"],         # extra argument
    ["inbox", "--wait", "abc"],                                   # bad number
    ["send", "--body", "x"],                                      # missing --channel
])
def test_other_usage_errors_do_not_claim_the_subcommand_was_wrong(world, monkeypatch, capsys, argv):
    r = run_aicomm(argv, monkeypatch, capsys)
    assert r.code == 1
    assert r.err.strip()
    assert SUFFIX not in r.err
    assert world.calls() == [] and world.sidecar.requests == []


def test_a_wrong_subcommand_name_still_gets_the_hint(world, monkeypatch, capsys):
    r = run_aicomm(["poke"], monkeypatch, capsys)
    assert r.code == 1
    assert "invalid choice" in r.err
    assert SUFFIX in r.err


@pytest.mark.parametrize("extra", [["--to", ""], ["--mention", ""], ["--to", "", "--mention", ""]])
def test_an_empty_name_is_never_looked_up_or_woken(world, monkeypatch, capsys, extra):
    # the fake angareion accepts the empty address and exits 0 (the real one refuses it today)
    r = run_aicomm(["send", "--channel", "team-demo", *extra, "--body", "x"], monkeypatch, capsys)
    assert r.code == 0
    assert len(world.calls()) == 1
    assert world.sidecar.requests == []


def test_an_empty_name_does_not_stop_a_real_one_from_being_woken(world, monkeypatch, capsys):
    agent(world)
    r = run_aicomm(["send", "--channel", "team-demo", "--to", "demo-pm-t1", "--mention", "",
                    "--body", "x"], monkeypatch, capsys)
    assert r.code == 0
    assert [q["path"] for q in world.sidecar.requests] == ["/agents/demo-pm-t1", "/agents/demo-pm-t1/poke"]


def test_guard_refuses_to_run_without_a_fake_sidecar(world, monkeypatch, capsys):
    monkeypatch.delenv("AICOMM_SIDECAR")
    with pytest.raises(AssertionError, match="AICOMM_SIDECAR"):
        run_aicomm(["send", "--channel", "c", "--body", "x"], monkeypatch, capsys)


# --- delivery = written into Angareion ---------------------------------------------------

def test_the_message_id_is_the_first_stdout_line(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.out.splitlines()[0] == FAKE_ID


def test_angareion_failure_exits_with_its_code_and_wakes_nobody(world, monkeypatch, capsys):
    agent(world)
    world.setenv("FAKE_ANG_SEND_EXIT", "1")
    r = send(world, monkeypatch, capsys)
    assert r.code == 1
    assert world.sidecar.requests == []


def test_angareion_backend_error_code_is_passed_through(world, monkeypatch, capsys):
    agent(world)
    world.setenv("FAKE_ANG_SEND_EXIT", "2")
    r = send(world, monkeypatch, capsys)
    assert r.code == 2
    assert world.sidecar.requests == []


def test_sidecar_down_still_exits_0_and_says_delivered_not_woken(world, monkeypatch, capsys):
    with socket.socket() as s:  # a loopback port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    world.setenv("AICOMM_SIDECAR", f"http://127.0.0.1:{dead}")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.out.splitlines()[0] == FAKE_ID
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"


def test_a_missing_angareion_executable_is_a_config_error(world, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    r = send(world, monkeypatch, capsys)
    assert r.code == 2
    assert "angareion" in r.err


def test_body_from_stdin_goes_through_to_angareion(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, body="-", stdin="多行\n內文\n")
    assert r.code == 0
    (call,) = world.calls()
    assert opt(call["argv"], "--body") == "-"
    assert call["stdin"] == "多行\n內文\n"


def test_message_fields_are_handed_to_angareion_unchanged(world, monkeypatch, capsys):
    agent(world)
    send(world, monkeypatch, capsys, "--in-reply-to", "01FAKEREPLY00000000000000", "--urgency", "7",
         "--mention", "demo-arch-t1")
    (call,) = world.calls()
    argv = call["argv"]
    assert argv[0] == "send"
    assert opt(argv, "--channel") == "team-demo"
    assert opt(argv, "--to") == "demo-pm-t1"
    assert opt(argv, "--mention") == "demo-arch-t1"
    assert opt(argv, "--in-reply-to") == "01FAKEREPLY00000000000000"
    assert opt(argv, "--urgency") == "7"
    assert opt(argv, "--body") == BODY


def test_channel_is_required(world, monkeypatch, capsys):
    r = run_aicomm(["send", "--to", "demo-pm-t1", "--body", "x"], monkeypatch, capsys)
    assert r.code == 1
    assert world.calls() == []


# --- ticket ------------------------------------------------------------------------------

def test_no_ticket_means_no_ref_attachment(world, monkeypatch, capsys):
    agent(world)
    send(world, monkeypatch, capsys)
    (call,) = world.calls()
    assert "--ref" not in call["argv"]


def test_ticket_becomes_a_ref_and_the_body_is_not_touched(world, monkeypatch, capsys):
    agent(world)
    send(world, monkeypatch, capsys, "--ticket", "MyAiStorage#212")
    (call,) = world.calls()
    argv = call["argv"]
    assert opt(argv, "--ref") == "ticket=MyAiStorage#212"
    assert opt(argv, "--body") == BODY


@pytest.mark.parametrize("bad", ["MyAiStorage", "MyAiStorage#", "#212", "My Repo#1", "a/b#1", "x#1a", "x#1\n"])
def test_a_malformed_ticket_exits_1_and_sends_nothing(world, monkeypatch, capsys, bad):
    agent(world)
    r = send(world, monkeypatch, capsys, "--ticket", bad)
    assert r.code == 1
    assert world.calls() == [] and world.sidecar.requests == []


# --- --ref (#278) -------------------------------------------------------------------------

def refs(argv):
    """Every `--ref` value in the order handed to angareion."""
    assert "--ref" not in argv, f"--ref must be a single --ref=VALUE token: {argv}"
    return [a[len("--ref="):] for a in argv if a.startswith("--ref=")]


def test_a_ref_is_attached_and_the_recipient_is_still_woken(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, "--ref", "pr=https://example.invalid/pull/278")
    assert r.code == 0
    (call,) = world.calls()
    assert refs(call["argv"]) == ["pr=https://example.invalid/pull/278"]
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]


def test_two_refs_keep_their_order_and_follow_the_ticket(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys,
             "--ref", "pr=https://example.invalid/pull/278",
             "--ticket", "MyAiStorage#278",
             "--ref", "commit=abc123")
    assert r.code == 0
    (call,) = world.calls()
    assert refs(call["argv"]) == [
        "ticket=MyAiStorage#278",
        "pr=https://example.invalid/pull/278",
        "commit=abc123",
    ]
    assert opt(call["argv"], "--body") == BODY


@pytest.mark.parametrize("extra", [
    ["--ref", "pr"],
    ["--ref", ""],
    ["--ref", "https://example.invalid/pull/278"],
    ["--ref", "pr=https://example.invalid/pull/278", "--ref", "noequals"],
    ["--ticket", "MyAiStorage#278", "--ref", "noequals"],
], ids=["no-equals", "empty", "url-only", "second-one-bad", "with-ticket"])
def test_a_ref_without_an_equals_sign_exits_1_and_sends_nothing(world, monkeypatch, capsys, extra):
    agent(world)
    r = send(world, monkeypatch, capsys, *extra)
    assert r.code == 1
    assert "name" in r.err and "locator" in r.err
    assert world.calls() == [] and world.sidecar.requests == []


def test_a_ref_starting_with_a_dash_stays_one_token(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, "--ref", "-x=-y --not-an-option")
    assert r.code == 0
    (call,) = world.calls()
    assert "--ref=-x=-y --not-an-option" in call["argv"]
    assert refs(call["argv"]) == ["-x=-y --not-an-option"]


def test_send_help_lists_ref_like_angareion(world, monkeypatch, capsys):
    r = run_aicomm(["send", "--help"], monkeypatch, capsys)
    assert r.code == 0
    assert "[--ref NAME=LOCATOR]" in r.out
    assert "Attach a reference or link (repeatable)." in r.out


# --- who gets woken, and when --------------------------------------------------------------

@pytest.mark.parametrize("extra", [{"attach": "tmux:demo"}, {}], ids=["tui", "headless"])
def test_an_idle_agent_is_poked_once_without_interrupt_whatever_its_kind(world, monkeypatch, capsys, extra):
    agent(world, state="idle", **extra)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]
    assert [q["path"] for q in world.sidecar.requests] == ["/agents/demo-pm-t1", "/agents/demo-pm-t1/poke"]


def test_a_running_agent_is_poked_too_without_interrupt(world, monkeypatch, capsys):
    agent(world, state="running")
    send(world, monkeypatch, capsys)
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]


def test_two_sends_to_a_running_agent_are_two_pokes(world, monkeypatch, capsys):
    agent(world, state="running")
    send(world, monkeypatch, capsys)
    send(world, monkeypatch, capsys)
    assert len(world.sidecar.pokes()) == 2


def test_interrupt_on_a_running_agent_pokes_with_interrupt(world, monkeypatch, capsys):
    agent(world, state="running")
    send(world, monkeypatch, capsys, "--interrupt")
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": True, "via": "aicomm", "ref": FAKE_ID}]


def test_interrupt_on_an_idle_agent_stays_a_plain_poke(world, monkeypatch, capsys):
    agent(world, state="idle")
    send(world, monkeypatch, capsys, "--interrupt")
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]


def test_interrupt_and_no_wake_exclude_each_other_before_anything_is_sent(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, "--interrupt", "--no-wake")
    assert r.code == 1
    assert world.calls() == [] and world.sidecar.requests == []


@pytest.mark.parametrize("state", ["starting", "waiting-quota", "exited"])
def test_other_states_are_not_poked(world, monkeypatch, capsys, state):
    agent(world, state=state)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert r.err == f"wake: demo-pm-t1 not-woken state-{state}\n"


def test_an_agent_on_another_machine_is_not_poked_and_that_is_normal(world, monkeypatch, capsys):
    r = send(world, monkeypatch, capsys)  # the fake sidecar does not know the agent -> 404
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert r.out.splitlines()[0] == FAKE_ID


def test_a_broadcast_touches_no_sidecar_at_all(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, to=None)
    assert r.code == 0
    assert world.sidecar.requests == []
    assert len(world.calls()) == 1


def test_a_mentioned_agent_is_woken_like_the_recipient(world, monkeypatch, capsys):
    agent(world, "demo-arch-t1", state="idle")
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1", to=None)
    assert r.code == 0
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]
    assert world.sidecar.requests[1]["path"] == "/agents/demo-arch-t1/poke"


def test_to_and_mentions_are_each_judged_once(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1")
    agent(world, "demo-arch-t1")
    send(world, monkeypatch, capsys, "--mention", "demo-arch-t1", "--mention", "demo-pm-t1",
         "--mention", "demo-arch-t1")
    gets = [q["path"] for q in world.sidecar.requests if q["method"] == "GET"]
    assert sorted(gets) == ["/agents/demo-arch-t1", "/agents/demo-pm-t1"]
    assert len(world.sidecar.pokes()) == 2


def test_nobody_pokes_themselves(world, monkeypatch, capsys):
    agent(world, world.sender, state="idle")
    r = send(world, monkeypatch, capsys, to=world.sender)
    assert r.code == 0
    assert world.sidecar.requests == []


def test_no_wake_touches_no_sidecar(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, "--no-wake")
    assert r.code == 0
    assert world.sidecar.requests == []
    assert len(world.calls()) == 1


@pytest.mark.parametrize("sender", ["demo-pm-t1", "demo-impl-t1-2", "demo-rev-t1-1", "demo-secretary-t1", "someone-else"])
def test_behaviour_does_not_depend_on_the_role_of_either_side(world, monkeypatch, capsys, sender):
    world.setenv("ANGAREION_IDENTITY", sender)
    for name in ("demo-pm-t2", "demo-impl-t2-1", "demo-secretary-t2"):
        agent(world, name, state="idle")
        r = send(world, monkeypatch, capsys, to=name)
        assert r.code == 0
    assert [p["interrupt"] for p in world.sidecar.pokes()] == [False, False, False]
    assert len(world.sidecar.pokes()) == 3


# --- the poke itself ---------------------------------------------------------------------------

def test_the_poke_text_is_fixed_and_carries_nothing_from_the_message(world, monkeypatch, capsys):
    agent(world)
    send(world, monkeypatch, capsys, "--ticket", "MyAiStorage#212")
    (poke,) = world.sidecar.pokes()
    assert poke["text"] == WAKE_TEXT
    for leaked in (world.sender, FAKE_ID, "SECRET-BODY-TEXT", "212", "team-demo"):
        assert leaked not in poke["text"]
    assert set(poke) == {"text", "interrupt", "via", "ref"}


def test_a_poke_timeout_is_not_retried_and_does_not_change_the_exit_code(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "POKE_TIMEOUT", 0.3)
    world.sidecar.poke_delay = 2.0
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert len(world.sidecar.pokes()) == 1
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"


@pytest.mark.parametrize("status", [409, 503, 500])
def test_a_refused_poke_is_not_retried_and_the_exit_code_stays_0(world, monkeypatch, capsys, status):
    agent(world)
    world.sidecar.poke_status = status
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert len(world.sidecar.pokes()) == 1
    assert r.err == f"wake: demo-pm-t1 not-woken http-{status}\n"


def test_a_lookup_timeout_means_no_poke(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "GET_TIMEOUT", 0.3)
    world.sidecar.get_delay = 2.0
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'"idle"', b"{}", b'{"state": 3}', b'{"state": null}', b'{"id": "x"}'],
    ids=["text", "list", "string", "no-state", "int-state", "null-state", "other-field"],
)
def test_a_broken_lookup_answer_means_no_poke_and_exit_0(world, monkeypatch, capsys, payload):
    world.sidecar.raw["demo-pm-t1"] = (200, payload)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert r.err == "wake: demo-pm-t1 not-woken bad-answer\n"
    assert r.out.splitlines()[0] == FAKE_ID


def test_an_unexpected_lookup_status_means_no_poke(world, monkeypatch, capsys):
    world.sidecar.raw["demo-pm-t1"] = (500, b'{"state": "idle"}')
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert r.err == "wake: demo-pm-t1 not-woken http-500\n"


def test_the_sidecar_only_sees_lookups_and_pokes(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1", state="running")
    agent(world, "demo-arch-t1", state="idle")
    send(world, monkeypatch, capsys, "--mention", "demo-arch-t1", "--interrupt")
    send(world, monkeypatch, capsys, "--mention", "demo-gone-t1")
    assert world.sidecar.requests  # something was asked...
    assert world.sidecar.foreign_requests() == []  # ...and only lookups and pokes
    assert {(q["method"], q["path"].endswith("/poke")) for q in world.sidecar.requests} <= {("GET", False), ("POST", True)}


# --- the address --------------------------------------------------------------------------------

@pytest.mark.parametrize("override", ["http://192.0.2.1:8471", "http://example.invalid:8471", "192.0.2.1:8471", "http://[2001:db8::1]:8471"])
def test_a_non_loopback_override_is_refused_before_anything_is_written(world, monkeypatch, capsys, override):
    world.setenv("AICOMM_SIDECAR", override)
    r = send(world, monkeypatch, capsys)
    assert r.code == 1
    assert world.calls() == [] and world.sidecar.requests == []


@pytest.mark.parametrize("extra,to", [(("--no-wake",), "demo-pm-t1"), ((), None)], ids=["no-wake", "broadcast"])
def test_a_non_loopback_override_does_not_matter_when_nobody_is_to_be_woken(world, monkeypatch, capsys, extra, to):
    world.setenv("AICOMM_SIDECAR", "http://192.0.2.1:8471")
    r = send(world, monkeypatch, capsys, *extra, to=to)
    assert r.code == 0
    assert len(world.calls()) == 1 and world.sidecar.requests == []


def test_sending_only_to_myself_does_not_need_the_address(world, monkeypatch, capsys):
    world.setenv("AICOMM_SIDECAR", "http://192.0.2.1:8471")
    r = send(world, monkeypatch, capsys, to=world.sender)
    assert r.code == 0
    assert len(world.calls()) == 1


@pytest.mark.parametrize("value,base", [
    ("localhost:8471", "http://localhost:8471"),
    ("http://127.0.0.1:9", "http://127.0.0.1:9"),
    ("http://127.9.9.9:9/", "http://127.9.9.9:9"),
    ("http://[::1]:9", "http://[::1]:9"),
])
def test_loopback_override_spellings_are_accepted(value, base):
    from aicomm import cli

    assert cli.sidecar_base(value) == base


@pytest.mark.parametrize("value", ["192.0.2.1:8471", "http://example.invalid", "http://10.0.0.5:1", "http://[2001:db8::1]:1", ""])
def test_non_loopback_override_spellings_are_rejected(value):
    from aicomm import cli

    with pytest.raises(ValueError):
        cli.sidecar_base(value)


@pytest.mark.parametrize("override", ["http://127.0.0.1:1,http://127.0.0.1:2", "http://127.0.0.1:1 http://127.0.0.1:2", "http://127.0.0.1:1/path"])
def test_only_a_single_bare_address_is_accepted(world, monkeypatch, capsys, override):
    world.setenv("AICOMM_SIDECAR", override)
    r = send(world, monkeypatch, capsys)
    assert r.code == 1
    assert world.calls() == []


def test_no_override_means_the_documented_default_loopback_address(world, monkeypatch, capsys):
    from aicomm import cli

    assert cli.DEFAULT_SIDECAR == "http://127.0.0.1:8471"
    assert cli.sidecar_base(None) == "http://127.0.0.1:8471"


# --- review fixes (fix1) -------------------------------------------------------------------------

_IDLE = b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\n{"state":"idle"}'
_GARBAGE = b"GARBAGE\r\n\r\n"
_TRUNCATED = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"


@pytest.mark.parametrize("answers,connections", [
    ([_GARBAGE], 2),            # broken status line on every lookup
    ([_TRUNCATED], 2),          # body cut short on every lookup
    ([_IDLE, _GARBAGE, _IDLE, _GARBAGE], 4),     # lookup fine, the poke answer is broken (both recipients)
    ([_IDLE, _TRUNCATED, _IDLE, _TRUNCATED], 4),
], ids=["bad-status-line", "truncated-lookup", "bad-poke-status", "truncated-poke"])
def test_a_broken_sidecar_answer_never_undoes_a_delivered_message(world, monkeypatch, capsys, answers, connections):
    raw = RawSidecar(answers)
    try:
        world.setenv("AICOMM_SIDECAR", raw.url)
        r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1")
    finally:
        raw.close()
    assert r.code == 0
    assert r.out.splitlines()[0] == FAKE_ID
    assert "Traceback" not in r.err
    lines = [l for l in r.err.splitlines() if l.startswith("wake: ")]
    assert {l.split(" ")[1] for l in lines} == {"demo-pm-t1", "demo-arch-t1"}
    for line in lines:  # an unreadable lookup answer, or a poke gone wrong: never a resend
        assert line.endswith(("not-woken bad-answer", "unknown poke-error")), line
    # the second recipient was still judged after the first one broke
    assert raw.connections == connections


@pytest.mark.parametrize("override", ["127.0.0.1:abc", "http://127.0.0.1:99999", "http://127.0.0.1:-1"])
def test_a_bad_port_is_refused_before_anything_is_written(world, monkeypatch, capsys, override):
    world.setenv("AICOMM_SIDECAR", override)
    r = send(world, monkeypatch, capsys)
    assert r.code == 1
    assert "Traceback" not in r.err
    assert world.calls() == []


def test_an_empty_to_is_left_to_angareion_to_refuse(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_SEND_EXIT", "1")  # what the real angareion does with an empty address
    r = run_aicomm(["send", "--channel", "team-demo", "--to", "", "--body", "x"], monkeypatch, capsys)
    assert r.code == 1
    (call,) = world.calls()
    assert opt(call["argv"], "--to") == ""   # not dropped into a broadcast
    assert world.sidecar.requests == []      # and nobody is looked up or woken


def test_a_body_starting_with_a_dash_stays_one_token(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, body="-x --not-an-option")
    assert r.code == 0
    (call,) = world.calls()
    assert "--body=-x --not-an-option" in call["argv"]


@pytest.mark.parametrize("flag", ["nan", "inf", "-inf"])
def test_wait_refuses_a_non_finite_number(world, monkeypatch, capsys, flag):
    r = run_aicomm(["inbox", "--wait", flag], monkeypatch, capsys)
    assert r.code == 1
    assert world.calls() == []


def test_version_and_help_work_and_touch_nothing(world, monkeypatch, capsys):
    r = run_aicomm(["--version"], monkeypatch, capsys)
    assert r.code == 0 and r.out.startswith("aicomm ")
    for argv in (["--help"], ["send", "--help"], ["inbox", "--help"]):
        r = run_aicomm(argv, monkeypatch, capsys)
        assert r.code == 0 and "usage:" in r.out
    assert world.calls() == [] and world.sidecar.requests == []


def test_an_http_proxy_in_the_environment_is_never_used_for_the_sidecar(world, monkeypatch, capsys):
    import importlib

    from aicomm import cli

    with socket.socket() as s:  # a loopback port nobody listens on, as the "proxy"
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    agent(world)
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{dead}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(cli)  # the opener is built at import time, from the environment
    try:
        r = send(world, monkeypatch, capsys)
    finally:
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.delenv(name)
        importlib.reload(cli)
    assert r.code == 0
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]


# --- #368 PR-1: the `wake:` lines and `--require-wake` --------------------------------------
# C-n refers to the contract list in the `aicomm-cross-machine-wake` change (tasks.md §1).
# The local path came first; the cross-machine fallback is below (#457).
# The one grammar every `wake:` line is checked against is `WAKE_406_RE` (#406 D-1, M-1).


def test_c1_an_idle_local_agent_is_woken_locally_and_nobody_else_is_asked(world, monkeypatch, capsys):
    agent(world, state="idle")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken local\n"
    assert [q["path"] for q in world.sidecar.requests] == ["/agents/demo-pm-t1", "/agents/demo-pm-t1/poke"]


def test_c2_a_running_agent_poked_with_interrupt_says_so(world, monkeypatch, capsys):
    agent(world, state="running")
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.err == "wake: demo-pm-t1 woken local interrupt\n"
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": True, "via": "aicomm", "ref": FAKE_ID}]


def test_c3_a_local_agent_in_another_state_is_not_woken(world, monkeypatch, capsys):
    agent(world, state="exited")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken state-exited\n"
    assert world.sidecar.pokes() == []


@pytest.mark.parametrize("state", [
    "idle\nwake: demo-pm-t1 woken local",
    "idle with a blank",
    "exited ",
    "",
    "閒置",
], ids=["newline", "blank", "trailing-blank", "empty", "non-ascii"])
def test_l1_a_state_that_is_not_one_token_is_bad_answer_on_one_line(world, monkeypatch, capsys, state):
    # One recipient gets exactly one `wake:` line, so `state-<state>` must stay one token: a
    # state that is not `[A-Za-z0-9_-]+` cannot be spelled there and is `bad-answer` instead.
    world.sidecar.raw["demo-pm-t1"] = (200, json.dumps({"state": state}).encode("utf-8"))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken bad-answer\n"
    assert world.sidecar.pokes() == []


def test_c16_a_local_poke_timeout_is_unknown_and_is_not_retried(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "POKE_TIMEOUT", 0.3)
    world.sidecar.poke_delay = 2.0
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"
    assert len(world.sidecar.pokes()) == 1


def test_c18_exactly_one_wake_line_per_recipient_in_the_wake_grammar(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1", state="idle")
    agent(world, "demo-arch-t1", state="running")
    agent(world, "demo-rev-t1-1", state="exited")
    world.sidecar.raw["demo-gone-t1"] = (500, b"{}")
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1", "--interrupt",
             "--mention", "demo-rev-t1-1", "--mention", "demo-gone-t1")
    lines = [line for line in r.err.splitlines() if line.startswith("wake: ")]
    assert len(lines) == len(r.err.splitlines()) == 4
    # The order is the one the addresses were given in: `--to` first, then `--mention` in
    # order (`candidates` in `_cmd_send`), the same order `angareion send` gets them in.
    assert [line.split(" ")[1] for line in lines] == [
        "demo-pm-t1", "demo-arch-t1", "demo-rev-t1-1", "demo-gone-t1",
    ]
    for line in lines:
        assert WAKE_406_RE.fullmatch(line), line


def test_c19_require_wake_is_0_when_everybody_is_woken(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, "--require-wake")
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken local\n"


def test_c19_require_wake_is_4_when_somebody_is_not_woken(world, monkeypatch, capsys):
    agent(world, state="exited")
    r = send(world, monkeypatch, capsys, "--require-wake")
    assert r.code == 4
    assert r.err == "wake: demo-pm-t1 not-woken state-exited\n"
    # the message went in before the wake was tried: exit 4 means do not resend
    assert r.out.splitlines()[0] == FAKE_ID and len(world.calls()) == 1


def test_c19_require_wake_is_4_when_a_result_is_unknown(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "POKE_TIMEOUT", 0.3)
    world.sidecar.poke_delay = 2.0
    r = send(world, monkeypatch, capsys, "--require-wake")
    assert r.code == 4
    assert r.out.splitlines()[0] == FAKE_ID


def test_c19_require_wake_is_4_when_the_first_of_two_recipients_is_not_woken(world, monkeypatch, capsys):
    # "any of them": the answer must not depend on which recipient happens to be the last one.
    agent(world, "demo-pm-t1", state="exited")
    agent(world, "demo-arch-t1", state="idle")
    r = send(world, monkeypatch, capsys, "--require-wake", "--mention", "demo-arch-t1")
    assert r.code == 4
    assert r.err == (
        "wake: demo-pm-t1 not-woken state-exited\n"
        "wake: demo-arch-t1 woken local\n"
    )
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]
    assert r.out.splitlines()[0] == FAKE_ID and len(world.calls()) == 1


def test_c19_require_wake_is_4_when_the_second_of_two_recipients_is_not_woken(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1", state="idle")
    agent(world, "demo-arch-t1", state="exited")
    r = send(world, monkeypatch, capsys, "--require-wake", "--mention", "demo-arch-t1")
    assert r.code == 4
    assert r.err == (
        "wake: demo-pm-t1 woken local\n"
        "wake: demo-arch-t1 not-woken state-exited\n"
    )
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]
    assert r.out.splitlines()[0] == FAKE_ID and len(world.calls()) == 1


def test_c19_require_wake_is_0_when_both_recipients_are_woken(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1", state="idle")
    agent(world, "demo-arch-t1", state="idle")
    r = send(world, monkeypatch, capsys, "--require-wake", "--mention", "demo-arch-t1")
    assert r.code == 0
    assert r.err == (
        "wake: demo-pm-t1 woken local\n"
        "wake: demo-arch-t1 woken local\n"
    )
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}] * 2
    assert r.out.splitlines()[0] == FAKE_ID and len(world.calls()) == 1


def test_c19_require_wake_is_0_when_nobody_is_to_be_woken(world, monkeypatch, capsys):
    broadcast = send(world, monkeypatch, capsys, "--require-wake", to=None)
    myself = send(world, monkeypatch, capsys, "--require-wake", to=world.sender)
    assert (broadcast.code, myself.code) == (0, 0)
    assert world.sidecar.requests == []


def test_c19_require_wake_with_no_wake_is_refused_before_anything_is_written(world, monkeypatch, capsys):
    agent(world)
    r = send(world, monkeypatch, capsys, "--require-wake", "--no-wake")
    assert r.code == 1
    assert r.err.strip()
    assert world.calls() == [] and world.sidecar.requests == []


@pytest.mark.parametrize("code", ["1", "2"])
def test_c20_a_failed_write_keeps_its_exit_code_and_touches_nothing(world, monkeypatch, capsys, code):
    agent(world)
    world.setenv("FAKE_ANG_SEND_EXIT", code)
    r = send(world, monkeypatch, capsys, "--require-wake")
    assert r.code == int(code)
    assert len(world.calls()) == 1
    assert world.sidecar.requests == []


@pytest.mark.parametrize("extra,to", [(("--no-wake",), "demo-pm-t1"), ((), None)], ids=["no-wake", "broadcast"])
def test_c22_nobody_is_looked_up_and_the_message_is_written_once(world, monkeypatch, capsys, extra, to):
    agent(world)
    r = send(world, monkeypatch, capsys, *extra, to=to)
    assert r.code == 0
    assert r.err == ""
    assert world.sidecar.requests == []
    assert len(world.calls()) == 1


@pytest.mark.parametrize(
    "answer",
    [b"not json", b"[]", b'"idle"', b"{}", b'{"state": 3}', b'{"state": null}', b'{"id": "x"}'],
    ids=["text", "list", "string", "no-state", "int-state", "null-state", "other-field"],
)
def test_c24_a_local_answer_that_cannot_be_read_is_bad_answer(world, monkeypatch, capsys, answer):
    world.sidecar.raw["demo-pm-t1"] = (200, answer)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken bad-answer\n"
    assert world.sidecar.pokes() == []


@pytest.mark.parametrize("body,code", [
    (b'{"code": "agent_awaiting_answer"}', "awaiting-answer"),
    (b'{"error": "agent_awaiting_answer"}', "awaiting-answer"),
    (b'{"code": "agent_not_running"}', "agent-not-running"),
    (b'{"error": "agent_not_running"}', "agent-not-running"),
    (b'{"code": "something_else"}', "http-409"),
    (b'{"code": 7, "error": "agent_not_running"}', "agent-not-running"),
], ids=["code-awaiting", "error-awaiting", "code-not-running", "error-not-running",
        "unknown-code", "code-not-a-string"])
def test_c25_a_local_409_is_read_for_its_reason_and_poked_once(world, monkeypatch, capsys, body, code):
    agent(world)
    world.sidecar.poke_status = 409
    world.sidecar.poke_body = body
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"
    assert len(world.sidecar.pokes()) == 1


def _broken_poke(error: BaseException):
    """A `_request` that answers the lookup for real and fails every poke with `error`."""
    from aicomm import cli

    original = cli._request

    def request(method, url, payload, timeout):
        if method == "POST":
            raise error
        return original(method, url, payload, timeout)

    return request


def test_c26_a_local_poke_whose_connection_is_reset_is_unknown_poke_error(world, monkeypatch, capsys):
    agent(world)
    monkeypatch.setattr("aicomm.cli._request", _broken_poke(ConnectionResetError("reset by peer")))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown poke-error\n"
    assert world.sidecar.pokes() == []
    assert [q["method"] for q in world.sidecar.requests] == ["GET"]


def test_c31_a_local_lookup_whose_http_layer_cannot_be_read_is_bad_answer(world, monkeypatch, capsys):
    agent(world)

    def broken(method, url, payload, timeout):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr("aicomm.cli._request", broken)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken bad-answer\n"
    assert world.sidecar.pokes() == []


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"code": 5}', b'{"code": null, "error": 7}',
                                 b'{"code": null}'],
                         ids=["text", "list", "empty", "int-code", "two-non-strings", "null-code"])
def test_c32_a_409_without_a_usable_error_code_is_http_409(world, monkeypatch, capsys, body):
    agent(world)
    world.sidecar.poke_status = 409
    world.sidecar.poke_body = body
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken http-409\n"
    assert len(world.sidecar.pokes()) == 1


@pytest.mark.parametrize("status", [201, 204])
def test_c33_another_2xx_is_reported_as_http_code(world, monkeypatch, capsys, status):
    agent(world)
    world.sidecar.poke_status = status
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 not-woken http-{status}\n"
    assert len(world.sidecar.pokes()) == 1


def test_c37_a_local_poke_that_is_refused_is_unknown_poke_error(world, monkeypatch, capsys):
    agent(world)
    monkeypatch.setattr("aicomm.cli._request", _broken_poke(ConnectionRefusedError("refused")))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown poke-error\n"
    assert world.sidecar.pokes() == []
    assert [q["method"] for q in world.sidecar.requests] == ["GET"]


def test_c30_the_reason_code_constants_are_exactly_the_spec_list():
    from aicomm import cli

    assert cli.NOT_WOKEN_CODES == {
        "not-found", "peer-unavailable", "lookup-incomplete", "peer-port-out-of-range",
        "peer-bad-answer", "bad-answer", "bad-name", "agent-not-running", "awaiting-answer",
        "state-", "http-",   # prefixes: `state-<state>`, `http-<code>`
    }
    assert cli.UNKNOWN_CODES == {"poke-timeout", "poke-error", "peer-bad-answer", "internal-error"}


def test_an_unexpected_error_in_the_wake_path_is_unknown_internal_error(world, monkeypatch, capsys):
    agent(world)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("aicomm.cli._wake_one_inner", boom)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown internal-error\n"


# --- #457: the cross-machine fallback through the sidecar-link ports -------------------------
# C-n below is the contract list of the `aicomm-direct-peer` change (design.md §3.9). The
# local sidecar is asked first; only a 404, an unreachable sidecar or a lookup timeout sends
# aicomm to the sidecar-link directory. `aicomm.sidecar_link` reads its two files (pure unit
# tests first), and every listed port is then queried and poked directly over loopback HTTP
# (FakeSidecar): no external command runs and no other machine is reached.


def woken(*ports):
    """The woken code for these peer ports, ascending, as the `wake:` line spells it."""
    return "peer:" + ",".join(str(port) for port in sorted(ports))


def peer_records(*pairs):
    """The `record=` entries for (peer, id) pairs, ascending by port."""
    return ",".join(f"peer:{peer.port}/{record}"
                    for peer, record in sorted(pairs, key=lambda pair: pair[0].port))


def free_port():
    """A loopback port nobody listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def dead_sidecar(world):
    """Point aicomm at a loopback port nobody listens on (the local sidecar is unreachable)."""
    world.setenv("AICOMM_SIDECAR", f"http://127.0.0.1:{free_port()}")


def stub_ports(monkeypatch, *ports):
    """Make the sidecar-link reader hand cli exactly these ports, in this order."""
    monkeypatch.setattr(sidecar_link, "read",
                        lambda **kwargs: sidecar_link.Ports(tuple(ports)))


def link_read(directory, *, local_port=8471, now=None, pid_alive=None, env=None, home=None):
    """Read one link directory, with the clock and the pid check under the test's control."""
    if env is None:
        env = {"AICOMM_SIDECAR_LINK": str(directory)}
    return sidecar_link.read(
        local_port=local_port,
        env=env,
        home=home if home is not None else Path.home,
        now=now if now is not None else time.time,
        pid_alive=pid_alive if pid_alive is not None else (lambda pid: True),
    )


def link_read_at(directory, offset, **kwargs):
    """Read with the clock pinned `offset` seconds after this heartbeat's mtime."""
    mtime = (directory / "heartbeat").stat().st_mtime
    return link_read(directory, now=lambda: mtime + offset, **kwargs)


# --- C-1..C-9: the sidecar-link directory on its own (no HTTP, injected clock and pid) ------


def test_c1_a_missing_heartbeat_is_unavailable(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    link.no_beat()
    assert link_read(link.root) == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)


def test_c1_a_heartbeat_that_is_not_a_regular_file_is_unavailable(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    link.no_beat()
    link.heartbeat.mkdir()  # a directory: readable as a path, not as a heartbeat
    assert link_read(link.root) == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)


def test_c2_the_window_is_the_ninety_seconds_the_interface_names():
    # The numbers below are literal on purpose: a boundary test that read the constant
    # would move with it and catch nothing. This pin is what keeps the two in step.
    assert sidecar_link.LINK_FRESH_SECONDS == 90


@pytest.mark.parametrize("offset,fresh", [
    (-91, False), (-90, True), (-89, True),
    (89, True), (90, True), (91, False), (300, False),
], ids=["future-over", "future-edge", "future-inside",
        "old-inside", "old-edge", "old-over", "old-far"])
def test_c2_the_freshness_window_is_symmetrical(tmp_path, offset, fresh):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    got = link_read_at(link.root, offset)
    assert isinstance(got, sidecar_link.Ports) is fresh
    if fresh:
        assert got == sidecar_link.Ports((5201,))
    else:
        assert got == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)


@pytest.mark.parametrize("content", ["0", "-1", "12 34", "", "abc", "01", "2147483648"],
                         ids=["zero", "negative", "two-numbers", "empty", "words", "leading-zero",
                              "too-big"])
def test_c3_a_bad_pid_is_unavailable_and_kill_is_never_called(tmp_path, content):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    link.beat(content)
    asked = []
    got = link_read(link.root, pid_alive=lambda pid: asked.append(pid) or True)
    assert got == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)
    assert asked == []  # 0 or 2147483648 must never reach os.kill


def test_c3_a_padded_positive_pid_is_accepted(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    link.beat(" 4242 \n")
    asked = []
    got = link_read(link.root, pid_alive=lambda pid: asked.append(pid) or True)
    assert got == sidecar_link.Ports((5201,))
    assert asked == [4242]


@pytest.mark.parametrize("alive", [True, False], ids=["alive", "gone"])
def test_c4_the_injected_pid_check_decides_whether_the_heartbeat_is_fresh(tmp_path, alive):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    got = link_read(link.root, pid_alive=lambda pid: alive)
    assert isinstance(got, sidecar_link.Ports) is alive


@pytest.mark.parametrize("error,alive", [
    (ProcessLookupError(), False),
    (PermissionError(), True),
    (OSError(), False),
], ids=["gone", "no-permission", "other-os-error"])
def test_c4_the_default_pid_check_reads_the_os_errors(monkeypatch, error, alive):
    def kill(pid, signal):
        assert pid == 4242 and signal == 0
        raise error

    monkeypatch.setattr(sidecar_link.os, "kill", kill)
    assert sidecar_link._pid_alive(4242) is alive


def test_c5_a_missing_ports_file_is_unavailable(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    link.no_ports()
    assert link_read(link.root) == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)


@pytest.mark.parametrize("data,kind", [
    (b"\xff\xfe not utf-8", sidecar_link.BAD_FORMAT),
    (b"5201\n\n5202\n", sidecar_link.BAD_FORMAT),
    (b"52x1\n", sidecar_link.BAD_FORMAT),
    (b"123456\n", sidecar_link.BAD_FORMAT),  # six digits: not a port line at all
    (b" 5201\n", sidecar_link.BAD_FORMAT),
    (b"5201 5202\n", sidecar_link.BAD_FORMAT),
    (b"\n", sidecar_link.UNAVAILABLE),  # one trailing newline, nothing else: empty list
    (b"", sidecar_link.UNAVAILABLE),
], ids=["not-utf8", "blank-line", "words", "six-digits", "leading-space", "two-on-a-line",
        "only-newline", "empty"])
def test_c6_a_ports_file_that_cannot_be_read(tmp_path, data, kind):
    link = LinkDir(tmp_path / "link")
    link.fresh()
    link.ports_bytes(data)
    assert link_read(link.root) == sidecar_link.Unusable(kind)


@pytest.mark.parametrize("data,kind", [
    (b"0\n", sidecar_link.OUT_OF_RANGE),
    (b"65536\n", sidecar_link.OUT_OF_RANGE),
    (b"70000\n", sidecar_link.OUT_OF_RANGE),
    (b"99999\n", sidecar_link.OUT_OF_RANGE),
], ids=["zero", "65536", "70000", "99999"])
def test_c7_a_port_outside_one_to_65535_is_out_of_range(tmp_path, data, kind):
    link = LinkDir(tmp_path / "link")
    link.fresh()
    link.ports_bytes(data)
    assert link_read(link.root) == sidecar_link.Unusable(kind)


def test_c7_the_edges_of_the_port_range_are_ports(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh()
    link.ports_text(f"{sidecar_link.PORT_MIN}\n{sidecar_link.PORT_MAX}\n")
    assert link_read(link.root) == sidecar_link.Ports(
        (sidecar_link.PORT_MIN, sidecar_link.PORT_MAX))


def test_c6_the_first_bad_line_decides_which_code(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh()
    link.ports_text("5201\n0\n52x1\n")  # the out-of-range line comes first
    assert link_read(link.root) == sidecar_link.Unusable(sidecar_link.OUT_OF_RANGE)
    link.ports_text("5201\n52x1\n0\n")  # the broken shape comes first
    assert link_read(link.root) == sidecar_link.Unusable(sidecar_link.BAD_FORMAT)


def test_c8_a_repeated_port_is_one_port_and_the_result_is_ascending(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh()
    link.ports_text("5300\n5201\n5300\n")
    assert link_read(link.root) == sidecar_link.Ports((5201, 5300))


def test_c8_the_callers_own_port_is_dropped(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201, 8471, 5300)
    assert link_read(link.root, local_port=8471) == sidecar_link.Ports((5201, 5300))


def test_c8_only_the_callers_own_port_is_unavailable(tmp_path):
    link = LinkDir(tmp_path / "link")
    link.fresh(8471)
    assert link_read(link.root, local_port=8471) == sidecar_link.Unusable(
        sidecar_link.UNAVAILABLE)


def test_c9_the_default_directory_is_under_home(tmp_path):
    home = tmp_path / "home"
    link = LinkDir(home / ".mylinuxpool" / "sidecar-link")
    link.fresh(5201)
    got = link_read(link.root, env={}, home=lambda: home)
    assert got == sidecar_link.Ports((5201,))


@pytest.mark.parametrize("value", ["", "link", "./link"], ids=["empty", "relative", "dotted"])
def test_c9_an_empty_or_relative_hook_is_no_directory(tmp_path, value):
    link = LinkDir(tmp_path / "link")
    link.fresh(5201)
    got = link_read(link.root, env={"AICOMM_SIDECAR_LINK": value})
    assert got == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)


@pytest.mark.parametrize("error", [RuntimeError("no HOME"), OSError("unreadable")],
                         ids=["runtime", "os"])
def test_c9_a_home_that_cannot_be_read_is_no_directory(tmp_path, error):
    def home():
        raise error

    got = link_read(tmp_path, env={}, home=home)
    assert got == sidecar_link.Unusable(sidecar_link.UNAVAILABLE)


# --- C-9..C-16: the cli's fallback against the fake link directory ---------------------------


def test_c9_an_empty_port_list_is_peer_unavailable(world, monkeypatch, capsys):
    # the fixture starts with a fresh heartbeat and no ports: nothing to try
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.out.splitlines()[0] == FAKE_ID
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"


@pytest.mark.parametrize("break_link", ["no-heartbeat", "stale", "future", "dead-pid", "zero-pid",
                                       "huge-pid", "words-pid", "no-ports", "empty-ports"],
                         ids=["no-heartbeat", "old-mtime", "future-mtime", "dead-pid", "pid-0",
                              "pid-2**31", "pid-words", "no-ports", "empty-ports"])
def test_c9_a_link_that_cannot_be_used_is_peer_unavailable(world, monkeypatch, capsys, break_link):
    peer = world.add_peer()
    if break_link == "no-heartbeat":
        world.link.no_beat()
    elif break_link == "stale":
        world.link.beat(age=91)  # just past the 90-second window the interface names
    elif break_link == "future":
        world.link.beat(age=-300)
    elif break_link == "dead-pid":
        world.link.beat(2147483647)  # passes the shape check, nothing answers os.kill
    elif break_link == "zero-pid":
        world.link.beat(0)
    elif break_link == "huge-pid":
        world.link.beat(2147483648)
    elif break_link == "words-pid":
        world.link.beat("not a pid")
    elif break_link == "no-ports":
        world.link.no_ports()
    else:
        world.link.ports_text("")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert peer.requests == []  # a link that is no use pokes nobody


@pytest.mark.parametrize("value", ["", "link"], ids=["empty", "relative"])
def test_c9_a_hook_that_is_not_an_absolute_path_is_peer_unavailable(world, monkeypatch, capsys, value):
    peer = world.add_peer()
    world.setenv("AICOMM_SIDECAR_LINK", value)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0  # not a usage error: it is read after the message was delivered
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert peer.requests == []


def test_c9_the_local_port_of_an_address_without_a_port_is_eighty():
    from aicomm import cli

    assert cli._local_port("http://127.0.0.1") == 80
    assert cli._local_port("http://localhost") == 80
    assert cli._local_port("http://127.0.0.1:8471") == 8471


def test_c10_every_port_says_404_is_not_found(world, monkeypatch, capsys):
    a = world.add_peer(name=None)
    b = world.add_peer(name=None)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken not-found\n"
    assert [q["method"] for q in a.requests] == ["GET"]
    assert [q["method"] for q in b.requests] == ["GET"]


def test_c10_one_port_that_cannot_be_asked_is_lookup_incomplete(world, monkeypatch, capsys):
    peer = world.add_peer(name=None)
    world.link.fresh(peer.port, free_port())  # the second port has no listener
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert peer.pokes() == []


def test_c10_a_200_whose_body_cannot_be_read_is_peer_bad_answer(world, monkeypatch, capsys):
    peer = world.add_peer()
    peer.raw["demo-pm-t1"] = (200, b"not json")
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert peer.pokes() == []


def test_c10_a_200_body_too_deep_to_read_is_peer_bad_answer(world, monkeypatch, capsys):
    peer = world.add_peer()
    peer.raw["demo-pm-t1"] = (200, DEEP_BODY)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert peer.pokes() == []


@pytest.mark.parametrize("status", [500, 403, 201, 204], ids=["500", "403", "201", "204"])
def test_c10_another_status_is_as_good_as_no_answer(world, monkeypatch, capsys, status):
    peer = world.add_peer()
    peer.raw["demo-pm-t1"] = (status, b"{}")
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert peer.pokes() == []


def test_c10_one_port_is_poked_and_the_other_answers_404(world, monkeypatch, capsys):
    absent = world.add_peer(name=None)
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"
    assert absent.pokes() == []


def test_c8_one_peer_is_poked_with_the_local_pokes_body(world, monkeypatch, capsys):
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"
    assert peer.pokes() == [
        {"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer", "ref": FAKE_ID}]
    assert world.sidecar.pokes() == []  # only the peer sidecar was poked


def test_c10_the_poke_path_quotes_the_name(world, monkeypatch, capsys):
    peer = world.add_peer(name="demo pm/1")
    r = send(world, monkeypatch, capsys, to="demo pm/1")
    assert r.code == 0
    quoted = urllib.parse.quote("demo pm/1", safe="")
    assert [(q["method"], q["path"]) for q in peer.requests] == [
        ("GET", f"/agents/{quoted}"), ("POST", f"/agents/{quoted}/poke")]


def test_c15_each_recipient_is_looked_up_on_its_own(world, monkeypatch, capsys):
    a = world.add_peer(name="demo-a-t1")
    b = world.add_peer(name="demo-b-t1")
    r = send(world, monkeypatch, capsys, "--mention", "demo-b-t1", to="demo-a-t1")
    assert r.err == (
        f"wake: demo-a-t1 woken {woken(a.port)}\n"
        f"wake: demo-b-t1 woken {woken(b.port)}\n"
    )
    # every port is asked about every recipient; only the one that has it is poked
    assert [q["path"] for q in a.requests if q["method"] == "POST"] == ["/agents/demo-a-t1/poke"]
    assert [q["path"] for q in b.requests if q["method"] == "POST"] == ["/agents/demo-b-t1/poke"]
    for peer in (a, b):
        assert [q["method"] for q in peer.requests].count("GET") == 2  # one per recipient
        assert [q["method"] for q in peer.requests].count("POST") == 1


def test_c15_every_recipient_rereads_the_link(world, monkeypatch, capsys):
    first = world.add_peer(name="demo-a-t1")
    second = world.add_peer(name="demo-b-t1")
    world.link.fresh(first.port)
    real_read = sidecar_link.read
    seen = []

    def read(**kwargs):
        result = real_read(**kwargs)
        seen.append(result)
        world.link.fresh(second.port)  # the file changes between the two recipients
        return result

    monkeypatch.setattr(sidecar_link, "read", read)
    r = send(world, monkeypatch, capsys, "--mention", "demo-b-t1", to="demo-a-t1")
    assert r.err == (
        f"wake: demo-a-t1 woken {woken(first.port)}\n"
        f"wake: demo-b-t1 woken {woken(second.port)}\n"
    )
    assert len(seen) == 2  # once per recipient, no cache
    assert second.pokes() == [
        {"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer", "ref": FAKE_ID}]


def test_c15_a_sidecar_that_cannot_be_reached_still_looks_elsewhere(world, monkeypatch, capsys):
    dead_sidecar(world)
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"
    assert [q["method"] for q in peer.requests] == ["GET", "POST"]


@pytest.mark.parametrize("local", ["idle", "running", "exited", "500", "broken"],
                         ids=["idle", "running", "exited", "http-500", "bad-answer"])
def test_c16_a_local_answer_never_asks_another_sidecar(world, monkeypatch, capsys, local):
    if local in ("idle", "running", "exited"):
        agent(world, state=local)
    else:
        world.sidecar.raw["demo-pm-t1"] = (500, b"{}") if local == "500" else (200, b"not json")
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert peer.requests == []


def test_c16_a_local_poke_timeout_never_asks_another_sidecar(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "POKE_TIMEOUT", 0.3)
    world.sidecar.poke_delay = 2.0
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"
    assert peer.requests == []


def test_c16_every_peer_sees_only_the_two_routes(world, monkeypatch, capsys):
    a = world.add_peer()
    b = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(a.port, b.port)}\n"
    for peer in (a, b):
        assert [(q["method"], q["path"]) for q in peer.requests] == [
            ("GET", "/agents/demo-pm-t1"), ("POST", "/agents/demo-pm-t1/poke")]
        assert all("/pokes/" not in q["path"] for q in peer.requests)


def test_c16_a_pool_peer_on_path_is_never_run(world, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "pool-peer-ran"
    bin_dir = tmp_path / "old-bin"
    bin_dir.mkdir()
    script = bin_dir / "pool-peer"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    world.link.no_beat()  # "cannot look": no reason to fall back to anything else either
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert not marker.exists()


def test_c9_the_same_port_listed_twice_is_looked_up_once(world, monkeypatch, capsys):
    peer = world.add_peer()
    world.link.ports_text(f"{peer.port}\n{peer.port}\n")
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"
    assert [q["method"] for q in peer.requests] == ["GET", "POST"]


def test_c8_the_local_sidecar_port_is_never_asked_again(world, monkeypatch, capsys):
    peer = world.add_peer()
    world.link.fresh(world.sidecar.port, peer.port)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"
    assert [q["path"] for q in world.sidecar.requests] == ["/agents/demo-pm-t1"]


def test_c8_a_list_with_only_the_local_port_is_peer_unavailable(world, monkeypatch, capsys):
    world.link.fresh(world.sidecar.port)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"


@pytest.mark.parametrize("data,code", [
    ("0\n", "peer-port-out-of-range"),
    ("65536\n", "peer-port-out-of-range"),
    ("123456\n", "peer-bad-answer"),  # six digits: a broken line, not a port
    ("52x1\n", "peer-bad-answer"),
    ("5201\n\n", "peer-bad-answer"),
], ids=["zero", "65536", "six-digits", "words", "blank-line"])
def test_c7_a_bad_port_line_is_reported_before_any_poke(world, monkeypatch, capsys, data, code):
    peer = world.add_peer()
    world.link.ports_text(data)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"
    assert peer.requests == []  # the whole list is dropped, not just the bad line


@pytest.mark.parametrize("state", ["starting", "waiting-quota", "exited"])
def test_c12_a_peer_in_another_state_is_not_poked(world, monkeypatch, capsys, state):
    peer = world.add_peer(state=state)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken state-{state}\n"
    assert [q["method"] for q in peer.requests] == ["GET"]


def test_c12_a_peer_state_that_is_not_one_token_is_peer_bad_answer(world, monkeypatch, capsys):
    peer = world.add_peer()
    peer.agents["demo-pm-t1"]["state"] = "idle\nwake: demo-pm-t1 woken local"
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert [q["method"] for q in peer.requests] == ["GET"]


def test_c12_the_peers_own_interrupt_is_decided_per_sidecar(world, monkeypatch, capsys):
    running = world.add_peer(state="running")
    idle = world.add_peer(state="idle")
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.err == f"wake: demo-pm-t1 woken {woken(running.port, idle.port)} interrupt\n"
    bodies = {peer.port: peer.pokes()[0] for peer in (running, idle)}
    assert bodies == {
        running.port: {"text": WAKE_TEXT, "interrupt": True, "via": "aicomm-peer", "ref": FAKE_ID},
        idle.port: {"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer", "ref": FAKE_ID},
    }


def test_c12_a_peer_that_did_not_wake_does_not_add_interrupt(world, monkeypatch, capsys):
    failed = world.add_peer(state="running")
    failed.poke_status = 500
    won = world.add_peer(state="idle")
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.err == f"wake: demo-pm-t1 woken {woken(won.port)}\n"


def test_c13_every_pokeable_peer_is_poked_and_only_the_woken_ones_are_listed(world, monkeypatch, capsys):
    failed = world.add_peer()
    failed.poke_status = 500
    won = world.add_peer()
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(won.port)}\n"
    assert [q["method"] for q in failed.requests] == ["GET", "POST"]  # both were tried
    assert [q["method"] for q in won.requests] == ["GET", "POST"]


def test_c13_the_woken_ports_are_listed_from_the_smallest(world, monkeypatch, capsys):
    a = world.add_peer()
    b = world.add_peer()
    world.link.ports_list(b.port, a.port)  # file order is not the result order
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(a.port, b.port)}\n"


@pytest.mark.parametrize("status,body,code", [
    (409, '{"code": "agent_awaiting_answer"}', "awaiting-answer"),
    (409, '{"error": "agent_awaiting_answer"}', "awaiting-answer"),
    (409, '{"code": "agent_not_running"}', "agent-not-running"),
    (409, '{"code": "something_else"}', "http-409"),
    (409, "not json", "http-409"),
    (500, "", "http-500"),
    (201, "", "http-201"),
], ids=["code-awaiting", "error-awaiting", "code-not-running", "unknown-code",
        "unreadable-409", "http-500", "http-201"])
def test_c12_a_poke_answer_is_read_with_the_local_pokes_table(world, monkeypatch, capsys,
                                                             status, body, code):
    peer = world.add_peer()
    peer.poke_status = status
    peer.poke_body = body.encode("utf-8")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"
    assert len(peer.pokes()) == 1  # one poke, no resend


def break_poke(monkeypatch, error):
    """Make the peer poke raise `error`; every other request goes through as usual."""
    from aicomm import cli

    real = cli._request

    def request(method, url, payload, timeout):
        if method == "POST":
            raise error
        return real(method, url, payload, timeout)

    monkeypatch.setattr(cli, "_request", request)


@pytest.mark.parametrize("error,kind,code", [
    (ConnectionRefusedError("refused"), "not-woken", "peer-unavailable"),
    (urllib.error.URLError(ConnectionRefusedError("refused")), "not-woken", "peer-unavailable"),
    (ConnectionResetError("reset"), "unknown", "poke-error"),
    (socket.timeout("slow"), "unknown", "poke-timeout"),
    (http.client.BadStatusLine("broken"), "unknown", "peer-bad-answer"),
], ids=["refused", "refused-in-urlerror", "reset", "timeout", "bad-status-line"])
def test_c11_the_pokes_transport_result(world, monkeypatch, capsys, error, kind, code):
    peer = world.add_peer()
    break_poke(monkeypatch, error)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 {kind} {code}\n"
    assert [q["method"] for q in peer.requests] == ["GET"]  # one attempt, no resend


def test_c11_a_poke_that_cannot_connect_is_peer_unavailable(world, monkeypatch, capsys):
    from aicomm import cli

    peer = world.add_peer()
    real = cli._request

    def request(method, url, payload, timeout):
        if method == "POST":
            raise ConnectionRefusedError("refused")
        return real(method, url, payload, timeout)

    monkeypatch.setattr(cli, "_request", request)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert len(peer.requests) == 1


def test_c11_a_peer_that_closes_after_reading_the_poke_is_peer_bad_answer(
        world, monkeypatch, capsys):
    # The other real reset (#457 review 低-4): the peer answers the lookup with a normal
    # 200, then reads the poke body and closes with no byte back. urllib raises
    # `RemoteDisconnected`, which is both an `HTTPException` and a `ConnectionResetError`;
    # the `HTTPException` branch wins, so this is `unknown peer-bad-answer`. An injected
    # `ConnectionResetError` (above) is the case where the socket is reset instead.
    body = json.dumps({"state": "idle"}).encode("utf-8")
    answer = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
              + str(len(body)).encode() + b"\r\n\r\n" + body)
    peer = RawSidecar([answer, b""])
    try:
        world.link.fresh(peer.port)
        r = send(world, monkeypatch, capsys)
    finally:
        peer.close()
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown peer-bad-answer\n"  # never "not-woken", never a resend


def test_c14_a_stuck_port_uses_up_the_budget_and_the_rest_are_unasked(world, monkeypatch, capsys):
    from aicomm import cli

    stuck = world.add_peer(name=None)
    stuck.get_delay = 2.0
    later = world.add_peer()
    stub_ports(monkeypatch, stuck.port, later.port)
    monkeypatch.setattr(cli, "PEER_GET_TIMEOUT", 3.0)
    monkeypatch.setattr(cli, "PEER_LOOKUP_BUDGET", 0.5)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert later.requests == []  # the budget ran out before this one was asked
    assert stuck.pokes() == []


def test_c14_each_request_is_capped_by_what_is_left_of_the_budget(world, monkeypatch, capsys):
    from aicomm import cli

    stuck = world.add_peer()
    stuck.get_delay = 3.0
    stub_ports(monkeypatch, stuck.port)
    monkeypatch.setattr(cli, "PEER_GET_TIMEOUT", 3.0)
    monkeypatch.setattr(cli, "PEER_LOOKUP_BUDGET", 0.4)
    started = time.monotonic()
    r = send(world, monkeypatch, capsys)
    elapsed = time.monotonic() - started
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert elapsed < 1.5  # cut at the remaining budget, not at the 3 s per-request cap


def test_c14_a_slow_port_that_answers_in_time_is_used(world, monkeypatch, capsys):
    from aicomm import cli

    peer = world.add_peer()
    peer.get_delay = 0.2
    monkeypatch.setattr(cli, "PEER_GET_TIMEOUT", 1.0)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"


def test_c14_the_budget_and_timeouts_are_the_numbers_the_spec_names():
    from aicomm import cli

    assert (cli.PEER_LOOKUP_BUDGET, cli.PEER_GET_TIMEOUT, cli.PEER_POKE_TIMEOUT) == (
        10.0, 3.0, 5.0)


class FakeClock:
    """A monotonic clock the test moves by hand, so no test ever sleeps."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_c14_each_port_gets_the_smaller_of_the_cap_and_the_budget_left(
        world, monkeypatch, capsys):
    from aicomm import cli

    ports = (50001, 50002, 50003)
    stub_ports(monkeypatch, *ports)
    clock = FakeClock()
    monkeypatch.setattr(cli.time, "monotonic", clock)
    seen = []
    peers = {f"127.0.0.1:{port}" for port in ports}

    def request(method, url, payload, timeout):
        if url.split("/")[2] in peers:
            seen.append(timeout)
            clock.value += 8.0  # each answer eats eight of the ten seconds
        return 404, b""

    monkeypatch.setattr(cli, "_request", request)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    # 10 s left -> 3 s; 2 s left -> 2 s; then the budget is gone and the third port is unasked
    assert seen == [3.0, 2.0]


def test_c14_a_wall_clock_jump_does_not_move_the_budget(world, monkeypatch, capsys):
    from aicomm import cli

    ports = (50001, 50002, 50003)
    stub_ports(monkeypatch, *ports)
    clock = FakeClock()
    monkeypatch.setattr(cli.time, "monotonic", clock)  # the budget's clock
    wall = FakeClock()
    monkeypatch.setattr(cli.time, "time", wall)  # the wall clock, which now runs backwards
    seen = []
    peers = {f"127.0.0.1:{port}" for port in ports}

    def request(method, url, payload, timeout):
        if url.split("/")[2] in peers:
            seen.append(timeout)
            clock.value += 8.0
            wall.value -= 10_000.0
        return 404, b""

    monkeypatch.setattr(cli, "_request", request)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert seen == [3.0, 2.0]  # the wall clock jumped back; the budget did not reset


def test_c14_a_peer_poke_uses_the_poke_timeout(world, monkeypatch, capsys):
    from aicomm import cli

    peer = world.add_peer()
    seen = []
    real = cli._request

    def request(method, url, payload, timeout):
        if f":{peer.port}/" in url:
            seen.append((method, timeout))
        return real(method, url, payload, timeout)

    monkeypatch.setattr(cli, "_request", request)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"
    assert seen == [("GET", 3.0), ("POST", 5.0)]


def test_c13_an_unknown_from_one_peer_beats_a_not_woken_from_another(world, monkeypatch, capsys):
    from aicomm import cli

    reset = world.add_peer()
    awaiting = world.add_peer()
    awaiting.poke_status = 409
    awaiting.poke_body = b'{"code": "agent_awaiting_answer"}'
    real = cli._request

    def request(method, url, payload, timeout):
        if method == "POST" and f":{reset.port}/" in url:
            raise ConnectionResetError("reset")
        return real(method, url, payload, timeout)

    monkeypatch.setattr(cli, "_request", request)
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown poke-error\n"


def test_c13_two_unknowns_take_the_lowest_ports_reason(world, monkeypatch, capsys):
    first = world.add_peer()
    second = world.add_peer()
    from aicomm import cli

    real = cli._request

    def request(method, url, payload, timeout):
        if method == "POST":
            if f":{first.port}/" in url:
                raise http.client.BadStatusLine("broken")
            raise socket.timeout("slow")
        return real(method, url, payload, timeout)

    monkeypatch.setattr(cli, "_request", request)
    r = send(world, monkeypatch, capsys)
    low = min(first.port, second.port)
    expected = "peer-bad-answer" if low == first.port else "poke-timeout"
    assert r.err == f"wake: demo-pm-t1 unknown {expected}\n"


def test_c13_two_not_woken_take_the_lowest_ports_reason(world, monkeypatch, capsys):
    exited = world.add_peer(state="exited")
    awaiting = world.add_peer(state="idle")
    awaiting.poke_status = 409
    awaiting.poke_body = b'{"code": "agent_awaiting_answer"}'
    r = send(world, monkeypatch, capsys)
    low = min(exited.port, awaiting.port)
    expected = "state-exited" if low == exited.port else "awaiting-answer"
    assert r.err == f"wake: demo-pm-t1 not-woken {expected}\n"


def test_c18_a_peer_result_is_still_one_line_per_recipient(world, monkeypatch, capsys):
    agent(world, "demo-arch-t1", state="exited")
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1")
    assert r.code == 0
    lines = r.err.splitlines()
    assert lines == [
        f"wake: demo-pm-t1 woken {woken(peer.port)}",
        "wake: demo-arch-t1 not-woken state-exited",
    ]
    for line in lines:
        assert WAKE_406_RE.fullmatch(line), line


@pytest.mark.parametrize("state,expected", [
    ("unreadable", "peer-bad-answer"),
    ("http-500", "http-500"),
], ids=["unreadable-200", "poke-500"])
def test_c18_a_peer_answer_never_reaches_the_wake_line(world, monkeypatch, capsys, state, expected):
    peer = world.add_peer()
    if state == "unreadable":
        peer.raw["demo-pm-t1"] = (200, b'SECRET-DIAGNOSTIC not json')
        want = f"wake: demo-pm-t1 not-woken {expected}\n"
    else:
        peer.poke_status = 500
        peer.poke_body = b'{"error": "SECRET-DIAGNOSTIC"}'
        want = f"wake: demo-pm-t1 not-woken {expected}\n"
    r = send(world, monkeypatch, capsys)
    assert r.err == want
    assert "SECRET" not in r.err and "SECRET" not in r.out


def test_c21_a_name_starting_with_a_dash_is_never_given_to_a_sidecar(world, monkeypatch, capsys):
    peer = world.add_peer(name="-demo-pm-t1")
    # `--to=-name` so argparse takes it as the value and not as an option
    r = run_aicomm(["send", "--channel", "team-demo", "--to=-demo-pm-t1", "--body", BODY],
                   monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: -demo-pm-t1 not-woken bad-name\n"
    assert peer.requests == []


@pytest.mark.parametrize("extra,to", [(("--no-wake",), "demo-pm-t1"), ((), None)],
                         ids=["no-wake", "broadcast"])
def test_c22_nobody_is_looked_up_when_waking_is_off(world, monkeypatch, capsys, extra, to):
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys, *extra, to=to)
    assert r.code == 0
    assert peer.requests == []


def test_a2_a_ref_without_equals_asks_nobody_anywhere(world, monkeypatch, capsys):
    peer = world.add_peer()
    r = send(world, monkeypatch, capsys, "--ref", "noequals")
    assert r.code == 1
    assert world.sidecar.requests == [] and peer.requests == []


def test_a3_log_never_asks_a_sidecar(world, monkeypatch, capsys):
    peer = world.add_peer()
    r = run_aicomm(["log"], monkeypatch, capsys)
    assert r.code == 0
    assert peer.requests == [] and world.sidecar.requests == []


# --- #406: the wake outcome (`queued`) and the sidecar record id ---------------------------
# C-n below refers to the contract list in the `aicomm-wake-outcome` change (design.md §3).
# The local lines come from the fake sidecar; the cross-machine ones from a peer FakeSidecar.
# The fake sidecar answers a plain `{}` by default, so every test above keeps
# its exact old line (no `record=`, no ` queued`).

RECORD_A = "01JX000000000000000000000A"
RECORD_B = "pk-01JX00000000000000000000B"
RECORD_C = "01JX000000000000000000000C"

# D-1's closed grammar (M-1), for `re.fullmatch` (a `$` would eat a trailing newline).
WAKE_406_RE = re.compile(
    r"^wake: \S+ (woken (local|peer:\d+(,\d+)*)( interrupt| queued)?|"
    r"(not-woken|unknown) [A-Za-z0-9_-]+)"
    r"( record=(local|peer:\d+)/[A-Za-z0-9_-]{1,64}"
    r"(,(local|peer:\d+)/[A-Za-z0-9_-]{1,64})*)?$"
)


def poke_answer(**fields) -> bytes:
    """A fake poke body with these top-level fields."""
    return json.dumps(fields).encode("utf-8")


def local_poke(world, status=200, **fields) -> None:
    world.sidecar.poke_status = status
    world.sidecar.poke_body = poke_answer(**fields)


# --- C-1: a local 200 with `sent` and an id ------------------------------------------------


def test_406_c1_a_sent_local_poke_carries_its_record_id(world, monkeypatch, capsys):
    agent(world, state="idle")
    local_poke(world, poke_id=RECORD_A, poke_outcome="sent")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 woken local record=local/{RECORD_A}\n"
    assert len(world.sidecar.pokes()) == 1


# --- C-2: a local 200 with `queued` -------------------------------------------------------


@pytest.mark.parametrize("extra", [(), ("--require-wake",)], ids=["plain", "require-wake"])
def test_406_c2_a_queued_local_poke_says_queued_and_counts_as_woken(world, monkeypatch, capsys, extra):
    agent(world, state="running")
    local_poke(world, poke_id=RECORD_A, poke_outcome="queued")
    r = send(world, monkeypatch, capsys, *extra)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 woken local queued record=local/{RECORD_A}\n"
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]


# --- C-3: an old sidecar's 200 is the old line, unchanged ---------------------------------


def test_406_c3_an_old_sidecar_answer_is_exactly_the_old_line(world, monkeypatch, capsys):
    agent(world, state="idle")
    # the fixture's default poke body is `{}`: neither `poke_id` nor `poke_outcome`
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken local\n"


# --- C-4: interrupt wins over queued ------------------------------------------------------


@pytest.mark.parametrize("outcome", ["sent", "queued"])
def test_406_c4_interrupt_never_prints_queued(world, monkeypatch, capsys, outcome):
    agent(world, state="running")
    local_poke(world, poke_id=RECORD_A, poke_outcome=outcome)
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 woken local interrupt record=local/{RECORD_A}\n"


# --- C-5: an unusable `poke_id` / `poke_outcome` is no field ------------------------------


@pytest.mark.parametrize("value", [
    "a b",
    "x\nwake: demo-pm-t1 woken local",
    "A" * 65,
    123,
    None,
], ids=["blank", "newline", "sixty-five", "number", "null"])
def test_406_c5_an_unusable_poke_id_means_no_record(world, monkeypatch, capsys, value):
    agent(world, state="idle")
    local_poke(world, poke_id=value, poke_outcome="sent")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken local\n"
    assert len(r.err.splitlines()) == 1


def test_406_c5_a_sixty_four_character_id_is_still_usable(world, monkeypatch, capsys):
    agent(world, state="idle")
    local_poke(world, poke_id="A" * 64, poke_outcome="sent")
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 woken local record=local/" + "A" * 64 + "\n"


def test_406_c5_an_unusable_poke_id_does_not_change_a_refusal(world, monkeypatch, capsys):
    agent(world, state="idle")
    world.sidecar.poke_status = 409
    world.sidecar.poke_body = poke_answer(error={"code": "agent_awaiting_answer"}, poke_id="a b")
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken awaiting-answer\n"


@pytest.mark.parametrize("value", ["later", 1, None], ids=["later", "number", "null"])
def test_406_c5_an_unusable_poke_outcome_is_not_queued(world, monkeypatch, capsys, value):
    agent(world, state="running")
    local_poke(world, poke_id=RECORD_A, poke_outcome=value)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken local record=local/{RECORD_A}\n"


# --- C-6: a refusal still carries the id --------------------------------------------------


@pytest.mark.parametrize("status,body,code", [
    (409, {"code": "agent_awaiting_answer", "poke_id": RECORD_A}, "awaiting-answer"),
    (409, {"error": {"code": "agent_not_running"}, "poke_id": RECORD_A}, "agent-not-running"),
    (503, {"error": {"code": "unavailable"}, "poke_id": RECORD_A}, "http-503"),
], ids=["awaiting-answer", "agent-not-running", "503"])
def test_406_c6_a_refused_poke_still_reports_its_record_id(world, monkeypatch, capsys, status, body, code):
    agent(world, state="idle")
    local_poke(world, status=status, **body)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken {code} record=local/{RECORD_A}\n"
    assert len(world.sidecar.pokes()) == 1


# --- C-7: a cross-machine 200 with `queued` -----------------------------------------------


@pytest.mark.parametrize("extra", [(), ("--require-wake",)], ids=["plain", "require-wake"])
def test_406_c7_a_queued_peer_poke_carries_a_peer_record(world, monkeypatch, capsys, extra):
    peer = world.add_peer(state="running")
    peer.poke_body = poke_answer(poke_id=RECORD_A, poke_outcome="queued")
    r = send(world, monkeypatch, capsys, *extra)
    assert r.code == 0
    assert r.err == (f"wake: demo-pm-t1 woken {woken(peer.port)} queued "
                     f"record=peer:{peer.port}/{RECORD_A}\n")


# --- C-8: merging several machines -------------------------------------------------------


def test_406_c8_one_sent_and_one_queued_is_not_queued(world, monkeypatch, capsys):
    sent = world.add_peer(state="idle")
    sent.poke_body = poke_answer(poke_id=RECORD_A, poke_outcome="sent")
    queued = world.add_peer(state="running")
    queued.poke_body = poke_answer(poke_id=RECORD_B, poke_outcome="queued")
    r = send(world, monkeypatch, capsys)
    assert r.err == (f"wake: demo-pm-t1 woken {woken(sent.port, queued.port)} "
                     f"record={peer_records((sent, RECORD_A), (queued, RECORD_B))}\n")


def test_406_c8_every_woken_peer_queued_is_queued(world, monkeypatch, capsys):
    first = world.add_peer(state="running")
    first.poke_body = poke_answer(poke_id=RECORD_A, poke_outcome="queued")
    second = world.add_peer(state="running")
    second.poke_body = poke_answer(poke_id=RECORD_B, poke_outcome="queued")
    r = send(world, monkeypatch, capsys)
    assert r.err == (f"wake: demo-pm-t1 woken {woken(first.port, second.port)} queued "
                     f"record={peer_records((first, RECORD_A), (second, RECORD_B))}\n")


def test_406_c8_a_failed_peer_still_lists_its_record(world, monkeypatch, capsys):
    refused = world.add_peer(state="idle")
    refused.poke_status = 409
    refused.poke_body = poke_answer(code="agent_awaiting_answer", poke_id=RECORD_A)
    queued = world.add_peer(state="running")
    queued.poke_body = poke_answer(poke_id=RECORD_B, poke_outcome="queued")
    r = send(world, monkeypatch, capsys)
    assert r.err == (f"wake: demo-pm-t1 woken {woken(queued.port)} queued "
                     f"record={peer_records((refused, RECORD_A), (queued, RECORD_B))}\n")


# --- C-9: no poke, no record --------------------------------------------------------------


def _local_exited(world):
    agent(world, state="exited")


def _not_found(world):
    world.add_peer(name=None)


def _peer_unavailable(world):
    pass  # the fixture's link has a fresh heartbeat and no ports: nothing to try


def _lookup_incomplete(world):
    world.link.fresh(free_port())  # a port nobody listens on


def _port_out_of_range(world):
    world.link.ports_text("0\n")


@pytest.mark.parametrize("setup,code", [
    (_local_exited, "state-exited"),
    (_not_found, "not-found"),
    (_peer_unavailable, "peer-unavailable"),
    (_lookup_incomplete, "lookup-incomplete"),
    (_port_out_of_range, "peer-port-out-of-range"),
], ids=["state-exited", "not-found", "peer-unavailable", "lookup-incomplete",
        "peer-port-out-of-range"])
def test_406_c9_nobody_poked_means_no_record(world, monkeypatch, capsys, setup, code):
    setup(world)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"


def test_406_c9_bad_name_means_no_record(world, monkeypatch, capsys):
    peer = world.add_peer(name="-demo-pm-t1")
    # `--to=-name` so argparse takes it as the value, not as an option
    r = run_aicomm(["send", "--channel", "team-demo", "--to=-demo-pm-t1", "--body", BODY],
                   monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: -demo-pm-t1 not-woken bad-name\n"
    assert peer.requests == []


# --- C-10: the answer never arrived, so there is no record --------------------------------


def test_406_c10_a_local_poke_timeout_has_no_record(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world, state="idle")
    monkeypatch.setattr(cli, "POKE_TIMEOUT", 0.3)
    world.sidecar.poke_delay = 2.0
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"
    assert len(world.sidecar.pokes()) == 1


def test_406_c10_a_local_poke_error_has_no_record(world, monkeypatch, capsys):
    agent(world, state="idle")
    monkeypatch.setattr("aicomm.cli._request", _broken_poke(ConnectionResetError("reset by peer")))
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown poke-error\n"


@pytest.mark.parametrize("error,kind,code", [
    (socket.timeout("slow"), "unknown", "poke-timeout"),
    (ConnectionRefusedError("refused"), "not-woken", "peer-unavailable"),
    (http.client.BadStatusLine("broken"), "unknown", "peer-bad-answer"),
], ids=["timeout", "refused", "bad-answer"])
def test_406_c10_a_peer_poke_without_a_usable_answer_has_no_record(world, monkeypatch, capsys,
                                                                  error, kind, code):
    world.add_peer()
    break_poke(monkeypatch, error)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 {kind} {code}\n"


# --- C-11: the closed grammar -------------------------------------------------------------


def test_406_c11_every_line_matches_the_closed_grammar(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1", state="running")
    world.sidecar.poke_bodies["demo-pm-t1"] = (
        200, poke_answer(poke_id=RECORD_A, poke_outcome="queued"))
    agent(world, "demo-arch-t1", state="exited")
    agent(world, "demo-gone-t1", state="idle")
    world.sidecar.poke_bodies["demo-gone-t1"] = (
        500, poke_answer(poke_id=RECORD_B, poke_outcome="failed"))
    peer = world.add_peer(state="running", name="demo-peer-t1")
    peer.poke_body = poke_answer(poke_id=RECORD_C, poke_outcome="queued")
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1",
             "--mention", "demo-gone-t1", "--mention", "demo-peer-t1")
    assert r.code == 0
    lines = r.err.splitlines()
    assert lines == [
        f"wake: demo-pm-t1 woken local queued record=local/{RECORD_A}",
        "wake: demo-arch-t1 not-woken state-exited",
        f"wake: demo-gone-t1 not-woken http-500 record=local/{RECORD_B}",
        f"wake: demo-peer-t1 woken {woken(peer.port)} queued "
        f"record=peer:{peer.port}/{RECORD_C}",
    ]
    for line in lines:
        assert WAKE_406_RE.fullmatch(line), line


@pytest.mark.parametrize("line", [
    "wake: a woken local",
    "wake: a woken local interrupt",
    "wake: a woken local queued",
    f"wake: a woken local queued record=local/{RECORD_A}",
    f"wake: a woken peer:5201,5202 interrupt record=peer:5201/{RECORD_A},peer:5202/{RECORD_B}",
    f"wake: a not-woken awaiting-answer record=local/{RECORD_A}",
    f"wake: a unknown poke-timeout record=local/{RECORD_A}",
], ids=["local", "interrupt", "queued", "queued-record", "two-peers-record",
        "not-woken-record", "unknown-record"])
def test_406_c11_the_grammar_accepts_the_expected_lines(line):
    assert WAKE_406_RE.fullmatch(line), line


@pytest.mark.parametrize("line", [
    "wake: a woken local interrupt queued",
    "wake: a woken local queued interrupt record=local/x",
    "wake: a woken local bogus",
    "wake: a woken local record=x/y",
    "wake: a woken local record=local/a b",
    "wake: a woken local record=local/" + "A" * 65,
    "wake: a woken local record=",
    "wake: a woken local record=local/A,",
    "wake: a not-woken http-409 record=local/a b",
    "wake: a queued local",
    "wake: a woken",
    "wake: a woken peer:5201,",
    "wake: a woken local record=local/A extra",
], ids=["both-modifiers", "both-modifiers-reversed", "bogus", "bad-location", "blank-id",
        "sixty-five", "empty-record", "trailing-comma", "not-woken-blank-id", "queued-kind",
        "no-code", "peer-trailing-comma", "trailing-text"])
def test_406_c11_the_grammar_rejects_broken_lines(line):
    assert WAKE_406_RE.fullmatch(line) is None, line


# --- C-12: `--require-wake` with queued ---------------------------------------------------


def test_406_c12_require_wake_counts_queued_as_woken(world, monkeypatch, capsys):
    agent(world, state="running")
    local_poke(world, poke_id=RECORD_A, poke_outcome="queued")
    r = send(world, monkeypatch, capsys, "--require-wake")
    assert r.code == 0


def test_406_c12_require_wake_is_4_for_one_not_woken_with_a_record(world, monkeypatch, capsys):
    agent(world, state="idle")
    world.sidecar.poke_status = 503
    world.sidecar.poke_body = poke_answer(poke_id=RECORD_A, poke_outcome="failed")
    r = send(world, monkeypatch, capsys, "--require-wake")
    assert r.code == 4
    assert r.err == f"wake: demo-pm-t1 not-woken http-503 record=local/{RECORD_A}\n"


def test_406_c12_require_wake_and_no_wake_is_still_refused_before_anything(world, monkeypatch, capsys):
    agent(world, state="idle")
    r = send(world, monkeypatch, capsys, "--require-wake", "--no-wake")
    assert r.code == 1
    assert world.calls() == [] and world.sidecar.requests == []


# --- C-13: no extra request and no extra subcommand ---------------------------------------


def test_406_c13_the_new_fields_add_no_request_and_no_subcommand(world, monkeypatch, capsys):
    agent(world, "demo-pm-t1", state="idle")
    local_poke(world, poke_id=RECORD_A, poke_outcome="sent")
    first = send(world, monkeypatch, capsys)
    assert first.code == 0
    peer = world.add_peer(name="demo-elsewhere-t1")
    second = send(world, monkeypatch, capsys, to="demo-elsewhere-t1")
    assert second.code == 0
    assert second.err == f"wake: demo-elsewhere-t1 woken {woken(peer.port)}\n"
    # only the two documented requests, and never a record lookup
    assert world.sidecar.foreign_requests() == []
    assert peer.foreign_requests() == []
    assert all("/pokes/" not in r["path"] for r in world.sidecar.requests)
    assert all("/pokes/" not in r["path"] for r in peer.requests)
    # the pokes carry `text`, `interrupt`, `via` and `ref`
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": FAKE_ID}]
    assert peer.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer", "ref": FAKE_ID}]
    # still only three subcommands
    bad = run_aicomm(["status"], monkeypatch, capsys)
    assert bad.code == 1
    assert "send" in bad.err and "inbox" in bad.err and "log" in bad.err


# --- C-15: the nested 409 error code (D-7) ------------------------------------------------


@pytest.mark.parametrize("transport", ["local", "peer"], ids=["local", "peer"])
@pytest.mark.parametrize("body,code", [
    ({"error": {"code": "agent_awaiting_answer", "message": "x"}}, "awaiting-answer"),
    ({"error": {"code": "agent_not_running"}}, "agent-not-running"),
    ({"error": {"code": "poke_timeout"}}, "http-409"),
    ({"code": "agent_awaiting_answer"}, "awaiting-answer"),
    ({"error": "agent_not_running"}, "agent-not-running"),
    ({"error": {"code": "weird"}, "code": "agent_not_running"}, "http-409"),
    ({"error": {"code": 5}, "code": "agent_not_running"}, "agent-not-running"),
    ({"error": {"message": "x"}}, "http-409"),
], ids=["nested-awaiting", "nested-not-running", "nested-unknown", "flat-code",
        "flat-error", "nested-wins", "nested-not-string", "message-only"])
def test_406_c15_the_409_error_code_is_read_from_both_shapes(world, monkeypatch, capsys,
                                                             transport, body, code):
    if transport == "local":
        agent(world, state="idle")
        world.sidecar.poke_status = 409
        world.sidecar.poke_body = poke_answer(**body)
    else:
        peer = world.add_peer(state="idle")
        peer.poke_status = 409
        peer.poke_body = json.dumps(body).encode("utf-8")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"


# --- C-17: 404 / 500 still carry the id ---------------------------------------------------


@pytest.mark.parametrize("status", [404, 500])
def test_406_c17_an_error_answer_still_reports_its_record_id(world, monkeypatch, capsys, status):
    agent(world, state="idle")
    local_poke(world, status=status, poke_id=RECORD_A, poke_outcome="failed")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken http-{status} record=local/{RECORD_A}\n"
    assert len(world.sidecar.pokes()) == 1


# --- #412: a body too deep to decode is an unreadable body, not an internal error ----------
# `json.loads` gives up on a deeply nested body with RecursionError, not ValueError. Reading
# the poke answer must treat the two the same: a 200 is still `woken`, a 409 still `http-409`.
# Otherwise `_wake_one`'s catch-all turns an already-successful wake into `unknown`.

DEEP_BODY = b"[" * 200000
DEEP_NESTED_BODY = b'{"poke_id":' + b"[" * 200000 + b"]" * 200000 + b"}"


@pytest.mark.parametrize("body", [DEEP_BODY, DEEP_NESTED_BODY], ids=["array", "nested-id"])
def test_412_a_200_with_a_body_too_deep_to_read_is_woken_without_a_record(body):
    from aicomm import cli

    answer = cli._poke_answer(200, body)
    assert (answer.kind, answer.code, answer.poke_id, answer.queued) == ("woken", "local", None, False)


@pytest.mark.parametrize("body", [DEEP_BODY, DEEP_NESTED_BODY], ids=["array", "nested-id"])
def test_412_a_409_with_a_body_too_deep_to_read_is_http_409(body):
    from aicomm import cli

    answer = cli._poke_answer(409, body)
    assert (answer.kind, answer.code, answer.poke_id) == ("not-woken", "http-409", None)


@pytest.mark.parametrize("body", [DEEP_BODY, DEEP_NESTED_BODY], ids=["array", "nested-id"])
def test_412_a_deep_poke_body_does_not_turn_a_wake_into_unknown(world, monkeypatch, capsys, body):
    agent(world, state="idle")
    world.sidecar.poke_body = body
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken local\n"


# --- #413: the poke body carries `via` and `ref` (contracts C-1..C-14 of aicomm-poke-ref) ---

ULID = "01JZ413000000000000000000A"
BAD_IDS = ["has space", "a" * 65, "", "abcé", "a.b"]


def send_out(world, text):
    """Make the fake angareion print exactly `text` on stdout (no newline added)."""
    world.setenv("FAKE_ANG_SEND_RAW", json.dumps(text))


def test_413_c1_local_poke_carries_via_and_ref(world, monkeypatch, capsys):
    agent(world)
    send_out(world, ULID + "\n")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == [
        {"text": WAKE_TEXT, "interrupt": False, "via": "aicomm", "ref": ULID}]
    assert r.err == "wake: demo-pm-t1 woken local\n"


def test_413_c2_peer_poke_stdin_is_the_exact_body_with_peer_via(world, monkeypatch, capsys):
    peer = world.add_peer()
    send_out(world, ULID + "\n")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert peer.pokes() == [
        {"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer", "ref": ULID}]
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"


@pytest.mark.parametrize("bad", BAD_IDS, ids=["space", "65", "empty", "non-ascii", "dot"])
def test_413_c3_a_bad_message_id_drops_the_ref_key_but_still_pokes_once(world, monkeypatch, capsys, bad):
    agent(world)
    send_out(world, bad + "\n")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm"}]
    assert "ref" not in world.sidecar.pokes()[0]
    assert r.err == "wake: demo-pm-t1 woken local\n"  # same line and code as with a good id


@pytest.mark.parametrize("bad", BAD_IDS, ids=["space", "65", "empty", "non-ascii", "dot"])
def test_413_c3_a_bad_message_id_drops_the_ref_key_on_a_peer_too(world, monkeypatch, capsys, bad):
    peer = world.add_peer()
    send_out(world, bad + "\n")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert peer.pokes() == [{"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer"}]
    assert r.err == f"wake: demo-pm-t1 woken {woken(peer.port)}\n"


@pytest.mark.parametrize("out", ["  " + ULID + "  \n", ULID + "\nextra\n"], ids=["padded", "extra-line"])
def test_413_c4_only_the_first_line_stripped_is_the_ref(world, monkeypatch, capsys, out):
    agent(world)
    send_out(world, out)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes()[0]["ref"] == ULID


@pytest.mark.parametrize("out", [ULID + "\r\n", "  " + ULID + "  \n", ULID + "\nextra\n", ULID])
def test_413_c4_poke_ref_takes_the_first_line_stripped(out):
    from aicomm import cli
    assert cli._poke_ref(out) == ULID


def test_413_c5_poke_ref_uses_fullmatch():
    from aicomm import cli
    assert cli._poke_ref("a" * 64) == "a" * 64
    assert cli._poke_ref("a" * 65) is None
    assert cli._poke_ref(ULID + "\x00") is None
    assert cli._poke_ref("") is None
    assert cli._poke_ref("\n" + ULID) is None  # the first line is empty


def test_413_c6_every_recipient_gets_the_same_ref(world, monkeypatch, capsys):
    for name in ("demo-a-t1", "demo-b-t1", "demo-c-t1"):
        agent(world, name)
    send_out(world, ULID + "\n")
    r = send(world, monkeypatch, capsys, "--mention", "demo-b-t1", "--mention", "demo-c-t1", to="demo-a-t1")
    assert r.code == 0
    pokes = world.sidecar.pokes()
    assert len(pokes) == 3
    assert all(p["ref"] == ULID and p["via"] == "aicomm" for p in pokes)


def test_413_c7_every_machine_gets_the_same_via_and_ref(world, monkeypatch, capsys):
    running = world.add_peer(state="running")
    idle = world.add_peer(state="idle")
    send_out(world, ULID + "\n")
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.err == f"wake: demo-pm-t1 woken {woken(running.port, idle.port)} interrupt\n"
    bodies = {peer.port: peer.pokes()[0] for peer in (running, idle)}
    assert bodies == {
        running.port: {"text": WAKE_TEXT, "interrupt": True, "via": "aicomm-peer", "ref": ULID},
        idle.port: {"text": WAKE_TEXT, "interrupt": False, "via": "aicomm-peer", "ref": ULID},
    }


def test_413_c8_via_and_ref_constants_fit_the_sidecar_shape():
    from aicomm import cli
    for via in (cli.POKE_VIA_LOCAL, cli.POKE_VIA_PEER):
        assert re.fullmatch(r"[a-z][a-z0-9-]{0,31}", via)
    assert (cli.POKE_VIA_LOCAL, cli.POKE_VIA_PEER) == ("aicomm", "aicomm-peer")
    assert cli.POKE_REF_RE.pattern == "[A-Za-z0-9_-]{1,64}"


def test_413_c9_no_wake_no_recipient_and_only_self_send_no_request(world, monkeypatch, capsys):
    agent(world)
    peer = world.add_peer()
    assert send(world, monkeypatch, capsys, "--no-wake").code == 0
    assert send(world, monkeypatch, capsys, to=None).code == 0
    world.setenv("ANGAREION_IDENTITY", "demo-pm-t1")
    assert send(world, monkeypatch, capsys).code == 0
    assert world.sidecar.requests == []
    assert peer.requests == []


def test_413_c10_a_failed_send_pokes_nobody(world, monkeypatch, capsys):
    agent(world)
    peer = world.add_peer()
    world.setenv("FAKE_ANG_SEND_EXIT", "1")
    r = send(world, monkeypatch, capsys)
    assert r.code == 1
    assert world.sidecar.requests == []
    assert peer.requests == []


def test_413_c11_text_stays_fixed_and_the_keys_are_one_of_two_sets(world, monkeypatch, capsys):
    agent(world)
    send(world, monkeypatch, capsys, "--ticket", "MyAiStorage#212")
    send_out(world, "a.b\n")
    send(world, monkeypatch, capsys)
    good, bad = world.sidecar.pokes()
    assert set(good) == {"text", "interrupt", "via", "ref"}
    assert set(bad) == {"text", "interrupt", "via"}
    for poke in (good, bad):
        assert poke["text"] == WAKE_TEXT
        for leaked in (world.sender, FAKE_ID, "SECRET-BODY-TEXT", "212", "team-demo"):
            assert leaked not in poke["text"]


def test_413_c12_a_send_ref_value_never_reaches_the_poke(world, monkeypatch, capsys):
    agent(world)
    peer = world.add_peer()
    send(world, monkeypatch, capsys, "--ref", "note=REF-SENTINEL-413")
    agent(world, "demo-other-t1")
    send(world, monkeypatch, capsys, "--ref", "note=REF-SENTINEL-413", to="demo-other-t1")
    raw = json.dumps(world.sidecar.pokes()) + json.dumps(peer.pokes())
    assert "REF-SENTINEL-413" not in raw and "note=" not in raw


def test_413_c13_local_and_peer_bodies_differ_only_in_via():
    from aicomm import cli
    for interrupt in (True, False):
        for mid in (ULID, None):
            local = cli._poke_body(interrupt, cli.POKE_VIA_LOCAL, mid)
            peer = cli._poke_body(interrupt, cli.POKE_VIA_PEER, mid)
            assert local["via"] == "aicomm" and peer["via"] == "aicomm-peer"
            assert {**local, "via": None} == {**peer, "via": None}
            assert list(local) == (["text", "interrupt", "via", "ref"] if mid else ["text", "interrupt", "via"])


def test_413_c14_the_record_id_is_still_read_from_a_poke_with_via_and_ref(world, monkeypatch, capsys):
    agent(world)
    local_poke(world, poke_id=RECORD_A)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken local record=local/{RECORD_A}\n"
    assert world.sidecar.pokes()[0]["ref"] == FAKE_ID

# --- #420: the other json.loads sites read a too-deep body as unreadable, too ---------------

def test_420_a_200_answer_too_deep_to_read_is_no_state():
    from aicomm import cli

    assert cli._agent_state(DEEP_BODY) is None


def test_420_a_deep_agent_answer_is_not_woken_bad_answer(world, monkeypatch, capsys):
    world.sidecar.raw["demo-pm-t1"] = (200, DEEP_BODY)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken bad-answer\n"
    assert world.sidecar.pokes() == []
