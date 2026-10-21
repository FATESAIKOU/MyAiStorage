"""The agy (Antigravity CLI) adapter - read only, first version (#26 PR1).

A conversation is one SQLite database at
`$AGORA_AGY_HOME/conversations/<conversation-uuid>.db` (WAL mode, page size
32768), and the list/titles live in `conversation_summaries.db` next to that
directory. `$AGORA_AGY_HOME` defaults to `~/.gemini/antigravity-cli/` and is
read in *every* place this module touches the agent's files, so a test can
point it at a temp directory and the user's real home is never opened.

What PR1 supports - import and reading only:

    export()        snapshot -> JSON envelope with the whole db base64'd
    turns()         user (step_type 14) / model text and tool calls (15) from
                    the protobuf blobs; tool steps (132) are known and silent
    list_sessions() title/preview/step_count from conversation_summaries.db
    last_message()  one snapshot of that conversation, role from step_type
    search_text()   title + preview only (agy's own table has no body text)

`native` / `append_native` / `summarize` / `start_native` / `collect` are
refused with `AGY_READ_ONLY`; `cli.cmd_continue` and `cli.cmd_merge` refuse
`--agent agy` before doing any work, so a refusal is never mistaken for a bad
model answer. They also refuse a *source* session that came from agy: the
first version supports neither direction of a cross-agent move. Continuing is
PR2, fork/merge are later (#26).

Snapshot (docs/agy-storage.md §1.2, §8): an idle agy database has no `-wal`,
and copying just the `.db` then opening it with `immutable=1` is a consistent
read. While a turn is running the `-wal` holds the new rows and the main file
does not, so both are copied - never `-shm`, SQLite rebuilds it - and the copy
is opened normally in a temp dir, which replays the wal, then `VACUUM INTO`
collapses it to one file. The copy is checked before/after with
`(size, mtime_ns)` and retried when a checkpoint moved the files under it;
a file that vanished mid-copy is the same race, not an error. Nothing here
ever opens a file inside `AGORA_AGY_HOME` writable, and every temp directory
is a `TemporaryDirectory` cleaned on the way out (also on the failing path).

The raw envelope::

    {"format": "agy-conversation/1", "agy_version": ..., "trajectory_id": ...,
     "scan_text": [<plain strings from the conversation steps>], "db_b64": <base64 db>}

`db_b64` is base64 so `store.copy_atomic(utf8=True)` can take it (the mirror
only carries UTF-8). `scan_text` is a separate extraction, not the reading
version: every UTF-8 string of at least 4 bytes found in any length-delimited
field of a step's `step_payload` / `render_info` / `metadata`, untruncated and
deduplicated. That is what a credential scan needs - tool output and tool
arguments are in there in full, while the reading version keeps tool results
out (design 4.4). It does not cover `gen_metadata` / `executor_metadata` /
`trajectory_metadata_blob`: those are settings and system prompts, and they
stay only inside the base64 db (a documented gap, not a silent one).

The db keeps every row - gen_metadata and all - because PR2 restores it and
PR0's R1 asks for no rewriting: the idle path exports the source file byte for
byte, and the live path exports the (row-preserving) `VACUUM INTO` collapse of
the replayed snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from agora.store import normalize

from .base import (AgentError, Exported, Launch, Listed, Turns,
                   iso_utc, tool_line, warn)

FORMAT = "agy-conversation/1"

#: step_type values observed on fake conversations (docs/agy-storage.md §3).
STEP_USER = 14
STEP_MODEL = 15
STEP_TOOL = 132

PREVIEW_MAX = 2000         # last_message text cap, the same as claude's
COPY_TRIES = 3             # a snapshot is retried at most this many times
B64_CHUNK = 3 * 32768      # base64 input chunk: a multiple of 3, so every
                           # chunk ends on a 4-character boundary

#: One message for every place that refuses agy: the command boundary
#: (`cli.cmd_merge` / `cli.cmd_continue`) and the adapter's own stubs.
AGY_READ_ONLY = ("agy 第一版只支援 import／讀取；接續在 PR2、fork／merge 為後續（#26）")

#: What `search_text` can see. Written into `agora --help` and the TUI's
#: content-search note, because "no result" must not look like "not there".
SEARCH_SCOPE = "agy 只搜標題與 preview（不搜內文）"

_TITLE_MAX = 60
_summaries_cache: tuple[tuple, list["_Summary"]] | None = None
_warned_summary_for = False
_warned_stamp = False

#: A plausible timestamp for `last_modified_time` (the column's unit is not
#: known). Anything outside this window is reported as unknown, once - a value
#: that looks like a valid date but is not would silently reorder the list.
_MIN_STAMP = 1_577_836_800.0        # 2020-01-01T00:00:00Z
_MAX_STAMP = 4_102_444_800.0        # 2100-01-01T00:00:00Z


def home() -> Path:
    """Where agy keeps everything; `AGORA_AGY_HOME` overrides it (tests)."""
    return Path(os.environ.get("AGORA_AGY_HOME") or "~/.gemini/antigravity-cli").expanduser()


def conversations_dir() -> Path:
    return home() / "conversations"


def summaries_path() -> Path:
    return home() / "conversation_summaries.db"


def _db_path(session_id: str) -> Path:
    """The conversation db, refusing an id that could walk out of conversations/.

    The id comes from the command line, and a path is not an id: one `..`
    would turn "import this conversation" into "read any file under the home".
    """
    if (not session_id or Path(session_id).name != session_id or "\\" in session_id
            or session_id in (".", "..")):
        raise AgentError(f"agy 的 conversation id 不合法：{session_id!r}")
    return conversations_dir() / f"{session_id}.db"


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _copy_stable(db: Path, dest_dir: Path) -> bool:
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
        before = (_fingerprint(db), _fingerprint(wal))
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
        if (_fingerprint(db), _fingerprint(wal)) == before:
            return live
    raise AgentError(
        f"agy 對話在複製時一直變動（試了 {COPY_TRIES} 次）：{db.name}；"
        "可能有一輪正在寫入，或檔案正在被刪除，等它結束再試")


def _immutable(path: Path) -> sqlite3.Connection:
    """A read-only connection to a single-file snapshot (never to a live db)."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)


