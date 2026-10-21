"""The agy summaries list: reading, caching, and the one row a continue adds.

`conversation_summaries.db` next to `conversations/` is the file agy loads a
conversation list by, and (per the PR2b spike) the row a conversation needs
before `agy --conversation <id>` opens it. This module reads that list
(`summary_rows`／`summary_for` and their cache) and inserts the missing row
(`ensure_summary_row`). **Everything it writes stays inside `$AGORA_AGY_HOME`**
(see `agy_store`), and it never creates or rewrites a table - one
`BEGIN IMMEDIATE` insert, only when the id has no row (docs §10).

Dependency direction (review W5): `agy_store` → this file → `agy_restore`;
nothing here imports the restore half. The table's real column shapes and the
stamp's unit are UNVERIFIED (docs §10): the table is found by its column set,
never its name, and the unit is guessed by magnitude in `_unit_scale` only.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from agora.agents.agy_store import copy_stable, file_fingerprint, immutable, summaries_path
from agora.agents.base import AgentError, iso_utc, warn

#: How long the writable summaries connection waits on agy's own lock before
#: answering `AgentError` (agy checkpoints within a second of a turn ending,
#: docs §7; the insert itself is one short transaction).
SUMMARY_TIMEOUT = 30.0

#: A plausible timestamp for `last_modified_time` (the column's unit is not
#: known). Anything outside this window is reported as unknown, once - a value
#: that looks like a valid date but is not would silently reorder the list.
_MIN_STAMP = 1_577_836_800.0        # 2020-01-01T00:00:00Z
_MAX_STAMP = 4_102_444_800.0        # 2100-01-01T00:00:00Z

_summaries_cache: tuple[tuple, list["_Summary"]] | None = None
_warned_summary_for = False
_warned_stamp = False


# timestamps: one place guesses the unit (review N6)


def _number(value: object) -> float | None:
    """A numeric stamp (int/float, or a numeric string) as a float; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _unit_scale(number: float) -> float:
    """How many of the stamp's units make a second, by magnitude.

    The one place the 1e12/1e15 thresholds exist (seconds / milliseconds /
    microseconds). agy's real unit is unverified (docs §10); the reading side
    and the fresh row's shape both derive from this guess.
    """
    if number > 1e15:                    # microseconds
        return 1_000_000.0
    return 1_000.0 if number > 1e12 else 1.0


def epoch_seconds(value: object) -> float | None:
    """Any plausible stamp as epoch seconds; None outside 2020-2100.

    The unit rule lives here once (N6), shared with `_iso` and `_live_stamp`.
    """
    seconds = _number(value)
    if seconds is None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            seconds = datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    seconds /= _unit_scale(seconds)
    if not _MIN_STAMP <= seconds < _MAX_STAMP:
        return None
    return seconds


def _iso(value: object) -> str | None:
    """The summaries' timestamp in RFC 3339; the column's unit is not known.

    PR0 never read this table, so unix seconds / milliseconds / microseconds /
    ISO text inside 2020-2100 is accepted; anything else is None and one warning.
    """
    global _warned_stamp
    seconds = epoch_seconds(value)
    if seconds is None:
        # Only a number of the right shape is worth the warning; an unreadable
        # string is just missing, not a unit this adapter got wrong.
        if _number(value) is not None and not _warned_stamp:
            _warned_stamp = True
            warn("agy：summaries 的時間欄位看不懂（不在 2020–2100），排序與新鮮度先當作未知")
        return None
    try:
        return iso_utc(seconds)
    except (ValueError, OverflowError, OSError):
        return None


def _stamp_like(seconds: float, samples: list[object]) -> object:
    """A fresh stamp in the shape the table's own rows carry (unverified unit).

    The first plausible sample decides its unit (`_unit_scale`) and type; an
    empty table gets ISO 8601 UTC `Z`, marked 未驗 in the docs.
    """
    for sample in samples:
        if isinstance(sample, bool):
            continue
        if isinstance(sample, (int, float)):
            return type(sample)(seconds * _unit_scale(sample))
        if isinstance(sample, str) and sample.strip():
            return iso_utc(seconds)
    return iso_utc(seconds)


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
    #: the raw stamp column value, for the restore's verdict and the fresh
    #: row's shape (the reading side keeps the normalised ISO in updated_at).
    stamp: object = None


_SUMMARY_COLUMNS = ("conversation_id", "title", "preview", "last_modified_time", "step_count")


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


def summary_table(connection: sqlite3.Connection) -> tuple[str, dict[str, str]] | None:
    """The table that looks like conversation_summaries, by columns not by name.

    PR0 did not decipher this file, so its table name is unknown; one with these
    columns is one this adapter can read. Names match case-insensitively; the
    map is lowercase-name → the name as written.
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
        live = copy_stable(path, root)
        copy = root / "snapshot.db"
        try:
            connection = (sqlite3.connect(str(copy)) if live else immutable(copy))
        except sqlite3.Error as e:
            raise AgentError(f"agy summaries 開不起來：{e}")
        try:
            try:
                found = summary_table(connection)
                if found is None:
                    raise AgentError("agy：conversation_summaries.db 的結構認不出來，"
                                     "未匯入清單與搜尋先停用（不是沒有 session）")
                quoted = found[0].replace('"', '""')
                columns = found[1]
                wanted = [columns[column] for column in _SUMMARY_COLUMNS]
                if "workspace_uris" in columns:
                    wanted.append(columns["workspace_uris"])
                select = ", ".join(f'"{column}"' for column in wanted)
                rows_ = connection.execute(f'SELECT {select} FROM "{quoted}"').fetchall()
            except sqlite3.Error as e:
                raise AgentError(f"agy summaries 讀不到：{e}")
        finally:
            connection.close()
    out: list[_Summary] = []
    for row in rows_:
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
            stamp=row[3],
        ))
    return out


def summary_rows() -> list[_Summary]:
    """All summary rows, cached by the summaries db's (mtime, size).

    The key includes the `-wal` pair: a turn in progress changes the wal first.
    """
    global _summaries_cache
    path = summaries_path()
    if not path.is_file():
        return []
    wal = path.with_name(path.name + "-wal")
    key = (str(path), file_fingerprint(path), file_fingerprint(wal))
    if _summaries_cache is not None and _summaries_cache[0] == key:
        return _summaries_cache[1]
    listed = _read_summaries(path)
    _summaries_cache = (key, listed)
    return listed


def summary_for(session_id: str) -> _Summary | None:
    """This session's summary row, or None - said out loud once when unreadable."""
    global _warned_summary_for
    try:
        return next((row for row in summary_rows() if row.session_id == session_id), None)
    except Exception as e:                  # a missing/broken list must not fail an import
        if not _warned_summary_for:
            _warned_summary_for = True
            warn(f"agy：讀不到 conversation_summaries.db，匯入照常但沒有標題與目錄（{e}）")
        return None


