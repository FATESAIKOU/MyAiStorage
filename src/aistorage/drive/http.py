"""AiStorage Google Drive REST API HTTP 用戶端實作。

依據規格：
- docs/impl/group3-modules.md 第 2.1 節
- review-g3a.md M1（create 非冪等重試處理、move 冪等驗證、delete 404 視為成功）
- review-g3a.md M2（例外型別齊全化、IncompleteRead/KeyError/ValueError 轉換、401 刷新、403 限流重試）
- review-g3a.md L（查詢跳脫反斜線、下載重試刷新 token）
"""

from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid

from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient, DriveFile
from aistorage.errors import NotFound, ReadError, TooLarge, WriteError

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DEFAULT_FIELDS = "id,name,mimeType,parents,size,sha256Checksum,md5Checksum,createdTime,modifiedTime,trashed"


def _escape_q(val: str) -> str:
    """Drive API 查詢字串跳脫（L：先將 \\ 換成 \\\\，再將 ' 換成 \\'）。"""
    return val.replace("\\", "\\\\").replace("'", "\\'")


def _is_rate_limit_403(err: urllib.error.HTTPError) -> bool:
    """檢查 403 回應是否為限流（rateLimitExceeded 或 userRateLimitExceeded）。"""
    try:
        err_body = err.read().decode("utf-8", errors="replace")
        err_json = json.loads(err_body)
        reasons = [
            item.get("reason")
            for item in err_json.get("error", {}).get("errors", [])
        ]
        return any(r in ("rateLimitExceeded", "userRateLimitExceeded") for r in reasons)
    except Exception:
        return False


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

    def _invalidate_token(self) -> None:
        """M2: 遇到 401 時主動廢棄舊 token。"""
        if hasattr(self._token_provider, "invalidate"):
            self._token_provider.invalidate()

    def _request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        *,
        is_write: bool = False,
        retry_network_and_5xx: bool = True,
        ignore_404: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        """發送 HTTP 請求，支援 401 強制刷新一次、429/403 限流與 5xx 退避重試。

        M1: create 非冪等操作傳入 retry_network_and_5xx=False，僅在確定伺服器未處理的 429/403 重試。
        M2: 寫入遇到 404 拋出 WriteError（delete 可設定 ignore_404=True 視為成功）。
        """
        attempts = 0
        token_invalidated = False
        last_error: Exception | None = None

        while attempts <= self._max_retries:
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

            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_data = resp.read()
                    resp_headers = dict(resp.headers)
                    return resp.status, resp_headers, resp_data
            except urllib.error.HTTPError as e:
                last_error = e
                code = e.code

                if code == 404:
                    if ignore_404:
                        return 204, {}, b""
                    if is_write:
                        raise WriteError(f"Drive 寫入目標不存在 (HTTP 404): {url}", status_code=404) from None
                    raise NotFound(f"Drive 資源不存在 (HTTP 404): {url}") from None

                # M2: 401 嘗試強制刷新一次 token
                if code == 401 and not token_invalidated:
                    self._invalidate_token()
                    token_invalidated = True
                    attempts += 1
                    continue

                # 429 或 403 限流
                is_rate_limit = (code == 429) or (code == 403 and _is_rate_limit_403(e))
                if is_rate_limit and attempts < self._max_retries:
                    wait_sec = self._backoff_base * (2 ** attempts)
                    time.sleep(wait_sec)
                    attempts += 1
                    continue

                # 5xx 伺服器錯誤（受 retry_network_and_5xx 控制）
                if (500 <= code < 600) and retry_network_and_5xx and attempts < self._max_retries:
                    wait_sec = self._backoff_base * (2 ** attempts)
                    time.sleep(wait_sec)
                    attempts += 1
                    continue

                if is_write:
                    raise WriteError(f"Drive 請求失敗 (HTTP {code}): {url}", status_code=code) from None
                raise ReadError(f"Drive 請求失敗 (HTTP {code}): {url}") from None

            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as e:
                last_error = e
                if retry_network_and_5xx and attempts < self._max_retries:
                    wait_sec = self._backoff_base * (2 ** attempts)
                    time.sleep(wait_sec)
                    attempts += 1
                    continue
                err_cls = WriteError if is_write else ReadError
                raise err_cls(f"Drive 網路連線失敗: {e}") from None

        err_cls = WriteError if is_write else ReadError
        raise err_cls(f"Drive 重試超過上限: {last_error}") from None

    def _parse_drive_file(self, item: dict[str, Any], *, is_write: bool = False) -> DriveFile:
        """解析 Drive 回傳之 JSON 物件為 DriveFile（M2：捕捉 KeyError 與 ValueError）。"""
        err_cls = WriteError if is_write else ReadError
        try:
            file_id = item["id"]
            name = item.get("name", "")
            mime_type = item.get("mimeType", "")
            parents = tuple(item.get("parents", ()))
            size = int(item["size"]) if "size" in item else None
            sha256 = item.get("sha256Checksum", "").lower() or None
            md5 = item.get("md5Checksum", "").lower() or None
            created_time = item.get("createdTime", "")
            modified_time = item.get("modifiedTime", "")
            trashed = item.get("trashed", False)
            return DriveFile(
                id=file_id,
                name=name,
                mime_type=mime_type,
                parents=parents,
                size=size,
                sha256=sha256,
                md5=md5,
                created_time=created_time,
                modified_time=modified_time,
                trashed=trashed,
            )
        except (KeyError, ValueError, TypeError) as e:
            raise err_cls(f"解析 Drive 檔案物件欄位失敗: {e}") from None

    # -----------------------------------------------------------------------
    # DriveClient 介面實作
    # -----------------------------------------------------------------------

    def list_children(self, folder_id: str) -> list[DriveFile]:
        result: list[DriveFile] = []
        page_token: str | None = None
        safe_fid = _escape_q(folder_id)
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
                raise ReadError(f"解析 Drive list 回應失敗: {e}") from None

            for f_data in data.get("files", []):
                result.append(self._parse_drive_file(f_data, is_write=False))

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return result

    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]:
        result: list[DriveFile] = []
        page_token: str | None = None
        safe_pid = _escape_q(parent_id)
        safe_name = _escape_q(name)
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
                raise ReadError(f"解析 Drive find_by_name 回應失敗: {e}") from None

            for f_data in data.get("files", []):
                result.append(self._parse_drive_file(f_data, is_write=False))

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
            raise ReadError(f"解析 Drive get 回應失敗: {e}") from None

        df = self._parse_drive_file(data, is_write=False)
        if df.trashed:
            raise NotFound(f"檔案已被丟入垃圾桶: {file_id}")
        return df

    def download(self, file_id: str, dest: Path, *, max_bytes: int) -> int:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest_path.with_suffix(dest_path.suffix + f".tmp.{uuid.uuid4().hex[:6]}")

        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        attempts = 0
        token_invalidated = False

        while attempts <= self._max_retries:
            total_read = 0
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            req = urllib.request.Request(url, headers=headers)

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
                    raise NotFound(f"找不到檔案 (HTTP 404): {file_id}") from None

                if e.code == 401 and not token_invalidated:
                    self._invalidate_token()
                    token_invalidated = True
                    attempts += 1
                    continue

                is_rate_limit = (e.code == 429) or (e.code == 403 and _is_rate_limit_403(e))
                if (is_rate_limit or (500 <= e.code < 600)) and attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    continue

                tmp_dest.unlink(missing_ok=True)
                raise ReadError(f"下載失敗 (HTTP {e.code}): {file_id}") from None

            except TooLarge:
                tmp_dest.unlink(missing_ok=True)
                raise

            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as e:
                if attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    continue
                tmp_dest.unlink(missing_ok=True)
                raise ReadError(f"下載網路失敗: {e}") from None

        tmp_dest.unlink(missing_ok=True)
        raise ReadError(f"下載重試次數耗盡: {file_id}")

    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        bio = io.BytesIO()
        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        attempts = 0
        token_invalidated = False

        while attempts <= self._max_retries:
            total_read = 0
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            req = urllib.request.Request(url, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    chunk_size = 65536
                    bio.seek(0)
                    bio.truncate()
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
                    raise NotFound(f"找不到檔案 (HTTP 404): {file_id}") from None

                if e.code == 401 and not token_invalidated:
                    self._invalidate_token()
                    token_invalidated = True
                    attempts += 1
                    continue

                is_rate_limit = (e.code == 429) or (e.code == 403 and _is_rate_limit_403(e))
                if (is_rate_limit or (500 <= e.code < 600)) and attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    continue
                raise ReadError(f"下載失敗 (HTTP {e.code}): {file_id}") from None

            except TooLarge:
                raise

            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as e:
                if attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    continue
                raise ReadError(f"下載網路失敗: {e}") from None

        raise ReadError(f"下載重試次數耗盡: {file_id}")

    def create(
        self,
        parent_id: str,
        name: str,
        content: bytes | Path,
        *,
        mime_type: str = "application/octet-stream",
    ) -> DriveFile:
        """建立新檔案。

        M1: create 非冪等，僅在 429 或限流 403 重試；遇 5xx 或逾時直接拋出 WriteError，絕不重複發送。
        """
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
        # M1: retry_network_and_5xx=False
        _, _, resp_data = self._request(
            url,
            method="POST",
            headers=headers,
            data=body,
            is_write=True,
            retry_network_and_5xx=False,
        )

        try:
            resp_json = json.loads(resp_data.decode("utf-8"))
        except Exception as e:
            raise WriteError(f"解析 Drive create 回應失敗: {e}") from None

        return self._parse_drive_file(resp_json, is_write=True)

    def update_content(self, file_id: str, content: bytes | Path) -> DriveFile:
        """更新檔案內容（PATCH 媒體內容，屬冪等寫入）。"""
        data = content.read_bytes() if isinstance(content, Path) else bytes(content)
        url = f"{DRIVE_UPLOAD_BASE}/files/{file_id}?uploadType=media&fields={DEFAULT_FIELDS}&supportsAllDrives=true"
        headers = {"Content-Type": "application/octet-stream"}
        _, _, resp_data = self._request(
            url,
            method="PATCH",
            headers=headers,
            data=data,
            is_write=True,
            retry_network_and_5xx=True,
        )

        try:
            resp_json = json.loads(resp_data.decode("utf-8"))
        except Exception as e:
            raise WriteError(f"解析 Drive update_content 回應失敗: {e}") from None

        return self._parse_drive_file(resp_json, is_write=True)

    def move(self, file_id: str, *, from_parent: str, to_parent: str) -> DriveFile:
        """移動檔案至新資料夾。

        M1: 若因網路或逾時重試，重試前與錯誤後檢查目標檔案 parents：若已達目標狀態則視為成功。
        """
        attempts = 0
        while attempts <= self._max_retries:
            if attempts > 0:
                try:
                    cur = self.get(file_id)
                    if to_parent in cur.parents and from_parent not in cur.parents:
                        return cur
                except Exception:
                    pass

            params = {
                "addParents": to_parent,
                "removeParents": from_parent,
                "fields": DEFAULT_FIELDS,
                "supportsAllDrives": "true",
            }
            url = f"{DRIVE_API_BASE}/files/{file_id}?{urllib.parse.urlencode(params)}"
            try:
                _, _, resp_data = self._request(
                    url,
                    method="PATCH",
                    is_write=True,
                    retry_network_and_5xx=False,  # 由 move 自身掌控冪等重試
                )
                try:
                    resp_json = json.loads(resp_data.decode("utf-8"))
                except Exception as e:
                    raise WriteError(f"解析 Drive move 回應失敗: {e}") from None
                return self._parse_drive_file(resp_json, is_write=True)
            except WriteError as e:
                # 檢查是否前次嘗試已成功生效
                try:
                    cur = self.get(file_id)
                    if to_parent in cur.parents and from_parent not in cur.parents:
                        return cur
                except Exception:
                    pass

                # N8: 僅對暫時性失敗（網路/連線錯誤 status_code is None、5xx 或 429）重試
                # 非暫時性錯誤（400、權限 403、404）不進行無意義重試
                is_transient = (
                    e.status_code is None
                    or (500 <= e.status_code < 600)
                    or e.status_code == 429
                )
                if not is_transient:
                    raise

                if attempts < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempts))
                    attempts += 1
                    continue
                raise

        raise WriteError(f"Drive move 重試次數耗盡: {file_id}")

    def share(
        self, file_id: str, *, email: str, role: str = "reader"
    ) -> str:
        """分享檔案／資料夾給指定 email（Drive API permissions.create）。

        1.5 的驗證就是用這個呼叫把讀取用的 service account 加成檢視者。
        `permissions.create` 不是冪等操作，所以不重試 5xx（與 create 同一個考量）；
        4xx／權限不足原樣往上拋（`DriveError`）。
        """
        body = json.dumps({
            "type": "user", "role": role, "emailAddress": email,
        }).encode("utf-8")
        url = (f"{DRIVE_API_BASE}/files/{file_id}/permissions"
               f"?fields=id&supportsAllDrives=true")
        _status, _headers, data = self._request(
            url, method="POST", data=body,
            headers={"Content-Type": "application/json"},
            is_write=True, retry_network_and_5xx=False)
        try:
            return str(json.loads(data.decode("utf-8")).get("id", ""))
        except (ValueError, AttributeError):
            return ""

    def delete_permanently(self, file_id: str) -> None:
        """永久刪除檔案。

        M1/M2/N3: 遇到 404 視為成功（檔案已不存在，冪等刪除）。
        注意：delete 在第一次呼叫就回 404 時亦會視為成功。
        因此呼叫端（如 gc）執行前必須先透過 get() 嚴格確認其 parents 與元資料，
        不能單靠 delete 的成功回應作為目標 id 正確無誤的依據。
        """
        url = f"{DRIVE_API_BASE}/files/{file_id}?supportsAllDrives=true"
        self._request(url, method="DELETE", is_write=True, ignore_404=True)
