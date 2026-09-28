"""AiStorage 收件匣評估模組。

依據規格：
- docs/impl/group3-modules.md 第 4.2、4.4 節
- design D2、D3、D4（快照時間上限）
- review-g3d H1（.sig 的 ReadError 不得被轉為 REJECT）
- review-g3d M1（清冊僅記 LEDGER_CODES、多候選嘗試、不記驗章前 REJECT）
- review-g3d M2（REJECT 生命週期、rejections 快取、deletable_after 與 rejected_at）
- review-g3d M3（RFC 3339 datetime 比較與統一 format_rfc3339 格式）
- review-g3d M5（評估順序最佳化：提前 ALREADY/stale/artifact DEFER，避免非必要 raw 下載）
- review-g3d M6（真本評估 vs 同輪已套用狀態的單調性說明）
- review-g3d M7（reference 單調性依 links/reference/<from>/<to>.json 索引比對）
- review-g3d L（stamp_record ValueError 直拋、strict_json 解析 sig、年輕孤兒 incomplete）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore
from aistorage.clock import Clock, format_rfc3339, parse_rfc3339
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import MismatchError, TooLarge
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

# M1: 僅允許以下通過驗章與授權之決策代碼記錄至真本清冊
LEDGER_CODES: frozenset[str] = frozenset({
    "ok",
    "already",
    "stale",
    "collision",
    "raw_mismatch",
    "too_old",
    "replayed_item_key",
    "too_large",
})


class DecisionKind(Enum):
    """收件匣決策類型。"""

    ACCEPT = "accept"    # 接受並納入真本
    REJECT = "reject"    # 拒收（錯誤格式、偽造簽章、重放衝突等）
    DEFER = "defer"      # 暫緩處理（如缺 sig 未滿 24 小時、artifact 尚未啟用 foundry）
    ALREADY = "already"  # 已經收過且內容相同（冪等跳過，收件匣檔案可刪除）
    #: H2（review-25a48a9）：這個項目不屬於本 pipeline（型態是別的 repo 的）。
    #: **不進清冊、不寫拒收、不進收件匣的刪除判斷**——由另一個 pipeline 處理。
    SKIP = "skip"


#: 通過驗章的候選，其 `metadata.type` 落在這裡面就歸這個 pipeline（Agora）。
AGORA_ITEM_TYPES: frozenset[str] = frozenset(
    {"session", "handoff", "claim", "reference", "rewrite"})
#: 歸 Foundry 的型態。
FOUNDRY_ITEM_TYPES: frozenset[str] = frozenset({"artifact"})


@dataclass(frozen=True)
class VerifiedSidecar:
    """通過驗章＋授權的那一個候選（分派用；H2）。"""

    sc_file: DriveFile
    sig_file: DriveFile
    sc_dict: dict[str, Any]
    producer: str

    @property
    def item_type(self) -> str:
        return str((self.sc_dict.get("metadata") or {}).get("type") or "")


@dataclass(frozen=True)
class Decision:
    """單一收件匣項目之評估決策。

    M2: 包含 rejected_at 與 deletable_after，標識拒收發佈時間與可自收件匣刪除時間。
    R2: 包含 authenticated，標識是否通過簽章與授權驗證。
    """

    kind: DecisionKind
    item: InboxItem
    code: str
    authenticated: bool = False
    producer: str | None = None
    record_metadata: dict[str, Any] | None = None
    sidecar: dict[str, Any] | None = None
    raw_path: Path | None = None
    rejected_at: str | None = None
    deletable_after: datetime | None = None


def strict_json(data: bytes) -> dict[str, Any]:
    """嚴格解析 JSON，拒絕重複鍵名 (object_pairs_hook)。"""
    def check_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"JSON 包含重複的鍵名: {repr(k)}")
            d[k] = v
        return d

    text = data.decode("utf-8")
    res = json.loads(text, object_pairs_hook=check_duplicate_keys)
    if not isinstance(res, dict):
        raise ValueError("JSON 最外層必須為物件 (dict)")
    return res


def stamp_record(inbox_metadata: dict[str, Any], *, producer: str) -> dict[str, Any]:
    """提交流程唯一的蓋章入口（review-2.1 L2）。

    步驟：
    1. strip_claimed_producer（去掉可能被偽造的 producer）
    2. 填入已認證之 producer
    3. 確保 case_id 與 provenance 欄位存在（若未提供則補為 None）
    4. validate_record_metadata 檢驗；不通過直接拋出 ValueError（系統內部錯誤，Review-g3d L）
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