def _quick_check(path: Path) -> None:
    try:
        with closing(_immutable(path)) as connection:
            result = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照開不起來：{e}")
    if result != ["ok"]:
        raise AgentError(f"agy 快照 quick_check 沒過：{'；'.join(map(str, result))[:300]}")


def _step_fingerprint(path: Path) -> tuple[int, int]:
    """(steps, highest idx) - the conversation's own content fingerprint."""
    try:
        with closing(_immutable(path)) as connection:
            row = connection.execute("SELECT count(*), coalesce(max(idx), -1) FROM steps").fetchone()
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照讀不到 steps：{e}")
    return int(row[0]), int(row[1])


def _cross_check(path: Path, session_id: str) -> None:
    """Compare the snapshot's step count with conversation_summaries.db's.

    A mismatch is only ever a warning: the summaries table is written by agy
    itself and may lag a checkpoint, and PR1 must still read the conversation.
    A summaries file we cannot read is not evidence about this snapshot either,
    so it is skipped rather than allowed to fail the read.
    """
    count, _ = _step_fingerprint(path)
    try:
        rows = _summary_rows()
    except Exception:                       # broken/unreadable list: no cross-check
        return
    for row in rows:
        if row.session_id == session_id and row.step_count is not None and row.step_count != count:
            warn(f"agy：{session_id} 的快照有 {count} 步，summaries 說 {row.step_count} 步；"
                 "以快照為準")
            return


@contextmanager
def snapshot(session_id: str) -> Iterator[Path]:
    """One consistent, single-file copy of the conversation db; caller owns it.

    The temp directory lives for the `with` block and is removed on the way
    out - including when the copy, the replay or `quick_check` fails.
    """
    db = _db_path(session_id)
    if not db.is_file():
        raise AgentError(f"找不到 agy 對話：{session_id}（{db}）")
    tmpdir = tempfile.TemporaryDirectory(prefix="agora-agy-")
    try:
        root = Path(tmpdir.name)
        live = _copy_stable(db, root)
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
        _quick_check(result)
        _cross_check(result, session_id)
        yield result
    finally:
        tmpdir.cleanup()


# ---------------------------------------------------------------------------
# The protobuf wire format (docs/agy-storage.md §3)
# ---------------------------------------------------------------------------


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 被截斷")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 太長")


