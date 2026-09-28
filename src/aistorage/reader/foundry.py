"""Foundry 讀取介面（唯讀，tasks 7.4）。

與 AgoraReader 分開（ADR 0001）。
依據：
- docs/impl/group5-7-modules.md 第 6.4 節 (7.4)
- 查詢產出登錄（find）
- 取回本體或原處資訊（get）：
  - contained：以 index 記錄的 object_file_id 依 id 定位自 Drive 取回，並驗證 sha256。
  - link：回傳出處（origin）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
from pathlib import Path
from typing import Any

from aistorage.clock import Clock
from aistorage.drive.model import DriveClient
from aistorage.errors import MismatchError
from aistorage.foundry.index import ArtifactRow
from aistorage.reader import Freshness, Result
from aistorage.reader.client import ReadViewClient
from aistorage.reader.freshness import evaluate_freshness
from aistorage.search.index import RejectionRow, normalize_time
from aistorage.search.query import get_rejection


@dataclass(frozen=True)
class FoundArtifact:
    """find 的一筆產出結果：ArtifactRow ＋ 它自己的 Freshness。"""

    artifact: ArtifactRow
    freshness: Freshness


@dataclass(frozen=True)
class ArtifactContent:
    """get 的產出內容結果。"""

    row: ArtifactRow
    kind: str  # "contained" | "link"
    data: bytes | None = None
    path: Path | None = None
    origin: dict[str, Any] | None = None


class FoundryReader:
    """Foundry 產出儲存庫讀取介面（唯讀）。"""

    def __init__(
        self,
        client: ReadViewClient,
        *,
        drive: DriveClient | None = None,
        clock: Clock,
    ) -> None:
        self._client = client
        self._drive = drive or getattr(client, "_drive", None)
        self._clock = clock

    def _fresh(
        self,
        manifest: dict[str, Any],
        *,
        snapshot_at: str | None,
        status: str | None = None,
        stopped_at: str | None = None,
        max_lag: timedelta | None,
    ) -> Freshness:
        return Freshness(
            **evaluate_freshness(
                snapshot_at=snapshot_at,
                status=status,
                stopped_at=stopped_at,
                generation=manifest["generation"],
                published_at=manifest["published_at"],
                now=self._clock.now(),
                max_lag=max_lag,
            )
        )

    def _worst(
        self,
        manifest: dict[str, Any],
        at_list: list[str | None],
        max_lag: timedelta | None,
    ) -> Freshness:
        known = [a for a in at_list if a]
        if not known:
            return self._fresh(manifest, snapshot_at=None, max_lag=max_lag)
        return self._fresh(manifest, snapshot_at=min(known), max_lag=max_lag)

    def manifest(self) -> dict[str, Any]:
        """目前讀取視圖的 manifest。"""
        return dict(self._client.manifest())

    def find(
        self,
        *,
        type: str | None = None,
        kind: str | None = None,
        content_type: str | None = None,
        case_id: str | None = None,
        producer: str | None = None,
        session_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        max_lag: timedelta | None = None,
    ) -> Result[list[FoundArtifact]]:
        """依型態、所屬案件、產生者、時間與產出它的 Session 查詢產出登錄。"""
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            conditions: list[str] = []
            params: list[Any] = []

            if type is not None:
                conditions.append("(kind = ? OR content_type = ?)")
                params.extend([type, type])
            if kind is not None:
                conditions.append("kind = ?")
                params.append(kind)
            if content_type is not None:
                conditions.append("content_type = ?")
                params.append(content_type)
            if case_id is not None:
                conditions.append("case_id = ?")
                params.append(case_id)
            if producer is not None:
                conditions.append("producer = ?")
                params.append(producer)
            if session_id is not None:
                conditions.append("produced_by_session_id = ?")
                params.append(session_id)
            if since is not None:
                conditions.append("created_at >= ?")
                params.append(normalize_time(since))
            if until is not None:
                conditions.append("created_at <= ?")
                params.append(normalize_time(until))

            where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = (
                "SELECT artifact_id, kind, content_type, name, producer, case_id,"
                " produced_by_session_id, created_at, updated_at, size, sha256,"
                " annex_key, repo, path, link, object_file_id"
                f" FROM artifacts{where_sql} ORDER BY created_at DESC, artifact_id ASC"
            )

            cursor = db.execute(sql, tuple(params))
            artifacts: list[ArtifactRow] = []
            for r in cursor.fetchall():
                artifacts.append(
                    ArtifactRow(
                        artifact_id=r[0],
                        kind=r[1],
                        content_type=r[2],
                        name=r[3],
                        producer=r[4],
                        case_id=r[5],
                        produced_by_session_id=r[6],
                        created_at=r[7],
                        updated_at=r[8],
                        size=r[9],
                        sha256=r[10],
                        annex_key=r[11],
                        repo=r[12],
                        path=r[13],
                        link=r[14],
                        object_file_id=r[15],
                    )
                )
        finally:
            db.close()

        found = [
            FoundArtifact(
                artifact=a,
                freshness=self._fresh(
                    manifest,
                    snapshot_at=a.created_at,
                    max_lag=max_lag,
                ),
            )
            for a in artifacts
        ]
        freshness = self._worst(
            manifest,
            [a.created_at for a in artifacts],
            max_lag,
        )
        return Result(value=found, freshness=freshness)

    def get(
        self,
        artifact_id: str,
        dest: Path | str | None = None,
    ) -> Result[ArtifactContent]:
        """取回產出項目。

        - contained：依 object_file_id 自 Drive 下載並以 sha256 驗證內容。
        - link：回傳出處字典（origin）。
        """
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            sql = (
                "SELECT artifact_id, kind, content_type, name, producer, case_id,"
                " produced_by_session_id, created_at, updated_at, size, sha256,"
                " annex_key, repo, path, link, object_file_id"
                " FROM artifacts WHERE artifact_id = ?"
            )
            cursor = db.execute(sql, (artifact_id,))
            r = cursor.fetchone()
            if r is None:
                raise KeyError(f"Foundry 讀取視圖沒有這個產出: {artifact_id}")
            row = ArtifactRow(
                artifact_id=r[0],
                kind=r[1],
                content_type=r[2],
                name=r[3],
                producer=r[4],
                case_id=r[5],
                produced_by_session_id=r[6],
                created_at=r[7],
                updated_at=r[8],
                size=r[9],
                sha256=r[10],
                annex_key=r[11],
                repo=r[12],
                path=r[13],
                link=r[14],
                object_file_id=r[15],
            )
        finally:
            db.close()

        if row.kind == "contained":
            if not row.object_file_id:
                raise MismatchError(f"contained 產出未記錄 object_file_id: {artifact_id}")
            if self._drive is None:
                raise RuntimeError("FoundryReader 缺少 drive 用戶端，無法取回 contained 物件")

            if dest is not None:
                dest_p = Path(dest)
                dest_p.parent.mkdir(parents=True, exist_ok=True)
                self._drive.download(row.object_file_id, dest_p, max_bytes=100 * 1024 * 1024)
                actual_sha = hashlib.sha256(dest_p.read_bytes()).hexdigest().lower()
                if row.sha256 and actual_sha != row.sha256.lower():
                    raise MismatchError(
                        f"物件內容雜湊不符: {artifact_id}（預期 {row.sha256}，實際 {actual_sha}）"
                    )
                content = ArtifactContent(row=row, kind="contained", path=dest_p)
            else:
                data = self._drive.download_bytes(row.object_file_id, max_bytes=100 * 1024 * 1024)
                actual_sha = hashlib.sha256(data).hexdigest().lower()
                if row.sha256 and actual_sha != row.sha256.lower():
                    raise MismatchError(
                        f"物件內容雜湊不符: {artifact_id}（預期 {row.sha256}，實際 {actual_sha}）"
                    )
                content = ArtifactContent(row=row, kind="contained", data=data)
        else:
            origin = {
                "link": row.link,
                "repo": row.repo,
                "path": row.path,
            }
            content = ArtifactContent(row=row, kind="link", origin=origin)

        return Result(
            value=content,
            freshness=self._fresh(
                manifest,
                snapshot_at=row.created_at,
                max_lag=None,
            ),
        )

    def get_rejection(self, item_key: str) -> Result[RejectionRow | None]:
        """查詢特定 item_key 的拒收紀錄。"""
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            row = get_rejection(db, item_key)
        finally:
            db.close()
        return Result(
            value=row,
            freshness=self._fresh(
                manifest,
                snapshot_at=manifest["published_at"],
                max_lag=None,
            ),
        )