# ---------------------------------------------------------------------------
# the missing row a restored conversation needs
# ---------------------------------------------------------------------------


def _column_default(declared: str) -> object:
    """'' for stringish columns, 0 for numeric ones, NULL for the rest.

    The real row's shape is unverified (docs §10), so the *declared* type
    decides: string-like columns stay empty, numbers 0, blobs like
    `raw_summary` NULL rather than made up.
    """
    text = (declared or "").upper()
    if any(word in text for word in ("CHAR", "CLOB", "TEXT")):
        return ""
    if any(word in text for word in ("INT", "REAL", "FLOA", "DOUB",
                                     "NUM", "DEC", "BOOL")):
        return 0
    return None


def _row_values(columns: dict[str, str], declared: dict[str, str], conversation_id: str, *,
                title: str | None, preview: str | None, step_count: int,
                last_modified: float, workdir: str | None,
                stamps: list[object]) -> tuple[object, ...]:
    """Every column of the summaries table, filled the way the row deserves.

    The five read columns decide themselves; the rest follow their declared
    type (`_column_default`); `last_modified_time` mirrors the table's own
    stamp shape (`_stamp_like`). Order follows `columns`, the INSERT's order.
    """
    workspace = f"file://{workdir}" if workdir else ""
    values: list[object] = []
    for name in columns:
        if name == "conversation_id":
            values.append(conversation_id)
        elif name == "title":
            values.append(title or "")
        elif name == "preview":
            values.append(preview or "")
        elif name == "step_count":
            values.append(int(step_count))
        elif name == "last_modified_time":
            values.append(_stamp_like(last_modified, stamps))
        elif name == "workspace_uris":
            values.append(workspace)
        else:
            values.append(_column_default(declared.get(name) or ""))
    return tuple(values)


def ensure_summary_row(conversation_id: str, *, title: str | None, preview: str | None,
                       step_count: int, last_modified: float,
                       workdir: "Path | str | None" = None) -> bool:
    """Put the summaries row a restored conversation opens by - if it is missing.

    The PR2b spike: agy needs a row in conversation_summaries.db (by
    `conversation_id`) even with the `.db` file in place, or it opens an empty
    one. Filling it is **only** under `$AGORA_AGY_HOME`, **only** when the file
    exists and its table is recognised by the reader's column-set match
    (anything else is `AgentError`; no table is ever created or rewritten), and
    **only** when this id has no row yet (an already-listed id returns False).
    One `BEGIN IMMEDIATE` transaction, so a failure leaves no half row.

    Nothing here touches `-wal`／`-shm`: SQLite removes them when the last
    connection closes, and while another agy process holds the db they are its
    live wal-index - deleting them under it can hide or corrupt its writes
    (review W2).

    The row's real column shapes are UNVERIFIED (docs §10): `last_modified_time`
    follows the existing rows' unit (`_stamp_like`), `workspace_uris` becomes
    `file://<workdir>`. Returns True when a row was inserted, False otherwise.
    """
    path = summaries_path()
    if not path.is_file():
        raise AgentError("agy conversation_summaries.db 不存在，不補列（#26：不建表）")
    connection = sqlite3.connect(str(path), timeout=SUMMARY_TIMEOUT)
    inserted = False
    try:
        try:
            found = summary_table(connection)
            if found is None:
                raise AgentError(
                    "agy：conversation_summaries.db 的結構認不出來，不補列"
                    "（#26 PR2b；不是「沒有 session」）")
            table, columns = found
            quoted = table.replace('"', '""')
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(f'SELECT 1 FROM "{quoted}" WHERE conversation_id = ?',
                                      (conversation_id,)).fetchone() is None:
                    stamps = [row[0] for row in connection.execute(
                        f'SELECT "last_modified_time" FROM "{quoted}" LIMIT 50')]
                    declared = {str(row[1]).lower(): str(row[2]) for row in connection.execute(
                        f'PRAGMA table_info("{quoted}")')}
                    place = ", ".join(f'"{columns[name]}"' for name in columns)
                    marks = ", ".join("?" for _ in columns)
                    connection.execute(
                        f'INSERT INTO "{quoted}" ({place}) VALUES ({marks})',
                        _row_values(columns, declared, conversation_id, title=title,
                                    preview=preview, step_count=step_count,
                                    last_modified=last_modified,
                                    workdir=str(workdir) if workdir else None,
                                    stamps=stamps))
                    inserted = True
                connection.execute("COMMIT")
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            raise AgentError(f"agy：conversation_summaries.db 寫不進去：{e}") from e
    finally:
        connection.close()
    return inserted
