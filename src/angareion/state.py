"""Local cursor state (D4): per target, the backend's position/at/seen.

Nothing else lives here — no message cache, no acks. Deleting the file means
reading from the start. ETag and scan_since are not part of the cursor: they
live in the backend instance for one process (D2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from angareion.backend import Cursor, Target
from angareion.config import identity_slug


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

    def cursor_for(self, target: Target) -> Cursor:
        """The stored cursor for a target; an empty cursor when never read."""
        return self._load().get(target.key(), Cursor())

    def save(self, target: Target, cursor: Cursor) -> None:
        """Store the cursor for a target, atomically."""
        cursors = self._load()
        cursors[target.key()] = cursor
        payload = {
            key: {"position": c.position, "at": c.at, "seen": list(c.seen)}
            for key, c in cursors.items()
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
