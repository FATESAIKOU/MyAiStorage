"""AiStorage Claude Code 原始紀錄轉換器 (ClaudeCodeConverter)。

輸入是 Claude Code 的 session jsonl（一行一個 JSON 物件）。形狀依 review-g3f.md
第一節的統計（該節只有型態計數與鍵名集合，沒有任何內容）：紀錄帶 `uuid`／
`parentUuid` 構成樹狀分支、`isSidechain` 標示子代理、`message.content` 為字串或
內容區塊清單；同一個 API 訊息（`message.id`）會被拆成 1〜5 筆紀錄。

依據規格：
- docs/impl/group3-modules.md 第 5 節（source 名稱 `claude-code`，PM 決定 1）
- schemas/reading-version.md 轉換對應表「Claude Code」那一段
- schemas/reading-version.schema.json
- design D4（格式轉換集中在提交流程）、D10（接續點釘在快照上）
- review-g3f.md H1〜H3、M1〜M3、M5、L

沿用 opencode.py 的作風與界線：
- 不捏造雜湊：沒有 base64 位元組就不產生 sha256，改成明確標示的附件文字。
- 結構真的壞掉（user/assistant/system 缺 uuid 或時間、父節點斷鏈、環）才 raise
  ConversionError（提交流程照收原始紀錄，只把 `reading_status` 標為 failed）。
- 書記性紀錄（attachment、turn_duration、沒有 uuid 的中繼資料）不製造雜訊：
  丟棄時仍留在樹裡維持鏈的連續，只是不輸出成訊息。
- `snapshot_sha256` 由轉換器自原始位元組自行計算，傳入值只用於交叉檢查。
- 摘要以 Unicode code point 截斷於 4,000（含結尾 `…`）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from aistorage.converters.base import ConversionError, Converter, SessionFacts
from aistorage.reading import validate_reading

MAX_SUMMARY = 4000

#: 提供標題的紀錄型態，依優先順序（M1）：目前版本是 custom-title／ai-title，
#: `summary` 是舊版格式，相容保留。
TITLE_RECORD_TYPES = ("custom-title", "ai-title", "summary")

#: 閱讀版可表達的角色，對應 jsonl 的 `type`；`attachment` 另見下方處理表。
#: 會形成閱讀版節點的紀錄型態就是這四個，其餘（queue-operation、last-prompt、mode、
#: permission-mode、atis-latch、agent-name、file-history-snapshot、
#: file-history-delta、cost-state…）在實際檔案裡都沒有 uuid，屬於樹外的中繼資料，
#: 直接略過（review-g3f H1）。
MESSAGE_TYPES = ("user", "assistant", "system")

#: 對話壓縮的邊界標記：`type: "system"` 且 `subtype` 為此值。
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

#: 純書記性的 system subtype：丟棄（review-g3f M2；turn_duration 沒有 content，
#: 留下來只會變成空白的 system 訊息）。
DROP_SYSTEM_SUBTYPES = frozenset({"turn_duration"})

#: 有意義、要轉成文字段落的 attachment 內部型態（review-g3f M2）。
KEEP_ATTACHMENT_SUBTYPES = frozenset({"queued_command", "edited_text_file"})

#: 要丟棄的 attachment 內部型態；hook_success 可能含有 hook 輸出，丟棄以免
#: 內容外流到讀取視圖。
DROP_ATTACHMENT_SUBTYPES = frozenset({
    "total_tokens_reminder",
    "silent_turn_reminder",
    "skill_listing",
    "environment",
    "hook_success",
})

#: 以前綴判定的丟棄型態（實測 deferred_tools_record，未來可能加後綴）。
DROP_ATTACHMENT_SUBTYPE_PREFIXES = ("deferred_tools_",)

#: 只有這些工具才可能是子代理的啟動者，才會填 `child_session_id`。
SUBAGENT_TOOL_NAMES = frozenset({"Task", "Agent"})


def _load_jsonl(raw_bytes: bytes) -> tuple[list[dict], bool]:
    """把 jsonl 位元組解析成 JSON 物件清單。

    回傳 `(records, truncated_tail)`。`truncated_tail` 表示最後一行寫到一半：
    生成中的 Session 最後一行可能是不完整的 JSON（D4 的定期匯出），略過該行
    並視為仍在生成（review-g3f H3）。其他任何一行解析失敗都是 ConversionError。
    """
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConversionError(f"原始紀錄不是合法的 UTF-8: {e}") from None

    lines = text.splitlines()
    last_index = -1
    for i, line in enumerate(lines):
        if line.strip():
            last_index = i
    ends_with_newline = text.endswith("\n")

    records: list[dict] = []
    truncated_tail = False
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception as e:
            if lineno - 1 == last_index and not ends_with_newline:
                truncated_tail = True
                break
            raise ConversionError(f"第 {lineno} 行 JSON 解析失敗: {e}") from None
        if not isinstance(obj, dict):
            raise ConversionError(f"第 {lineno} 行必須是 JSON 物件")
        records.append(obj)

    if not records:
        raise ConversionError("原始紀錄沒有任何內容")
    return records, truncated_tail


def _format_timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    """把來源時間值轉成 RFC 3339 UTC 字串與 datetime；無法解析一律 ConversionError。"""
    dt: datetime | None = None
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            dt = None
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        dt = datetime.fromtimestamp(float(value) / 1000.0, timezone.utc)

    if dt is None:
        raise ConversionError(f"{field} 缺少有效的時間值: {value!r}")

    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        frac = f"{dt.microsecond:06d}".rstrip("0")
        return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{frac}Z", dt
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt


def _format_datetime(dt: datetime) -> str:
    """把 datetime 轉成 RFC 3339 UTC 字串（有小數部分時保留最多 6 位）。"""
    if dt.microsecond:
        frac = f"{dt.microsecond:06d}".rstrip("0")
        return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{frac}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate_summary(val: Any, max_codepoints: int = MAX_SUMMARY) -> str:
    """擷取摘要內容，以 Unicode code point 為準，上限 4,000 字元，超過時截斷並標示 …。"""
    if isinstance(val, (dict, list)):
        s = json.dumps(val, sort_keys=True, ensure_ascii=False)
    elif val is None:
        s = ""
    else:
        s = str(val)

    if len(s) > max_codepoints:
        s = s[: max_codepoints - 1] + "…"
    return s


def _format_session_id(value: Any, *, source: str) -> str | None:
    """把來源端的 Session id 補上 `<source>:` 前綴；已是完整識別碼則原樣使用。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s if ":" in s else f"{source}:{s}"


