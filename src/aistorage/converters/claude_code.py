"""AiStorage Claude Code 原始紀錄轉換器 (ClaudeCodeConverter)。

輸入是 Claude Code 的 session jsonl（一行一個 JSON 物件，形狀依公開知識：
每筆紀錄帶 `uuid`／`parentUuid` 構成樹狀分支、`isSidechain` 標示子代理、
`message.content` 為字串或內容區塊清單）。

依據規格：
- docs/impl/group3-modules.md 第 5 節（source 名稱 `claude-code`，PM 決定 1）
- schemas/reading-version.md 轉換對應表「Claude Code」那一段
- schemas/reading-version.schema.json
- design D4（格式轉換集中在提交流程）、D10（接續點釘在快照上）

沿用 opencode.py 的作風與界線：
- 不捏造雜湊：沒有 base64 位元組就不產生 sha256，改成明確標示的附件文字。
- 結構真的壞掉（缺 uuid／時間、父節點斷鏈、環）才 raise ConversionError
  （提交流程照收原始紀錄，只把 `reading_status` 標為 failed）。
- 未知的紀錄型態寬容處理：轉成 `[未支援的紀錄型態：<type>]` 文字段落，不讓
  整份閱讀版失敗（PM 決定 3）。
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
import re
from typing import Any

from aistorage.converters.base import ConversionError, Converter, SessionFacts
from aistorage.reading import validate_reading

MAX_SUMMARY = 4000

#: 非訊息類的紀錄型態：直接丟棄（其中 summary 提供標題）。
DROP_RECORD_TYPES = frozenset({"summary", "file-history-snapshot"})

#: 閱讀版可表達的訊息角色，對應 jsonl 的 `type`。
MESSAGE_TYPES = ("user", "assistant", "system")

#: 對話壓縮的邊界標記（`type: "system"` 且 `subtype` 為此值）：轉成 compaction 段落。
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

#: 只有這些工具才可能是子代理的啟動者，才會填 `child_session_id`。
SUBAGENT_TOOL_NAMES = frozenset({"Task", "Agent"})

#: 子代理識別碼的候選來源：Task 結果尾端的 `<task_metadata>` 區塊。
_TASK_METADATA_RE = re.compile(r"<task_metadata>(.*?)</task_metadata>", re.DOTALL)
_SESSION_ID_IN_TEXT_RE = re.compile(
    r"\bsession[_-]?id\b\s*[:=]\s*([0-9a-fA-F][0-9a-fA-F-]{7,})"
)


def _load_jsonl(raw_bytes: bytes) -> list[dict]:
    """把 jsonl 位元組解析成 JSON 物件清單；空行略過，其餘異常一律 ConversionError。"""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConversionError(f"原始紀錄不是合法的 UTF-8: {e}") from None

    records: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception as e:
            raise ConversionError(f"第 {lineno} 行 JSON 解析失敗: {e}") from None
        if not isinstance(obj, dict):
            raise ConversionError(f"第 {lineno} 行必須是 JSON 物件")
        records.append(obj)

    if not records:
        raise ConversionError("原始紀錄沒有任何內容")
    return records


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
                        size = len(base64.b64decode(source["data"], validate=False))
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


@dataclass(frozen=True)
class _Node:
    """一則已驗證的訊息紀錄。"""

    position: int
    uuid: str
    parent_uuid: str | None
    kind: str
    record_type: str
    is_message_type: bool
    timestamp: str
    moment: datetime
    session_id: str | None
    is_sidechain: bool
    record: dict


@dataclass(frozen=True)
class _Analysis:
    """原始紀錄的結構分析結果，供 facts／convert／child_session_ids 共用。"""

    title: str | None
    ordered: tuple[_Node, ...]
    main_uuids: frozenset[str]
    parent_source_id: str
    tool_uses: dict[str, tuple[_Node, dict]]
    tool_results: dict[str, tuple[str, bool, dict]]
    sidechain_sessions: dict[str, list[str]]


class ClaudeCodeConverter(Converter):
    """Claude Code session jsonl 至 aistorage.reading/v1 之轉換器。"""

    source: str = "claude-code"

    # ---------------------------------------------------------------- 分析

    def _analyze(self, raw_path: Path, *, parent_source_id: str | None = None) -> _Analysis:
        """解析 jsonl、建樹、決定主幹與分支，並索引 tool_use／tool_result 配對。"""
        raw_bytes = Path(raw_path).read_bytes()
        records = _load_jsonl(raw_bytes)

        title: str | None = None
        nodes: list[_Node] = []
        by_uuid: dict[str, _Node] = {}
        main_children: dict[str, list[_Node]] = {}
        sidechain_sessions: dict[str, list[str]] = {}

        for pos, rec in enumerate(records):
            rtype = rec.get("type")
            if rtype in DROP_RECORD_TYPES:
                if rtype == "summary":
                    s = rec.get("summary")
                    if isinstance(s, str) and s.strip():
                        title = s.strip()  # 最後一筆 summary 為準
                continue

            uuid = rec.get("uuid")
            if not isinstance(uuid, str) or not uuid.strip():
                raise ConversionError(f"第 {pos + 1} 筆紀錄缺少 uuid")
            uuid = uuid.strip()
            if uuid in by_uuid:
                raise ConversionError(f"第 {pos + 1} 筆紀錄之 uuid 重複: '{uuid}'")

            parent_uuid = rec.get("parentUuid")
            if parent_uuid is not None:
                if not isinstance(parent_uuid, str) or not parent_uuid.strip():
                    raise ConversionError(
                        f"紀錄 '{uuid}' 的 parentUuid 型態無效: {parent_uuid!r}"
                    )
                parent_uuid = parent_uuid.strip()

            is_sidechain = rec.get("isSidechain") is True
            raw_ts = rec.get("timestamp")
            ts, moment = _format_timestamp(raw_ts, field=f"紀錄 '{uuid}' 的 timestamp")

            # PM 決定 3：未知的紀錄型態寬容處理，以 system 角色與標記段落輸出。
            is_message_type = rtype in MESSAGE_TYPES
            sid = rec.get("sessionId")
            node = _Node(
                position=pos,
                uuid=uuid,
                parent_uuid=parent_uuid,
                kind=str(rtype) if is_message_type else "system",
                record_type=str(rtype),
                is_message_type=is_message_type,
                timestamp=ts,
                moment=moment,
                session_id=str(sid) if isinstance(sid, str) and sid.strip() else None,
                is_sidechain=is_sidechain,
                record=rec,
            )
            by_uuid[uuid] = node

            if is_sidechain:
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

        # 本 Session 在來源端的 id：優先用呼叫端給的，否則取非子代理紀錄中最常見者。
        effective_parent_id = parent_source_id or self._primary_session_id(nodes)

        tool_uses: dict[str, tuple[_Node, dict]] = {}
        tool_results: dict[str, tuple[str, bool, dict]] = {}
        for node in nodes:  # 配對以全部主幹與分支紀錄為範圍
            blocks = self._blocks_of(node)
            for block in blocks:
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
                    tool_results.setdefault(
                        tid.strip(), (text, is_error, node.record)
                    )

        return _Analysis(
            title=title,
            ordered=ordered,
            main_uuids=main_uuids,
            parent_source_id=effective_parent_id,
            tool_uses=tool_uses,
            tool_results=tool_results,
            sidechain_sessions=sidechain_sessions,
        )

    @staticmethod
    def _order_nodes(
        nodes: list[_Node],
        by_uuid: dict[str, _Node],
        main_children: dict[str, list[_Node]],
    ) -> tuple[tuple[_Node, ...], frozenset[str]]:
        """依「沿最新葉節點展開」決定閱讀版順序：主幹在前，其餘分支保留在後。

        葉節點只在 user／assistant／system 紀錄之間挑選（PM 決定 3）：未知的
        紀錄型態不該讓書記性紀錄變成主幹末端，那會把整段對話誤判成被捨棄的分支。
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
            # 整份紀錄都沒有可當主幹的訊息：全部保留，但都視為分支。
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
            if cur.parent_uuid is None:
                break
            parent = by_uuid.get(cur.parent_uuid)
            if parent is None or parent.is_sidechain:
                raise ConversionError(
                    f"紀錄 '{cur.uuid}' 的 parentUuid '{cur.parent_uuid}' "
                    "找不到對應的主幹紀錄"
                )
            cur = parent
        chain.reverse()
        main_uuids = frozenset(n.uuid for n in chain)

        branches = [n for n in nodes if n.uuid not in main_uuids]
        return tuple(chain) + tuple(branches), main_uuids

    @staticmethod
    def _primary_session_id(nodes: list[_Node]) -> str:
        """推測本檔案所屬的 Session id（非子代理紀錄中最常見者，並列時取先出現者）。"""
        counts: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        for node in nodes:
            if not node.session_id:
                continue
            counts[node.session_id] = counts.get(node.session_id, 0) + 1
            first_seen.setdefault(node.session_id, node.position)
        if not counts:
            return ""
        return min(
            counts.items(),
            key=lambda kv: (-kv[1], first_seen[kv[0]]),
        )[0]

    @staticmethod
    def _blocks_of(node: _Node) -> list[dict]:
        """取出一則紀錄的內容區塊清單；user 的字串內容視為單一文字區塊。"""
        if node.kind == "system":
            content = node.record.get("content")
            return [{"type": "text", "text": content}] if isinstance(content, str) else []

        message = node.record.get("message")
        if not isinstance(message, dict):
            raise ConversionError(f"紀錄 '{node.uuid}' 缺少 message 物件")

        declared_role = message.get("role")
        if isinstance(declared_role, str) and declared_role != node.kind:
            raise ConversionError(
                f"紀錄 '{node.uuid}' 的 type ('{node.kind}') 與 message.role "
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

    def facts(self, raw_path: Path) -> SessionFacts:
        """自 Claude Code jsonl 提取 SessionFacts。"""
        analysis = self._analyze(raw_path)

        message_ids = tuple(n.uuid for n in analysis.ordered)
        moments = [n.moment for n in analysis.ordered]

        created_at = _format_datetime(min(moments)) if moments else None
        updated_at = _format_datetime(max(moments)) if moments else None
        last_message_at = updated_at

        # Claude Code 的 jsonl 沒有封存標記：停止中只由來源端或同步器明確宣告。
        archived_at: str | None = None
        archived_ms: int | None = None
        last_message_ms = int(max(moments).timestamp() * 1000) if moments else None

        in_progress = False
        if analysis.ordered:
            last = analysis.ordered[-1]
            in_progress = last.kind == "assistant" and not self._is_completed(last)

        return SessionFacts(
            title=analysis.title,
            created_at=created_at,
            updated_at=updated_at,
            message_ids=message_ids,
            archived_at=archived_at,
            last_message_at=last_message_at,
            in_progress=in_progress,
            archived_ms=archived_ms,
            last_message_ms=last_message_ms,
        )

    def child_session_ids(self, raw_path: Path) -> tuple[str, ...]:
        """掃描 Task／Agent 工具調用所啟動的子代理 Session ID。"""
        analysis = self._analyze(raw_path)
        children: list[str] = []
        cursor: dict[str, int] = {}
        for node in analysis.ordered:
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

        parent_source_id = session_id.split(":", 1)[1] if ":" in session_id else ""
        analysis = self._analyze(raw_p, parent_source_id=parent_source_id)

        final_parent_id = _format_session_id(parent_id, source=self.source)
        cursor: dict[str, int] = {}
        converted_messages: list[dict[str, Any]] = []

        for index, node in enumerate(analysis.ordered):
            parts = self._convert_parts(node, analysis, cursor)
            if not parts:
                parts = [{"type": "text", "text": ""}]
            converted_messages.append({
                "message_id": node.uuid,
                "index": index,
                "role": node.kind,
                "created_at": node.timestamp,
                "completed": self._is_completed(node),
                "reverted": node.uuid not in analysis.main_uuids,
                "parts": parts,
            })

        in_progress = False
        if analysis.ordered:
            last = analysis.ordered[-1]
            in_progress = last.kind == "assistant" and not self._is_completed(last)

        reading: dict[str, Any] = {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": self.source,
            "title": analysis.title,
            "parent_id": final_parent_id,
            "snapshot_sha256": actual_sha256,
            "in_progress": in_progress,
            "messages": converted_messages,
        }

        errors = validate_reading(reading)
        if errors:
            err_msg = "; ".join(f"{e.field}: {e.message}" for e in errors)
            raise ConversionError(f"轉換後閱讀版未通過規格驗證: {err_msg}")
        return reading

    # ---------------------------------------------------------------- 細部

    @staticmethod
    def _is_completed(node: _Node) -> bool:
        """assistant 以 `stop_reason` 判定是否生成完畢；user／system 一律視為完成。"""
        if node.kind != "assistant":
            return True
        message = node.record.get("message")
        if not isinstance(message, dict):
            return False
        return message.get("stop_reason") is not None

    def _convert_parts(
        self,
        node: _Node,
        analysis: _Analysis,
        cursor: dict[str, int],
    ) -> list[dict]:
        # PM 決定 3：未知的紀錄型態不讓整份閱讀版失敗，輸出明確標記。
        if not node.is_message_type:
            return [{"type": "text", "text": f"[未支援的紀錄型態：{node.record_type}]"}]

        # 對話壓縮的邊界標記：轉成空的 compaction 段落（PM 決定 2）。
        if node.kind == "system" and node.record.get("subtype") == COMPACT_BOUNDARY_SUBTYPE:
            return [{"type": "compaction", "summary": ""}]

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
        """解析子代理 Session 識別碼；無法確證時回傳 null（不捏造）。"""
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
            result_record = matched[2]
            tool_use_result = result_record.get("toolUseResult")
            if isinstance(tool_use_result, dict):
                for key in ("sessionId", "session_id"):
                    val = tool_use_result.get(key)
                    if isinstance(val, str) and val.strip():
                        candidates.append(val.strip())
            for meta in _TASK_METADATA_RE.findall(matched[0]):
                found = _SESSION_ID_IN_TEXT_RE.search(meta)
                if found:
                    candidates.append(found.group(1))

        for cand in candidates:
            if cand == analysis.parent_source_id:
                continue  # 子代理不能指向本 Session 自己
            formatted = _format_session_id(cand, source=self.source)
            if formatted:
                return formatted
        return None
