"""轉接器：把起點包載入成 opencode 的原生 session（`agora-opencode load`）。

**這個模組是 opencode 專屬的**。`aistorage.agora_cli` 不 import 這裡的任何東西
——Agora 只管產出起點包，載入成哪個 agent 的原生 session 是各轉接器的事
（`agora-<coding agent 名稱>`）。

做法固定（`docs/spike/session-import.md`，opencode 1.18.32 實測）：

> 匯出 JSON 截斷到接續點 → **session／message／part id 全部重編** →
> 在目標專案目錄執行 `opencode import`。

五個不能省的規則：
1. **id 一定要重編，而且要唯一**。`import` 對已存在的 message／part id 是
   `onConflictDoNothing` **靜默丟棄**：沿用原 id 匯入兩次，第二次得到的是空
   session（Q1-1）；**自己編的 id 若互相重複，丟棄的是同一批**——9.1 的 8 則進、
   1 則出就是這樣來的（`_reidentified_id` 的說明）。
2. **重編後的 id 字典序要跟匯出檔的順序一致**。opencode 匯出訊息是
   `ORDER BY time_created, id`、訊息內的 part 是 `ORDER BY message_id, id`：
   `time.created` 一樣時（合成資料、或同毫秒產生）順序由 id 決定。實測把 id 弄成
   遞減，匯出順序就會翻轉（`docs/spike/session-import.md` Q6）。
3. **送模型的上下文是照時間順序排出來的，不是照 parent 樹**（Q6 實測：parent
   鏈斷掉、倒轉、指標不存在，送模型的歷史都一樣）。所以 n→1「最長的一段放最前面」
   （ADR 0010）要真的成立，靠的是 **`time.created`**：後面的段落必須整段排在前面
   那段之後（`shift_plan`）。parent 鏈只負責分支結構，而且**只有 assistant 接得起來**
   （`chain_parents`）。
4. **在目標專案目錄匯入**。`import` 強制把 directory／project/path 改寫為當下的
   context，目錄寫在匯出檔裡也沒用（Q1-2）。
5. **不能寫出 opencode 拒收的形狀**。`import` 會用 zod 驗每一則訊息：assistant 的
   `parentID` 必須是**非 null 字串**（缺或 null 都整份拒絕），user 訊息的 `parentID`
   則會被**去掉**（Q6）。所以「把指不到的 parent 清成 null」是錯的。

`load` 印出新 session id 就結束。**開不開 agent、在哪開，是呼叫者的事**。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from aistorage.agora_cli.package import (
    TIME_SHIFT_RULE,
    ContextPackageError,
    read_package,
)

#: 匯入後預設的 session 標題前綴（`package.json` 的 `task` 會接在後面）。
TITLE_PREFIX = "Agora 起點"

#: 重編 id 的長度上限。opencode 自己的 id 是 29～30 個字元（Q6）；超過就明確
#: 報錯，**絕不截斷**——截斷會把 id 尾端那些真正負責唯一的位數吃掉，於是同一批
#: 訊息拿到同一個 id，匯入時被靜默丟棄（impl2 9.1／9.2）。
_ID_MAX = 30
#: id 裡時間那一段的位數（16 進位）。`time.created` 是毫秒，12 碼夠用到西元
#: 10889 年。
_TIME_HEX = 12
#: id 裡序號那一段的位數（16 進位）：單一匯入最多 16,777,216 則訊息／單一訊息
#: 最多 16,777,216 個 part。
_ORDINAL_HEX = 6
#: id 裡鹽雜湊那一段的位數（16 進位，24 bits）：讓不同匯入（1→n、重跑）不會
#: 拿到同一個 id。
_SALT_HEX = 6

__all__ = [
    "TIME_SHIFT_RULE",
    "AdapterError",
    "LoadResult",
    "build_export",
    "chain_parents",
    "load",
    "merge_offsets",
    "reidentify",
    "run_import",
    "salt_for",
    "shift_plan",
    "shift_times",
    "truncate_to",
]


class AdapterError(RuntimeError):
    """轉接器失敗。訊息裡只有 id 與代碼，不含 Session 內文。"""


@dataclass(frozen=True)
class LoadResult:
    """一次 `load` 的結果。"""

    session_id: str
    source: str
    messages: int
    segments: int
    import_output: str = ""
    #: 每一段被往後移了多少毫秒（第一段固定 0）。`load` 把它回報出來，讓
    #: 「後段時間已改寫」在載入端也看得見（實際數字只有載入端算得出來）。
    time_shift_ms: tuple[int, ...] = ()


def _messages(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        raise AdapterError("匯出檔的最外層必須是 JSON 物件")
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        raise AdapterError("匯出檔缺少 messages 清單")
    for position, m in enumerate(msgs):
        if not isinstance(m, dict) or not isinstance(m.get("info"), dict):
            raise AdapterError(f"第 {position} 則訊息形狀不對（缺 info 物件）")
    return msgs


def truncate_to(payload: dict, message_id: str | None) -> list[dict]:
    """截到接續點為止（**含**該則）；`message_id` 為 None 就整份不截。

    接續點是「最後一則已完成的訊息」，所以**那一則要含在內**——新 session 從
    它之後開始寫，模型才看得到交接的那一段（`reading.messages_before` 的同一個
    語意：`<= target_index`）。
    """
    msgs = _messages(payload)
    if message_id is None:
        return msgs
    for position, m in enumerate(msgs):
        if str(m["info"].get("id")) == message_id:
            return msgs[: position + 1]
    known = len(msgs)
    raise AdapterError(
        f"這個快照裡沒有訊息 {message_id}（實際有 {known} 則）；"
        "起點包的 snapshot_sha256 與訊息 id 對不上，不能拿錯位置去接續"
    )


def salt_for(session_id: str) -> str:
    """這個新 session 的重編鹽（`_SALT_HEX` 碼）。

    **由新 session id 推導，不隨機**：
    - 同一個起點包重跑（`agora checkout --resume` 之後再 `load` 一次）算出一模
      一樣的 id，匯入因此是 no-op，不會把同一段歷史複製一份（9.2 的重跑情境）。
    - 1→n 每一份起點包的預留 session id 不同（`checkout._new_session_id` 每次
      編新的），鹽就不同，id 也就不同——撞了會被 `onConflictDoNothing` 靜默丟棄。
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_SALT_HEX]


