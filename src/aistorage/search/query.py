"""搜尋索引的查詢（tasks 4.2，照 group4-modules.md 第 3 節 C／D）。

純 SQLite，不碰網路；讀者端使用。查詢語意的規格見 schemas/searchindex.md，
TS 用戶端照該文件實作，結果須與此處一致。

- `text` 命中＝訊息文字包含 query 子字串（只有 ASCII 不分大小寫，不做 NFKC）。
  3 字元以上走 FTS5 trigram；查詢中任一片語／詞不足 3 字元時整組改走 LIKE，
  兩者結果相同；`matched_by` 只是除錯資訊。
- 多個詞以空白分隔，全部都要命中（AND）；支援 `"…"` 片語；不支援 FTS5
  運算子語法（query 在程式裡逐字跳脫，全部包成雙引號片語）。
- 排序一律 `updated_at` 由新到舊，平手依 `session_id`，不用 bm25。
- 分頁：`limit`（預設 50，上限 500）＋`cursor`（上一頁最後一筆的
  `(updated_at, session_id)`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from typing import Any

from aistorage.search.index import (
    HandoffRow,
    LinkRow,
    RejectionRow,
    ensure_sqlite_version,
    normalize_time,
)

MAX_LIMIT = 500
LIKE_WINDOW = 40


@dataclass(frozen=True)
class Query:
    text: str | None = None
    title_contains: str | None = None
    case_id: str | None = None
    source: str | None = None
    status: str | None = None
    producer: str | None = None
    parent_id: str | None = None  # None＝不限；""＝只要主 Session
    in_progress: bool | None = None
    updated_after: str | None = None
    updated_before: str | None = None
    limit: int = 50
    cursor: tuple[str, str] | None = None


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    source: str
    title: str | None
    producer: str
    case_id: str | None
    status: str
    stopped_at: str | None
    in_progress: bool
    created_at: str
    updated_at: str
    snapshot_at: str
    raw_sha256: str
    raw_size: int
    parent_id: str | None
    reading_status: str
    reading_error_code: str | None
    committed_at: str


@dataclass(frozen=True)
class MessageMatch:
    message_id: str
    index: int
    snippet: str


@dataclass(frozen=True)
class Hit:
    session: SessionRow
    matches: tuple[MessageMatch, ...] = ()


@dataclass(frozen=True)
class ReadingRef:
    """reading 檔的定位（index 同時是目錄）。readview/model.py（4.1）可直接取用此型別。"""

    session_id: str
    snapshot_sha256: str
    file_id: str
    sha256: str
    size: int
    is_latest: bool


def _session_row(row: tuple) -> SessionRow:
    return SessionRow(
        session_id=row[0], source=row[1], title=row[2], producer=row[3],
        case_id=row[4], status=row[5], stopped_at=row[6],
        in_progress=bool(row[7]), created_at=row[8], updated_at=row[9],
        snapshot_at=row[10], raw_sha256=row[11], raw_size=row[12],
        parent_id=row[13], reading_status=row[14],
        reading_error_code=row[15], committed_at=row[16],
    )


def _split_terms(text: str) -> list[tuple[bool, str]]:
    """把查詢拆成 [(is_phrase, term)]；未閉合的引號視為開到結尾。"""
    terms: list[tuple[bool, str]] = []
    buf: list[str] = []
    in_quote = False
    quoted_empty = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if in_quote:
                terms.append((True, "".join(buf)))
                buf = []
                in_quote = False
            else:
                if "".join(buf).strip():
                    for word in "".join(buf).split():
                        terms.append((False, word))
                buf = []
                in_quote = True
                quoted_empty = True
            i += 1
            continue
        if ch.isspace() and not in_quote:
            if "".join(buf).strip():
                for word in "".join(buf).split():
                    terms.append((False, word))
            buf = []
            i += 1
            continue
        buf.append(ch)
        if in_quote:
            quoted_empty = False
        i += 1
    rest = "".join(buf)
    if in_quote:
        terms.append((True, rest))
    elif rest.strip():
        for word in rest.split():
            terms.append((False, word))
    elif quoted_empty:
        pass
    return [(ph, t) for ph, t in terms if t]


def _fts_phrase(term: str) -> str:
    """跳脫成 FTS5 雙引號片語（不支援運算子語法，全部按字面比對）。"""
    return '"' + term.replace('"', '""') + '"'


def _escape_like(text: str) -> str:
    return (text.replace("\\", "\\\\")
                 .replace("%", "\\%")
                 .replace("_", "\\_"))


def _filter_sql(q: Query, alias: str = "s") -> tuple[str, list[Any]]:
    """條件篩選片段（D）。時間用固定寬度字串比較＝datetime 比較。"""
    clauses: list[str] = []
    params: list[Any] = []
    if q.title_contains:
        clauses.append(f"{alias}.title LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(q.title_contains)}%")
    for key in ("case_id", "source", "status", "producer"):
        value = getattr(q, key)
        if value is not None:
            clauses.append(f"{alias}.{key} = ?")
            params.append(value)
    if q.parent_id is not None:
        if q.parent_id == "":
            clauses.append(f"{alias}.parent_id IS NULL")
        else:
            clauses.append(f"{alias}.parent_id = ?")
            params.append(q.parent_id)
    if q.in_progress is not None:
        clauses.append(f"{alias}.in_progress = ?")
        params.append(1 if q.in_progress else 0)
    if q.updated_after is not None:
        clauses.append(f"{alias}.updated_at >= ?")
        params.append(normalize_time(q.updated_after))
    if q.updated_before is not None:
        clauses.append(f"{alias}.updated_at <= ?")
        params.append(normalize_time(q.updated_before))
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _cursor_sql(cursor: tuple[str, str] | None) -> tuple[str, list[Any]]:
    if cursor is None:
        return "", []
    return (" AND (s.updated_at < ? OR (s.updated_at = ? AND s.session_id > ?))",
            [cursor[0], cursor[0], cursor[1]])


def _like_snippet(text: str, needle: str, window: int = LIKE_WINDOW) -> str:
    """取命中前後 window 字元的片段（LIKE 路徑用）。"""
    idx = text.lower().find(needle.lower()) if needle.isascii() else text.find(needle)
    if idx < 0:
        idx = 0
    start = max(0, idx - window)
    end = min(len(text), idx + len(needle) + window)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def search(db: sqlite3.Connection, q: Query) -> tuple[list[Hit], tuple[str, str] | None]:
    """查索引，回傳 (hits, next_cursor)。hits 依 updated_at 新到舊、session_id 排序。"""
    ensure_sqlite_version()
    limit = max(0, min(int(q.limit), MAX_LIMIT))
    if limit == 0:
        return [], None
    # 多取一筆來判斷還有沒有下一頁。
    fetch = limit + 1

    words = _split_terms(q.text.strip()) if isinstance(q.text, str) and q.text.strip() else []
    use_like = any(len(term) < 3 for _, term in words)

    filter_sql, filter_params = _filter_sql(q)
    cursor_sql, cursor_params = _cursor_sql(q.cursor)

    # 第一階段：先取命中的 Session 頁（分頁以 Session 為單位）。
    # text 同時比對訊息文字與標題（PM 決定）：只命中標題的 Session 也回傳，
    # matches 為空（標題見 session.title）。
    if not words:
        cur = db.execute(
            "SELECT session_id FROM sessions s WHERE 1 = 1"
            f"{filter_sql}{cursor_sql}"
            " ORDER BY s.updated_at DESC, s.session_id ASC"
            f" LIMIT {fetch}",
            [*filter_params, *cursor_params],
        )
        page_ids = [r[0] for r in cur.fetchall()]
        match_rows: dict[str, list[tuple[str, int, str]]] = {sid: [] for sid in page_ids}
    else:
        title_likes = " AND ".join(["s.title LIKE ? ESCAPE '\\'"] * len(words))
        title_params = [f"%{_escape_like(term)}%" for _, term in words]
        if use_like:
            msg_cond = "(" + " AND ".join(["m.text LIKE ? ESCAPE '\\'"] * len(words)) + ")"
            msg_params = [f"%{_escape_like(term)}%" for _, term in words]
        else:
            msg_cond = "message_fts MATCH ?"
            msg_params = [" AND ".join(_fts_phrase(term) for _, term in words)]
        cur = db.execute(
            "SELECT session_id, updated_at FROM ("
            " SELECT DISTINCT s.session_id AS session_id, s.updated_at AS updated_at"
            " FROM message_fts m JOIN sessions s ON s.session_id = m.session_id"
            f" WHERE {msg_cond}{filter_sql}{cursor_sql}"
            " UNION"
            " SELECT s.session_id AS session_id, s.updated_at AS updated_at"
            " FROM sessions s"
            f" WHERE ({title_likes}){filter_sql}{cursor_sql}"
            ") ORDER BY updated_at DESC, session_id ASC"
            f" LIMIT {fetch}",
            [*msg_params, *filter_params, *cursor_params,
             *title_params, *filter_params, *cursor_params],
        )
        page_ids = [r[0] for r in cur.fetchall()]
        match_rows = {sid: [] for sid in page_ids}
        # 第二階段：取本頁各 Session 的訊息命中（只命中標題者自然為空）。
        if page_ids:
            placeholders = ",".join("?" * len(page_ids))
            if use_like:
                likes = " AND ".join(["text LIKE ? ESCAPE '\\'"] * len(words))
                like_params = [f"%{_escape_like(term)}%" for _, term in words]
                cur = db.execute(
                    "SELECT session_id, message_id, idx, text FROM message_fts"
                    f" WHERE session_id IN ({placeholders}) AND ({likes})"
                    " ORDER BY session_id, idx",
                    [*page_ids, *like_params],
                )
                needle = words[0][1]
                for sid, mid, idx, text in cur.fetchall():
                    if sid in match_rows:
                        match_rows[sid].append(
                            (mid, idx, _like_snippet(text or "", needle)))
            else:
                match_expr = " AND ".join(_fts_phrase(term) for _, term in words)
                cur = db.execute(
                    "SELECT session_id, message_id, idx,"
                    " snippet(message_fts, 0, '', '', '…', 15) FROM message_fts"
                    f" WHERE session_id IN ({placeholders}) AND message_fts MATCH ?"
                    " ORDER BY session_id, idx",
                    [*page_ids, match_expr],
                )
                for sid, mid, idx, snip in cur.fetchall():
                    if sid in match_rows:
                        match_rows[sid].append((mid, idx, snip or ""))

    has_more = len(page_ids) > limit
    page_ids = page_ids[:limit]
    hits = [
        Hit(session=get_session_row(db, sid),  # type: ignore[arg-type]
            matches=tuple(MessageMatch(message_id=mid, index=idx, snippet=snip)
                          for mid, idx, snip in match_rows[sid]))
        for sid in page_ids
    ]
    if has_more:
        last = hits[-1].session
        return hits, (last.updated_at, last.session_id)
    return hits, None


def get_session_row(db: sqlite3.Connection, session_id: str) -> SessionRow | None:
    ensure_sqlite_version()
    row = db.execute("SELECT * FROM sessions WHERE session_id = ?",
                     (session_id,)).fetchone()
    return _session_row(row) if row is not None else None


def _link_row(row: tuple) -> LinkRow:
    return LinkRow(kind=row[0], from_session_id=row[1], to_session_id=row[2],
                   handoff_id=row[3], claim_id=row[4], snapshot_sha256=row[5],
                   message_id=row[6], reference_id=row[7], read_snapshot_at=row[8])


#: 讀取者認得的 Session Link 類型（specs/agora/session-link）。
#: 類型集合可以擴充；不認得新類型的讀取者 MUST 忽略該 Link，而不是視為錯誤。
#: 索引照存（build_index 不過濾），只在讀出時忽略。
KNOWN_LINK_KINDS = frozenset({"continuation", "reference"})


def get_links(db: sqlite3.Connection, session_id: str
              ) -> tuple[list[LinkRow], list[LinkRow]]:
    """回傳 (出去的 Link, 指向它的 Link)。

    未知 kind 的 Link 在此忽略（spec session-link「兩種類型」）：
    不報錯、不回傳。寫入路徑（publisher.collect_links）目前只產出
    continuation／reference；將來新增類型時舊讀者照常運作。
    """
    ensure_sqlite_version()
    order = ("ORDER BY from_session_id, to_session_id,"
             " COALESCE(handoff_id,''), COALESCE(reference_id,'')")
    out_rows = [_link_row(r) for r in db.execute(
        f"SELECT * FROM links WHERE from_session_id = ? {order}", (session_id,))]
    in_rows = [_link_row(r) for r in db.execute(
        f"SELECT * FROM links WHERE to_session_id = ? {order}", (session_id,))]
    return ([r for r in out_rows if r.kind in KNOWN_LINK_KINDS],
            [r for r in in_rows if r.kind in KNOWN_LINK_KINDS])


def _handoff_row(row: tuple) -> HandoffRow:
    return HandoffRow(
        handoff_id=row[0], target_session_id=row[1], snapshot_sha256=row[2],
        message_id=row[3], producer=row[4], created_at=row[5], updated_at=row[6],
        case_id=row[7], body_json=row[8], claimed_by_claim_id=row[9],
        claimed_by_session_id=row[10], claimed_at=row[11],
        author_session_id=row[12] if len(row) > 12 else None,
    )


def get_handoff(db: sqlite3.Connection, handoff_id: str) -> HandoffRow | None:
    ensure_sqlite_version()
    row = db.execute("SELECT * FROM handoffs WHERE handoff_id = ?",
                     (handoff_id,)).fetchone()
    if row is None:
        return None
    return _handoff_row(row)


def get_handoffs(db: sqlite3.Connection, *, target_session_id: str | None = None,
                 open_only: bool = False) -> list[HandoffRow]:
    ensure_sqlite_version()
    clauses: list[str] = []
    params: list[Any] = []
    if target_session_id is not None:
        clauses.append("target_session_id = ?")
        params.append(target_session_id)
    if open_only:
        clauses.append("claimed_by_claim_id IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return [_handoff_row(r) for r in db.execute(
        f"SELECT * FROM handoffs{where} ORDER BY updated_at DESC, handoff_id ASC",
        params)]


def get_reading_ref(db: sqlite3.Connection, session_id: str,
                    snapshot_sha256: str | None = None) -> ReadingRef | None:
    """取 reading 定位；snapshot 為 None 時取最新。"""
    ensure_sqlite_version()
    if snapshot_sha256 is None:
        row = db.execute(
            "SELECT * FROM readings WHERE session_id = ? AND is_latest = 1",
            (session_id,)).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM readings WHERE session_id = ? AND snapshot_sha256 = ?",
            (session_id, snapshot_sha256)).fetchone()
    if row is None:
        return None
    return ReadingRef(session_id=row[0], snapshot_sha256=row[1], file_id=row[2],
                      sha256=row[3], size=row[4], is_latest=bool(row[5]))


def get_rejection(db: sqlite3.Connection, item_key: str) -> RejectionRow | None:
    ensure_sqlite_version()
    row = db.execute("SELECT * FROM rejections WHERE item_key = ?",
                     (item_key,)).fetchone()
    if row is None:
        return None
    return RejectionRow(item_key=row[0], code=row[1], at=row[2],
                        item_id=row[3], authenticated=bool(row[4]))
