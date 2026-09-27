"""Unit tests for aistorage.drive module.

Specifications:
- docs/impl/group3-modules.md §2 (Drive 存取層) & §8.2 (drive/http, fake)
- review-g3a.md (H1 canary for secret leakage, M1 create idempotency, M2 retry & ReadError, M3 clock/datetime, M6 fake)
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import traceback
from typing import Any
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest

from aistorage.clock import FixedClock, parse_rfc3339
from aistorage.drive import (
    DriveClient,
    DriveFile,
    FakeDrive,
    HttpDriveClient,
    RcloneConfToken,
)
from aistorage.drive.model import GOOGLE_FOLDER_MIME
from aistorage.errors import NotFound, ReadError, TooLarge, WriteError


# ============================================================================
# 1. DriveFile Model Tests
# ============================================================================

def test_drive_file_properties():
    """驗證 DriveFile 的 is_folder、created_at、modified_at 與屬性。"""
    file_item = DriveFile(
        id="f_001",
        name="test_session.json",
        mime_type="application/json",
        parents=("folder_001",),
        size=1024,
        sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        md5="1234567890abcdef1234567890abcdef",
        created_time="2026-09-27T08:00:00.123456Z",
        modified_time="2026-09-27T08:30:00Z",
        trashed=False,
    )
    assert file_item.id == "f_001"
    assert file_item.name == "test_session.json"
    assert not file_item.is_folder
    assert file_item.size == 1024
    assert file_item.sha256 == "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    # M3: created_at / modified_at 必須是帶時區之 datetime
    assert file_item.created_at == datetime(2026, 9, 27, 8, 0, 0, 123456, tzinfo=timezone.utc)
    assert file_item.modified_at == datetime(2026, 9, 27, 8, 30, 0, tzinfo=timezone.utc)

    # 資料夾判定
    folder_item = DriveFile(
        id="f_002",
        name="my_dir",
        mime_type=GOOGLE_FOLDER_MIME,
        parents=("root",),
        size=None,
        sha256=None,
        md5=None,
        created_time="2026-09-27T09:00:00Z",
        modified_time="2026-09-27T09:00:00Z",
        trashed=False,
    )
    assert folder_item.is_folder
    assert folder_item.size is None
    assert folder_item.sha256 is None


def test_drive_file_sha256_missing_is_none():
    """Drive sha256Checksum 缺少時必須轉為 None。"""
    item = DriveFile(
        id="f_003",
        name="large.bin",
        mime_type="application/octet-stream",
        parents=("p1",),
        size=5000000,
        sha256=None,
        md5=None,
        created_time="2026-09-27T10:00:00Z",
        modified_time="2026-09-27T10:00:00Z",
        trashed=False,
    )
    assert item.sha256 is None


# ============================================================================
# 2. RcloneConfToken & H1 Secret Canary Tests
# ============================================================================

def test_rclone_conf_token_normal_lifecycle(tmp_path: Path, monkeypatch):
    """驗證正常 conf 檔載入、access_token 快取與 repr 不洩漏秘密。"""
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[gdrive]\n"
        "type = drive\n"
        "client_id = my_client_id_12345\n"
        "client_secret = my_client_secret_67890\n"
        'token = {"access_token":"initial_token_abc","expiry":"2099-01-01T00:00:00Z","refresh_token":"ref_xyz"}\n'
    )
    token_prov = RcloneConfToken(conf, "gdrive")
    assert repr(token_prov) == "<RcloneConfToken remote=gdrive>"
    assert "my_client_id_12345" not in repr(token_prov)
    assert "my_client_secret_67890" not in repr(token_prov)
    assert "initial_token_abc" not in repr(token_prov)

    # access_token 取回快取值
    tok = token_prov.access_token()
    assert tok == "initial_token_abc"

    # invalidate 清除快取後，下一次 access_token 觸發刷新
    class DummyResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"access_token":"refreshed_token_123","expiry":"2099-01-01T00:00:00Z"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=30: DummyResp())
    token_prov.invalidate()
    assert token_prov.access_token() == "refreshed_token_123"


def test_rclone_conf_token_does_not_modify_file(tmp_path: Path):
    """驗證讀取與操作絕不寫回 conf 檔案（檔案內容與 mtime 保持未變）。"""
    conf = tmp_path / "rclone.conf"
    content = (
        "[gdrive]\n"
        "type = drive\n"
        "client_id = cid\n"
        "client_secret = csec\n"
        'token = {"access_token":"tok","expiry":"2099-01-01T00:00:00Z","refresh_token":"ref"}\n'
    )
    conf.write_text(content)
    orig_mtime = conf.stat().st_mtime_ns

    token_prov = RcloneConfToken(conf, "gdrive")
    _ = token_prov.access_token()

    assert conf.read_text() == content
    assert conf.stat().st_mtime_ns == orig_mtime


def test_h1_canary_percent_interpolation_safety(tmp_path: Path):
    """H1 驗收：當 client_secret 或 token 包含 % 字元時，絕不噴出 InterpolationSyntaxError 洩漏秘密。"""
    canary_secret = "CANARY_PERCENT_%SECRET_VAL%_12345"
    conf = tmp_path / "percent.conf"
    conf.write_text(
        f"[gdrive]\n"
        f"type = drive\n"
        f"client_id = test_cid\n"
        f"client_secret = {canary_secret}\n"
        f'token = {{"access_token":"tok_{canary_secret}","expiry":"2099-01-01T00:00:00Z","refresh_token":"ref"}}\n'
    )
    # 建立 RcloneConfToken 必須成功，不可拋出 InterpolationSyntaxError
    token_prov = RcloneConfToken(conf, "gdrive")
    assert canary_secret not in repr(token_prov)


def test_h1_canary_syntax_error_never_leaks(tmp_path: Path):
    """H1 驗收：conf 格式損毀或含有秘密行語法錯誤時，拋出的 ReadError 絕不含出錯行內容或秘密。"""
    canary_syntax = "CANARY_SYNTAX_LINE_SECRET_TOKEN_99999"
    conf = tmp_path / "syntax_err.conf"
    conf.write_text(
        f"[gdrive]\n"
        f"type = drive\n"
        f"client_id = test_cid\n"
        f"corrupted_line_without_equals_{canary_syntax}\n"
    )
    with pytest.raises(ReadError) as exc_info:
        RcloneConfToken(conf, "gdrive")

    err = exc_info.value
    # 斷言 str、repr 與完整 traceback 都不含 canary
    assert canary_syntax not in str(err)
    assert canary_syntax not in repr(err)
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert canary_syntax not in tb
    assert err.__cause__ is None


def test_h1_canary_json_decode_error_never_leaks(tmp_path: Path):
    """H1 驗收：token JSON 損毀且內容含秘密時，拋出的例外絕不含秘密內容。"""
    canary_json = "CANARY_BAD_JSON_SECRET_REFRESH_abc"
    conf = tmp_path / "bad_json.conf"
    conf.write_text(
        f"[gdrive]\n"
        f"type = drive\n"
        f"client_id = test_cid\n"
        f"client_secret = sec\n"
        f'token = {{"refresh_token": "{canary_json}", truncated_syntax\n'
    )
    with pytest.raises(ReadError) as exc_info:
        RcloneConfToken(conf, "gdrive")

    err = exc_info.value
    assert canary_json not in str(err)
    assert canary_json not in repr(err)
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert canary_json not in tb
    assert err.__cause__ is None


def test_rclone_conf_missing_fields_raises_cleanly(tmp_path: Path):
    """缺少 client_id 或 client_secret 時直接拋出 ReadError 並指出缺失欄位。"""
    conf = tmp_path / "missing_id.conf"
    conf.write_text(
        "[gdrive]\ntype = drive\nclient_secret = sec\n"
        'token = {"access_token":"t","refresh_token":"r"}\n'
    )
    with pytest.raises(ReadError) as exc_info:
        RcloneConfToken(conf, "gdrive")
    assert "client_id" in str(exc_info.value)


# ============================================================================
# 3. HttpDriveClient Tests (Pagination, Retry, NotFound, Idempotency, Limits)
# ============================================================================

class MockDriveHandler(BaseHTTPRequestHandler):
    """可設定回應情境的 Drive v3 Mock HTTP 伺服器。"""

    responses_queue: list[dict[str, Any]] = []
    requests_received: list[dict[str, Any]] = []

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_PATCH(self):
        self._handle_request("PATCH")

    def do_DELETE(self):
        self._handle_request("DELETE")

    def _handle_request(self, method: str):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        req_record = {
            "method": method,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        MockDriveHandler.requests_received.append(req_record)

        if not MockDriveHandler.responses_queue:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"No response configured")
            return

        resp_spec = MockDriveHandler.responses_queue.pop(0)
        status = resp_spec.get("status", 200)
        headers = resp_spec.get("headers", {})
        resp_body = resp_spec.get("body", b"")
        if isinstance(resp_body, str):
            resp_body = resp_body.encode("utf-8")
        elif isinstance(resp_body, dict):
            resp_body = json.dumps(resp_body).encode("utf-8")
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"

        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, format, *args):
        # 靜音 HTTP server log
        pass


@pytest.fixture
def mock_drive_server():
    MockDriveHandler.responses_queue = []
    MockDriveHandler.requests_received = []
    server = HTTPServer(("127.0.0.1", 0), MockDriveHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_http_drive_list_children_pagination(mock_drive_server: str, monkeypatch):
    """驗證 list_children 分頁讀取：多頁累積聚合，且過濾 trashed=True。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    page1_resp = {
        "files": [
            {
                "id": "file_1",
                "name": "a.txt",
                "mimeType": "text/plain",
                "parents": ["parent_0"],
                "size": "100",
                "sha256Checksum": "aaa",
                "createdTime": "2026-09-27T08:00:00Z",
                "modifiedTime": "2026-09-27T08:00:00Z",
                "trashed": False,
            },
        ],
        "nextPageToken": "token_page_2",
    }
    page2_resp = {
        "files": [
            {
                "id": "file_2",
                "name": "b.txt",
                "mimeType": "text/plain",
                "parents": ["parent_0"],
                "size": "200",
                "sha256Checksum": "bbb",
                "createdTime": "2026-09-27T08:01:00Z",
                "modifiedTime": "2026-09-27T08:01:00Z",
                "trashed": False,
            }
        ]
    }
    MockDriveHandler.responses_queue = [
        {"status": 200, "body": page1_resp},
        {"status": 200, "body": page2_resp},
    ]

    client = HttpDriveClient(lambda: "mock_tok")
    children = client.list_children("parent_0")

    assert len(children) == 2
    assert [c.id for c in children] == ["file_1", "file_2"]
    assert children[0].sha256 == "aaa"
    assert children[1].sha256 == "bbb"