def _reidentified_id(prefix: str, *, time_ms: int, ordinal: int, salt: str) -> str:
    """組一個重編 id：`<prefix>_<時間><序號><鹽雜湊>`（固定 28 個字元）。

    三段各有用途，缺一段就會出事：

    1. **時間**（`_TIME_HEX` 碼，原始紀錄的 `time.created`）：opencode 自己就是
       「時間＋亂數」的形式，而且匯出訊息是 `ORDER BY time_created, id`——
       `time.created` 相同時順序由 id 決定，所以 id 的字典序必須跟匯出檔的順序
       一致（Q6）。
    2. **序號**（`_ORDINAL_HEX` 碼，這次匯入裡的位置）：負責**唯一**。舊的寫法
       是 `f"msg_{tag}{i:020d}"[:30]`，tag 一旦長過 10 碼就會被截斷，序號的位數
       全被吃掉，**每一則訊息都拿到同一個 id**；`opencode import` 對重複 id 是
       `onConflictDoNothing`，於是 8 則進、1 則出，而且 rc=0、沒有任何訊息
       （impl2 e2e 9.1／9.2）。**所有欄位都是固定寬度，所以不可能再被截斷。**
    3. **鹽雜湊**（`_SALT_HEX` 碼）：把新 session id、段落、原始 id 一起雜湊進去，
       讓不同匯入（1→n 的 n 份、重跑、n→1 的多段）拿到不同的 id。

    匯出時的排序是 `ORDER BY time_created, id`（訊息）與 `ORDER BY message_id, id`
    （同一則訊息內的 part），所以兩個序號都要**依匯出檔的順序遞增**。
    """
    stamp = f"{max(int(time_ms), 0) & ((1 << (4 * _TIME_HEX)) - 1):0{_TIME_HEX}x}"
    ident = f"{prefix}_{stamp}{ordinal:0{_ORDINAL_HEX}x}{salt}"
    if len(ident) > _ID_MAX:  # pragma: no cover - 固定寬度欄位加起來不會超過
        raise AdapterError(
            f"重編出來的 id 有 {len(ident)} 個字元，超過 opencode 的 {_ID_MAX}："
            f"{ident[:12]}…（要重新檢查 _TIME_HEX／_ORDINAL_HEX／_SALT_HEX 的寬度）"
        )
    return ident


def _time_ms(info: dict, fallback: int) -> int:
    """訊息的 `time.created`（毫秒）。缺就用前一則的，維持順序不倒退。"""
    time = info.get("time")
    created = time.get("created") if isinstance(time, dict) else None
    return created if isinstance(created, int) else fallback


