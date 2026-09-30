"""管理操作共用（第 6 組，只在 Mac 上、以管理憑證執行）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aistorage.clock import Clock
from aistorage.drive.model import DriveClient


class AdminError(Exception):
    """管理操作失敗（前置條件不符、確認碼不符、中止）。"""


@dataclass
class AdminDeps:
    """管理操作的共用依賴（憑證一律只以路徑引用）。"""

    drive: DriveClient
    clock: Clock
    workdir: Path
    repo: str = "agora"
    repo_uuid: str = ""
    prefix_folder_id: str = ""
    quarantine_folder_id: str = ""
    readview_folder_id: str | None = None
    manifest_file_id: str | None = None
    repo_uuids: tuple[str, ...] = ()
    known_clones: tuple[str, ...] = ()
    # 抹除時一併掃描的額外 Drive 根（例如 annex special remote 的前綴）
    extra_scan_roots: tuple[str, ...] = ()
    # 收件匣資料夾（抹除計畫用 sidecar 指認相關項目）
    inbox_folder_ids: tuple[str, ...] = ()
