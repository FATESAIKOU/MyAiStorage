"""起點（`agora checkout` 的起點參數）的解析與解析後的表達式。

依據 `docs/design/agora-session-operations.md`：

    `<起點>` 可以是 `handoff:<id>`（接某張交接單），也可以是
    `<session>[@<訊息>]`（直接從任何 session 的任何位置開始）。

「起點」＝**一個 session 的某個位置（快照＋那一則訊息）**。不指定位置就是最新
已提交的那一則。

**這個模組完全不碰任何 coding agent**：原始紀錄是來源應用的匯出檔，起點包原封
不動地放著它，截斷與重編 id 是轉接器的事（`agora-opencode load`）。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

#: 交接單 id（`build_claim_item` / `schemas/inbox-sidecar.schema.json` 同一套）。
HANDOFF_ID_RE = re.compile(r"^handoff:[0-9A-HJKMNP-TV-Z]{26}$")

#: Session id 是 `<source>:<source_session_id>`（`schemas/README.md` 第 4 節）。
#: 起點裡允許省略前綴（`ses_abc`），由呼叫端補上。
SESSION_ID_RE = re.compile(r"^(?P<source>[a-z0-9][a-z0-9_-]*):(?P<native>\S+)$")
NATIVE_ONLY_RE = re.compile(r"^(?P<native>[A-Za-z0-9][A-Za-z0-9_.:-]*)$")

#: 預設的來源應用。省略前綴時補上它（`agora checkout ses_abc` 是常見寫法）。
DEFAULT_SOURCE = "opencode"


class StartPointError(ValueError):
    """起點字串看不懂或查不到。訊息裡只有 id 與規則，不含內容。"""


@dataclass(frozen=True)
class StartPoint:
    """解析後（還沒查讀取介面）的起點。"""

    #: 呼叫端給的原字串（`package.json` 的 `segments[].startpoint` 留著它）
    text: str
    #: 完整 Session id（`<source>:<native>`）
    session_id: str
    #: 來源應用
    source: str
    #: 來源端自己的 Session id（不含前綴）
    native_session_id: str
    #: 指定的訊息 id；None 代表「最新已提交的那一則」
    message_id: str | None = None
    #: 起點是交接單時的交接單 id
    handoff_id: str | None = None

    @property
    def is_handoff(self) -> bool:
        return self.handoff_id is not None


def parse_startpoint(text: str, *, source: str = DEFAULT_SOURCE) -> StartPoint:
    """把起點字串解析成 `StartPoint`；看不懂就明確拒絕（不要猜）。

    - `handoff:<ULID>` → 交接單起點（Session id 與訊息要查讀取介面才知道）。
    - `<session>[@<訊息>]` → 直接起點。`<session>` 可以帶或不帶 `<source>:` 前綴。

    注意 `handoff:` 這個前綴不是 Session id 的 `<source>:`，所以要先分開判斷：
    `SESSION_ID_RE` 的 source 允許 `handoff`，但那在 Session id 的保留清單裡，
    不會出現在真實的 Agora Session id 上。
    """
    raw = (text or "").strip()
    if not raw:
        raise StartPointError("起點不可為空")
    if HANDOFF_ID_RE.match(raw):
        # 交接單的目標 Session 要查讀取介面，這裡先放空字串（由 resolve 補）。
        return StartPoint(text=raw, session_id="", source=source,
                          native_session_id="", handoff_id=raw)

    session_part, at, message_id = raw.partition("@")
    if at and not message_id.strip():
        raise StartPointError(
            f"起點 {raw!r} 有 @ 但沒給訊息 id；請寫成 <session>@<message id>"
        )
    session_part = session_part.strip()
    if not session_part:
        raise StartPointError(f"起點 {raw!r} 缺少 Session id")

    matched = SESSION_ID_RE.match(session_part)
    if matched:
        got_source, native = matched.group("source"), matched.group("native")
        if got_source in ("handoff", "claim", "reference", "rewrite", "artifact", "session"):
            raise StartPointError(
                f"起點 {raw!r} 的前綴 {got_source!r} 是項目型態的保留字，"
                "不是來源應用名稱"
            )
    elif NATIVE_ONLY_RE.match(session_part):
        got_source, native = source, session_part
    else:
        raise StartPointError(
            f"起點 {raw!r} 的 Session id 看不懂："
            "請用 <source>:<source_session_id> 或省略前綴的 <source_session_id>"
        )

    return StartPoint(
        text=raw,
        session_id=f"{got_source}:{native}",
        source=got_source,
        native_session_id=native,
        message_id=message_id.strip() or None,
    )


@dataclass(frozen=True)
class ResolvedStartPoint:
    """查過讀取介面、真正定位到一個快照與接續點的起點。"""

    startpoint: StartPoint
    session_id: str
    source: str
    snapshot_sha256: str
    snapshot_at: str | None
    message_id: str | None
    handoff_id: str | None = None
    title: str | None = None
    task: str | None = None

    @property
    def order_key(self) -> tuple[str, str]:
        """n→1 決定「誰放最前面」時用的穩定鍵（同一個起點永遠同一把鍵）。"""
        return (self.session_id, self.snapshot_sha256)


def _message_ids(reading: dict) -> list[str]:
    """從閱讀版取出訊息 id（依 index 排序）。"""
    out: list[str] = []
    for position, msg in enumerate(reading.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        mid = msg.get("message_id")
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


def last_completed(reading: dict) -> str | None:
    """最新已提交且已完成、未撤銷的那一則訊息 id；沒有就 None。

    語意與 `syncer.continuation.last_completed_message_id` **完全一致**
    （index 決定順序、completed 與 reverted 決定算不算）——自己重寫一份就會
    在「接續點」這件事上與寫交接單的那一邊漂移。
    """
    from aistorage.syncer.continuation import last_completed_message_id

    return last_completed_message_id(reading)[0]


def resolve_startpoint(reader: Any, sp: StartPoint, *, max_lag: Any = None
                       ) -> ResolvedStartPoint:
    """把起點解析到讀取介面裡的一個快照與接續點。

    交接單起點：讀取介面已經知道那張單的目標 Session、釘住的快照與接續點，
    所以一律以交接單記錄的為準（`agora checkout` 不接受在交接單起點上改位置）。
    """
    if sp.is_handoff:
        return _resolve_handoff(reader, sp)

    result = reader.get_session(sp.session_id, max_lag=max_lag)
    view = getattr(result, "value", result)
    session = getattr(view, "session", None)
    if session is None:
        raise StartPointError(f"讀取介面沒有這個 Session: {sp.session_id}")
    snapshot_sha = getattr(session, "raw_sha256", None)
    if not snapshot_sha:
        raise StartPointError(
            f"Session {sp.session_id} 還沒有已提交的快照，沒有東西可以接續"
        )

    # 接續點必須落在**被釘住的那一份快照**裡，所以閱讀版也要指定那個雜湊
    # （不要用「最新」——那可能是另一份快照，接續點就對不上了）。
    reading = _read_pinned(reader, sp.session_id, str(snapshot_sha), max_lag=max_lag)
    if sp.message_id is None:
        # 不指定位置 → 最新已提交的那一則
        message_id = last_completed(reading)
        if not message_id:
            raise StartPointError(
                f"Session {sp.session_id} 的這一份快照沒有任何已完成的訊息，"
                "沒有可以接續的位置"
            )
    else:
        known = _message_ids(reading)
        if sp.message_id not in known:
            raise StartPointError(
                f"Session {sp.session_id} 的最新快照裡沒有訊息 {sp.message_id}；"
                f"這個快照有 {len(known)} 則訊息"
            )
        message_id = sp.message_id

    return ResolvedStartPoint(
        startpoint=sp, session_id=sp.session_id, source=sp.source,
        snapshot_sha256=str(snapshot_sha).lower(),
        snapshot_at=getattr(session, "snapshot_at", None),
        message_id=message_id, handoff_id=None,
        title=getattr(session, "title", None),
    )


def _resolve_handoff(reader: Any, sp: StartPoint) -> ResolvedStartPoint:
    """交接單起點：位置完全由交接單決定。"""
    assert sp.handoff_id is not None
    try:
        result = reader.get_continuation(sp.handoff_id)
    except KeyError as e:
        raise StartPointError(f"讀取介面沒有這張交接單: {sp.handoff_id}") from e
    value = getattr(result, "value", result)
    handoff = getattr(value, "handoff", None)
    if handoff is None:
        raise StartPointError(f"讀取介面沒有這張交接單: {sp.handoff_id}")
    target = str(getattr(handoff, "target_session_id", "") or "")
    if not target or ":" not in target:
        raise StartPointError(
            f"交接單 {sp.handoff_id} 的目標 Session id 不合法: {target!r}"
        )
    source, _sep, _native = target.partition(":")
    body = getattr(handoff, "body_json", "") or ""
    task = None
    if isinstance(body, str) and body.strip():
        import json

        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            content = parsed.get("content")
            if isinstance(content, str) and content.strip():
                task = content
    return ResolvedStartPoint(
        startpoint=sp,
        session_id=target,
        source=source,
        snapshot_sha256=str(getattr(handoff, "snapshot_sha256", "") or "").lower(),
        snapshot_at=None,
        message_id=str(getattr(handoff, "message_id", "") or "") or None,
        handoff_id=sp.handoff_id,
        task=task,
    )


def _read_pinned(reader: Any, session_id: str, snapshot_sha256: str, *,
                 max_lag: Any = None) -> dict:
    """讀某個快照的閱讀版；讀不到就明確拒絕。"""
    try:
        result = reader.get_reading(session_id, snapshot_sha256=snapshot_sha256,
                                    max_lag=max_lag)
    except KeyError as e:
        raise StartPointError(
            f"讀取介面沒有發佈這個快照的閱讀版: {session_id}@{snapshot_sha256[:12]}"
        ) from e
    return getattr(result, "value", result)