def _check_reference_monotonicity(
    sc_dict: dict[str, Any],
    store: AgoraStore,
    reject_decision: Any,
) -> Decision | None:
    """檢查 reference 類型的單調性（M7 & R3）。

    規則（R3）：
    - 輸入不合法（Session id 不合法、read_snapshot_at 解析失敗）→ REJECT(invalid_format)
    - 索引檔損毀（JSONDecodeError 或時間格式損毀）→ raise MismatchError 中止
    - 傳入之 read_snapshot_at 早於或等於現有索引之 read_snapshot_at → REJECT(stale)
    """
    body = sc_dict.get("body", {})
    from_sess = body.get("from_session_id")
    to_sess = body.get("to_session_id")
    inc_read_snap = body.get("read_snapshot_at")
    if not (from_sess and to_sess and inc_read_snap):
        return None

    try:
        link_rel = layout.reference_link_path(from_sess, to_sess)
    except ValueError:
        return reject_decision("invalid_format", authenticated=True)

    try:
        inc_dt = parse_rfc3339(inc_read_snap)
    except ValueError:
        return reject_decision("invalid_format", authenticated=True)

    link_p = store.worktree / link_rel
    if not link_p.is_file() and not link_p.is_symlink():
        return None

    try:
        # .json 記錄可能被 git-annex 收走（工作樹裡是指標文字），用 store 的讀法
        # 讓它先取回真正的內容再解析。
        link_data = store.read_json_file(link_rel)
        if not isinstance(link_data, dict):
            raise MismatchError(f"參考 Link 索引內容非字典: {link_rel}")
    except Exception as e:
        raise MismatchError(f"參考 Link 索引檔案損毀: {link_rel}: {e}") from e

    ex_read_snap = link_data.get("read_snapshot_at")
    if ex_read_snap:
        try:
            ex_dt = parse_rfc3339(ex_read_snap)
        except ValueError as e:
            raise MismatchError(f"參考 Link 索引檔案時間格式損毀: {link_rel}: {e}") from e
        if inc_dt <= ex_dt:
            return reject_decision("stale", authenticated=True)

    return None


def _earliest_created_at(item: InboxItem) -> str | None:
    """項目所有候選檔案中最早的 Drive created_time（RFC 3339）。

    用它當驗章前拒收的 rejected_at（4.4）：這是 Drive 自己記的時間，寫入者控制不了，
    所以 24 小時的保存期是真的從檔案躺進收件匣那時開始算。
    """
    times = [
        f.created_at
        for f in (*item.sidecars, *item.sigs, *item.raws, *item.extras)
    ]
    if not times:
        return None
    return format_rfc3339(min(times), include_fraction=True)


