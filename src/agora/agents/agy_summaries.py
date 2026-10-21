"""The agy summaries list: reading, caching, and the one row a continue adds.

`conversation_summaries.db` next to `conversations/` is the file agy loads a
conversation list by, and (per the PR2b spike) the row a conversation needs
before `agy --conversation <id>` opens it. This module reads that list
(`summary_rows`／`summary_for` and their cache) and inserts the missing row
(`ensure_summary_row`). **Everything it writes stays inside `$AGORA_AGY_HOME`**
(see `agy_store`), and it never creates or rewrites a table - one
`BEGIN IMMEDIATE` insert, only when the id has no row (docs §10).

Dependency direction (review W5): `agy_store` → this file → `agy_restore`;
nothing here imports the restore half. The table is found by its column set,
never its name. The real agy's `NOT NULL` columns and the row's own shapes were
observed on 2026-10-21 (docs §12, ticket #136); a number of the shapes are
still UNVERIFIED (docs §10), and the stamp's unit is guessed by magnitude in
`_unit_scale` only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

#: The real agy row's datetime text (docs §12, #136): `2026-10-21
#: 08:41:06.65668+00:00` - date, a space, time, an optional fraction of
#: whatever width that row uses, and a zone. `_stamp_text` copies each part.
_STAMP_SHAPE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?P<sep>[ T])(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?(?P<zone>Z|[+-]\d{2}:?\d{2})$")


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


def _stamp_text(seconds: float, sample: str) -> str:
    """`seconds` written in the shape of one of the table's own datetime texts.

    The real agy writes `2026-10-21 08:41:06.65668+00:00` (docs §12, #136):
    a space, not `T`; a fraction of whatever width that row uses; `+00:00`,
    not `Z`. All three are copied from the sample - a version that changes
    them changes the fresh row with it, and an unreadable sample keeps the
    old ISO shape rather than inventing one.
    """
    match = _STAMP_SHAPE.match(sample.strip())
    if match is None:
        return iso_utc(seconds)
    zone = match.group("zone")
    offset = timedelta(0)
    if zone != "Z":
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        if zone.startswith("-"):
            offset = -offset
    local = datetime.fromtimestamp(seconds, timezone.utc).astimezone(timezone(offset))
    text = local.strftime("%Y-%m-%d") + match.group("sep") + local.strftime("%H:%M:%S")
    fraction = match.group("fraction") or ""
    if fraction:
        text += "." + local.strftime("%f")[:len(fraction)]
    if zone == "Z":
        return text + "Z"
    total = abs(int(offset.total_seconds())) // 60
    sign = "-" if offset < timedelta(0) else "+"
    separator = ":" if ":" in zone else ""
    return f"{text}{sign}{total // 60:02d}{separator}{total % 60:02d}"


def _stamp_like(seconds: float, samples: list[object]) -> object:
    """A fresh stamp in the shape the table's own rows carry (unverified unit).

    The first plausible sample decides its unit (`_unit_scale`) and type; a
    numeric column keeps a number, a datetime text keeps *its* shape
    (`_stamp_text`). An empty table gets ISO 8601 UTC `Z`, marked 未驗 in the
    docs.
    """
    for sample in samples:
        if isinstance(sample, bool):
            continue
        if isinstance(sample, (int, float)):
            return type(sample)(seconds * _unit_scale(sample))
        if isinstance(sample, str) and sample.strip():
            return _stamp_text(seconds, sample)
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


def _declared_default(text: str | None) -> tuple[bool, object]:
    """(found, value) for a column's own `DEFAULT`: `-1`, `0.5`, `""`, `'x'`, `false`, `true`.

    agy declares its defaults (`last_user_input_step_index` is `-1`, not 0);
    the row we add should carry what agy itself would have written. Anything
    this does not recognise (an expression, `CURRENT_TIMESTAMP`, `NULL`) is
    "not found" and the declared type decides instead.
    """
    if text is None:
        return False, None
    value = text.strip()
    if re.fullmatch(r"[+-]?\d+", value):
        return True, int(value)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?", value):
        return True, float(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return True, value[1:-1]
    if value.lower() in ("false", "true"):
        return True, int(value.lower() == "true")
    return False, None


def _column_default(declared: str) -> object:
    """'' for stringish columns, 0 for numeric ones, NULL for the rest.

    Only the *declared* type is known for the columns the reader does not
    read: string-like columns stay empty, numbers 0, blobs like `raw_summary`
    NULL rather than made up. A `NOT NULL` column this leaves NULL is refused
    by `precheck_row` before any restore, not filled with an invented value.
    """
    text = (declared or "").upper()
    if any(word in text for word in ("CHAR", "CLOB", "TEXT")):
        return ""
    if any(word in text for word in ("INT", "REAL", "FLOA", "DOUB",
                                     "NUM", "DEC", "BOOL")):
        return 0
    return None


#: How many of the table's own rows a fresh row copies shapes from.
_SAMPLE_LIMIT = 50

#: Columns whose value copies the table's own rows when it has any: they say
#: which app/project the list belongs to, not what this conversation is.
_SAMPLED_COLUMNS = ("status", "project_id", "app_data_dir")


@dataclass(frozen=True)
class _Column:
    """One declared column, as `PRAGMA table_info` describes it."""

    declared: str
    not_null: bool
    default: str | None = None      # `dflt_value` as SQLite prints it: `-1`, `""`, `false`


@dataclass(frozen=True)
class _Samples:
    """What the table's existing rows say a fresh row should look like."""

    stamps: tuple[object, ...] = ()
    workspace_uris: object | None = None
    common: dict[str, str] = field(default_factory=dict)


def _schema(connection: sqlite3.Connection, quoted: str) -> dict[str, _Column]:
    """Every column's declared type and constraints, lowercase name → column."""
    return {str(row[1]).lower(): _Column(declared=str(row[2] or ""),
                                         not_null=bool(row[3]),
                                         default=None if row[4] is None else str(row[4]))
            for row in connection.execute(f'PRAGMA table_info("{quoted}")')}


def _most_common_text(values: list[object]) -> str:
    """The value the table's own rows repeat most; '' when there is none."""
    counts = Counter(value for value in values if isinstance(value, str))
    return counts.most_common(1)[0][0] if counts else ""


def _samples(connection: sqlite3.Connection, quoted: str,
             columns: dict[str, str]) -> _Samples:
    """Up to `_SAMPLE_LIMIT` rows, reduced to what a fresh row copies from them.

    `last_modified_time` decides the stamp's shape, `workspace_uris` its own
    (JSON array text on the real agy, docs §12), and the app/project columns
    take the most common value. An empty table answers empty samples, and the
    fill rules fall back to what this adapter always wrote.
    """
    place = ", ".join(f'"{name}"' for name in columns)
    rows_ = [dict(zip(columns, row)) for row in connection.execute(
        f'SELECT {place} FROM "{quoted}" LIMIT {_SAMPLE_LIMIT}')]
    workspace = next((row.get("workspace_uris") for row in rows_
                      if isinstance(row.get("workspace_uris"), str)
                      and str(row["workspace_uris"]).strip()), None)
    return _Samples(
        stamps=tuple(row.get("last_modified_time") for row in rows_),
        workspace_uris=workspace,
        common={name: _most_common_text([row.get(name) for row in rows_])
                for name in _SAMPLED_COLUMNS})


def _workspace_value(workdir: str | None, samples: _Samples) -> str:
    """`workspace_uris` in the shape the table's rows use (JSON array or plain).

    The real agy writes a JSON array text (`["file:///private/tmp/…/proj"]`,
    #136), so a fresh row writes `["file://<workdir>"]`; a table whose rows
    carry a plain string keeps a plain string. An empty table (or a sample
    that is not text) falls back to the shape this adapter always wrote.
    """
    uri = f"file://{workdir}" if workdir else ""
    sample = samples.workspace_uris
    if isinstance(sample, str) and sample.strip().startswith("["):
        return json.dumps([uri] if uri else [])
    return uri


def _row_values(columns: dict[str, str], schema: dict[str, _Column],
                conversation_id: str, *, title: str | None, preview: str | None,
                step_count: int, last_modified: float, workdir: str | None,
                samples: _Samples) -> tuple[object, ...]:
    """Every column of the summaries table, filled the way the row deserves.

    The read columns decide themselves; `last_user_input_time` carries the
    **same** moment as `last_modified_time` - the real table makes both
    `NOT NULL` and the acceptance run (#136) failed on exactly that; the
    stamps copy the table's own datetime text (`_stamp_like`). The workspace
    and app/project columns copy the shape or value the rows use
    (`_workspace_value`, `_samples`); the rest follow their declared type
    (`_column_default`). Order follows `columns`, the INSERT's order.
    """
    stamp = _stamp_like(last_modified, list(samples.stamps))
    workspace = _workspace_value(workdir, samples)
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
            values.append(stamp)
        elif name == "last_user_input_time":
            values.append(stamp)                 # the same moment, in the same shape
        elif name == "workspace_uris":
            values.append(workspace)
        elif name in _SAMPLED_COLUMNS:
            values.append(samples.common.get(name, ""))
        else:
            column = schema.get(name)
            found, own = _declared_default(column.default if column else None)
            values.append(own if found else _column_default(column.declared if column else ""))
    return tuple(values)


def precheck_row(connection: sqlite3.Connection, found: tuple[str, dict[str, str]], *,
                 workdir: "Path | str | None" = None) -> tuple[object, ...]:
    """Build the row this continue would insert; refuse one that cannot fit.

    `restore` replaces the conversation file for real and `ensure_summary_row`
    runs after it, so «this row is insertable» is checked **before** any write
    (ticket #136). Building it is pure Python over the schema and up to
    `_SAMPLE_LIMIT` sample rows: no `BEGIN IMMEDIATE`, no INSERT/ROLLBACK dry
    run. A dry insert would take agy's write lock (and wait up to
    `SUMMARY_TIMEOUT`) for a row we were not going to keep, and even rolled
    back it bumps the file's change counter; the static check is the simpler
    of the ticket's two options and leaves nothing for another agy process to
    collide with. What it catches is what the fill rules can predict: a
    `NOT NULL` column they leave NULL - a future agy adding, say, a
    `blob NOT NULL`. Triggers and CHECK constraints are `ensure_summary_row`'s
    to report, and then `prepare_continue` withdraws the restore.

    Raises AgentError naming the column. Returns the row it built (the caller
    may ignore it; it is also what the tests can compare against).
    """
    table, columns = found
    quoted = table.replace('"', '""')
    schema = _schema(connection, quoted)
    samples = _samples(connection, quoted, columns)
    values = _row_values(columns, schema, "<dry-run>", title=None, preview=None,
                         step_count=0, last_modified=time.time(),
                         workdir=str(workdir) if workdir else None, samples=samples)
    for name, value in zip(columns, values):
        column = schema.get(name)
        if column is not None and column.not_null and value is None:
            declared = column.declared or "空白"
            raise AgentError(
                f"agy：conversation_summaries.db 的欄位 {columns[name]} 是 NOT NULL，"
                f"這一列填不出來（宣告型別 {declared}），不還原也不補列（#136）")
    return values


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

    Every `NOT NULL` column the real agy has gets a value (#136): both
    datetimes carry the same moment in the table's own text shape
    (`_stamp_like`), `workspace_uris` its own shape, and
    `status`／`project_id`／`app_data_dir` the value the existing rows repeat.
    Returns True when a row was inserted, False otherwise.
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
                    schema = _schema(connection, quoted)
                    samples = _samples(connection, quoted, columns)
                    place = ", ".join(f'"{columns[name]}"' for name in columns)
                    marks = ", ".join("?" for _ in columns)
                    connection.execute(
                        f'INSERT INTO "{quoted}" ({place}) VALUES ({marks})',
                        _row_values(columns, schema, conversation_id, title=title,
                                    preview=preview, step_count=step_count,
                                    last_modified=last_modified,
                                    workdir=str(workdir) if workdir else None,
                                    samples=samples))
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
