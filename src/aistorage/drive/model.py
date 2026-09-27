"""AiStorage Google Drive 存取層模型與協定定義。

依據規格：docs/impl/group3-modules.md 第 2.1 節
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass(frozen=True)
class DriveFile:
    """Google Drive 檔案或資料夾元資料。"""

    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...]
    size: int | None
    sha256: str | None
    md5: str | None
    created_time: str
    modified_time: str
    trashed: bool

    @property
    def is_folder(self) -> bool:
        """判定此項目是否為資料夾。"""
        return self.mime_type == GOOGLE_FOLDER_MIME

    @property
    def created_at(self) -> datetime:
        """M3: 將 created_time 轉為帶時區之 UTC datetime。"""
        from aistorage.clock import parse_rfc3339
        return parse_rfc3339(self.created_time)

    @property
    def modified_at(self) -> datetime:
        """M3: 將 modified_time 轉為帶時區之 UTC datetime。"""
        from aistorage.clock import parse_rfc3339
        return parse_rfc3339(self.modified_time)


@runtime_checkable
class DriveClient(Protocol):
    """Google Drive 操作用戶端介面協定。"""

    def list_children(self, folder_id: str) -> list[DriveFile]:
        """列舉指定資料夾下所有直接子項目（分頁讀完，僅回傳 trashed=False 者）。"""
        ...

    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]:
        """在指定資料夾下依名稱搜尋檔案（可能有多個同名者）。"""
        ...

    def get(self, file_id: str) -> DriveFile:
        """取得單一檔案之元資料。404 時拋出 NotFound，其他錯誤拋出 ReadError。"""
        ...

    def download(self, file_id: str, dest: Path, *, max_bytes: int) -> int:
        """串流下載檔案至指定本機路徑。超過 max_bytes 立即中止並拋出 TooLarge。回傳下載位元組數。"""
        ...

    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        """下載小檔內容為 bytes。超過 max_bytes 立即中止並拋出 TooLarge。"""
        ...

    def create(
        self,
        parent_id: str,
        name: str,
        content: bytes | Path,
        *,
        mime_type: str = "application/octet-stream",
    ) -> DriveFile:
        """在指定資料夾建立新檔案。"""
        ...

    def update_content(self, file_id: str, content: bytes | Path) -> DriveFile:
        """原地更新既有檔案之內容（file_id 保持不變）。"""
        ...

    def move(self, file_id: str, *, from_parent: str, to_parent: str) -> DriveFile:
        """搬移檔案至另一個父資料夾。"""
        ...

    def delete_permanently(self, file_id: str) -> None:
        """永久刪除檔案（不移入垃圾桶）。"""
        ...
