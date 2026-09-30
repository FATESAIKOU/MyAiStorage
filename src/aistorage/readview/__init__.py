"""讀取視圖：manifest、FileRef、ReadingRef 與格式常數（純資料＋純函式）。"""

from __future__ import annotations

from aistorage.readview.model import (
    ELEMENT_AGORA,
    MANIFEST_FILE_NAME,
    MANIFEST_MAX_BYTES,
    READVIEW_FORMAT,
    FileRef,
    Manifest,
    ReadingRef,
    initial_manifest,
    next_manifest,
    parse_manifest,
    serialize_manifest,
    trusted_ids,
)

__all__ = [
    "ELEMENT_AGORA",
    "MANIFEST_FILE_NAME",
    "MANIFEST_MAX_BYTES",
    "READVIEW_FORMAT",
    "FileRef",
    "Manifest",
    "ReadingRef",
    "initial_manifest",
    "next_manifest",
    "parse_manifest",
    "serialize_manifest",
    "trusted_ids",
]
