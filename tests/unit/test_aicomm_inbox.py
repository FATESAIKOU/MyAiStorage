"""aicomm inbox: read what was sent to me, always without --channel (#212)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from _aicomm_fakes import aicomm_world as world, run_aicomm


def verbs(world):
    return [c["argv"][0] for c in world.calls()]


def test_without_wait_it_reads_once_and_never_waits(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_OUT", "from: demo-pm-t1\nhello\n")
    r = run_aicomm(["inbox"], monkeypatch, capsys)
    assert r.code == 0
    assert r.out == "from: demo-pm-t1\nhello\n"
    assert world.calls() == [{"argv": ["read"], "stdin": ""}]


def test_json_is_passed_to_read_and_the_output_comes_back_verbatim(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_OUT", '[\n  {"id": "x"}\n]\n')
    r = run_aicomm(["inbox", "--json"], monkeypatch, capsys)
    assert r.code == 0
    assert r.out == '[\n  {"id": "x"}\n]\n'
    assert world.calls()[0]["argv"] == ["read", "--json"]


def test_wait_then_read_when_the_wait_says_there_is_mail(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_OUT", "hi\n")
    r = run_aicomm(["inbox", "--wait", "5"], monkeypatch, capsys)
    assert r.code == 0 and r.out == "hi\n"
    assert [c["argv"] for c in world.calls()] == [["wait", "--timeout", "5"], ["read"]]


def test_wait_timeout_exits_3_and_does_not_read(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_WAIT_EXIT", "3")
    r = run_aicomm(["inbox", "--wait", "5"], monkeypatch, capsys)
    assert r.code == 3
    assert verbs(world) == ["wait"]


def test_wait_backend_error_exits_2_and_is_not_taken_for_no_mail(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_WAIT_EXIT", "2")
    r = run_aicomm(["inbox", "--wait", "5"], monkeypatch, capsys)
    assert r.code == 2
    assert verbs(world) == ["wait"]


def test_wait_zero_checks_once(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_WAIT_EXIT", "3")
    r = run_aicomm(["inbox", "--wait", "0"], monkeypatch, capsys)
    assert r.code == 3
    assert world.calls()[0]["argv"] == ["wait", "--timeout", "0"]


def test_mentioned_goes_to_both_wait_and_read(world, monkeypatch, capsys):
    r = run_aicomm(["inbox", "--wait", "5", "--mentioned"], monkeypatch, capsys)
    assert r.code == 0
    assert [c["argv"] for c in world.calls()] == [["wait", "--mentioned", "--timeout", "5"], ["read", "--mentioned"]]


def test_json_goes_to_read_only_never_to_wait(world, monkeypatch, capsys):
    r = run_aicomm(["inbox", "--wait", "5", "--json"], monkeypatch, capsys)
    assert r.code == 0
    assert [c["argv"] for c in world.calls()] == [["wait", "--timeout", "5"], ["read", "--json"]]


def test_read_failure_code_is_passed_through(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_EXIT", "2")
    r = run_aicomm(["inbox"], monkeypatch, capsys)
    assert r.code == 2


def test_there_is_no_channel_option(world, monkeypatch, capsys):
    r = run_aicomm(["inbox", "--channel", "team-demo"], monkeypatch, capsys)
    assert r.code == 1
    assert world.calls() == []


@pytest.mark.parametrize("bad", ["-1", "abc"])
def test_a_bad_wait_value_is_a_usage_error(world, monkeypatch, capsys, bad):
    r = run_aicomm(["inbox", "--wait", bad], monkeypatch, capsys)
    assert r.code == 1
    assert world.calls() == []


def test_angareion_is_never_called_with_a_channel_and_the_sidecar_is_never_asked(world, monkeypatch, capsys):
    run_aicomm(["inbox", "--wait", "1", "--mentioned", "--json"], monkeypatch, capsys)
    assert all("--channel" not in c["argv"] for c in world.calls())
    assert world.sidecar.requests == []


# --- #219: an empty inbox says why on stderr (stdout and exit code unchanged) ---

HINT_KEYS = ("沒有新信", "同一個身分", "背景", "angareion read --channel <ch> --since <時刻>", "不移動游標")


def test_empty_read_hints_on_stderr_exit_0_stdout_unchanged(world, monkeypatch, capsys):
    r = run_aicomm(["inbox"], monkeypatch, capsys)
    assert r.code == 0
    assert r.out == ""
    for key in HINT_KEYS:
        assert key in r.err


@pytest.mark.parametrize("empty", ["[]\n", "[]", "[ ]\n", "  \n"])
def test_empty_json_read_hints_on_stderr_and_stdout_is_verbatim(world, monkeypatch, capsys, empty):
    world.setenv("FAKE_ANG_READ_OUT", empty)
    r = run_aicomm(["inbox", "--json"], monkeypatch, capsys)
    assert r.code == 0
    assert r.out == empty
    for key in HINT_KEYS:
        assert key in r.err


def test_empty_read_after_a_wait_that_succeeded_also_hints(world, monkeypatch, capsys):
    r = run_aicomm(["inbox", "--wait", "5"], monkeypatch, capsys)
    assert r.code == 0
    assert "同一個身分" in r.err


def test_mail_present_means_no_hint(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_OUT", "hello\n")
    r = run_aicomm(["inbox"], monkeypatch, capsys)
    assert r.code == 0 and r.out == "hello\n"
    assert "同一個身分" not in r.err and "沒有新信" not in r.err


def test_json_with_messages_means_no_hint(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_OUT", '[{"id": "x"}]\n')
    r = run_aicomm(["inbox", "--json"], monkeypatch, capsys)
    assert r.code == 0
    assert "同一個身分" not in r.err and "沒有新信" not in r.err


def test_wait_timeout_gives_no_hint(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_WAIT_EXIT", "3")
    r = run_aicomm(["inbox", "--wait", "5"], monkeypatch, capsys)
    assert r.code == 3
    assert "同一個身分" not in r.err and "沒有新信" not in r.err


def test_wait_backend_error_gives_no_hint(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_WAIT_EXIT", "2")
    r = run_aicomm(["inbox", "--wait", "5"], monkeypatch, capsys)
    assert r.code == 2
    assert "同一個身分" not in r.err and "沒有新信" not in r.err


def test_read_backend_error_gives_no_hint(world, monkeypatch, capsys):
    world.setenv("FAKE_ANG_READ_EXIT", "2")
    r = run_aicomm(["inbox"], monkeypatch, capsys)
    assert r.code == 2
    assert "同一個身分" not in r.err and "沒有新信" not in r.err
