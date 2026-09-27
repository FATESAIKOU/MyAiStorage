"""AiStorage Google Drive REST API HTTP 用戶端實作。

依據規格：docs/impl/group3-modules.md 第 2.1 節
- HttpDriveClient: 依 Google Drive v3 REST API 實作 DriveClient 協定
- 使用標準庫 urllib（無外部依賴）
- 針對 429 與 5xx 具備指數退避重試（至多 3 次），重試耗盡拋出 ReadError/WriteError
- 404 拋出 NotFound；下載超過上限立即停止並拋出 TooLarge
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient, DriveFile
from aistorage.errors import NotFound, ReadError, TooLarge, WriteError

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DEFAULT_FIELDS = "id,name,mimeType,parents,size,sha256Checksum,md5Checksum,createdTime,modifiedTime,trashed"


class HttpDriveClient(DriveClient):
    """Google Drive REST v3 用戶端。"""

    def __init__(
        self,
        token_provider: Any,
        *,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        """
        Args:
            token_provider: 具備 access_token() 方法的物件或直接提供 token 的 callable。
            max_retries: 429 / 5xx 最大重試次數。
            backoff_base: 退避基準秒數。
        """
        self._token_provider = token_provider
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _get_token(self) -> str:
        if hasattr(self._token_provider, "access_token"):
            return self._token_provider.access_token()
        if callable(self._token_provider):
            return self._token_provider()
        return str(self._token_provider)

    def _request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        *,
        is_write: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        """發送 HTTP 請求，含 429/5xx 指數退避重試。"""
        all_headers = {
            "Authorization": f"Bearer {self._get_token()}",
        }
        if headers:
            all_headers.update(headers)

        req = urllib.request.Request(
            url,
            data=data,
            headers=all_headers,
            method=method,
        )

        attempts = 0
        last_error: Exception | None = None

        while attempts <= self._max_retries:
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_data = resp.read()
                    resp_headers = dict(resp.headers)
                    return resp.status, resp_headers, resp_data
            except urllib.error.HTTPError as e:
                last_error = e
                code = e.code
                if code == 404:
                    raise NotFound(f"Drive 資源不存在 (HTTP 404): {url}") from e

                # 429 或 5xx: 執行重試
                if (code == 429 or 500 <= code < 600) and attempts < self._max_retries:
                    wait_sec = self._backoff_base * (2 ** attempts)
                    time.sleep(wait_sec)
                    attempts += 1
                    # 重新取得 token（避免過期）
                    req.headers["Authorization"] = f"Bearer {self._get_token()}"
                    continue

                err_cls = WriteError if is_write else ReadError
                raise err_cls(f"Drive 請求失敗 (HTTP {code}): {url}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
                if attempts < self._max_retries:
                    wait_sec = self._backoff_base * (2 ** attempts)
                    time.sleep(wait_sec)
                    attempts += 1
                    continue
                err_cls = WriteError if is_write else ReadError
                raise err_cls(f"Drive 網路連線失敗: {e}") from e

        err_cls = WriteError if is_write else ReadError
        raise err_cls(f"Drive 重試超過上限: {last_error}")

    def _parse_drive_file(self, item: dict[str, Any]) -> DriveFile:
        return DriveFile(
            id=item["id"],
            name=item.get("name", ""),
            mime_type=item.get("mimeType", ""),
            parents=tuple(item.get("parents", ())),
            size=int(item["size"]) if "size" in item else None,
            sha256=item.get("sha256Checksum", "").lower() or None,
            md5=item.get("md5Checksum", "").lower() or None,
            created_time=item.get("createdTime", ""),
            modified_time=item.get("modifiedTime", ""),
            trashed=item.get("trashed", False),
        )

    # -----------------------------------------------------------------------
    # DriveClient 介面實作
    # -----------------------------------------------------------------------

    def list_children(self, folder_id: str) -> list[DriveFile]:
        result: list[DriveFile] = []
        page_token: str | None = None
        safe_fid = folder_id.replace("'", "\\'")
        q = f"'{safe_fid}' in parents and trashed = false"

        while True:
            params = {
                "q": q,
                "fields": f"nextPageToken,files({DEFAULT_FIELDS})",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token

            query_str = urllib.parse.urlencode(params)
            url = f"{DRIVE_API_BASE}/files?{query_str}"
            _, _, body = self._request(url)

            try:
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                raise ReadError(f"解析 Drive list 回應失敗: {e}") from e

            for f_data in data.get("files", []):
                result.append(self._parse_drive_file(f_data))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return result

    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]:
        result: list[DriveFile] = []
        page_token: str | None = None
        safe_pid = parent_id.replace("'", "\\'")
        safe_name = name.replace("'", "\\'")
        q = f"'{safe_pid}' in parents and name = '{safe_name}' and trashed = false"

        while True:
            params = {
                "q": q,
                "fields": f"nextPageToken,files({DEFAULT_FIELDS})",
                "pageSize": "100",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token

            query_str = urllib.parse.urlencode(params)
            url = f"{DRIVE_API_BASE}/files?{query_str}"
            _, _, body = self._request(url)

            try:
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                raise ReadError(f"解析 Drive find_by_name 回應失敗: {e}") from e

            for f_data in data.get("files", []):
                result.append(self._parse_drive_file(f_data))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return result

    def get(self, file_id: str) -> DriveFile:
        params = {
            "fields": DEFAULT_FIELDS,
            "supportsAllDrives": "true",
        }
        url = f"{DRIVE_API_BASE}/files/{file_id}?{urllib.parse.urlencode(params)}"
        _, _, body = self._request(url)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            raise ReadError(f"解析 Drive get 回應失敗: {e}") from e

        df = self._parse_drive_file(data)
        if df.trashed:
            raise NotFound(f"檔案已被丟入垃圾桶: {file_id}")
        return df

    def download(self, file_id: str, dest: Path, *, max_bytes: int) -> int:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest_path.with_suffix(dest_path.suffix + f".tmp.{uuid.uuid4().hex[:6]}")

        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        req = urllib.request.Request(url, headers=headers)

        attempts = 0
        total_read = 0

        while attempts <= self._max_retries:
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    with open(tmp_dest, "wb") as f_out:
                        chunk_size = 65536
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            total_read += len(chunk)
                            if total_read > max_bytes:
                                tmp_dest.unlink(missing_ok=True)
                                raise TooLarge(f"檔案下載大小 {total_read} 超過上限 {max_bytes}")
                            f_out.write(chunk)

                tmp_dest.replace(dest_path)
                return total_read
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    tmp_dest.unlink(missing_ok=True)
                    raise NotFound(f"找不到檔案 (HTTP 404): {file_id}") from e
                if (e.code == 429 or 500 <= e.code < 600) and attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    total_read = 0
                    continue
                tmp_dest.unlink(missing_ok=True)
                raise ReadError(f"下載失敗 (HTTP {e.code}): {file_id}") from e
            except TooLarge:
                raise
            except Exception as e:
                if attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    total_read = 0
                    continue
                tmp_dest.unlink(missing_ok=True)
                raise ReadError(f"下載網路失敗: {e}") from e

        tmp_dest.unlink(missing_ok=True)
        raise ReadError(f"下載重試次數耗盡: {file_id}")

    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        bio = io.BytesIO()
        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        req = urllib.request.Request(url, headers=headers)

        attempts = 0
        total_read = 0

        while attempts <= self._max_retries:
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    chunk_size = 65536
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        total_read += len(chunk)
                        if total_read > max_bytes:
                            raise TooLarge(f"檔案下載大小 {total_read} 超過上限 {max_bytes}")
                        bio.write(chunk)
                return bio.getvalue()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(f"找不到檔案 (HTTP 404): {file_id}") from e
                if (e.code == 429 or 500 <= e.code < 600) and attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    total_read = 0
                    bio.seek(0)
                    bio.truncate()
                    continue
                raise ReadError(f"下載失敗 (HTTP {e.code}): {file_id}") from e
            except TooLarge:
                raise
            except Exception as e:
                if attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    total_read = 0
                    bio.seek(0)
                    bio.truncate()
                    continue
                raise ReadError(f"下載網路失敗: {e}") from e

        raise ReadError(f"下載重試次數耗盡: {file_id}")

    def create(
        self,
        parent_id: str,
        name: str,
        content: bytes | Path,
        *,
        mime_type: str = "application/octet-stream",
    ) -> DriveFile:
        data = content.read_bytes() if isinstance(content, Path) else bytes(content)

        # Multipart 上傳
        boundary = f"===============BOUNDARY_{uuid.uuid4().hex}==============="
        metadata = {
            "name": name,
            "parents": [parent_id],
            "mimeType": mime_type,
        }

        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        headers = {
            "Content-Type": f"multipart/related; boundary={boundary}",
        }
        url = f"{DRIVE_UPLOAD_BASE}/files?uploadType=multipart&fields={DEFAULT_FIELDS}&supportsAllDrives=true"
        _, _, resp_data = self._request(url, method="POST", headers=headers, data=body, is_write=True)

        try:
            resp_json = json.loads(resp_data.decode("utf-8"))
        except Exception as e:
            raise WriteError(f"解析 Drive create 回應失敗: {e}") from e

        return self._parse_drive_file(resp_json)

    def update_content(self, file_id: str, content: bytes | Path) -> DriveFile:
        data = content.read_bytes() if isinstance(content, Path) else bytes(content)
        url = f"{DRIVE_UPLOAD_BASE}/files/{file_id}?uploadType=media&fields={DEFAULT_FIELDS}&supportsAllDrives=true"
        headers = {"Content-Type": "application/octet-stream"}
        _, _, resp_data = self._request(url, method="PATCH", headers=headers, data=data, is_write=True)

        try:
            resp_json = json.loads(resp_data.decode("utf-8"))
        except Exception as e:
            raise WriteError(f"解析 Drive update_content 回應失敗: {e}") from e

        return self._parse_drive_file(resp_json)

    def move(self, file_id: str, *, from_parent: str, to_parent: str) -> DriveFile:
        params = {
            "addParents": to_parent,
            "removeParents": from_parent,
            "fields": DEFAULT_FIELDS,
            "supportsAllDrives": "true",
        }
        url = f"{DRIVE_API_BASE}/files/{file_id}?{urllib.parse.urlencode(params)}"
        _, _, resp_data = self._request(url, method="PATCH", is_write=True)

        try:
            resp_json = json.loads(resp_data.decode("utf-8"))
        except Exception as e:
            raise WriteError(f"解析 Drive move 回應失敗: {e}") from e

        return self._parse_drive_file(resp_json)

    def delete_permanently(self, file_id: str) -> None:
        url = f"{DRIVE_API_BASE}/files/{file_id}?supportsAllDrives=true"
        self._request(url, method="DELETE", is_write=True)
