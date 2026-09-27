"""AiStorage Google Drive 記憶體測試實作（FakeDrive）。

依據規格：
- docs/impl/group3-modules.md 第 2.3 節
- review-g3a.md M6（呼叫紀錄、第 n 次讀取注入、私有 _lookup 避免重複計算、排序支援、嚴格檢查、確定性 ID、set_checksum）
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import random
from typing import Any, Literal

from aistorage.clock import Clock, FixedClock, format_rfc3339
from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient, DriveFile
from aistorage.errors import NotFound, ReadError, TooLarge, WriteError


class FakeDrive(DriveClient):
    """記憶體中 Google Drive 實作，供單元測試與性質測試使用。"""

    READ_OPS: frozenset[str] = frozenset(
        {"list_children", "find_by_name", "get", "download", "download_bytes"}
    )
    WRITE_OPS: frozenset[str] = frozenset(
        {"create", "update_content", "move", "delete_permanently"}
    )

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        order: Literal["insertion", "reverse", "shuffle"] = "insertion",
        seed: int | None = None,
    ) -> None:
        self._clock: Clock = clock or FixedClock()
        self._order: Literal["insertion", "reverse", "shuffle"] = order
        self._rng: random.Random = random.Random(seed) if seed is not None else random.Random(42)
        self._files: dict[str, DriveFile] = {}
        self._contents: dict[str, bytes] = {}
        self._injections: list[dict[str, Any]] = []
        self._nth_read_injections: dict[int, type[Exception]] = {}
        self.calls: list[tuple[str, str | None]] = []
        self._folder_seq: int = 0
        self._file_seq: int = 0
        self._read_count: int = 0

    def seed_folder(self, name: str, parent: str | None = None) -> str:
        """建立種子資料夾並回傳其 folder_id（不計入 DriveClient API calls）。"""
        self._folder_seq += 1
        fid = f"folder_{self._folder_seq:04d}"
        now = format_rfc3339(self._clock.now(), include_fraction=True)
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
        modified_time: str | None = None,
        trashed: bool = False,
        mime_type: str = "application/octet-stream",
        file_id: str | None = None,
    ) -> str:
        """建立種子檔案並回傳其 file_id（不計入 DriveClient API calls）。"""
        if file_id is not None:
            fid = file_id
        else:
            self._file_seq += 1
            fid = f"file_{self._file_seq:04d}"
        now = created_time or format_rfc3339(self._clock.now(), include_fraction=True)
        mtime = modified_time or now

        calc_sha256 = (
            hashlib.sha256(content).hexdigest().lower()
            if sha256 == "auto"
            else (sha256.lower() if sha256 else None)
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
            modified_time=mtime,
            trashed=trashed,
        )
        self._files[fid] = df
        self._contents[fid] = bytes(content)
        return fid

    def set_checksum(self, file_id: str, sha256: str | None) -> None:
        """設定指定檔案之 sha256（支援模擬上傳後 sha256 暫時為 None 之情境）。"""
        if file_id not in self._files:
            raise NotFound(f"找不到檔案: {file_id}")
        old_f = self._files[file_id]
        self._files[file_id] = DriveFile(
            id=old_f.id,
            name=old_f.name,
            mime_type=old_f.mime_type,
            parents=old_f.parents,
            size=old_f.size,
            sha256=sha256.lower() if sha256 else None,
            md5=old_f.md5,
            created_time=old_f.created_time,
            modified_time=old_f.modified_time,
            trashed=old_f.trashed,
        )

    def inject(
        self,
        op: str,
        file_id: str | None = None,
        *,
        error: type[Exception] = ReadError,
        times: int = 1,
    ) -> None:
        """注入指定操作的錯誤。"""
        self._injections.append({
            "op": op,
            "file_id": file_id,
            "error": error,
            "times": times,
        })

    def inject_nth_read(self, n: int, error: type[Exception] = ReadError) -> None:
        """設定在第 n 次讀取操作時（0-indexed）拋出指定例外。"""
        self._nth_read_injections[n] = error

    def _check_injections(self, op: str, file_id: str | None = None) -> None:
        for inj in list(self._injections):
            if inj["op"] == op:
                if inj["file_id"] is None or inj["file_id"] == file_id:
                    inj["times"] -= 1
                    if inj["times"] <= 0:
                        self._injections.remove(inj)
                    err_cls = inj["error"]
                    raise err_cls(f"FakeDrive injected error on {op}({file_id})")

    def _check_read_injection(self) -> None:
        idx = self._read_count
        self._read_count += 1
        if idx in self._nth_read_injections:
            err_cls = self._nth_read_injections[idx]
            raise err_cls(f"FakeDrive injected nth read error at index {idx}")

    def _lookup(self, file_id: str) -> DriveFile:
        """內部私有查詢：不計入 calls，不觸發 get 注入，供 download 與內部邏輯使用。"""
        f = self._files.get(file_id)
        if f is None or f.trashed:
            raise NotFound(f"找不到檔案: {file_id}")
        return f

    def _apply_order(self, items: list[DriveFile]) -> list[DriveFile]:
        if self._order == "reverse":
            return list(reversed(items))
        elif self._order == "shuffle":
            res = list(items)
            self._rng.shuffle(res)
            return res
        return list(items)

    def snapshot(self) -> dict[str, Any]:
        """取得目前所有檔案與內容之快照字典，供比對狀態未受異動。"""
        return {
            "files": copy.deepcopy(self._files),
            "contents": copy.deepcopy(self._contents),
        }

    # -----------------------------------------------------------------------
    # DriveClient 介面實作
    # -----------------------------------------------------------------------

    def list_children(self, folder_id: str) -> list[DriveFile]:
        self.calls.append(("list_children", folder_id))
        self._check_read_injection()
        self._check_injections("list_children", folder_id)
        result = [
            f for f in self._files.values()
            if not f.trashed and folder_id in f.parents
        ]
        return self._apply_order(result)

    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]:
        self.calls.append(("find_by_name", parent_id))
        self._check_read_injection()
        self._check_injections("find_by_name", parent_id)
        result = [
            f for f in self._files.values()
            if not f.trashed and parent_id in f.parents and f.name == name
        ]
        return self._apply_order(result)

    def get(self, file_id: str) -> DriveFile:
        self.calls.append(("get", file_id))
        self._check_read_injection()
        self._check_injections("get", file_id)
        return self._lookup(file_id)

    def download(self, file_id: str, dest: Path, *, max_bytes: int) -> int:
        self.calls.append(("download", file_id))
        self._check_read_injection()
        self._check_injections("download", file_id)

        # M6: 內部使用 _lookup 避免重複計入 get 注入與呼叫
        self._lookup(file_id)
        content = self._contents.get(file_id, b"")
        if len(content) > max_bytes:
            raise TooLarge(f"檔案大小 {len(content)} 超過上限 {max_bytes}")

        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return len(content)

    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        self.calls.append(("download_bytes", file_id))
        self._check_read_injection()
        self._check_injections("download_bytes", file_id)

        # M6: 內部使用 _lookup
        self._lookup(file_id)
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
        self.calls.append(("create", parent_id))
        self._check_injections("create", parent_id)

        # M6 & N3: 檢查父資料夾存在（root 除外），寫入操作找不到父資料夾拋出 WriteError
        if parent_id != "root" and parent_id not in self._files:
            raise WriteError(f"找不到父資料夾: {parent_id}")

        data = content.read_bytes() if isinstance(content, Path) else bytes(content)

        self._file_seq += 1
        fid = f"file_{self._file_seq:04d}"
        now = format_rfc3339(self._clock.now(), include_fraction=True)
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
        self.calls.append(("update_content", file_id))
        self._check_injections("update_content", file_id)

        # N3: 寫入操作找不到欲更新檔案拋出 WriteError
        if file_id not in self._files:
            raise WriteError(f"找不到欲更新之檔案: {file_id}")

        old_f = self._files[file_id]
        data = content.read_bytes() if isinstance(content, Path) else bytes(content)

        now = format_rfc3339(self._clock.now(), include_fraction=True)
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
        self.calls.append(("move", file_id))
        self._check_injections("move", file_id)

        # M6: 嚴格檢查檔案存在、from_parent 為實際 parent、to_parent 資料夾存在
        if file_id not in self._files:
            raise WriteError(f"找不到欲移動之檔案: {file_id}")

        old_f = self._files[file_id]
        if from_parent not in old_f.parents:
            raise WriteError(f"'{from_parent}' 不是檔案 '{file_id}' 的父資料夾 (parents={old_f.parents})")

        if to_parent != "root" and to_parent not in self._files:
            raise WriteError(f"目標父資料夾不存在: {to_parent}")

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
            modified_time=format_rfc3339(self._clock.now(), include_fraction=True),
            trashed=old_f.trashed,
        )
        self._files[file_id] = new_f
        return new_f

    def delete_permanently(self, file_id: str) -> None:
        self.calls.append(("delete_permanently", file_id))
        self._check_injections("delete_permanently", file_id)

        # N3: 404 視為成功（檔案已不存在，冪等刪除），記錄在 calls
        if file_id in self._files:
            del self._files[file_id]
        if file_id in self._contents:
            del self._contents[file_id]
