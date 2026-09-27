"""AiStorage opencode 原始紀錄轉換器 (OpencodeConverter)。

依據規格：
- docs/impl/group3-modules.md 第 5 節
- schemas/reading-version.md 轉換對應表
- schemas/reading-version.schema.json
- design D4、D10
- docs/spike/evidence/1.7a〜1.7h 欄位描述
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from aistorage.converters.base import Converter, SessionFacts
from aistorage.reading import validate_reading


def _format_time(val: Any) -> str | None:
    """將時間戳轉換為標準 RFC 3339 UTC 格式字串（YYYY-MM-DDTHH:MM:SSZ）。"""
    if val is None or val == 0 or val == "":
        return None
    if isinstance(val, (int, float)):
        # 判斷是否為毫秒時間戳 (> 1e11)
        if val > 1e11:
            dt = datetime.fromtimestamp(val / 1000.0, timezone.utc)
        else:
            dt = datetime.fromtimestamp(val, timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, str):
        val_clean = val.strip()
        if not val_clean:
            return None
        try:
            dt = datetime.fromisoformat(val_clean.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return val_clean if val_clean.endswith("Z") else f"{val_clean}Z"
    return None


def _truncate_summary(val: Any, max_codepoints: int = 4000) -> str:
    """擷取摘要內容，以 Unicode code point 為準，上限 4,000 字元，超過時截斷並標示 …。"""
    if isinstance(val, (dict, list)):
        s = json.dumps(val, ensure_ascii=False)
    elif val is None:
        s = ""
    else:
        s = str(val)

    if len(s) > max_codepoints:
        s = s[: max_codepoints - 1] + "…"
    return s


class OpencodeConverter(Converter):
    """opencode 會話原始匯出格式至 aistorage.reading/v1 之轉換器。"""

    source: str = "opencode"

    def facts(self, raw_path: Path) -> SessionFacts:
        """自 opencode 原始匯出 JSON 提取 SessionFacts。"""
        with open(raw_path, encoding="utf-8") as f:
            data = json.load(f)

        info = data.get("info", {})
        time_info = info.get("time", {}) if isinstance(info, dict) else {}

        title = info.get("title") if isinstance(info, dict) else None
        created_at = _format_time(time_info.get("created"))
        updated_at = _format_time(time_info.get("updated"))

        archived_val = time_info.get("archived")
        archived_at = _format_time(archived_val) if archived_val and archived_val > 0 else None

        raw_messages = data.get("messages", []) if isinstance(data, dict) else []
        message_ids: list[str] = []
        last_message_at: str | None = None
        in_progress = False

        for idx, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                continue
            m_info = m.get("info", {}) if isinstance(m, dict) else {}
            m_id = str(m_info.get("id") or m.get("id") or f"msg_{idx}")
            message_ids.append(m_id)

            m_time = m_info.get("time", {}) if isinstance(m_info, dict) else {}
            m_created = _format_time(m_time.get("created") if isinstance(m_time, dict) else None)
            if m_created:
                last_message_at = m_created

        # 若最後一則為 assistant 且尚未 completed，則標記 in_progress
        if raw_messages:
            last_m = raw_messages[-1]
            if isinstance(last_m, dict):
                last_info = last_m.get("info", {}) if isinstance(last_m, dict) else {}
                role = last_info.get("role") or last_m.get("role")
                if role == "assistant":
                    last_time = last_info.get("time", {}) if isinstance(last_info, dict) else {}
                    completed = bool(
                        last_time.get("completed") if isinstance(last_time, dict) else last_info.get("completed")
                    )
                    if not completed:
                        in_progress = True

        return SessionFacts(
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            message_ids=tuple(message_ids),
            archived_at=archived_at,
            last_message_at=last_message_at,
            in_progress=in_progress,
        )

    def child_session_ids(self, raw_path: Path) -> tuple[str, ...]:
        """自 opencode 原始紀錄中掃描 task 工具調用所產生的子代理 Session ID。"""
        with open(raw_path, encoding="utf-8") as f:
            data = json.load(f)

        child_ids: list[str] = []
        raw_messages = data.get("messages", []) if isinstance(data, dict) else []

        for m in raw_messages:
            if not isinstance(m, dict):
                continue
            parts = m.get("parts", []) if isinstance(m, dict) else []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                p_type = p.get("type")
                tool_name = p.get("tool") or p.get("name")
                if p_type in ("tool", "tool_call") and tool_name == "task":
                    state = p.get("state", {}) if isinstance(p.get("state"), dict) else {}
                    meta = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
                    cid = meta.get("sessionId") or meta.get("childSessionId") or p.get("child_session_id")
                    if cid:
                        cid_str = str(cid)
                        formatted_id = cid_str if ":" in cid_str else f"opencode:{cid_str}"
                        if formatted_id not in child_ids:
                            child_ids.append(formatted_id)

        return tuple(child_ids)

    def convert(
        self,
        raw_path: Path,
        *,
        session_id: str,
        snapshot_sha256: str,
        parent_id: str | None,
    ) -> dict:
        """轉換 opencode 匯出紀錄至 aistorage.reading/v1 格式。"""
        with open(raw_path, encoding="utf-8") as f:
            data = json.load(f)

        info = data.get("info", {}) if isinstance(data, dict) else {}
        title = info.get("title") if isinstance(info, dict) else None

        # 若未指定 parent_id，則嘗試讀取 info.parentID
        final_parent_id = parent_id
        if final_parent_id is None and isinstance(info, dict):
            raw_parent = info.get("parentID") or info.get("parent_id")
            if raw_parent:
                raw_parent_str = str(raw_parent)
                final_parent_id = (
                    raw_parent_str if ":" in raw_parent_str else f"opencode:{raw_parent_str}"
                )

        # 判定 revert 指標
        revert_target_id: str | None = None
        if isinstance(info, dict):
            revert_obj = info.get("revert")
            if isinstance(revert_obj, dict):
                revert_target_id = revert_obj.get("messageID") or revert_obj.get("message_id")
            elif isinstance(revert_obj, str):
                revert_target_id = revert_obj

        raw_messages = data.get("messages", []) if isinstance(data, dict) else []
        converted_messages: list[dict[str, Any]] = []
        is_reverted_mode = False

        for idx, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                continue
            m_info = m.get("info", {}) if isinstance(m, dict) else {}
            m_id = str(m_info.get("id") or m.get("id") or f"msg_{idx:03d}")

            # 角色
            role = m_info.get("role") or m.get("role") or "user"
            if role not in ("user", "assistant", "system"):
                role = "user"

            # 建立時間
            m_time = m_info.get("time", {}) if isinstance(m_info, dict) else {}
            created_at = _format_time(
                m_time.get("created") if isinstance(m_time, dict) else m.get("created_at")
            )

            # completed
            if role == "assistant":
                completed_val = (
                    m_time.get("completed") if isinstance(m_time, dict) else m_info.get("completed")
                )
                completed = bool(completed_val)
            else:
                completed = True

            # reverted 判定
            if revert_target_id and m_id == revert_target_id:
                is_reverted_mode = True

            # 個別訊息宣告之 reverted
            if m_info.get("reverted") is True or m.get("reverted") is True:
                msg_reverted = True
            elif m_info.get("reverted") is False or m.get("reverted") is False:
                msg_reverted = False
            else:
                msg_reverted = is_reverted_mode

            # 對話摘要標記
            is_summary_msg = bool(m_info.get("summary") or m.get("summary"))

            # 段落轉換
            raw_parts = m.get("parts", []) if isinstance(m, dict) else []
            converted_parts: list[dict[str, Any]] = []

            for p in raw_parts:
                if not isinstance(p, dict):
                    continue
                p_type = p.get("type")

                # (1) step-start / step-finish 丟棄
                if p_type in ("step-start", "step-finish", "step_start", "step_finish"):
                    continue

                # (2) text 段落
                if p_type == "text":
                    text_content = str(p.get("text", ""))
                    if is_summary_msg:
                        converted_parts.append({
                            "type": "compaction",
                            "summary": text_content,
                        })
                    else:
                        converted_parts.append({
                            "type": "text",
                            "text": text_content,
                        })

                # (3) reasoning / thinking 段落
                elif p_type in ("reasoning", "thinking"):
                    converted_parts.append({
                        "type": "reasoning",
                        "text": str(p.get("text", "")),
                    })

                # (4) tool 呼叫與回應
                elif p_type in ("tool", "tool_call"):
                    tool_name = str(p.get("tool") or p.get("name") or "unknown_tool")
                    state = p.get("state", {}) if isinstance(p.get("state"), dict) else {}

                    raw_in = (
                        state.get("input")
                        if "input" in state
                        else p.get("input") if "input" in p else p.get("args", "")
                    )
                    in_summary = _truncate_summary(raw_in, 4000)

                    raw_out = (
                        state.get("output")
                        if "output" in state
                        else p.get("output") if "output" in p else state.get("error", "")
                    )
                    out_summary = _truncate_summary(raw_out, 4000)

                    is_error = state.get("status") == "error" or p.get("status") == "error"
                    if is_error:
                        if not out_summary.startswith("[ERROR]"):
                            out_summary = f"[ERROR] {out_summary}"
                        if len(out_summary) > 4000:
                            out_summary = out_summary[:3999] + "…"

                    # 子代理識別碼
                    child_id: str | None = None
                    if tool_name == "task":
                        meta = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
                        cid = meta.get("sessionId") or meta.get("childSessionId") or p.get("child_session_id")
                        if cid:
                            cid_str = str(cid)
                            child_id = cid_str if ":" in cid_str else f"opencode:{cid_str}"

                    converted_parts.append({
                        "type": "tool_call",
                        "name": tool_name,
                        "input_summary": in_summary,
                        "output_summary": out_summary,
                        "child_session_id": child_id,
                    })

                # (5) image 段落
                elif p_type == "image":
                    m_type = str(
                        p.get("media_type") or p.get("mime_type") or p.get("mime") or "image/png"
                    )
                    if p.get("data") or p.get("base64") or p.get("image_data"):
                        raw_b64 = str(p.get("data") or p.get("base64") or p.get("image_data"))
                        try:
                            img_bytes = base64.b64decode(raw_b64)
                            img_sha = hashlib.sha256(img_bytes).hexdigest().lower()
                            img_size = len(img_bytes)
                        except Exception:
                            img_sha = "0" * 64
                            img_size = 0
                    else:
                        img_sha = str(p.get("sha256") or p.get("hash") or "0" * 64).lower()
                        img_size = int(p.get("size", 0))

                    converted_parts.append({
                        "type": "image",
                        "media_type": m_type,
                        "sha256": img_sha,
                        "size": img_size,
                    })

                # (6) file 附件段落
                elif p_type == "file":
                    m_type = str(
                        p.get("media_type") or p.get("mime_type") or p.get("mime") or "application/octet-stream"
                    )
                    f_sha = str(p.get("sha256") or p.get("hash") or "0" * 64).lower()
                    f_size = int(p.get("size", 0))
                    f_name = str(p.get("name") or p.get("filename") or "unnamed_file")

                    converted_parts.append({
                        "type": "file",
                        "media_type": m_type,
                        "sha256": f_sha,
                        "size": f_size,
                        "name": f_name,
                    })

                # (7) compaction 段落
                elif p_type == "compaction":
                    c_summary = _truncate_summary(
                        p.get("summary") or p.get("text") or p.get("content") or ""
                    )
                    converted_parts.append({
                        "type": "compaction",
                        "summary": c_summary,
                    })

            # 若完全沒有段落（例如僅有 step-start 被丟棄），補一個空的 text 段落確保符合格式
            if not converted_parts:
                converted_parts.append({
                    "type": "text",
                    "text": "",
                })

            converted_messages.append({
                "message_id": m_id,
                "index": idx,
                "role": role,
                "created_at": created_at,
                "completed": completed,
                "reverted": msg_reverted,
                "parts": converted_parts,
            })

        # 判斷整體 Session 是否 in_progress
        in_progress = False
        if converted_messages:
            last_msg = converted_messages[-1]
            if last_msg["role"] == "assistant" and not last_msg["completed"]:
                in_progress = True

        reading_dict: dict[str, Any] = {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": self.source,
            "title": title,
            "parent_id": final_parent_id,
            "snapshot_sha256": snapshot_sha256,
            "in_progress": in_progress,
            "messages": converted_messages,
        }

        # 執行 validate_reading 驗證
        errors = validate_reading(reading_dict)
        if errors:
            err_msg = "; ".join(f"{e.field}: {e.message}" for e in errors)
            raise ValueError(f"轉換後閱讀版未通過規格驗證: {err_msg}")

        return reading_dict
