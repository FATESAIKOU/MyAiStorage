"""Agora 搜尋索引（tasks 4.2）。

依 scratchpad/g4-api-4.2.md 介面草案、design D5「搜尋」、spec agora/search。

- 索引是一個 SQLite 檔（FTS5，tokenize='trigram'），由提交流程每次提交後
  全量重建，放進讀取視圖；讀取介面下載到本地查詢。
- 只用 Python 標準函式庫的 sqlite3。FTS5 trigram 在本機（SQLite 3.51.0）
  與 Ubuntu 24.04（libsqlite3-0 3.45.1）皆已實測可用；trigram 查詢最少
  要 3 個字元，不足時改走 LIKE（見 search）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from aistorage.reading import plain_text

# 索引檔大小門檻（design D5 健康檢查用）：超過即標 over_threshold。
INDEX_SIZE_THRESHOLD = 50 * 1024 * 1024

_SESSION_COLUMNS = (
    "session_id",
    "source",
    "title",
    "producer",
    "case_id",
    "status",
    "created_at",
    "updated_at",
    "snapshot_at",
    "parent_id",
)

# LIKE 轉義字元。
_LIKE_ESCAPE = "\\"


@dataclass
class IndexEntry:
    """一筆待索引的 Session：真本 metadata 與閱讀版。

    reading 為 None（轉換失敗）時 body 為空，但 metadata 仍可篩選。
    """

    metadata: dict
    reading: dict | None


@dataclass(frozen=True)
class IndexStats:
    """一次全量重建的統計。"""

    sessions: int
    bytes: int
    over_threshold: bool


@dataclass(frozen=True)
class Hit:
    """一筆搜尋命中。"""

    session_id: str
    title: str | None
    snippet: str | None
    matched_by: str  # "fts" | "like" | "filter"
    snapshot_at: str


def _body_of(reading: dict | None) -> str:
    """取索引用內文：預設不含 reverted 與 reasoning。失敗時回空字串。"""
    if not isinstance(reading, dict):
        return ""
    try:
        text = plain_text(reading)
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


def _row_of(entry: IndexEntry) -> tuple[Any, ...]:
    meta = entry.metadata if isinstance(entry.metadata, dict) else {}
    return tuple(meta.get(col) for col in _SESSION_COLUMNS)


def build_index(path: str | Path, entries: Iterable[IndexEntry]) -> IndexStats:
    """全量重建索引到 path（先寫同目錄暫存檔，fsync 後原子改名）。

    同一個 session_id 重複出現時以後寫入者為準。
    """
    dest = Path(path)
    if dest.parent != Path(""):
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")

    rows: dict[str, tuple[Any, ...]] = {}
    bodies: dict[str, str] = {}
    for entry in entries:
        row = _row_of(entry)
        session_id = row[0]
        if not isinstance(session_id, str) or not session_id:
            continue
        rows[session_id] = row
        bodies[session_id] = _body_of(entry.reading)

    con = sqlite3.connect(str(tmp))
    try:
        con.execute(
            """CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                producer TEXT,
                case_id TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                snapshot_at TEXT,
                parent_id TEXT
            )"""
        )
        con.execute(
            """CREATE VIRTUAL TABLE session_fts
               USING fts5(session_id UNINDEXED, title, body, tokenize="trigram")"""
        )
        con.executemany(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            list(rows.values()),
        )
        con.executemany(
            "INSERT OR REPLACE INTO session_fts(session_id, title, body) VALUES (?,?,?)",
            [(sid, rows[sid][2], bodies[sid]) for sid in rows],
        )
        con.commit()
    finally:
        con.close()

    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    size = dest.stat().st_size
    return IndexStats(
        sessions=len(rows),
        bytes=size,
        over_threshold=size > INDEX_SIZE_THRESHOLD,
    )


def _escape_like(text: str) -> str:
    """轉義 LIKE 萬用字元（%、_ 與轉義字元本身）。"""
    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _filter_clause(filters: dict | None) -> tuple[str, list[Any]]:
    """組條件篩選的 SQL 片段。時間比較用 RFC3339 Z 字串字典序。"""
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for key in ("source", "producer", "case_id", "status"):
        if key in filters and filters[key] is not None:
            clauses.append(f"s.{key} = ?")
            params.append(filters[key])
    if filters.get("updated_after") is not None:
        clauses.append("s.updated_at >= ?")
        params.append(filters["updated_after"])
    if filters.get("updated_before") is not None:
        clauses.append("s.updated_at <= ?")
        params.append(filters["updated_before"])
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _like_snippet(title: str | None, body: str, text: str, window: int = 40) -> str | None:
    """在 title 優先、其次 body 取命中前後 window 字元的片段。"""
    for source in (title or "", body):
        idx = source.find(text)
        if idx >= 0:
            start = max(0, idx - window)
            end = min(len(source), idx + len(text) + window)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(source) else ""
            return f"{prefix}{source[start:end]}{suffix}"
    return None


def search(
    path: str | Path,
    *,
    text: str | None = None,
    filters: dict | None = None,
    limit: int = 50,
) -> list[Hit]:
    """查索引：全文（FTS5 trigram）可與條件篩選組合。

    - text 不足 3 個字元時改用 LIKE 查 title/body，並標 matched_by="like"。
    - text 為空時只做條件篩選，matched_by="filter"，snippet 為 None。
    - filters 支援 source、producer、case_id、status、updated_after、
      updated_before（RFC3339 Z）。
    """
    db = Path(path)
    if not db.is_file():
        raise FileNotFoundError(f"找不到搜尋索引檔: {db}")
    query = text.strip() if isinstance(text, str) else ""
    where, params = _filter_clause(filters)
    limit = max(0, int(limit))

    con = sqlite3.connect(str(db))
    try:
        if not query:
            cur = con.execute(
                "SELECT s.session_id, s.title, s.snapshot_at FROM sessions s"
                f" WHERE 1 = 1{where} ORDER BY s.updated_at DESC, s.session_id ASC"
                f" LIMIT {limit}",
                params,
            )
            return [
                Hit(session_id=sid, title=title, snippet=None,
                    matched_by="filter", snapshot_at=snap)
                for sid, title, snap in cur.fetchall()
            ]
        if len(query) < 3:
            pattern = f"%{_escape_like(query)}%"
            cur = con.execute(
                "SELECT s.session_id, s.title, s.snapshot_at, f.body"
                " FROM sessions s JOIN session_fts f ON f.session_id = s.session_id"
                f" WHERE (s.title LIKE ? ESCAPE '\\' OR f.body LIKE ? ESCAPE '\\')"
                f"{where} ORDER BY s.updated_at DESC, s.session_id ASC LIMIT {limit}",
                [pattern, pattern, *params],
            )
            hits: list[Hit] = []
            for sid, title, snap, body in cur.fetchall():
                hits.append(Hit(
                    session_id=sid,
                    title=title,
                    snippet=_like_snippet(title, body or "", query),
                    matched_by="like",
                    snapshot_at=snap,
                ))
            return hits
        phrase = '"' + query.replace('"', '""') + '"'
        cur = con.execute(
            "SELECT s.session_id, s.title, s.snapshot_at,"
            " snippet(session_fts, 2, '', '', '…', 15)"
            " FROM session_fts JOIN sessions s ON s.session_id = session_fts.session_id"
            f" WHERE session_fts MATCH ?{where} ORDER BY rank LIMIT {limit}",
            [phrase, *params],
        )
        hits = []
        for sid, title, snap, snip in cur.fetchall():
            snippet = snip or (title[:120] if title else None)
            hits.append(Hit(session_id=sid, title=title, snippet=snippet,
                            matched_by="fts", snapshot_at=snap))
        return hits
    finally:
        con.close()