def verify_item_sidecar(
    item: InboxItem,
    *,
    drive: DriveClient,
    registry: Registry,
) -> tuple[VerifiedSidecar | None, str]:
    """只做「驗章＋授權」，回傳通過的那一個候選（H2 的分派依據）。

    失敗時回傳 `(None, code)`；`code` 的可能值與 `evaluate` 的拒收代碼一致。
    **不寫任何真本**（不記清冊、不寫拒收快取），所以可以安全地在決定分派之前呼叫。

    為什麼要獨立出來（H2）：分派必須以**通過驗章的候選**的 `metadata.type` 為準。
    原本的 `items_for` 讀「第一個 sidecar 候選」的 type，而那一步在驗章之前，
    住民只要在同一個收件匣放一個排在前面的垃圾 sidecar（`type: artifact`）就能
    把合法的 session 導到 Foundry（或反過來）。
    """
    folder_profile = registry.inbox_folders().get(item.inbox_folder_id)
    if not folder_profile:
        return None, "unauthorized"
    if not item.sigs or not item.sidecars:
        return None, "incomplete"

    active_keys = registry.active_public_keys(folder_profile)
    last_reject_code = "bad_signature"

    for sc_file in item.sidecars:
        try:
            sc_bytes = drive.download_bytes(sc_file.id, max_bytes=1024 * 1024)
        except TooLarge:
            last_reject_code = "sidecar_too_large"
            continue
        # H1: 讀取錯誤（ReadError / NotFound）不補捉，直接拋出讓整輪中止

        for sig_file in item.sigs:
            try:
                sig_bytes = drive.download_bytes(sig_file.id, max_bytes=4 * 1024)
            except TooLarge:
                last_reject_code = "sig_too_large"
                continue
            # H1: 讀取錯誤（ReadError / NotFound）不補捉，直接拋出讓整輪中止

            # L: 簽章檔使用 strict_json 解析，拒絕重複鍵名
            try:
                sig_dict = strict_json(sig_bytes)
            except Exception:
                last_reject_code = "bad_signature"
                continue

            verified_key_id = verify_sidecar_bytes(sc_bytes, sig_dict, active_keys)
            if verified_key_id is None:
                last_reject_code = "bad_signature"
                continue

            try:
                sc_dict = strict_json(sc_bytes)
            except Exception:
                last_reject_code = "invalid_format"
                continue

            errs = validate_sidecar(sc_dict, expected_item_key=item.item_key)
            if errs:
                last_reject_code = "invalid_format"
                continue

            if sc_dict.get("profile") != folder_profile:
                last_reject_code = "unauthorized"
                continue

            producer, why = registry.authorize(sc_dict, verified_key_id)
            if producer is None:
                last_reject_code = "unauthorized"
                continue

            return VerifiedSidecar(
                sc_file=sc_file, sig_file=sig_file, sc_dict=sc_dict,
                producer=producer), "ok"

    return None, last_reject_code


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
    foundry_enabled: bool = False,
    foundry_store: Any = None,
    allowed_types: frozenset[str] | None = None,
    preverified: VerifiedSidecar | None = None,
) -> Decision:
    """評估單一收件匣項目。

    評估流水線順序（review-g3d M5）：
    1. 驗章＋授權（多候選逐一嘗試，通過才往下）
    2. 清冊與防重放檢核（命中 REJECT 沿用代碼，同 raw sha 回傳 ALREADY）
    3. artifact → Foundry 未設定則 REJECT(foundry_not_enabled)（無需下載 raw）
    4. Session 快照上限調整與蓋章 stamp_record + classify_id
    5. 單調性防重放檢核（以宣告之 raw sha256 與快照時間比較，無需下載 raw）
    6. Raw metadata 比對（size、sha256Checksum）
    7. Raw 串流下載並經由 check_raw 驗證
    8. 接受 ACCEPT

    H2（review-25a48a9）：`allowed_types` 給定時，型態不在其中的項目回
    `SKIP(wrong_pipeline)`（不進清冊、不寫拒收）——分派依的是**通過驗章的**
    候選，這裡是第二道防呆。`preverified` 是分派那一步已驗過的結果，
    傳進來就不會重複下載。

    M6 備註：此函式僅比對當前真本已提交之狀態。同輪內多項目套用時，apply_*
    須針對同一 Session 或同一對 reference link 再次確認單調性。
    """
    workdir = Path(workdir).resolve()
    now_dt = clock.now()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    now_rfc3339 = format_rfc3339(now_dt, include_fraction=True)

    def reject_decision(
        code: str,
        *,
        rejected_at: str | None = None,
        authenticated: bool = False,
    ) -> Decision:
        # 4.4（PM 決定 4）：驗章前的拒收每一輪都會重新評估，若 rejected_at  always
        # 取「當下」，deletable_after 永遠到不了，第 14 步永遠不會刪掉這些檔案。
        # 改用該項目檔案**最早的 created_time**（Drive 的 metadata，寫入者無法控制），
        # 「每一輪都發佈、24 小時後刪除」才成立。
        if rejected_at is None and not authenticated:
            rejected_at = _earliest_created_at(item)
        r_at = rejected_at or now_rfc3339
        r_dt = parse_rfc3339(r_at)
        deletable_after = r_dt + timedelta(hours=24)

        # R1: 僅驗章通過後的 REJECT 才寫入真本 _committer/rejections 快取
        # 驗章前之拒收（orphan、bad_signature、invalid_format、unauthorized、sidecar/sig too_large）不寫入真本
        if authenticated:
            cand_ids = sorted([f.id for f in item.sidecars] + [f.id for f in item.sigs])
            rej_rel = layout.rejection_path(item.item_key)
            rej_path = store.worktree / rej_rel
            entries: list[dict[str, Any]] = []
            if rej_path.is_file():
                try:
                    with open(rej_path, encoding="utf-8") as rf:
                        existing_data = json.load(rf)
                    if "entries" in existing_data and isinstance(existing_data["entries"], list):
                        entries = existing_data["entries"]
                    elif "inbox_folder_id" in existing_data:
                        entries = [existing_data]
                except Exception:
                    entries = []

            exists = any(
                e.get("inbox_folder_id") == item.inbox_folder_id and e.get("candidate_ids") == cand_ids
                for e in entries
            )
            if not exists:
                new_entry = {
                    "inbox_folder_id": item.inbox_folder_id,
                    "candidate_ids": cand_ids,
                    "code": code,
                    "at": r_at,
                }
                entries.append(new_entry)
                # 寫入真本快取；若失敗則拋出異常（Review L）
                store.put_json(rej_rel, {
                    "item_key": item.item_key,
                    "code": code,
                    "at": r_at,
                    "inbox_folder_id": item.inbox_folder_id,
                    "candidate_ids": cand_ids,
                    "entries": entries,
                })

        return Decision(
            kind=DecisionKind.REJECT,
            item=item,
            code=code,
            authenticated=authenticated,
            rejected_at=r_at,
            deletable_after=deletable_after,
        )

    # 0-a. M2 & R1: 檢查真本 _committer/rejections 快取
    # 只有「同一個收件匣資料夾、完全相同的候選檔案組合 (candidate_ids)」才短路
    try:
        rej_rel = layout.rejection_path(item.item_key)
        rej_file = store.worktree / rej_rel
        if rej_file.is_file():
            with open(rej_file, encoding="utf-8") as rf:
                cached_data = json.load(rf)
            entries = []
            if "entries" in cached_data and isinstance(cached_data["entries"], list):
                entries = cached_data["entries"]
            elif "inbox_folder_id" in cached_data:
                entries = [cached_data]
            elif "code" in cached_data and not item.sidecars and not item.sigs:
                entries = [cached_data]

            cand_ids = sorted([f.id for f in item.sidecars] + [f.id for f in item.sigs])
            matched_entry = None
            for e in entries:
                e_folder = e.get("inbox_folder_id")
                e_cands = e.get("candidate_ids")
                if e_folder is None and e_cands is None and not cand_ids:
                    matched_entry = e
                    break
                if e_folder == item.inbox_folder_id and e_cands == cand_ids:
                    matched_entry = e
                    break

            if matched_entry is not None:
                code = matched_entry.get("code", "invalid_format")
                r_at = matched_entry.get("at", now_rfc3339)
                r_dt = parse_rfc3339(r_at)
                return Decision(
                    kind=DecisionKind.REJECT,
                    item=item,
                    code=code,
                    authenticated=True,
                    rejected_at=r_at,
                    deletable_after=r_dt + timedelta(hours=24),
                )
    except Exception:
        pass

    # 0-b. 缺 sig 或 sidecar 檢查
    # Review-g3d L: 年輕孤兒（未滿 24 小時）為 DEFER(incomplete)；超過 24 小時為 REJECT(orphan)
    if not item.sigs or not item.sidecars:
        ref_file = (
            (item.sidecars[0] if item.sidecars else None)
            or (item.raws[0] if item.raws else None)
            or (item.extras[0] if item.extras else None)
        )
        if ref_file is not None:
            created_dt = ref_file.created_at
            if (now_dt - created_dt) > timedelta(hours=24):
                return reject_decision("orphan", authenticated=False)
            else:
                return Decision(kind=DecisionKind.DEFER, item=item, code="incomplete", authenticated=False)
        return reject_decision("orphan", authenticated=False)

    # 1. 驗章＋授權（M1: 逐一嘗試 sidecars × sigs 之所有組合）
    # H2：`preverified` 是分派那一步已經驗過的結果（同一輪內不做兩次下載）。
    if preverified is not None:
        selected = preverified
    else:
        selected, verify_code = verify_item_sidecar(item, drive=drive, registry=registry)
        if selected is None:
            return reject_decision(verify_code, authenticated=False)
    selected_sc_file = selected.sc_file
    selected_sc_dict = selected.sc_dict
    selected_producer = selected.producer

    # 2. 清冊與防重放檢核
    if is_item_key_too_old(item.item_key, now_dt):
        return reject_decision("too_old", authenticated=True)

    entry = ledger.contains(item.item_key)
    if entry is not None:
        raw_meta = selected_sc_dict.get("raw")
        sidecar_raw_sha = (
            raw_meta.get("sha256").lower()
            if isinstance(raw_meta, dict) and raw_meta.get("sha256")
            else None
        )
        if entry.decision in ("accept", "ok", "already"):
            if entry.raw_sha256 == sidecar_raw_sha:
                return Decision(kind=DecisionKind.ALREADY, item=item, code="already", authenticated=True)
            else:
                return reject_decision("replayed_item_key", rejected_at=entry.at, authenticated=True)
        else:
            # M2: 原本是 REJECT 則沿用原 decision 與原 rejected_at
            return reject_decision(entry.decision, rejected_at=entry.at, authenticated=True)

    metadata = selected_sc_dict.get("metadata", {})
    item_type = metadata.get("type")

    # H2：型態不屬於這個 pipeline → SKIP。**不寫清冊、不寫拒收快取**，
    # 由另一個 pipeline 處理。用戶的合法項目不會因為分派錯誤被永久燒掉。
    if allowed_types is not None and str(item_type or "") not in allowed_types:
        return Decision(
            kind=DecisionKind.SKIP,
            item=item,
            code="wrong_pipeline",
            authenticated=True,
            rejected_at=now_rfc3339,
            deletable_after=None,
        )

    # M4: 期 1 不提供改寫功能 → REJECT(rewrite_not_supported)（無需下載 raw）
    if item_type == "rewrite":
        return reject_decision("rewrite_not_supported", authenticated=True)

    # 3. artifact：Foundry 沒設定就 REJECT（PM 指示）。
    #    為什麼不是 DEFER：DEFER 的項目會一直留在收件匣裡、每一輪都被重新評估，
    #    永遠不會前進，而且收件匣永遠不是空的（7.3 的 M4 垃圾清理也抓不到它）。
    #    REJECT 會被記錄、發佈拒收原因，並在 24 小時後被清掉，寫入者看得到原因。
    if item_type == "artifact":
        if not foundry_enabled and foundry_store is None:
            return reject_decision("foundry_not_enabled", authenticated=True)
        body = selected_sc_dict.get("body", {})
        art_kind = body.get("kind")
        if art_kind not in ("link", "contained"):
            return reject_decision("invalid_format", authenticated=True)
        if not body.get("produced_by_session_id"):
            return reject_decision("invalid_format", authenticated=True)
        if art_kind == "link":
            if not body.get("link") and not (body.get("repo") and body.get("path")):
                return reject_decision("invalid_format", authenticated=True)

    # 4. Session 調整 snapshot_at 上限（D4）與蓋章 stamp_record + classify
    if item_type == "session":
        sess_info = dict(selected_sc_dict.get("session", {}))
        claimed_snap = sess_info.get("snapshot_at", "")
        try:
            claimed_dt = parse_rfc3339(claimed_snap)
        except Exception:
            return reject_decision("invalid_format", authenticated=True)
        effective_dt = min(claimed_dt, selected_sc_file.created_at)
        # M3: 統一以 format_rfc3339 寫入真本
        sess_info["snapshot_at"] = format_rfc3339(effective_dt, include_fraction=True)
        selected_sc_dict["session"] = sess_info

    # L: 蓋章失敗屬於程式錯誤，直接 raise ValueError
    record = stamp_record(metadata, producer=selected_producer)

    existing = store.get_record(record["id"])
    existing_cat: dict[str, Any] | None = None
    if item_type == "artifact" and foundry_store is not None:
        ulid = record["id"].split(":", 1)[1] if ":" in record["id"] else record["id"]
        existing_cat = foundry_store.get_catalog(ulid)
        if existing_cat is not None:
            existing = existing_cat.get("metadata", existing_cat)

    cid = classify_id(existing, record)
    if cid == "collision":
        return reject_decision("collision", authenticated=True)

    # 5. M5: 以 sidecar 宣告之 raw sha256 與快照時間比對單調性（無需下載 raw）
    if existing is not None:
        if item_type == "session":
            existing_raw_sha = existing.get("raw_sha256")
            incoming_raw_sha = (
                selected_sc_dict.get("raw", {}).get("sha256")
                if isinstance(selected_sc_dict.get("raw"), dict)
                else None
            )
            if (
                existing_raw_sha
                and incoming_raw_sha
                and existing_raw_sha.lower() == incoming_raw_sha.lower()
            ):
                return Decision(kind=DecisionKind.ALREADY, item=item, code="already", authenticated=True)
            existing_snap = existing.get("snapshot_at")
            if not existing_snap:
                raise MismatchError(f"真本現有 session 紀錄缺少 snapshot_at: {record.get('id')}")
            inc_snap_dt = parse_rfc3339(selected_sc_dict["session"]["snapshot_at"])
            try:
                ex_snap_dt = parse_rfc3339(existing_snap)
            except ValueError as e:
                raise MismatchError(f"真本現有 session 紀錄 snapshot_at 時間格式損毀: {existing_snap}: {e}") from e
            if inc_snap_dt <= ex_snap_dt:
                return reject_decision("stale", authenticated=True)
        elif item_type == "reference":
            rej = _check_reference_monotonicity(selected_sc_dict, store, reject_decision)
            if rej is not None:
                return rej
        else:
            inc_updated = parse_rfc3339(record.get("updated_at", ""))
            ex_updated = parse_rfc3339(existing.get("updated_at", ""))
            ex_body = existing_cat.get("body") if existing_cat is not None else existing.get("body")
            if inc_updated < ex_updated:
                return reject_decision("stale", authenticated=True)
            elif inc_updated == ex_updated:
                if selected_sc_dict.get("body") == ex_body:
                    return Decision(kind=DecisionKind.ALREADY, item=item, code="already", authenticated=True)
                else:
                    return reject_decision("stale", authenticated=True)
    elif item_type == "reference":
        # M7 & R3: 即使該 ULID reference 尚未提交過，若 links/reference 索引已有較新者仍須擋下
        rej = _check_reference_monotonicity(selected_sc_dict, store, reject_decision)
        if rej is not None:
            return rej

    # 6. M5: Raw metadata 檢查
    is_contained_artifact = (
        item_type == "artifact" and selected_sc_dict.get("body", {}).get("kind") == "contained"
    )
    needs_raw = item_type in ("session", "rewrite") or is_contained_artifact

    artifact_max_raw = 100 * 1024 * 1024  # 100 MiB (D7)
    effective_max_raw = min(max_raw, artifact_max_raw) if item_type == "artifact" else max_raw

    raw_path: Path | None = None
    if needs_raw:
        raw_meta = selected_sc_dict.get("raw")
        if not isinstance(raw_meta, dict):
            return reject_decision("invalid_format", authenticated=True)
        decl_size = raw_meta.get("size")
        decl_sha = raw_meta.get("sha256")
        if decl_size is None or not isinstance(decl_size, int) or decl_sha is None:
            return reject_decision("invalid_format", authenticated=True)

        if decl_size > effective_max_raw:
            return reject_decision("too_large", authenticated=True)

        if not item.raws:
            return reject_decision("raw_mismatch", authenticated=True)

        # 比對 Drive metadata（若符合直接選取，不必下載不符合的檔案）
        matched_raw_file: DriveFile | None = None
        for rf in item.raws:
            if rf.size is not None and rf.size != decl_size:
                continue
            if rf.sha256 is not None and rf.sha256.lower() != decl_sha.lower():
                continue
            matched_raw_file = rf
            break

        if matched_raw_file is None:
            return reject_decision("raw_mismatch", authenticated=True)

        # 7. 串流下載 raw 並經由 check_raw 驗證
        raw_dest = workdir / f"{item.item_key}.raw"
        try:
            drive.download(matched_raw_file.id, raw_dest, max_bytes=effective_max_raw)
        except TooLarge:
            return reject_decision("too_large", authenticated=True)

        with open(raw_dest, "rb") as f_obj:
            raw_errs = check_raw(selected_sc_dict, f_obj, max_size=effective_max_raw)
        if raw_errs:
            return reject_decision("raw_mismatch", authenticated=True)
        raw_path = raw_dest
    else:
        if bool(item.raws):
            return reject_decision("raw_mismatch", authenticated=True)
        raw_errs = check_raw(selected_sc_dict, None, max_size=effective_max_raw)
        if raw_errs:
            return reject_decision("raw_mismatch", authenticated=True)

    # 8. 接受 ACCEPT
    return Decision(
        kind=DecisionKind.ACCEPT,
        item=item,
        code="ok",
        authenticated=True,
        producer=selected_producer,
        record_metadata=record,
        sidecar=selected_sc_dict,
        raw_path=raw_path,
    )


