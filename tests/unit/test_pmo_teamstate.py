"""Unit tests for the pmo team state schema (`#110` T-1, design §3.4, §1.1).

The field names are MyPMO's (`pmo/state/README.md`, the authority design §3.4
names): the file is shared with MyPMO during the transition (Q-9), so a file
MyPMO wrote has to read here unchanged.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from pmo.errors import SchemaError
from pmo.teamstate import TeamState, load, parse, save


def _state(**overrides: object) -> dict[str, object]:
    """One dispatch's team state, with the fields MyPMO's README names."""
    data: dict[str, object] = {
        "agent": "myaistorage-pm-m1",
        "state": "running",
        "exit": {"reason": "done"},
        "ticket": "myaistorage#352",
        "message_id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "dispatched_at": "2026-11-03T00:59:00Z",
        "received_at": "2026-11-03T01:00:02Z",
        "ack_at": "2026-11-03T01:04:00Z",
        "ack_poke_at": "2026-11-03T01:14:00Z",
        "ack_escalated_at": "2026-11-03T01:24:00Z",
    }
    data.update(overrides)
    return data


def test_a_full_state_parses():
    state = parse(_state(version=1))
    assert state.version == 1
    assert state.agent == "myaistorage-pm-m1"
    assert state.state == "running"
    assert state.exit == {"reason": "done"}
    assert state.ticket == "myaistorage#352"
    assert state.message_id == "01JABCDEFGHJKMNPQRSTVWXYZ0"
    assert state.dispatched_at == "2026-11-03T00:59:00Z"
    assert state.received_at == "2026-11-03T01:00:02Z"
    assert state.ack_at == "2026-11-03T01:04:00Z"
    assert state.ack_poke_at == "2026-11-03T01:14:00Z"
    assert state.ack_escalated_at == "2026-11-03T01:24:00Z"
    assert state.extra == {}


def test_a_file_mympo_wrote_has_no_version_and_still_reads():
    """Architect 中-1: MyPMO's team state files carry no `version` (they must read)."""
    state = parse(_state())
    assert state.version is None
    # ... and the canonical form does not add one back (no stranger's field).
    assert "version" not in state.to_dict()
    assert state.to_dict() == _state()


def test_a_version_that_is_present_must_still_be_the_integer_one():
    assert parse(_state(version=1)).to_dict()["version"] == 1
    for bad in (2, "1", True, None, 1.0, 0):
        with pytest.raises(SchemaError) as caught:
            parse(_state(version=bad))
        assert "version" in str(caught.value)


def test_an_empty_state_file_parses():
    assert parse({}) == TeamState()


def test_the_exit_object_and_the_ack_times_may_be_null():
    state = parse({"exit": None, "ack_at": None, "ack_poke_at": None, "ack_escalated_at": None})
    assert state.exit is None
    assert (state.ack_at, state.ack_poke_at, state.ack_escalated_at) == (None, None, None)


def test_unknown_fields_are_preserved_and_round_trip():
    # `session` 自 #358 T5-2（裁定第 11 條）起是已定型別的欄位，不再是 unknown；
    # 其餘不認得的欄位照舊原樣保留（Q-9）。
    data = _state(version=1, session="agora:ses-1", closed=True, worker_note={"by": "mypmo"})
    state = parse(data)
    assert state.session == "agora:ses-1"
    assert state.extra == {
        "closed": True,
        "worker_note": {"by": "mypmo"},
    }
    assert state.to_dict() == data


def test_save_creates_the_directory_and_load_reads_it_back(tmp_path):
    path = tmp_path / "state" / "myaistorage-m1.json"
    state = parse(_state())
    save(path, state)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["agent"] == "myaistorage-pm-m1"
    assert "version" not in written
    assert load(path) == state


def test_a_missing_file_is_a_file_error_not_a_schema_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nowhere.json")


def test_a_file_that_is_not_one_json_object_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(SchemaError) as caught:
        load(path)
    assert "one JSON object" in str(caught.value)


def test_invalid_json_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"state": "running",', encoding="utf-8")
    with pytest.raises(SchemaError) as caught:
        load(path)
    assert "not valid JSON" in str(caught.value)


@pytest.mark.parametrize(
    ["changes", "expect_in_message"],
    [
        ({"agent": 3}, "must be a string"),
        ({"state": ["running"]}, "must be a string"),
        ({"exit": "running"}, "must be an object or null"),
        ({"exit": [1]}, "must be an object or null"),
        ({"ticket": 352}, "must be a string"),
        ({"message_id": {}}, "must be a string"),
        ({"dispatched_at": 1.5}, "must be a string"),
        ({"received_at": False}, "must be a string"),
        ({"ack_at": 17}, "must be a string or null"),
        ({"ack_poke_at": ["t"]}, "must be a string"),
        ({"ack_escalated_at": 0}, "must be a string or null"),
    ],
)
def test_wrong_field_types_are_refused(changes, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_state(**changes))
    assert expect_in_message in str(caught.value)


def test_a_file_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaError) as caught:
        parse([{"state": "running"}])
    assert "not a JSON object" in str(caught.value)


# --------------------------------------------------------------------------- #
# 每個 team 一把鎖（arch review 中-1）：跨進程排他、同一行程可重入、依 key 分開
# --------------------------------------------------------------------------- #


def test_the_team_lock_is_reentrant_and_exclusive(tmp_path):
    import fcntl
    import os

    from pmo import teamstate

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = teamstate.lock_file(state_dir, "demo2-m1")
    assert lock.name == "demo2-m1.json.lock"

    # 同一線程可重入（`send` 與 tick 的 ack 比對都在鎖內呼叫 load／save）
    with teamstate.state_lock(state_dir, "demo2-m1"):
        with teamstate.state_lock(state_dir, "demo2-m1"):
            pass

    # 拿鎖期間別的 fd 拿不到（跨進程排他）
    with teamstate.state_lock(state_dir, "demo2-m1"):
        assert lock.exists()
        fd = os.open(str(lock), os.O_RDWR)
        try:
            with pytest.raises((BlockingIOError, OSError)):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # 放掉之後拿得到
    fd = os.open(str(lock), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_the_team_lock_is_per_team_key(tmp_path):
    """兩個 team 的鎖各自獨立：不能因為一個 team 在寫就鎖住另一個。"""
    from pmo import teamstate

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with teamstate.state_lock(state_dir, "demo2-m1"):
        with teamstate.state_lock(state_dir, "demo3-m1"):
            assert teamstate.lock_file(state_dir, "demo2-m1").exists()
            assert teamstate.lock_file(state_dir, "demo3-m1").exists()
