"""`agora checkout`：把起點變成一個起點包（docs/design/agora-session-operations.md）。

流程（設計文件第 1、2、3 點），**順序本身就是安全的一部分**：

1. 從讀取介面取得起點與**被釘住的快照**，把原始紀錄**原封不動**放進起點包；
2. **所有本機檢查都在登記認領之前完成**（review-73dbf2c H1）：輸出目錄可寫而且是
   空的、每段 raw 的 SHA-256 等於快照雜湊、Agora 物件讀得到。任一項失敗就停下，
   這個世界裡還沒有任何認領；
3. 起點是交接單時，放**一筆**認領進收件匣——認領自己**預留**那個空的新 session，
   等讀取介面確認，**被拒就不產出**；
4. 確認之後才把 `package.json` 寫進暫存目錄並一次改名（此時才知道 claim id）。

**認領被接受時，等候確認的同時新 session 已經是一筆空紀錄**（`apply_claim` 從
認領單裡的預留生出來）。被拒時**什麼都沒寫進 Agora**：沒有「已被認領卻沒有人接手」
的交接單，也沒有留在 Agora 裡的假快照（review-73dbf2c H2）。

**重跑要沿用同一個認領**（`--resume`，見 `agora_cli/claims.py`）：交接單只能被
認領一次，換一個新的 claim id 重來只會得到 `already_claimed`，而那張單永遠不會
有 session 接手。

**這個模組完全不碰任何 coding agent**：原始紀錄是來源應用的匯出檔，起點包
原封不動地放著它。截斷、重編 id、匯入是轉接器（`agora-opencode load`）的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Sequence

from aistorage.agora_cli.claims import ClaimJournal, ClaimRecord
from aistorage.agora_cli.objects import ObjectError
from aistorage.agora_cli.package import (
    DEFAULT_MAX_CONTEXT_CHARS,
    ContextLimitExceeded,
    ContextPackage,
    ContextPackageError,
    PackageSegment,
    check_context_length,
    commit_staged_package,
    order_segments,
    stage_package,
)
from aistorage.agora_cli.startpoint import (
    ResolvedStartPoint,
    StartPointError,
    parse_startpoint,
    resolve_startpoint,
)

#: 認領放進收件匣之後，等讀取介面確認的時間。逾時就明確拒絕（不產出）。
DEFAULT_CLAIM_TIMEOUT = timedelta(minutes=15)

#: 預留給新 session 的匯出檔標題前綴（沒有 `--task` 時用）。
RESERVED_TITLE = "Agora 起點"


class CheckoutError(RuntimeError):
    """`checkout` 失敗。訊息裡只有 id、代碼與規則，不含 Session 內文。"""


class ClaimRejected(CheckoutError):
    """認領被拒收（或等不到確認）——**不產出起點包**。

    照 AGENTS.md 的「認領」：接續 Link 屬於自己之後才開工。被拒就停下來，
    不要重試同一批（被拒的原因通常要人處理，例如交接單已被別人接走）。
    """


@dataclass
class CheckoutDeps:
    """`checkout` 需要的相依（全部注入，測試不需要真的 Drive／網路）。"""

    reader: Any
    clock: Any
    #: 依 annex key 去 Agora 物件資料夾取原始紀錄（`objects.ObjectFetcher`）。
    #: **沒有它就沒有起點包**——讀取視圖不發佈 raw 的位元組。
    objects: Any = None
    #: 產生認領用的東西（profile、簽章金鑰、收件匣）。`None` 表示這台機器
    #: 沒有寫入身分 → 起點是交接單時明確拒絕（不能假裝認領了）。
    signer: Any = None
    inbox_folder_id: str = ""
    drive: Any = None
    #: 放認領並同步並提交（照 skill 的 `_export_and_commit`：回傳
    #: `rejected`／`timed_out`／`ok`）。測試注入假的。
    commit_claim: Any = None
    #: 本機認領記錄（`--resume` 的依據）。`None` 表示不記——那樣重跑會拿到
    #: `already_claimed` 而認不回來，所以 CLI 一律給它一個。
    journal: ClaimJournal | None = None


def _measure(reader: Any, resolved: ResolvedStartPoint, raw: bytes, *,
              max_lag: Any) -> tuple[int, int]:
    """算這一段在接續點之前有幾則訊息、多少純文字字元。

    **長度用閱讀版的 `plain_text` 算，不用 raw 位元組**：真正會變成 token 的是
    文字，raw 還包含沒有進模型的欄位（id、時間戳），用它估會過度保守而誤殺。
    閱讀版轉不出來就退回「用 raw 位元組當上限估計」並照樣明確拒絕——寧可拒多
    不可放行。
    """
    from aistorage.reading import plain_text

    try:
        result = reader.get_reading(
            resolved.session_id, snapshot_sha256=resolved.snapshot_sha256,
            max_lag=max_lag)
    except Exception:
        return (0, len(raw))
    reading = getattr(result, "value", result)
    try:
        from aistorage.reading import messages_before

        if resolved.message_id:
            messages = messages_before(
                reading, resolved.message_id,
                snapshot_sha256=resolved.snapshot_sha256)
        else:
            messages = [m for m in (reading.get("messages") or [])
                        if not m.get("reverted", False)]
        return (len(messages), len(plain_text({"messages": messages})))
    except Exception:
        return (0, len(raw))


def _new_session_id(source: str) -> str:
    """為這個起點包編一個新的 Session id（Agora 的完整形式）。

    轉接器匯入時會**沿用**這個 id（而不是自己編），所以收件匣裡的認領
    （`claimer_session_id`）與之後同步器送上來的新 Session 對得上——那樣
    提交流程才收得到接續 Link。

    尾段沿用來源應用慣例（opencode 是 `ses_` ＋ ULID 的後 16 碼）：opencode 的
    `import` 對 id 前綴寬鬆但仍以 `ses_` 為慣例（spike Q1-4）。1→n 時每個起點包
    各編一套（不能共用：id 撞了匯入會被靜默丟棄）。
    """
    from aistorage.schema import generate_ulid

    return f"{source}:ses_{generate_ulid()[-16:]}"


def _empty_export_bytes(native_id: str, title: str, reserved_at_ms: int) -> bytes:
    """新 session 的**空**匯出檔（零則訊息）——認領預留用的那份。

    **不是**來源 session 的截斷副本：Agora 裡不該出現一份「別人的對話」掛在
    新 session id 底下（review-73dbf2c H2：標題、狀態、接續點之後的訊息全都會
    是錯的，而且被拒時那份複製會變成孤兒）。

    **位元組必須由參數唯一決定**：`--resume` 重跑時要產生一模一樣的檔案，否則
    清冊會看成換了一個 item（`replayed_item_key`）而拒收。
    """
    payload = {
        "info": {
            "id": native_id,
            "title": title,
            "time": {"created": reserved_at_ms, "updated": reserved_at_ms},
        },
        "messages": [],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _reserved_title(task: str | None, handoff_id: str | None) -> str:
    """預留 session 的標題（`--task` 優先，否則指出它從哪張交接單來）。"""
    text = (task or "").strip()
    if text:
        return text[:120]
    if handoff_id:
        return f"{RESERVED_TITLE}（{handoff_id}）"[:120]
    return RESERVED_TITLE


def _reservation_path(stage: Path, item_key: str) -> Path:
    """預留匯出檔的路徑（由 `item_key` 唯一決定，重跑會是同一個檔名）。"""
    return stage / f"reserved-{item_key}.json"


def _build_claim(deps: CheckoutDeps, resolved: ResolvedStartPoint, *,
                 record: ClaimRecord, stage: Path) -> Any:
    """組一筆認領，並且**把新 session 的空紀錄一起帶上**（照
    `inbox_builder.build_claim_item`，不重寫簽章邏輯）。

    預留（reservation）放在認領單裡而不是另外送一個 session 項目，這樣被拒時
    Agora 裡不會留下任何東西。
    """
    from aistorage.inbox_builder import NewSessionReservation, build_claim_item
    from aistorage.clock import parse_rfc3339

    assert resolved.handoff_id is not None
    _source, _sep, native = record.new_session_id.partition(":")
    raw_path = _reservation_path(stage, record.item_key)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(_empty_export_bytes(
        native, record.title,
        int(parse_rfc3339(record.reserved_at).timestamp() * 1000)))
    return build_claim_item(
        handoff_id=resolved.handoff_id,
        claimer_session_id=record.new_session_id,
        profile=deps.signer.profile,
        key=deps.signer.key,
        key_id=deps.signer.key_id,
        now=record.created_at,
        item_key=record.item_key,
        new_session=NewSessionReservation(
            session_id=record.new_session_id,
            raw_path=raw_path,
            snapshot_at=record.reserved_at,
        ),
    )


def _with_claim(segment: PackageSegment, claim_id: str) -> PackageSegment:
    return PackageSegment(
        resolved=segment.resolved, raw=segment.raw,
        message_count=segment.message_count, text_chars=segment.text_chars,
        claim_id=claim_id, order=segment.order,
    )


def checkout(
    reader: Any,
    deps: CheckoutDeps,
    startpoint_texts: Sequence[str],
    out_dir: Path,
    *,
    task: str | None = None,
    new_session_id: str | None = None,
    source: str = "opencode",
    max_lag: timedelta | None = None,
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    claim_timeout: timedelta = DEFAULT_CLAIM_TIMEOUT,
    stage_dir: Path | None = None,
    resume: bool = False,
) -> ContextPackage:
    """產出一個起點包；回傳組好的內容（已寫到 `out_dir`）。

    呼叫端要自己讀 `out_dir / "package.json"` 拿 `new_session.session_id`
    交給轉接器——這裡的回傳只是為了測試與人看的摘要。

    `resume=True` 時沿用本機記錄的認領（同一個 claim id 與新 session id）。
    """
    if not startpoint_texts:
        raise StartPointError("至少要給一個起點")
    if deps.objects is None:
        raise CheckoutError(
            "沒有設定 Agora 物件資料夾（讀取端設定的 agora_folder_id），"
            "所以取不到原始紀錄、無法產出起點包。"
            "讀取身分需要對那個資料夾有唯讀權限（見 docs/runbooks/deploy.md 的分享步驟）。"
        )

    resolved: list[ResolvedStartPoint] = []
    for text in startpoint_texts:
        resolved.append(resolve_startpoint(
            reader, parse_startpoint(text, source=source), max_lag=max_lag))

    # 讀取介面只給 key，位元組自己去 Agora 的物件資料夾取並驗證（見 objects.py）
    stage = Path(stage_dir) if stage_dir is not None else _make_stage_dir()
    stage.mkdir(parents=True, exist_ok=True)
    staging: Path | None = None
    try:
        segments: list[PackageSegment] = []
        for index, r in enumerate(resolved):
            raw = _fetch_raw(reader, r, deps.objects)
            count, chars = _measure(reader, r, raw, max_lag=max_lag)
            segments.append(PackageSegment(
                resolved=r, raw=raw, message_count=count, text_chars=chars,
                order=index,
            ))

        # n→1：最長的一段放最前面（ADR 0010）。長度超過就明確拒絕、什麼都不寫。
        ordered = order_segments(segments)
        target_session_id = new_session_id or _new_session_id(
            ordered[0].source or source)
        pkg = ContextPackage(
            segments=tuple(ordered),
            task=task,
            new_session_id=target_session_id,
            created_at=deps.clock.now_utc(),
            created_by=f"profile:{_profile_of(deps)}",
            max_context_chars=max_context_chars,
        )
        check_context_length(pkg)

        # ★ 所有本機檢查都在認領之前（review-73dbf2c H1）：輸出目錄、位元組、
        #   雜湊都在這裡做完，之後才碰得到那張交接單。
        staging = stage_package(pkg, Path(out_dir))

        if any(s.resolved.handoff_id for s in ordered):
            ordered = _claim(deps, ordered, pkg, stage, timeout=claim_timeout,
                             resume=resume)
            pkg = ContextPackage(
                segments=tuple(ordered), task=pkg.task,
                new_session_id=pkg.new_session_id,
                claimed_handoffs=tuple(
                    s.resolved.handoff_id for s in ordered
                    if s.resolved.handoff_id),
                created_at=pkg.created_at, created_by=pkg.created_by,
                max_context_chars=pkg.max_context_chars,
            )
    except BaseException:
        # 被拒或失敗：暫存目錄與認領記錄以外的東西都不留在磁碟上
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        # 暫存目錄只是組項目用的（預留的匯出檔），產出成功與否都要清掉
        shutil.rmtree(stage, ignore_errors=True)

    assert staging is not None
    commit_staged_package(pkg, staging, Path(out_dir))
    return pkg


def _make_stage_dir() -> Path:
    """checkout 的暫存目錄（放預留新 session 的空匯出檔）。

    放在**系統暫存目錄**而不是 `out_dir` 的旁邊：起點包可能產在共用目錄（多個
    worker 同時 checkout），固定名字會互相蓋掉。
    """
    return Path(tempfile.mkdtemp(prefix="agora-checkout-"))


def _profile_of(deps: CheckoutDeps) -> str:
    """`package.json` 的 `created_by`＝建立者的 profile（不是自由文字）。

    沒有簽章金鑰就沒有 profile，起點包就不能說明自己是誰做的——明確拒絕。
    """
    signer = deps.signer
    profile = getattr(signer, "profile", None) if signer is not None else None
    if not profile:
        raise CheckoutError(
            "這台機器沒有可用的寫入身分（沒有簽章金鑰），"
            "所以無法產出起點包：package.json 要記下建立者的 profile。"
            "請設定 worker 的簽章金鑰（`SyncerConfig.signer`）後再試。"
        )
    return str(profile)


def _fetch_raw(reader: Any, r: ResolvedStartPoint, objects: Any) -> bytes:
    """取**原始紀錄本體**：讀取介面給 key，位元組自己去 Agora 物件資料夾取。

    讀取視圖不發佈 raw（那等於把真本的位元組複製一份到衍生物裡），所以這裡
    走「位址 → 位元組」：先問讀取介面要 annex key（內容定址的位址），再依 key
    去唯讀分享的物件資料夾取，取回後由 `ObjectFetcher` 用 key 內嵌的 sha256
    驗證。取不到就明確拒絕——不要用閱讀版頂替（那樣開頭就不會位元組相同）。
    """
    try:
        view = reader.get_snapshot(r.session_id, r.snapshot_sha256)
    except KeyError as e:
        raise CheckoutError(
            f"讀取介面沒有這個快照: {r.session_id}@{r.snapshot_sha256[:12]}（{e}）。"
            "請等下一輪提交流程發佈後再試。"
        ) from e
    ref = getattr(getattr(view, "value", view), "annex_key", None)
    if not ref:
        raise CheckoutError(
            f"讀取介面沒有這個快照的 annex key: "
            f"{r.session_id}@{r.snapshot_sha256[:12]}。"
            "原始紀錄是重建位元組相同的開頭的必要條件（閱讀版還原不了，"
            "讀取視圖也不發佈它的位元組），所以這裡明確拒絕。"
        )
    try:
        return objects.fetch(str(ref))
    except ObjectError as e:
        raise CheckoutError(str(e)) from e


def _claim(deps: CheckoutDeps, ordered: list[PackageSegment], pkg: ContextPackage,
           stage: Path, *, timeout: timedelta,
           resume: bool) -> list[PackageSegment]:
    """放認領（**連同新 session 的空紀錄**），等讀取介面確認。

    `--resume` 時沿用本機記錄：同一個 claim id、同一個新 session id、同一份
    空匯出檔位元組，所以提交流程把它當成同一筆（`already`）而不是新認領。
    """
    if deps.signer is None or not deps.inbox_folder_id:
        raise CheckoutError(
            "起點是交接單，但這台機器沒有可用的寫入身分（沒有簽章金鑰／收件匣），"
            "不能放認領。要接手就得起點用 <session>[@<訊息>]，"
            "或讓有寫入身分的 profile 來跑這一次 checkout。"
        )
    if deps.commit_claim is None:
        raise CheckoutError(
            "起點是交接單，但這台機器沒有可用的寫入身分（沒有簽章金鑰／收件匣），"
            "不能放認領。要接手就得起點用 <session>[@<訊息>]，"
            "或讓有寫入身分的 profile 來跑這一次 checkout。"
        )

    claims: list[Any] = []
    claimed: list[PackageSegment] = []
    for segment in ordered:
        if not segment.resolved.handoff_id:
            claimed.append(segment)
            continue
        record = _claim_record(deps, segment.resolved, pkg, resume=resume)
        item = _build_claim(deps, segment.resolved, record=record, stage=stage)
        claims.append(item)
        claimed.append(_with_claim(segment, item.item_id))
    _commit_claims(deps, claims, timeout=timeout)
    return claimed


def _claim_record(deps: CheckoutDeps, resolved: ResolvedStartPoint,
                  pkg: ContextPackage, *, resume: bool) -> ClaimRecord:
    """這張交接單的認領記錄：`--resume` 沿用舊的，否則新編。

    **寫進本機記錄是在把項目送出去之前**（`commit_claim` 之後就來不及了）：
    行程在中間被殺掉時才救得回來。
    """
    from aistorage.schema import generate_ulid

    handoff_id = str(resolved.handoff_id)
    journal = deps.journal
    if resume and journal is not None:
        existing = journal.get(handoff_id)
        if existing is not None:
            if existing.new_session_id != pkg.new_session_id:
                raise CheckoutError(
                    f"本機記錄的認領（{handoff_id}）預留的是 "
                    f"{existing.new_session_id}，但這次要的是 {pkg.new_session_id}；"
                    "換 id 重來只會得到 already_claimed，請用同一個 "
                    "--new-session-id 重跑，或先清掉本機認領記錄"
                )
            return existing
        raise CheckoutError(
            f"--resume 找不到 {handoff_id} 的本機認領記錄"
            "（認領記錄在 ~/.aistorage/checkout-claims.json）。"
            "沒有記錄就沒有辦法沿用同一個認領：請去掉 --resume 重新開始，"
            "或確認那張交接單還沒被別人接走"
        )

    now = deps.clock.now_utc()
    item_key = generate_ulid()
    record = ClaimRecord(
        handoff_id=handoff_id,
        claim_id=f"claim:{item_key}",
        item_key=item_key,
        new_session_id=pkg.new_session_id,
        reserved_at=now,
        title=_reserved_title(pkg.task, handoff_id),
        profile=str(deps.signer.profile),
        created_at=now,
    )
    if journal is not None:
        journal.put(record)
    return record


def _commit_claims(deps: CheckoutDeps, claims: list[Any], *,
                   timeout: timedelta) -> None:
    """把認領提交並等確認；被拒就明確拒絕。

    認領裡帶著新 session 的**空**紀錄，所以 `apply_claim` 不需要另外收到一個
    session 項目：被接受時接續 Link 與那筆空紀錄一起進 Agora，被拒時什麼都沒寫。

    等的是**認領**（讀取介面看到接續 Link 屬於新 session），不是新 session 本身
    ——它這時還沒有後續快照，用 `raw_sha256` 判可見會永遠等不到。
    """
    result = deps.commit_claim(claims, timeout=timeout)
    rejected = tuple(getattr(result, "rejected", ()) or ())
    if rejected:
        details = "、".join(f"{_label(a)} {code}" for a, code in rejected)
        _forget_claims(deps, claims)
        raise ClaimRejected(
            f"認領被拒收，所以沒有產出起點包：{details}。"
            "被拒通常要人處理（例如交接單已被別人接走、快照已過期）；"
            "不要重試同一批，請把原因回報給使用者。"
        )
    if getattr(result, "timed_out", False):
        # 逾時不等於被拒：認領可能下一輪就被收進去了，所以**留著記錄**，
        # 讓 `--resume` 沿用同一個 claim id 重試（否則就會 already_claimed）。
        raise ClaimRejected(
            "認領還沒被讀取介面確認，所以沒有產出起點包："
            f"{getattr(result, 'summary', lambda: '')()}。"
            "先確認提交流程正常（健康檢查）再重跑一次；"
            "重跑請加 --resume 沿用同一個認領（不要換一個新的）"
        )


def _forget_claims(deps: CheckoutDeps, claims: Sequence[Any]) -> None:
    """認領被明確拒收時刪掉本機記錄——那張單已經不是這次 checkout 的了。"""
    journal = deps.journal
    if journal is None:
        return
    for item in claims:
        handoff_id = (item.sidecar.get("body") or {}).get("handoff_id")
        if isinstance(handoff_id, str):
            try:
                journal.forget(handoff_id)
            except Exception:  # noqa: BLE001 - 記錄是衍生狀態，不遮蔽真正的拒絕原因
                pass


def _label(awaited: Any) -> str:
    kind = getattr(awaited, "kind", "?")
    target = getattr(awaited, "target", "?")
    return f"{kind}:{target}"


__all__ = [
    "CheckoutDeps",
    "CheckoutError",
    "ClaimRejected",
    "ContextLimitExceeded",
    "ContextPackageError",
    "DEFAULT_CLAIM_TIMEOUT",
    "ObjectError",
    "checkout",
]