def _fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """(field number, wire type, value) for one message; groups are not used."""
    i, end = 0, len(buf)
    while i < end:
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 0x07
        if not number:
            raise ValueError("欄位編號 0")
        if wire == 0:                       # varint
            value, i = _varint(buf, i)
        elif wire == 1:                    # 64-bit
            if i + 8 > end:
                raise ValueError("64-bit 欄位被截斷")
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:                    # length-delimited
            length, i = _varint(buf, i)
            if i + length > end:
                raise ValueError("長度超過訊息")
            value, i = buf[i:i + length], i + length
        elif wire == 5:                    # 32-bit
            if i + 4 > end:
                raise ValueError("32-bit 欄位被截斷")
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"不支援的 wire type {wire}")
        yield number, wire, value


def _all(buf: bytes | None, number: int) -> list[bytes]:
    if not buf:
        return []
    return [value for field, wire, value in _fields(buf)
            if field == number and wire == 2]


def _one(buf: bytes | None, number: int) -> bytes | None:
    hits = _all(buf, number)
    return hits[0] if hits else None


def _texts(buf: bytes | None, number: int) -> list[str]:
    return [value.decode("utf-8", "replace") for value in _all(buf, number)]


def _text(buf: bytes | None, number: int) -> str | None:
    hits = _texts(buf, number)
    return hits[0] if hits else None


def _one_line(text: str) -> str:
    """Text flattened to one line, so a reading turn stays line-shaped."""
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


def _args(value: str | None) -> object:
    """A tool's argument JSON as an object, or the raw string when it is not JSON."""
    if value is None:
        return ""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _user_turn(payload: bytes) -> Turns:
    texts = _texts(_one(payload, 19), 2)
    lines = [text for text in texts if text.strip()]
    return [("user", lines)] if lines else []


def _model_turn(payload: bytes) -> Turns:
    message = _one(payload, 20)
    lines = [text for text in _texts(message, 1) if text.strip()]
    for part in _all(message, 7):
        name = _text(part, 2) or "?"
        lines.append(tool_line(name, _args(_text(part, 3))))
    return [("assistant", lines)] if lines else []


def _turn(step_type: int, payload: bytes) -> Turns:
    if step_type == STEP_USER:
        return _user_turn(payload)
    if step_type == STEP_MODEL:
        return _model_turn(payload)
    return []                                  # unknown: the caller marks it


def _read_turns(rows: Iterator[tuple[int, int, bytes]]) -> tuple[Turns, int]:
    """(reading turns, message count) for the steps of one conversation.

    Step type 132 is the tool *execution* step: it is recognised, and it has no
    reading text of its own (the call was already said by the model's 20.7 part
    and its output belongs to `scan_text`, not to session.md - design 4.4). So
    it is silent, not `[skip …]`. Anything not observed yet is visible as
    `[skip agy-step-<idx>/<type>]`; a step whose protobuf cannot be read is the
    same marker for that step only. The message count is the 14/15 turns that
    produced reading content - `[skip …]` lines are not conversation (N4).
    """
    turns: Turns = []
    messages = 0
    unknown = broken = 0
    for idx, step_type, payload in rows:
        if step_type == STEP_TOOL:
            continue
        if step_type not in (STEP_USER, STEP_MODEL):
            unknown += 1
            turns.append(("user", [f"[skip agy-step-{idx}/{step_type}]"]))
            continue
        try:
            part = _turn(step_type, payload)
        except (ValueError, IndexError, UnicodeDecodeError):
            broken += 1
            turns.append(("user", [f"[skip agy-step-{idx}/{step_type}]"]))
            continue
        if part:
            turns.extend(part)
            messages += 1
        else:
            unknown += 1
            turns.append(("user", [f"[skip agy-step-{idx}/{step_type}]"]))
    if unknown or broken:
        said = []
        if unknown:
            said.append(f"{unknown} 個步驟不認得")
        if broken:
            said.append(f"{broken} 個步驟解不開")
        warn("agy：" + "、".join(said) + "，已標成 [skip …]，其餘照常讀取")
    return turns, messages


def _rows(path: Path) -> Iterator[tuple[int, int, bytes]]:
    try:
        with closing(_immutable(path)) as connection:
            for idx, step_type, payload in connection.execute(
                    "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"):
                yield int(idx), int(step_type), payload or b""
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照讀不到 steps：{e}")


