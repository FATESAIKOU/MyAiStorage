"""Agora 真本套用模組：依型態將收件匣決策寫入真本。

依據規格：
- docs/impl/group3-modules.md 第 6.2 節
- design D10（接續點釘在快照上、交接單只能被認領一次、統合、參考單調、
  接續只能由持有者發起、認領只能在主 Session 執行）
- ADR 0007（讀者不觸發別人的寫入）
- specs/agora/session-link、session-record、session-sync
- review-g3d M6（apply 以本輪已套用狀態重檢單調性）
- review-g3e（H1 兩階段、H2 轉換失敗照收、H3 持有者檢查、M3〜M6、L）
- PM 決定（期 1 拿掉改寫：apply_rewrite 一律 REJECT）

呼叫順序由 intake.evaluate.sort_accepted_decisions 決定：
session（依 snapshot_at 由舊到新）→ rewrite → handoff → claim → reference。

每個 apply_* 都是兩階段（H1）：先完成所有檢查（形狀、單調性、持有者、
路徑計算、所需檔案讀取），確定可套用後才寫入；寫入階段不再有
「失敗 → REJECT」分支，錯誤一律往上拋、整輪中止。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, SessionRecord
from aistorage.clock import Clock, format_rfc3339, parse_rfc3339
from aistorage.converters import get_converter
from aistorage.converters.base import ConversionError, Converter
from aistorage.errors import MismatchError
from aistorage.intake.evaluate import Decision
from aistorage.reading import check_continuation


@dataclass(frozen=True)
class ApplyResult:
    """單一決策套用結果。

    ok=True（含 code="already" 的同內容冪等跳過）表示 run.py 可繼續；
    ok=False 表示轉成 REJECT（拒收檔已由本模組寫入，不影響同輪其他項目）。
    paths 為本次寫入的新增相對路徑（供 run.py 批次 commit 與發佈用）。
    rejected_at／deletable_after 供 run.py 發佈拒收原因（24 小時窗口）。
    """

    ok: bool
    code: str
    paths: list[str] = field(default_factory=list)
    rejected_at: str | None = None
    deletable_after: datetime | None = None


def _item_key(dec: Decision) -> str:
    item = getattr(dec, "item", None)
    key = getattr(item, "item_key", None) if item is not None else None
    if isinstance(key, str) and key:
        return key
    return ""


def _record(dec: Decision) -> dict[str, Any]:
    rec = getattr(dec, "record_metadata", None)
    return dict(rec) if isinstance(rec, dict) else {}


def _sidecar(dec: Decision) -> dict[str, Any]:
    sc = getattr(dec, "sidecar", None)
    return dict(sc) if isinstance(sc, dict) else {}


def _new_paths(store: AgoraStore, before: list[str]) -> list[str]:
    seen = set(before)
    return [p for p in store.changed_paths() if p not in seen]


def _write_rejection(
    store: AgoraStore,
    item_key: str,
    code: str,
    at: str,
    item_id: str | None,
) -> str:
    """寫入拒收紀錄（第 4 組發佈到讀取視圖的來源）。回傳相對路徑。

    item_key 格式錯誤時直接 raise（scan 已驗證 ULID，走到這裡代表程式錯誤）。
    """
    rel = layout.rejection_path(item_key)
    obj: dict[str, Any] = {"code": code, "at": at}
    if item_id:
        obj["item_id"] = item_id
    store.put_json(rel, obj)
    return rel


def _fail(
    store: AgoraStore,
    dec: Decision,
    before: list[str],
    code: str,
    at: str,
) -> ApplyResult:
    """套用失敗轉 REJECT：寫拒收紀錄，回傳 ok=False。"""
    item_id = _record(dec).get("id")
    _write_rejection(store, _item_key(dec), code, at,
                     item_id if isinstance(item_id, str) else None)
    return ApplyResult(
        ok=False,
        code=code,
        paths=_new_paths(store, before),
        rejected_at=at,
        deletable_after=parse_rfc3339(at) + timedelta(hours=24),
    )


def _converter_for(conv: Converter, session_id: str) -> Converter:
    """依目標 Session 的 source 選轉換器（review-g3e L）。

    目標 source 與呼叫端傳入的轉換器相同時直接使用；不同時（例如別的
    來源應用替這個 Session 寫交接單）向登錄查詢。來源不明時 KeyError
    往上拋（evaluate 應已擋下，走到這裡代表程式或設定錯誤，整輪中止）。
    """
    source = session_id.split(":", 1)[0] if ":" in session_id else ""
    if source == getattr(conv, "source", None):
        return conv
    return get_converter(source)


def _is_stopped(facts: Any) -> bool:
    """3.9：事實上有封存紀錄，且最後一則訊息不晚於封存 → 停止中；否則運作中。

    比較一律用毫秒整數時間戳（R3）；缺少任一者時，有封存、無訊息視為停止中。
    """
    archived_ms = getattr(facts, "archived_ms", None)
    last_ms = getattr(facts, "last_message_ms", None)
    if archived_ms is None or archived_ms <= 0:
        return False
    if last_ms is None:
        return True
    return last_ms <= archived_ms


def _is_last_completed(reading: dict, message_id: str) -> bool:
    """M5：接續點必須是該快照裡最後一則已完成的訊息。

    check_continuation 已確認目標存在、已完成、未撤銷；這裡再確認目標
    之後沒有其他 completed 且未撤銷的訊息。
    """
    messages = reading.get("messages", [])
    if not isinstance(messages, list):
        return False
    target_index: int | None = None
    for m in messages:
        if isinstance(m, dict) and m.get("message_id") == message_id:
            target_index = m.get("index")
            break
    if not isinstance(target_index, int):
        return False
    for m in messages:
        if not isinstance(m, dict):
            continue
        idx = m.get("index")
        if isinstance(idx, int) and idx > target_index:
            if m.get("completed", False) and not m.get("reverted", False):
                return False
    return True


def apply_session(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock
) -> ApplyResult:
    """套用 session 快照：寫 raw、追加 snapshots.jsonl、更新 meta.json。

    - 同內容（raw_sha256 相同）→ ok/already，不寫不 commit（冪等）。
    - snapshot_at 單調（M6：以本輪已套用狀態重檢）：新快照 ≤ 現有 → REJECT(stale)。
    - 狀態只依轉換器事實判定（3.9）：封存後又有新訊息 → 回到 running。
    - H2：facts() 失敗也照收（沿用現有值或預設），記 reading_status="failed"。
    - 轉換失敗不拒收：raw 才是真本，只記 reading_error_code（M6：不存訊息內文）。
    - 每份快照各 commit 一次由 store.put_session 執行（有 git 時）。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    now_str = format_rfc3339(clock.now(), include_fraction=True)

    # ---- 檢查階段（不寫入真本） ----
    session_id = rec.get("id")
    if not isinstance(session_id, str) or not session_id:
        return _fail(store, dec, before, "invalid_format", now_str)

    sess = sc.get("session")
    if not isinstance(sess, dict):
        return _fail(store, dec, before, "invalid_format", now_str)
    snapshot_at = sess.get("snapshot_at")
    if not isinstance(snapshot_at, str) or not snapshot_at:
        return _fail(store, dec, before, "invalid_format", now_str)
    try:
        parse_rfc3339(snapshot_at)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", now_str)

    raw_path = getattr(dec, "raw_path", None)
    if raw_path is None:
        return _fail(store, dec, before, "raw_mismatch", now_str)
    raw_p = Path(raw_path)
    raw_bytes = raw_p.read_bytes()  # OSError 往上拋（I/O 錯誤不轉 REJECT）
    incoming_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
    incoming_size = len(raw_bytes)

    # sidecar 宣告與實際檔案需一致（evaluate 已驗過，這裡是套用前最後防線）
    raw_meta = sc.get("raw")
    if isinstance(raw_meta, dict):
        decl_sha = raw_meta.get("sha256")
        decl_size = raw_meta.get("size")
        if (
            (isinstance(decl_sha, str) and decl_sha.lower() != incoming_sha)
            or (isinstance(decl_size, int) and decl_size != incoming_size)
        ):
            return _fail(store, dec, before, "raw_mismatch", now_str)

    # M6：以本輪已套用狀態重檢單調性
    existing = store.get_session(session_id)
    if existing is not None:
        if existing.raw_sha256.lower() == incoming_sha:
            return ApplyResult(ok=True, code="already", paths=[])
        try:
            incoming_newer = parse_rfc3339(snapshot_at) > parse_rfc3339(existing.snapshot_at)
        except ValueError:
            return _fail(store, dec, before, "invalid_format", now_str)
        if not incoming_newer:
            return _fail(store, dec, before, "stale", now_str)

    # H2：facts 失敗也照收 raw
    try:
        facts = conv.facts(raw_p)
        facts_ok = True
    except Exception:
        facts = None
        facts_ok = False

    if not facts_ok:
        status = existing.status if existing is not None else "running"
        stopped_at = existing.stopped_at if existing is not None else None
        in_progress_raw = sess.get("in_progress", False)
        in_progress = in_progress_raw if isinstance(in_progress_raw, bool) else False
        title = existing.title if existing is not None else None
        archived_at = existing.archived_at if existing is not None else None
        reading_status = "failed"
        reading_error_code: str | None = "facts_error"
    else:
        stopped = _is_stopped(facts)
        if stopped:
            status = "stopped"
            if existing is not None and existing.status == "stopped":
                stopped_at = existing.stopped_at  # 沿用原本的停止時間
            else:
                stopped_at = sess.get("stopped_at") or now_str
        else:
            status = "running"
            stopped_at = None
        in_progress = bool(getattr(facts, "in_progress", False))
        title = getattr(facts, "title", None)
        archived_at = getattr(facts, "archived_at", None)
        # 閱讀版轉換狀態（R9、M6）：失敗不拒收，只記代碼不記內文
        reading_status = "ok"
        reading_error_code = None
        try:
            conv.convert(raw_p, session_id=session_id, parent_id=sess.get("parent_id"))
        except Exception:
            reading_status = "failed"
            reading_error_code = "conversion_error"

    new_rec = SessionRecord(
        id=session_id,
        producer=rec.get("producer", ""),
        created_at=rec.get("created_at", ""),
        updated_at=rec.get("updated_at", ""),
        status=status,
        snapshot_at=snapshot_at,
        raw_sha256=incoming_sha,
        raw_size=incoming_size,
        committed_at=now_str,
        last_item_key=_item_key(dec),
        case_id=rec.get("case_id"),
        provenance=rec.get("provenance"),
        role=rec.get("role"),
        role_version=rec.get("role_version"),
        stopped_at=stopped_at,
        parent_id=sess.get("parent_id"),
        in_progress=in_progress,
        archived_at=archived_at,
        title=title,
        extra={
            "reading_status": reading_status,
            "reading_error_code": reading_error_code,
            "reading_error_message": None,
        },
    )
    # ---- 寫入階段（M3：錯誤一律往上拋，不轉 REJECT） ----
    store.put_session(new_rec, raw_p)
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def apply_rewrite(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock
) -> ApplyResult:
    """期 1 不提供改寫（PM 決定）：一律 REJECT(rewrite_not_supported)。

    來源應用自己的編輯走 apply_session（原始紀錄的新版本），不走這裡。
    """
    before = list(store.changed_paths())
    now_str = format_rfc3339(clock.now(), include_fraction=True)
    return _fail(store, dec, before, "rewrite_not_supported", now_str)


