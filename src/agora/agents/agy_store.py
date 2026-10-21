"""The agy (Antigravity CLI) adapter's directory-and-database access layer.

**Everything this layer writes stays inside `$AGORA_AGY_HOME`** - the
conversation dbs under `conversations/` and `conversation_summaries.db` - and
a test always points `AGORA_AGY_HOME` at a temporary fake home, so the user's
real agy directory is only ever touched by the person running it themselves
(#26; the restore/summaries half is PR2b).

This file holds the paths (`home`／`conversations_dir`／`summaries_path`／
`db_path`), the snapshot (`snapshot` with its fingerprints/copy/`VACUUM INTO`
replay), the read side (`rows`／`trajectory_id`／`cascade_id`), the
credential-scan surface (`scan_text`／`scan_strings` - PR2b moved it here from
agy_wire, which is now pure byte-only functions on base) and the raw envelope
(`pack_raw_file`／`envelope`／`decode_db` - PR2b moved it here from agy.py).

Since PR2b's review (W5) the file is split further and the dependency
direction is one-way: this layer → `agy_summaries` (the summaries list and the
row a continue adds) → `agy_restore` (restore／`prepare_continue`). Nothing
here imports the two upper layers: the step-count cross-check against
conversation_summaries.db lives in `agy.py`, its caller (`snapshot` no longer
knows summaries; review N3), so there is no upward edge at all.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from agora.agents.agy_wire import fields
from agora.agents.base import AgentError
from agora.private_tmp import PrivateTmpDir

COPY_TRIES = 3             # a snapshot is retried at most this many times

FORMAT = "agy-conversation/1"

PREVIEW_MAX = 2000         # last_message/preview text cap, the same as claude's

#: base64 input chunk (`pack_raw_file`／`decode_db` walk the db by it): a
#: multiple of 3, so every chunk ends on a 4-character boundary of the
#: encoding.
B64_CHUNK = 3 * 32768


def home() -> Path:
    """Where agy keeps everything; `AGORA_AGY_HOME` overrides it (tests)."""
    return Path(os.environ.get("AGORA_AGY_HOME") or "~/.gemini/antigravity-cli").expanduser()


def conversations_dir() -> Path:
    return home() / "conversations"


def summaries_path() -> Path:
    return home() / "conversation_summaries.db"


def db_path(session_id: str) -> Path:
    """The conversation db, refusing an id that could walk out of conversations/.

    The id comes from the command line, and a path is not an id: one `..`
    would turn "import this conversation" into "read any file under the home".
    A NUL byte and a name past the filesystem's 255-byte limit are refused the
    same way (`AgentError`, review N3) - left to `Path`／`stat` they would leave
    the command as an unexpected `ValueError`／`OSError` instead of a clean
    error. `Path()` itself rejects a NUL, so that check comes first.
    """
    if (not session_id or "\x00" in session_id or len(session_id) > 255
            or Path(session_id).name != session_id or "\\" in session_id
            or session_id in (".", "..")):
        raise AgentError(f"agy 的 conversation id 不合法：{session_id!r}")
    return conversations_dir() / f"{session_id}.db"


def file_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def move_fingerprint(path: Path) -> tuple | None:
    """The cheap "did the conversation move" mark: (db size, db mtime_ns,
    wal size or 0, wal mtime_ns or 0) - `cli._fingerprint`'s agy branch.

    A turn in progress grows the `-wal` before the main file changes, so the
    wal pair is part of what a periodic round compares, and the 35 MB export
    is skipped when nothing moved. A missing wal *and* a 0-byte one are the
    same "nothing pending" state - the empty wal is what SQLite leaves after
    its last connection closes, and its mtime carries nothing the size and the
    main pair do not - so both report 0/0; a db that cannot be stat'ed is None,
    and the export is the thing that will say so.
    """
    main = file_fingerprint(path)
    if main is None:
        return None
    wal = file_fingerprint(path.with_name(path.name + "-wal"))
    return (*main, *(wal if wal and wal[0] > 0 else (0, 0)))


# ---------------------------------------------------------------------------
# snapshot: one consistent, single-file copy
# ---------------------------------------------------------------------------


def copy_stable(db: Path, dest_dir: Path) -> bool:
    """Copy `db` (+ its `-wal` when there is one) into `dest_dir`; True if a wal came.

    `(size, mtime_ns)` before and after is the whole consistency argument: the
    main file only changes at a checkpoint and the wal only grows or vanishes,
    so a pair that moved means the copy may straddle a checkpoint. A vanished
    wal is not an error - the next attempt takes the idle single-file path.

    `-shm` is deliberately never copied: it is a wal-index cache SQLite rebuilds
    on the first connection, so copying it would add a third file to check and
    buy nothing.

    A `FileNotFoundError` is the same race in its most literal form - agy's
    last connection closes and its checkpoint removes the `-wal`, or the whole
    conversation is deleted - so that attempt is abandoned and the next one
    re-reads the pair (a vanished wal lands on the idle single-file path).
    """
    wal = db.with_name(db.name + "-wal")
    target = dest_dir / "snapshot.db"
    for _ in range(COPY_TRIES):
        before = (file_fingerprint(db), file_fingerprint(wal))
        live = before[1] is not None and before[1][0] > 0
        for stale in (target, target.with_name(target.name + "-wal"),
                      target.with_name(target.name + "-shm")):
            stale.unlink(missing_ok=True)
        try:
            shutil.copyfile(db, target)
            if live:
                shutil.copyfile(wal, target.with_name(target.name + "-wal"))
        except FileNotFoundError:
            continue
        if (file_fingerprint(db), file_fingerprint(wal)) == before:
            return live
    raise AgentError(
        f"agy 對話在複製時一直變動（試了 {COPY_TRIES} 次）：{db.name}；"
        "可能有一輪正在寫入，或檔案正在被刪除，等它結束再試")


def immutable(path: Path) -> sqlite3.Connection:
    """A read-only connection to a single-file snapshot (never to a live db)."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)


