"""Local cursor state (D4): per target, the last read message's (at, id).

Nothing else lives here — no message cache, no acks. Deleting the file means
reading from the start. The ETag used by `check` is *not* stored: it lives in
the backend instance for one process, so `wait` never writes the cursor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from angareion.backend import Cursor, Target
from angareion.config import get_state_dir, identity_slug


def cursor_file_path(state_dir: Path | str, identity: dict[str, Any]) -> Path:
    """Path of the cursor file for an identity: cursor-<identity>.json."""
    return Path(state_dir) / f"cursor-{identity_slug(identity)}.json"


class CursorStore:
    """Reads and writes `cursor-<identity>.json` for one identity."""

    def __init__(
        self,
        state_dir: Path | str | None = None,
        identity: dict[str, Any] | None = None,
        path: Path | str | None = None,
    ):
        if path is None:
            if state_dir is None or identity is None:
                raise ValueError("CursorStore needs either path, or state_dir and identity")
            path = cursor_file_path(Path(state_dir), identity)
        self.path = Path(path)

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
            if isinstance(value, dict):
                cursors[key] = Cursor(at=value.get("at"), id=value.get("id"))
        return cursors

    def cursor_for(self, target: Target) -> Cursor:
        """The stored cursor for a target; an empty cursor when never read."""
        return self._load().get(target.key(), Cursor())

    def save(self, target: Target, cursor: Cursor) -> None:
        """Store the cursor for a target, atomically."""
        cursors = self._load()
        cursors[target.key()] = cursor
        payload = {key: {"at": c.at, "id": c.id} for key, c in cursors.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.path.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp_file.replace(self.path)
