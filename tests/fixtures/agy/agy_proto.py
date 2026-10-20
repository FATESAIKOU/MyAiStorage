"""Minimal protobuf wire-format reader for the synthetic agy fixture dbs.

The real agy (Antigravity CLI) databases store every piece of interesting content
inside `steps.step_payload` as an unencrypted protobuf of unknown message types.
Field numbers were resolved by probing fake conversations generated only from fake
content (see docs/agy-storage.md); this module holds just the two things PR1 needs
in tests: a wire-format walker and the resolved field paths.

Nothing here talks to a real agy home; it only opens SQLite files the caller points
at (pytest runs inside the isolated test environment).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field as dc_field

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def fields(buf: bytes) -> list[tuple[int, int, object]]:
    """Parse a wire-format message into (field_number, wire_type, value) triples."""
    out: list[tuple[int, int, object]] = []
    i, end = 0, len(buf)
    while i < end:
        key, i = read_varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == WIRE_VARINT:
            value, i = read_varint(buf, i)
        elif wt == WIRE_64BIT:
            value, i = buf[i:i + 8], i + 8
        elif wt == WIRE_32BIT:
            value, i = buf[i:i + 4], i + 4
        elif wt == WIRE_LEN:
            length, i = read_varint(buf, i)
            value = buf[i:i + length]
            i += length
        else:
            raise ValueError(f"unsupported wire type {wt} in field {num}")
        out.append((num, wt, value))
    if i != end:
        raise ValueError("trailing bytes after protobuf message")
    return out


def at(msg: bytes, *path: int) -> list[bytes]:
    """Follow a field-number chain and return every hit (len-delimited only)."""
    current = [msg]
    for num in path:
        nxt = []
        for blob in current:
            nxt += [v for n, wt, v in fields(blob) if n == num and wt == WIRE_LEN]
        current = nxt
    return current


def first_at(msg: bytes, *path: int) -> bytes:
    hits = at(msg, *path)
    if not hits:
        raise LookupError(f"path {path} not present in message")
    return hits[0]


def text_of(msg: bytes, *path: int) -> str:
    """Text at the end of a field-number chain (the paths store UTF-8 strings)."""
    return first_at(msg, *path).decode("utf-8")


# step_type values observed in fake conversations; see docs/agy-storage.md.
STEP_USER = 14
STEP_MODEL = 15
STEP_TOOL = 132

# Resolved payload paths (every path from the steps.step_payload root).
PATH_USER_TEXT = (19, 2)
PATH_MODEL_TEXT = (20, 1)
PATH_MODEL_TOOL_NAME = (20, 7, 2)
PATH_MODEL_TOOL_ARGS = (20, 7, 3)
PATH_TOOL_NAME = (5, 4, 2)
PATH_TOOL_CALL_ID = (5, 4, 1)
PATH_TOOL_ARGS = (5, 4, 3)
PATH_TOOL_OUTPUT = (140, 2, 1)
PATH_IDS = (5, 20)  # inner: 1 = trajectory_id, 4 = cascade_id


@dataclass
class StepRow:
    idx: int
    step_type: int
    status: int
    has_subtrajectory: int
    metadata: bytes | None
    step_payload: bytes
    step_format: int

    @property
    def tool_name(self) -> str | None:
        if self.step_type == STEP_MODEL:
            path = PATH_MODEL_TOOL_NAME
        elif self.step_type == STEP_TOOL:
            path = PATH_TOOL_NAME
        else:
            return None
        try:
            return text_of(self.step_payload, *path)
        except LookupError:  # text-only model step / unknown shape
            return None

    @property
    def tool_args(self) -> str | None:
        if self.step_type == STEP_MODEL:
            path = PATH_MODEL_TOOL_ARGS
        elif self.step_type == STEP_TOOL:
            path = PATH_TOOL_ARGS
        else:
            return None
        try:
            return text_of(self.step_payload, *path)
        except LookupError:
            return None

    @property
    def embedded_ids(self) -> dict[str, str]:
        """trajectory_id / cascade_id inside every step's context envelope.

        Empty for step shapes that do not carry the envelope (unknown types).
        """
        try:
            return {"trajectory_id": text_of(self.step_payload, *PATH_IDS, 1),
                    "cascade_id": text_of(self.step_payload, *PATH_IDS, 4)}
        except LookupError:
            return {}


def read_steps(db: str, *, immutable: bool = True) -> list[StepRow]:
    """Read every step from a generated (or previously snapshotted) agy-format db.

    Opens read-only via immutable=1 because agy databases without a live writer
    have no sidecar files; that constraint is exactly what PR1 must live with.
    For a live-writer snapshot (db + -wal copied without -shm, with_wal mode)
    pass immutable=False: replay of the uncheckpointed wal needs an ordinary
    connection (it will create the -shm; run it on a copy, never beside a live
    agy).
    """
    uri = f"file:{db}" + ("?immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT idx, step_type, status, has_subtrajectory, metadata, "
            "step_payload, step_format FROM steps ORDER BY idx").fetchall()
    finally:
        con.close()
    return [StepRow(*r) for r in rows]
