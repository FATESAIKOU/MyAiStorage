"""Unit tests for Angareion local state and rebuild mechanism."""

from __future__ import annotations

import pytest

from angareion.messages import Message
from angareion.state import (
    LocalState,
    identity_slug,
    rebuild_from_backend,
    state_file_path,
)


class FakeBackend:
    """Fake backend stub simulating list_messages and channels."""

    def __init__(self, messages: list[Message] | None = None, channels: dict[str, int] | None = None):
        self.messages = messages or []
        self.channels = channels or {}

    def list_messages(self, since: str | None = None) -> tuple[list[Message], str]:
        # Return (messages, etag)
        return self.messages, 'W/"etag-12345"'

    def list_channels(self) -> list[tuple[str, int]]:
        return list(self.channels.items())


def test_identity_slug():
    id_member = {"repo": "owner-org/team", "role": "PM"}
    assert identity_slug(id_member) == "owner-org_team__PM"

    id_group = {"group": "all-workers"}
    assert identity_slug(id_group) == "group__all-workers"


def test_save_and_load_state(tmp_path):
    identity = {"repo": "owner-org/team", "role": "PM"}
    path = state_file_path(tmp_path, identity)

    state = LocalState(
        identity=identity,
        since="2026-10-10T12:00:00Z",
        etags={"messages": 'W/"abc"'},
        acked={"01JABC1", "01JABC2"},
        channels={"phase-1": 10, "phase-2": 20},
        path=path,
    )
    state.save()

    assert path.is_file()

    loaded = LocalState.load(path, identity=identity)
    assert loaded.identity == identity
    assert loaded.since == "2026-10-10T12:00:00Z"
    assert loaded.etags == {"messages": 'W/"abc"'}
    assert loaded.acked == {"01JABC1", "01JABC2"}
    assert loaded.channels == {"phase-1": 10, "phase-2": 20}


def test_rebuild_state_from_backend(tmp_path):
    identity = {"repo": "owner-org/team", "role": "PM"}
    state_file = state_file_path(tmp_path, identity)

    # Prepare fake messages on channel
    msg1 = Message(
        from_={"repo": "owner-org/team", "role": "worker"},
        to=identity,
        channel="phase-1",
        urgency=5,
        content="Task started",
        id="01MSG0001",
        at="2026-10-10T10:00:00Z",
    )
    setattr(msg1, "issue_number", 42)

    msg2 = Message(
        from_=identity,
        to={"repo": "owner-org/team", "role": "worker"},
        channel="phase-1",
        urgency=5,
        content="Acked task",
        id="01MSG0002",
        at="2026-10-10T11:00:00Z",
        ack="01MSG0001",
    )
    setattr(msg2, "issue_number", 42)

    backend = FakeBackend(messages=[msg1, msg2], channels={"phase-1": 42, "phase-2": 55})

    # Rebuild from backend
    rebuilt = rebuild_from_backend(backend, identity=identity, state_dir=tmp_path)

    assert state_file.is_file()
    assert rebuilt.channels == {"phase-1": 42, "phase-2": 55}
    assert "01MSG0001" in rebuilt.acked
    assert rebuilt.since == "2026-10-10T11:00:00Z"
    assert rebuilt.etags.get("messages") == 'W/"etag-12345"'

    # Verify deleting state file and rebuilding again recovers everything
    state_file.unlink()
    assert not state_file.is_file()

    rebuilt2 = rebuild_from_backend(backend, identity=identity, state_dir=tmp_path)
    assert state_file.is_file()
    assert rebuilt2.channels == rebuilt.channels
    assert rebuilt2.acked == rebuilt.acked
    assert rebuilt2.since == rebuilt.since
