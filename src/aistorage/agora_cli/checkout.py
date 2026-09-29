"""`agora checkout`：把起點變成一個起點包（docs/design/agora-session-operations.md）。

流程（設計文件第 1、2、3 點），**順序本身就是安全的一部分**：

1. 從讀取介面取得起點與**被釘住的快照**，把原始紀錄**原封不動**放進起點包；
2. **所有本機檢查都在登記接續之前完成**（review-73dbf2c H1）：輸出目錄可寫而且是
   空的、每段 raw 的 SHA-256 等於快照雜湊、Agora 物件讀得到。任一項失敗就停下，
   這個世界裡還沒有任何接續記錄；
3. **每一個起點都要留下一條接續 Link**（impl2 M6）。起點是交接單時放**一筆認領**
   （claim 本身就代表接續），起點是 `<session>[@<訊息>]` 時放**一筆接續單**；
   兩者都自己**預留**那個空的新 session，都等讀取介面確認，**被拒就不產出**；
4. 確認之後才把 `package.json` 寫進暫存目錄並一次改名。

**Session 之間只有四種關係，一律記錄**：1→1、1→n、n→1 各是新 session 對每個
起點一條接續 Link；n↔m 是 `agora read` 記的參考 Link。**接續不需要交接單**——
交接單只是可選的便利（持有者事先寫好任務，而且只能被接一次）。

被接受時，等候確認的同時新 session 已經是一筆空紀錄（`apply_claim`／
`apply_continuation` 從項目裡的預留生出來）。被拒時**什麼都沒寫進 Agora**：沒有
「已被接走卻沒有人接手」的交接單，也沒有留在 Agora 裡的假快照（review-73dbf2c H2）。

**重跑要沿用同一組預留**（`--resume`，見 `agora_cli/claims.py`）：交接單只能被
認領一次，換一個新的 claim id 重來只會得到 `already_claimed`；直接起點雖然沒有
「只能被接一次」的問題，重跑卻會多留一筆沒有人開工的預留 session。

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

from aistorage.agora_cli.claims import ClaimJournal, ClaimRecord, startpoint_key
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
    """接續記錄（認領或接續單）被拒收（或等不到確認）——**不產出起點包**。

    照 AGENTS.md 的「認領」：接續 Link 屬於自己之後才開工。被拒就停下來，
    不要重試同一批（被拒的原因通常要人處理，例如交接單已被別人接走）。
    """


class PartialClaimAccepted(ClaimRejected):
    """n→1 時**部分**認領被接受、其餘被拒（review-7a4ca87 H2）。

    提交流程對每一筆認領分別套用，所以「兩張交接單、其中一張已被別人接走」會
    落在這裡：被接受的那張**已經**在 Agora 裡留下一筆預留的 session 與接續 Link，
    撤不回來。這時最該做的是把「已被接受的有哪些、預留的 session 是哪個」講清楚，
    並且**不把它們的記錄刪掉**——那個預留日後還要被認得出來。
    """

    def __init__(self, message: str, *, accepted: tuple[str, ...],
                 rejected: tuple[str, ...], new_session_id: str) -> None:
        super().__init__(message)
        self.accepted = accepted
        self.rejected = rejected
        self.new_session_id = new_session_id


class ClaimAlreadyCommitted(ClaimRejected):
    """認領**已經被接受**，但產出起點包的最後一步失敗了（review-7a4ca87 M2）。

    這一步在 `commit_staged_package`（寫 `package.json` 並改名）。到這裡為止
    交接單已經被接走、Agora 裡已經有一筆預留的 session——所以這**不是**可以
    「修好再重跑」的錯誤：重跑會撞 `already_claimed`。訊息要明確說出「認領已經
    成立」與那個預留的 session id。
    """

    def __init__(self, message: str, *, claimed_handoffs: tuple[str, ...],
                 new_session_id: str) -> None:
        super().__init__(message)
        self.claimed_handoffs = claimed_handoffs
        self.new_session_id = new_session_id


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


def _recorded_session_id(deps: CheckoutDeps,
                         ordered: Sequence[PackageSegment]) -> str | None:
    """本機記錄裡，這批起點已經預留過的 session id；沒有就 None。

    **為什麼要在編 id 之前查**（review-7a4ca87 H1）：預設流程每次都重新編一個新
    session id，所以 `--resume` 拿它去對記錄一定對不上——住民 AI 沒有
    `--new-session-id` 這個參數可傳，重跑路徑等於走不通。改成「記錄有就沿用」，
    重跑就不需要任何額外參數。

    鍵是**起點鍵**（`claims.startpoint_key`）：交接單起點是交接單 id，直接起點是
    `startpoint:<session>@<訊息>`——兩種起點都要認得出來。

    n→1 時多個起點各自有記錄，它們**必須是同一個** session id（那才是同一個
    新 session）。不一致代表這批之中有一個起點已經被別的 checkout 預留走了，明確
    拒絕，不要猜。
    """
    journal = deps.journal
    if journal is None:
        return None
    found: set[str] = set()
    for segment in ordered:
        record = journal.get(_journal_key(segment.resolved))
        if record is not None:
            found.add(record.new_session_id)
    if not found:
        return None
    if len(found) > 1:
        joined = "、".join(sorted(found))
        raise CheckoutError(
            f"這批起點的本機認領記錄預留了不同的新 session id: {joined}。"
            "它們不可能是同一個新 session（每一批預留各自一個 id），"
            "所以其中某一個起點已經被另一個 checkout 預留走了。"
            "請把這批拆成與記錄相符的幾組再各自 checkout，"
            "或清掉 ~/.aistorage/checkout-claims/ 裡過期的記錄後重新開始"
        )
    return found.pop()


def _journal_key(resolved: ResolvedStartPoint) -> str:
    """這個起點在本機認領記錄裡的鍵（交接單 id 或 `startpoint:<session>@<訊息>`）。"""
    return startpoint_key(
        resolved.handoff_id, session_id=resolved.session_id,
        message_id=resolved.message_id)


def _reject_duplicate_sources(resolved: Sequence[ResolvedStartPoint]) -> None:
    """同一批起點裡同一個**被接續 session** 只准出現一次（review-2bc0785 M1）。

    這是提交流程那條規則（同一個新 session 對同一個 to 只能一條接續 Link）的本機
    版：交接單起點的「來源」是那張單的目標 session，直接起點的來源就是它自己。
    兩者混用也算同一個來源——`handoff:H` 與 `S1@某訊息` 若 H 的目標就是 S1，那是
    同一個 session 被接兩次。

    在**登記任何接續記錄之前**就拒絕，所以被擋下時 Agora 裡不會有半套東西。
    """
    seen: dict[str, str] = {}
    for r in resolved:
        label = r.handoff_id or f"{r.session_id}@{r.message_id}"
        first = seen.get(r.session_id)
        if first is not None:
            raise CheckoutError(
                f"這批起點裡 {r.session_id} 出現兩次（{first} 與 {label}）。"
                "同一個新 session 對同一個被接續 session 只能有一條接續 Link，"
                "所以同一個來源只能接一次——要分岔就各跑一次 checkout（1→n），"
                "要收攏就接**不同**的來源（n→1）。"
            )
        seen[r.session_id] = label


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


def _reservation(deps: CheckoutDeps, record: ClaimRecord, stage: Path) -> Any:
    """新 session 的空紀錄（預留）——認領與接續單**都**帶著它。

    預留放在記錄裡而不是另外送一個 session 項目，這樣被拒時 Agora 裡不會留下
    任何東西。
    """
    from aistorage.inbox_builder import NewSessionReservation
    from aistorage.clock import parse_rfc3339

    _source, _sep, native = record.new_session_id.partition(":")
    raw_path = _reservation_path(stage, record.item_key)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(_empty_export_bytes(
        native, record.title,
        int(parse_rfc3339(record.reserved_at).timestamp() * 1000)))
    return NewSessionReservation(
        session_id=record.new_session_id,
        raw_path=raw_path,
        snapshot_at=record.reserved_at,
    )


def _build_claim(deps: CheckoutDeps, resolved: ResolvedStartPoint, *,
                 record: ClaimRecord, stage: Path) -> Any:
    """組一筆認領（交接單起點），連同新 session 的空紀錄。"""
    from aistorage.inbox_builder import build_claim_item

    assert resolved.handoff_id is not None
    return build_claim_item(
        handoff_id=resolved.handoff_id,
        claimer_session_id=record.new_session_id,
        profile=deps.signer.profile,
        key=deps.signer.key,
        key_id=deps.signer.key_id,
        now=record.created_at,
        item_key=record.item_key,
        new_session=_reservation(deps, record, stage),
    )


def _build_continuation(deps: CheckoutDeps, resolved: ResolvedStartPoint, *,
                        record: ClaimRecord, stage: Path) -> Any:
    """組一筆接續單（`<session>[@<訊息>]` 起點），連同新 session 的空紀錄。

    接續點是**讀取介面釘住的那個位置**：快照雜湊 ＋ 該快照裡的那一則訊息。提交流程
    會重新確認它落在被接續 Session 的既有快照裡、那一則已完成且未撤銷。
    """
    from aistorage.inbox_builder import build_continuation_item

    if not resolved.message_id:
        raise CheckoutError(
            f"起點 {resolved.session_id} 沒有解析出接續點的訊息 id，"
            "不能建立接續記錄"
        )
    return build_continuation_item(
        target_session_id=resolved.session_id,
        new_session_id=record.new_session_id,
        continuation={
            "snapshot_sha256": resolved.snapshot_sha256,
            "message_id": resolved.message_id,
        },
        profile=deps.signer.profile,
        key=deps.signer.key,
        key_id=deps.signer.key_id,
        now=record.created_at,
        item_key=record.item_key,
        new_session=_reservation(deps, record, stage),
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

    **同一批起點不能有兩個指向同一個 session 的來源**（`_reject_duplicate_sources`）：
    那在提交流程那邊只會留下一條 Link，而且留下哪一條取決於順序。在本機就拒絕，
    錯誤訊息才說得清楚。
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

    # ★ 同一個被接續 session 不能出現兩次（review-2bc0785 M1／Night decisions）。
    #   提交流程那邊是「一個新 session 對同一個被接續 session 只能一條 Link」，所以
    #   這組合本來就會被拒（`duplicate_link`），而且**順序**會決定哪一筆留下來。
    #   在本機就擋掉，錯誤訊息才說得清楚「同一個來源只能接一次」——兩次接同一個
    #   session 的語意本來就是 1→1 或 1→n，不是 n→1。
    _reject_duplicate_sources(resolved)

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
        # 交接單起點時，任務文字要**放進起點包**（M3）。`--task` 是額外附加的，
        # 兩者都要：交接單的內容是接手者必須知道的，`--task` 是這一次額外交代的。
        if task is None:
            for segment in ordered:
                if segment.resolved.task:
                    task = segment.resolved.task
                    break
        # ★ 新 session id 要**先**看本機記錄（review-7a4ca87 H1）。記錄裡已經有
        #   這個起點就表示那筆接續記錄可能已經被接受，此時重新編一個 id 會讓這次
        #   認領得到 already_claimed，而那張單永遠沒有 session 接手（直接起點則會
        #   多留一筆沒有人開工的預留 session）。
        #   沒有記錄才編新的。
        target_session_id = (
            new_session_id
            or _recorded_session_id(deps, ordered)
            or _new_session_id(ordered[0].source or source)
        )
        pkg = ContextPackage(
            segments=tuple(ordered),
            task=task,
            new_session_id=target_session_id,
            created_at=deps.clock.now_utc(),
            created_by=f"profile:{_profile_of(deps)}",
            max_context_chars=max_context_chars,
        )
        check_context_length(pkg)

        # ★ 所有本機檢查都在寫接續記錄之前（review-73dbf2c H1）：輸出目錄、位元組、
        #   雜湊都在這裡做完，之後才碰得到那些起點。
        staging = stage_package(pkg, Path(out_dir))

        handoff_ids = tuple(
            s.resolved.handoff_id for s in ordered if s.resolved.handoff_id)
        # ★ 每一個起點都要有接續記錄：交接單 → 認領，直接起點 → 接續單。
        ordered = _link_items(deps, ordered, pkg, stage, timeout=claim_timeout,
                              resume=resume)
        pkg = ContextPackage(
            segments=tuple(ordered), task=pkg.task,
            new_session_id=pkg.new_session_id,
            claimed_handoffs=handoff_ids,
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
    try:
        commit_staged_package(pkg, staging, Path(out_dir))
    except ContextPackageError as e:
        # ★ 接續記錄確認**之後**才發生的失敗（review-7a4ca87 M2）：到這一步為止，
        #   交接單已經被接走、Agora 裡已經有一筆預留的 session 了。這不是一般錯誤——
        #   把它講成一般錯誤，人會去「修好再重跑」，而重跑會撞 already_claimed。
        #   必須明確說明：認領已經成立、不要再送新的認領、要沿用同一個。
        raise ClaimAlreadyCommitted(
            f"認領**已經成立**，但起點包沒有產出來：{e}\n"
            f"  已經被接走的交接單：{'、'.join(pkg.claimed_handoffs) or '（無）'}\n"
            f"  預留給新 session 的 id：{pkg.new_session_id}"
            "（Agora 裡已經有一筆空的預留 session 與接續 Link）\n"
            "  **不要再送一次認領**（會得到 already_claimed）。"
            "把本訊息原樣回報使用者，由他決定怎麼處置那個預留；"
            "本機認領記錄留著，可用它重跑這一次 checkout。",
            claimed_handoffs=tuple(pkg.claimed_handoffs),
            new_session_id=pkg.new_session_id,
        ) from e
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


def _link_items(deps: CheckoutDeps, ordered: list[PackageSegment],
                pkg: ContextPackage, stage: Path, *, timeout: timedelta,
                resume: bool) -> list[PackageSegment]:
    """為**每個**起點放一筆接續記錄（連同新 session 的空紀錄），等讀取介面確認。

    - 起點是交接單 → 認領（claim 本身就代表接續，不再另外送接續單）
    - 起點是 `<session>[@<訊息>]` → 接續單

    所以 1→1、1→n、n→1 都會在 Agora 裡留下對應的接續 Link；n↔m 是 `agora read`
    記的參考 Link。

    `--resume` 時沿用本機記錄：同一個項目 id、同一個新 session id、同一份
    空匯出檔位元組，所以提交流程把它當成同一筆（`already`）而不是新的一筆。
    """
    if deps.signer is None or not deps.inbox_folder_id:
        raise CheckoutError(
            "這台機器沒有可用的寫入身分（沒有簽章金鑰／收件匣），"
            "不能為這個起點留下接續記錄——沒有接續 Link 的新 session 等於"
            "沒有人負責的孤兒，Agora 不能接受。"
            "請讓有寫入身分的 profile 來跑這一次 checkout。"
        )
    if deps.commit_claim is None:
        raise CheckoutError(
            "這台機器沒有可用的寫入身分（沒有簽章金鑰／收件匣），"
            "不能為這個起點留下接續記錄——沒有接續 Link 的新 session 等於"
            "沒有人負責的孤兒，Agora 不能接受。"
            "請讓有寫入身分的 profile 來跑這一次 checkout。"
        )

    items: list[Any] = []
    linked: list[PackageSegment] = []
    for segment in ordered:
        record = _link_record(deps, segment.resolved, pkg, resume=resume)
        if segment.resolved.handoff_id:
            item = _build_claim(deps, segment.resolved, record=record, stage=stage)
        else:
            item = _build_continuation(
                deps, segment.resolved, record=record, stage=stage)
        items.append(item)
        linked.append(_with_claim(segment, item.item_id))
    _commit_claims(deps, items, timeout=timeout)
    return linked


def _link_record(deps: CheckoutDeps, resolved: ResolvedStartPoint,
                 pkg: ContextPackage, *, resume: bool) -> ClaimRecord:
    """這個起點的本機預留記錄：`--resume` 沿用舊的，否則新編。

    鍵是**起點鍵**（交接單 id 或 `startpoint:<session>@<訊息>`），所以 1→n 的
    同一個起點checkout n 次時，n 個預留各自一個檔案（不共用一個 id）。

    **寫進本機記錄是在把項目送出去之前**（`commit_claim` 之後就來不及了）：
    行程在中間被殺掉時才救得回來。
    """
    from aistorage.schema import generate_ulid

    key = _journal_key(resolved)
    journal = deps.journal
    existing = journal.get(key) if journal is not None else None

    if existing is not None:
        # ★ 有記錄就**自動沿用**，不管有沒有 --resume（review-7a4ca87 H1）。
        #   記錄存在的唯一理由是「那筆接續記錄可能已經被接受」；換一個新的 id
        #   重來，交接單起點會得到 already_claimed 而那張單永遠沒有 session
        #   接手，直接起點則會多留一筆沒有人開工的預留 session。
        #   `pkg.new_session_id` 必須已經是記錄裡那個（`checkout()` 會先對齊）。
        return existing
    if resume and journal is not None:
        raise CheckoutError(
            f"--resume 找不到 {key} 的本機認領記錄"
            "（認領記錄在 ~/.aistorage/checkout-claims/）。"
            "沒有記錄就沒有辦法沿用同一組預留：請去掉 --resume 重新開始，"
            "或確認那個起點還沒被別的 checkout 預留走"
        )

    now = deps.clock.now_utc()
    item_key = generate_ulid()
    item_id = (
        f"claim:{item_key}" if resolved.handoff_id else f"continuation:{item_key}")
    record = ClaimRecord(
        startpoint_key=key,
        claim_id=item_id,
        item_key=item_key,
        new_session_id=pkg.new_session_id,
        reserved_at=now,
        title=_reserved_title(pkg.task, resolved.handoff_id),
        profile=str(deps.signer.profile),
        created_at=now,
    )
    if journal is not None:
        journal.put(record)
    return record


def _commit_claims(deps: CheckoutDeps, claims: list[Any], *,
                   timeout: timedelta) -> None:
    """把接續記錄（認領／接續單）提交並等確認；被拒就明確拒絕。

    這些項目裡帶著新 session 的**空**紀錄，所以 `apply_claim`／
    `apply_continuation` 不需要另外收到一個 session 項目：被接受時接續 Link 與
    那筆空紀錄一起進 Agora，被拒時什麼都沒寫。

    等的是**接續記錄**（讀取介面看到接續 Link 屬於新 session），不是新 session
    本身——它這時還沒有後續快照，用 `raw_sha256` 判可見會永遠等不到。
    """
    result = deps.commit_claim(claims, timeout=timeout)
    rejected = tuple(getattr(result, "rejected", ()) or ())
    if rejected:
        details = "、".join(f"{_label(a)} {code}" for a, code in rejected)
        # ★ 只刪**被拒的那幾張**的記錄（review-7a4ca87 H2）。n→1 時提交流程是
        #   一筆一筆分別套用的，所以可能有幾張被接受、幾張被拒（那幾張已經被
        #   別人接走）。此時若把整批記錄都刪掉，已經被接受的那幾張就變成
        #   「已被預留、卻沒有記錄可以 resume」的狀態——交接單卡死，而且救不回來。
        rejected_ids = _rejected_ids(rejected)
        if not rejected_ids:
            # 拿不到被拒的是哪幾張（回報格式變了）：**寧可一張都不刪**。留下
            # 來只是下次重跑時被當成「已經認領過」，那是可恢復的；刪錯了就
            # 永久認不回來。
            rejected_ids = set()
        accepted = [c for c in claims
                    if _handoff_of(c) not in rejected_ids]
        _forget_claims(deps, rejected_ids=rejected_ids)
        if accepted and rejected_ids:
            raise PartialClaimAccepted(
                f"這批接續記錄**部分被接受**，所以沒有產出起點包：{details}。"
                f"已經被接受的是：{'、'.join(_handoff_of(c) for c in accepted)}，"
                f"它們已被預留的 {pkg_session_id(deps, accepted)} 接走"
                "（Agora 裡現在有一筆空的預留 session 與接續 Link）。"
                "接下來請人決定：只拿已被接受的那幾筆產出起點包"
                "（--accept-partial），或等它們的狀態釐清。"
                "已被拒那幾筆的對象已經不是這次的了，不要重試它們。",
                accepted=tuple(_handoff_of(c) for c in accepted),
                rejected=tuple(sorted(_rejected_ids(rejected))),
                new_session_id=pkg_session_id(deps, accepted),
            )
        raise ClaimRejected(
            f"接續記錄被拒收，所以沒有產出起點包：{details}。"
            "被拒通常要人處理（例如交接單已被別人接走、快照已過期）；"
            "不要重試同一批，請把原因回報給使用者。"
        )
    if getattr(result, "timed_out", False):
        # 逾時不等於被拒：那筆可能下一輪就被收進去了，所以**留著記錄**，
        # 重跑時自動沿用（見 `_recorded_session_id` 與 `_link_record`）。
        raise ClaimRejected(
            "接續記錄還沒被讀取介面確認，所以沒有產出起點包："
            f"{getattr(result, 'summary', lambda: '')()}。"
            "先確認提交流程正常（健康檢查）再重跑一次；"
            "本機已記下這次預留，重跑會自動沿用同一組 id 與同一個預留的 session id"
            "（不要換新的，也不要自己去清認領記錄）"
        )


def _handoff_of(item: Any) -> str:
    """這個接續記錄項目接的是哪個起點（交接單 id，或 `<session>@<訊息>`）。

    認領項目看 `body.handoff_id`；接續單項目沒有交接單，用它記錄的接續點。
    """
    body = (getattr(item, "sidecar", {}) or {}).get("body") or {}
    handoff_id = body.get("handoff_id")
    if handoff_id:
        return str(handoff_id)
    cont = body.get("continuation") or {}
    message_id = cont.get("message_id") if isinstance(cont, dict) else None
    return f"{body.get('target_session_id')}@{message_id}"


def _rejected_ids(rejected: Sequence[Any]) -> set[str]:
    """`commit_claim` 回報的拒收清單裡的項目 id。

    `rejected` 的每一項是 `(awaited, code)`；`awaited.target` 是那個項目自己的 id
    （`claim:<ULID>` 或 `continuation:<ULID>`，`_label` 印出來的形式就是它）。
    取不到就回空集合——寧可少刪，也不要刪到別人的記錄（見 `_forget_claims`）。
    """
    out: set[str] = set()
    for entry in rejected:
        awaited = entry[0] if isinstance(entry, tuple) else entry
        target = getattr(awaited, "target", None)
        if isinstance(target, str) and target:
            out.add(target)
    return out


def pkg_session_id(deps: CheckoutDeps, claims: Sequence[Any]) -> str:
    """這批接續記錄預留的 session id（已經被接受的那幾筆）。"""
    for item in claims:
        body = (getattr(item, "sidecar", {}) or {}).get("body") or {}
        session_id = body.get("claimer_session_id") or body.get("new_session_id")
        if isinstance(session_id, str):
            return session_id
    return "?"


def _forget_claims(deps: CheckoutDeps, *, rejected_ids: set[str]) -> None:
    """只刪**被明確拒收**那幾筆的記錄（review-7a4ca87 H2）。

    已經被接受的那幾筆**一定要留著**：它們的起點已被預留的 session 接走，記錄是
    那個預留日後還能被認出來的唯一線索。逾時也留著（同樣理由）。
    """
    journal = deps.journal
    if journal is None:
        return
    for handoff_id in sorted(rejected_ids):
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
    "ClaimAlreadyCommitted",
    "ClaimRejected",
    "ContextLimitExceeded",
    "ContextPackageError",
    "DEFAULT_CLAIM_TIMEOUT",
    "ObjectError",
    "PartialClaimAccepted",
    "checkout",
]
