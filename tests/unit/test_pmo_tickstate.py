"""Unit tests for the pmo tick state schema (`#110` T-1, design §3.4, §3.6)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from pmo.errors import SchemaError
from pmo.tickstate import STATE_FILE_NAME, Handoff, TickState, Wake, WorkerPoll, empty, load_or_empty, parse, save


def _tick(**overrides: object) -> dict[str, object]:
    """One tick state with every field the design §3.4 names."""
    data: dict[str, object] = {
        "version": 1,
        "last_tick_at": "2026-11-03T01:00:00Z",
        "handoffs": {
            "01JGROUP": {
                "handed_at": "2026-11-03T00:00:00Z",
                "last_reminded_at": "2026-11-03T00:10:00Z",
                "next_due_at": "2026-11-03T00:20:00Z",
            }
        },
        "worker_poll": {
            "mylinuxpool-w1": {
                "last_reported_at": "2026-11-03T00:05:00Z",
                "next_due_at": "2026-11-03T00:15:00Z",
            }
        },
        "wake": {
            "every": "10m",
            "set_at": "2026-11-03T00:00:00Z",
            "missing_reported_at": "2026-11-03T00:30:00Z",
            "error_reported": {"job-1": "agent_not_running"},
            "orphan_reported_at": "2026-11-03T00:40:00Z",
        },
    }
    data.update(overrides)
    return data


def test_a_full_tick_state_parses():
    state = parse(_tick())
    assert state.last_tick_at == "2026-11-03T01:00:00Z"
    handoff = state.handoffs["01JGROUP"]
    assert (handoff.handed_at, handoff.last_reminded_at, handoff.next_due_at) == (
        "2026-11-03T00:00:00Z",
        "2026-11-03T00:10:00Z",
        "2026-11-03T00:20:00Z",
    )
    poll = state.worker_poll["mylinuxpool-w1"]
    assert (poll.last_reported_at, poll.next_due_at) == (
        "2026-11-03T00:05:00Z",
        "2026-11-03T00:15:00Z",
    )
    assert state.wake.every == "10m"
    assert state.wake.set_at == "2026-11-03T00:00:00Z"
    assert state.wake.missing_reported_at == "2026-11-03T00:30:00Z"
    assert state.wake.error_reported == {"job-1": "agent_not_running"}
    assert state.wake.orphan_reported_at == "2026-11-03T00:40:00Z"


def test_the_minimum_tick_state_is_the_version():
    assert parse({"version": 1}) == TickState()
    assert empty() == TickState()


def test_wake_every_is_an_interval_string_or_null():
    assert parse(_tick(wake={"every": None})).wake.every is None
    assert parse(_tick(wake={"every": "10m"})).wake.every == "10m"
    # A number of seconds is not what `wake set` writes; the file holds a string.
    with pytest.raises(SchemaError) as caught:
        parse(_tick(wake={"every": 600}))
    assert "string or null" in str(caught.value)


@pytest.mark.parametrize("every", ["10x", "", "m", "10", "10 m", "1.5h", "-10m", "10M", "10m ", "h10"])
def test_a_wake_every_that_is_not_an_interval_is_refused_at_read_time(every):
    """Rev 中-1: a broken interval is a bad file now, not a T-5 surprise later."""
    with pytest.raises(SchemaError) as caught:
        parse(_tick(wake={"every": every}))
    assert "every" in str(caught.value)


@pytest.mark.parametrize("every", ["2m", "10m", "24h", "90m", "1h"])
def test_the_interval_grammar_accepts_minutes_and_hours(every):
    assert parse(_tick(wake={"every": every})).wake.every == every


def test_the_empty_wake_object_has_every_null():
    wake = parse({"version": 1}).wake
    assert wake == Wake()
    assert wake.every is None and wake.error_reported == {}


def test_unknown_fields_are_preserved_at_every_level_and_round_trip():
    data = _tick(
        extra_top={"note": 1},
        handoffs={"01JGROUP": {"handed_at": "a", "next_due_at": "b", "extra_handoff": True}},
        worker_poll={"mylinuxpool-w1": {"next_due_at": "b", "extra_poll": [1]}},
        wake={"every": None, "extra_wake": "kept"},
    )
    state = parse(data)
    assert state.extra == {"extra_top": {"note": 1}}
    assert state.handoffs["01JGROUP"].extra == {"extra_handoff": True}
    assert state.worker_poll["mylinuxpool-w1"].extra == {"extra_poll": [1]}
    assert state.wake.extra == {"extra_wake": "kept"}
    assert state.to_dict() == {
        **data,
        "wake": {
            "every": None,
            "set_at": None,
            "missing_reported_at": None,
            "error_reported": {},
            "orphan_reported_at": None,
            "extra_wake": "kept",
        },
    }


def test_a_missing_file_counts_as_empty(tmp_path):
    assert load_or_empty(tmp_path / STATE_FILE_NAME) == empty()


def test_a_bad_file_is_not_empty(tmp_path):
    path = tmp_path / STATE_FILE_NAME
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SchemaError):
        load_or_empty(path)


def test_save_creates_the_directory_and_load_reads_it_back(tmp_path):
    path = tmp_path / "state" / STATE_FILE_NAME
    state = parse(_tick())
    save(path, state)
    assert json.loads(path.read_text(encoding="utf-8"))["wake"]["every"] == "10m"
    assert load_or_empty(path) == state


def test_a_missing_version_is_refused():
    data = _tick()
    del data["version"]
    with pytest.raises(SchemaError) as caught:
        parse(data)
    assert "'version' is missing" in str(caught.value)


@pytest.mark.parametrize(
    ["changes", "expect_in_message"],
    [
        ({"version": 3}, "must be 1"),
        ({"last_tick_at": 17}, "must be a string"),
        ({"handoffs": []}, "must be an object"),
        ({"worker_poll": "x"}, "must be an object"),
        ({"wake": []}, "not a JSON object"),
    ],
)
def test_wrong_top_level_fields_are_refused(changes, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_tick(**changes))
    assert expect_in_message in str(caught.value)


@pytest.mark.parametrize(
    ["handoff", "expect_in_message"],
    [
        ({}, "'handed_at' is missing"),
        ({"handed_at": "a"}, "'next_due_at' is missing"),
        ({"handed_at": 1, "next_due_at": "b"}, "must be a string"),
        ({"handed_at": "a", "next_due_at": "b", "last_reminded_at": 1}, "must be a string"),
        ("nope", "not a JSON object"),
    ],
)
def test_wrong_handoff_entries_are_refused(handoff, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_tick(handoffs={"01JGROUP": handoff}))
    assert expect_in_message in str(caught.value)


@pytest.mark.parametrize(
    ["poll", "expect_in_message"],
    [
        ({}, "'next_due_at' is missing"),
        ({"next_due_at": 1}, "must be a string"),
        ({"next_due_at": "b", "last_reported_at": []}, "must be a string"),
    ],
)
def test_wrong_worker_poll_entries_are_refused(poll, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_tick(worker_poll={"mylinuxpool-w1": poll}))
    assert expect_in_message in str(caught.value)


def test_error_reported_holds_one_string_code_per_job():
    with pytest.raises(SchemaError) as caught:
        parse(_tick(wake={"error_reported": {"job-1": 404}}))
    assert "not a string" in str(caught.value)
    with pytest.raises(SchemaError) as caught:
        parse(_tick(wake={"error_reported": ["job-1"]}))
    assert "must be an object" in str(caught.value)


def test_the_handoff_and_poll_records_round_trip():
    assert Handoff(handed_at="a", next_due_at="b").to_dict() == {"handed_at": "a", "next_due_at": "b"}
    assert WorkerPoll(next_due_at="b").to_dict() == {"next_due_at": "b"}
    assert Wake().to_dict() == {
        "every": None,
        "set_at": None,
        "missing_reported_at": None,
        "error_reported": {},
        "orphan_reported_at": None,
    }
