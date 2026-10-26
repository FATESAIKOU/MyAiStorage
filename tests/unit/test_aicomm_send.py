"""aicomm send: write to Angareion first, then wake the recipient on the local sidecar (#212)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import socket

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
    assert "已送達" in r.err and "未叫醒" in r.err


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
    assert state in r.err


def test_an_agent_on_another_machine_is_not_poked_and_that_is_normal(world, monkeypatch, capsys):
    r = send(world, monkeypatch, capsys)  # the fake sidecar does not know the agent -> 404
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert "不在本機" in r.err
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
    assert "叫醒結果未知" in r.err


@pytest.mark.parametrize("status", [409, 503, 500])
def test_a_refused_poke_is_not_retried_and_the_exit_code_stays_0(world, monkeypatch, capsys, status):
    agent(world)
    world.sidecar.poke_status = status
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert len(world.sidecar.pokes()) == 1
    assert "未叫醒" in r.err and str(status) in r.err


def test_a_lookup_timeout_means_no_poke(world, monkeypatch, capsys):
    from aicomm import cli

    agent(world)
    monkeypatch.setattr(cli, "GET_TIMEOUT", 0.3)
    world.sidecar.get_delay = 2.0
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == []
    assert "未叫醒" in r.err


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
    assert "未叫醒" in r.err
    assert r.out.splitlines()[0] == FAKE_ID


def test_an_unexpected_lookup_status_means_no_poke(world, monkeypatch, capsys):
    world.sidecar.raw["demo-pm-t1"] = (500, b'{"state": "idle"}')
    r = send(world, monkeypatch, capsys)
    assert r.code == 0
    assert world.sidecar.pokes() == []


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
    for name in ("demo-pm-t1", "demo-arch-t1"):
        assert f"未叫醒 {name}" in r.err
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