def _content_text(value: Any) -> str:
    """把 tool_result 的 content（字串或區塊清單）壓成純文字摘要。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                pieces.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                pieces.append(str(block.get("text") or ""))
            elif btype == "image":
                source = block.get("source")
                media = "unknown"
                size: int | None = None
                if isinstance(source, dict) and isinstance(source.get("data"), str):
                    media = str(source.get("media_type") or "unknown")
                    try:
                        # 與 _media_part 一致使用嚴格模式
                        size = len(base64.b64decode(source["data"], validate=True))
                    except Exception:
                        size = None
                if size is not None:
                    pieces.append(f"[圖片：{media} {size} bytes]")
                else:
                    pieces.append(f"[圖片：{media}，內容不在匯出中]")
            else:
                pieces.append(f"[未支援的段落型態：{btype}]")
        return "\n".join(p for p in pieces if p)
    if value is None:
        return ""
    return str(value)


def _attachment_text(att: dict) -> str:
    """attachment 的文字內容：整個物件的 compact JSON。

    不猜欄位名：JSON 裡本來就含有檔名、指令與內容，缺的欄位不會被遺漏，也不會
    被寫成不存在的值（review-g3f M2）。
    """
    return json.dumps(att, sort_keys=True, ensure_ascii=False)


def _attachment_disposition(att: dict) -> str:
    """attachment 的處置：`drop`（書記性，丟棄）、`keep`（轉文字）、`mark`（未支援標記）。"""
    sub = att.get("type")
    if not isinstance(sub, str) or not sub.strip():
        return "drop"
    sub = sub.strip()
    if sub in DROP_ATTACHMENT_SUBTYPES or sub.startswith(DROP_ATTACHMENT_SUBTYPE_PREFIXES):
        return "drop"
    if sub in KEEP_ATTACHMENT_SUBTYPES:
        return "keep"
    return "mark"


@dataclass(frozen=True)
class _Node:
    """一則已驗證的訊息紀錄（可能是被丟棄的書記性紀錄，仍留在樹裡維持鏈的連續）。"""

    position: int
    uuid: str
    parent_uuid: str | None
    record_type: str
    role: str                        # 閱讀版的角色
    is_message_type: bool
    is_compact_boundary: bool
    emits: bool                      # 是否輸出成閱讀版訊息
    timestamp: str
    moment: datetime
    session_id: str | None
    is_sidechain: bool
    record: dict

    @property
    def kind(self) -> str:
        """來源端的角色（user／assistant／system），未知名型態視為 system。"""
        return self.record_type if self.is_message_type else "system"

    @property
    def message_id(self) -> str | None:
        """API 訊息識別碼（同一個 id 會被拆成多筆記錄，需合併）。"""
        if not self.is_message_type:
            return None
        message = self.record.get("message")
        if not isinstance(message, dict):
            return None
        mid = message.get("id")
        return mid if isinstance(mid, str) and mid.strip() else None


@dataclass(frozen=True)
class _Merged:
    """合併後的一則閱讀版訊息（同一個 `message.id` 的多筆紀錄）。"""

    nodes: tuple[_Node, ...]
    reverted: bool

    @property
    def head(self) -> _Node:
        return self.nodes[0]

    @property
    def completed(self) -> bool:
        """拆分訊息以任一筆帶有 stop_reason 為準（最後一筆才有真正的結束原因）。"""
        for node in self.nodes:
            if self._record_completed(node):
                return True
        return False

    @staticmethod
    def _record_completed(node: _Node) -> bool:
        if node.kind != "assistant":
            return True
        message = node.record.get("message")
        if not isinstance(message, dict):
            return False
        return message.get("stop_reason") is not None


@dataclass(frozen=True)
class _Analysis:
    """原始紀錄的結構分析結果，供 facts／convert／child_session_ids 共用。"""

    title: str | None
    merged: tuple[_Merged, ...]
    main_uuids: frozenset[str]
    parent_source_id: str
    truncated_tail: bool
    tool_uses: dict[str, tuple[_Node, dict]]
    tool_results: dict[str, tuple[str, bool, dict]]
    sidechain_sessions: dict[str, list[str]]


class ClaudeCodeConverter(Converter):
    """Claude Code session jsonl 至 aistorage.reading/v1 之轉換器。"""

    source: str = "claude-code"

    # ---------------------------------------------------------------- 分析

    def _analyze(self, raw_path: Path, *, parent_source_id: str) -> _Analysis:
        """解析 jsonl、建樹、決定主幹與分支，並索引 tool_use／tool_result 配對。"""
        raw_bytes = Path(raw_path).read_bytes()
        records, truncated_tail = _load_jsonl(raw_bytes)

        titles: dict[str, str] = {}
        nodes: list[_Node] = []
        by_uuid: dict[str, _Node] = {}
        main_children: dict[str, list[_Node]] = {}
        sidechain_sessions: dict[str, list[str]] = {}

        for pos, rec in enumerate(records):
            rtype = rec.get("type")
            rtype_str = rtype.strip() if isinstance(rtype, str) else ""

            # 1. 標題型態：沒有 uuid，先取出來就不進樹
            if rtype_str in TITLE_RECORD_TYPES:
                value = rec.get("title") if rtype_str != "summary" else rec.get("summary")
                if not isinstance(value, str) or not value.strip():
                    value = rec.get("summary") or rec.get("title")
                if isinstance(value, str) and value.strip():
                    titles[rtype_str] = value.strip()  # 同一型態以最後一筆為準
                continue

            # 2. H1：沒有 uuid 而且不是訊息型態 → 樹外的中繼資料，直接略過
            raw_uuid = rec.get("uuid")
            uuid = raw_uuid.strip() if isinstance(raw_uuid, str) else ""
            if not uuid:
                if rtype_str in MESSAGE_TYPES:
                    raise ConversionError(
                        f"第 {pos + 1} 筆 {rtype_str} 紀錄缺少 uuid"
                    )
                continue
            if uuid in by_uuid:
                raise ConversionError(f"第 {pos + 1} 筆紀錄之 uuid 重複: '{uuid}'")

            # 3. 父子鏈
            raw_parent = rec.get("parentUuid")
            if raw_parent is None:
                parent_uuid = None
            elif isinstance(raw_parent, str) and raw_parent.strip():
                parent_uuid = raw_parent.strip()
            else:
                raise ConversionError(
                    f"紀錄 '{uuid}' 的 parentUuid 型態無效: {raw_parent!r}"
                )

            is_message_type = rtype_str in MESSAGE_TYPES
            ts, moment = _format_timestamp(
                rec.get("timestamp"), field=f"紀錄 '{uuid}' 的 timestamp"
            )
            subtype = rec.get("subtype")
            subtype_str = subtype.strip() if isinstance(subtype, str) else ""
            is_compact_boundary = (
                rtype_str == "system" and subtype_str == COMPACT_BOUNDARY_SUBTYPE
            )

            role, emits = self._role_and_emits(rec, rtype_str, is_message_type, subtype_str)

            sid = rec.get("sessionId")
            node = _Node(
                position=pos,
                uuid=uuid,
                parent_uuid=parent_uuid,
                record_type=rtype_str or "unknown",
                role=role,
                is_message_type=is_message_type,
                is_compact_boundary=is_compact_boundary,
                emits=emits,
                timestamp=ts,
                moment=moment,
                session_id=str(sid).strip() if isinstance(sid, str) and sid.strip() else None,
                is_sidechain=rec.get("isSidechain") is True,
                record=rec,
            )
            by_uuid[uuid] = node

            if node.is_sidechain:
                # 子代理紀錄不進入本 Session 的閱讀版，只用來認出子 Session。
                if parent_uuid and node.session_id:
                    bucket = sidechain_sessions.setdefault(parent_uuid, [])
                    if node.session_id not in bucket:
                        bucket.append(node.session_id)
            else:
                nodes.append(node)
                if parent_uuid:
                    main_children.setdefault(parent_uuid, []).append(node)

        ordered, main_uuids = self._order_nodes(nodes, by_uuid, main_children)
        merged = self._merge_messages(ordered, main_uuids)

        tool_uses: dict[str, tuple[_Node, dict]] = {}
        tool_results: dict[str, tuple[str, bool, dict]] = {}
        for node in nodes:  # 配對以全部主幹與分支紀錄為範圍
            for block in self._blocks_of(node):
                btype = block.get("type")
                if btype == "tool_use":
                    tid = block.get("id")
                    if not isinstance(tid, str) or not tid.strip():
                        raise ConversionError(
                            f"紀錄 '{node.uuid}' 的 tool_use 區塊缺少 id"
                        )
                    tool_uses.setdefault(tid.strip(), (node, block))
                elif btype == "tool_result":
                    tid = block.get("tool_use_id")
                    if not isinstance(tid, str) or not tid.strip():
                        raise ConversionError(
                            f"紀錄 '{node.uuid}' 的 tool_result 區塊缺少 tool_use_id"
                        )
                    is_error = block.get("is_error") is True
                    text = _content_text(block.get("content"))
                    tool_results.setdefault(tid.strip(), (text, is_error, node.record))

        return _Analysis(
            title=self._pick_title(titles),
            merged=merged,
            main_uuids=main_uuids,
            parent_source_id=parent_source_id,
            truncated_tail=truncated_tail,
            tool_uses=tool_uses,
            tool_results=tool_results,
            sidechain_sessions=sidechain_sessions,
        )

    @staticmethod
    def _pick_title(titles: dict[str, str]) -> str | None:
        """標題優先順序：custom-title → ai-title → summary（review-g3f M1）。"""
        for key in TITLE_RECORD_TYPES:
            value = titles.get(key)
            if value:
                return value
        return None

    def _role_and_emits(
        self, rec: dict, rtype: str, is_message_type: bool, subtype: str
    ) -> tuple[str, bool]:
        """決定節點在閱讀版的角色，以及是否輸出成訊息。"""
        if not is_message_type:
            if rtype == "attachment":
                att = rec.get("attachment")
                att = att if isinstance(att, dict) else {}
                return "system", _attachment_disposition(att) != "drop"
            # PM 決定 3：未知名態有 uuid 就輸出標記段落，不讓整份閱讀版失敗
            return "system", True

        if rtype == "system":
            # 純書記性的 system 紀錄（turn_duration 沒有 content）丟棄
            return "system", subtype not in DROP_SYSTEM_SUBTYPES

        # M3：壓縮後的摘要與本機命令說明都不是使用者發言，改以 system 角色呈現
        if rec.get("isCompactSummary") is True or rec.get("isMeta") is True:
            return "system", True
        return rtype, True

    def _order_nodes(
        self,
        nodes: list[_Node],
        by_uuid: dict[str, _Node],
        main_children: dict[str, list[_Node]],
    ) -> tuple[tuple[_Node, ...], frozenset[str]]:
        """依「沿最新葉節點展開」決定順序：主幹在前，其餘分支保留在後。

        葉節點只在 user／assistant／system 紀錄之間挑選（PM 決定 3）：書記性紀錄
        不該讓主幹末端跑掉，那會把整段對話誤判成被捨棄的分支。
        """
        if not nodes:
            return (), frozenset()

        leaves = [
            n
            for n in nodes
            if n.is_message_type
            and not any(c.is_message_type for c in main_children.get(n.uuid, ()))
        ]
        tips = [n for n in nodes if n.is_message_type]
        if tips and not leaves:
            raise ConversionError("原始紀錄的父子結構無法決定葉節點（可能形成環）")
        if not leaves:
            # 沒有可當主幹的訊息：全部保留，但都視為分支。
            return tuple(nodes), frozenset()

        leaf = max(leaves, key=lambda n: (n.moment, n.position))

        chain: list[_Node] = []
        seen: set[str] = set()
        cur = leaf
        while True:
            if cur.uuid in seen:
                raise ConversionError(f"原始紀錄的 parentUuid 形成環: '{cur.uuid}'")
            seen.add(cur.uuid)
            chain.append(cur)
            link = self._parent_link(cur)
            if link is None:
                break
            parent = by_uuid.get(link)
            if parent is None or parent.is_sidechain:
                raise ConversionError(
                    f"紀錄 '{cur.uuid}' 的父節點 '{link}' 找不到對應的主幹紀錄"
                )
            cur = parent
        chain.reverse()

        # 書記性紀錄（attachment、turn_duration…）不參與末梢選舉，但它們通常就接在
        # 對話之後；從末梢往前把這條鏈補回來，否則它們會被誤判成分支而標成
        # reverted（review-g3f M2）。
        chain_uuids = {n.uuid for n in chain}
        tail = chain[-1]
        while True:
            children = main_children.get(tail.uuid, ())
            if not children:
                break
            nxt = max(children, key=lambda n: (n.moment, n.position))
            if nxt.uuid in chain_uuids:
                raise ConversionError(f"原始紀錄的 parentUuid 形成環: '{nxt.uuid}'")
            chain_uuids.add(nxt.uuid)
            chain.append(nxt)
            tail = nxt

        main_uuids = frozenset(chain_uuids)
        branches = [n for n in nodes if n.uuid not in main_uuids]
        return tuple(chain) + tuple(branches), main_uuids

    @staticmethod
    def _parent_link(node: _Node) -> str | None:
        """往上走的父節點。

        壓縮邊界標記的 `parentUuid` 是 null，邏輯上的前一則在 `logicalParentUuid`
        ；照 parentUuid 停下來的話，壓縮之前的全部歷史都會被誤判成分支
        （review-g3f H2）。
        """
        if node.parent_uuid:
            return node.parent_uuid
        if node.is_compact_boundary:
            logical = node.record.get("logicalParentUuid")
            if isinstance(logical, str) and logical.strip():
                return logical.strip()
        return None

    @staticmethod
    def _merge_messages(
        ordered: tuple[_Node, ...], main_uuids: frozenset[str]
    ) -> tuple[_Merged, ...]:
        """過濾不輸出的書記性紀錄，並把同一個 `message.id` 的多筆紀錄合併。

        實際檔案會把一個 API 訊息拆成 1〜5 筆紀錄（每筆一個區塊），它們在樹裡是
        相鄰的鏈；合併後才是一則閱讀版訊息。合併只在相鄰且同為主幹或同為分支時
        進行，避免跨邊界混進去。
        """
        merged: list[_Merged] = []
        run: list[_Node] = []

        def flush() -> None:
            if run:
                merged.append(_Merged(nodes=tuple(run), reverted=reverted))
                run.clear()

        reverted = False
        for node in ordered:
            if not node.emits:
                continue
            node_reverted = node.uuid not in main_uuids
            mid = node.message_id
            if run and (node_reverted != reverted or mid is None or mid != run[-1].message_id):
                flush()
            if not run:
                reverted = node_reverted
            run.append(node)
        flush()
        return tuple(merged)

    def _blocks_of(self, node: _Node) -> list[dict]:
        """取出一則紀錄的內容區塊清單；user 的字串內容視為單一文字區塊。"""
        if not node.is_message_type:
            return []

        if node.record_type == "system":
            content = node.record.get("content")
            return [{"type": "text", "text": content}] if isinstance(content, str) else []

        message = node.record.get("message")
        if not isinstance(message, dict):
            raise ConversionError(f"紀錄 '{node.uuid}' 缺少 message 物件")

        declared_role = message.get("role")
        if isinstance(declared_role, str) and declared_role != node.record_type:
            raise ConversionError(
                f"紀錄 '{node.uuid}' 的 type ('{node.record_type}') 與 message.role "
                f"('{declared_role}') 不一致"
            )

        content = message.get("content")
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if isinstance(content, list):
            blocks: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    raise ConversionError(f"紀錄 '{node.uuid}' 的內容區塊必須是 JSON 物件")
                blocks.append(block)
            return blocks
        raise ConversionError(f"紀錄 '{node.uuid}' 的 message.content 必須是字串或清單")

    # ---------------------------------------------------------------- facts

    def facts(self, raw_path: Path, *, session_id: str | None = None) -> SessionFacts:
        """自 Claude Code jsonl 提取 SessionFacts。

        `session_id` 應由呼叫端明確傳入（review-g3f L3）；未傳入不推測。
        """
        analysis = self._analyze(raw_path, parent_source_id=self._source_id(session_id))

        merged = analysis.merged
        message_ids = tuple(m.head.uuid for m in merged)
        moments = [m.head.moment for m in merged]

        created_at = _format_datetime(min(moments)) if moments else None
        updated_at = _format_datetime(max(moments)) if moments else None

        # Claude Code 的 jsonl 沒有封存標記：停止中只由來源端或同步器明確宣告。
        archived_at: str | None = None
        archived_ms: int | None = None
        last_message_ms = int(max(moments).timestamp() * 1000) if moments else None

        return SessionFacts(
            title=analysis.title,
            created_at=created_at,
            updated_at=updated_at,
            message_ids=message_ids,
            archived_at=archived_at,
            last_message_at=updated_at,
            in_progress=self._in_progress(analysis),
            archived_ms=archived_ms,
            last_message_ms=last_message_ms,
        )

    def child_session_ids(self, raw_path: Path, *, session_id: str | None = None) -> tuple[str, ...]:
        """掃描 Task／Agent 工具調用所啟動的子代理 Session ID。

        `session_id` 應由呼叫端明確傳入（review-g3f L3）；未傳入不推測。
        """
        analysis = self._analyze(raw_path, parent_source_id=self._source_id(session_id))
        children: list[str] = []
        cursor: dict[str, int] = {}
        for merged in analysis.merged:
            for node in merged.nodes:
                if node.kind != "assistant":
                    continue
                for block in self._blocks_of(node):
                    if block.get("type") != "tool_use":
                        continue
                    child = self._resolve_child_session_id(node, block, analysis, cursor)
                    if child and child not in children:
                        children.append(child)
        return tuple(children)

    # -------------------------------------------------------------- convert

    def convert(
        self,
        raw_path: Path,
        *,
        session_id: str,
        parent_id: str | None = None,
        snapshot_sha256: str | None = None,
    ) -> dict:
        """把 Claude Code jsonl 轉換成 aistorage.reading/v1 閱讀版。"""
        raw_p = Path(raw_path)
        raw_bytes = raw_p.read_bytes()

        # snapshot_sha256 由轉換器自原始位元組計算（唯一來源）；傳入值只用於交叉檢查。
        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest().lower()
        if snapshot_sha256 is not None and snapshot_sha256.lower() != actual_sha256:
            raise ConversionError(
                f"傳入之 snapshot_sha256 ({snapshot_sha256}) 與檔案本體計算之 "
                f"SHA-256 ({actual_sha256}) 不符"
            )

        analysis = self._analyze(
            raw_p, parent_source_id=self._source_id(session_id)
        )

        final_parent_id = _format_session_id(parent_id, source=self.source)
        cursor: dict[str, int] = {}
        converted_messages: list[dict[str, Any]] = []

        for index, merged in enumerate(analysis.merged):
            parts: list[dict] = []
            for node in merged.nodes:
                parts.extend(self._convert_parts(node, analysis, cursor))
            if not parts:
                parts = [{"type": "text", "text": ""}]
            converted_messages.append({
                "message_id": merged.head.uuid,
                "index": index,
                "role": merged.head.role,
                "created_at": merged.head.timestamp,
                "completed": merged.completed,
                "reverted": merged.reverted,
                "parts": parts,
            })

        reading: dict[str, Any] = {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": self.source,
            "title": analysis.title,
            "parent_id": final_parent_id,
            "snapshot_sha256": actual_sha256,
            "in_progress": self._in_progress(analysis),
            "messages": converted_messages,
        }

        errors = validate_reading(reading)
        if errors:
            err_msg = "; ".join(f"{e.field}: {e.message}" for e in errors)
            raise ConversionError(f"轉換後閱讀版未通過規格驗證: {err_msg}")
        return reading

    # ---------------------------------------------------------------- 細部

    @staticmethod
    def _source_id(session_id: str | None) -> str:
        """來源端自己的 Session id（`<source>:<source_session_id>` 的後半段）。

        review-g3f L3：呼叫端應明確傳入。沒有傳入時回傳空字串，**不推測**
        （原本會用「最常見的 sessionId」推測，那在主幹與子代理紀錄數量接近時
        會猜錯）；此時只是不啟用「子代理不得指向自己」的防護。
        """
        if session_id is None:
            return ""
        if not isinstance(session_id, str) or not session_id.strip():
            raise ConversionError("session_id 必須是 <source>:<source_session_id>")
        s = session_id.strip()
        return s.split(":", 1)[1] if ":" in s else ""

    @staticmethod
    def _in_progress(analysis: _Analysis) -> bool:
        """仍在生成中：最後一行寫到一半，或最後一則訊息尚未生成完畢。"""
        if analysis.truncated_tail:
            return True
        if analysis.merged:
            last = analysis.merged[-1]
            if last.head.kind == "assistant" and not last.completed:
                return True
        return False

    def _convert_parts(
        self,
        node: _Node,
        analysis: _Analysis,
        cursor: dict[str, int],
    ) -> list[dict]:
        # 書記性的 attachment：丟棄或轉成文字段落（review-g3f M2）
        if not node.is_message_type and node.record_type == "attachment":
            return self._attachment_parts(node)

        # PM 決定 3：未知的紀錄型態不讓整份閱讀版失敗，輸出明確標記。
        if not node.is_message_type:
            return [{"type": "text", "text": f"[未支援的紀錄型態：{node.record_type}]"}]

        # 對話壓縮的邊界標記：轉成空的 compaction 段落（PM 決定 2）。
        if node.is_compact_boundary:
            return [{"type": "compaction", "summary": ""}]

        # M3：壓縮後的摘要是機器寫的，不是使用者發言。
        if node.record_type == "user" and node.record.get("isCompactSummary") is True:
            message = node.record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            return [{
                "type": "compaction",
                "summary": _content_text(content),
            }]

        parts: list[dict] = []
        for block in self._blocks_of(node):
            btype = block.get("type")

            if btype == "text":
                parts.append({"type": "text", "text": str(block.get("text") or "")})

            elif btype == "thinking":
                parts.append({"type": "reasoning", "text": str(block.get("thinking") or "")})

            elif btype == "tool_use":
                parts.append(self._tool_call_part(node, block, analysis, cursor))

            elif btype == "tool_result":
                # 已配對者合併進 assistant 的 tool_call，這裡不重複輸出。
                if str(block.get("tool_use_id") or "") in analysis.tool_uses:
                    continue
                tid = block.get("tool_use_id")
                text = _truncate_summary(_content_text(block.get("content")))
                if block.get("is_error") is True and not text.startswith("[ERROR]"):
                    text = f"[ERROR] {text}"
                parts.append({
                    "type": "text",
                    "text": f"[未配對的工具結果：{tid}] {text}",
                })

            elif btype in ("image", "document"):
                parts.append(self._media_part(node, block, btype))

            else:
                parts.append({"type": "text", "text": f"[未支援的段落型態：{btype}]"})

        return parts

    def _attachment_parts(self, node: _Node) -> list[dict]:
        att = node.record.get("attachment")
        att = att if isinstance(att, dict) else {}
        disposition = _attachment_disposition(att)
        if disposition == "drop":
            return []
        sub = att.get("type")
        sub = sub.strip() if isinstance(sub, str) and sub.strip() else "(無型態)"
        if disposition == "mark":
            return [{"type": "text", "text": f"[未支援的附件型態：{sub}]"}]
        return [{
            "type": "text",
            "text": f"[{sub}] {_truncate_summary(_attachment_text(att))}",
        }]

    def _tool_call_part(
        self,
        node: _Node,
        block: dict,
        analysis: _Analysis,
        cursor: dict[str, int],
    ) -> dict:
        name = block.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConversionError(f"紀錄 '{node.uuid}' 的 tool_use 區塊缺少 name")
        name = name.strip()

        tid = str(block.get("id") or "").strip()
        raw_input = block.get("input")
        in_summary = _truncate_summary(raw_input)

        matched = analysis.tool_results.get(tid)
        if matched is None:
            out_summary = ""
        else:
            text, is_error, _record = matched
            out_summary = _truncate_summary(text)
            if is_error and not out_summary.startswith("[ERROR]"):
                out_summary = _truncate_summary(f"[ERROR] {out_summary}")

        return {
            "type": "tool_call",
            "name": name,
            "input_summary": in_summary,
            "output_summary": out_summary,
            "child_session_id": self._resolve_child_session_id(
                node, block, analysis, cursor
            ),
        }

    @staticmethod
    def _media_part(node: _Node, block: dict, kind: str) -> dict:
        """Base64 圖片／文件算出 sha256 與大小；沒有 base64 位元組就不捏造雜湊。"""
        source = block.get("source")
        if not isinstance(source, dict):
            raise ConversionError(f"紀錄 '{node.uuid}' 的 {kind} 區塊缺少 source 物件")

        media_type = source.get("media_type")
        if not isinstance(media_type, str) or not media_type.strip():
            raise ConversionError(
                f"紀錄 '{node.uuid}' 的 {kind} 區塊缺少 media_type"
            )
        media_type = media_type.strip().lower()

        raw_name = block.get("title") or block.get("name") or source.get("title")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None

        if source.get("type") != "base64":
            display = name if name else "（無檔名）"
            return {
                "type": "text",
                "text": f"[附件：{display} {media_type}，內容不在匯出中]",
            }

        data = source.get("data")
        if not isinstance(data, str) or not data.strip():
            raise ConversionError(f"紀錄 '{node.uuid}' 的 {kind} 區塊缺少 base64 資料")
        try:
            content = base64.b64decode(data, validate=True)
        except Exception as e:
            raise ConversionError(
                f"紀錄 '{node.uuid}' 的 {kind} 區塊 base64 解碼失敗: {e}"
            ) from None

        sha256 = hashlib.sha256(content).hexdigest().lower()
        size = len(content)
        if kind == "image":
            return {
                "type": "image",
                "media_type": media_type,
                "sha256": sha256,
                "size": size,
            }
        return {
            "type": "file",
            "media_type": media_type,
            "sha256": sha256,
            "size": size,
            "name": name if name else "（無檔名）",
        }

    def _resolve_child_session_id(
        self,
        node: _Node,
        block: dict,
        analysis: _Analysis,
        cursor: dict[str, int],
    ) -> str | None:
        """解析子代理 Session 識別碼；無法確證時回傳 null（不捏造）。

        只採結構化的來源（`toolUseResult` 的欄位、tool_use input 的欄位、
        parentUuid 指向本則 assistant 的子代理紀錄），不從 tool_result 的文字
        內容推測（review-g3f L2）。
        """
        if str(block.get("name") or "").strip() not in SUBAGENT_TOOL_NAMES:
            return None

        candidates: list[str] = []
        raw_input = block.get("input")
        if isinstance(raw_input, dict):
            for key in ("sessionId", "session_id"):
                val = raw_input.get(key)
                if isinstance(val, str) and val.strip():
                    candidates.append(val.strip())

        # 以 parentUuid 指向本則 assistant 紀錄的子代理紀錄，依序配對第 N 個 Task。
        bucket = analysis.sidechain_sessions.get(node.uuid, [])
        idx = cursor.get(node.uuid, 0)
        cursor[node.uuid] = idx + 1
        if idx < len(bucket):
            candidates.append(bucket[idx])

        tid = str(block.get("id") or "").strip()
        matched = analysis.tool_results.get(tid)
        if matched is not None:
            tool_use_result = matched[2].get("toolUseResult")
            if isinstance(tool_use_result, dict):
                for key in ("sessionId", "session_id"):
                    val = tool_use_result.get(key)
                    if isinstance(val, str) and val.strip():
                        candidates.append(val.strip())

        for cand in candidates:
            if cand == analysis.parent_source_id:
                continue  # 子代理不能指向本 Session 自己
            formatted = _format_session_id(cand, source=self.source)
            if formatted:
                return formatted
        return None
