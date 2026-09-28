"""`agora checkout`：把起點變成一個起點包（docs/design/agora-session-operations.md）。

流程（設計文件第 1、2、3 點）：
1. 從讀取介面取得起點與**被釘住的快照**，把原始紀錄**原封不動**放進起點包；
2. 起點是交接單時，放一筆認領進收件匣，等讀取介面確認，**被拒就不產出**；
3. n→1 時最長的一段放最前面，並檢查總長度沒有超過上限，**超過就明確拒絕**。

**這個模組完全不碰任何 coding agent**：原始紀錄是來源應用的匯出檔，起點包
原封不動地放著它。截斷、重編 id、匯入是轉接器（`agora-opencode load`）的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
from typing import Any, Sequence

from aistorage.agora_cli.objects import ObjectError
from aistorage.agora_cli.package import (
    DEFAULT_MAX_CONTEXT_CHARS,
    ContextLimitExceeded,
    ContextPackage,
    ContextPackageError,
    PackageSegment,
    check_context_length,
    order_segments,
    write_package,
)
from aistorage.agora_cli.startpoint import (
    ResolvedStartPoint,
    StartPointError,
    parse_startpoint,
    resolve_startpoint,
)

#: 認領放進收件匣之後，等讀取介面確認的時間。逾時就明確拒絕（不產出）。
DEFAULT_CLAIM_TIMEOUT = timedelta(minutes=15)


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


def _build_claim(deps: CheckoutDeps, resolved: ResolvedStartPoint, *,
                 claimer_session_id: str) -> Any:
    """組一筆認領（照 `inbox_builder.build_claim_item`，不重寫簽章邏輯）。"""
    from aistorage.inbox_builder import build_claim_item

    assert resolved.handoff_id is not None
    return build_claim_item(
        handoff_id=resolved.handoff_id,
        claimer_session_id=claimer_session_id,
        profile=deps.signer.profile,
        key=deps.signer.key,
        key_id=deps.signer.key_id,
        now=deps.clock.now_utc(),
    )


def _seed_session_item(deps: CheckoutDeps, session_id: str, raw: bytes, *,
                       stage: Path) -> Any:
    """組新 session 的第一份快照（認領者必須已經在 Agora 裡，見 `apply_claim`）。

    提交流程的 `apply_claim` 要求「認領者 Session 已在 Agora（含本輪剛收）」，
    而且 `producer` 要與認領一致。所以這裡上傳新 session 的空匯出檔，**與
    認領同一批**提交（`apply` 的順序是 session 在前）。

    本體用**最長那一段截斷後的原始紀錄**——它就是新 session 接下來的內容，
    所以兩邊的位元組相同（`agora-opencode load` 會再截一次、重編 id，結果
    一致）。用它當第一份快照而不是一個空殼，Agora 裡就立刻有可讀的內容。
    """
    from aistorage.converters import get_converter
    from aistorage.converters.base import ConversionError
    from aistorage.inbox_builder import build_session_item

    source, _sep, native = session_id.partition(":")
    raw_path = stage / "seed.json"
    raw_path.write_bytes(raw)
    converter = get_converter(source)
    try:
        facts = converter.facts(raw_path, session_id=session_id)
    except ConversionError as e:
        raise CheckoutError(
            f"新 session 的第一份快照組不起來（{source} 轉換器讀不了這份匯出檔：{e}）；"
            "沒有它認領就會被收成 unknown_claimer，所以不要繼續"
        ) from e
    return build_session_item(
        raw_path,
        source=source,
        source_session_id=native,
        facts=converter.facts(raw_path, session_id=session_id),
        profile=deps.signer.profile,
        key=deps.signer.key,
        key_id=deps.signer.key_id,
        status="running",
        provenance="agora checkout 的新 session 第一份快照",
        now=deps.clock.now_utc(),
    )


def _commit_claims(deps: CheckoutDeps, seed: Any, claims: Sequence[Any], *,
                   timeout: timedelta) -> None:
    """把新 session 的第一份快照與認領一起提交並等確認；被拒就明確拒絕。

    **新 session 的快照一定要與認領同一批**：`apply_claim` 要求「認領者 Session
    已在 Agora（含本輪剛收）」，而新 session 這時還不存在於任何來源應用裡。
    上傳順序是 snapshot → claim（`upload_item` 的不可分單位順序）。

    等的是**認領**（讀取介面看到接續 Link 屬於新 session），不是新 session 本身
    ——它這時還沒有後續快照，用 `raw_sha256` 判可見會永遠等不到。
    """
    if deps.commit_claim is None:
        raise CheckoutError(
            "起點是交接單，但這台機器沒有可用的寫入身分（沒有簽章金鑰／收件匣），"
            "不能放認領。要接手就得起點用 <session>[@<訊息>]，"
            "或讓有寫入身分的 profile 來跑這一次 checkout。"
        )
    result = deps.commit_claim(seed, list(claims), timeout=timeout)
    rejected = tuple(getattr(result, "rejected", ()) or ())
    if rejected:
        details = "、".join(f"{_label(a)} {code}" for a, code in rejected)
        raise ClaimRejected(
            f"認領被拒收，所以沒有產出起點包：{details}。"
            "被拒通常要人處理（例如交接單已被別人接走、快照已過期）；"
            "不要重試同一批，請把原因回報給使用者。"
        )
    if getattr(result, "timed_out", False):
        raise ClaimRejected(
            "認領還沒被讀取介面確認，所以沒有產出起點包："
            f"{getattr(result, 'summary', lambda: '')()}。"
            "先確認提交流程正常（健康檢查）再重跑一次。"
        )


def _label(awaited: Any) -> str:
    kind = getattr(awaited, "kind", "?")
    target = getattr(awaited, "target", "?")
    return f"{kind}:{target}"


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
) -> ContextPackage:
    """產出一個起點包；回傳組好的內容（`write_package` 已寫到 `out_dir`）。

    呼叫端要自己讀 `out_dir / "package.json"` 拿 `new_session.session_id`
    交給轉接器——這裡的回傳只是為了測試與人看的摘要。
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

    # 讀取介面只給 key，位元組自己去 Agora 的物件資料夾取並驗證（見 objects.py）。
    stage = Path(stage_dir) if stage_dir is not None else _make_stage_dir()
    stage.mkdir(parents=True, exist_ok=True)
    segments: list[PackageSegment] = []
    try:
        for index, r in enumerate(resolved):
            raw = _fetch_raw(reader, r, deps.objects)
            count, chars = _measure(reader, r, raw, max_lag=max_lag)
            segments.append(PackageSegment(
                resolved=r, raw=raw, message_count=count, text_chars=chars,
                order=index,
            ))

        # n→1：最長的一段放最前面（ADR 0010）。長度超過就明確拒絕、什麼都不寫。
        ordered = order_segments(segments)
        target_session_id = new_session_id or _new_session_id(ordered[0].source or source)
        pkg = ContextPackage(
            segments=tuple(ordered),
            task=task,
            new_session_id=target_session_id,
            created_at=deps.clock.now_utc(),
            created_by=f"profile:{_profile_of(deps)}",
            max_context_chars=max_context_chars,
        )
        check_context_length(pkg)

        # 交接單起點：放認領並等確認；被拒就不產出。
        if any(s.resolved.handoff_id for s in ordered):
            ordered = _claim(deps, ordered, pkg, stage, timeout=claim_timeout)
            pkg = ContextPackage(
                segments=tuple(ordered), task=pkg.task,
                new_session_id=pkg.new_session_id,
                claimed_handoffs=tuple(
                    s.resolved.handoff_id for s in ordered
                    if s.resolved.handoff_id),
                created_at=pkg.created_at, created_by=pkg.created_by,
                max_context_chars=pkg.max_context_chars,
            )
    finally:
        # 暫存目錄只是組項目用的（seed 匯出檔），產出成功與否都要清掉
        shutil.rmtree(stage, ignore_errors=True)

    write_package(pkg, Path(out_dir))
    return pkg


