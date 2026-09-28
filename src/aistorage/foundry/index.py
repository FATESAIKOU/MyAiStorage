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
class ArtifactIssue:
    """無法解析 object_file_id 的原因（發佈端要記進 RunReport，review F-H3）。"""

    artifact_id: str
    code: str          # missing_annex_key / key_not_in_pin / object_not_found /
                       # checksum_mismatch / size_mismatch
    detail: str = ""


@dataclass(frozen=True)
class FoundryIndexMeta:
    """Foundry 索引的 metadata。"""

    generation: int
    built_at: str
    foundry_main_sha: str
    element: str = "foundry"


def resolve_object_file_ids(
    rows: Iterable[ArtifactRow],
    *,
    drive: Any,
    prefix_folder_id: str,
    allowed_keys: Iterable[str] | None = None,
) -> tuple[list[ArtifactRow], list[ArtifactIssue]]:
    """把 contained 產出的 annex key 解析成 Drive 上的 object_file_id（F-H3）。

    讀取介面是「以 id 從 Drive 取物件」（D5），所以發佈端必須在建立索引之前
    把 file id 寫進來；沒有寫的話 `reader.get()` 每一次都會
    `MismatchError("未記錄 object_file_id")`。

    解析規則（與 review 建議一致）：
    - 以前綴資料夾的列舉結果比對 `name == annex_key`（git-remote-annex 以 key 命名）；
    - Drive 有提供 checksum／size 時必須相符，否則這一筆不發佈；
    - 可以傳 `allowed_keys`（正式 pin 的 annex_keys）擋掉不在釘選內容裡的 key；
    - 找不到或對不上就**不發佈這一筆**，並回傳原因給呼叫端記進 RunReport。

    回傳 `(可發佈的 rows, 問題清單)`；callers 不該自行比對檔名。
    """
    from dataclasses import replace as _replace

    allowed = frozenset(allowed_keys) if allowed_keys is not None else None
    by_name: dict[str, Any] = {}
    for child in drive.list_children(prefix_folder_id):
        if not child.is_folder:
            by_name[child.name] = child

    ok: list[ArtifactRow] = []
    issues: list[ArtifactIssue] = []
    for row in rows:
        if not row.annex_key:
            issues.append(ArtifactIssue(row.artifact_id, "missing_annex_key",
                                        "catalog 沒有記 annex_key"))
            continue
        if allowed is not None and row.annex_key not in allowed:
            issues.append(ArtifactIssue(
                row.artifact_id, "key_not_in_pin",
                f"annex_key 不在正式 pin 的 annex_keys 內: {row.annex_key}"))
            continue
        f = by_name.get(row.annex_key)
        if f is None:
            issues.append(ArtifactIssue(
                row.artifact_id, "object_not_found",
                f"前綴下找不到名為 {row.annex_key} 的檔案"))
            continue
        if row.sha256 and f.sha256 and f.sha256.lower() != row.sha256.lower():
            issues.append(ArtifactIssue(
                row.artifact_id, "checksum_mismatch",
                f"Drive checksum {f.sha256} 與 catalog {row.sha256} 不符"))
            continue
        if row.size is not None and f.size is not None and int(f.size) != int(row.size):
            issues.append(ArtifactIssue(
                row.artifact_id, "size_mismatch",
                f"Drive size {f.size} 與 catalog {row.size} 不符"))
            continue
        ok.append(_replace(row, object_file_id=f.id))
    return ok, issues


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