def apply_handoff(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock
) -> ApplyResult:
    """套用交接單：接續點快照必須已在目標 Session 歷史裡（含本輪剛收），
    取出該快照轉換後以 reading.check_continuation 驗證（快照識別＋message id、
    已完成、未撤銷），且必須是該快照最後一則已完成的訊息（M5）。

    H3：只有目標 Session 的持有者（producer 相同）能寫交接單。
    失敗碼：invalid_format、unknown_target、not_holder、invalid_continuation、stale。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    now_str = format_rfc3339(clock.now(), include_fraction=True)

    # ---- 檢查階段（不寫入真本） ----
    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", now_str)
    target_id = body.get("target_session_id")
    continuation = body.get("continuation")
    if not isinstance(target_id, str) or not isinstance(continuation, dict):
        return _fail(store, dec, before, "invalid_format", now_str)
    snap_sha = continuation.get("snapshot_sha256")
    message_id = continuation.get("message_id")
    if not isinstance(snap_sha, str) or not snap_sha:
        return _fail(store, dec, before, "invalid_continuation", now_str)
    if not isinstance(message_id, str) or not message_id:
        return _fail(store, dec, before, "invalid_continuation", now_str)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    rel = layout.handoff_path(ulid)  # ULID 非法 → ValueError 往上拋（程式錯誤）

    # 同輪重複：同 id 已收且內容相同 → 冪等跳過
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") == body:
            return ApplyResult(ok=True, code="already", paths=[])
        return _fail(store, dec, before, "stale", now_str)

    target = store.get_session(target_id)
    if target is None:
        return _fail(store, dec, before, "unknown_target", now_str)
    if not isinstance(rec.get("producer"), str) or rec.get("producer") != target.producer:
        return _fail(store, dec, before, "not_holder", now_str)

    snaps = {s.snapshot_sha256.lower() for s in store.snapshots(target_id)}
    if snap_sha.lower() not in snaps:
        return _fail(store, dec, before, "invalid_continuation", now_str)

    # M3：真本讀取錯誤（KeyError／MismatchError／OSError）一律往上拋，不轉 REJECT
    raw_p = store.raw_path_for_snapshot(target_id, snap_sha)

    conv2 = _converter_for(conv, target_id)
    try:
        reading = conv2.convert(raw_p, session_id=target_id)
    except ConversionError:
        return _fail(store, dec, before, "invalid_continuation", now_str)

    if check_continuation(reading, continuation):
        return _fail(store, dec, before, "invalid_continuation", now_str)
    if not _is_last_completed(reading, message_id):
        return _fail(store, dec, before, "invalid_continuation", now_str)

    # ---- 寫入階段 ----
    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "claimed_by": None,
            "committed_at": now_str,
        },
    )
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def apply_claim(store: AgoraStore, dec: Decision, clock: Clock) -> ApplyResult:
    """套用認領：交接單存在且未被認領、認領者 Session 已在 Agora（含本輪剛收），
    寫 claim、標記 claimed_by、建接續 Link（claimer → target，記接續點）。

    一張交接單只能被認領一次（同輪第二個認領者看到 claimed_by 非空 → already_claimed）；
    一個 Session 可認領多張（統合：每張交接單各建一條 Link）。
    H3：認領者必須是自己的持有者（producer 相同）、是主 Session（parent_id 為 None）、
    且不能是交接單的目標本身。
    失敗碼：invalid_format、unknown_handoff、already_claimed、unknown_claimer、
    not_holder、claim_from_subsession、self_claim、stale。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    now_str = format_rfc3339(clock.now(), include_fraction=True)

    # ---- 檢查階段（不寫入真本） ----
    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", now_str)
    handoff_id = body.get("handoff_id")
    claimer_id = body.get("claimer_session_id")
    if not isinstance(handoff_id, str) or not isinstance(claimer_id, str):
        return _fail(store, dec, before, "invalid_format", now_str)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    rel = layout.claim_path(ulid)
    handoff_ulid = handoff_id.split(":", 1)[1] if ":" in handoff_id else handoff_id
    h_rel = layout.handoff_path(handoff_ulid)
    link_rel = layout.continuation_link_path(claimer_id, handoff_ulid)

    # 同輪重複：同 claim id 已收且內容相同 → 需三處齊全才是冪等跳過，
    # 否則是中断後的不完整狀態 → MismatchError 中止（H1 可重入）。
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") != body:
            return _fail(store, dec, before, "stale", now_str)
        handoff_now = store.get_record(handoff_id)
        link_now = store.worktree / link_rel
        cb_now = handoff_now.get("claimed_by") if isinstance(handoff_now, dict) else None
        if (
            isinstance(cb_now, dict)
            and cb_now.get("claim_id") == rec_id
            and link_now.is_file()
        ):
            return ApplyResult(ok=True, code="already", paths=[])
        raise MismatchError(f"認領 {rec_id} 的真本狀態不完整（claim 已寫但 link 或 claimed_by 缺失）")

    handoff = store.get_record(handoff_id)
    if handoff is None:
        return _fail(store, dec, before, "unknown_handoff", now_str)
    claimed_by = handoff.get("claimed_by")
    if claimed_by is not None:
        if isinstance(claimed_by, dict) and claimed_by.get("claim_id") == rec_id:
            pass  # 中斷後重跑的補齊路徑（claim 檔尚未寫入）
        else:
            return _fail(store, dec, before, "already_claimed", now_str)

    claimer = store.get_session(claimer_id)
    if claimer is None:
        return _fail(store, dec, before, "unknown_claimer", now_str)
    if not isinstance(rec.get("producer"), str) or rec.get("producer") != claimer.producer:
        return _fail(store, dec, before, "not_holder", now_str)
    if claimer.parent_id is not None:
        return _fail(store, dec, before, "claim_from_subsession", now_str)

    h_body = handoff.get("body", {})
    target_id = h_body.get("target_session_id") if isinstance(h_body, dict) else None
    continuation = h_body.get("continuation") if isinstance(h_body, dict) else None
    if claimer_id == target_id:
        return _fail(store, dec, before, "self_claim", now_str)

    # ---- 寫入階段（顺序：link → handoff → claim；错误往上抛） ----
    store.put_json(
        link_rel,
        {
            "from": claimer_id,
            "to": target_id,
            "continuation": continuation,
            "handoff_id": handoff_id,
            "claim_id": rec_id,
        },
    )
    handoff["claimed_by"] = {"claim_id": rec_id, "session_id": claimer_id, "at": now_str}
    store.put_json(h_rel, handoff)
    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "committed_at": now_str,
            "result": {
                "handoff_id": handoff_id,
                "claimer_session_id": claimer_id,
                "link": link_rel,
            },
        },
    )
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def apply_reference(store: AgoraStore, dec: Decision, clock: Clock) -> ApplyResult:
    """套用參考：同一對 Session 只留一條 Link，read_snapshot_at 單調
    （M6：以本輪已套用狀態重檢，舊時間 → REJECT(stale)）。

    H3：只有 from Session 的持有者能建參考，且 to Session 必須存在於 Agora。
    失敗碼：invalid_format、not_holder、unknown_target、stale。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    now_str = format_rfc3339(clock.now(), include_fraction=True)

    # ---- 檢查階段（不寫入真本） ----
    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", now_str)
    from_id = body.get("from_session_id")
    to_id = body.get("to_session_id")
    read_at = body.get("read_snapshot_at")
    if not all(isinstance(v, str) and v for v in (from_id, to_id, read_at)):
        return _fail(store, dec, before, "invalid_format", now_str)
    assert isinstance(from_id, str) and isinstance(to_id, str) and isinstance(read_at, str)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    rel = layout.reference_path(ulid)
    link_rel = layout.reference_link_path(from_id, to_id)

    from_sess = store.get_session(from_id)
    if from_sess is None:
        return _fail(store, dec, before, "not_holder", now_str)
    if not isinstance(rec.get("producer"), str) or rec.get("producer") != from_sess.producer:
        return _fail(store, dec, before, "not_holder", now_str)
    if store.get_session(to_id) is None:
        return _fail(store, dec, before, "unknown_target", now_str)

    try:
        incoming_dt = parse_rfc3339(read_at)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", now_str)

    # M3：索引檔讀取失敗或損毀 → MismatchError 中止，不轉 REJECT
    link_p = store.worktree / link_rel
    existing_dt = None
    existing_ref: str | None = None
    if link_p.is_file():
        try:
            link_data = json.loads(link_p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise MismatchError(f"參考索引損毀: {link_rel}: {e}") from e
        ex_read = link_data.get("read_snapshot_at")
        if not isinstance(ex_read, str) or not ex_read:
            raise MismatchError(f"參考索引缺少 read_snapshot_at: {link_rel}")
        try:
            existing_dt = parse_rfc3339(ex_read)
        except ValueError as e:
            raise MismatchError(f"參考索引時間損毀: {link_rel}: {e}") from e
        ref_val = link_data.get("reference_id")
        existing_ref = ref_val if isinstance(ref_val, str) else None

    # 同輪重複：同 reference id 已收且內容相同、索引也已指向自己 → 冪等跳過
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") != body:
            return _fail(store, dec, before, "stale", now_str)
        if existing_ref == rec_id and existing_dt is not None and incoming_dt <= existing_dt:
            return ApplyResult(ok=True, code="already", paths=[])
        # 索引尚未指向自己（中断後重跑）→ 往下補齊

    if existing_dt is not None and incoming_dt <= existing_dt and existing_ref != rec_id:
        return _fail(store, dec, before, "stale", now_str)

    # ---- 寫入階段（顺序：reference 檔 → 索引；中断重跑可補齊） ----
    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "committed_at": now_str,
        },
    )
    store.put_json(
        link_rel,
        {
            "from": from_id,
            "to": to_id,
            "reference_id": rec_id,
            "read_snapshot_at": read_at,
        },
    )
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))
