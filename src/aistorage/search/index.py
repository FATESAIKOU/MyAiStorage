"""搜尋索引的建立（tasks 4.2，照 group4-modules.md 第 3 節調整）。

- 索引同時是讀取視圖的目錄（catalog）：除全文外，還放 metadata、快照歷史、
  Link、交接單、拒收原因與 reading 的 file id。schema 見 schema.sql（TS 照抄）。
- 全量重建：先寫同目錄暫存檔，fsync 後原子改名。
- 只用標準函式庫 sqlite3；trigram 需要 SQLite 3.34 以上，啟動時檢查。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from aistorage.clock import parse_rfc3339
from aistorage.reading import plain_text

FORMAT = "aistorage.searchindex/v1"

# 索引檔大小門檻（design D5 健康檢查用）：超過即標 over_threshold。
INDEX_SIZE_THRESHOLD = 50 * 1024 * 1024

# trigram 需要的最低 SQLite 版本（第 3 節 H）。
MIN_SQLITE_VERSION = (3, 34, 0)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def ensure_sqlite_version() -> tuple[int, int, int]:
    """檢查 SQLite 版本是否支援 FTS5 trigram，不符就 raise 並寫明原因。"""
    ver = sqlite3.sqlite_version_info
    if ver < MIN_SQLITE_VERSION:
        raise RuntimeError(
            "搜尋索引需要 SQLite 3.34 以上（含 FTS5 trigram），"
            f"目前為 {sqlite3.sqlite_version}（{sqlite3.__name__}）。"
            "Mac 請用 uv 的 Python 3.12（自帶新版 SQLite），"
            "Linux 請確認 libsqlite3 為 3.34 以上。"
        )
    return ver


def normalize_time(value: str | None) -> str | None:
    """把 RFC 3339 時間轉成 UTC 到毫秒的固定寬度字串；None 保持 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"非法時間（非字串）: {value!r}")
    try:
        dt = parse_rfc3339(value)
    except ValueError as e:
        raise ValueError(f"非法時間: {value!r}: {e}") from e
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class IndexEntry:
    """一筆待索引的 Session（由 publisher 組出，impl2 可先用假資料）。

    snapshots 元素形狀：{snapshot_sha256, snapshot_at, committed_at, via,
    file_id?, sha256?, size?}（file 欄位只在該快照有發佈 reading 時出現）。
    reading／reading_ref 描述最新快照；reading 為 None（轉換失敗）時全文為空，
    但 metadata 仍可篩選。reading_ref 形狀：{snapshot_sha256, file_id, sha256, size}。

    `agora checkout` 要的**原始紀錄本體**不在讀取視圖裡：閱讀版會丟掉工具呼叫的
    原始輸入輸出，還原不了位元組相同的開頭（ADR 0010），而發佈一份等於把
    真本的位元組複製一份到衍生物裡。它改用快照的 **annex key**（`snapshots`
    表的 `annex_key`）直接去 Agora 的物件資料夾取——key 是內容定址的位址，
    取回後用 key 內嵌的 sha256 驗證。
    """

    metadata: dict
    snapshots: list[dict] = field(default_factory=list)
    reading: dict | None = None
    reading_ref: dict | None = None


@dataclass(frozen=True)
class LinkRow:
    kind: str  # 'continuation' | 'reference'
    from_session_id: str
    to_session_id: str
    handoff_id: str | None = None
    claim_id: str | None = None
    snapshot_sha256: str | None = None
    message_id: str | None = None
    reference_id: str | None = None
    read_snapshot_at: str | None = None


@dataclass(frozen=True)
class HandoffRow:
    handoff_id: str
    target_session_id: str
    snapshot_sha256: str
    message_id: str
    producer: str
    created_at: str
    updated_at: str
    case_id: str | None = None
    body_json: str = ""
    claimed_by_claim_id: str | None = None
    claimed_by_session_id: str | None = None
    claimed_at: str | None = None
    # 作者 Session：寫這張交接單的那個 Session 的 id（讀取端用它只列主 Session
    # 寫的待認領交接單）。拿不到時為 NULL。
    author_session_id: str | None = None


