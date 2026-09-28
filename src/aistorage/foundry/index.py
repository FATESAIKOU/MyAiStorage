"""Foundry 搜尋索引與目錄建置模組（tasks 7.4）。

依據規格：
- docs/impl/group5-7-modules.md 第 6.4 節 (7.4)
- Foundry 讀取視圖使用同一套 readview 格式（element="foundry"）。
- 索引增加 artifacts 表，記錄產出 metadata、雜湊與 object_file_id。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from aistorage.search.index import (
    FORMAT,
    INDEX_SIZE_THRESHOLD,
    IndexMeta,
    IndexStats,
    RejectionRow,
    ensure_sqlite_version,
    normalize_time,
)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "search" / "schema.sql"


@dataclass(frozen=True)
class ArtifactRow:
    """Foundry 產出登錄紀錄列。"""

    artifact_id: str
    kind: str
    name: str
    producer: str
    produced_by_session_id: str
    created_at: str
    updated_at: str
    content_type: str | None = None
    case_id: str | None = None
    size: int | None = None
    sha256: str | None = None
    annex_key: str | None = None
    repo: str | None = None
    path: str | None = None
    link: str | None = None
    object_file_id: str | None = None


@dataclass(frozen=True)
class FoundryIndexMeta:
    """Foundry 索引的 metadata。"""

    generation: int
    built_at: str
    foundry_main_sha: str
    element: str = "foundry"


def build_foundry_index(
    path: str | Path,
    *,
    artifacts: Iterable[ArtifactRow],
    rejections: Iterable[RejectionRow] = (),
    meta: FoundryIndexMeta | IndexMeta,
) -> IndexStats:
    """全量重建 Foundry 搜尋索引到 path（先寫暫存檔，fsync 後原子改名）。"""
    ensure_sqlite_version()
    dest = Path(path)
    if str(dest.parent) not in ("", "."):
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")

    art_list = list(artifacts)
    rej_list = list(rejections)

    con = sqlite3.connect(str(tmp))
    try:
        con.executescript(schema)

        def _bool(v: Any) -> int:
            return 1 if v else 0

        for a in art_list:
            con.execute(
                """INSERT OR REPLACE INTO artifacts VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    a.artifact_id,
                    a.kind,
                    a.content_type,
                    a.name,
                    a.producer,
                    a.case_id,
                    a.produced_by_session_id,
                    normalize_time(a.created_at),
                    normalize_time(a.updated_at),
                    a.size,
                    a.sha256,
                    a.annex_key,
                    a.repo,
                    a.path,
                    a.link,
                    a.object_file_id,
                ),
            )

        for r in rej_list:
            con.execute(
                "INSERT OR REPLACE INTO rejections VALUES (?,?,?,?,?)",
                (r.item_key, r.code, normalize_time(r.at), r.item_id, _bool(r.authenticated)),
            )

        main_sha = getattr(meta, "foundry_main_sha", getattr(meta, "agora_main_sha", ""))
        element = getattr(meta, "element", "foundry")

        con.execute(
            "INSERT INTO meta VALUES ('format', ?), ('generation', ?),"
            " ('built_at', ?), ('agora_main_sha', ?), ('element', ?)",
            (
                FORMAT,
                str(meta.generation),
                normalize_time(meta.built_at),
                main_sha,
                element,
            ),
        )
        con.commit()
    finally:
        con.close()

    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    size = dest.stat().st_size
    return IndexStats(
        sessions=len(art_list),
        bytes=size,
        over_threshold=size > INDEX_SIZE_THRESHOLD,
        file_count=len(art_list) + 1,
    )
