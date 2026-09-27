"""AiStorage Google Drive 記憶體測試實作（FakeDrive）。

依據規格：docs/impl/group3-modules.md 第 2.3 節
- FakeDrive: 實作 DriveClient 協定，支援種子資料（seed_folder, seed_file）
- 錯誤注入（inject: 可針對特定 op 與 file_id 注入 ReadError/WriteError 等）
- 狀態快照（snapshot: 供測試驗證「未產生任何非預期異動」）
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import uuid

from aistorage.clock import Clock, FixedClock
from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient, DriveFile
from aistorage.errors import NotFound, ReadError, TooLarge, WriteError


class FakeDrive(DriveClient):
    """記憶體中 Google Drive 實作，供單元測試使用。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or FixedClock()
        self._files: dict[str, DriveFile] = {}
        self._contents: dict[str, bytes] = {}
        self._injections: list[dict] = []

    def seed_folder(self, name: str, parent: str | None = None) -> str:
        """建立種子資料夾並回傳其 folder_id。"""
        fid = f"folder_{uuid.uuid4().hex[:12]}"
        now = self._clock.now_utc()
        parents = (parent,) if parent else ()
        df = DriveFile(
            id=fid,
            name=name,
            mime_type=GOOGLE_FOLDER_MIME,
            parents=parents,
            size=None,
            sha256=None,
            md5=None,
            created_time=now,
            modified_time=now,
            trashed=False,
        )
        self._files[fid] = df
        return fid

    def seed_file(
        self,
        parent: str,
        name: str,
        content: bytes,
        *,
        sha256: str | None = "auto",
        created_time: str | None = None,
        trashed: bool = False,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """建立種子檔案並回傳其 file_id。"""
        fid = f"file_{uuid.uuid4().hex[:12]}"
        now = created_time or self._clock.now_utc()

        calc_sha256 = (
            hashlib.sha256(content).hexdigest().lower()
            if sha256 == "auto"
            else sha256
        )
        calc_md5 = hashlib.md5(content).hexdigest().lower()

        df = DriveFile(
            id=fid,
            name=name,
            mime_type=mime_type,
            parents=(parent,),
            size=len(content),
            sha256=calc_sha256,
            md5=calc_md5,
            created_time=now,
            modified_time=now,
            trashed=trashed,
        )
        self._files[fid] = df
        self._contents[fid] = bytes(content)
        return fid

    def inject(
        self,
        op: str,
        file_id: str | None = None,
        *,
        error: type[Exception] = ReadError,
        times: int = 1,
    ) -> None:
        """注入錯誤。

        Args:
            op: 操作名稱（例如 'download', 'list_children', 'get', 'move' 等）
            file_id: 若指定，僅在針對該 file_id 操作時注入
            error: 拋出之例外類別
            times: 注入次數
        """
        self._injections.append({
            "op": op,
            "file_id": file_id,
            "error": error,
            "times": times,
        })

    def _check_injections(self, op: str, file_id: str | None = None) -> None:
        for inj in list(self._injections):
            if inj["op"] == op:
                if inj["file_id"] is None or inj["file_id"] == file_id:
                    inj["times"] -= 1
                    if inj["times"] <= 0:
                        self._injections.remove(inj)
                    err_cls = inj["error"]
                    raise err_cls(f"FakeDrive injected error on {op}({file_id})")

    def snapshot(self) -> dict:
        """取得目前所有檔案與內容之快照字典，供比對狀態未受異動。"""
        return {
            "files": copy.deepcopy(self._files),
            "contents": copy.deepcopy(self._contents),
        }

    # -----------------------------------------------------------------------
    # DriveClient 介面實作
    # -----------------------------------------------------------------------

    def list_children(self, folder_id: str) -> list[DriveFile]:
        self._check_injections("list_children", folder_id)
        result = [
            f for f in self._files.values()
            if not f.trashed and folder_id in f.parents
        ]
        return result

    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]:
        self._check_injections("find_by_name", parent_id)
        result = [
            f for f in self._files.values()
            if not f.trashed and parent_id in f.parents and f.name == name
        ]
        return result

    def get(self, file_id: str) -> DriveFile:
        self._check_injections("get", file_id)
        f = self._files.get(file_id)
        if f is None or f.trashed:
            raise NotFound(f"找不到檔案: {file_id}")
        return f

    def download(self, file_id: str, dest: Path, *, max_bytes: int) -> int:
        self._check_injections("download", file_id)
        f = self.get(file_id)
        content = self._contents.get(file_id, b"")
        if len(content) > max_bytes:
            raise TooLarge(f"檔案大小 {len(content)} 超過上限 {max_bytes}")

        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return len(content)

    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        self._check_injections("download_bytes", file_id)
        f = self.get(file_id)
        content = self._contents.get(file_id, b"")
        if len(content) > max_bytes:
            raise TooLarge(f"檔案大小 {len(content)} 超過上限 {max_bytes}")
        return content

    def create(
        self,
        parent_id: str,
        name: str,
        content: bytes | Path,
        *,
        mime_type: str = "application/octet-stream",
    ) -> DriveFile:
        self._check_injections("create", parent_id)
        data = content.read_bytes() if isinstance(content, Path) else bytes(content)

        fid = f"file_{uuid.uuid4().hex[:12]}"
        now = self._clock.now_utc()
        sha256_val = hashlib.sha256(data).hexdigest().lower()
        md5_val = hashlib.md5(data).hexdigest().lower()

        df = DriveFile(
            id=fid,
            name=name,
            mime_type=mime_type,
            parents=(parent_id,),
            size=len(data),
            sha256=sha256_val,
            md5=md5_val,
            created_time=now,
            modified_time=now,
            trashed=False,
        )
        self._files[fid] = df
        self._contents[fid] = data
        return df

    def update_content(self, file_id: str, content: bytes | Path) -> DriveFile:
        self._check_injections("update_content", file_id)
        old_f = self.get(file_id)
        data = content.read_bytes() if isinstance(content, Path) else bytes(content)

        now = self._clock.now_utc()
        sha256_val = hashlib.sha256(data).hexdigest().lower()
        md5_val = hashlib.md5(data).hexdigest().lower()

        new_f = DriveFile(
            id=file_id,
            name=old_f.name,
            mime_type=old_f.mime_type,
            parents=old_f.parents,
            size=len(data),
            sha256=sha256_val,
            md5=md5_val,
            created_time=old_f.created_time,
            modified_time=now,
            trashed=old_f.trashed,
        )
        self._files[file_id] = new_f
        self._contents[file_id] = data
        return new_f

    def move(self, file_id: str, *, from_parent: str, to_parent: str) -> DriveFile:
        self._check_injections("move", file_id)
        old_f = self.get(file_id)
        new_parents = tuple(p for p in old_f.parents if p != from_parent) + (to_parent,)

        new_f = DriveFile(
            id=file_id,
            name=old_f.name,
            mime_type=old_f.mime_type,
            parents=new_parents,
            size=old_f.size,
            sha256=old_f.sha256,
            md5=old_f.md5,
            created_time=old_f.created_time,
            modified_time=self._clock.now_utc(),
            trashed=old_f.trashed,
        )
        self._files[file_id] = new_f
        return new_f

    def delete_permanently(self, file_id: str) -> None:
        self._check_injections("delete_permanently", file_id)
        if file_id in self._files:
            del self._files[file_id]
        if file_id in self._contents:
            del self._contents[file_id]
