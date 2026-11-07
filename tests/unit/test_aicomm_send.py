"""aicomm send: write to Angareion first, then wake the recipient on the local sidecar (#212)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import base64
import http.client
import json
import os
import re
import shutil
import socket
import sys
import urllib.parse

import pytest

from _aicomm_fakes import FAKE_ID, WAKE_TEXT, RawSidecar, run_aicomm
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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]


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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]
    assert [q["path"] for q in world.sidecar.requests] == ["/agents/demo-pm-t1", "/agents/demo-pm-t1/poke"]


def test_a_running_agent_is_poked_too_without_interrupt(world, monkeypatch, capsys):
    agent(world, state="running")
    send(world, monkeypatch, capsys)
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]


def test_two_sends_to_a_running_agent_are_two_pokes(world, monkeypatch, capsys):
    agent(world, state="running")
    send(world, monkeypatch, capsys)
    send(world, monkeypatch, capsys)
    assert len(world.sidecar.pokes()) == 2


def test_interrupt_on_a_running_agent_pokes_with_interrupt(world, monkeypatch, capsys):
    agent(world, state="running")
    send(world, monkeypatch, capsys, "--interrupt")
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": True}]


def test_interrupt_on_an_idle_agent_stays_a_plain_poke(world, monkeypatch, capsys):
    agent(world, state="idle")
    send(world, monkeypatch, capsys, "--interrupt")
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]


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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]
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
    assert set(poke) == {"text", "interrupt"}


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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]


# --- #368 PR-1: the `wake:` lines and `--require-wake` --------------------------------------
# C-n refers to the contract list in the `aicomm-cross-machine-wake` change (tasks.md §1).
# PR-1 implemented the local path; PR-2 adds the peer fallback below (`pool-peer`).
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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": True}]


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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]
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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]
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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}] * 2
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


# --- #368 PR-2: the peer fallback through `pool-peer` ----------------------------------------
# A recipient that is not on the local sidecar is looked up on the other machines. The fake
# `pool-peer` (in _aicomm_fakes) records every argv and answers from a scenario file, so a
# test never runs a real `pool-peer` and never touches the network.


def use_peer(world, find=None, calls=None):
    """Point aicomm at the fake `pool-peer`, with this `find` answer and per-port `call` answers."""
    world.use_fake_peer()
    world.peers({"find": {"rc": 1} if find is None else find, "call": calls or {}})


def found(*pairs, rc=0, **extra):
    """A `find` answer that reports these flat `port, state` pairs, as the JSON array."""
    return {"rc": rc, "stdout": json.dumps(
        [{"port": port, "agent": {"state": state}}
         for port, state in zip(pairs[::2], pairs[1::2])]), **extra}


def found_raw(stdout, rc=0, **extra):
    return {"rc": rc, "stdout": stdout, **extra}


def b64(data: bytes) -> str:
    """The fake peer's `stdout_b64` value: raw bytes it prints, for not-UTF-8 answers."""
    return base64.b64encode(data).decode("ascii")


def called(status=None, body="", rc=0, **extra):
    """A `call` answer: the HTTP status on the first line and the body after it."""
    return {"rc": rc, "stdout": "" if status is None else f"{status}\n{body}\n", **extra}