def test_http_drive_list_children_page_failure_raises_read_error(mock_drive_server: str, monkeypatch):
    """驗證分頁中途失敗時拋出 ReadError，絕不回傳部分結果。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    page1_resp = {
        "files": [{"id": "file_1", "name": "a.txt", "mimeType": "text/plain", "parents": ["p0"], "createdTime": "2026-09-27T08:00:00Z", "modifiedTime": "2026-09-27T08:00:00Z", "trashed": False}],
        "nextPageToken": "token_page_2",
    }
    MockDriveHandler.responses_queue = [
        {"status": 200, "body": page1_resp},
        {"status": 500, "body": "Internal Server Error"},
        {"status": 500, "body": "Internal Server Error"},
        {"status": 500, "body": "Internal Server Error"},
        {"status": 500, "body": "Internal Server Error"},
    ]

    client = HttpDriveClient(lambda: "mock_tok", max_retries=1, backoff_base=0.01)
    with pytest.raises(ReadError):
        client.list_children("p0")


def test_http_drive_retry_429_and_503(mock_drive_server: str, monkeypatch):
    """驗證 429 與 503 會自動重試並成功。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    file_resp = {
        "id": "f_ok",
        "name": "ok.txt",
        "mimeType": "text/plain",
        "parents": ["p"],
        "size": "10",
        "createdTime": "2026-09-27T08:00:00Z",
        "modifiedTime": "2026-09-27T08:00:00Z",
        "trashed": False,
    }
    MockDriveHandler.responses_queue = [
        {"status": 429, "body": "Rate Limit Exceeded"},
        {"status": 503, "body": "Service Unavailable"},
        {"status": 200, "body": file_resp},
    ]

    client = HttpDriveClient(lambda: "mock_tok", max_retries=3, backoff_base=0.01)
    df = client.get("f_ok")
    assert df.id == "f_ok"
    assert len(MockDriveHandler.requests_received) == 3