@dataclass(frozen=True)
class RejectionRow:
    item_key: str
    code: str
    at: str
    item_id: str | None = None
    authenticated: bool = True


@dataclass(frozen=True)
class IndexMeta:
    generation: int
    built_at: str
    agora_main_sha: str
    converter_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexStats:
    sessions: int
    bytes: int
    over_threshold: bool
    file_count: int  # 讀取視圖檔案數估計＝reading 份數＋index 自己（D5 的 5,000 門檻用）


def _message_texts(reading: dict | None) -> list[tuple[str, str, int]]:
    """取最新快照閱讀版每則非撤銷訊息的 (message_id, idx, text)。"""
    if not isinstance(reading, dict):
        return []
    messages = reading.get("messages", [])
    if not isinstance(messages, list):
        return []
    out: list[tuple[str, str, int]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("reverted", False):
            continue
        mid = msg.get("message_id")
        idx = msg.get("index")
        if not isinstance(mid, str) or not isinstance(idx, int):
            continue
        text = plain_text({"messages": [msg]})
        if isinstance(text, str) and text:
            out.append((mid, idx, text))
    return out


def build_index(
    path: str | Path,
    *,
    entries: Iterable[IndexEntry],
    links: Iterable[LinkRow] = (),
    handoffs: Iterable[HandoffRow] = (),
    rejections: Iterable[RejectionRow] = (),
    meta: IndexMeta,
) -> IndexStats:
    """全量重建索引到 path（先寫同目錄暫存檔，fsync 後原子改名）。"""
    ensure_sqlite_version()
    dest = Path(path)
    if str(dest.parent) not in ("", "."):
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")

    entries = list(entries)
    links = list(links)
    handoffs = list(handoffs)
    rejections = list(rejections)

    con = sqlite3.connect(str(tmp))
    try:
        con.executescript(schema)

        def _bool(v: Any) -> int:
            return 1 if v else 0

        n_readings = 0
        for entry in entries:
            m = entry.metadata
            in_progress = m.get("in_progress", False)
            con.execute(
                """INSERT OR REPLACE INTO sessions VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.get("session_id"),
                    m.get("source"),
                    m.get("title"),
                    m.get("producer"),
                    m.get("case_id"),
                    m.get("status"),
                    normalize_time(m.get("stopped_at")),
                    _bool(in_progress) if not isinstance(in_progress, int) else in_progress,
                    normalize_time(m.get("created_at")),
                    normalize_time(m.get("updated_at")),
                    normalize_time(m.get("snapshot_at")),
                    m.get("raw_sha256"),
                    m.get("raw_size"),
                    m.get("parent_id"),
                    m.get("reading_status"),
                    m.get("reading_error_code"),
                    normalize_time(m.get("committed_at")),
                    # 預留期限：只有 status='reserved' 的 Session 有值（review-2bc0785 M2）
                    normalize_time(m.get("reserved_until")),
                ),
            )
            sid = m.get("session_id")
            latest_sha = (entry.reading_ref or {}).get("snapshot_sha256")
            seen: set[str] = set()
            for snap in entry.snapshots:
                sha = snap.get("snapshot_sha256")
                if not isinstance(sha, str) or not sha or sha in seen:
                    continue
                seen.add(sha)
                con.execute(
                    "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                    (sid, sha, normalize_time(snap.get("snapshot_at")),
                     normalize_time(snap.get("committed_at")), snap.get("via"),
                     snap.get("annex_key")),
                )
                if snap.get("file_id") is not None:
                    con.execute(
                        "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)",
                        (sid, sha, snap.get("file_id"), snap.get("sha256"),
                         snap.get("size"), _bool(sha == latest_sha)),
                    )
                    n_readings += 1
            if entry.reading_ref and entry.reading_ref.get("snapshot_sha256") not in seen:
                r = entry.reading_ref
                con.execute(
                    "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)",
                    (sid, r.get("snapshot_sha256"), r.get("file_id"),
                     r.get("sha256"), r.get("size"), 1),
                )
                n_readings += 1
            for mid, idx, text in _message_texts(entry.reading):
                con.execute(
                    "INSERT INTO message_fts(text, session_id, message_id, idx)"
                    " VALUES (?,?,?,?)",
                    (text, sid, mid, idx),
                )
            if m.get("title"):
                con.execute(
                    "INSERT INTO title_fts(title, session_id) VALUES (?,?)",
                    (m.get("title"), sid),
                )

        con.executemany(
            "INSERT INTO links VALUES (?,?,?,?,?,?,?,?,?)",
            [(l.kind, l.from_session_id, l.to_session_id, l.handoff_id, l.claim_id,
              l.snapshot_sha256, l.message_id, l.reference_id, l.read_snapshot_at)
             for l in links],
        )
        for h in handoffs:
            con.execute(
                "INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (h.handoff_id, h.target_session_id, h.snapshot_sha256, h.message_id,
                 h.producer, normalize_time(h.created_at), normalize_time(h.updated_at),
                 h.case_id, h.body_json, h.claimed_by_claim_id,
                 h.claimed_by_session_id,
                 normalize_time(h.claimed_at) if h.claimed_at else None,
                 h.author_session_id),
            )
        for r in rejections:
            con.execute(
                "INSERT OR REPLACE INTO rejections VALUES (?,?,?,?,?)",
                (r.item_key, r.code, normalize_time(r.at), r.item_id, _bool(r.authenticated)),
            )
        con.execute(
            "INSERT INTO meta VALUES ('format', ?), ('generation', ?),"
            " ('built_at', ?), ('agora_main_sha', ?), ('converter_versions', ?)",
            (FORMAT, str(meta.generation), normalize_time(meta.built_at),
             meta.agora_main_sha,
             json.dumps(meta.converter_versions, sort_keys=True)),
        )
        con.commit()
    finally:
        con.close()

    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    size = dest.stat().st_size
    return IndexStats(
        sessions=len(entries),
        bytes=size,
        over_threshold=size > INDEX_SIZE_THRESHOLD,
        file_count=n_readings + 1,
    )


_DUMP_TABLES = (
    "meta",
    "sessions",
    "snapshots",
    "readings",
    "links",
    "handoffs",
    "rejections",
)

_DUMP_ORDER = {
    "meta": "key",
    "sessions": "session_id",
    "snapshots": "session_id, snapshot_sha256",
    "readings": "session_id, snapshot_sha256",
    "links": ("from_session_id, to_session_id,"
              " COALESCE(handoff_id,''), COALESCE(reference_id,''),"
              " COALESCE(snapshot_sha256,''), COALESCE(message_id,'')"),
    "handoffs": "handoff_id",
    "rejections": "item_key",
}


def dump_tables(path: str | Path) -> dict[str, list[tuple]]:
    """以各表主鍵排序倒出內容（G：確定性比對用，不比較檔案雜湊）。

    含 message_fts／title_fts 的內容（依 session_id、idx 排序），不含 FTS 內部表。
    """
    ensure_sqlite_version()
    con = sqlite3.connect(str(path))
    try:
        out: dict[str, list[tuple]] = {}
        for table in _DUMP_TABLES:
            out[table] = [tuple(row) for row in
                          con.execute(f"SELECT * FROM {table} ORDER BY {_DUMP_ORDER[table]}")]
        out["message_fts"] = [tuple(row) for row in con.execute(
            "SELECT text, session_id, message_id, idx FROM message_fts"
            " ORDER BY session_id, idx")]
        out["title_fts"] = [tuple(row) for row in con.execute(
            "SELECT title, session_id FROM title_fts ORDER BY session_id")]
        return out
    finally:
        con.close()
