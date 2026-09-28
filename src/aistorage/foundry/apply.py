"""Foundry 產出套用模組（apply_artifact）。

依據規格：
- docs/impl/group5-7-modules.md 第 6.2、6.3 節 (7.2, 7.3)
- 两階段套用：先檢驗（Agora Session 存在性、持有者相符、大小雜湊比對），後寫入。
- contained：以 git annex add 存成 annex 物件 objects/<ULID>/<安全檔名>，寫入 catalog/<ULID>.json。
- link：僅寫入 catalog/<ULID>.json。
- produced_by_session_id 必須是 Agora 裡已存在的 Session，且產生者是其持有者（比照 g3e 的 H3）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aistorage.agora.apply import ApplyResult
from aistorage.agora import rejections
from aistorage.agora.store import AgoraStore
from aistorage.clock import Clock, format_rfc3339
from aistorage.foundry import layout
from aistorage.foundry.store import FoundryStore

if TYPE_CHECKING:  # pragma: no cover
    from aistorage.intake.evaluate import Decision

MAX_ARTIFACT_SIZE = 100 * 1024 * 1024  # 100 MiB


def apply_artifact(
    store_foundry: FoundryStore,
    dec: Decision,
    agora_store: AgoraStore,
    clock: Clock,
) -> ApplyResult:
    """將已通過 evaluate 的 artifact 項目套用至 Foundry 真本工作樹。

    Args:
        store_foundry: Foundry 真本工作樹。
        dec: evaluate 產出之 ACCEPT 決策（含 sidecar 與 raw_path）。
        agora_store: Agora 真本工作樹（用於驗證關聯之 Session 是否存在且持有者相符）。
        clock: 系統時鐘。

    Returns:
        ApplyResult 包含套用狀態、代碼與異動路徑清單。
    """
    item = dec.item
    sidecar = dec.sidecar or {}
    metadata = dec.record_metadata or sidecar.get("metadata", {})
    body = sidecar.get("body", {})

    artifact_id = metadata.get("id", "")
    ulid = artifact_id.split(":", 1)[1] if ":" in artifact_id else artifact_id
    kind = body.get("kind")
    produced_by_session_id = body.get("produced_by_session_id")
    name = body.get("name") or body.get("filename") or "object.bin"

    now_iso = format_rfc3339(clock.now(), include_fraction=True)
    item_files = list(item.sidecars) + list(item.sigs) + list(item.raws)
    candidates = rejections.candidate_ids(item.sidecars, item.sigs)

    def reject(code: str) -> ApplyResult:
        # 寫入拒收紀錄至 Foundry 工作樹
        rejections.record_rejection(
            store_foundry,  # type: ignore[arg-type]
            item_key=item.item_key,
            code=code,
            at=now_iso,
            item_id=artifact_id,
            inbox_folder_id=item.inbox_folder_id,
            candidates=candidates,
        )
        deletable_after = rejections.deletable_after_for(item_files, now_iso)
        return ApplyResult(
            ok=False,
            code=code,
            rejected_at=now_iso,
            deletable_after=deletable_after,
        )

    # ------------------------------------------------------------------
    # 階段一：檢查 (Pre-check)
    # ------------------------------------------------------------------

    # 1. produced_by_session_id 必須是 Agora 裡已存在的 Session
    if not produced_by_session_id:
        return reject("invalid_format")

    sess = agora_store.get_session(produced_by_session_id)
    if sess is None:
        return reject("session_not_found")

    # 2. 產生者必須是該 Session 的持有者（owner / producer）
    if dec.producer != sess.producer:
        return reject("unauthorized")

    # 3. 檢查型態與本體規範
    if kind not in ("link", "contained"):
        return reject("invalid_format")

    raw_p: Path | None = None
    actual_size: int | None = None
    actual_sha256: str | None = None

    if kind == "contained":
        if dec.raw_path is None:
            return reject("raw_mismatch")
        raw_p = Path(dec.raw_path)
        if not raw_p.is_file():
            return reject("raw_mismatch")

        actual_size = raw_p.stat().st_size
        if actual_size > MAX_ARTIFACT_SIZE:
            return reject("too_large")

        actual_sha256 = hashlib.sha256(raw_p.read_bytes()).hexdigest().lower()
        declared_raw = sidecar.get("raw") or {}
        declared_sha = (declared_raw.get("sha256") or "").lower()
        if declared_sha and declared_sha != actual_sha256:
            return reject("raw_mismatch")
    elif kind == "link":
        link = body.get("link")
        repo = body.get("repo")
        path = body.get("path")
        if not link and not (repo and path):
            return reject("invalid_format")

    # ------------------------------------------------------------------
    # 階段二：寫入 (Execution)
    # ------------------------------------------------------------------
    written_paths: list[str] = []

    annex_key: str | None = None
    rel_obj_path: str | None = None

    if kind == "contained" and raw_p is not None:
        annex_key = store_foundry.put_contained_object(ulid, name, raw_p)
        rel_obj_path = layout.object_path(ulid, name)
        written_paths.append(rel_obj_path)

    catalog_data: dict[str, Any] = {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": name,
        "content_type": body.get("content_type"),
        "producer": dec.producer,
        "case_id": metadata.get("case_id"),
        "produced_by_session_id": produced_by_session_id,
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "size": actual_size,
        "sha256": actual_sha256,
        "annex_key": annex_key,
        "object_path": rel_obj_path,
        "description": body.get("description"),
        "link": body.get("link"),
        "repo": body.get("repo"),
        "path": body.get("path"),
        "metadata": metadata,
        "body": body,
    }

    rel_catalog = store_foundry.put_catalog(ulid, catalog_data)
    written_paths.append(rel_catalog)

    return ApplyResult(ok=True, code="ok", paths=written_paths)
