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
import sys
import tempfile
from typing import Any, Callable

from angareion.backend import Cursor, ETagRecord
from angareion.config import identity_slug


def cursor_file_path(state_dir: Path | str, identity: str) -> Path:
    """Path of the cursor file for an agent name: cursor-<identity>.json."""
    return Path(state_dir) / f"cursor-{identity_slug(identity)}.json"


WAIT_STATE_VERSION = 2  # 2: "to me" also counts a mention (#209); a v1 ETag must not answer for it


def wait_state_file_path(state_dir: Path | str, identity: str) -> Path:
    """Path of the wait/ETag state file for an agent name: wait-<identity>.json."""
    return Path(state_dir) / f"wait-{identity_slug(identity)}.json"


def _warn_to_stderr(message: str) -> None:
    sys.stderr.write(f"Warning: {message}\n")


def _record_to_dict(record: ETagRecord) -> dict[str, Any]:
    return {
        "url": record.url,
        "cursor": {
            "position": record.cursor.position,
            "at": record.cursor.at,
            "seen": list(record.cursor.seen),
        },
        "etag": record.etag,
    }


def _record_from_dict(value: Any) -> ETagRecord | None:
    """A usable record, or None when the entry is malformed (it is just a cache)."""
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    etag = value.get("etag")
    cursor = value.get("cursor")
    if not isinstance(url, str) or not url or not isinstance(etag, str) or not etag:
        return None
    if not isinstance(cursor, dict):
        return None
    position = cursor.get("position")
    at = cursor.get("at")
    seen = cursor.get("seen", [])
    if position is not None and not isinstance(position, str):
        return None
    if at is not None and not isinstance(at, str):
        return None
    if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
        return None
    return ETagRecord(url=url, cursor=Cursor(position=position, at=at, seen=list(seen)), etag=etag)


class FileETagCache:
    """The wait/ETag state persisted across `wait` runs (D2/#96).

    The file is `wait-<agent name>.json` next to the cursor file. It only holds
    responses that were fully scanned and found clean (see `github.check`);
    `read` never touches it and deleting it only means full queries. A missing,
    corrupt, future-version or partly malformed file is treated as empty and is
    rewritten on the next put/drop. A failed write is reported once (or to the
    injected `warn`) and otherwise ignored: the cache must never fail a wait.
    """

    def __init__(
        self,
        state_dir: Path | str | None = None,
        identity: str | None = None,
        path: Path | str | None = None,
        warn: Callable[[str], None] | None = None,
    ):
        if path is None:
            if state_dir is None or identity is None:
                raise ValueError("FileETagCache needs either path, or state_dir and identity")
            path = wait_state_file_path(Path(state_dir), identity)
        self.path = Path(path)
        self._warn = warn or _warn_to_stderr
        self._warned_write_failure = False

    def _load(self) -> tuple[dict[str, ETagRecord], bool]:
        """The usable records, and whether the file needs rewriting (bad content)."""
        if not self.path.is_file():
            return {}, False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}, True
        if not isinstance(data, dict) or data.get("version") != WAIT_STATE_VERSION:
            return {}, True
        targets = data.get("targets")
        if not isinstance(targets, dict):
            return {}, True
        records: dict[str, ETagRecord] = {}
        needs_rewrite = False
        for key, value in targets.items():
            record = _record_from_dict(value)
            if record is None:
                needs_rewrite = True
                continue
            records[str(key)] = record
        return records, needs_rewrite

    def get(self, key: str) -> ETagRecord | None:
        records, _ = self._load()
        return records.get(key)

    def put(self, key: str, record: ETagRecord) -> None:
        records, needs_rewrite = self._load()
        if not needs_rewrite and records.get(key) == record:
            return
        records[key] = record
        self._write(records)

    def drop(self, key: str) -> None:
        records, needs_rewrite = self._load()
        if key not in records and not needs_rewrite:
            return
        records.pop(key, None)
        self._write(records)

    def _write(self, records: dict[str, ETagRecord]) -> None:
        payload = {
            "version": WAIT_STATE_VERSION,
            "targets": {key: _record_to_dict(record) for key, record in records.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # L6 (same as CursorStore): a unique temp file in the same directory,
            # so two waits writing at once cannot leave a half-written file.
            fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as e:
            self._report_write_failure(e)

    def _report_write_failure(self, error: OSError) -> None:
        """Say it once: the state file is an optimisation, so a failed write is not fatal."""
        if self._warned_write_failure:
            return
        self._warned_write_failure = True
        self._warn(
            f"could not write the wait state file {self.path} ({error}); "
            "waits will use full queries until it can be written. Messages are not affected."
        )


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


def mention_cursor_key(identity: str, channel: str) -> str:
    """The cursor key for "@ me" in one channel.

    A dimension of its own (like `to:`, one entry per channel): reading "@ me"
    never moves the "to me" or the channel cursor, and the other way round.
    """
    return f"mention:{identity_slug(identity)}:{channel}"


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