def quick_check(path: Path) -> None:
    try:
        with closing(immutable(path)) as connection:
            result = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照開不起來：{e}")
    if result != ["ok"]:
        raise AgentError(f"agy 快照 quick_check 沒過：{'；'.join(map(str, result))[:300]}")


def step_fingerprint(path: Path) -> tuple[int, int]:
    """(steps, highest idx) - the conversation's own content fingerprint."""
    try:
        with closing(immutable(path)) as connection:
            row = connection.execute("SELECT count(*), coalesce(max(idx), -1) FROM steps").fetchone()
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照讀不到 steps：{e}")
    return int(row[0]), int(row[1])


@contextmanager
def snapshot(session_id: str) -> Iterator[Path]:
    """One consistent, single-file copy of the conversation db; caller owns it.

    The temp directory lives for the `with` block and is removed on the way
    out - including when the copy, the replay or `quick_check` fails.
    """
    db = db_path(session_id)
    if not db.is_file():
        raise AgentError(f"找不到 agy 對話：{session_id}（{db}）")
    tmpdir = PrivateTmpDir("agora-agy")
    try:
        root = Path(tmpdir.name)
        live = copy_stable(db, root)
        copy = root / "snapshot.db"
        if live:
            # A normal open in the temp dir replays the wal and may rebuild the
            # shm there; VACUUM INTO then leaves one file to check and export.
            out = root / "snapshot-out.db"
            try:
                with closing(sqlite3.connect(str(copy))) as connection:
                    connection.execute("VACUUM INTO ?", (str(out),))
            except sqlite3.Error as e:
                raise AgentError(f"agy 快照重放失敗：{e}")
            result = out
        else:
            result = copy
        quick_check(result)
        yield result
    finally:
        tmpdir.cleanup()


def rows(path: Path) -> Iterator[tuple[int, int, bytes]]:
    try:
        with closing(immutable(path)) as connection:
            for idx, step_type, payload in connection.execute(
                    "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"):
                yield int(idx), int(step_type), payload or b""
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照讀不到 steps：{e}")