def reidentify(payload: dict, messages: Sequence[dict], *, session_id: str,
               tag: str | None = None, first_ordinal: int = 0) -> dict:
    """把截斷後的訊息**全部 id 重編**成新 session 的（回傳新的匯出字典）。

    前綴保留（`ses_`／`msg_`／`prt_`，spike Q1-4：id 必須以這些開頭，實測以數字
    開頭會被 `import` 擋下），後綴是「時間＋序號＋鹽」（見 `_reidentified_id`）。
    `parentID` 與 `part.messageID` 依對應表改寫，**其餘欄位一個位元組都不動**
    ——動了開頭就不會與原 session 位元組相同。

    `tag` 是鹽（測試可以固定它）；預設由 `session_id` 推導（`salt_for`）。
    `first_ordinal` 是這一段在**整份匯出**裡的起始序號（n→1 時遞增），讓重編
    出來的 id 字典序與整份匯出的順序一致。
    """
    out = dict(payload)
    info = dict(payload.get("info") or {})
    info["id"] = session_id
    out["info"] = info

    salt = tag if tag else salt_for(session_id)
    message_map: dict[str, str] = {}
    new_messages: list[dict] = []
    previous_time = 0
    for i, m in enumerate(messages):
        m = dict(m)
        m_info = dict(m["info"])
        old = str(m_info.get("id"))
        previous_time = _time_ms(m_info, previous_time)
        new = _reidentified_id(
            "msg", time_ms=previous_time, ordinal=first_ordinal + i, salt=salt)
        message_map[old] = new
        m_info["id"] = new
        m_info["sessionID"] = session_id
        parent = m_info.get("parentID")
        if isinstance(parent, str) and parent in message_map:
            m_info["parentID"] = message_map[parent]
        # parent 指向保留範圍之外（截斷切掉了、或本來就斷的）：**原樣留著**。
        # `import` 不驗 parent（Q1-3），而 user 訊息的 `parentID` 本來就會被 schema
        # 去掉（Q6）；把它清成 null 反而會讓 assistant 訊息被**整份拒絕**
        # （`Expected string, got null`）。跨段的首則交給 `chain_parents`。
        m["info"] = m_info
        parts: list[dict] = []
        for j, p in enumerate(m.get("parts") or []):
            p = dict(p)
            p["id"] = _reidentified_id(
                "prt", time_ms=previous_time, ordinal=j, salt=salt)
            p["sessionID"] = session_id
            p["messageID"] = new
            parts.append(p)
        m["parts"] = parts
        new_messages.append(m)
    out["messages"] = new_messages
    return out


def chain_parents(segments: Sequence[Sequence[dict]]) -> list[dict]:
    """n→1：把後一段的**首則** parent 手工鏈到前一段的**末則**（spike Q4）。

    **只有 assistant 接得起來**：opencode 的訊息 schema 對 assistant 要求
    `parentID` 是非 null 字串，對 user 則會把 `parentID` 去掉（Q6 實測）。所以
    user 訊息就當它是 root——真實的 opencode session 在 `/undo` 之後長的就是
    這個樣子（新的 user 訊息自己是一個 root，後面的 assistant 掛在它下面）。

    段與段的先後**不是**靠 parent 鏈，而是靠 `time.created`（`shift_plan`）：
    送模型的上下文是照時間順序排出來的，parent 樹只負責分支結構。所以這裡漏掉的
    鏈接不會讓模型少看到前面的內容，但 assistant 首則接上來仍然值得做——它讓匯出
    檔的樹是一條線，而不是兩條互不相干的線。
    """
    if len(segments) < 2:
        return list(segments[0]) if segments else []
    merged: list[dict] = []
    for index, segment in enumerate(segments):
        if not segment:
            raise AdapterError(f"第 {index + 1} 段沒有訊息，無法串接")
        if index == 0:
            merged.extend(dict(m) for m in segment)
            continue
        previous_last = merged[-1]["info"]["id"]
        first = dict(segment[0])
        first_info = dict(first["info"])
        if first_info.get("role") == "assistant":
            first_info["parentID"] = previous_last
        first["info"] = first_info
        merged.append(first)
        merged.extend(dict(m) for m in segment[1:])
    return merged


def _segment_span(messages: Sequence[dict]) -> tuple[int, int]:
    """這一段（已截斷）最早與最晚的 `time.created`（毫秒）。沒有訊息就 (0, 0)。"""
    times = [_time_ms(m.get("info") or {}, 0) for m in messages]
    return (min(times), max(times)) if times else (0, 0)


