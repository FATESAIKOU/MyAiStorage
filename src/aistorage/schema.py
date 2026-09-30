"""AiStorage phase 1 共通項目 metadata schema 驗證與 ID 處理模組。"""

from __future__ import annotations

import datetime
import importlib.resources
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import time
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

# Crockford's Base32 字母表（排除 I, L, O, U）
CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# 保留之項目型態名稱
RESERVED_TYPE_NAMES = frozenset({
    "session",
    "handoff",
    "claim",
    "continuation",
    "reference",
    "rewrite",
    "artifact",
})

_UTC_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)


@dataclass(frozen=True)
class FieldError:
    """欄位驗證錯誤。

    Attributes:
        field: 發生錯誤的欄位名稱（例如 "id"、"created_at"；根物件非 dict 錯誤為 ""）
        message: 錯誤描述訊息
    """

    field: str
    message: str


def _encode_base32(value: int, length: int) -> str:
    """將整數編碼為固定長度的 Crockford's Base32 字串。"""
    chars = []
    for _ in range(length):
        chars.append(CROCKFORD_BASE32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def generate_ulid(timestamp_ms: int | None = None) -> str:
    """產生標準 26 字元之 ULID（時間可排序）。

    前 10 字元為 48-bit UTC 毫秒時間戳，後 16 字元為 80-bit 加密安全隨機數。
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    time_part = _encode_base32(timestamp_ms, 10)
    rand_part = _encode_base32(secrets.randbits(80), 16)
    return time_part + rand_part


def _create_format_checker() -> FormatChecker:
    """建立支援 RFC 3339 UTC Z date-time 格式之 FormatChecker（純標準庫，不加依賴）。"""
    checker = FormatChecker()

    @checker.checks("date-time")
    def check_datetime(val: Any) -> bool:
        if not isinstance(val, str):
            return True
        if not _UTC_DATETIME_PATTERN.match(val):
            return False
        try:
            dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.tzinfo is not None
        except Exception:
            return False

    return checker


def _locate_schema_file(filename: str) -> Path:
    """定位 JSON Schema 檔案。

    尋找順序（review-2.1 L3）：
    1. 環境變數 AISTORAGE_SCHEMA_DIR 覆寫（若設定則優先採用）
    2. importlib.resources 讀取套件內 aistorage/schemas
    3. 開發模式：repo 根目錄之 schemas/
    4. 工作目錄之 schemas/
    """
    # 1. 環境變數覆寫
    env_dir = os.environ.get("AISTORAGE_SCHEMA_DIR")
    if env_dir:
        candidate = Path(env_dir) / filename
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(
            f"AISTORAGE_SCHEMA_DIR ({env_dir}) 中找不到 JSON Schema 檔案: {filename}"
        )

    # 2. importlib.resources (支援 wheel 打包)
    try:
        res = importlib.resources.files("aistorage").joinpath("schemas", filename)
        if res.is_file():
            if isinstance(res, Path):
                return res
            with importlib.resources.as_file(res) as p:
                return Path(p)
    except Exception:
        pass

    # 3. 開發環境：相對於目前模組檔案的 repo 根目錄
    candidate = Path(__file__).resolve().parent.parent.parent / "schemas" / filename
    if candidate.is_file():
        return candidate

    # 4. 工作目錄下的 schemas 目錄
    candidate = Path.cwd() / "schemas" / filename
    if candidate.is_file():
        return candidate

    raise FileNotFoundError(f"找不到 JSON Schema 檔案: {filename}")


# 模組載入時快取編譯後的 Validators
_FORMAT_CHECKER = _create_format_checker()

with open(_locate_schema_file("metadata-inbox.schema.json"), encoding="utf-8") as _f:
    _INBOX_SCHEMA = json.load(_f)
_INBOX_VALIDATOR = Draft202012Validator(_INBOX_SCHEMA, format_checker=_FORMAT_CHECKER)

with open(_locate_schema_file("metadata-record.schema.json"), encoding="utf-8") as _f:
    _RECORD_SCHEMA = json.load(_f)
_RECORD_VALIDATOR = Draft202012Validator(_RECORD_SCHEMA, format_checker=_FORMAT_CHECKER)

_REQUIRED_PATTERN = re.compile(r"'([^']+)' is a required property")


def _run_validation(validator: Draft202012Validator, obj: Any) -> list[FieldError]:
    """執行 JSON Schema 驗證並整理為 FieldError 清單。"""
    if not isinstance(obj, dict):
        return [FieldError(field="", message="metadata 必須是字典 (dict)")]

    errors: list[FieldError] = []
    for err in validator.iter_errors(obj):
        path_str = ".".join(str(p) for p in err.path)
        if err.validator == "required":
            m = _REQUIRED_PATTERN.search(err.message)
            field_name = m.group(1) if m else path_str
            errors.append(FieldError(field=field_name, message=f"缺少必填欄位: {field_name}"))
        else:
            field_name = path_str or (str(err.path[-1]) if err.path else "")
            errors.append(FieldError(field=str(field_name), message=err.message))

    return errors


def validate_inbox_metadata(obj: dict) -> list[FieldError]:
    """驗證收件匣項目的 metadata。

    寫入者提供，必填包含 id, type, created_at, updated_at。不包含 producer（若帶有則視為擴充欄位忽略）。
    回傳空 list 代表驗證通過。
    """
    return _run_validation(_INBOX_VALIDATOR, obj)


def validate_record_metadata(obj: dict) -> list[FieldError]:
    """驗證真本項目的 metadata。

    提交流程蓋章後，必填包含 id, type, producer, created_at, updated_at, case_id, provenance。
    回傳空 list 代表驗證通過。
    """
    return _run_validation(_RECORD_VALIDATOR, obj)


def strip_claimed_producer(obj: dict) -> dict:
    """回傳去掉 producer 的新 dict（不修改原物件）。"""
    if not isinstance(obj, dict):
        raise TypeError("obj 必須是字典 (dict)")
    return {k: v for k, v in obj.items() if k != "producer"}


_SESSION_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: Session 項目 id 的唯一格式定義（`<source>:<source_session_id>`）。
#: 與 inbox-sidecar / metadata / reading-version 三個 schema 的 pattern 一致；
#: 寫入端在本地組裝時直接用這一份，不要各自重寫（review-g3g L）。
SESSION_ID_PATTERN = re.compile(
    r"^(?!(handoff|claim|reference|continuation|rewrite|artifact|session):)"
    r"[a-z0-9][a-z0-9_-]*:\S+$"
)


def is_session_id(value: object) -> bool:
    """判斷字串是否為合法的 Session 項目 id（見 SESSION_ID_PATTERN）。"""
    return isinstance(value, str) and bool(SESSION_ID_PATTERN.match(value))


def make_session_id(source: str, source_session_id: str) -> str:
    """產生 Session 項目 ID：'<source>:<source_session_id>'。

    Args:
        source: 來源應用識別碼（非空，符合小寫 ^[a-z0-9][a-z0-9_-]*$，且不得使用保留型態名）
        source_session_id: 來源應用內部的 Session ID（非空，不能包含空白）

    Raises:
        ValueError: 當 source 或 source_session_id 不符規範時。
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source 不能為空字串或純空白")
    if not _SESSION_SOURCE_PATTERN.match(source):
        raise ValueError(
            f"source 必須符合小寫英數、底線或連字號格式 (^[a-z0-9][a-z0-9_-]*$): {source}"
        )
    if source in RESERVED_TYPE_NAMES:
        raise ValueError(f"source 不能使用保留型態名稱: {source}")

    if not isinstance(source_session_id, str) or not source_session_id.strip():
        raise ValueError("source_session_id 不能為空字串或純空白")
    if any(c.isspace() for c in source_session_id):
        raise ValueError("source_session_id 不能包含空白字元")

    return f"{source}:{source_session_id}"



def make_item_id(item_type: str) -> str:
    """產生非 Session 項目的 ID：'<type>:<ULID>'。

    ULID 為 26 字元之 Crockford's Base32 編碼，保證時間可排序與全域唯一性。

        Args:
            item_type: 項目型態（如 'handoff'、'claim'、'continuation'、'reference'、'rewrite'、'artifact'；拒絕 'session'）

    Raises:
        ValueError: 當 item_type 為空、純空白、為 'session' 或含有冒號/空白時。
    """
    if not isinstance(item_type, str) or not item_type.strip():
        raise ValueError("item_type 不能為空字串或純空白")
    if item_type.lower() == "session":
        raise ValueError("make_item_id 拒絕 'session' 型態，請使用 make_session_id")
    if ":" in item_type or any(c.isspace() for c in item_type):
        raise ValueError("item_type 不能包含冒號 (':') 或空白字元")

    return f"{item_type}:{generate_ulid()}"


def classify_id(existing: dict | None, incoming: dict) -> str:
    """分類項目 ID 的生命週期狀態。

    Args:
        existing: 真本中同 ID 的既有 metadata（若真本尚無此項目則傳入 None）
        incoming: 已認證並蓋好 producer 的新 metadata

    Returns:
        "new": 真本尚無此 ID，為全新項目
        "update": 同 ID、同 producer 且同 type，判定為合法版本更新
        "collision": 同 ID，但 producer 或 type 不符，判定為撞號衝突

    Raises:
        ValueError: 當 incoming 缺必填欄位、或 existing 與 incoming 的 id 不同時。
    """
    if not isinstance(incoming, dict):
        raise ValueError("incoming 必須是字典 (dict)")

    inc_id = incoming.get("id")
    inc_producer = incoming.get("producer")
    inc_type = incoming.get("type")

    if not isinstance(inc_id, str) or not inc_id.strip():
        raise ValueError("incoming 必須包含有效非空 id")
    if not isinstance(inc_producer, str) or not inc_producer.strip():
        raise ValueError("incoming 必須包含有效非空 producer")
    if not isinstance(inc_type, str) or not inc_type.strip():
        raise ValueError("incoming 必須包含有效非空 type")

    if existing is None:
        return "new"

    if not isinstance(existing, dict):
        raise ValueError("existing 必須是字典 (dict)")

    ex_id = existing.get("id")
    ex_producer = existing.get("producer")
    ex_type = existing.get("type")

    if not isinstance(ex_id, str) or not ex_id.strip():
        raise ValueError("existing 必須包含有效非空 id")
    if not isinstance(ex_producer, str) or not ex_producer.strip():
        raise ValueError("existing 必須包含有效非空 producer")
    if not isinstance(ex_type, str) or not ex_type.strip():
        raise ValueError("existing 必須包含有效非空 type")

    if ex_id != inc_id:
        raise ValueError(
            f"existing id ('{ex_id}') 與 incoming id ('{inc_id}') 不相同"
        )

    if ex_producer == inc_producer and ex_type == inc_type:
        return "update"

    return "collision"
