"""Agora 真本資料模型檔案佈局模組。

依據規格：
- docs/impl/group3-modules.md 第 6.1 節
- review-g3b.md M5（路徑安全、ULID 驗證、reference 路由、保留型態拒絕）
"""

from __future__ import annotations

import re
import urllib.parse

from aistorage.schema import RESERVED_TYPE_NAMES

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_LEDGER_YM_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def _validate_ulid(ulid: str, field_name: str = "ULID") -> None:
    if not isinstance(ulid, str) or not _ULID_PATTERN.match(ulid):
        raise ValueError(f"無效之 {field_name} 格式: {repr(ulid)}（必須符合 26 碼 Crockford Base32）")


def enc(s: str) -> str:
    """編碼字串為安全路徑元件。

    依 RFC 3986 未保留字元預設不被 urllib.parse.quote 編碼（包括 '.'、'_'、'-'、'~'）。
    因此防止路徑穿越（如 '.' 與 '..'）係透過明確檢查拒絕，絕不可移除下列檢查。
    safe="-_" 指定額外保留連字號與底線。
    若為空或為 '.' / '..' 則拒絕。
    """
    if not s or s in (".", ".."):
        raise ValueError(f"無效或不安全之路徑元件: {repr(s)}")
    return urllib.parse.quote(s, safe="-_")


def dec(s: str) -> str:
    """解碼經由 enc 編碼之路徑元件。"""
    return urllib.parse.unquote(s)


def split_session_id(session_id: str) -> tuple[str, str]:
    """將 Session ID (<source>:<source_session_id>) 拆解為 source 與 source_session_id。

    Raises:
        ValueError: 若未包含冒號或為空
    """
    if not session_id or ":" not in session_id:
        raise ValueError(f"無效之 Session ID: '{session_id}' (必須符合 <source>:<id>)")
    parts = session_id.split(":", 1)
    source, source_session_id = parts[0], parts[1]
    if not _SOURCE_PATTERN.match(source):
        raise ValueError(f"無效之 Session source: {repr(source)}")
    if not source_session_id:
        raise ValueError(f"Session source_session_id 不得為空: '{session_id}'")
    return source, source_session_id


def session_dir(source: str, source_session_id: str) -> str:
    """取得指定 Session 之目錄相對路徑：sessions/<source>/<enc(source_session_id)>。"""
    if not _SOURCE_PATTERN.match(source):
        raise ValueError(f"無效之 Session source: {repr(source)}")
    return f"sessions/{source}/{enc(source_session_id)}"


def session_dir_for_id(session_id: str) -> str:
    """依據完整 session_id 取得 Session 目錄相對路徑。"""
    src, src_id = split_session_id(session_id)
    return session_dir(src, src_id)


def session_meta_path(session_id: str) -> str:
    """Session meta.json 相對路徑。"""
    return f"{session_dir_for_id(session_id)}/meta.json"


def session_raw_path(session_id: str) -> str:
    """Session 原始紀錄本體 (raw) 相對路徑。"""
    return f"{session_dir_for_id(session_id)}/raw"


def session_snapshots_path(session_id: str) -> str:
    """Session 快照歷史清單 (snapshots.jsonl) 相對路徑。"""
    return f"{session_dir_for_id(session_id)}/snapshots.jsonl"


def handoff_path(ulid: str) -> str:
    """交接單檔案相對路徑：handoffs/<ULID>.json。"""
    _validate_ulid(ulid, "handoff ULID")
    return f"handoffs/{ulid}.json"


def claim_path(ulid: str) -> str:
    """認領單檔案相對路徑：claims/<ULID>.json。"""
    _validate_ulid(ulid, "claim ULID")
    return f"claims/{ulid}.json"


def rewrite_path(ulid: str) -> str:
    """改寫提案檔案相對路徑：rewrites/<ULID>.json。"""
    _validate_ulid(ulid, "rewrite ULID")
    return f"rewrites/{ulid}.json"


def reference_path(ulid: str) -> str:
    """M5: 參考紀錄檔案相對路徑：references/<ULID>.json。"""
    _validate_ulid(ulid, "reference ULID")
    return f"references/{ulid}.json"


def continuation_link_path(new_session_id: str, handoff_ulid: str) -> str:
    """接續 Link 相對路徑：links/continuation/<enc(new_session_id)>/<handoff ULID>.json。"""
    _validate_ulid(handoff_ulid, "handoff ULID")
    return f"links/continuation/{enc(new_session_id)}/{handoff_ulid}.json"


def reference_link_path(from_session_id: str, to_session_id: str) -> str:
    """參考 Link 索引相對路徑：links/reference/<enc(from_session_id)>/<enc(to_session_id)>.json。"""
    return f"links/reference/{enc(from_session_id)}/{enc(to_session_id)}.json"


def ledger_path(year_month: str) -> str:
    """提交流程處理清冊路徑：_committer/ledger/<YYYY-MM>.jsonl。"""
    if not isinstance(year_month, str) or not _LEDGER_YM_PATTERN.match(year_month):
        raise ValueError(f"無效之清冊月份格式: {repr(year_month)}（必須符合 YYYY-MM）")
    return f"_committer/ledger/{year_month}.jsonl"


def rejection_path(item_key: str) -> str:
    """提交流程拒收紀錄路徑：_committer/rejections/<item_key>.json。"""
    _validate_ulid(item_key, "rejection item_key ULID")
    return f"_committer/rejections/{item_key}.json"


def checksums_path() -> str:
    """提交流程快照雜湊快取路徑：_committer/checksums.json。"""
    return "_committer/checksums.json"


def schema_version_path() -> str:
    """Agora 真本 schema 版本標識路徑：_committer/schema_version。"""
    return "_committer/schema_version"


def record_path_for_id(item_id: str) -> str:
    """依據項目 ID (<type>:<ULID> 或 <source>:<source_session_id>) 推算真本內儲存相對路徑。

    M5 規則：
    - handoff / claim / rewrite / reference 明確路由至專屬目錄
    - artifact 產出登錄不進 Agora（ADR 0009），拋出 ValueError
    - session 或其他 RESERVED_TYPE_NAMES 不得直接推算檔案路徑
    - 其它符合 <source>:<id> 格式者路由為 session_meta_path
    """
    if not item_id or ":" not in item_id:
        raise ValueError(f"無效之項目 ID: '{item_id}'")

    prefix, suffix = item_id.split(":", 1)
    if prefix == "handoff":
        return handoff_path(suffix)
    if prefix == "claim":
        return claim_path(suffix)
    if prefix == "rewrite":
        return rewrite_path(suffix)
    if prefix == "reference":
        return reference_path(suffix)

    if prefix in ("artifact", "session") or prefix in RESERVED_TYPE_NAMES:
        raise ValueError(f"保留型態 '{prefix}' 不得推算路徑或不在 Agora 範圍: '{item_id}'")

    # Session (<source>:<source_session_id>)
    return session_meta_path(item_id)
