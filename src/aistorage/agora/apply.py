"""Agora 真本套用模組：依型態將收件匣決策寫入真本。

依據規格：
- docs/impl/group3-modules.md 第 6.2 節
- design D10（接續點釘在快照上、交接單只能被認領一次、統合、參考單調、
  接續只能由持有者發起、認領只能在主 Session 執行）
- ADR 0007（讀者不觸發別人的寫入）
- specs/agora/session-link、session-record、session-sync
- review-g3d M6（apply 以本輪已套用狀態重檢單調性）
- review-g3e（H1 兩階段、H2 轉換失敗照收、H3 持有者檢查、M3〜M6、L）
- review-2bc0785 M1（同一個新 session 對同一個被接續 session 只能一條 Link，
  claim 與 continuation 共用同一個檢查）、M2（預留標成 reserved 並設期限、每個
  profile 的預留／接續數量上限）、L（接續目標只能是主 Session、same_link
  提早回傳、被拒不留預留殘留、預留 id 的格式規則）
- review-1926cd3-142fd04 M3（PM 裁決：上限是**跨輪累計的未結預留數**，不是每輪
  限速；直接從真本算，不留任何程式內帳本）
- review-55edd374 M3（過了 `reserved_until` 的預留不佔額度——仍然顯示、仍然等人
  清理，只是不計數；`link_quota_exceeded` 的詳細訊息列出是哪些預留佔住額度）
- PM 決定（期 1 拿掉改寫：apply_rewrite 一律 REJECT）

呼叫順序由 intake.evaluate.sort_accepted_decisions 決定：
session（依 snapshot_at 由舊到新）→ rewrite → handoff → claim → continuation →
reference。

每個 apply_* 都是兩階段（H1）：先完成所有檢查（形狀、單調性、持有者、
路徑計算、所需檔案讀取），確定可套用後才寫入；寫入階段不再有
「失敗 → REJECT」分支，錯誤一律往上拋、整輪中止。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aistorage.agora import layout, rejections
from aistorage.agora.store import AgoraStore, SessionRecord
from aistorage.clock import Clock, format_rfc3339, parse_rfc3339
from aistorage.converters import get_converter
from aistorage.converters.base import ConversionError, Converter
from aistorage.errors import MismatchError
from aistorage.reading import check_continuation

if TYPE_CHECKING:  # pragma: no cover
    # Decision 只用在型別註解（`from __future__ import annotations` 讓它不需在執行期存在）。
    # 執行期匯入會形成循環：agora.__init__ → agora.apply → intake.evaluate → agora.store
    # →（回到）agora.__init__，結果 `import aistorage.intake.evaluate` 直接失敗。
    # 依賴方向應該是 intake → agora，不是 agora → intake。
    from aistorage.intake.evaluate import Decision


#: 每個 profile 能同時掛著的**未結預留**（`reserved` 且還沒有後續快照）數量上限
#: （review-2bc0785 M2 開始，review-1926cd3-142fd04 M3 改成跨輪累計）。任何
#: profile 都能為任何一份既有快照送接續，所以不設上限時，讀取視圖要發佈的閱讀版
#: 數量沒有邊界。預設值給 20：同時掛著十幾個沒人開工的預留已經是不對勁的量。
DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE = 20

#: 預留（checkout 為新 session 準備的空紀錄）多久算過期
#: （review-2bc0785 M2／L）。**期 1 不自動刪除**：刪除真本裡的項目是管理操作，
#: 不該由提交流程顺手做。期限只是顯示與管理用的訊號（`agora show` 標 `expired`）。
DEFAULT_RESERVED_TTL_DAYS = 7


@dataclass(frozen=True)
class ApplyResult:
    """單一決策套用結果。

    ok=True（含 code="already" 的同內容冪等跳過）表示 run.py 可繼續；
    ok=False 表示轉成 REJECT（拒收檔已由本模組寫入，不影響同輪其他項目）。
    paths 為本次寫入的新增相對路徑（供 run.py 批次 commit 與發佈用）。
    rejected_at／deletable_after 供 run.py 發佈拒收原因（24 小時窗口）。
    detail 是給寫入端看的補充說明（目前只有預留上限用得到，
    review-55edd374 M3），隨拒收紀錄發佈；沒有就留 None。
    """

    ok: bool
    code: str
    paths: list[str] = field(default_factory=list)
    rejected_at: str | None = None
    deletable_after: datetime | None = None
    detail: str | None = None


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
    dec: Decision,
    code: str,
    at: str,
    item_id: str | None,
    detail: str | None = None,
) -> str:
    """寫入拒收紀錄（第 4 組發佈到讀取視圖的來源）。回傳相對路徑。

    M3：形狀必須與 evaluate 的 `reject_decision` 完全一致（`inbox_folder_id`、
    `candidate_ids`、`entries`），否則 evaluate 的拒收快取認不得這一筆，
    同一個項目會每一輪重新評估、重新拒收。`at` 只在第一次寫入。

    `detail` 只加在**最上層**、不放進 `entries`：entries 是 evaluate 與 apply 共用
    的 fast path，比對只看 `inbox_folder_id` ＋ `candidate_ids`，多帶一個欄位不會
    壞掉比對，但保持兩邊形狀一致比較安全（review-55edd374 M3）。

    item_key 格式錯誤時直接 raise（scan 已驗證 ULID，走到這裡代表程式錯誤）。
    """
    item = getattr(dec, "item", None)
    sidecars = list(getattr(item, "sidecars", []) or [])
    sigs = list(getattr(item, "sigs", []) or [])
    rejections.record_rejection(
        store,
        item_key=_item_key(dec),
        code=code,
        at=at,
        item_id=item_id,
        inbox_folder_id=str(getattr(item, "inbox_folder_id", "") or ""),
        candidates=rejections.candidate_ids(sidecars, sigs),
        detail=detail,
    )
    return layout.rejection_path(_item_key(dec))


def _item_files(dec: Decision) -> list[Any]:
    """該項目的所有候選檔案（sidecar／sig／raw／extra），提供 created_time 給刪除期限。"""
    item = getattr(dec, "item", None)
    out: list[Any] = []
    for name in ("sidecars", "sigs", "raws", "extras"):
        out.extend(list(getattr(item, name, []) or []))
    return out


def _fail(
    store: AgoraStore,
    dec: Decision,
    before: list[str],
    code: str,
    at: str,
    *,
    detail: str | None = None,
) -> ApplyResult:
    """套用失敗轉 REJECT：寫拒收紀錄，回傳 ok=False。

    `detail` 是給寫入端看的補充說明（`link_quota_exceeded` 會帶「哪些預留佔住
    額度」，review-55edd374 M3）；它寫進拒收紀錄，跟著發佈到讀取視圖。
    """
    item_id = _record(dec).get("id")
    _write_rejection(store, dec, code, at,
                     item_id if isinstance(item_id, str) else None,
                     detail=detail)
    return ApplyResult(
        ok=False,
        code=code,
        paths=_new_paths(store, before),
        rejected_at=at,
        # M3／docs 4.4：刪除期限以項目檔案最早的 created_time 為基準，
        # 不是「當下 + 24h」，否則躺了幾天的壞項目永遠多等一天。
        deletable_after=rejections.deletable_after_for(_item_files(dec), at),
        detail=detail,
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


def _existing_continuation_links(store: AgoraStore, new_session_id: str
                                 ) -> list[tuple[str, dict[str, Any]]]:
    """新 Session 已經發出的接續 Link：[(相對路徑, 內容), ...]。

    讀不到就 raise（`store.read_json_file` 會把損毀轉成 MismatchError）——真本
    的索引損毀不能當成「沒有這條 Link」，那會讓同一個起點被記成兩條。
    """
    base = store.worktree / layout.continuation_link_dir(new_session_id)
    if not base.is_dir():
        return []
    return [
        (f"links/continuation/{layout.enc(new_session_id)}/{p.name}",
         store.read_json_file(
             f"links/continuation/{layout.enc(new_session_id)}/{p.name}"))
        for p in sorted(base.glob("*.json"))
    ]


def _check_one_link_per_source(
    store: AgoraStore,
    new_session_id: str,
    target_id: str,
    continuation: dict[str, Any] | None,
    *,
    own_link_rel: str,
    own_item_id: str,
) -> str:
    """**同一個新 Session 對同一個被接續 Session 只能有一條接續 Link**
    （review-2bc0785 M1）。claim 與 continuation 共用這一個檢查。

    回傳三種狀態，呼叫端各自映射成自己的結果：

    - `clear`：還沒有指向 `target_id` 的 Link → 可以寫一條。
    - `own`：唯一那條 Link 就是**本項目自己**寫的那個（`own_link_rel` 加上
      `own_item_id`，中斷後重跑要補齊的情況）→ 往下走補齊，不要重寫。
    - `same`：已經有一條指向 `target_id`、**接續點也相同**、而且是**接續單**建的
      Link → 這是同一個起點的重送（換 `item_key` 重送、`--resume`），冪等。
    - `duplicate`：已經有指向同一個 `to` 的 Link，而這一次**不是**上面那兩種
      （接續點不同、或既有那條是 claim 建的）→ 這個新 Session 已經在接續那個
      Session 了，再要一條就是送錯。

    判成 `duplicate` 而不是一律冪等，是為了讓**兩種順序的結果一樣**：先 claim
    再 continuation 與先 continuation 再 claim，第二筆都拿到 `duplicate_link`，
    真本裡永遠只有一條 Link，留下哪一條不再取決於套用順序。

    Link 缺 `continuation` 時 raise MismatchError：真本索引不完整不能當成
    「沒有這條 Link」，那會讓同一組起點被記成兩條。
    """
    cont = continuation if isinstance(continuation, dict) else {}
    same = False
    for rel, existing_link in _existing_continuation_links(store, new_session_id):
        if existing_link.get("to") != target_id:
            continue
        if (rel == own_link_rel
                and (existing_link.get("claim_id") == own_item_id
                     or existing_link.get("continuation_id") == own_item_id)):
            return "own"        # 自己寫的那條（寫到一半中斷）→ 往下補齊
        existing_cont = existing_link.get("continuation")
        if not isinstance(existing_cont, dict):
            raise MismatchError(f"接續 Link 缺少 continuation: {rel}（真本索引不完整）")
        if (str(existing_cont.get("snapshot_sha256") or "").lower()
                == str(cont.get("snapshot_sha256") or "").lower()
                and existing_cont.get("message_id") == cont.get("message_id")
                and not existing_link.get("claim_id")):
            same = True          # 接續單建的那一條、接續點又相同 → 重送
        else:
            return "duplicate"
    return "same" if same else "clear"


def _is_stopped(facts: Any) -> bool:
    """3.9：事實上有封存紀錄，且最後一則訊息不晚於封存 → 停止中；否則運作中。

    比較一律用毫秒整數時間戳（R3）；缺少任一者時，有封存、無訊息視為停止中。

    **「訊息」用 `created` 判斷，不用 completed**（review-g5-6 H3、D10）：
    宣告停止是在 AI 回覆**生成中**呼叫的，那一則訊息在封存**之前**建立、
    封存**之後**才完成。用 completed 判，下一輪會把它誤認成「封存之後的新訊息」
    而自己恢復成 running，宣告停止就永遠不成立。
    `last_message_created_ms` 沒有（舊的 facts 物件）時退回 `last_message_ms`。
    """
    archived_ms = getattr(facts, "archived_ms", None)
    last_ms = getattr(facts, "last_message_created_ms", None)
    if last_ms is None:
        last_ms = getattr(facts, "last_message_ms", None)
    if archived_ms is None or archived_ms <= 0:
        return False
    if last_ms is None:
        return True
    return last_ms <= archived_ms


def has_message_created_after_archive(facts: Any) -> bool:
    """封存之後**又建立了**訊息 → 不能宣告停止中（review-g5-6 H3）。

    沒有封存時間就回 False（那不是「有訊息在封存之後」）。
    """
    archived_ms = getattr(facts, "archived_ms", None)
    if archived_ms is None or archived_ms <= 0:
        return False
    created_ms = getattr(facts, "last_message_created_ms", None)
    if created_ms is None:
        return False
    return created_ms > archived_ms


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
        # `reserved` 是預留專用的狀態：新的快照表示有人真的載入那個 session 了
        # （預留的那份是零則訊息的空匯出檔，不會再送一次），所以預留到此為止。
        # facts 失敗時判不出 running／stopped，但「不再只是預留」是確定的。
        status = existing.status if existing is not None else "running"
        if status == "reserved":
            status = "running"
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


def _build_reserved_session(
    store: AgoraStore,
    dec: Decision,
    rec: dict,
    sc: dict,
    claimer_id: str,
    clock: Clock,
) -> SessionRecord | ApplyResult:
    """從 claim／continuation 單裡的預留組出那個**空**的新 Session 紀錄（不寫入）。

    預留是 `agora checkout` 替新 session 準備的第一份快照：**零則訊息**的空匯出檔
    （review-73dbf2c H2）。它不是來源 session 的副本，所以 Agora 裡不會出現一份
    掛在新 id 底下、內容與標題全是錯的紀錄；而被拒時這裡根本不會被呼叫到寫入。

    回傳 `ApplyResult` 代表形狀不合法（呼叫端轉成 REJECT）；回傳 `SessionRecord`
    代表可以寫。**不寫任何東西**——寫入由呼叫端在所有檢查都通過之後才做。

    **狀態是 `reserved` 而不是 `running`**（review-2bc0785 M2／L）：這筆 session
    還沒有人開工，帶著期限（`reserved_until`）。有人真的載入它（第一份真實快照）
    之後，`apply_session` 會把它改回 `running`／`stopped`，期限欄一併消失。
    """
    before = list(store.changed_paths())
    now_str = format_rfc3339(clock.now(), include_fraction=True)
    sess = sc.get("session")
    if not isinstance(sess, dict):
        # 沒有預留、認領者又不存在 → 認領一個不存在的 session
        return _fail(store, dec, before, "unknown_claimer", now_str)

    def bad(code: str) -> ApplyResult:
        return _fail(store, dec, before, code, now_str)

    source = sess.get("source")
    native = sess.get("source_session_id")
    if not isinstance(source, str) or not isinstance(native, str):
        return bad("invalid_format")
    # 預留的 session 必須**就是**那個 claimer：不能挾帶別的 session 的內容
    if f"{source}:{native}" != claimer_id:
        return bad("invalid_format")
    if sess.get("parent_id") is not None:
        # 接續只能由主 Session 發起；預留也一樣
        return bad("claim_from_subsession")
    snapshot_at = sess.get("snapshot_at")
    if not isinstance(snapshot_at, str) or not snapshot_at:
        return bad("invalid_format")
    try:
        parse_rfc3339(snapshot_at)
    except ValueError:
        return bad("invalid_format")

    raw_p = dec.raw_path
    if raw_p is None:
        return bad("invalid_format")
    raw_bytes = Path(raw_p).read_bytes()
    incoming_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
    raw_meta = sc.get("raw")
    if isinstance(raw_meta, dict):
        decl_sha = raw_meta.get("sha256")
        decl_size = raw_meta.get("size")
        if (
            (isinstance(decl_sha, str) and decl_sha.lower() != incoming_sha)
            or (isinstance(decl_size, int) and decl_size != len(raw_bytes))
        ):
            return bad("raw_mismatch")

    # 預留的匯出檔形狀由 `agora checkout` 保證（零則訊息）；轉換器讀不出來就照
    # 收（與 `apply_session` 的 H2 同一個立場：raw 才是真本），但記下代碼。
    title: str | None = None
    reading_status = "ok"
    reading_error_code: str | None = None
    try:
        facts = get_converter(source).facts(Path(raw_p))
        title = getattr(facts, "title", None)
    except Exception:
        reading_status = "failed"
        reading_error_code = "facts_error"

    return SessionRecord(
        id=claimer_id,
        producer=rec.get("producer", ""),
        created_at=rec.get("created_at", ""),
        updated_at=rec.get("updated_at", ""),
        status="reserved",
        snapshot_at=snapshot_at,
        raw_sha256=incoming_sha,
        raw_size=len(raw_bytes),
        committed_at=now_str,
        last_item_key=_item_key(dec),
        case_id=rec.get("case_id"),
        provenance=rec.get("provenance"),
        role=rec.get("role"),
        role_version=rec.get("role_version"),
        stopped_at=None,
        parent_id=None,
        in_progress=False,
        archived_at=None,
        title=title,
        extra={
            "reading_status": reading_status,
            "reading_error_code": reading_error_code,
            "reading_error_message": None,
            "reserved_by": rec.get("id"),
            "reserved_until": _reserved_until(clock),
        },
    )


def _reserved_until(clock: Clock) -> str:
    """預留的期限（`reserved_until`）：建立之後 `DEFAULT_RESERVED_TTL_DAYS` 天。

    **期 1 不會自動刪除過期的預留**（review-2bc0785 M2／L，PM 裁決）：刪除真本裡
    的項目是管理操作（`admin erase`／rollback），提交流程不該顺手做。期限是顯示與
    管理用的訊號——`agora show` 看到過期就標 `expired`，要清理由人決定。
    """
    return format_rfc3339(
        clock.now() + timedelta(days=DEFAULT_RESERVED_TTL_DAYS),
        include_fraction=True)


def _reservation_expired(meta: dict, now: datetime) -> bool:
    """這筆預留是不是已經過了 `reserved_until`？

    期 1 不會自動刪除過期的預留（見 `_reserved_until`），所以「過期」純粹是一個
    判斷：**它還在真本裡、還會被 `agora show` 標成 `expired`、還在等人清理，
    只是不再佔未結預留的額度**（review-55edd374 M3）。否則一個放棄過太多次
    checkout 的 profile 會被自己過去留下的殘留永久鎖住，而且不會自己好。

    期限**讀不到或壞掉時一律當作還沒過期**（仍然佔額度）：上限是保護，壞掉就
    放行等於把它關掉。
    """
    raw = meta.get("reserved_until")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        deadline = parse_rfc3339(raw)
    except ValueError:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now


def _open_reservation_ids(
    store: AgoraStore, producer: str, now: datetime
) -> list[str]:
    """這個 profile 目前的**未結預留** session id（直接從真本算，沒有任何帳本）。

    「未結」＝ `status == "reserved"` **而且**只有預留自己那一份快照。三個條件都查：
    `apply_session` 收到新快照時本來就會把狀態換成 running／stopped，再查一次快照
    數是因為期限只顯示、不刪除（期 1 不自動清除過期的預留，見 `_reserved_until`），
    真本裡一筆「狀態說是預留、快照卻已經有後續」的行不該繼續佔著額度。

    **過了 `reserved_until` 的不算數**（review-55edd374 M3）：它們仍然存在、
    仍然被顯示、仍然等人清理，只是不佔額度——這樣放棄過太多次 checkout 的
    profile 過了一週就自己好，不必管理者先動手抹。

    **為什麼不算在程式裡**（review-1926cd3-142fd04 M3，PM 裁決）：原本這裡有一個
    每輪歸零的 in-process ledger，但輪數是住民自己觸發的（`sync and commit` 會觸發
    提交流程），每輪歸零等於只是限速、總量沒有上限。這個數量本來就擺在真本裡，
    每輪重算就跨輪一致，也不用擔心崩潰時少算。

    讀不到或損毀的 meta／快照歷史由 `read_json_file`／`snapshots` 往上拋
    （fail-closed），不會被當成「這個 profile 沒有預留」而放行。
    """
    root = store.worktree / "sessions"
    if not root.is_dir():
        return []
    open_ids: list[str] = []
    for source_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for sess_dir in sorted(p for p in source_dir.iterdir() if p.is_dir()):
            rel = f"sessions/{source_dir.name}/{sess_dir.name}/meta.json"
            if not (store.worktree / rel).is_file():
                continue
            meta = store.read_json_file(rel)
            if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
                continue
            if meta.get("producer") != producer or meta.get("status") != "reserved":
                continue
            if len(store.snapshots(meta["id"])) > 1:
                continue          # 已經有後續快照 → 預留已經開工，不再計數
            if _reservation_expired(meta, now):
                continue          # 過期：仍然顯示、等人清理，但不佔額度
            open_ids.append(meta["id"])
    return open_ids


#: 拒收訊息裡最多列幾個佔住額度的預留（再多對寫入端也沒用，看要清的是哪幾個就夠）
QUOTA_DETAIL_LIMIT = 10


def _quota_detail(open_ids: Sequence[str], cap: int) -> str:
    """`link_quota_exceeded` 的詳細訊息：到底是哪些預留佔住了額度。

    沒有這段的話，寫入端只看到一個 `link_quota_exceeded`，既不知道是哪些預留、
    也不知道該去看哪裡（review-55edd374 M3）。內容**只有 session id**——那些本來
    就是讀取視圖裡公開的欄位，不帶任何交接內容或訊息本文。
    """
    shown = list(open_ids[:QUOTA_DETAIL_LIMIT])
    more = len(open_ids) - len(shown)
    listed = "、".join(shown)
    tail = f"…另外 {more} 筆" if more > 0 else ""
    return (
        f"這個 profile 已有 {len(open_ids)} 筆未結預留，達到上限 {cap}；"
        f"佔住額度的預留：{listed}{tail}。"
        f"清掉不再需要的預留（或等它們過期）就能再 checkout；"
        f"過期的預留不佔額度但要由管理者抹除。"
    )


def _check_reservation_cap(
    store: AgoraStore,
    dec: Decision,
    before: list[str],
    producer: str,
    now_str: str,
    *,
    max_open_reservations: int,
) -> ApplyResult | None:
    """每個 profile 的**未結預留**數量上限，超過就明確拒收（`link_quota_exceeded`）。

    只有**會新增一筆預留**的項目受這個上限管：claim／continuation 帶著 `session`
    區塊時才會在真本裡多一個沒人開工的 Session（每個都是讀取視圖要發佈的一份閱讀
    版）。不預留的項目不讓未結預留數變多，所以不佔額度；冪等重送更早一步就
    `already` 回傳，同樣不佔額度（否則逾時重跑會把自己的額度吃掉）。

    拒收碼沿用 `link_quota_exceeded`：它是已經發佈到讀取視圖給寫入端看的外部契約，
    語意從「每輪限速」改成「跨輪累計」不該換掉寫入端認得的那個字串。詳細訊息
    （哪些預留佔住額度）走 `detail` 欄，隨拒收紀錄發佈到讀取視圖。

    回傳 `None` 代表還在上限內。
    """
    if max_open_reservations <= 0:
        # 刻意不保留「0 ＝ 不設上限」這個後門：一個打錯字就默默關掉保護。設定檔在
        # 載入時就擋掉（`committer/config.py`），走到這裡代表呼叫端傳了不法的程式值。
        raise ValueError(
            f"max_open_reservations 必須是正整數: {max_open_reservations!r}")
    open_ids = _open_reservation_ids(store, producer, parse_rfc3339(now_str))
    if len(open_ids) < max_open_reservations:
        return None
    return _fail(
        store, dec, before, "link_quota_exceeded", now_str,
        detail=_quota_detail(open_ids, max_open_reservations),
    )


def apply_claim(
    store: AgoraStore,
    dec: Decision,
    clock: Clock,
    *,
    max_open_reservations: int = DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE,
) -> ApplyResult:
    """套用認領：交接單存在且未被認領、認領者 Session 已在 Agora（含本輪剛收），
    寫 claim、標記 claimed_by、建接續 Link（claimer → target，記接續點）。

    一張交接單只能被認領一次（同輪第二個認領者看到 claimed_by 非空 → already_claimed）；
    一個 Session 可認領**不同**的多張（統合：S2、S3 各一張交接單 → 兩條 Link）。
    H3：認領者必須是自己的持有者（producer 相同）、是主 Session（parent_id 為 None）、
    且不能是交接單的目標本身。

    **預留（review-73dbf2c H2）**：帶 `session` 區塊的 claim **自己帶著**一個空的
    新 Session 紀錄（`agora checkout` 預留給新 session 的第一份快照，狀態
    `reserved` 並帶期限）。所以認領者不必是「先存在的 session」，而且**被拒時
    Agora 裡不會留下任何東西**——所有檢查都通過之後才寫，順序是
    預留 → link → handoff → claim。

    **一個新 Session 對同一個被接續 Session 只能一條 Link**（review-2bc0785 M1）：
    這裡與 `apply_continuation` 共用 `_check_one_link_per_source`。同一個新 session
    認領同一個 session 的**第二張**交接單 → `duplicate_link`（不管接續點是否相同：
    兩個起點指向同一個來源就是同一件事，checkout 也擋掉這種組合）。

    **預留數量上限**（`_check_reservation_cap`）：每個 profile 能同時掛著的未結
    預留有上限，跨輪累計、從真本算。帶預留的 claim 撞到上限 → `link_quota_exceeded`。

    失敗碼：invalid_format、unknown_handoff、already_claimed、unknown_claimer、
    not_holder、claim_from_subsession、self_claim、duplicate_link、
    link_quota_exceeded、stale。
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
    reserved: SessionRecord | None = None
    if claimer is None:
        # 預留路徑：claim 自己帶著一個空的新 Session 紀錄（review-73dbf2c H2）
        built = _build_reserved_session(store, dec, rec, sc, claimer_id, clock)
        if isinstance(built, ApplyResult):
            return built  # 預留形狀不合法 → 明確拒收（不寫入任何東西）
        reserved = built
        producer = rec.get("producer")
        parent_id = None
    else:
        producer = claimer.producer
        parent_id = claimer.parent_id
    if not isinstance(producer, str) or rec.get("producer") != producer:
        return _fail(store, dec, before, "not_holder", now_str)
    if parent_id is not None:
        return _fail(store, dec, before, "claim_from_subsession", now_str)

    h_body = handoff.get("body", {})
    target_id = h_body.get("target_session_id") if isinstance(h_body, dict) else None
    continuation = h_body.get("continuation") if isinstance(h_body, dict) else None
    if claimer_id == target_id:
        return _fail(store, dec, before, "self_claim", now_str)
    if not isinstance(target_id, str) or not target_id:
        return _fail(store, dec, before, "invalid_format", now_str)
    target_sess = store.get_session(target_id)
    if target_sess is None:
        return _fail(store, dec, before, "unknown_target", now_str)
    if target_sess.parent_id is not None:
        # 被接續的目標也必須是主 Session（與交接單一致；review-2bc0785 L）
        return _fail(store, dec, before, "continuation_to_subsession", now_str)

    # M1：同一個新 Session 對同一個被接續 Session 只能有一條 Link。
    # 這裡與 `apply_continuation` 共用 `_check_one_link_per_source`；那條 Link 是
    # **認領**建的，所以同一個新 session 認領同一個 session 的第二張交接單
    # （接續點相同也一樣）→ `duplicate_link`。
    link_state = _check_one_link_per_source(
        store, claimer_id, target_id, continuation,
        own_link_rel=link_rel, own_item_id=str(rec_id))
    if link_state in ("same", "duplicate"):
        return _fail(store, dec, before, "duplicate_link", now_str)

    # M2：每個 profile 的未結預留數量上限（跨輪累計，不是每輪歸零）。只有這次
    # 真的會預留一個新 session 才佔額度。
    if reserved is not None:
        cap = _check_reservation_cap(
            store, dec, before, str(producer), now_str,
            max_open_reservations=max_open_reservations)
        if cap is not None:
            return cap

    # ---- 寫入階段（顺序：預留 → link → handoff → claim；错误往上抛） ----
    if reserved is not None:
        # 預留只在這裡才寫：前面任何一個檢查失敗都已經回傳了，Agora 裡不會留下
        # 「已被別人預留、卻沒有接續 Link」的 session。
        raw_p = Path(dec.raw_path) if dec.raw_path is not None else None
        assert raw_p is not None  # _build_reserved_session 已驗過
        store.put_session(reserved, raw_p, via="import")
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


