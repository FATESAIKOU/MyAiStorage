"""Unit tests for the local cursor store (D4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

from angareion.backend import Cursor, Target
from angareion.state import CursorStore, cursor_file_path


def test_cursor_file_path_uses_identity_slug(tmp_path):
    path = cursor_file_path(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    assert path == tmp_path / "cursor-owner-org_team__PM.json"


def test_missing_file_means_empty_cursor(tmp_path):
    store = CursorStore(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    assert not store.path.exists()
    assert store.cursor_for(Target(channel="general")) == Cursor()


def test_save_and_load_roundtrip_per_target(tmp_path):
    identity = {"repo": "owner-org/team", "role": "PM"}
    store = CursorStore(tmp_path, identity)
    channel_target = Target(channel="general")
    me_target = Target(recipient=identity)

    store.save(channel_target, Cursor(position="100", at="2026-10-10T12:00:00Z", seen=["98", "99", "100"]))
    store.save(me_target, Cursor(position="55", at="2026-10-10T13:00:00Z", seen=["55"]))

    reloaded = CursorStore(tmp_path, identity)
    assert reloaded.cursor_for(channel_target) == Cursor(position="100", at="2026-10-10T12:00:00Z", seen=["98", "99", "100"])
    assert reloaded.cursor_for(me_target) == Cursor(position="55", at="2026-10-10T13:00:00Z", seen=["55"])

    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data == {
        "channel:general": {"position": "100", "at": "2026-10-10T12:00:00Z", "seen": ["98", "99", "100"]},
        "to:owner-org_team__PM": {"position": "55", "at": "2026-10-10T13:00:00Z", "seen": ["55"]},
    }
    # The cursor file carries position/at/seen only: no ETag, no cached messages.
    assert all(set(entry) == {"position", "at", "seen"} for entry in data.values())


def test_corrupt_cursor_file_reads_as_empty(tmp_path):
    store = CursorStore(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.cursor_for(Target(channel="general")) == Cursor()

    # Saving repairs the file.
    store.save(Target(channel="general"), Cursor(position="7", at="2026-10-10T12:00:00Z", seen=["7"]))
    assert store.cursor_for(Target(channel="general")).position == "7"


def test_old_format_cursor_is_dropped_and_reported(tmp_path):
    """The pre-review {at, id} entry cannot be translated: read from the start and say so."""
    identity = {"repo": "owner-org/team", "role": "PM"}
    store = CursorStore(tmp_path, identity)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"channel:general": {"at": "2026-10-10T12:00:00+00:00", "id": "01SENDERULID"}}),
        encoding="utf-8",
    )

    target = Target(channel="general")
    assert store.cursor_for(target) == Cursor()
    assert store.dropped_old_format == {"channel:general"}

    # Saving replaces the old entry with the new format.
    store.save(target, Cursor(position="42", at="2026-10-10T12:01:00Z", seen=["42"]))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data == {"channel:general": {"position": "42", "at": "2026-10-10T12:01:00Z", "seen": ["42"]}}
    assert CursorStore(tmp_path, identity).dropped_old_format == set()


def test_deleting_the_file_means_reading_from_the_start(tmp_path):
    store = CursorStore(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    target = Target(channel="general")
    store.save(target, Cursor(position="7", at="2026-10-10T12:00:00Z", seen=["7"]))
    store.path.unlink()
    assert store.cursor_for(target) == Cursor()


def test_save_uses_a_unique_temp_file_in_the_same_directory(tmp_path):
    """L6: a fixed .tmp name would let two processes clobber each other's in-flight file."""
    store = CursorStore(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    target = Target(channel="general")

    other_process = store.path.with_suffix(".tmp")
    other_process.write_text("another process's in-flight cursor", encoding="utf-8")

    store.save(target, Cursor(position="7", at="2026-10-10T12:00:00Z", seen=["7"]))

    assert other_process.read_text(encoding="utf-8") == "another process's in-flight cursor"
    assert store.cursor_for(target).position == "7"
    # Our own temp file is gone: just the cursor and the other process's file remain.
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(store.path.stem))
    assert leftovers == sorted([store.path.name, other_process.name])
