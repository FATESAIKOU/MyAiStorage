"""Local cursor state (D4/v5): per target key, the backend's position/at/seen.

Nothing else lives here — no message cache, no acks. Deleting the file means
reading from the start. ETag and scan_since are not part of the cursor: they
live in the backend instance for one process (D2).

The cursor file is named after the agent name (v5); v0's `{repo}__{role}`
files are naturally abandoned — a new identity starts from the beginning.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from angareion.backend import Cursor
from angareion.config import identity_slug


def cursor_file_path(state_dir: Path | str, identity: str) -> Path:
    """Path of the cursor file for an agent name: cursor-<identity>.json."""
    return Path(state_dir) / f"cursor-{identity_slug(identity)}.json"


def channel_cursor_key(channel: str) -> str:
    """The cursor key for a channel target."""
    return f"channel:{channel}"


def recipient_cursor_key(identity: str, channel: str) -> str:
    """The cursor key for "to me" in one channel.

    "To me" spans every channel, and each channel is a different backend
    entity with its own arrival positions, so each channel keeps its own
    cursor entry.
    """
    return f"to:{identity_slug(identity)}:{channel}"


class CursorStore:
    """Reads and writes `cursor-<identity>.json` for one identity."""

    def __init__(
        self,
        state_dir: Path | str | None = None,
        identity: str | None = None,
        path: Path | str | None = None,
    ):
        if path is None:
            if state_dir is None or identity is None:
                raise ValueError("CursorStore needs either path, or state_dir and identity")
            path = cursor_file_path(Path(state_dir), identity)
        self.path = Path(path)
        # Targets whose entry was in the old ({at, id}) format: they are treated
        # as never read, and the CLI tells the user once per run.
        self.dropped_old_format: set[str] = set()

    def _load(self) -> dict[str, Cursor]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        cursors: dict[str, Cursor] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            if "position" not in value and "id" in value:
                # The pre-review format stored the sender's at/id; without a
                # backend position it cannot be translated, so read from the
                # start and say so once.
                self.dropped_old_format.add(key)
                continue
            seen = value.get("seen")
            cursors[key] = Cursor(
                position=value.get("position"),
                at=value.get("at"),
                seen=[str(item) for item in seen if isinstance(item, str)] if isinstance(seen, list) else [],
            )
        return cursors

    def cursor(self, key: str) -> Cursor:
        """The stored cursor for a target key; an empty cursor when never read."""
        return self._load().get(key, Cursor())

    def save(self, key: str, cursor: Cursor) -> None:
        """Store the cursor for one target key, atomically."""
        self.save_many({key: cursor})

    def save_many(self, cursors: dict[str, Cursor]) -> None:
        """Store several cursors at once (a "to me" read spans channels)."""
        current = self._load()
        current.update(cursors)
        payload: dict[str, Any] = {
            key: {"position": c.position, "at": c.at, "seen": list(c.seen)}
            for key, c in current.items()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # L6: a unique temp file in the same directory, so two processes
        # reading at the same time cannot overwrite each other's in-flight file.
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
