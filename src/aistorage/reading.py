"""Agora Session 閱讀版共通格式驗證與操作模組。

依據規格：
- g2-api-2.4.md、g2-2.4-2.3-fixes.md 與 review-2.1f-2.4-2.3.md
- schemas/reading-version.schema.json
- schemas/reading-version.md
- design D4、D10
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator

from aistorage.schema import (
    FieldError,
    _create_format_checker,
    _locate_schema_file,
)

_FORMAT_CHECKER = _create_format_checker()
_SCHEMA_PATH = _locate_schema_file("reading-version.schema.json")

with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    _READING_SCHEMA = json.load(_f)

_READING_VALIDATOR = Draft202012Validator(
    _READING_SCHEMA, format_checker=_FORMAT_CHECKER
)

_REQUIRED_PATTERN = re.compile(r"'([^']+)' is a required property")


def validate_reading(obj: dict) -> list[FieldError]:
    """驗證閱讀版物件是否符合 schemas/reading-version.schema.json 規範與領域規則。

    檢查項目：
    1. 符合 Schema 定義。
    2. message_id 在整份閱讀版中全域唯一。
    3. messages 之 index 必須從 0 開始、連續且嚴格遞增（即 index == 0, 1, 2, ...）。

    Args:
        obj: 待驗證的閱讀版字典。

    Returns:
        FieldError 清單；空清單表示驗證通過。
    """
    if not isinstance(obj, dict):
        return [FieldError(field="", message="閱讀版資料必須是字典 (dict)")]

    errors: list[FieldError] = []

    # 1. JSON Schema 結構驗證
    for err in _READING_VALIDATOR.iter_errors(obj):
        path_str = ".".join(str(p) for p in err.path)
        if err.validator == "required":
            m = _REQUIRED_PATTERN.search(err.message)
            missing = m.group(1) if m else ""
            field_name = f"{path_str}.{missing}" if path_str else missing
            errors.append(FieldError(field=field_name, message=f"缺少必填欄位: {missing}"))
        else:
            errors.append(FieldError(field=path_str, message=err.message))

    # 2. 領域規則檢查：message_id 唯一性、index 從 0 起算連續嚴格遞增
    messages = obj.get("messages")
    if isinstance(messages, list):
        seen_ids: set[str] = set()
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue

            mid = msg.get("message_id")
            if isinstance(mid, str):
                if mid in seen_ids:
                    errors.append(
                        FieldError(
                            field=f"messages[{i}].message_id",
                            message=f"重複的 message_id: '{mid}'",
                        )
                    )
                else:
                    seen_ids.add(mid)

            idx = msg.get("index")
            if idx is not None and idx != i:
                errors.append(
                    FieldError(
                        field=f"messages[{i}].index",
                        message=f"訊息 index 必須從 0 起算連續嚴格遞增: 預期 {i}，實際為 {idx}",
                    )
                )

    return errors


def check_continuation(reading: dict, continuation: dict) -> list[FieldError]:
    """檢查交接單的 continuation 是否合法。

    規則：
    1. continuation 必須是包含 snapshot_sha256 與 message_id 的字典。
    2. continuation["snapshot_sha256"] 必須等於 reading["snapshot_sha256"]。
    3. continuation["message_id"] 必須存在於 reading["messages"]。
    4. 目標訊息必須 completed == True 且 reverted == False。

    Args:
        reading: 閱讀版字典。
        continuation: 交接單 continuation 字典 {"snapshot_sha256": "...", "message_id": "..."}。

    Returns:
        FieldError 清單；通過時為空清單。
    """
    errors: list[FieldError] = []
    if not isinstance(continuation, dict):
        return [FieldError(field="continuation", message="continuation 必須是字典 (dict)")]

    expected_snap = continuation.get("snapshot_sha256")
    actual_snap = reading.get("snapshot_sha256") if isinstance(reading, dict) else None
    if not expected_snap or expected_snap != actual_snap:
        errors.append(
            FieldError(
                field="continuation.snapshot_sha256",
                message=f"快照雜湊不符: 交接單為 '{expected_snap}'，閱讀版為 '{actual_snap}'",
            )
        )

    target_mid = continuation.get("message_id")
    if not target_mid:
        errors.append(
            FieldError(field="continuation.message_id", message="缺少接續點 message_id")
        )
        return errors

    messages = reading.get("messages", []) if isinstance(reading, dict) else []
    target_msg = None
    for msg in messages:
        if isinstance(msg, dict) and msg.get("message_id") == target_mid:
            target_msg = msg
            break

    if target_msg is None:
        errors.append(
            FieldError(
                field="continuation.message_id",
                message=f"在閱讀版快照中找不到接續點訊息 ID: '{target_mid}'",
            )
        )
    else:
        if not target_msg.get("completed", False):
            errors.append(
                FieldError(
                    field="continuation.message_id",
                    message=f"接續點訊息 '{target_mid}' 尚未生成完成 (completed=false)",
                )
            )
        if target_msg.get("reverted", False):
            errors.append(
                FieldError(
                    field="continuation.message_id",
                    message=f"接續點訊息 '{target_mid}' 已被撤銷 (reverted=true)",
                )
            )

    return errors


def messages_before(
    reading: dict,
    message_id: str,
    *,
    snapshot_sha256: str,
    include_reverted: bool = False,
) -> list[dict]:
    """取得接續點之前的內容。

    規則：
    1. snapshot_sha256 為必填 keyword-only 參數，必須與 reading["snapshot_sha256"] 相符，否則 raise ValueError。
    2. 目標 message_id 必須存在於 reading["messages"]，否則 raise KeyError。
    3. 目標訊息若 completed == False 或 reverted == True，raise ValueError（不得默默過濾）。
    4. 回傳 index 小於等於目標訊息 index 的訊息列表（含目標訊息）。
    5. 若 include_reverted 為 False，回傳清單中過濾排除 reverted == True 的前置訊息。

    Args:
        reading: 閱讀版字典。
        message_id: 作為接續點邊界的訊息識別碼。
        snapshot_sha256: 預期釘住的原始紀錄快照內容 SHA256 雜湊值（必填）。
        include_reverted: 是否包含標記為 reverted: true 的訊息。

    Returns:
        符合條件的訊息清單，依 index 排序。

    Raises:
        ValueError: 若快照雜湊不符，或目標訊息未完成 / 已被撤銷。
        KeyError: 若在 reading 中找不到指定之 message_id。
    """
    actual_snap = reading.get("snapshot_sha256")
    if actual_snap != snapshot_sha256:
        raise ValueError(
            f"快照雜湊不符: 預期 '{snapshot_sha256}'，實際閱讀版為 '{actual_snap}'"
        )

    messages = reading.get("messages", [])
    target_msg = None
    target_index = None

    for msg in messages:
        if isinstance(msg, dict) and msg.get("message_id") == message_id:
            target_msg = msg
            target_index = msg.get("index")
            break

    if target_msg is None or target_index is None:
        raise KeyError(f"找不到訊息 ID: {message_id}")

    if not target_msg.get("completed", False):
        raise ValueError(f"目標接續點訊息 '{message_id}' 未完成 (completed=false)")

    if target_msg.get("reverted", False):
        raise ValueError(f"目標接續點訊息 '{message_id}' 已被撤銷 (reverted=true)")

    results: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        idx = msg.get("index")
        if idx is not None and idx <= target_index:
            if not include_reverted and msg.get("reverted", False):
                continue
            results.append(msg)

    results.sort(key=lambda m: m.get("index", 0))
    return results


def plain_text(
    reading: dict,
    *,
    include_reverted: bool = False,
    include_reasoning: bool = False,
) -> str:
    """擷取閱讀版中的純文字與摘要，供全文搜尋索引使用。

    串接所有未撤銷（或包含撤銷）之訊息段落中的 text、input_summary、output_summary 與 compaction summary。
    預設排除 reasoning（思考過程）段落與 file 附件中繼資料。

    Args:
        reading: 閱讀版字典。
        include_reverted: 是否包含已撤銷訊息中的文字內容。
        include_reasoning: 是否包含 reasoning 推理段落（預設 False）。

    Returns:
        串接後的純文字字串，各文字塊以換行分隔。
    """
    messages = reading.get("messages", [])
    sorted_msgs = sorted(
        (m for m in messages if isinstance(m, dict)),
        key=lambda m: m.get("index", 0),
    )

    text_pieces: list[str] = []
    for msg in sorted_msgs:
        if not include_reverted and msg.get("reverted", False):
            continue

        for part in msg.get("parts", []):
            if not isinstance(part, dict):
                continue

            part_type = part.get("type")
            if part_type == "text":
                val = part.get("text")
                if val:
                    text_pieces.append(str(val).strip())
            elif part_type == "tool_call":
                inp = part.get("input_summary")
                out = part.get("output_summary")
                if inp:
                    text_pieces.append(str(inp).strip())
                if out:
                    text_pieces.append(str(out).strip())
            elif part_type == "compaction":
                summary = part.get("summary")
                if summary:
                    text_pieces.append(str(summary).strip())
            elif part_type == "reasoning" and include_reasoning:
                r_text = part.get("text")
                if r_text:
                    text_pieces.append(str(r_text).strip())

    return "\n".join(t for t in text_pieces if t)
