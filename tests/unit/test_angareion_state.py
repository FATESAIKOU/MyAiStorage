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

    store.save(channel_target, Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA"))
    store.save(me_target, Cursor(at="2026-10-10T13:00:00+00:00", id="01BBBB"))

    reloaded = CursorStore(tmp_path, identity)
    assert reloaded.cursor_for(channel_target) == Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA")
    assert reloaded.cursor_for(me_target) == Cursor(at="2026-10-10T13:00:00+00:00", id="01BBBB")

    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data == {
        "channel:general": {"at": "2026-10-10T12:00:00+00:00", "id": "01AAAA"},
        "to:owner-org_team__PM": {"at": "2026-10-10T13:00:00+00:00", "id": "01BBBB"},
    }
    # The cursor file carries at+id only: no ETag, no cached messages.
    assert all(set(entry) == {"at", "id"} for entry in data.values())


def test_corrupt_cursor_file_reads_as_empty(tmp_path):
    store = CursorStore(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.cursor_for(Target(channel="general")) == Cursor()

    # Saving repairs the file.
    store.save(Target(channel="general"), Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA"))
    assert store.cursor_for(Target(channel="general")).id == "01AAAA"


def test_deleting_the_file_means_reading_from_the_start(tmp_path):
    store = CursorStore(tmp_path, {"repo": "owner-org/team", "role": "PM"})
    target = Target(channel="general")
    store.save(target, Cursor(at="2026-10-10T12:00:00+00:00", id="01AAAA"))
    store.path.unlink()
    assert store.cursor_for(target) == Cursor()