def _trajectory_id(path: Path) -> str | None:
    try:
        with closing(_immutable(path)) as connection:
            row = connection.execute(
                "SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) and row[0] else None


# ---------------------------------------------------------------------------
# conversation_summaries.db: the list, titles and step counts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Summary:
    session_id: str
    title: str | None
    preview: str | None
    updated_at: str | None
    step_count: int | None
    workspace: str | None


_SUMMARY_COLUMNS = ("conversation_id", "title", "preview", "last_modified_time", "step_count")


def _iso(value: object) -> str | None:
    """The summaries' timestamp in RFC 3339; the column's unit is not known.

    PR0 never read this table (it holds real titles), so the adapter accepts
    what a timestamp plausibly is - unix seconds, milliseconds, microseconds or
    an ISO string - inside a sane window (2020-2100). Anything else is None and
    one warning, rather than a valid-looking date that silently reorders the
    list, marks a session as stale, or makes the TUI's ↻ appear.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _stamp(float(value))
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return _stamp(float(text))
        except ValueError:
            try:
                return _stamp(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            except (ValueError, OverflowError, OSError):
                return None
    return None


def _stamp(number: float) -> str | None:
    """A plausible epoch number as RFC 3339, picking the unit by magnitude."""
    global _warned_stamp
    if number > 1e15:            # microseconds
        number /= 1_000_000
    elif number > 1e12:         # milliseconds
        number /= 1_000
    if not _MIN_STAMP <= number < _MAX_STAMP:
        if not _warned_stamp:
            _warned_stamp = True
            warn("agy：summaries 的時間欄位看不懂（不在 2020–2100），排序與新鮮度先當作未知")
        return None
    try:
        return iso_utc(number)
    except (ValueError, OverflowError, OSError):
        return None


def _as_text(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else None


def _workspace(value: object) -> str | None:
    """The first workspace URI as a plain path; the column's shape is unknown."""
    text = _as_text(value)
    if not text or not text.strip():
        return None
    text = text.strip()
    items: object = text
    if text[:1] in "[{":
        try:
            items = json.loads(text)
        except ValueError:
            items = [text]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = [text]
    for item in items:
        if isinstance(item, dict):
            item = item.get("uri") or item.get("path")
        if not isinstance(item, str) or not item.strip():
            continue
        item = item.strip()
        if item.startswith("file://"):
            item = unquote(item[len("file://"):])
        return item
    return None


def _summary_table(connection: sqlite3.Connection) -> tuple[str, dict[str, str]] | None:
    """The table that looks like conversation_summaries, by columns not by name.

    PR0 did not decipher this file, so the table's own name is unknown; a
    database that has a table with these columns is one this adapter can read.
    SQLite column names are case-insensitive, so the match is too; the returned
    map is lowercase-name → the name as written, which is what SELECT needs.
    """
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for name in names:
        quoted = str(name).replace('"', '""')
        columns = {str(row[1]).lower(): str(row[1])
                   for row in connection.execute(f'PRAGMA table_info("{quoted}")')}
        if set(_SUMMARY_COLUMNS) <= set(columns):
            return str(name), columns
    return None


def _read_summaries(path: Path) -> list[_Summary]:
    with tempfile.TemporaryDirectory(prefix="agora-agy-sum-") as tmp:
        root = Path(tmp)
        live = _copy_stable(path, root)
        copy = root / "snapshot.db"
        try:
            connection = (sqlite3.connect(str(copy)) if live else _immutable(copy))
        except sqlite3.Error as e:
            raise AgentError(f"agy summaries 開不起來：{e}")
        try:
            try:
                found = _summary_table(connection)
                if found is None:
                    raise AgentError("agy：conversation_summaries.db 的結構認不出來，"
                                     "未匯入清單與搜尋先停用（不是沒有 session）")
                quoted = found[0].replace('"', '""')
                columns = found[1]
                wanted = [columns[column] for column in _SUMMARY_COLUMNS]
                if "workspace_uris" in columns:
                    wanted.append(columns["workspace_uris"])
                select = ", ".join(f'"{column}"' for column in wanted)
                rows = connection.execute(f'SELECT {select} FROM "{quoted}"').fetchall()
            except sqlite3.Error as e:
                raise AgentError(f"agy summaries 讀不到：{e}")
        finally:
            connection.close()
    out: list[_Summary] = []
    for row in rows:
        session_id = _as_text(row[0])
        if not session_id:
            continue
        count = row[4]
        try:
            step_count = int(count) if isinstance(count, (int, float)) else None
        except (ValueError, OverflowError):
            step_count = None
        out.append(_Summary(
            session_id=session_id,
            title=_as_text(row[1]),
            preview=_as_text(row[2]),
            updated_at=_iso(row[3]),
            step_count=step_count,
            workspace=_workspace(row[5]) if len(row) > 5 else None,
        ))
    return out


def _summary_rows() -> list[_Summary]:
    """All summary rows, cached by the summaries db's (mtime, size).

    The TUI rescans on every filter keystroke; copying and opening this file
    each time is the cost this cache removes. The key includes the `-wal` pair
    too, because a turn in progress changes the wal before the main file.
    """
    global _summaries_cache
    path = summaries_path()
    if not path.is_file():
        return []
    wal = path.with_name(path.name + "-wal")
    key = (str(path), _fingerprint(path), _fingerprint(wal))
    if _summaries_cache is not None and _summaries_cache[0] == key:
        return _summaries_cache[1]
    rows = _read_summaries(path)
    _summaries_cache = (key, rows)
    return rows


def _summary_for(session_id: str) -> _Summary | None:
    """This session's summary row, or None - said out loud once when unreadable."""
    global _warned_summary_for
    try:
        return next((row for row in _summary_rows() if row.session_id == session_id), None)
    except Exception as e:                  # a missing/broken list must not fail an import
        if not _warned_summary_for:
            _warned_summary_for = True
            warn(f"agy：讀不到 conversation_summaries.db，匯入照常但沒有標題與目錄（{e}）")
        return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _scan_strings(blob: bytes | None, out: list[str], seen: set[str], depth: int = 0) -> None:
    """Collect every UTF-8 string in any length-delimited field of `blob`, recursively.

    No field is picked and nothing is truncated: the scan text is a coverage
    surface, not a reading. A blob that is not valid wire format just stops
    this branch (the step is still read for the reading version). The depth
    guard is only there so a pathological blob cannot recurse forever.
    """
    if not blob or depth > 12:
        return
    try:
        for _number, wire, value in _fields(blob):
            if wire != 2:
                continue
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and len(value) >= 4 and text not in seen:
                seen.add(text)
                out.append(text)
            _scan_strings(value, out, seen, depth + 1)
    except ValueError:
        return                                 # broken tail: keep what parsed before it


def _scan_text(path: Path) -> list[str]:
    """Every conversation-step string of the snapshot, in order, deduplicated.

    Only the `steps` columns are walked: `gen_metadata`, `executor_metadata` and
    `trajectory_metadata_blob` are settings/system prompts, not conversation,
    and stay inside `db_b64` (a documented gap). Tool output is here in full.
    """
    out: list[str] = []
    seen: set[str] = set()
    try:
        with closing(_immutable(path)) as connection:
            rows = connection.execute(
                "SELECT step_payload, render_info, metadata FROM steps ORDER BY idx")
            for payload, render_info, metadata in rows:
                for blob in (payload, render_info, metadata):
                    _scan_strings(blob, out, seen)
    except sqlite3.Error as e:
        raise AgentError(f"agy 快照讀不到 steps：{e}")
    return out


def _pack_raw_file(path: Path, db: Path, trajectory_id: str | None,
                   scan_text: list[str]) -> tuple[str, int]:
    """Stream the envelope to `path`; return (md5, size) without reading it back.

    base64 is written in 3-byte multiples and the db is read chunk by chunk, so
    the whole raw is never in memory (the same shape claude's `_pack_raw_file`
    has). The byte order is fixed, which is what makes two exports of the same
    unchanged snapshot produce the same md5.
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


def _envelope(raw: bytes) -> dict:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise AgentError(f"agy raw 不是 JSON 信封：{e}")
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise AgentError("agy raw 的信封格式認不出來（要 agy-conversation/1）")
    if not isinstance(document.get("db_b64"), str):
        raise AgentError("agy raw 沒有 db_b64")
    return document


def _decode_db(document: dict, path: Path) -> None:
    """base64 → a temp db file, chunk by chunk (a chunk is a multiple of 4)."""
    encoded = document["db_b64"]
    with path.open("wb") as out:
        for start in range(0, len(encoded), B64_CHUNK):
            try:
                out.write(base64.b64decode(encoded[start:start + B64_CHUNK]))
            except ValueError as e:
                raise AgentError(f"agy raw 的 db_b64 壞了：{e}")


class AgyAgent:
    """Reads agy's own files; never runs the CLI and never writes its home."""

    name = "agy"

    # -- the Agent protocol ------------------------------------------------

    def export(self, session_id: str) -> Exported:
        with snapshot(session_id) as db:
            turns, message_count = _read_turns(_rows(db))
            scan_text = _scan_text(db)
            trajectory_id = _trajectory_id(db)
            summary = _summary_for(session_id)
            tmpdir = tempfile.TemporaryDirectory(prefix="agora-agy-raw-")
            raw_path = Path(tmpdir.name) / "raw.json"
            md5, size = _pack_raw_file(raw_path, db, trajectory_id, scan_text)
        return Exported(
            session_id=session_id,
            raw_file=raw_path,
            raw_md5=md5,
            raw_size=size,
            _raw_tmpdir=tmpdir,
            dir=summary.workspace if summary else None,
            title=summary.title if summary else None,
            created_at=None,          # summaries has no created time (unknown, §10)
            agent_version=None,       # no version field was found in the db (unknown)
            message_count=message_count,
            model=None,               # no model name was found in the steps (unknown)
            turns=turns,
        )

    def turns(self, raw: bytes) -> Turns:
        """The envelope's db, decoded to a temp file and read step by step."""
        document = _envelope(raw)
        with tempfile.TemporaryDirectory(prefix="agora-agy-raw-") as tmp:
            db = Path(tmp) / "raw.db"
            _decode_db(document, db)
            return _read_turns(_rows(db))[0]

    def list_sessions(self) -> list[Listed]:
        """Every conversation the summaries table knows, newest first.

        Read-only and cached (see `_summary_rows`); an unreadable database
        raises `AgentError` - the TUI prints that per agent and carries on,
        while an empty list would quietly claim there are no conversations.
        """
        found = []
        for row in _summary_rows():
            title = (row.title or "").strip() or None
            if title is None and row.preview:
                title = _one_line(row.preview)[:_TITLE_MAX] or None
            found.append(Listed(session_id=row.session_id, dir=row.workspace,
                                title=title, updated_at=row.updated_at))
        found.sort(key=lambda item: item.updated_at or "", reverse=True)
        return found

    def last_message(self, session_id: str) -> tuple[str, str] | None:
        """(role, text) of the last text step; role comes from step_type.

        The summaries table has no role column, so this takes one snapshot when
        the row is picked and reads the last 14/15 step. A tool-call model step
        (no 20.1) is not something the user or the model said as prose, so it
        is passed over; an unreadable session gives None, never a raise.
        """
        try:
            with snapshot(session_id) as db:
                with closing(_immutable(db)) as connection:
                    for step_type, payload in connection.execute(
                            "SELECT step_type, step_payload FROM steps ORDER BY idx DESC"):
                        try:
                            if step_type == STEP_USER:
                                text = "\n".join(_texts(_one(payload or b"", 19), 2))
                                role = "user"
                            elif step_type == STEP_MODEL:
                                text = "\n".join(_texts(_one(payload or b"", 20), 1))
                                role = "assistant"
                            else:
                                continue
                        except (ValueError, IndexError, UnicodeDecodeError):
                            continue
                        if text.strip():
                            return role, text[-PREVIEW_MAX:]
        except (AgentError, sqlite3.Error, OSError):
            return None
        return None

    def search_text(self, keyword: str, only: "set[str] | None" = None) -> Iterator[str]:
        """Ids whose title or preview contains `keyword` - agy's body is not searchable."""
        needle = normalize(keyword)
        if not needle:
            return
        for row in _summary_rows():
            if only is not None and row.session_id not in only:
                continue
            haystack = normalize("\n".join(part for part in (row.title, row.preview) if part))
            if needle in haystack:
                yield row.session_id

    # -- refused until PR2 / later (#26) -----------------------------------

    def native(self, turns: Turns) -> bytes:
        raise AgentError(AGY_READ_ONLY)

    def append_native(self, raw: bytes, turns: Turns) -> bytes:
        raise AgentError(AGY_READ_ONLY)

    def summarize(self, prompt: str, workdir: Path) -> tuple[str, str | None]:
        raise AgentError(AGY_READ_ONLY)

    def start_native(self, raw: bytes, workdir: Path, *, headless: bool = False,
                     headless_port: int | None = None,
                     harness_file: Path | None = None) -> Launch:
        raise AgentError(AGY_READ_ONLY)

    def collect(self, launch: Launch) -> Exported | None:
        raise AgentError(AGY_READ_ONLY)


ADAPTER = AgyAgent()