def trajectory_id(path: Path) -> str | None:
    """The conversation's inner uuid (`trajectory_meta.trajectory_id`)."""
    try:
        with closing(immutable(path)) as connection:
            row = connection.execute(
                "SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def cascade_id(path: Path) -> str | None:
    """The uuid the conversation FILE is named by (`trajectory_meta.cascade_id`).

    The summaries list and `agy --conversation <id>` both go by this one, so
    the restore takes the file name from the restored bytes' own meta
    (docs/agy-storage.md §5) rather than from the envelope, where PR1 only
    carried the inner `trajectory_id`.
    """
    try:
        with closing(immutable(path)) as connection:
            row = connection.execute(
                "SELECT cascade_id FROM trajectory_meta LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


# ---------------------------------------------------------------------------
# the credential-scan surface (PR2b: moved here from agy_wire, unchanged)
# ---------------------------------------------------------------------------


def scan_strings(blob: bytes | None, out: list[str], seen: set[str], depth: int = 0) -> None:
    """Collect every UTF-8 string in any length-delimited field of `blob`, recursively.

    No field is picked and nothing is truncated: the scan text is a coverage
    surface, not a reading. A blob that is not valid wire format just stops
    this branch (the step is still read for the reading version). The depth
    guard is only there so a pathological blob cannot recurse forever.
    """
    if not blob or depth > 12:
        return
    try:
        for _number, wire, value in fields(blob):
            if wire != 2:
                continue
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and len(value) >= 4 and text not in seen:
                seen.add(text)
                out.append(text)
            scan_strings(value, out, seen, depth + 1)
    except ValueError:
        return                                 # broken tail: keep what parsed before it


def scan_text(path: Path) -> list[str]:
    """Every conversation-step string of the snapshot, in order, deduplicated.

    Only the `steps` columns are walked: `gen_metadata`, `executor_metadata` and
    `trajectory_metadata_blob` are settings/system prompts, not conversation,
    and stay inside `db_b64` (a documented gap). Tool output is here in full.
    """
    out: list[str] = []
    seen: set[str] = set()
    try:
        with closing(immutable(path)) as connection:
            streams = connection.execute(
                "SELECT step_payload, render_info, metadata FROM steps ORDER BY idx")
            for payload, render_info, metadata in streams:
                for blob in (payload, render_info, metadata):
                    scan_strings(blob, out, seen)
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照讀不到 steps：{e}")
    return out


# ---------------------------------------------------------------------------
# the raw envelope (PR2b: moved here from agy.py, unchanged)
# ---------------------------------------------------------------------------


def pack_raw_file(path: Path, db: Path, trajectory_id: str | None,
                  scan_text: list[str]) -> tuple[str, int]:
    """Stream the envelope to `path`; return (md5, size) without reading it back.

    base64 is written in 3-byte multiples and the db is read chunk by chunk, so
    the whole raw is never in memory (the same shape claude's `_pack_raw_file`
    has). The byte order is fixed, which is what makes two exports of the same
    unchanged snapshot produce the same md5. The parameters deliberately
    mirror the envelope's own field names.
    """
    digest = hashlib.md5()
    size = 0
    with path.open("wb") as out:
        def write(piece: bytes) -> None:
            nonlocal size
            out.write(piece)
            digest.update(piece)
            size += len(piece)

        write(b'{"format": "agy-conversation/1", "agy_version": null, "trajectory_id": ')
        write(json.dumps(trajectory_id, ensure_ascii=False).encode("utf-8"))
        write(b', "scan_text": ')
        write(json.dumps(scan_text, ensure_ascii=False).encode("utf-8"))
        write(b', "db_b64": "')
        with open(db, "rb") as source:
            while chunk := source.read(B64_CHUNK):
                write(base64.b64encode(chunk))
        write(b'"}')
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size


def envelope(raw) -> dict:
    """The raw envelope's JSON document, refusing anything unclear."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError) as e:
        raise AgentError(f"agy raw 不是 JSON 信封：{e}")
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise AgentError("agy raw 的信封格式認不出來（要 agy-conversation/1）")
    if not isinstance(document.get("db_b64"), str):
        raise AgentError("agy raw 沒有 db_b64")
    return document


def decode_db(document: dict, path: Path) -> None:
    """base64 → a temp db file, chunk by chunk (a chunk is a multiple of 4)."""
    encoded = document["db_b64"]
    with path.open("wb") as out:
        for start in range(0, len(encoded), B64_CHUNK):
            try:
                out.write(base64.b64decode(encoded[start:start + B64_CHUNK]))
            except ValueError as e:
                raise AgentError(f"agy raw 的 db_b64 壞了：{e}")