def _make_stage_dir() -> Path:
    """checkout 的暫存目錄（放組認領用的匯出檔）。

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
           stage: Path, *, timeout: timedelta) -> list[PackageSegment]:
    """放認領（新 session 的第一份快照 ＋ 每張交接單一筆），等讀取介面確認。"""
    if deps.signer is None or not deps.inbox_folder_id:
        raise CheckoutError(
            "起點是交接單，但這台機器沒有可用的寫入身分（沒有簽章金鑰／收件匣），"
            "不能放認領。"
        )
    longest = max(ordered, key=lambda s: (s.text_chars, -s.order))
    seed = _seed_session_item(deps, pkg.new_session_id, longest.raw, stage=stage)
    claims: list[Any] = []
    claimed: list[PackageSegment] = []
    for segment in ordered:
        if not segment.resolved.handoff_id:
            claimed.append(segment)
            continue
        item = _build_claim(deps, segment.resolved,
                            claimer_session_id=pkg.new_session_id)
        claims.append(item)
        claimed.append(_with_claim(segment, item.item_id))
    _commit_claims(deps, seed, claims, timeout=timeout)
    return claimed


def _with_claim(segment: PackageSegment, claim_id: str) -> PackageSegment:
    return PackageSegment(
        resolved=segment.resolved, raw=segment.raw,
        message_count=segment.message_count, text_chars=segment.text_chars,
        claim_id=claim_id, order=segment.order,
    )


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
