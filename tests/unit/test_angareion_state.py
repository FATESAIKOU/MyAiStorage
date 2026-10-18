"""Unit tests for the local cursor store (D4/v5): key-based, one file per agent name."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

from angareion.backend import Cursor
from angareion.state import (
    CursorStore,
    channel_cursor_key,
    cursor_file_path,
    recipient_cursor_key,
)

ME = "mylinuxpool-impl-w1-1"


def test_cursor_file_path_uses_the_agent_name(tmp_path):
    path = cursor_file_path(tmp_path, ME)
    assert path == tmp_path / "cursor-mylinuxpool-impl-w1-1.json"


def test_cursor_keys(tmp_path):
    assert channel_cursor_key("general") == "channel:general"
    assert recipient_cursor_key(ME, "general") == "to:mylinuxpool-impl-w1-1:general"


def test_missing_file_means_empty_cursor(tmp_path):
    store = CursorStore(tmp_path, ME)
    assert not store.path.exists()
    assert store.cursor("channel:general") == Cursor()


def test_save_and_load_roundtrip_per_key(tmp_path):
    store = CursorStore(tmp_path, ME)
    store.save("channel:general", Cursor(position="100", at="2026-10-10T12:00:00Z", seen=["98", "99", "100"]))
    store.save("to:mylinuxpool-impl-w1-1:general", Cursor(position="55", at="2026-10-10T13:00:00Z", seen=["55"]))

    reloaded = CursorStore(tmp_path, ME)
    assert reloaded.cursor("channel:general") == Cursor(
        position="100", at="2026-10-10T12:00:00Z", seen=["98", "99", "100"]
    )
    assert reloaded.cursor("to:mylinuxpool-impl-w1-1:general") == Cursor(
        position="55", at="2026-10-10T13:00:00Z", seen=["55"]
    )

    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data == {
        "channel:general": {"position": "100", "at": "2026-10-10T12:00:00Z", "seen": ["98", "99", "100"]},
        "to:mylinuxpool-impl-w1-1:general": {"position": "55", "at": "2026-10-10T13:00:00Z", "seen": ["55"]},
    }
    # The cursor file carries position/at/seen only: no ETag, no cached messages.
    assert all(set(entry) == {"position", "at", "seen"} for entry in data.values())


def test_save_many_writes_every_cursor_at_once(tmp_path):
    store = CursorStore(tmp_path, ME)
    store.save_many(
        {
            "to:mylinuxpool-impl-w1-1:general": Cursor(position="1", at="2026-10-10T12:00:00Z", seen=["1"]),
            "to:mylinuxpool-impl-w1-1:dev": Cursor(position="9", at="2026-10-10T12:00:01Z", seen=["9"]),
        }
    )
    assert store.cursor("to:mylinuxpool-impl-w1-1:general").position == "1"
    assert store.cursor("to:mylinuxpool-impl-w1-1:dev").position == "9"


def test_old_v0_cursor_file_is_naturally_abandoned(tmp_path):
    """v5 names the file after the agent name; the v0 `{repo}__{role}` file is never read."""
    old_file = tmp_path / "cursor-owner-org_team__PM.json"
    old_file.write_text(
        json.dumps({"channel:general": {"position": "7", "at": "2026-10-10T12:00:00Z", "seen": ["7"]}}),
        encoding="utf-8",
    )

    store = CursorStore(tmp_path, ME)
    assert store.path == tmp_path / "cursor-mylinuxpool-impl-w1-1.json"
    assert store.cursor("channel:general") == Cursor()  # starts from the beginning
    assert store.dropped_old_format == set()  # the old file was not even opened

    store.save("channel:general", Cursor(position="42", at="2026-10-10T12:01:00Z", seen=["42"]))
    # The old file is untouched and the new one only holds the new cursor.
    assert json.loads(old_file.read_text(encoding="utf-8"))["channel:general"]["position"] == "7"
    assert store.cursor("channel:general").position == "42"


def test_corrupt_cursor_file_reads_as_empty(tmp_path):
    store = CursorStore(tmp_path, ME)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.cursor("channel:general") == Cursor()

    # Saving repairs the file.
    store.save("channel:general", Cursor(position="7", at="2026-10-10T12:00:00Z", seen=["7"]))
    assert store.cursor("channel:general").position == "7"


def test_old_in_file_format_is_dropped_and_reported(tmp_path):
    """The pre-review {at, id} entry cannot be translated: read from the start and say so."""
    store = CursorStore(tmp_path, ME)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"channel:general": {"at": "2026-10-10T12:00:00+00:00", "id": "01SENDERULID"}}),
        encoding="utf-8",
    )

    assert store.cursor("channel:general") == Cursor()
    assert store.dropped_old_format == {"channel:general"}

    # Saving replaces the old entry with the new format.
    store.save("channel:general", Cursor(position="42", at="2026-10-10T12:01:00Z", seen=["42"]))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data == {"channel:general": {"position": "42", "at": "2026-10-10T12:01:00Z", "seen": ["42"]}}
    assert CursorStore(tmp_path, ME).dropped_old_format == set()


def test_deleting_the_file_means_reading_from_the_start(tmp_path):
    store = CursorStore(tmp_path, ME)
    store.save("channel:general", Cursor(position="7", at="2026-10-10T12:00:00Z", seen=["7"]))
    store.path.unlink()
    assert store.cursor("channel:general") == Cursor()


def test_save_uses_a_unique_temp_file_in_the_same_directory(tmp_path):
    """L6: a fixed .tmp name would let two processes clobber each other's in-flight file."""
    store = CursorStore(tmp_path, ME)
    other_process = store.path.with_suffix(".tmp")
    other_process.write_text("another process's in-flight cursor", encoding="utf-8")

    store.save("channel:general", Cursor(position="7", at="2026-10-10T12:00:00Z", seen=["7"]))

    assert other_process.read_text(encoding="utf-8") == "another process's in-flight cursor"
    assert store.cursor("channel:general").position == "7"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(store.path.stem))
    assert leftovers == sorted([store.path.name, other_process.name])
