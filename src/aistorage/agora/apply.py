"""Agora 真本套用模組：依型態將收件匣決策寫入真本。

依據規格：
- docs/impl/group3-modules.md 第 6.2 節
- design D10（接續點釘在快照上、交接單只能被認領一次、統合、參考單調）
- specs/agora/session-link、specs/agora/session-record
- review-g3d M6（apply 以本輪已套用狀態重檢單調性）

呼叫順序由 intake.evaluate.sort_accepted_decisions 決定：
session（依 snapshot_at 由舊到新）→ rewrite → handoff → claim → reference。
本模組每個 apply_* 都以「當下 store 狀態」（含本輪稍早已套用者）重檢單調性，
evaluate 階段只比對已提交狀態，無法擋下同輪內的重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, SessionRecord
from aistorage.clock import Clock, SystemClock, format_rfc3339, parse_rfc3339
from aistorage.converters.base import ConversionError, Converter
from aistorage.intake.evaluate import Decision
from aistorage.reading import check_continuation


@dataclass(frozen=True)
class ApplyResult:
    """單一決策套用結果。

    ok=True（含 code="already" 的同內容冪等跳過）表示 run.py 可繼續；
    ok=False 表示轉成 REJECT（拒絕檔已由本模組寫入，不影響同輪其他項目）。
    paths 為本次寫入的新增相對路徑（供 run.py 批次 commit 與發佈用）。
    """

    ok: bool
    code: str
    paths: list[str] = field(default_factory=list)


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


def _commit_now(clock: Clock | None, fallback: str | None) -> str:
    """取提交時間：有 clock 用 clock，否則用 fallback（writer 宣告的 updated_at），再無則系統時間。"""
    if clock is not None:
        return format_rfc3339(clock.now(), include_fraction=True)
    if isinstance(fallback, str) and fallback.strip():
        try:
            parse_rfc3339(fallback)
            return fallback
        except ValueError:
            pass
    return format_rfc3339(datetime.now(timezone.utc), include_fraction=True)


def _write_rejection(
    store: AgoraStore,
    item_key: str,
    code: str,
    at: str,
    item_id: str | None,
) -> str:
    """寫入拒收紀錄（第 4 組發佈到讀取視圖的來源）。回傳相對路徑。"""
    try:
        rel = layout.rejection_path(item_key)
    except ValueError:
        rel = f"_committer/rejections/{item_key}.json"
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
    rel = _write_rejection(store, _item_key(dec), code, at, item_id if isinstance(item_id, str) else None)
    return ApplyResult(ok=False, code=code, paths=_new_paths(store, before))


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


def apply_session(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock
) -> ApplyResult:
    """套用 session 快照：寫 raw、追加 snapshots.jsonl、更新 meta.json。

    - 同內容（raw_sha256 相同）→ ok/already，不寫不 commit（冪等）。
    - snapshot_at 單調（M6：以本輪已套用狀態重檢）：新快照 ≤ 現有 → REJECT(stale)。
    - 狀態只依轉換器事實判定（3.9）：封存後又有新訊息 → 回到 running。
    - 轉換失敗不拒收：raw 才是真本，記 reading_status="failed" 供讀取視圖跟進。
    - 每份快照各 commit 一次由 store.put_session 執行（有 git 時）。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    now_str = format_rfc3339(clock.now(), include_fraction=True)

    session_id = rec.get("id")
    if not isinstance(session_id, str) or not session_id:
        return _fail(store, dec, before, "invalid_format", now_str)

    sess = sc.get("session")
    if not isinstance(sess, dict):
        return _fail(store, dec, before, "invalid_format", now_str)
    snapshot_at = sess.get("snapshot_at")
    if not isinstance(snapshot_at, str) or not snapshot_at:
        return _fail(store, dec, before, "invalid_format", now_str)

    raw_path = getattr(dec, "raw_path", None)
    if raw_path is None:
        return _fail(store, dec, before, "raw_mismatch", now_str)
    raw_p = Path(raw_path)
    try:
        raw_bytes = raw_p.read_bytes()
    except OSError:
        raise
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
            if parse_rfc3339(snapshot_at) <= parse_rfc3339(existing.snapshot_at):
                return _fail(store, dec, before, "stale", now_str)
        except ValueError:
            return _fail(store, dec, before, "invalid_format", now_str)

    try:
        facts = conv.facts(raw_p)
    except (ConversionError, ValueError, OSError, KeyError):
        return _fail(store, dec, before, "conversion_error", now_str)

    stopped = _is_stopped(facts)
    if stopped:
        status = "stopped"
        stopped_at = sess.get("stopped_at") or getattr(facts, "archived_at", None)
    else:
        status = "running"
        stopped_at = None

    # 閱讀版轉換狀態（R9）：失敗不拒收，只記錄供第 4 組讀取視圖跟進
    reading_status = "ok"
    reading_error_code: str | None = None
    reading_error_message: str | None = None
    try:
        conv.convert(raw_p, session_id=session_id, parent_id=sess.get("parent_id"))
    except ConversionError as e:
        reading_status = "failed"
        reading_error_code = "conversion_error"
        reading_error_message = str(e)[:500]
    except Exception as e:  # 轉換器內部錯誤不應中斷整輪
        reading_status = "failed"
        reading_error_code = "conversion_error"
        reading_error_message = f"{type(e).__name__}: {str(e)[:400]}"

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
        in_progress=bool(getattr(facts, "in_progress", False)),
        archived_at=getattr(facts, "archived_at", None),
        title=getattr(facts, "title", None),
        extra={
            "reading_status": reading_status,
            "reading_error_code": reading_error_code,
            "reading_error_message": reading_error_message,
        },
    )
    try:
        store.put_session(new_rec, raw_p)
    except (ValueError, KeyError) as e:
        # metadata 驗證失敗等程式面問題：記為格式拒收，不中斷整輪
        return _fail(store, dec, before, "invalid_format", now_str)
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def _position_ok(old_reading: dict, new_reading: dict) -> bool:
    """改寫不得改變位置：舊訊息的 message_id 序列與 index 必須原樣保留為新版前綴。"""
    old_msgs = old_reading.get("messages", [])
    new_msgs = new_reading.get("messages", [])
    if not isinstance(old_msgs, list) or not isinstance(new_msgs, list):
        return False
    if len(new_msgs) < len(old_msgs):
        return False
    for i, old_m in enumerate(old_msgs):
        new_m = new_msgs[i]
        if not isinstance(old_m, dict) or not isinstance(new_m, dict):
            return False
        if old_m.get("message_id") != new_m.get("message_id"):
            return False
        if old_m.get("index") != new_m.get("index"):
            return False
    return True