def shift_plan(spans: Sequence[tuple[int, int]]) -> tuple[int, ...]:
    """每一段要整體往後移多少毫秒（第一段固定 0）。

    為什麼需要：opencode 匯出訊息是 `ORDER BY time_created, id`，而且**送模型的
    上下文就是照這個順序**（Q6 實測）。所以 ADR 0010 的「最長的一段放最前面」
    光是把陣列排好還不夠——兩段的時間一旦交錯，匯出（以及模型看到的歷史）就會
    交錯排列，「原封不動重建開頭」的第一段就不再是開頭。

    規則：**段內相對順序完全不動**，段與段之間是「上一段最後一則的下一毫秒」接續。
    已經排好的段落位移為 0（沒有東西被改寫）。
    """
    if not spans:
        return ()
    out = [0]
    for index in range(1, len(spans)):
        low, _high = spans[index]
        previous_high = spans[index - 1][1]
        out.append(0 if low > previous_high else previous_high + 1 - low)
    return tuple(out)


def _shift_time_object(value: Any, offset: int) -> Any:
    """把一個 `time` 物件裡的數字欄位整體往後移 `offset` 毫秒。"""
    if not isinstance(value, dict) or not offset:
        return value
    return {k: (v + offset if isinstance(v, int) and not isinstance(v, bool) else v)
            for k, v in value.items()}


def shift_times(messages: Sequence[dict], offset: int) -> list[dict]:
    """把這一段的時間整體往後移 `offset` 毫秒（段內相對順序不變）。

    訊息的 `time`（`created`／`completed`）與每個 part 的 `time`（`start`／`end`）
    一起移：排序只看得到訊息的 `created`，但讓整段的時鐘一致，匯出檔才不會出現
    「part 發生在它的訊息之前」。`offset` 為 0 時**原樣回傳**（第一段就是這樣，
    它必須與原始紀錄一個位元組都不差）。
    """
    if not offset:
        return list(messages)
    out: list[dict] = []
    for m in messages:
        info = dict(m.get("info") or {})
        info["time"] = _shift_time_object(info.get("time"), offset)
        parts: list[dict] = []
        for p in m.get("parts") or []:
            p = dict(p)
            p["time"] = _shift_time_object(p.get("time"), offset)
            parts.append(p)
        out.append({**m, "info": info, "parts": parts})
    return out


def merge_offsets(data: dict, spans: Sequence[tuple[int, int]]) -> tuple[int, ...]:
    """算出每段的時間位移，並確認起點包有宣告那條規則。

    兩段以上時 `package.json` 必須有 `time_shift`（`agora checkout` 寫的，見
    `TIME_SHIFT_RULE`）。**沒有就明確拒絕**：那個欄位是「後段時間已改寫」這件事
    在起點包 metadata 裡的記錄，缺了它就不知道這份起點包是不是用新規則組的；
    默默照舊組出來的 session，順序會在時間交錯時悄悄變掉。

    實際位移在這裡算（**以 raw 為準**，毫秒精度；閱讀版的 `created_at` 只有秒，
    拿它算會差幾百毫秒），起點包裡只放規則宣告。
    """
    if len(spans) < 2:
        return (0,) * len(spans)
    declared = data.get("time_shift")
    if not isinstance(declared, dict):
        raise AdapterError(
            f"這個起點包有 {len(spans)} 段，卻沒有 time_shift 宣告"
            f"（期望 rule={TIME_SHIFT_RULE!r}）："
            "後段的時間必須整體排在第一段之後，否則 opencode 匯出（以及送模型的"
            "上下文）會依 time.created 交錯排列，ADR 0010 的「最長的一段放最前面」"
            "就不成立。請重新跑 `agora checkout`（已經認領過就加 --resume，"
            "沿用同一組預留）。"
        )
    if declared.get("rule") != TIME_SHIFT_RULE:
        raise AdapterError(
            f"起點包宣告的 time_shift 規則是 {declared.get('rule')!r}，"
            f"這個轉接器只認得 {TIME_SHIFT_RULE!r}：不要猜，"
            "請用同一版的 agora checkout 與轉接器。"
        )
    if declared.get("first_segment_unchanged") is not True:
        raise AdapterError(
            "起點包宣告 time_shift.first_segment_unchanged 不是 true："
            "第一段必須原封不動，後段才需要往後排。",
        )
    return shift_plan(spans)