def apply_continuation(
    store: AgoraStore, dec: Decision, conv: Converter, clock: Clock,
    *,
    max_open_reservations: int = DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE,
) -> ApplyResult:
    """套用接續單：**每一次 checkout 都記錄接續 Link**（impl2 M6）。

    起點是交接單時走 `apply_claim`（claim 本身就代表接續，不會另外送接續單）；
    起點是 `<session>[@<訊息>]` 時走這裡。四種關係因此都會留下一條接續 Link：
    1→1、1→n、n→1 各是一個新 Session 對每個起點一條，n↔m 走 `apply_reference`。

    接續點必須指向**被接續 Session 的一份既有快照**，而且那一則訊息要在該快照裡
    存在、已完成、未撤銷（M3 的「已完成的訊息才算數」）。這裡**不**要求它是該
    快照最後一則完成的訊息——交接單是「交出手上做到哪」，接續點自然是最後一則；
    直接起點則允許從任何位置開始（`agora checkout <session>@<訊息>`）。

    冪等：同一個新 Session 對同一個起點（同一個 to ＋ 同一個接續點）只保留一條
    Link，換 `item_key` 重送也一樣；同一個 id 重送且內容相同 → already。已經有
    那條 Link 時**提早回傳**（review-2bc0785 L）：既有 Link 記的就是同一個接續
    點，再轉換一次那份快照只是白費力氣。

    H3：只有新 Session 的持有者（producer 相同）能建立它的接續 Link，且必須是
    主 Session（parent_id 為 None）、不能接續自己。**被接續的目標也必須是主
    Session**（與交接單一致；review-2bc0785 L／Night decisions）——子 Session
    是母 Session 內部的一段工作，要接就接那個母 Session。

    **預留數量上限**（`_check_reservation_cap`）：每個 profile 能同時掛著的未結
    預留有上限，跨輪累計、從真本算。帶預留的接續單撞到上限 →
    `link_quota_exceeded`。

    失敗碼：invalid_format、unknown_target、unknown_claimer、not_holder、
    continuation_from_subsession、continuation_to_subsession、self_continuation、
    invalid_continuation、duplicate_link、link_quota_exceeded、stale。
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
    new_id = body.get("new_session_id")
    continuation = body.get("continuation")
    if not (isinstance(target_id, str) and target_id
            and isinstance(new_id, str) and new_id
            and isinstance(continuation, dict)):
        return _fail(store, dec, before, "invalid_format", now_str)
    snap_sha = continuation.get("snapshot_sha256")
    message_id = continuation.get("message_id")
    if not isinstance(snap_sha, str) or not snap_sha:
        return _fail(store, dec, before, "invalid_continuation", now_str)
    if not isinstance(message_id, str) or not message_id:
        return _fail(store, dec, before, "invalid_continuation", now_str)

    rec_id = rec.get("id")
    ulid = rec_id.split(":", 1)[1] if isinstance(rec_id, str) and ":" in rec_id else ""
    rel = layout.continuation_path(ulid)  # ULID 非法 → ValueError 往上拋（程式錯誤）
    link_rel = layout.continuation_link_path(new_id, ulid)

    # 同 id 重送：內容相同且 Link 也在 → 冪等跳過；內容不同 → stale。
    dup = store.get_record(rec_id) if isinstance(rec_id, str) else None
    if dup is not None:
        if dup.get("body") != body:
            return _fail(store, dec, before, "stale", now_str)
        link_now = store.worktree / link_rel
        if link_now.is_file():
            return ApplyResult(ok=True, code="already", paths=[])
        # Link 還沒寫（中断後重跑）→ 往下補齊

    if new_id == target_id:
        return _fail(store, dec, before, "self_continuation", now_str)

    target = store.get_session(target_id)
    if target is None:
        return _fail(store, dec, before, "unknown_target", now_str)
    if target.parent_id is not None:
        # 被接續的目標也必須是主 Session（與交接單一致）
        return _fail(store, dec, before, "continuation_to_subsession", now_str)

    new_sess = store.get_session(new_id)
    reserved: SessionRecord | None = None
    if new_sess is None:
        # 預留路徑：接續單自己帶著新 Session 的空紀錄（與 claim 同一個理由：
        # 被拒時 Agora 裡不該留下任何東西）。
        built = _build_reserved_session(store, dec, rec, sc, new_id, clock)
        if isinstance(built, ApplyResult):
            return built
        reserved = built
        producer = rec.get("producer")
    else:
        producer = new_sess.producer
    if not isinstance(producer, str) or rec.get("producer") != producer:
        return _fail(store, dec, before, "not_holder", now_str)
    if new_sess is not None and new_sess.parent_id is not None:
        # 接續只能由主 Session 發起；預留也一樣
        return _fail(store, dec, before, "continuation_from_subsession", now_str)

    # M1：同一個新 Session 對同一個被接續 Session 只能有一條 Link（與
    # `apply_claim` 共用同一個檢查）。`own`＝中斷後重跑（Link 已寫、接續單還沒
    # 寫），照樣往下補齊；`same`＝同一個起點的重送（冪等提早回傳）；`duplicate`
    # ＝同一個來源的第二個起點，或同一個來源被 claim 接走過 → 明確拒收。
    # 索引讀取失敗或損毀 → MismatchError 中止，不轉 REJECT。
    link_state = _check_one_link_per_source(
        store, new_id, target_id, continuation,
        own_link_rel=link_rel, own_item_id=str(rec_id))
    if link_state == "same":
        return ApplyResult(ok=True, code="already", paths=[])
    if link_state == "duplicate":
        return _fail(store, dec, before, "duplicate_link", now_str)

    # 接續點必須落在**被接續 Session 既有快照**裡（不是最新版本）。
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

    # M2：每個 profile 的未結預留數量上限（跨輪累計，不是每輪歸零）。只有這次
    # 真的會預留一個新 session 才佔額度；冪等重送上面就提早回傳了。
    if reserved is not None:
        cap = _check_reservation_cap(
            store, dec, before, str(producer), now_str,
            max_open_reservations=max_open_reservations)
        if cap is not None:
            return cap

    # ---- 寫入階段（順序：預留 → Link → 接續單；錯誤往上拋） ----
    if reserved is not None:
        raw_path = Path(dec.raw_path) if dec.raw_path is not None else None
        assert raw_path is not None  # _build_reserved_session 已驗過
        store.put_session(reserved, raw_path, via="import")
    store.put_json(
        link_rel,
        {
            "from": new_id,
            "to": target_id,
            "continuation": {"snapshot_sha256": snap_sha.lower(),
                             "message_id": message_id},
            "continuation_id": rec_id,
        },
    )
    store.put_json(
        rel,
        {
            **rec,
            "body": body,
            "committed_at": now_str,
            "result": {"link": link_rel},
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
