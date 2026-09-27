"""AiStorage 收件匣評估模組。

依據規格：
- docs/impl/group3-modules.md 第 4.2、4.4 節
- 驗章 → authorize → 格式 → 防重放 → dedup → 決策
- artifact 一律 DEFER(foundry_not_enabled)
- 單調性檢查、stamp_record 唯一蓋章入口
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any

from aistorage.agora.store import AgoraStore
from aistorage.clock import Clock
from aistorage.drive.model import DriveClient
from aistorage.errors import TooLarge
from aistorage.identity import Registry
from aistorage.inbox import (
    DEFAULT_MAX_RAW_SIZE,
    check_raw,
    validate_sidecar,
    verify_sidecar_bytes,
)
from aistorage.intake.ledger import Ledger, is_item_key_too_old
from aistorage.intake.scan import InboxItem
from aistorage.schema import (
    classify_id,
    strip_claimed_producer,
    validate_record_metadata,
)


class DecisionKind(Enum):
    """收件匣決策類型。"""

    ACCEPT = "accept"    # 接受並納入真本
    REJECT = "reject"    # 拒收（錯誤格式、偽造簽章、重放衝突等）
    DEFER = "defer"      # 暫緩處理（如缺 sig 未滿 24 小時、artifact 尚未啟用 foundry）
    ALREADY = "already"  # 已經收過且內容相同（冪等跳過，收件匣檔案可刪除）


@dataclass(frozen=True)
class Decision:
    """單一收件匣項目之評估決策。"""

    kind: DecisionKind
    item: InboxItem
    code: str
    producer: str | None = None
    record_metadata: dict[str, Any] | None = None
    sidecar: dict[str, Any] | None = None
    raw_path: Path | None = None


def strict_json(data: bytes) -> dict[str, Any]:
    """嚴格解析 JSON，拒絕重複鍵名 (object_pairs_hook)。"""
    def check_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"sidecar JSON 包含重複的鍵名: {repr(k)}")
            d[k] = v
        return d

    text = data.decode("utf-8")
    res = json.loads(text, object_pairs_hook=check_duplicate_keys)
    if not isinstance(res, dict):
        raise ValueError("sidecar 最外層必須為 JSON 物件 (dict)")
    return res


def stamp_record(inbox_metadata: dict[str, Any], *, producer: str) -> dict[str, Any]:
    """提交流程唯一的蓋章入口（review-2.1 L2）。

    步驟：
    1. strip_claimed_producer（去掉可能被偽造的 producer）
    2. 填入已認證之 producer
    3. 確保 case_id 與 provenance 欄位存在（若未提供則補為 None）
    4. validate_record_metadata 檢驗；不通過拋出 ValueError（屬於系統內部錯誤，非輸入錯誤）
    5. 回傳蓋好章的 record 字典
    """
    if not isinstance(inbox_metadata, dict):
        raise TypeError("inbox_metadata 必須是字典 (dict)")
    if not isinstance(producer, str) or not producer.strip():
        raise ValueError("producer 必須是非空字串")

    record = strip_claimed_producer(inbox_metadata)
    record["producer"] = producer
    if "case_id" not in record:
        record["case_id"] = None
    if "provenance" not in record:
        record["provenance"] = None

    errs = validate_record_metadata(record)
    if errs:
        err_msgs = "; ".join(f"{e.field}: {e.message}" for e in errs)
        raise ValueError(f"stamp_record 產出不符合 record metadata 規範: {err_msgs}")

    return record


def evaluate(
    item: InboxItem,
    *,
    drive: DriveClient,
    registry: Registry,
    store: AgoraStore,
    ledger: Ledger,
    clock: Clock,
    workdir: Path,
    max_raw: int = DEFAULT_MAX_RAW_SIZE,
) -> Decision:
    """評估單一收件匣項目。

    評估流水線順序：驗章 → authorize → 格式 → 防重放 → dedup → 決策。
    """
    workdir = Path(workdir).resolve()
    now_dt = clock.now()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    # 1. 缺 sig 或 sidecar：raw 或 sidecar 或 extras 的 created_time 超過 24 小時 → REJECT(orphan)；否則 DEFER
    if item.sig is None or item.sidecar is None:
        ref_file = item.sidecar or item.raw or (item.extras[0] if item.extras else None)
        if ref_file is not None:
            created_dt = ref_file.created_at
            if (now_dt - created_dt) > timedelta(hours=24):
                return Decision(kind=DecisionKind.REJECT, item=item, code="orphan")
            else:
                return Decision(kind=DecisionKind.DEFER, item=item, code="orphan")
        return Decision(kind=DecisionKind.REJECT, item=item, code="orphan")

    # 2. 下載 sidecar 與 sig
    try:
        sidecar_bytes = drive.download_bytes(item.sidecar.id, max_bytes=1024 * 1024)
    except TooLarge:
        return Decision(kind=DecisionKind.REJECT, item=item, code="too_large")

    try:
        sig_bytes = drive.download_bytes(item.sig.id, max_bytes=4 * 1024)
        sig = json.loads(sig_bytes.decode("utf-8"))
        if not isinstance(sig, dict):
            return Decision(kind=DecisionKind.REJECT, item=item, code="bad_signature")
    except TooLarge:
        return Decision(kind=DecisionKind.REJECT, item=item, code="too_large")
    except Exception:
        return Decision(kind=DecisionKind.REJECT, item=item, code="bad_signature")

    # 3. 驗證所屬收件匣 profile 與簽章
    folder_profile = registry.inbox_folders().get(item.inbox_folder_id)
    if not folder_profile:
        return Decision(kind=DecisionKind.REJECT, item=item, code="unauthorized")

    active_keys = registry.active_public_keys(folder_profile)
    key_id = verify_sidecar_bytes(sidecar_bytes, sig, active_keys)
    if key_id is None:
        return Decision(kind=DecisionKind.REJECT, item=item, code="bad_signature")

    # 4. 嚴格解析 sidecar JSON 並驗證格式
    try:
        sidecar = strict_json(sidecar_bytes)
    except Exception:
        return Decision(kind=DecisionKind.REJECT, item=item, code="invalid_format")

    errs = validate_sidecar(sidecar, expected_item_key=item.item_key)
    if errs:
        return Decision(kind=DecisionKind.REJECT, item=item, code="invalid_format")

    # 5. 授權檢核（Profile 必須等於收件匣所屬 profile）
    if sidecar.get("profile") != folder_profile:
        return Decision(kind=DecisionKind.REJECT, item=item, code="unauthorized")

    producer, why = registry.authorize(sidecar, key_id)
    if producer is None:
        return Decision(kind=DecisionKind.REJECT, item=item, code="unauthorized")

    # 6. 清冊與防重放檢核
    if is_item_key_too_old(item.item_key, now_dt):
        return Decision(kind=DecisionKind.REJECT, item=item, code="too_old")

    entry = ledger.contains(item.item_key)
    if entry is not None:
        raw_meta = sidecar.get("raw")
        sidecar_raw_sha = (
            raw_meta.get("sha256").lower()
            if isinstance(raw_meta, dict) and raw_meta.get("sha256")
            else None
        )
        if entry.raw_sha256 == sidecar_raw_sha:
            return Decision(kind=DecisionKind.ALREADY, item=item, code="already")
        else:
            return Decision(kind=DecisionKind.REJECT, item=item, code="replayed_item_key")

    metadata = sidecar.get("metadata", {})
    item_type = metadata.get("type")

    # 7. raw 本體檢查與下載
    needs_raw = False
    if item_type in ("session", "rewrite"):
        needs_raw = True
    elif item_type == "artifact":
        body = sidecar.get("body", {})
        if isinstance(body, dict) and body.get("kind") == "contained":
            needs_raw = True

    raw_path: Path | None = None
    if needs_raw:
        if item.raw is None:
            return Decision(kind=DecisionKind.REJECT, item=item, code="raw_mismatch")
        if item.raw.size is not None and item.raw.size > max_raw:
            return Decision(kind=DecisionKind.REJECT, item=item, code="too_large")

        raw_dest = workdir / f"{item.item_key}.raw"
        try:
            drive.download(item.raw.id, raw_dest, max_bytes=max_raw)
        except TooLarge:
            return Decision(kind=DecisionKind.REJECT, item=item, code="too_large")

        with open(raw_dest, "rb") as rf:
            raw_errs = check_raw(sidecar, rf, max_size=max_raw)
        if raw_errs:
            return Decision(kind=DecisionKind.REJECT, item=item, code="raw_mismatch")
        raw_path = raw_dest
    else:
        # 不需要 raw 的項目若帶有 raw 視為不符
        if item.raw is not None:
            return Decision(kind=DecisionKind.REJECT, item=item, code="raw_mismatch")
        raw_errs = check_raw(sidecar, None, max_size=max_raw)
        if raw_errs:
            return Decision(kind=DecisionKind.REJECT, item=item, code="raw_mismatch")

    # 8. session 調整 snapshot_at 上限（D4）
    if item_type == "session":
        sess_info = dict(sidecar.get("session", {}))
        claimed_snap = sess_info.get("snapshot_at", "")
        # sidecar 檔案之 created_time 為上限
        effective_snap = min(claimed_snap, item.sidecar.created_time)
        sess_info["snapshot_at"] = effective_snap
        sidecar["session"] = sess_info

    # 9. 蓋上產生者章並做 classify_id 衝突檢核
    try:
        record = stamp_record(metadata, producer=producer)
    except ValueError:
        return Decision(kind=DecisionKind.REJECT, item=item, code="invalid_format")

    existing = store.get_record(record["id"])
    cid = classify_id(existing, record)
    if cid == "collision":
        return Decision(kind=DecisionKind.REJECT, item=item, code="collision")

    # 10. 單調性防重放檢查（review-2.2 H3）
    if existing is not None:
        if item_type == "session":
            existing_raw_sha = existing.get("raw_sha256")
            incoming_raw_sha = (
                sidecar.get("raw", {}).get("sha256")
                if isinstance(sidecar.get("raw"), dict)
                else None
            )
            if (
                existing_raw_sha
                and incoming_raw_sha
                and existing_raw_sha.lower() == incoming_raw_sha.lower()
            ):
                return Decision(kind=DecisionKind.ALREADY, item=item, code="already")
            existing_snap = existing.get("snapshot_at", "")
            if sidecar["session"]["snapshot_at"] <= existing_snap:
                return Decision(kind=DecisionKind.REJECT, item=item, code="stale")
        elif item_type == "reference":
            incoming_read_snap = sidecar.get("body", {}).get("read_snapshot_at", "")
            existing_read_snap = existing.get("read_snapshot_at") or existing.get("body", {}).get(
                "read_snapshot_at", ""
            )
            if existing_read_snap and incoming_read_snap <= existing_read_snap:
                return Decision(kind=DecisionKind.REJECT, item=item, code="stale")
        else:
            inc_updated = record.get("updated_at", "")
            ex_updated = existing.get("updated_at", "")
            if inc_updated < ex_updated:
                return Decision(kind=DecisionKind.REJECT, item=item, code="stale")
            elif inc_updated == ex_updated:
                inc_body = sidecar.get("body")
                ex_body = existing.get("body")
                if inc_body == ex_body:
                    return Decision(kind=DecisionKind.ALREADY, item=item, code="already")
                else:
                    return Decision(kind=DecisionKind.REJECT, item=item, code="stale")

    # 11. artifact → DEFER(foundry_not_enabled)（第 7 組之前留在收件匣）
    if item_type == "artifact":
        return Decision(kind=DecisionKind.DEFER, item=item, code="foundry_not_enabled")

    # 12. 接受 ACCEPT
    return Decision(
        kind=DecisionKind.ACCEPT,
        item=item,
        code="ok",
        producer=producer,
        record_metadata=record,
        sidecar=sidecar,
        raw_path=raw_path,
    )


_DISPATCH_ORDER = {
    "session": 1,
    "rewrite": 2,
    "handoff": 3,
    "claim": 4,
    "reference": 5,
}


def sort_accepted_decisions(decisions: list[Decision]) -> list[Decision]:
    """將 ACCEPT 的決策項目依真本套用順序排序。

    順序：session（依 snapshot_at 由舊到新）→ rewrite → handoff → claim → reference。
    """
    def sort_key(d: Decision) -> tuple[int, str]:
        item_type = ""
        if d.record_metadata and "type" in d.record_metadata:
            item_type = d.record_metadata["type"]
        elif d.sidecar and "metadata" in d.sidecar:
            item_type = d.sidecar["metadata"].get("type", "")

        order = _DISPATCH_ORDER.get(item_type, 99)
        time_key = ""
        if item_type == "session" and d.sidecar:
            time_key = d.sidecar.get("session", {}).get("snapshot_at", "")
        elif d.record_metadata:
            time_key = d.record_metadata.get("updated_at", "")

        return (order, time_key)

    return sorted(decisions, key=sort_key)