def dead_sidecar(world):
    """Point aicomm at a loopback port nobody listens on (the local sidecar is unreachable)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    world.setenv("AICOMM_SIDECAR", f"http://127.0.0.1:{dead}")


def test_c4_without_a_pool_peer_a_missing_agent_is_peer_unavailable(world, monkeypatch, capsys):
    # the fixture points AICOMM_POOL_PEER at a path that does not exist: no fallback at all
    r = send(world, monkeypatch, capsys)  # the fake sidecar does not know the agent -> 404
    assert r.code == 0
    assert r.out.splitlines()[0] == FAKE_ID
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert world.peer_calls() == []


def use_peer_from_path(world, bindir, monkeypatch):
    """The PATH fallback, for real: no `AICOMM_POOL_PEER` and the fake peer on PATH.

    Only reachable through a `pool-peer` wrapper aicomm must find with `shutil.which`;
    the wrapper leaves a marker next to itself, so a test can prove the wrapper (not an
    env hook) ran. Returns the marker path.
    """
    monkeypatch.delenv("AICOMM_POOL_PEER")
    bindir.mkdir(parents=True, exist_ok=True)
    marker = bindir / "path-peer-was-used"
    wrapper = bindir / "pool-peer"
    wrapper.write_text(
        f'#!/bin/sh\necho fell-back > "{marker}"\n'
        f'exec "{sys.executable}" "{world.fake_peer}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    world.peers({"find": {"rc": 1}, "call": {}})
    return marker


def test_c4_without_the_hook_pool_peer_comes_from_path(world, tmp_path, monkeypatch, capsys):
    from aicomm import cli

    bindir = tmp_path / "path-bin"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    marker = use_peer_from_path(world, bindir, monkeypatch)
    world.peers({"find": found(5201, "idle"), "call": {"5201": called(200)}})
    # running without the hook, aicomm must resolve the executable on PATH - the wrapper
    assert cli._peer_executable() == str(bindir / "pool-peer")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    assert marker.exists()  # the wrapper ran: the executable came from PATH, not a hook
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


def test_c4_without_the_hook_and_nothing_on_path_the_peer_is_unavailable(world, tmp_path, monkeypatch, capsys):
    # no hook, and PATH (the fake `angareion` dir only) has no `pool-peer`: nothing found,
    # nothing run; here a worker without either setup still answers "not woken", cleanly.
    # HOME is patched to tmp too (#392): a real `~/.mylinuxpool/bin/pool-peer` must not
    # be picked up by the bindir fallback.
    from aicomm import cli

    monkeypatch.delenv("AICOMM_POOL_PEER")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bindir = tmp_path / "path-bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{tmp_path / 'bin'}")
    assert cli._peer_executable() is None
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert world.peer_calls() == []


def use_peer_from_home_bin(world, home, monkeypatch, angareion_bin):
    """The `~/.mylinuxpool/bin` fallback (#392), for real: no hook and nothing on PATH.

    Like `use_peer_from_path`, but the wrapper sits in HOME's install bin; aicomm must
    find it through the HOME fallback (not PATH), and the wrapper leaves a marker next
    to itself to prove it (not an env hook) ran. Returns the marker path.
    """
    monkeypatch.delenv("AICOMM_POOL_PEER")
    monkeypatch.setenv("HOME", str(home))
    bindir = home / ".mylinuxpool" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    marker = bindir / "bin-peer-was-used"
    wrapper = bindir / "pool-peer"
    wrapper.write_text(
        f'#!/bin/sh\necho fell-back > "{marker}"\n'
        f'exec "{sys.executable}" "{world.fake_peer}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    # PATH carries the fake `angareion` dir only - no `pool-peer` on it.
    monkeypatch.setenv("PATH", str(angareion_bin))
    world.peers({"find": found(5201, "idle"), "call": {}})
    return marker


def test_c4_without_the_hook_pool_peer_comes_from_home_bin(world, tmp_path, monkeypatch, capsys):
    # #392: PATH has no `pool-peer`, but HOME's `~/.mylinuxpool/bin` does (the fake
    # wrapper) - aicomm must resolve and run that one, and the send still walks through.
    from aicomm import cli

    home = tmp_path / "home"
    marker = use_peer_from_home_bin(world, home, monkeypatch, tmp_path / "bin")
    world.peers({"find": found(5201, "idle"), "call": {"5201": called(200)}})
    assert shutil.which("pool-peer") is None
    assert cli._peer_executable() == str(home / ".mylinuxpool" / "bin" / "pool-peer")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    assert marker.exists()  # the wrapper ran: the executable came from HOME, not a hook
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


def test_when_path_and_home_bin_have_a_peer_the_path_one_wins(world, tmp_path, monkeypatch, capsys):
    # #392: both PATH and `~/.mylinuxpool/bin` carry a `pool-peer` - PATH wins, the bindir
    # file is never run.
    from aicomm import cli

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    bindir = home / ".mylinuxpool" / "bin"
    bindir.mkdir(parents=True)
    bin_marker = bindir / "bin-peer-was-used"
    bin_decoy = bindir / "pool-peer"
    # The decoy must satisfy the `run_aicomm` guard - since #393 low-3 that guard wants a
    # wrapper that really execs the fake, and a comment mention no longer passes - but a
    # good run must never execute it: PATH's wrapper does the find/call, and the exec
    # runs the fake with the scenario nailed to "nothing there", so a mistaken pick
    # still answers find rc 1 and still leaves its marker - both turn this test red.
    # (`exec env` is POSIX sh: the assignment is for the exec'd fake alone.)
    dead_scenario = tmp_path / "no-such-scenario"
    bin_decoy.write_text(
        f'#!/bin/sh\necho used > "{bin_marker}"\n'
        f'exec env FAKE_PEER_SCENARIO="{dead_scenario}" '
        f'"{sys.executable}" "{world.fake_peer}" "$@"\n', encoding="utf-8")
    bin_decoy.chmod(0o755)
    path_marker = use_peer_from_path(world, tmp_path / "path-bin", monkeypatch)
    world.peers({"find": found(5201, "idle"), "call": {"5201": called(200)}})
    assert cli._peer_executable() == str(tmp_path / "path-bin" / "pool-peer")
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    assert path_marker.exists()
    assert not bin_marker.exists()  # the bindir decoy stayed unused


def test_when_the_home_bin_peer_is_not_executable_it_is_as_if_absent(world, tmp_path, monkeypatch, capsys):
    # #392: a `~/.mylinuxpool/bin/pool-peer` without the exec bit does not count - no
    # candidate at all, the send answers `peer-unavailable` as before.
    from aicomm import cli

    monkeypatch.delenv("AICOMM_POOL_PEER")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    bindir = home / ".mylinuxpool" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "pool-peer").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bindir.joinpath("pool-peer").chmod(0o644)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}")
    assert shutil.which("pool-peer") is None
    assert cli._peer_executable() is None
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert world.peer_calls() == []


def test_when_path_home_fails_the_peer_is_unavailable(world, tmp_path, monkeypatch, capsys):
    # #394: `Path.home()` can raise RuntimeError (no HOME and no passwd entry for the
    # uid) - the bindir fallback must treat that as "no install dir", not die: the send
    # answers `peer-unavailable`, never an internal error.
    from aicomm import cli

    monkeypatch.delenv("AICOMM_POOL_PEER")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert shutil.which("pool-peer") is None

    def broken_home():
        raise RuntimeError("HOME not set, and no passwd entry for the uid")

    monkeypatch.setattr(cli.Path, "home", staticmethod(broken_home))
    assert cli._peer_executable() is None
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert world.peer_calls() == []


def test_the_hook_wins_over_path(world, tmp_path, monkeypatch, capsys):
    bindir = tmp_path / "path-bin"
    bindir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    marker = tmp_path / "path-peer-was-used"
    decoy = bindir / "pool-peer"
    decoy.write_text(f'#!/bin/sh\necho used > "{marker}"\nexit 1\n', encoding="utf-8")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    assert not marker.exists()


def test_c5_find_rc_1_is_not_found(world, monkeypatch, capsys):
    use_peer(world, {"rc": 1, "stdout": "[]\n"})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken not-found\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


def test_c6_find_rc_3_is_lookup_incomplete_and_calls_nobody(world, monkeypatch, capsys):
    use_peer(world, {"rc": 3, "stdout": "[]\n"})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


def test_c7_find_rc_2_is_peer_unavailable(world, monkeypatch, capsys):
    use_peer(world, {"rc": 2, "stdout": "", "stderr": "pool-peer: gateway down\n"})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"


@pytest.mark.parametrize("rc", [4, 42, 255])
def test_find_with_an_rc_outside_the_contract_is_peer_bad_answer(world, monkeypatch, capsys, rc):
    use_peer(world, {"rc": rc, "stdout": "[]\n"})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"


def test_c34_a_find_that_cannot_be_started_is_peer_unavailable(world, tmp_path, monkeypatch, capsys):
    # a directory is not executable: subprocess raises OSError before anything runs
    world.setenv("AICOMM_POOL_PEER", str(tmp_path))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"


@pytest.mark.parametrize("stdout", [
    "not json\n",
    "{}\n",
    '"idle"\n',
    "null\n",
    "[]\n",
    '[1, null, "x", []]\n',
    '[{"port": "5201", "agent": {"state": "idle"}}]\n',
    '[{"port": 5201}]\n',
    '[{"port": 5201, "agent": {}}]\n',
    '[{"port": 5201, "agent": {"state": 7}}]\n',
    '[{"port": true, "agent": {"state": "idle"}}]\n',
    '[{"port": 5201.5, "agent": {"state": "idle"}}]\n',
], ids=["text", "object", "string", "null", "empty", "no-objects", "string-port", "no-agent",
        "no-state", "int-state", "bool-port", "float-port"])
def test_c11_a_find_answer_that_cannot_be_read_is_peer_bad_answer(world, monkeypatch, capsys, stdout):
    use_peer(world, found_raw(stdout))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]  # nothing was poked


def test_a_find_answer_in_bytes_that_are_not_utf8_is_peer_bad_answer(world, monkeypatch, capsys):
    # the answer cannot be decoded at all: still an ordinary unreadable answer, nothing
    # was poked yet, so nobody is left "possibly pinged" (`not-woken`, never `unknown`)
    use_peer(world, {"rc": 0, "stdout_b64": b64(b"\xff\xfe not \x80 utf-8")})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]  # nothing was poked


def test_c8_one_peer_is_poked_with_the_local_pokes_body(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(200, '{"ok": true}')})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    find_call, poke_call = world.peer_calls()
    assert find_call["argv"] == ["find", "demo-pm-t1", "--json", "--timeout", "10"]
    assert poke_call["argv"] == ["call", "5201", "POST", "/agents/demo-pm-t1/poke", "--timeout", "5"]
    assert json.loads(poke_call["stdin"]) == {"text": WAKE_TEXT, "interrupt": False}
    assert world.sidecar.pokes() == []  # only the fake `pool-peer` was asked to poke


def test_each_recipient_is_looked_up_on_its_own(world, monkeypatch, capsys):
    world.use_fake_peer()
    world.peers({"find": {"by_id": {
        "demo-pm-t1": found(5201, "idle"),
        "demo-arch-t1": {"rc": 1, "stdout": "[]\n"},
    }}, "call": {"5201": called(200)}})
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1")
    assert r.err == (
        "wake: demo-pm-t1 woken peer:5201\n"
        "wake: demo-arch-t1 not-woken not-found\n"
    )
    finds = [c["argv"][1] for c in world.peer_calls() if c["argv"][0] == "find"]
    assert finds == ["demo-pm-t1", "demo-arch-t1"]  # one lookup per recipient, in order


def test_the_poke_path_quotes_the_name(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys, to="demo pm/1")
    assert r.code == 0
    find_call, poke_call = world.peer_calls()
    assert find_call["argv"][1] == "demo pm/1"  # argv, one token, no quoting
    assert poke_call["argv"][3] == f"/agents/{urllib.parse.quote('demo pm/1', safe='')}/poke"


def test_the_name_is_one_argv_token_and_never_a_shell_word(world, tmp_path, monkeypatch, capsys):
    canary = tmp_path / "canary"
    # A shell would split this into `<pool-peer> find demo-pm-t1` and `touch <canary>`.
    name = f"demo-pm-t1; touch {canary} #"
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys, to=name)
    assert r.code == 0
    assert not canary.exists()  # argv, not a shell word: nothing after the ';' runs
    find_call, _ = world.peer_calls()
    assert find_call["argv"] == ["find", name, "--json", "--timeout", "10"]


def test_c15_a_sidecar_that_cannot_be_reached_still_looks_elsewhere(world, monkeypatch, capsys):
    dead_sidecar(world)
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


@pytest.mark.parametrize("local", ["idle", "running", "exited", "500", "broken"],
                         ids=["idle", "running", "exited", "http-500", "bad-answer"])
def test_c15_a_local_answer_never_sends_it_to_another_machine(world, monkeypatch, capsys, local):
    if local in ("idle", "running", "exited"):
        agent(world, state=local)
    else:
        world.sidecar.raw["demo-pm-t1"] = (500, b"{}") if local == "500" else (200, b"not json")
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.peer_calls() == []  # the other machines are never asked


def test_c16_a_local_poke_timeout_never_asks_pool_peer(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "POKE_TIMEOUT", 0.3)
    world.sidecar.poke_delay = 2.0
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"
    assert world.peer_calls() == []


def test_c9_the_same_port_reported_twice_is_poked_once(world, monkeypatch, capsys):
    use_peer(world, found_raw(json.dumps([
        {"port": 5201, "agent": {"state": "idle"}},
        {"port": 5201, "agent": {"state": "idle"}},
    ])), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


def test_c9_the_first_item_of_a_port_is_the_one_that_counts(world, monkeypatch, capsys):
    use_peer(world, found_raw(json.dumps([
        {"port": 5201, "agent": {"state": "exited"}},
        {"port": 5201, "agent": {"state": "idle"}},
    ])), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken state-exited\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


def test_c9_every_pokeable_peer_is_poked_and_only_the_woken_ports_are_listed(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle", 5202, "idle"), {"5201": called(200), "5202": called(500)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 woken peer:5201\n"
    pokes = [c["argv"][1] for c in world.peer_calls() if c["argv"][0] == "call"]
    assert pokes == ["5201", "5202"]  # both were tried, in the order `find` listed them


def test_c9_the_woken_ports_are_listed_from_the_smallest(world, monkeypatch, capsys):
    use_peer(world, found(5202, "idle", 5201, "idle"), {"5201": called(200), "5202": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 woken peer:5201,5202\n"


@pytest.mark.parametrize("state", ["starting", "waiting-quota", "exited"])
def test_c9_a_peer_in_another_state_is_not_poked(world, monkeypatch, capsys, state):
    use_peer(world, found(5201, state), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken state-{state}\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


def test_a_peer_state_that_is_not_one_token_is_a_peer_bad_answer(world, monkeypatch, capsys):
    use_peer(world, found_raw(json.dumps([
        {"port": 5201, "agent": {"state": "idle\nwake: demo-pm-t1 woken local"}},
    ])), {"5201": called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


@pytest.mark.parametrize("port", [5199, 5100, 5400, 0, 65535])
def test_c10_a_port_outside_the_pool_range_is_never_called(world, monkeypatch, capsys, port):
    use_peer(world, found(port, "idle"), {str(port): called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-port-out-of-range\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]  # `call` was never run


@pytest.mark.parametrize("port", [5200, 5399])
def test_c10_the_edges_of_the_pool_range_are_called(world, monkeypatch, capsys, port):
    use_peer(world, found(port, "idle"), {str(port): called(200)})
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 woken peer:{port}\n"


def test_c27_each_peer_decides_its_own_interrupt(world, monkeypatch, capsys):
    use_peer(world, found(5201, "running", 5202, "idle"),
             {"5201": called(200), "5202": called(200)})
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.err == "wake: demo-pm-t1 woken peer:5201,5202 interrupt\n"
    bodies = {c["argv"][1]: json.loads(c["stdin"]) for c in world.peer_calls() if c["argv"][0] == "call"}
    assert bodies == {
        "5201": {"text": WAKE_TEXT, "interrupt": True},
        "5202": {"text": WAKE_TEXT, "interrupt": False},
    }


def test_c27_a_peer_that_did_not_wake_does_not_add_interrupt(world, monkeypatch, capsys):
    use_peer(world, found(5201, "running", 5202, "idle"),
             {"5201": called(500), "5202": called(200)})
    r = send(world, monkeypatch, capsys, "--interrupt")
    assert r.err == "wake: demo-pm-t1 woken peer:5202\n"


def test_c12_a_call_that_times_out_is_unknown_and_is_not_retried(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(rc=3)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]  # one call, no resend


@pytest.mark.parametrize("status,body,code", [
    (409, '{"code": "agent_awaiting_answer"}', "awaiting-answer"),
    (409, '{"error": "agent_awaiting_answer"}', "awaiting-answer"),
    (409, '{"code": "agent_not_running"}', "agent-not-running"),
    (409, '{"error": "agent_not_running"}', "agent-not-running"),
    (409, '{"code": "something_else"}', "http-409"),
    (409, '{"code": 7, "error": "agent_not_running"}', "agent-not-running"),
    (409, "not json", "http-409"),
    (500, "", "http-500"),
    (201, "", "http-201"),
], ids=["code-awaiting", "error-awaiting", "code-not-running", "error-not-running",
        "unknown-code", "code-not-a-string", "unreadable-409", "http-500", "http-201"])
def test_c13_a_call_answer_is_read_with_the_local_pokes_table(world, monkeypatch, capsys, status, body, code):
    use_peer(world, found(5201, "idle"), {"5201": called(status, body)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


@pytest.mark.parametrize("stdout", ["", "\n", "not a status\n{}", "2000\n{}", "OK\n{}", "20\n{}",
                                    "200 OK\n{}"],
                         ids=["empty", "blank", "words", "four-digits", "words-again", "two-digits",
                              "status-with-words"])
def test_c14_a_call_whose_first_line_is_not_a_status_is_unknown_peer_bad_answer(world, monkeypatch, capsys, stdout):
    use_peer(world, found(5201, "idle"), {"5201": {"rc": 0, "stdout": stdout}})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown peer-bad-answer\n"


def test_c14_a_call_answer_in_bytes_that_are_not_utf8_is_unknown_peer_bad_answer(world, monkeypatch, capsys):
    # same for bytes that cannot be decoded; the poke was already sent, so nobody may
    # conclude "not woken" - `unknown` is still the only safe word here
    use_peer(world, found(5201, "idle"), {"5201": {"rc": 0, "stdout_b64": b64(b"\xff\xfe 200")}})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown peer-bad-answer\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]  # one call, no resend


@pytest.mark.parametrize("rc", [1, 4, 255])
def test_c28_a_call_rc_outside_the_contract_is_unknown_peer_bad_answer(world, monkeypatch, capsys, rc):
    use_peer(world, found(5201, "idle"), {"5201": called(rc=rc)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown peer-bad-answer\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


def test_c36_a_call_that_cannot_connect_is_peer_unavailable(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(rc=2)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


def test_c35_a_call_that_cannot_be_started_is_peer_unavailable(world, monkeypatch, capsys):
    from aicomm import cli

    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    real_run = cli._run_pool_peer

    def run(executable, args, stdin_text, limit):
        if args[:1] == ["call"]:  # only the poke fails to start; `find` already answered
            raise FileNotFoundError(executable)
        return real_run(executable, args, stdin_text, limit)

    monkeypatch.setattr(cli, "_run_pool_peer", run)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


def test_c17_a_find_that_hangs_is_lookup_incomplete(world, monkeypatch, capsys):
    from aicomm import cli

    monkeypatch.setattr(cli, "_peer_child_limit", lambda seconds: 0.2)  # the real one is 12 s
    use_peer(world, found(5201, "idle", delay=2))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken lookup-incomplete\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]


def test_c17_a_call_that_hangs_is_unknown_poke_timeout(world, monkeypatch, capsys):
    from aicomm import cli

    monkeypatch.setattr(cli, "_peer_child_limit", lambda seconds: 0.2)  # the real one is 7 s
    use_peer(world, found(5201, "idle"), {"5201": called(200, delay=2)})
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]


def test_the_child_limit_is_the_given_timeout_plus_two_seconds():
    from aicomm import cli

    assert (cli.PEER_FIND_TIMEOUT, cli.PEER_CALL_TIMEOUT) == (10, 5)
    assert cli._peer_child_limit(cli.PEER_FIND_TIMEOUT) == 12
    assert cli._peer_child_limit(cli.PEER_CALL_TIMEOUT) == 7


def test_c18_a_peer_result_is_still_one_line_per_recipient(world, monkeypatch, capsys):
    agent(world, "demo-arch-t1", state="exited")
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1")
    assert r.code == 0
    lines = r.err.splitlines()
    assert lines == [
        "wake: demo-pm-t1 woken peer:5201",
        "wake: demo-arch-t1 not-woken state-exited",
    ]
    for line in lines:
        assert WAKE_406_RE.fullmatch(line), line


@pytest.mark.parametrize("scenario,code", [
    ({"rc": 2, "stdout": "SECRET-STDOUT", "stderr": "pool-peer: SECRET-DIAGNOSTIC\n"}, "peer-unavailable"),
    ({"rc": 0, "stdout": "SECRET-STDOUT", "stderr": "pool-peer: SECRET-DIAGNOSTIC\n"}, "peer-bad-answer"),
], ids=["rc-2", "rc-0-not-an-array"])
def test_c18_the_pool_peers_output_never_reaches_the_wake_line(world, monkeypatch, capsys, scenario, code):
    # A.4: the `wake:` line carries a reason code, never `pool-peer`'s own words.
    use_peer(world, scenario)
    r = send(world, monkeypatch, capsys)
    assert r.err == f"wake: demo-pm-t1 not-woken {code}\n"
    assert "SECRET" not in r.err and "SECRET" not in r.out


def test_c18_a_calls_own_output_never_reaches_the_wake_line(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"),
             {"5201": {"rc": 2, "stdout": "SECRET-STDOUT", "stderr": "pool-peer: SECRET-DIAGNOSTIC\n"}})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken peer-unavailable\n"
    assert "SECRET" not in r.err and "SECRET" not in r.out


def test_c21_a_name_starting_with_a_dash_is_never_given_to_pool_peer(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    # `--to=-name` so argparse takes it as the value and not as an option
    r = run_aicomm(["send", "--channel", "team-demo", "--to=-demo-pm-t1", "--body", BODY],
                   monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: -demo-pm-t1 not-woken bad-name\n"
    assert world.peer_calls() == []


@pytest.mark.parametrize("extra,to", [(("--no-wake",), "demo-pm-t1"), ((), None)], ids=["no-wake", "broadcast"])
def test_c22_nobody_is_looked_up_on_another_machine_either(world, monkeypatch, capsys, extra, to):
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys, *extra, to=to)
    assert r.code == 0
    assert world.peer_calls() == []


def test_an_unknown_from_one_peer_beats_a_not_woken_from_another(world, monkeypatch, capsys):
    use_peer(world, found(5202, "idle", 5201, "idle"),
             {"5201": called(rc=3), "5202": called(500)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown poke-timeout\n"


def test_two_unknowns_take_the_lowest_ports_reason(world, monkeypatch, capsys):
    use_peer(world, found(5202, "idle", 5201, "idle"),
             {"5201": called(rc=1), "5202": called(rc=3)})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 unknown peer-bad-answer\n"


def test_two_not_woken_take_the_lowest_ports_reason(world, monkeypatch, capsys):
    use_peer(world, found(5203, "idle", 5202, "exited"),
             {"5203": called(409, '{"error": "agent_awaiting_answer"}')})
    r = send(world, monkeypatch, capsys)
    assert r.err == "wake: demo-pm-t1 not-woken state-exited\n"


def test_a2_a_ref_without_equals_asks_nobody_on_another_machine(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = send(world, monkeypatch, capsys, "--ref", "noequals")
    assert r.code == 1
    assert world.peer_calls() == [] and world.sidecar.requests == []


def test_a3_log_never_asks_pool_peer(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    r = run_aicomm(["log"], monkeypatch, capsys)
    assert r.code == 0
    assert world.peer_calls() == []


# --- #406: the wake outcome (`queued`) and the sidecar record id ---------------------------
# C-n below refers to the contract list in the `aicomm-wake-outcome` change (design.md §3).
# The local lines come from the fake sidecar; the cross-machine ones from the fake
# `pool-peer`. The fake sidecar answers a plain `{}` by default, so every test above keeps
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
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]


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
    use_peer(world, found(5201, "running"), {
        "5201": called(200, json.dumps({"poke_id": RECORD_A, "poke_outcome": "queued"})),
    })
    r = send(world, monkeypatch, capsys, *extra)
    assert r.code == 0
    assert r.err == f"wake: demo-pm-t1 woken peer:5201 queued record=peer:5201/{RECORD_A}\n"


# --- C-8: merging several machines -------------------------------------------------------


def test_406_c8_one_sent_and_one_queued_is_not_queued(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle", 5202, "running"), {
        "5201": called(200, json.dumps({"poke_id": RECORD_A, "poke_outcome": "sent"})),
        "5202": called(200, json.dumps({"poke_id": RECORD_B, "poke_outcome": "queued"})),
    })
    r = send(world, monkeypatch, capsys)
    assert r.err == (f"wake: demo-pm-t1 woken peer:5201,5202 "
                     f"record=peer:5201/{RECORD_A},peer:5202/{RECORD_B}\n")


def test_406_c8_every_woken_peer_queued_is_queued(world, monkeypatch, capsys):
    use_peer(world, found(5201, "running", 5202, "running"), {
        "5201": called(200, json.dumps({"poke_id": RECORD_A, "poke_outcome": "queued"})),
        "5202": called(200, json.dumps({"poke_id": RECORD_B, "poke_outcome": "queued"})),
    })
    r = send(world, monkeypatch, capsys)
    assert r.err == (f"wake: demo-pm-t1 woken peer:5201,5202 queued "
                     f"record=peer:5201/{RECORD_A},peer:5202/{RECORD_B}\n")


def test_406_c8_a_failed_peer_still_lists_its_record(world, monkeypatch, capsys):
    use_peer(world, found(5201, "idle", 5202, "running"), {
        "5201": called(409, json.dumps({"code": "agent_awaiting_answer", "poke_id": RECORD_A})),
        "5202": called(200, json.dumps({"poke_id": RECORD_B, "poke_outcome": "queued"})),
    })
    r = send(world, monkeypatch, capsys)
    assert r.err == (f"wake: demo-pm-t1 woken peer:5202 queued "
                     f"record=peer:5201/{RECORD_A},peer:5202/{RECORD_B}\n")


# --- C-9: no poke, no record --------------------------------------------------------------


def _local_exited(world):
    agent(world, state="exited")


def _not_found(world):
    use_peer(world, {"rc": 1, "stdout": "[]\n"})


def _peer_unavailable(world):
    pass  # the fixture's hook points at a path that does not exist


def _lookup_incomplete(world):
    use_peer(world, {"rc": 3, "stdout": ""})


def _port_out_of_range(world):
    use_peer(world, found(5199, "idle"), {"5199": called(200)})


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
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    # `--to=-name` so argparse takes it as the value, not as an option
    r = run_aicomm(["send", "--channel", "team-demo", "--to=-demo-pm-t1", "--body", BODY],
                   monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: -demo-pm-t1 not-woken bad-name\n"
    assert world.peer_calls() == []


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


@pytest.mark.parametrize("rc,kind,code", [
    (3, "unknown", "poke-timeout"),
    (2, "not-woken", "peer-unavailable"),
    (1, "unknown", "peer-bad-answer"),
    (4, "unknown", "peer-bad-answer"),
], ids=["rc-3", "rc-2", "rc-1", "rc-4"])
def test_406_c10_a_call_without_a_usable_body_has_no_record(world, monkeypatch, capsys, rc, kind, code):
    use_peer(world, found(5201, "idle"), {"5201": called(rc=rc)})
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
    world.use_fake_peer()
    world.peers({
        "find": {"by_id": {"demo-peer-t1": found(5201, "running")}},
        "call": {"5201": called(200, json.dumps(
            {"poke_id": RECORD_C, "poke_outcome": "queued"}))},
    })
    r = send(world, monkeypatch, capsys, "--mention", "demo-arch-t1",
             "--mention", "demo-gone-t1", "--mention", "demo-peer-t1")
    assert r.code == 0
    lines = r.err.splitlines()
    assert lines == [
        f"wake: demo-pm-t1 woken local queued record=local/{RECORD_A}",
        "wake: demo-arch-t1 not-woken state-exited",
        f"wake: demo-gone-t1 not-woken http-500 record=local/{RECORD_B}",
        f"wake: demo-peer-t1 woken peer:5201 queued record=peer:5201/{RECORD_C}",
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
    use_peer(world, found(5201, "idle"), {"5201": called(200)})
    second = send(world, monkeypatch, capsys, to="demo-elsewhere-t1")
    assert second.code == 0
    assert second.err == "wake: demo-elsewhere-t1 woken peer:5201\n"
    # only the two documented requests, and never a record lookup
    assert world.sidecar.foreign_requests() == []
    assert all("/pokes/" not in r["path"] for r in world.sidecar.requests)
    # the pokes still carry exactly `text` and `interrupt` (C-16 is out of scope)
    assert world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": False}]
    peer_pokes = [c for c in world.peer_calls() if c["argv"][0] == "call"]
    assert [json.loads(c["stdin"]) for c in peer_pokes] == [{"text": WAKE_TEXT, "interrupt": False}]
    assert [c["argv"][0] for c in world.peer_calls()] == ["find", "call"]
    # still only three subcommands
    bad = run_aicomm(["status"], monkeypatch, capsys)
    assert bad.code == 1
    assert "send" in bad.err and "inbox" in bad.err and "log" in bad.err


# --- C-15: the nested 409 error code (D-7) ------------------------------------------------


@pytest.mark.parametrize("transport", ["local", "call"], ids=["local", "call"])
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
        use_peer(world, found(5201, "idle"), {"5201": called(409, json.dumps(body))})
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


# --- #420: the other json.loads sites read a too-deep body as unreadable, too ---------------

def test_420_a_200_agent_answer_too_deep_to_read_is_no_state():
    from aicomm import cli

    assert cli._agent_state(DEEP_BODY) is None


def test_420_a_deep_agent_answer_is_not_woken_bad_answer(world, monkeypatch, capsys):
    world.sidecar.raw["demo-pm-t1"] = (200, DEEP_BODY)
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken bad-answer\n"
    assert world.sidecar.pokes() == []


def test_420_a_find_answer_too_deep_to_read_is_no_peers():
    from aicomm import cli

    assert cli._parse_peers("[" * 200000) is None


def test_420_a_deep_find_answer_is_peer_bad_answer(world, monkeypatch, capsys):
    use_peer(world, found_raw("[" * 200000))
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert r.err == "wake: demo-pm-t1 not-woken peer-bad-answer\n"
    assert [c["argv"][0] for c in world.peer_calls()] == ["find"]  # nothing was poked
