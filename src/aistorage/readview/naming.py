"""讀取視圖的檔名（內容定址）。

名稱**只是資訊用途**：讀取一律以 id 定位（manifest 的固定 id、index 與 reading
的 file id），整條路徑不以名稱搜尋（D5）。名稱存在是為了讓 Drive 上的資料夾
可以用瀏覽器與人工除錯看懂。
"""

from __future__ import annotations

import hashlib

from aistorage.readview.model import MANIFEST_FILE_NAME

# 各檔名中雜湊取用的位數
READING_ID_PREFIX_LEN = 16
READING_SNAPSHOT_PREFIX_LEN = 16
INDEX_SHA_PREFIX_LEN = 12


def _sha256_prefix(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()[:length]


def manifest_name() -> str:
    """manifest 檔名（固定 id，這個名稱只在建立時用一次）。"""
    return MANIFEST_FILE_NAME


def reading_name(session_id: str, snapshot_sha256: str) -> str:
    """reading 檔名：reading-<sha256(session_id) 前 16 碼>-<snapshot_sha256 前 16 碼>.json。

    內容定址：同一個「Session × 快照」永遠算出同一個名稱。
    """
    if not session_id:
        raise ValueError("session_id 不得為空")
    if not snapshot_sha256:
        raise ValueError("snapshot_sha256 不得為空")
    return (
        f"reading-{_sha256_prefix(session_id, READING_ID_PREFIX_LEN)}"
        f"-{snapshot_sha256.lower()[:READING_SNAPSHOT_PREFIX_LEN]}.json"
    )


def index_name(generation: int, index_sha256: str) -> str:
    """index 檔名：index-g<世代>-<sha256 前 12 碼>.sqlite。"""
    if generation < 0:
        raise ValueError("generation 不得為負數")
    if not index_sha256:
        raise ValueError("index_sha256 不得為空")
    return f"index-g{generation}-{index_sha256.lower()[:INDEX_SHA_PREFIX_LEN]}.sqlite"


def raw_name(session_id: str, snapshot_sha256: str) -> str:
    """原始紀錄本體的檔名：raw-<sha256(session_id) 前 16 碼>-<snapshot_sha256 前 16 碼>.

    與 `reading_name` 同一套前綴規則，讓 Drive 上的資料夾看得出這一對是同一個
    「Session × 快照」。內容定址：同一組輸入永遠算出同一個名稱。
    """
    if not session_id:
        raise ValueError("session_id 不得為空")
    if not snapshot_sha256:
        raise ValueError("snapshot_sha256 不得為空")
    return (
        f"raw-{_sha256_prefix(session_id, READING_ID_PREFIX_LEN)}"
        f"-{snapshot_sha256.lower()[:READING_SNAPSHOT_PREFIX_LEN]}"
    )
