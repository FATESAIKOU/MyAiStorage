"""Foundry repo 檔案佈局與路徑工具模組。

依據規格：
- docs/impl/group5-7-modules.md 第 6.2 節 (7.2)
- catalog/<ULID>.json               # 產出目錄（每件一筆）：metadata＋body＋{object_key?, size?, sha256?}
- objects/<ULID>/<安全化的檔名>     # contained：以 git annex add 存成 annex 物件
- _committer/…                      # 清冊、拒收（與 Agora 相同）
"""

from __future__ import annotations

from pathlib import Path
import re

_SAFE_FILENAME_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]")


def sanitize_filename(name: str) -> str:
    """將產出檔名安全化，防止路徑遍歷與非法字元。"""
    if not name or not isinstance(name, str):
        return "object.bin"
    base = Path(name).name.strip()
    safe = _SAFE_FILENAME_PATTERN.sub("_", base)
    if not safe or safe in (".", ".."):
        return "object.bin"
    return safe


def catalog_path(ulid: str) -> str:
    """產出目錄相對路徑：catalog/<ULID>.json。"""
    if not ulid or ":" in ulid or "/" in ulid:
        raise ValueError(f"無效之 artifact ULID: {ulid!r}")
    return f"catalog/{ulid}.json"


def object_dir(ulid: str) -> str:
    """產出物件目錄：objects/<ULID>。"""
    if not ulid or ":" in ulid or "/" in ulid:
        raise ValueError(f"無效之 artifact ULID: {ulid!r}")
    return f"objects/{ulid}"


def object_path(ulid: str, name: str) -> str:
    """contained 產出物件本體相對路徑：objects/<ULID>/<安全化的檔名>。"""
    safe_name = sanitize_filename(name)
    return f"{object_dir(ulid)}/{safe_name}"


def rejection_path(item_key: str) -> str:
    """拒收紀錄路徑：_committer/rejections/<item_key>.json。"""
    if not item_key or "/" in item_key or "\\" in item_key:
        raise ValueError(f"無效之 item_key: {item_key!r}")
    return f"_committer/rejections/{item_key}.json"


def ledger_path(year_month: str) -> str:
    """清冊路徑：_committer/ledger/<YYYY-MM>.jsonl。"""
    return f"_committer/ledger/{year_month}.jsonl"


def schema_version_path() -> str:
    """Foundry 真本 schema 版本標識路徑：_committer/schema_version。"""
    return "_committer/schema_version"
