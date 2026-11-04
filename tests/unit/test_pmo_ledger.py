"""Unit tests for the pmo inbox ledger schema (`#110` T-1, design §3.4, §1.1, §1.2)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from pmo.errors import SchemaError
from pmo.ledger import Ledger, LedgerItem, load, parse, save

WORK_ID = "01JABCDEFGHJKMNPQRSTVWXYZ0"


def _item(**overrides: object) -> dict[str, object]:
    """One dispatch item with every field the design §1.2 table lists."""
    data: dict[str, object] = {
        "id": WORK_ID,
        "title": "pmo 骨架與 schema",
        "level": "d",
        "sender": "myaistorage-secretary-m1",
        "received_at": "2026-11-03T01:00:00Z",
        "ack_at": "2026-11-03T01:01:00Z",
        "schedule": "立即開工",
        "issue": "myaistorage#352",
        "status": "進行中",
        "reported_at": "2026-11-03T02:00:00Z",
        "pr": "myaistorage#356",
        "team": "myaistorage-m1",
    }
    data.update(overrides)
    return data


def _ledger(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "version": 1,
        "items": {WORK_ID: _item()},
        "notified": {"call": {"01JCALL": "2026-11-03T01:01:00Z"}, "poke": {"01JPOKE": "2026-11-03T01:02:00Z"}},
    }
    data.update(overrides)
    return data


def test_a_full_ledger_parses():
    ledger = parse(_ledger())
    assert list(ledger.items) == [WORK_ID]
    item = ledger.items[WORK_ID]
    assert item.item_id == WORK_ID
    assert item.title == "pmo 骨架與 schema"
    assert (item.level, item.sender) == ("d", "myaistorage-secretary-m1")
    assert (item.ack_at, item.schedule, item.issue) == (
        "2026-11-03T01:01:00Z",
        "立即開工",
        "myaistorage#352",
    )
    assert (item.status, item.reported_at, item.pr) == (
        "進行中",
        "2026-11-03T02:00:00Z",
        "myaistorage#356",
    )
    assert item.team == "myaistorage-m1"
    assert (ledger.notified_call, ledger.notified_poke) == (
        {"01JCALL": "2026-11-03T01:01:00Z"},
        {"01JPOKE": "2026-11-03T01:02:00Z"},
    )
    assert ledger.rebuilt_at is None


def test_an_empty_ledger_parses():
    ledger = parse({"version": 1, "items": {}, "notified": {"call": {}, "poke": {}}})
    assert ledger == Ledger(items={}, notified_call={}, notified_poke={})


def test_rebuilt_at_is_kept():
    ledger = parse(_ledger(rebuilt_at="2026-11-03T03:00:00Z"))
    assert ledger.rebuilt_at == "2026-11-03T03:00:00Z"
    assert ledger.to_dict()["rebuilt_at"] == "2026-11-03T03:00:00Z"


def test_unknown_fields_are_preserved_at_every_level_and_round_trip():
    data = _ledger(
        extra_top={"note": "kept"},
        items={WORK_ID: _item(extra_item={"by": "mypmo"})},
        notified={"call": {"01JCALL": "t1"}, "poke": {}, "extra_notified": [1, 2]},
    )
    ledger = parse(data)
    assert ledger.extra == {"extra_top": {"note": "kept"}}
    assert ledger.items[WORK_ID].extra == {"extra_item": {"by": "mypmo"}}
    assert ledger.notified_extra == {"extra_notified": [1, 2]}
    assert ledger.to_dict() == data


def test_save_creates_the_directory_and_load_reads_it_back(tmp_path):
    path = tmp_path / "state" / "ledger.json"
    ledger = parse(_ledger())
    save(path, ledger)
    assert json.loads(path.read_text(encoding="utf-8"))["items"][WORK_ID]["level"] == "d"
    assert load(path) == ledger


def test_a_missing_file_is_a_schema_error(tmp_path):
    """#381: `read_json` 把讀不起來（含不存在）當成壞檔，不是 `FileNotFoundError`。"""
    with pytest.raises(SchemaError) as caught:
        load(tmp_path / "nowhere.json")
    assert "cannot be read" in str(caught.value)


def test_invalid_json_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SchemaError) as caught:
        load(path)
    assert "not valid JSON" in str(caught.value)


def test_a_missing_required_field_is_refused():
    for missing in ("version", "items", "notified"):
        data = _ledger()
        del data[missing]
        with pytest.raises(SchemaError) as caught:
            parse(data)
        assert missing in str(caught.value)


def test_the_two_notified_objects_are_required():
    """Architect 中-3: `notified.call`／`poke` are id → time objects."""
    for missing in ("call", "poke"):
        data = _ledger(notified={"call": {}, "poke": {}})
        del data["notified"][missing]  # type: ignore[index]
        with pytest.raises(SchemaError) as caught:
            parse(data)
        assert missing in str(caught.value)


@pytest.mark.parametrize(
    ["changes", "expect_in_message"],
    [
        ({"version": 2}, "must be 1"),
        ({"items": []}, "must be an object"),
        ({"notified": []}, "must be an object"),
        ({"notified": {"call": "01JCALL", "poke": {}}}, "must be an object"),
        ({"notified": {"call": {}, "poke": [1]}}, "must be an object"),
        ({"notified": {"call": {"01JCALL": 1}, "poke": {}}}, "must be a string"),
        ({"rebuilt_at": 17}, "must be a string"),
    ],
)
def test_wrong_ledger_fields_are_refused(changes, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_ledger(**changes))
    assert expect_in_message in str(caught.value)


@pytest.mark.parametrize(
    ["changes", "expect_in_message"],
    [
        ({"id": "01JOTHER"}, "does not match its key"),
        ({"id": 3}, "must be a string"),
        ({"received_at": None}, "must be a string"),
        ({"title": 352}, "must be a string"),
        ({"level": ["d"]}, "must be a string"),
        ({"ack_at": 0}, "must be a string"),
        ({"team": {}}, "must be a string"),
    ],
)
def test_wrong_item_fields_are_refused(changes, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_ledger(items={WORK_ID: _item(**changes)}))
    assert expect_in_message in str(caught.value)


def test_an_item_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaError) as caught:
        parse(_ledger(items={WORK_ID: "nope"}))
    assert "not a JSON object" in str(caught.value)


def test_an_item_without_received_at_is_refused():
    item = _item()
    del item["received_at"]
    with pytest.raises(SchemaError) as caught:
        parse(_ledger(items={WORK_ID: item}))
    assert "'received_at' is missing" in str(caught.value)


def test_the_item_error_names_the_work_id():
    with pytest.raises(SchemaError) as caught:
        parse(_ledger(items={WORK_ID: _item(title=1)}))
    assert WORK_ID in str(caught.value)


def test_a_ledger_item_round_trips_through_to_dict():
    item = LedgerItem(item_id=WORK_ID, received_at="2026-11-03T01:00:00Z", extra={"kept": 1})
    assert item.to_dict() == {
        "id": WORK_ID,
        "received_at": "2026-11-03T01:00:00Z",
        "kept": 1,
    }