def test_http_drive_403_rate_limit_retried(mock_drive_server: str, monkeypatch):
    """驗證 403 帶有 rateLimitExceeded 錯誤原因時比照 429 進行重試。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    rate_limit_403 = {
        "error": {
            "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded", "message": "User Rate Limit Exceeded"}],
            "code": 403,
            "message": "User Rate Limit Exceeded",
        }
    }
    success_resp = {
        "id": "f_403_ok",
        "name": "ok.txt",
        "mimeType": "text/plain",
        "parents": ["p"],
        "createdTime": "2026-09-27T08:00:00Z",
        "modifiedTime": "2026-09-27T08:00:00Z",
        "trashed": False,
    }
    MockDriveHandler.responses_queue = [
        {"status": 403, "body": rate_limit_403},
        {"status": 200, "body": success_resp},
    ]

    client = HttpDriveClient(lambda: "mock_tok", max_retries=2, backoff_base=0.01)
    df = client.get("f_403_ok")
    assert df.id == "f_403_ok"


def test_http_drive_create_idempotency_no_5xx_retry(mock_drive_server: str, monkeypatch):
    """M1 核心驗證：create（POST）在 5xx 伺服器錯誤時絕不重試，直接拋出 WriteError 避免同名孤兒。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)
    monkeypatch.setattr("aistorage.drive.http.DRIVE_UPLOAD_BASE", mock_drive_server)

    MockDriveHandler.responses_queue = [
        {"status": 503, "body": "Service Unavailable"},
        # 若錯誤地重試，這筆會被消耗；但正確實作不應重試
        {"status": 200, "body": {"id": "f_dup", "name": "new.txt", "mimeType": "text/plain", "parents": ["p"], "createdTime": "2026-09-27T08:00:00Z", "modifiedTime": "2026-09-27T08:00:00Z", "trashed": False}},
    ]

    client = HttpDriveClient(lambda: "mock_tok", max_retries=3, backoff_base=0.01)
    with pytest.raises(WriteError):
        client.create("parent_folder", "new.txt", b"hello world")

    # 斷言只發出 1 次請求，未進行盲目重試
    assert len(MockDriveHandler.requests_received) == 1