def build_export(package_dir: Path, *, session_id: str | None = None,
                 tag: str | None = None,
                 decisions: dict | None = None) -> tuple[dict, str, int, str]:
    """把起點包組成一份可以 `opencode import` 的匯出 JSON。

    回傳 `(匯出字典, 新 session 的來源端 id, 段數, 來源應用)`。**這一步不碰
    opencode**——所以可以被單元測試直接驗（「重編 id」、「n→1 串接」與「後段時間
    往後排」）。

    `decisions` 是選用的輸出參數：呼叫端（例如 CLI 的 `--json`）拿它回報這一次
    做了哪些決定，例如 `{"time_shift_ms": [0, 1234]}`。**只是報告**，不影響產出。

    `session_id` 只能**等於**起點包預留的那個 id（M7）：`agora checkout` 已經把
    「由我接手」記進 Agora（認領或接續 Link，而且連帶預留了這個新 session），那是
    提交流程認得出這筆預留的唯一線索。換一個 id 匯入進去，Agora 裡那筆預留就永遠
    沒有任何對應的 Session——一筆沒有人負責的空 session。所以覆寫不等就明確拒絕，
    不要「幫忙」換一個。
    """
    data, raws = read_package(Path(package_dir))
    new_session = data.get("new_session") or {}
    reserved_ref = str(new_session.get("session_id") or "")
    if not reserved_ref:
        raise ContextPackageError(
            "起點包沒有 new_session.session_id："
            "轉接器必須沿用它（收件匣裡的認領或接續 Link 記的是這個 id）"
        )
    if session_id is not None and session_id != reserved_ref:
        raise ContextPackageError(
            f"--session-id 給的是 {session_id}，但起點包預留的是 {reserved_ref}。"
            "兩者必須一樣：`agora checkout` 已經把接續 Link 與那筆預留記進 Agora，"
            "換一個 id 匯入進去，那筆預留就永遠沒有對應的 Session"
            "（一筆沒有人負責的空 session）。"
            "要換 id，請在 checkout 時就用 --new-session-id 指定，"
            "讓它預留你要的那一個。"
        )
    session_ref = session_id or reserved_ref
    # Agora 的 Session id 是 `<source>:<native>`；opencode 的匯出檔用的是
    # 只有 `<native>` 的形式（`info.id`）。收件匣與同步器那邊認的是完整形式，
    # 所以完整形式留在 `package.json`，匯出時去掉前綴。
    source = str(new_session.get("source") or "opencode")
    target = session_ref.split(":", 1)[1] if ":" in session_ref else session_ref

    # 先把每一段解析、截斷（還沒動任何東西），再決定時間位移，最後才重編 id。
    # 順序有意義：位移要先於重編，因為重編出來的 id 帶著 `time.created`。
    payloads: list[dict] = []
    kept: list[list[dict]] = []
    for position, (segment, raw) in enumerate(zip(data["segments"], raws)):
        if segment.get("source") != new_session.get("source"):
            # 跨來源的片段沒有辦法「原封不動」重建開頭（ADR 0010）。期 1 只做
            # 同應用，明確拒絕而不是給一個開頭已被改寫的 session。
            raise AdapterError(
                f"第 {position + 1} 段是 {segment.get('source')!r}，"
                f"但新 session 是 {new_session.get('source')!r}："
                "跨來源應用要改走共通閱讀版轉換（開頭會被改寫、KV cache 會斷），"
                "期 1 只支援同來源。請把兩邊都換成同一個來源應用再接續。"
            )
        payload = _loads(raw, segment)
        payloads.append(payload)
        kept.append(truncate_to(payload, segment.get("message_id")))

    # n→1：後段的時間整體往後排到第一段之後（第一段原封不動）
    offsets = merge_offsets(data, [_segment_span(m) for m in kept])
    kept = [shift_times(messages, offset) if offset else messages
            for messages, offset in zip(kept, offsets)]
    _verify_merged_order(payloads, kept)

    segments: list[list[dict]] = []
    ordinal = 0
    for position, (payload, messages) in enumerate(zip(payloads, kept)):
        # 每段各自重編，並給每一段不同的鹽（同一個新 session 裡兩段不能撞 id）；
        # `first_ordinal` 讓序號在整份匯出裡遞增，id 字典序 = 匯出順序。
        rebuilt = reidentify(payload, messages, session_id=target,
                             tag=_segment_salt(tag, target, position),
                             first_ordinal=ordinal)["messages"]
        segments.append(rebuilt)
        ordinal += len(rebuilt)

    merged = chain_parents(segments)
    if not merged:
        raise AdapterError("起點包沒有任何訊息，沒有東西可以載入")

    # 最終形狀：info 取第一段的（同一個來源、同一次匯出），messages 是串接結果。
    first = _loads(raws[0], data["segments"][0])
    out = dict(first)
    info = dict(first.get("info") or {})
    info["id"] = target
    out["info"] = info
    out["messages"] = merged
    if decisions is not None:
        decisions["time_shift_ms"] = list(offsets)
    return out, target, len(segments), source


