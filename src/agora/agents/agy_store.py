"""agy 的目錄與資料庫存取層（#26 PR2a 自 agy.py 拆出；行為不變）。

路徑（`home`／`conversations_dir`／`summaries_path`／`_db_path`）、快照
（`snapshot` 與其指紋／複製／重放／cross-check）、`_rows`／`_trajectory_id`、
summaries 清單（`_summary_*` 與快取、warn 旗標）。`agy_wire` 只在 `_scan_text`
需要打開快照檔時取 `_immutable`；依賴方向單向，這一層不 import wire。
"""

from __future__ import annotations

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

from .base import AgentError, iso_utc, warn

COPY_TRIES = 3             # a snapshot is retried at most this many times

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