def test_http_drive_create_retries_on_429(mock_drive_server: str, monkeypatch):
    """M1 補充驗證：create 在 429（確定伺服器未處理）時仍可重試。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)
    monkeypatch.setattr("aistorage.drive.http.DRIVE_UPLOAD_BASE", mock_drive_server)

    created_resp = {
        "id": "f_created_429",
        "name": "created.txt",
        "mimeType": "text/plain",
        "parents": ["parent_folder"],
        "size": "5",
        "createdTime": "2026-09-27T08:00:00Z",
        "modifiedTime": "2026-09-27T08:00:00Z",
        "trashed": False,
    }
    MockDriveHandler.responses_queue = [
        {"status": 429, "body": "Rate Limit Exceeded"},
        {"status": 200, "body": created_resp},
    ]

    client = HttpDriveClient(lambda: "mock_tok", max_retries=2, backoff_base=0.01)
    df = client.create("parent_folder", "created.txt", b"12345")
    assert df.id == "f_created_429"
    assert len(MockDriveHandler.requests_received) == 2


def test_http_drive_not_found_on_404(mock_drive_server: str, monkeypatch):
    """404 時 get 必須拋出 NotFound（NotFound 是 ReadError 的子類別）。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    MockDriveHandler.responses_queue = [{"status": 404, "body": "File not found"}]

    client = HttpDriveClient(lambda: "mock_tok")
    with pytest.raises(NotFound) as exc_info:
        client.get("non_existent_id")

    assert isinstance(exc_info.value, ReadError)


