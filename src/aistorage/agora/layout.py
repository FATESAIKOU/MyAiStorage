"""Agora 真本資料模型檔案佈局模組。

依據規格：docs/impl/group3-modules.md 第 6.1 節
- enc / dec: 使用 urllib.parse.quote(s, safe="-_.") 保證路徑安全、可逆且不含冒號
- 路徑計算函式（純函式，無副作用）
"""

from __future__ import annotations

import urllib.parse


def enc(s: str) -> str:
    """編碼字串為安全路徑元件。保留字母、數字、連字號、底線與小數點；冒號等符號均被百分比編碼。"""
    return urllib.parse.quote(s, safe="-_.")


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
    return parts[0], parts[1]


def session_dir(source: str, source_session_id: str) -> str:
    """取得指定 Session 之目錄相對路徑：sessions/<source>/<enc(source_session_id)>。"""
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
    return f"handoffs/{ulid}.json"


def claim_path(ulid: str) -> str:
    """認領單檔案相對路徑：claims/<ULID>.json。"""
    return f"claims/{ulid}.json"


def rewrite_path(ulid: str) -> str:
    """改寫提案檔案相對路徑：rewrites/<ULID>.json。"""
    return f"rewrites/{ulid}.json"


def continuation_link_path(new_session_id: str, handoff_ulid: str) -> str:
    """接續 Link 相對路徑：links/continuation/<enc(new_session_id)>/<handoff ULID>.json。"""
    return f"links/continuation/{enc(new_session_id)}/{handoff_ulid}.json"


def reference_link_path(from_session_id: str, to_session_id: str) -> str:
    """參考 Link 相對路徑：links/reference/<enc(from_session_id)>/<enc(to_session_id)>.json。"""
    return f"links/reference/{enc(from_session_id)}/{enc(to_session_id)}.json"


def ledger_path(year_month: str) -> str:
    """提交流程處理清冊路徑：_committer/ledger/<YYYY-MM>.jsonl。"""
    return f"_committer/ledger/{year_month}.jsonl"


def rejection_path(item_key: str) -> str:
    """提交流程拒收紀錄路徑：_committer/rejections/<item_key>.json。"""
    return f"_committer/rejections/{item_key}.json"


def checksums_path() -> str:
    """提交流程快照雜湊快取路徑：_committer/checksums.json。"""
    return "_committer/checksums.json"


def schema_version_path() -> str:
    """Agora 真本 schema 版本標識路徑：_committer/schema_version。"""
    return "_committer/schema_version"


def record_path_for_id(item_id: str) -> str:
    """依據項目 ID (<type>:<ULID> 或 <source>:<source_session_id>) 推算真本內儲存相對路徑。"""
    if not item_id or ":" not in item_id:
        raise ValueError(f"無效之項目 ID: '{item_id}'")

    prefix, suffix = item_id.split(":", 1)
    if prefix == "handoff":
        return handoff_path(suffix)
    if prefix == "claim":
        return claim_path(suffix)
    if prefix == "rewrite":
        return rewrite_path(suffix)
    if prefix == "artifact":
        return f"artifacts/{suffix}.json"

    # 若非上述前綴，則為 Session (<source>:<source_session_id>)
    return session_meta_path(item_id)
