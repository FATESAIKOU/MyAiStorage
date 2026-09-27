"""AiStorage opencode 原始紀錄轉換器 (OpencodeConverter)。

依據規格：
- docs/impl/group3-modules.md 第 5 節
- schemas/reading-version.md 轉換對應表
- schemas/reading-version.schema.json
- design D4、D10
- docs/spike/evidence/1.7a〜1.7h 欄位描述
- review-g3b.md H1（compaction/summary/tool error/file 附件真實形狀）、H2（嚴格化、不補假值、ConversionError）、M1〜M4、L
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import urllib.parse

from aistorage.converters.base import ConversionError, Converter, SessionFacts
from aistorage.reading import validate_reading

DROP_PARTS = frozenset({"step-start", "step-finish", "snapshot", "patch"})


def _parse_time_ms(val: Any) -> int | None:
    """將時間值解析為毫秒整數時間戳。"""
    if val is None or val == "" or val == 0:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 1e11 else int(val * 1000)
    if isinstance(val, str):
        val_clean = val.strip()
        if not val_clean:
            return None
        try:
            dt = datetime.fromisoformat(val_clean.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def _format_time(val: Any) -> str | None:
    """將時間戳轉換為標準 RFC 3339 UTC 格式字串（YYYY-MM-DDTHH:MM:SSZ）。

    H2: 絕不在字串無效時補上 'Z' 假裝成功，解析失敗一律回傳 None。
    """
    if val is None or val == "" or val == 0:
        return None
    if isinstance(val, (int, float)):
        sec = val / 1000.0 if val > 1e11 else float(val)
        dt = datetime.fromtimestamp(sec, timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, str):
        val_clean = val.strip()
        if not val_clean:
            return None
        try:
            dt = datetime.fromisoformat(val_clean.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
    return None


def _truncate_summary(val: Any, max_codepoints: int = 4000) -> str:
    """擷取摘要內容，以 Unicode code point 為準，上限 4,000 字元，超過時截斷並標示 …。"""
    if isinstance(val, (dict, list)):
        s = json.dumps(val, sort_keys=True, ensure_ascii=False)
    elif val is None:
        s = ""
    else:
        s = str(val)

    # 以 Unicode code point 計數
    if len(s) > max_codepoints:
        s = s[: max_codepoints - 1] + "…"
    return s


class OpencodeConverter(Converter):
    """opencode 會話原始匯出格式至 aistorage.reading/v1 之轉換器。"""

    source: str = "opencode"

    def facts(self, raw_path: Path, *, session_id: str | None = None) -> SessionFacts:
        """自 opencode 原始匯出 JSON 提取 SessionFacts。

        `session_id`（`<source>:<source_session_id>`）由呼叫端傳入，opencode 的
        子代理 id 直接取自 `state.metadata.sessionId`，不需要知道自己的 id，因此
        這個參數在此不使用（見 review-g3f L3：呼叫端要明確提供，轉換器不推測）。

        M3: last_message_at 取所有訊息的 created 與 completed 之最大毫秒值轉換，避免字串比較誤判。
        """
        with open(raw_path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception as e:
                raise ConversionError(f"原始紀錄 JSON 解析失敗: {e}") from None

        if not isinstance(data, dict):
            raise ConversionError("原始紀錄根物件必須是 JSON 字典")

        info = data.get("info")
        if not isinstance(info, dict):
            info = {}
        time_info = info.get("time") if isinstance(info.get("time"), dict) else {}

        title = info.get("title")
        created_at = _format_time(time_info.get("created"))
        updated_at = _format_time(time_info.get("updated"))

        # R3: 記錄毫秒整數時間戳，用於判定「封存之後是否有新訊息」
        archived_ms = _parse_time_ms(time_info.get("archived"))
        if archived_ms is not None and archived_ms <= 0:
            archived_ms = None
        archived_at = _format_time(archived_ms) if archived_ms is not None else None

        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ConversionError("原始紀錄 messages 欄位必須是清單")

        message_ids: list[str] = []
        max_time_ms: int | None = None
        in_progress = False

        for idx, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                raise ConversionError(f"第 {idx} 則訊息必須是 JSON 字典")
            m_info = m.get("info")
            if not isinstance(m_info, dict):
                raise ConversionError(f"第 {idx} 則訊息缺少 info 物件")
            # R2: 只取 m_info.id
            m_id = m_info.get("id")
            if not m_id:
                raise ConversionError(f"第 {idx} 則訊息缺少 id 欄位")
            message_ids.append(str(m_id))

            m_time = m_info.get("time")
            if isinstance(m_time, dict):
                c_ms = _parse_time_ms(m_time.get("created"))
                comp_ms = _parse_time_ms(m_time.get("completed"))
                for ms in (c_ms, comp_ms):
                    if ms is not None:
                        if max_time_ms is None or ms > max_time_ms:
                            max_time_ms = ms

        last_message_ms = max_time_ms
        last_message_at = _format_time(max_time_ms)

        # R6: 判定 in_progress（中止判定以 MessageAbortedError 為依據）
        if raw_messages:
            last_m = raw_messages[-1]
            last_info = last_m.get("info") if isinstance(last_m.get("info"), dict) else {}
            # R2: 只取 info.role
            role = last_info.get("role")
            if role == "assistant":
                last_time = last_info.get("time") if isinstance(last_info.get("time"), dict) else {}
                completed = bool(last_time.get("completed"))
                error_obj = last_info.get("error")
                is_aborted = (
                    isinstance(error_obj, dict)
                    and error_obj.get("name") == "MessageAbortedError"
                )
                if not completed and not is_aborted:
                    in_progress = True

        return SessionFacts(
            title=str(title) if title is not None else None,
            created_at=created_at,
            updated_at=updated_at,
            message_ids=tuple(message_ids),
            archived_at=archived_at,
            last_message_at=last_message_at,
            in_progress=in_progress,
            archived_ms=archived_ms,
            last_message_ms=last_message_ms,
        )

    def child_session_ids(self, raw_path: Path, *, session_id: str | None = None) -> tuple[str, ...]:
        """自 opencode 原始紀錄中掃描 task 工具調用所產生的子代理 Session ID。

        `session_id` 與 `facts` 同理：opencode 不需要它，保留參數只是讓呼叫端
        能對兩種來源用同一組呼叫方式。
        """
        raw_p = Path(raw_path)
        try:
            data = json.loads(raw_p.read_text(encoding="utf-8"))
        except Exception as e:
            raise ConversionError(f"原始紀錄 JSON 解析失敗: {e}") from None

        if not isinstance(data, dict):
            raise ConversionError("原始紀錄根物件必須是 JSON 字典")

        child_ids: list[str] = []
        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ConversionError("原始紀錄 messages 欄位必須是清單")

        for idx, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                raise ConversionError(f"第 {idx} 則訊息必須是 JSON 字典")
            parts = m.get("parts")
            if not isinstance(parts, list):
                raise ConversionError(f"第 {idx} 則訊息缺少 parts 清單")
            for p in parts:
                if not isinstance(p, dict):
                    raise ConversionError("part 必須是 JSON 字典")
                # R2 & R7: 只接受 type == 'tool' 與 tool == 'task'，sessionId 來自 state.metadata
                if p.get("type") == "tool" and p.get("tool") == "task":
                    state = p.get("state")
                    if isinstance(state, dict):
                        meta = state.get("metadata")
                        if isinstance(meta, dict):
                            cid = meta.get("sessionId")
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
        parent_id: str | None = None,
        snapshot_sha256: str | None = None,
    ) -> dict:
        """轉換 opencode 匯出紀錄至 aistorage.reading/v1 格式。

        H1-a: compaction 邊界標記在 user 訊息時丟棄；摘要訊息嚴格比對 role=assistant 且 info.summary is True。
        H1-b: 工具錯誤訊息取自 state.error。
        H1-c & PM 決定 2: 附件為 data: URL 則解碼算 sha256/size；非 data: URL 轉為文字段落，絕不捏造雜湊。
        H2: 缺少必要欄位或結構異常拋出 ConversionError。
        M1 & PM 決定 4: snapshot_sha256 由轉換器自原始檔案位元組計算（唯一來源）。
        M2: 處理 revert.partID 規則，指到不存在的 id 拋出 ConversionError。
        M4: parent_id 與原始紀錄交叉驗證。
        """
        raw_p = Path(raw_path)
        raw_bytes = raw_p.read_bytes()

        # M1 & PM 決定 4: 由轉換器自原始位元組計算 snapshot_sha256
        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest().lower()
        if snapshot_sha256 is not None and snapshot_sha256.lower() != actual_sha256:
            raise ConversionError(
                f"傳入之 snapshot_sha256 ({snapshot_sha256}) 與檔案本體計算之 SHA-256 ({actual_sha256}) 不符"
            )
        final_snapshot_sha256 = actual_sha256

        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as e:
            raise ConversionError(f"原始紀錄 JSON 解析失敗: {e}") from None

        if not isinstance(data, dict):
            raise ConversionError("原始紀錄根物件必須是 JSON 字典")

        info = data.get("info")
        if not isinstance(info, dict):
            info = {}
        title = info.get("title")

        # M4: parent_id 交叉檢查
        raw_parent = info.get("parentID")
        formatted_info_parent: str | None = None
        if raw_parent:
            p_str = str(raw_parent)
            formatted_info_parent = p_str if ":" in p_str else f"opencode:{p_str}"

        formatted_param_parent: str | None = None
        if parent_id is not None:
            p_str = str(parent_id)
            formatted_param_parent = p_str if ":" in p_str else f"opencode:{p_str}"

        if formatted_param_parent is not None and formatted_info_parent is not None:
            if formatted_param_parent != formatted_info_parent:
                raise ConversionError(
                    f"傳入之 parent_id ('{formatted_param_parent}') 與原始紀錄中的 parentID ('{formatted_info_parent}') 不一致"
                )
            final_parent_id = formatted_param_parent
        elif formatted_param_parent is not None:
            final_parent_id = formatted_param_parent
        else:
            final_parent_id = formatted_info_parent

        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ConversionError("原始紀錄 messages 必須為清單")

        # M2: 判定 revert 指標與目標訊息合法性（R2: 只接受 messageID 與 partID，不接受別名與字串）
        revert_target_id: str | None = None
        revert_part_id: str | None = None
        revert_obj = info.get("revert")
        if isinstance(revert_obj, dict):
            revert_target_id = revert_obj.get("messageID")
            revert_part_id = revert_obj.get("partID")

        all_msg_ids: list[str] = []
        for idx, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                raise ConversionError(f"第 {idx} 則訊息必須是 JSON 字典")
            m_info = m.get("info")
            if not isinstance(m_info, dict):
                raise ConversionError(f"第 {idx} 則訊息缺少 info 物件")
            mid = m_info.get("id")
            if not mid:
                raise ConversionError(f"第 {idx} 則訊息缺少 id 欄位")
            all_msg_ids.append(str(mid))

        if revert_target_id:
            revert_target_id = str(revert_target_id)
            if revert_target_id not in all_msg_ids:
                raise ConversionError(
                    f"revert 指標指定的 messageID '{revert_target_id}' 不存在於會話紀錄中"
                )

        converted_messages: list[dict[str, Any]] = []
        is_reverted_mode = False

        for idx, m in enumerate(raw_messages):
            m_info = m.get("info")
            if not isinstance(m_info, dict):
                raise ConversionError(f"第 {idx} 則訊息缺少 info 物件")
            m_id = str(m_info.get("id"))

            # H2: role 嚴格驗證
            role = m_info.get("role")
            if role not in ("user", "assistant", "system"):
                raise ConversionError(f"訊息 '{m_id}' 之角色無效或未提供: {repr(role)}")

            # H2: 建立時間嚴格驗證
            m_time = m_info.get("time")
            if not isinstance(m_time, dict):
                raise ConversionError(f"訊息 '{m_id}' 缺少有效之 time 物件")
            created_at = _format_time(m_time.get("created"))
            if not created_at:
                raise ConversionError(f"訊息 '{m_id}' 缺少有效之 time.created 建立時間")

            # completed 狀態
            if role == "assistant":
                completed = bool(m_time.get("completed"))
            else:
                completed = True

            # M2 & R5: reverted 判定（不允許訊息層級隨意覆寫）
            if revert_target_id and m_id == revert_target_id:
                if revert_part_id:
                    # R5: 有指定 partID：此訊息本身不標成 reverted，自下一則起標記；但 partID 之後的 part 需丟棄
                    msg_reverted = False
                    is_reverted_mode = True
                else:
                    # 無指定 partID：此訊息本身及之後全部標為 reverted
                    msg_reverted = True
                    is_reverted_mode = True
            elif is_reverted_mode:
                msg_reverted = True
            else:
                msg_reverted = False

            # H1-a & R2: 摘要訊息判定（嚴格比對 role == assistant 且 info.summary is True）
            is_summary_msg = (
                role == "assistant"
                and m_info.get("summary") is True
            )

            # R8: parts 必須為 list
            raw_parts = m.get("parts")
            if not isinstance(raw_parts, list):
                raise ConversionError(f"訊息 '{m_id}' 缺少 parts 清單")

            converted_parts: list[dict[str, Any]] = []

            for p in raw_parts:
                # R8: part 必須為 dict
                if not isinstance(p, dict):
                    raise ConversionError(f"訊息 '{m_id}' 的 part 必須是 JSON 字典")

                # R5: 若當前訊息為 revert 目標且有指定 revert_part_id，自 partID 起中斷（丟棄）後續段落
                if (
                    revert_target_id
                    and m_id == revert_target_id
                    and revert_part_id
                    and p.get("id") == revert_part_id
                ):
                    break

                p_type = p.get("type")

                # (1) 丟棄清單 (白名單過濾)
                if p_type in DROP_PARTS:
                    continue

                # H1-a: user 訊息上的 compaction part 視為壓縮邊界標記，直接丟棄
                if p_type == "compaction" and role == "user":
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

                # (3) reasoning 段落（R2: 移除 thinking）
                elif p_type == "reasoning":
                    converted_parts.append({
                        "type": "reasoning",
                        "text": str(p.get("text", "")),
                    })

                # (4) tool 呼叫與回應（R2: 移除 tool_call 與 name 別名）
                elif p_type == "tool":
                    tool_name = str(p.get("tool") or "")
                    if not tool_name:
                        raise ConversionError(f"訊息 '{m_id}' 的 tool part 缺少 tool 工具名稱")

                    state = p.get("state")
                    if not isinstance(state, dict):
                        state = {}

                    raw_in = state.get("input")
                    in_summary = _truncate_summary(raw_in, 4000)

                    is_error = state.get("status") == "error"
                    if is_error:
                        # H1-b: 工具錯誤訊息取自 state.error
                        raw_err = state.get("error") or ""
                        err_text = str(raw_err)
                        out_summary = f"[ERROR] {err_text}" if not err_text.startswith("[ERROR]") else err_text
                        out_summary = _truncate_summary(out_summary, 4000)
                    else:
                        raw_out = state.get("output")
                        out_summary = _truncate_summary(raw_out, 4000)

                    # 子代理識別碼（R2: 只取 task 工具之 state.metadata.sessionId）
                    child_id: str | None = None
                    if tool_name == "task":
                        meta = state.get("metadata")
                        if isinstance(meta, dict):
                            cid = meta.get("sessionId")
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

                # (5) file 附件段落（H1-c, R1, R2, R4）
                elif p_type == "file":
                    raw_mime = p.get("mime")
                    if not raw_mime or not isinstance(raw_mime, str):
                        raise ConversionError(f"訊息 '{m_id}' 的 file part 缺少必填之 mime 欄位")
                    mime = raw_mime.strip().lower()

                    raw_url = p.get("url")
                    if not raw_url or not isinstance(raw_url, str):
                        raise ConversionError(f"訊息 '{m_id}' 的 file part 缺少必填之 url 欄位")
                    url = raw_url.strip()

                    filename = p.get("filename")
                    filename_str = str(filename).strip() if isinstance(filename, str) and filename.strip() else None

                    if url.startswith("data:"):
                        # R4: 嚴格解析 RFC 2397 data: URL
                        raw_data_part = url[5:]
                        if "," not in raw_data_part:
                            raise ConversionError(f"訊息 '{m_id}' 之 data URL 缺少逗號分隔符號")
                        header, data_payload = raw_data_part.split(",", 1)
                        header = header.strip()
                        if header.endswith(";base64"):
                            url_mime = header[:-7].strip().lower()
                            is_base64 = True
                        else:
                            url_mime = header.lower()
                            is_base64 = False

                        if url_mime and url_mime != mime:
                            raise ConversionError(
                                f"訊息 '{m_id}' 之 data URL MIME '{url_mime}' 與 part.mime '{mime}' 不一致"
                            )

                        if is_base64:
                            try:
                                content_bytes = base64.b64decode(data_payload, validate=True)
                            except Exception as e:
                                raise ConversionError(f"訊息 '{m_id}' 之 data URL Base64 解碼失敗: {e}") from None
                        else:
                            content_bytes = urllib.parse.unquote_to_bytes(data_payload)

                        calc_sha = hashlib.sha256(content_bytes).hexdigest().lower()
                        calc_size = len(content_bytes)

                        if mime.startswith("image/"):
                            converted_parts.append({
                                "type": "image",
                                "media_type": mime,
                                "sha256": calc_sha,
                                "size": calc_size,
                            })
                        else:
                            converted_parts.append({
                                "type": "file",
                                "media_type": mime,
                                "sha256": calc_sha,
                                "size": calc_size,
                                "name": filename_str if filename_str else "（無檔名）",
                            })
                    else:
                        # PM 決定 2 & R2: 非 data: URL 轉成 text part，無檔名標示為（無檔名），絕不捏造雜湊與檔名
                        display_name = filename_str if filename_str else "（無檔名）"
                        converted_parts.append({
                            "type": "text",
                            "text": f"[附件：{display_name} {mime}，內容不在匯出中]",
                        })

                # (6) H2 & R2: 未知或非預期段落型態（包含 assistant 上的 compaction）輸出明確標記，絕不靜默丟棄
                else:
                    converted_parts.append({
                        "type": "text",
                        "text": f"[未支援的段落型態：{p_type}]",
                    })

            # L: 若段落全數被過濾（例如僅有 step 標記），補空 text part
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

        # R6: 判斷整體 Session 是否 in_progress（已中止之會話 in_progress = False）
        in_progress = False
        if converted_messages:
            last_msg = converted_messages[-1]
            if last_msg["role"] == "assistant" and not last_msg["completed"]:
                last_raw = raw_messages[-1]
                last_info = last_raw.get("info") if isinstance(last_raw.get("info"), dict) else {}
                error_obj = last_info.get("error")
                is_aborted = (
                    isinstance(error_obj, dict)
                    and error_obj.get("name") == "MessageAbortedError"
                )
                if not is_aborted:
                    in_progress = True

        reading_dict: dict[str, Any] = {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": self.source,
            "title": str(title) if title is not None else None,
            "parent_id": final_parent_id,
            "snapshot_sha256": final_snapshot_sha256,
            "in_progress": in_progress,
            "messages": converted_messages,
        }

        # 執行 validate_reading 驗證
        errors = validate_reading(reading_dict)
        if errors:
            err_msg = "; ".join(f"{e.field}: {e.message}" for e in errors)
            raise ConversionError(f"轉換後閱讀版未通過規格驗證: {err_msg}")

        return reading_dict