def apply_rewrite(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock | None = None
) -> ApplyResult:
    """套用改寫提案：base 必須等於目前 raw_sha256；新舊閱讀版既有訊息位置完全相同。

    成功時以 via="rewrite" 追加新快照（舊快照保留，被釘住的接續點不受影響）；
    並驗證指向該 Session 的既有接續 Link 仍然有效。
    失敗碼：unknown_target、stale（base 不是最新）、position_changed、conversion_error。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    at = _commit_now(clock, rec.get("updated_at") if isinstance(rec.get("updated_at"), str) else None)

    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", at)
    target_id = body.get("target_session_id")
    base_sha = body.get("base_snapshot_sha256")
    if not isinstance(target_id, str) or not isinstance(base_sha, str):
        return _fail(store, dec, before, "invalid_format", at)

    target = store.get_session(target_id)
    if target is None:
        return _fail(store, dec, before, "unknown_target", at)
    if target.raw_sha256.lower() != base_sha.lower():
        return _fail(store, dec, before, "stale", at)

    raw_path = getattr(dec, "raw_path", None)
    if raw_path is None:
        return _fail(store, dec, before, "raw_mismatch", at)
    new_raw_p = Path(raw_path)
    try:
        new_bytes = new_raw_p.read_bytes()
    except OSError:
        raise
    new_sha = hashlib.sha256(new_bytes).hexdigest().lower()

    if new_sha == target.raw_sha256.lower():
        return ApplyResult(ok=True, code="already", paths=[])

    try:
        old_raw_p = store.raw_path_for_snapshot(target_id, target.raw_sha256)
    except (KeyError, ValueError, OSError):
        raise
    except Exception:
        return _fail(store, dec, before, "conversion_error", at)

    try:
        old_reading = conv.convert(old_raw_p, session_id=target_id)
        new_reading = conv.convert(new_raw_p, session_id=target_id)
    except ConversionError:
        return _fail(store, dec, before, "conversion_error", at)
    except (OSError, KeyError):
        raise
    if not _position_ok(old_reading, new_reading):
        return _fail(store, dec, before, "position_changed", at)

    rewrite_ulid = ""
    rec_id = rec.get("id")
    if isinstance(rec_id, str) and ":" in rec_id:
        rewrite_ulid = rec_id.split(":", 1)[1]
    rewrite_item_id = rec_id if isinstance(rec_id, str) else ""

    new_rec = SessionRecord(
        id=target.id,
        producer=target.producer,
        created_at=target.created_at,
        updated_at=rec.get("updated_at", target.updated_at),
        status=target.status,
        snapshot_at=rec.get("updated_at", target.snapshot_at)
        if isinstance(rec.get("updated_at"), str)
        else target.snapshot_at,
        raw_sha256=new_sha,
        raw_size=len(new_bytes),
        committed_at=at,
        last_item_key=_item_key(dec),
        case_id=target.case_id,
        provenance=target.provenance,
        role=target.role,
        role_version=target.role_version,
        stopped_at=target.stopped_at,
        parent_id=target.parent_id,
        in_progress=target.in_progress,
        archived_at=target.archived_at,
        title=target.title,
        extra=dict(target.extra),
    )
    try:
        store.put_session(new_rec, new_raw_p, via="rewrite", rewrite_id=rewrite_item_id)
    except (ValueError, KeyError):
        return _fail(store, dec, before, "invalid_format", at)

    # 接續點釘在快照上：確認指向該 Session 的既有接續 Link 快照仍在歷史裡
    known_shas = {s.snapshot_sha256.lower() for s in store.snapshots(target_id)}
    links_dir = store.worktree / "links" / "continuation"
    if links_dir.is_dir():
        for link_file in sorted(links_dir.rglob("*.json")):
            try:
                import json as _json

                link_data = _json.loads(link_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if link_data.get("to") != target_id:
                continue
            cont = link_data.get("continuation", {})
            snap = cont.get("snapshot_sha256", "") if isinstance(cont, dict) else ""
            if not isinstance(snap, str) or snap.lower() not in known_shas:
                return _fail(store, dec, before, "position_changed", at)

    try:
        rel = layout.rewrite_path(rewrite_ulid)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", at)
    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "applied_snapshot_sha256": new_sha,
            "committed_at": at,
        },
    )
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def apply_handoff(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock | None = None
) -> ApplyResult:
    """套用交接單：接續點快照必須已在目標 Session 歷史裡（含本輪剛收），
    取出該快照轉換後以 reading.check_continuation 驗證（快照識別＋message id、
    已完成、未撤銷）。失敗一律 REJECT(invalid_continuation)。"""
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    at = _commit_now(clock, rec.get("updated_at") if isinstance(rec.get("updated_at"), str) else None)

    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", at)
    target_id = body.get("target_session_id")
    continuation = body.get("continuation")
    if not isinstance(target_id, str) or not isinstance(continuation, dict):
        return _fail(store, dec, before, "invalid_format", at)
    snap_sha = continuation.get("snapshot_sha256")
    if not isinstance(snap_sha, str) or not snap_sha:
        return _fail(store, dec, before, "invalid_continuation", at)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    try:
        rel = layout.handoff_path(ulid)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", at)

    # 同輪重複：同 id 已收且內容相同 → 冪等跳過
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") == body:
            return ApplyResult(ok=True, code="already", paths=[])
        return _fail(store, dec, before, "stale", at)

    snaps = {s.snapshot_sha256.lower() for s in store.snapshots(target_id)}
    if snap_sha.lower() not in snaps:
        return _fail(store, dec, before, "invalid_continuation", at)

    try:
        raw_p = store.raw_path_for_snapshot(target_id, snap_sha)
    except (KeyError, ValueError):
        return _fail(store, dec, before, "invalid_continuation", at)
    except OSError:
        raise

    try:
        reading = conv.convert(raw_p, session_id=target_id)
    except ConversionError:
        return _fail(store, dec, before, "invalid_continuation", at)
    except (OSError, KeyError):
        raise

    if check_continuation(reading, continuation):
        return _fail(store, dec, before, "invalid_continuation", at)

    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "claimed_by": None,
            "committed_at": at,
        },
    )
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def apply_claim(store: AgoraStore, dec: Decision, clock: Clock | None = None) -> ApplyResult:
    """套用認領：交接單存在且未被認領、認領者 Session 已在 Agora（含本輪剛收），
    寫 claim、標記 claimed_by、建接續 Link（claimer → target，記接續點）。

    一張交接單只能被認領一次（同輪第二個認領者看到 claimed_by 非空 → already_claimed）；
    一個 Session 可認領多張（統合：每張交接單各建一條 Link）。
    失敗碼：unknown_handoff、already_claimed、unknown_claimer。
    """
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    at = _commit_now(clock, rec.get("updated_at") if isinstance(rec.get("updated_at"), str) else None)

    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", at)
    handoff_id = body.get("handoff_id")
    claimer_id = body.get("claimer_session_id")
    if not isinstance(handoff_id, str) or not isinstance(claimer_id, str):
        return _fail(store, dec, before, "invalid_format", at)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    try:
        rel = layout.claim_path(ulid)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", at)

    # 同輪重複：同 claim id 已收且內容相同 → 冪等跳過
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") == body:
            return ApplyResult(ok=True, code="already", paths=[])
        return _fail(store, dec, before, "stale", at)

    handoff = store.get_record(handoff_id)
    if handoff is None:
        return _fail(store, dec, before, "unknown_handoff", at)
    if handoff.get("claimed_by") is not None:
        return _fail(store, dec, before, "already_claimed", at)

    if store.get_session(claimer_id) is None and store.get_record(claimer_id) is None:
        return _fail(store, dec, before, "unknown_claimer", at)

    h_body = handoff.get("body", {})
    target_id = h_body.get("target_session_id") if isinstance(h_body, dict) else None
    continuation = h_body.get("continuation") if isinstance(h_body, dict) else None
    handoff_ulid = handoff_id.split(":", 1)[1] if ":" in handoff_id else handoff_id

    link_rel = layout.continuation_link_path(claimer_id, handoff_ulid)
    claim_ulid = ulid
    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "committed_at": at,
            "result": {
                "handoff_id": handoff_id,
                "claimer_session_id": claimer_id,
                "link": link_rel,
            },
        },
    )
    handoff["claimed_by"] = {"claim_id": rec_id, "session_id": claimer_id, "at": at}
    try:
        h_rel = layout.handoff_path(handoff_ulid)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", at)
    store.put_json(h_rel, handoff)
    store.put_json(
        link_rel,
        {
            "from": claimer_id,
            "to": target_id,
            "continuation": continuation,
            "handoff_id": handoff_id,
            "claim_id": rec_id,
            "claim_ulid": claim_ulid,
        },
    )
    return ApplyResult(ok=True, code="ok", paths=_new_paths(store, before))


def apply_reference(store: AgoraStore, dec: Decision, clock: Clock | None = None) -> ApplyResult:
    """套用參考：同一對 Session 只留一條 Link，read_snapshot_at 單調
   （M6：以本輪已套用狀態重檢，舊時間 → REJECT(stale)）。"""
    before = list(store.changed_paths())
    rec = _record(dec)
    sc = _sidecar(dec)
    at = _commit_now(clock, rec.get("updated_at") if isinstance(rec.get("updated_at"), str) else None)

    body = sc.get("body")
    if not isinstance(body, dict):
        return _fail(store, dec, before, "invalid_format", at)
    from_id = body.get("from_session_id")
    to_id = body.get("to_session_id")
    read_at = body.get("read_snapshot_at")
    if not all(isinstance(v, str) and v for v in (from_id, to_id, read_at)):
        return _fail(store, dec, before, "invalid_format", at)
    assert isinstance(from_id, str) and isinstance(to_id, str) and isinstance(read_at, str)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    try:
        rel = layout.reference_path(ulid)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", at)

    # 同輪重複：同 reference id 已收且內容相同 → 冪等跳過
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") == body:
            return ApplyResult(ok=True, code="already", paths=[])
        return _fail(store, dec, before, "stale", at)

    try:
        incoming_dt = parse_rfc3339(read_at)
    except ValueError:
        return _fail(store, dec, before, "invalid_format", at)

    link_rel = layout.reference_link_path(from_id, to_id)
    link_p = store.worktree / link_rel
    if link_p.is_file():
        try:
            import json as _json

            link_data = _json.loads(link_p.read_text(encoding="utf-8"))
            ex_read = link_data.get("read_snapshot_at")
            if isinstance(ex_read, str) and ex_read:
                if incoming_dt <= parse_rfc3339(ex_read):
                    return _fail(store, dec, before, "stale", at)
        except (ValueError, OSError):
            return _fail(store, dec, before, "invalid_format", at)

    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "committed_at": at,
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