def _verify_merged_order(payloads: Sequence[dict],
                        kept: Sequence[Sequence[dict]]) -> None:
    """確認兩件事：第一段一個位元組都沒動、合併後的時間順序與陣列順序一致。

    這是「最長的一段放最前面」與「第一段原封不動」的守門。opencode 不會提醒
    順序被改掉（它只照 `time.created` 排），所以錯了要靠這裡擋。
    """
    if not kept:
        return
    # 截斷永遠是「取前 k 則」，所以第一段該等於解析結果的前 k 則。
    source_first = list(payloads[0].get("messages") or [])[:len(kept[0])]
    if [dict(m) for m in source_first] != [dict(m) for m in kept[0]]:
        raise AdapterError(
            "第一段被改動了：第一段必須原封不動（ADR 0010 的位元組相同），"
            "只有後面的段落可以往後排。"
        )
    merged = [_time_ms((m.get("info") or {}), 0)
              for messages in kept for m in messages]
    for index in range(1, len(merged)):
        if merged[index] < merged[index - 1]:
            raise AdapterError(
                f"合併後第 {index + 1} 則的時間（{merged[index]}）比前一則"
                f"（{merged[index - 1]}）早：opencode 依 time.created 排序，"
                "這樣匯出與送模型的上下文會跟起點包的段落順序不一致。"
                "請回報這個起點包（`agora checkout` 的時間位移沒生效）。"
            )


def _loads(raw: bytes, segment: Any) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ContextPackageError(
            f"起點包裡的原始紀錄不是合法 JSON（{segment.get('raw_file')}）：{e}"
        ) from None
    if not isinstance(payload, dict):
        raise AdapterError("原始紀錄的最外層必須是 JSON 物件")
    return payload


def _segment_salt(tag: str | None, session_id: str, position: int) -> str:
    """第 `position` 段用的鹽（`_SALT_HEX` 碼十六進位）。

    單段時就是 `salt_for(session_id)`（重跑 `load` 因此是同一套 id，匯入 no-op）；
    n→1 時每段加進自己的位置，同一個新 session 裡兩段不會撞 id。
    """
    base = tag if tag else salt_for(session_id)
    if position == 0:
        return base
    mixed = hashlib.sha256(
        f"{base}|{session_id}|{position}".encode("utf-8")).hexdigest()
    return mixed[:_SALT_HEX]


def run_import(payload: dict, *, workdir: Path,
               runner: Callable[[Sequence[str], Path], Any] | None = None
               ) -> str:
    """在 `workdir` 執行 `opencode import`（spike Q1-2：必須在目標專案目錄）。

    `runner` 供測試注入；預設真的呼叫 `opencode`。
    """
    with tempfile.TemporaryDirectory(prefix="agora-opencode-") as td:
        export_path = Path(td) / "import.json"
        export_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        argv = ["opencode", "import", str(export_path)]
        if runner is not None:
            result = runner(argv, workdir)
            return str(result)
        proc = subprocess.run(  # noqa: S603 - argv 固定形狀，沒有使用者輸入
            argv, cwd=str(workdir), capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise AdapterError(
                f"opencode import 失敗 (rc={proc.returncode}): "
                f"{proc.stderr.strip()[-500:]}"
            )
        return (proc.stdout or "").strip()


def load(package_dir: Path, *, workdir: Path,
         runner: Callable[[Sequence[str], Path], Any] | None = None,
         session_id: str | None = None) -> LoadResult:
    """`agora-opencode load <起點包>`：載入成原生 session，回傳新 session id。"""
    decisions: dict = {}
    payload, target, segments, source = build_export(
        Path(package_dir), session_id=session_id, decisions=decisions)
    output = run_import(payload, workdir=Path(workdir), runner=runner)
    return LoadResult(
        session_id=target,
        source=source,
        messages=len(payload["messages"]),
        segments=segments,
        import_output=output,
        time_shift_ms=tuple(decisions.get("time_shift_ms") or ()),
    )