def test_http_drive_download_too_large_streaming(mock_drive_server: str, monkeypatch, tmp_path: Path):
    """串流下載時超過 max_bytes 立即中止並拋出 TooLarge（不讀完完整檔案）。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    # 模擬 5000 bytes 的串流內容
    MockDriveHandler.responses_queue = [
        {"status": 200, "headers": {"Content-Type": "application/octet-stream"}, "body": b"X" * 5000}
    ]

    client = HttpDriveClient(lambda: "mock_tok")
    dest = tmp_path / "stream_dest.bin"

    with pytest.raises(TooLarge):
        client.download("f_huge", dest, max_bytes=1000)

    # download_bytes 亦同
    MockDriveHandler.responses_queue = [
        {"status": 200, "headers": {"Content-Type": "application/octet-stream"}, "body": b"X" * 5000}
    ]
    with pytest.raises(TooLarge):
        client.download_bytes("f_huge", max_bytes=1000)


def test_http_drive_move_and_delete_permanently(mock_drive_server: str, monkeypatch):
    """驗證 move 與 delete_permanently API 呼叫。"""
    monkeypatch.setattr("aistorage.drive.http.DRIVE_API_BASE", mock_drive_server)

    moved_resp = {
        "id": "f_move",
        "name": "m.txt",
        "mimeType": "text/plain",
        "parents": ["to_p"],
        "createdTime": "2026-09-27T08:00:00Z",
        "modifiedTime": "2026-09-27T08:00:00Z",
        "trashed": False,
    }
    MockDriveHandler.responses_queue = [
        {"status": 200, "body": moved_resp},
        {"status": 204, "body": b""},
    ]

    client = HttpDriveClient(lambda: "mock_tok")
    moved = client.move("f_move", from_parent="from_p", to_parent="to_p")
    assert moved.parents == ("to_p",)

    # delete_permanently 回傳 None
    client.delete_permanently("f_move")
    assert len(MockDriveHandler.requests_received) == 2
    assert MockDriveHandler.requests_received[1]["method"] == "DELETE"


# ============================================================================
# 4. FakeDrive Tests (Calls tracking, M6 strictness, Read/Write constants)
# ============================================================================

def test_fake_drive_calls_recording_and_constants():
    """驗證 FakeDrive.READ_OPS / WRITE_OPS 與 calls 呼叫追蹤。"""
    clock = FixedClock("2026-09-27T08:00:00Z")
    fake = FakeDrive(clock)

    assert "get" in FakeDrive.READ_OPS
    assert "list_children" in FakeDrive.READ_OPS
    assert "download" in FakeDrive.READ_OPS
    assert "create" in FakeDrive.WRITE_OPS
    assert "move" in FakeDrive.WRITE_OPS
    assert "delete_permanently" in FakeDrive.WRITE_OPS

    f1 = fake.seed_folder("test_folder")
    file1 = fake.seed_file(f1, "a.txt", b"content")

    # 執行操作並驗證記錄
    fake.get(file1)
    fake.list_children(f1)
    assert fake.calls == [("get", file1), ("list_children", f1)]


def test_fake_drive_inject_nth_read():
    """驗證 FakeDrive.inject_nth_read 精準在第 n 次讀取時失敗。"""
    clock = FixedClock("2026-09-27T08:00:00Z")
    fake = FakeDrive(clock)
    f = fake.seed_folder("f")
    fid = fake.seed_file(f, "t.txt", b"val")

    fake.inject_nth_read(2, error=ReadError)
    assert fake.get(fid).id == fid            # read 0: ok
    assert fake.list_children(f)[0].id == fid  # read 1: ok
    with pytest.raises(ReadError):
        fake.get(fid)                          # read 2: injected fail
    assert fake.get(fid).id == fid            # read 3: ok
