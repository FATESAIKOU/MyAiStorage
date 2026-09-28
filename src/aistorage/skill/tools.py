"""住民工具的邏輯（tasks 5.4，docs/impl/group5-7 第 4 節）。

plugin（TypeScript）只做兩件事：把 `context.sessionID` 傳進來、轉呼叫
`python -m aistorage.skill <cmd>`。所有邏輯都在這裡，所以可以直接做單元測試。

三層主 Session 限定（plugin → 這裡 → 提交流程的 `apply_claim`）的中間層
在這裡：`_require_main_session`。宣告停止（stop）沒有提交流程那一層，
所以這裡的檢查是必要的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from aistorage.clock import Clock
from aistorage.errors import AiStorageError
from aistorage.inbox import DEFAULT_MAX_RAW_SIZE
from aistorage.inbox_builder import (
    BuiltItem,
    InboxBuildError,
    build_artifact_item,
    build_claim_item,
    build_handoff_item,
    build_reference_item,
    upload_item,
)
from aistorage.syncer.commit import SyncDeps, sync_and_commit
from aistorage.syncer.continuation import continuation_point
from aistorage.syncer.core import Signer, sync_once
from aistorage.syncer.opencode_api import OpencodeApi
from aistorage.syncer.state import SyncState

__all__ = [
    "MainSessionRequired",
    "RejectedItems",
    "SkillDeps",
    "SkillError",
    "claim",
    "find",
    "handoff_end",
    "list_handoffs",
    "normalize_parts",
    "read",
    "reference",
    "resolve_session",
    "split",
    "stop",
    "whoami",
]

#: 收容產出的單檔上限（D7：100 MiB）。與 sidecar schema 的 raw.size 上限、
#: `foundry.apply.MAX_ARTIFACT_SIZE` 是同一個值，這裡以 inbox 的常數為準。
ARTIFACT_MAX_BYTES = DEFAULT_MAX_RAW_SIZE


class SkillError(RuntimeError):
    """住民工具的錯誤；message 裡不得含秘密或 Session 內文。"""


class MainSessionRequired(SkillError):
    """這個操作只能在主 Session 做（plugin 與這裡都會擋）。"""


class RejectedItems(SkillError):
    """有項目被提交流程拒收：呼叫端必須停下並把原因回報給使用者。"""

    def __init__(self, reasons: Sequence[tuple[str, str]]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("有項目被拒收：" + "、".join(f"{k} {c}" for k, c in self.reasons))


@dataclass
class SkillDeps:
    """工具需要的相依（與 SynDeps 相同，改名以免混淆）。"""

    deps: SyncDeps
    state: SyncState

    @property
    def api(self) -> OpencodeApi:
        return self.deps.api

    @property
    def reader(self) -> Any:
        return self.deps.reader

    @property
    def clock(self) -> Clock:
        return self.deps.clock


# ---------------------------------------------------------------------------
# Session 身分（plugin 傳進來的 session id 不可信，這裡查一次 API）
# ---------------------------------------------------------------------------


def resolve_session(api: OpencodeApi, session_id: str) -> Any:
    """把 plugin 傳來的 id 轉成 OcSession；查不到就明確拒絕。"""
    if not session_id or not session_id.strip():
        raise SkillError("沒有 session id：plugin 必須從 context.sessionID 傳入")
    oc_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
    for oc in api.list_sessions():
        if oc.id == oc_id:
            return oc
    raise SkillError(f"opencode 裡找不到這個 Session: {oc_id}")


def _require_main_session(api: OpencodeApi, session_id: str) -> Any:
    """主 Session 限定的第二層（plugin 已經擋過一次）。"""
    oc = resolve_session(api, session_id)
    if oc.parent_id:
        raise MainSessionRequired(
            f"這個操作只能在主 Session 做（{oc.id} 是子 Session）；"
            "plugin 與提交流程都有另外兩層檢查"
        )
    return oc


def whoami(api: OpencodeApi, session_id: str) -> dict:
    """`aistorage_whoami`：目前的 Session 是誰。"""
    oc = resolve_session(api, session_id)
    return {
        "session_id": oc.session_id(),
        "parent_id": oc.parent_session_id(),
        "is_main": oc.is_main,
    }


# ---------------------------------------------------------------------------
# 讀取（find / read）：一律附上 freshness
# ---------------------------------------------------------------------------


def find(reader: Any, query: str, *, max_lag: timedelta | None = None,
         case_id: str | None = None, limit: int = 50) -> dict:
    """`aistorage_find`：找 Session，**每筆都附上快照時間與新鮮度**。"""
    from aistorage.search.query import Query

    result = reader.find_sessions(
        Query(text=query or None, case_id=case_id, limit=limit), max_lag=max_lag)
    value = getattr(result, "value", result)
    freshness = getattr(result, "freshness", None)
    hits = []
    for found in value:
        if isinstance(found, dict):
            row = found.get("hit", found)
            own = found.get("freshness", freshness)
        else:
            row = getattr(found, "hit", found)
            own = getattr(found, "freshness", freshness)
        row = row if isinstance(row, dict) else getattr(row, "__dict__", {})
        hits.append({
            "session_id": row.get("session_id"),
            "title": row.get("title"),
            "status": row.get("status"),
            "snapshot_at": row.get("snapshot_at"),
            "freshness": _freshness_dict(own),
        })
    return {"hits": hits, "freshness": _freshness_dict(freshness)}


def read(reader: Any, session_id: str, *, max_lag: timedelta | None = None) -> dict:
    """`aistorage_read`：讀一個 Session，**附上 freshness 與快照時間**。"""
    result = reader.get_session(session_id, max_lag=max_lag)
    view = getattr(result, "value", result)
    freshness = getattr(result, "freshness", None)
    session = getattr(view, "session", None)
    return {
        "session": session if isinstance(session, dict) else getattr(session, "__dict__", {}),
        "links_out": [
            row if isinstance(row, dict) else getattr(row, "__dict__", {})
            for row in getattr(view, "links_out", ())
        ],
        "freshness": _freshness_dict(freshness),
    }


def _freshness_dict(freshness: Any) -> dict:
    """把讀取介面的 Freshness 轉成給 AI 看的字典。

    **欄位名稱要和 `reader.Freshness` 一致**（review-g5-6 M3）：原本這裡寫的是
    `lag_s`／`ok`，但實際欄位是 `satisfied`／`warning`／`stopped_ok`／`generation`／
    `published_at` → 兩個值永遠是 None，AI 就看不到「未達新鮮度」的判斷。
    """
    if freshness is None:
        return {}
    if isinstance(freshness, dict):
        return dict(freshness)
    return {
        "snapshot_at": getattr(freshness, "snapshot_at", None),
        "generation": getattr(freshness, "generation", None),
        "published_at": getattr(freshness, "published_at", None),
        # 讀者有要求 max_lag 時，satisfied 才有意義（沒要求是 None）
        "satisfied": getattr(freshness, "satisfied", None),
        "warning": getattr(freshness, "warning", None),
        "stopped_ok": getattr(freshness, "stopped_ok", None),
    }


# ---------------------------------------------------------------------------
# 交接單與提交
# ---------------------------------------------------------------------------


def _signer_key(signer: Signer) -> bytes:
    """簽章金鑰的位元組（只在簽章當下取出，不進 log／repr）。"""
    return signer.key


def _build_handoffs(
    oc: Any, *, parts: Sequence[dict], deps: SyncDeps, signer: Signer,
    snapshot_sha256: str, raw_path: Path, clock: Clock,
) -> list[BuiltItem]:
    """為每一份工作各組一張交接單（分裂 1→n）。

    接續點是同一個（被接續 Session 與那份快照），每張單的內容不同。
    """
    point = continuation_point(
        deps.converter or _opencode_converter(), raw_path,
        session_id=oc.session_id(), snapshot_sha256=snapshot_sha256,
    )
    if point is None:
        raise SkillError(
            "這次 Session 沒有可用的接續點（最後一則已完成的訊息不存在）；"
            "請等這次回覆完成後再交出末端"
        )
    key = _signer_key(signer)
    now = clock.now_utc()
    items: list[BuiltItem] = []
    normalized = normalize_parts(parts)
    for index, part in enumerate(normalized):
        title = str(part.get("title") or "").strip()
        if not title:
            # 說清楚長什麼樣子：模型看到自己的參數才改得對
            # （9.1 e2e：space-bunny-free 因為錯誤訊息太模糊，重試四次都一樣）
            raise SkillError(
                f"第 {index + 1} 份工作沒有 title。"
                f"每一份都要 {{title, summary, next_steps}}；"
                f"這一次的鍵是 {sorted(part)}。請直接給一個清單，"
                '例如 [{"title": "後端架構實作", "summary": "…"}]'
            )
        body: dict[str, Any] = {
            "title": title,
            "content": str(part.get("summary") or "").strip() or title,
        }
        if part.get("next_steps"):
            body["next_steps"] = str(part["next_steps"]).strip()
        if part.get("case_id"):
            body["case_id"] = str(part["case_id"]).strip()
        items.append(build_handoff_item(
            target_session_id=oc.session_id(),
            continuation=point.continuation(),
            body=body, profile=signer.profile, key=key, key_id=signer.key_id,
            now=now,
        ))
    return items


def normalize_parts(parts: Any) -> list[dict]:
    """把模型給的 `parts` 正規化成「一張交接單一個 dict」的清單。

    小模型經常把清單包成物件（`{"item": [ {...}, {...} ]}`）、把清單包成
    JSON 字串、或在清單裡再包一層——那是**模型的形狀問題**，不是要它重試的理由，
    所以在這裡吸收掉（9.1 e2e 實測：`opencode/space-bunny-free` 送的是
    `{"item": [...]}`，四次全被回「每一份工作都要有 title」擋下）。

    吸收的都是「同一份內容換一種包法」，不會改動交接單的內容；看不懂就回錯誤。
    """
    if isinstance(parts, str):
        # 模型把整個 JSON 當字串塞進來（9.1 e2e 實測有這種）
        try:
            decoded = json.loads(parts)
        except json.JSONDecodeError:
            raise SkillError("parts 是字串，但不是合法的 JSON") from None
        return normalize_parts(decoded)
    if isinstance(parts, dict):
        # {"item": [...]}／{"items": [...]}／{"parts": [...]}：取唯一的清單值
        list_values = [v for v in parts.values() if isinstance(v, list)]
        if len(parts) == 1 and len(list_values) == 1:
            return normalize_parts(list_values[0])
        if any(k in parts for k in ("title", "summary", "next_steps")):
            return [parts]
        if not parts:
            raise SkillError("有一份工作是空的物件：至少要有 title")
        raise SkillError(
            f"parts 的形狀看不懂：{sorted(parts)}；"
            "請給一個清單，每一項是 {title, summary, next_steps}"
        )
    if isinstance(parts, list):
        out: list[dict] = []
        for item in parts:
            # 清單裡再包一層（`[{"item": [...]}]`）或塞 JSON 字串：攤平
            if isinstance(item, (str, dict)) and not (
                isinstance(item, dict) and any(
                    k in item for k in ("title", "summary", "next_steps")
                )
            ):
                nested = normalize_parts(item)
                if len(nested) == 1 and nested[0] is item:
                    raise SkillError(
                        f"parts 裡的每一項都要是 {{title, summary, next_steps}}，"
                        f"得到 {sorted(item)}"
                    )
                out.extend(nested)
                continue
            if not isinstance(item, dict):
                raise SkillError(f"parts 裡的每一項都要是物件，得到 {type(item).__name__}")
            out.append(item)
        if not out:
            raise SkillError("parts 是空的：至少要有一份工作")
        return out
    raise SkillError(
        f"parts 要是清單或物件，得到 {type(parts).__name__}"
    )


def _drop_file(path: Path) -> None:
    """刪掉用完的匯出副本（刪不到就算了，不影響結果）。"""
    try:
        path.unlink()
    except OSError:
        pass


def _opencode_converter() -> Any:
    from aistorage.converters import get_converter

    return get_converter("opencode")


def _export_and_sync(
    sd: SkillDeps, session_id: str, *, extra: Sequence[BuiltItem] = (),
    timeout: timedelta | None = None, already_synced: bool = False,
) -> Any:
    """同步自己（＋其他項目）並提交，然後等結果。"""
    deps = sd.deps
    result = sync_and_commit(
        session_ids=[session_id], extra_items=list(extra), deps=deps,
        timeout=timeout or timedelta(minutes=15),
        progress=lambda _m: None,
        already_synced=already_synced,
    )
    if result.rejected:
        raise RejectedItems([(a.item_key, code) for a, code in result.rejected])
    if result.sync_errors:
        raise SkillError(
            "同步失敗：" + "、".join(f"{sid} {code}" for sid, code in result.sync_errors)
        )
    return result


def split(sd: SkillDeps, session_id: str, parts: Sequence[dict], *,
          timeout: timedelta | None = None) -> dict:
    """`aistorage_split`：同步自己，為每一份工作各寫一張交接單，一起提交。"""
    oc = resolve_session(sd.api, session_id)
    deps = sd.deps
    # 先同步自己（commit 前必須讓快照在 Agora 裡）
    # keep_exports：接續點要從匯出檔算（review-g5-6 M5：預設上傳後會刪掉匯出檔，
    # 避免在 /work 累積真實對話內容）
    outcome = sync_once(
        api=deps.api, reader=deps.reader, drive=deps.drive,
        inbox_folder_id=deps.inbox_folder_id, signer=deps.signer,
        state=sd.state, clock=deps.clock, workdir=deps.workdir,
        converter=deps.converter, only=[oc.id], keep_exports=True,
    )
    rec = sd.state.sessions.get(oc.session_id())
    if rec is None or not rec.last_uploaded_sha:
        raise SkillError("同步自己失敗，拿不到快照雜湊")
    raw_path = deps.workdir / "exports" / f"{oc.id}.json"
    try:
        handoffs = _build_handoffs(
            oc, parts=parts, deps=deps, signer=deps.signer,
            snapshot_sha256=rec.last_uploaded_sha, raw_path=raw_path,
            clock=deps.clock,
        )
    finally:
        # 算完接續點就把匯出檔刪掉（M5）
        _drop_file(raw_path)
    result = _export_and_sync(sd, oc.id, extra=handoffs, timeout=timeout,
                              already_synced=True)
    if result.timed_out:
        raise SkillError(
            "交接單還沒被收進去：" + result.summary() + "（可以稍後用 aistorage_list_handoffs 確認）"
        )
    return {
        "session_id": oc.session_id(),
        "handoff_ids": [i.item_id for i in handoffs],
        "visible": [f"{a.kind}:{a.target}" for a in result.visible],
    }


def handoff_end(sd: SkillDeps, session_id: str, summary: str, *,
                next_steps: str | None = None, timeout: timedelta | None = None) -> dict:
    """`aistorage_handoff_end`：交出末端（只有一張交接單）。"""
    return split(
        sd, session_id,
        [{"title": str(summary)[:80] or "交出末端", "summary": summary,
          "next_steps": next_steps}],
        timeout=timeout,
    )


def claim(sd: SkillDeps, session_id: str, handoff_ids: Sequence[str], *,
          timeout: timedelta | None = None) -> dict:
    """`aistorage_claim`：認領一張或多張交接單（多張＝統合）。

    **只有主 Session 可以做**。等 Link 屬於自己之後，才回傳交接單與接續點
    之前的閱讀版。任一張被拒收就整個停下（`RejectedItems`）。
    """
    oc = _require_main_session(sd.api, session_id)
    if not handoff_ids:
        # 9.1 e2e 實測：免費模型常把 id 送成別的鍵名或別的形狀，plugin 吸收不到
        # 就會走到這裡。錯誤訊息要明確說「要什麼形狀」，模型才改得對。
        available = ""
        try:
            rows = list_handoffs(sd).get("handoffs") or []
            if rows:
                available = "；現在等著被認領的是：" + ", ".join(
                    str(r.get("handoff_id")) for r in rows
                )
        except Exception:  # noqa: BLE001 - 只是想多給一點線索，拿不到就算了
            available = ""
        raise SkillError(
            "至少要給一張交接單的 id。handoff_ids 是一個字串清單，"
            '例如 {"handoff_ids": ["handoff:01ABC…"]}（id 要用 '
            "aistorage_list_handoffs 回報的那個，不要自己拼）" + available
        )
    key = _signer_key(sd.deps.signer)
    now = sd.deps.clock.now_utc()
    items = [
        build_claim_item(
            handoff_id=hid, claimer_session_id=oc.session_id(),
            profile=sd.deps.signer.profile, key=key,
            key_id=sd.deps.signer.key_id, now=now,
        )
        for hid in handoff_ids
    ]
    result = _export_and_sync(sd, oc.id, extra=items, timeout=timeout)
    if result.timed_out:
        raise SkillError("認領還沒被收進去：" + result.summary())
    return {
        "session_id": oc.session_id(),
        "claim_ids": [i.item_id for i in items],
        "handoffs": _handoff_payloads(sd.reader, handoff_ids),
    }


def _handoff_payloads(reader: Any, handoff_ids: Sequence[str]) -> list[dict]:
    """讀回交接單內容與接續點之前的閱讀版（Link 屬於自己之後才讀）。"""
    out: list[dict] = []
    for hid in handoff_ids:
        try:
            view = reader.get_continuation(hid)
        except Exception as e:
            out.append({"handoff_id": hid, "error": type(e).__name__})
            continue
        value = getattr(view, "value", view)
        handoff = getattr(value, "handoff", None)
        messages = getattr(value, "messages", ())
        out.append({
            "handoff_id": hid,
            "target_session_id": getattr(handoff, "target_session_id", None),
            "message_id": getattr(handoff, "message_id", None),
            "snapshot_sha256": getattr(handoff, "snapshot_sha256", None),
            "body": getattr(handoff, "body_json", ""),
            "reading_before": messages,
        })
    return out


def reference(sd: SkillDeps, session_id: str, target_session_id: str, *,
              read_snapshot_at: str | None = None,
              upload_only: bool = True) -> dict:
    """`aistorage_reference`：留下參考 Link。

    **PM 決定 4**：預設只上傳、不觸發提交，由下一輪提交流程收進去。

    `read_snapshot_at` **必須是呼叫端真的讀到的快照時間**（review-g5-6 L5）：
    原本沒給就去抓對方「目前」的時間，等於宣稱讀到了一個其實沒讀過的版本。
    """
    oc = resolve_session(sd.api, session_id)
    snapshot_at = read_snapshot_at
    if not snapshot_at:
        raise SkillError(
            "read_snapshot_at 必填：要填你剛剛 aistorage_read 讀到的 snapshot_at。"
            "自己抓對方「目前」的時間會讓這筆參考宣稱讀到一個其實沒讀過的版本"
        )
    key = _signer_key(sd.deps.signer)
    item = build_reference_item(
        from_session_id=oc.session_id(), to_session_id=target_session_id,
        read_snapshot_at=snapshot_at, profile=sd.deps.signer.profile,
        key=key, key_id=sd.deps.signer.key_id, now=sd.deps.clock.now_utc(),
    )
    upload_item(sd.deps.drive, sd.deps.inbox_folder_id, item)
    return {
        "reference_id": item.item_id,
        "item_key": item.item_key,
        "from_session_id": oc.session_id(),
        "to_session_id": target_session_id,
        "read_snapshot_at": snapshot_at,
        "uploaded_only": bool(upload_only),
    }


def register_artifact(
    sd: SkillDeps,
    session_id: str,
    *,
    kind: str,
    name: str,
    content_type: str | None = None,
    link: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    file_path: str | None = None,
    description: str | None = None,
    case_id: str | None = None,
) -> dict:
    """`aistorage_register_artifact`：把一件產出登錄到 Foundry 產出目錄（7.2）。

    `produced_by_session_id` **由 plugin 的 context 帶入**（session_id），不給模型填：
    產生者是「哪個 Session 交出它」，模型可能填錯或填別人的（比照 Session id 的規則）。

    - `link`（原處產出）：`link` 是必填的對外連結（sidecar schema 的要求）；
      `repo`／`path` 是選填的出處補充（原處在哪個 repo 的哪個路徑）。
      真本留在自己的專案裡，Foundry 只登錄出處。
    - `contained`（收容產出）：`file_path` 指向容器內的本體檔（單檔上限 100 MiB；
      超過由 `build_artifact_item` 拒收），`content_type` 必填。

    只上傳，不觸發提交（與 `reference` 相同：由下一輪提交流程收進去）。
    """
    oc = resolve_session(sd.api, session_id)
    if kind not in ("link", "contained"):
        raise SkillError(f"kind 必須是 'link' 或 'contained': {kind!r}")
    if not isinstance(name, str) or not name.strip():
        raise SkillError("name 必填（產出的檔名）")
    if kind == "link":
        if not link:
            raise SkillError(
                "link 型必須給 link（對外連結；repo 與 path 可選填、補充出處）"
            )
        if file_path:
            raise SkillError("link 型不得提供 file_path（本體留在原處）")
    else:
        if not content_type:
            raise SkillError("contained 型必須給 content_type")
        if not file_path:
            raise SkillError("contained 型必須給 file_path（容器內的本體檔）")
        body_path = Path(file_path)
        if not body_path.is_file():
            raise SkillError(f"找不到本體檔案：{file_path}")

    now = sd.deps.clock.now_utc()
    key = _signer_key(sd.deps.signer)
    try:
        item = build_artifact_item(
            kind=kind,  # type: ignore[arg-type]
            produced_by_session_id=oc.session_id(),
            name=name.strip(),
            profile=sd.deps.signer.profile,
            key=key, key_id=sd.deps.signer.key_id,
            content_type=content_type,
            link=link, repo=repo, path=path,
            raw_path=Path(file_path) if file_path else None,
            description=description,
            case_id=case_id,
            now=now,
            max_raw=ARTIFACT_MAX_BYTES,
        )
    except InboxBuildError as e:
        # 大小上限／格式問題：轉成模型的錯誤訊息（不含內容）
        raise SkillError(f"產出登錄未通過檢查：{e}") from None

    upload_item(sd.deps.drive, sd.deps.inbox_folder_id, item)
    return {
        "artifact_id": item.item_id,
        "item_key": item.item_key,
        "kind": kind,
        "name": name.strip(),
        "content_type": content_type,
        "size": item.raw_size,
        "produced_by_session_id": oc.session_id(),
        "uploaded_only": True,
    }


def list_handoffs(sd: SkillDeps, *, case_id: str | None = None) -> dict:
    """`aistorage_list_handoffs`：列出還沒被認領的交接單。"""
    reader = sd.reader
    result = reader.list_open_handoffs(case_id=case_id)
    value = getattr(result, "value", result)
    rows = []
    for h in value:
        rows.append({
            "handoff_id": getattr(h, "handoff_id", None),
            "target_session_id": getattr(h, "target_session_id", None),
            "producer": getattr(h, "producer", None),
            "author_session_id": getattr(h, "author_session_id", None),
            "case_id": getattr(h, "case_id", None),
            "updated_at": getattr(h, "updated_at", None),
        })
    return {"handoffs": rows, "freshness": _freshness_dict(getattr(result, "freshness", None))}


def stop(sd: SkillDeps, session_id: str, *, timeout: timedelta | None = None) -> dict:
    """`aistorage_stop`：宣告停止（**主 Session 限定**）。

    以 API 設定 `time.archived = now`，再同步並提交。
    """
    oc = _require_main_session(sd.api, session_id)
    now_ms = int(sd.deps.clock.now().timestamp() * 1000)
    sd.api.archive(oc.id, now_ms)
    # 封存之後同步並提交，Agora 才會看到 stopped
    result = _export_and_sync(sd, oc.id, timeout=timeout)
    if result.timed_out:
        raise SkillError("宣告停止還沒被收進去：" + result.summary())
    return {"session_id": oc.session_id(), "archived_ms": now_ms}