_DISPATCH_ORDER = {
    "session": 1,
    "rewrite": 2,
    "handoff": 3,
    "claim": 4,
    "reference": 5,
    "artifact": 6,
}


def sort_accepted_decisions(decisions: list[Decision]) -> list[Decision]:
    """將 ACCEPT 的決策項目依真本套用順序排序。

    順序：session（依 snapshot_at 由舊到新，以 datetime 比較）→ rewrite → handoff → claim → reference。
    M3: 一律轉為 datetime 進行時間比較。
    M6: 備註：此處依據真本已提交之狀態評估，apply_* 須在同批次套用時對同一 Session/同一對 Link 重檢單調性。
    """
    def sort_key(d: Decision) -> tuple[int, datetime]:
        item_type = ""
        if d.record_metadata and "type" in d.record_metadata:
            item_type = d.record_metadata["type"]
        elif d.sidecar and "metadata" in d.sidecar:
            item_type = d.sidecar["metadata"].get("type", "")

        order = _DISPATCH_ORDER.get(item_type, 99)
        time_dt = datetime.min.replace(tzinfo=timezone.utc)
        if item_type == "session" and d.sidecar:
            t_str = d.sidecar.get("session", {}).get("snapshot_at")
            if t_str:
                try:
                    time_dt = parse_rfc3339(t_str)
                except Exception:
                    pass
        elif d.record_metadata:
            t_str = d.record_metadata.get("updated_at")
            if t_str:
                try:
                    time_dt = parse_rfc3339(t_str)
                except Exception:
                    pass

        return (order, time_dt)

    return sorted(decisions, key=sort_key)
