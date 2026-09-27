"""Smoke and security tests for drive module (DriveFile, DriveClient, FakeDrive, RcloneConfToken, HttpDriveClient)."""

from datetime import datetime, timezone
import io
from pathlib import Path
import traceback
import urllib.error
import pytest

from aistorage.clock import FixedClock
from aistorage.drive import (
    DriveClient,
    DriveFile,
    FakeDrive,
    HttpDriveClient,
    RcloneConfToken,
)
from aistorage.errors import NotFound, ReadError, TooLarge, WriteError


def test_drive_model_smoke():
    df = DriveFile(
        id="f1",
        name="test.txt",
        mime_type="text/plain",
        parents=("p1",),
        size=10,
        sha256="abc",
        md5="123",
        created_time="2026-09-27T08:00:00.500Z",
        modified_time="2026-09-27T08:00:00Z",
        trashed=False,
    )
    assert df.id == "f1"
    assert not df.is_folder
    # M3: created_at / modified_at properties
    assert df.created_at == datetime(2026, 9, 27, 8, 0, 0, 500000, tzinfo=timezone.utc)
    assert df.modified_at == datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)

    folder = DriveFile(
        id="f2",
        name="folder",
        mime_type="application/vnd.google-apps.folder",
        parents=(),
        size=None,
        sha256=None,
        md5=None,
        created_time="2026-09-27T08:00:00Z",
        modified_time="2026-09-27T08:00:00Z",
        trashed=False,
    )
    assert folder.is_folder


def test_fake_drive_smoke(tmp_path: Path):
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock)
    assert isinstance(drive, DriveClient)

    folder_id = drive.seed_folder("my_folder")
    assert folder_id == "folder_0001"
    file_id = drive.seed_file(folder_id, "hello.txt", b"hello world")
    assert file_id == "file_0001"

    # list_children & find_by_name
    children = drive.list_children(folder_id)
    assert len(children) == 1
    assert children[0].id == file_id

    found = drive.find_by_name(folder_id, "hello.txt")
    assert len(found) == 1
    assert found[0].id == file_id

    # get & download_bytes
    df = drive.get(file_id)
    assert df.size == 11
    data = drive.download_bytes(file_id, max_bytes=100)
    assert data == b"hello world"

    # TooLarge check
    with pytest.raises(TooLarge):
        drive.download_bytes(file_id, max_bytes=5)

    # download to file
    dest = tmp_path / "downloaded.txt"
    written = drive.download(file_id, dest, max_bytes=100)
    assert written == 11
    assert dest.read_bytes() == b"hello world"

    # create & update_content & move
    new_file = drive.create(folder_id, "new.txt", b"new content")
    assert new_file.id == "file_0002"
    assert new_file.size == 11
    updated = drive.update_content(new_file.id, b"updated content")
    assert updated.size == 15

    new_folder = drive.seed_folder("other_folder")
    assert new_folder == "folder_0002"
    moved = drive.move(new_file.id, from_parent=folder_id, to_parent=new_folder)
    assert new_folder in moved.parents
    assert folder_id not in moved.parents

    # snapshot & delete
    snap = drive.snapshot()
    assert "files" in snap and "contents" in snap
    drive.delete_permanently(new_file.id)
    with pytest.raises(NotFound):
        drive.get(new_file.id)

    # inject error
    drive.inject("get", file_id, error=ReadError, times=1)
    with pytest.raises(ReadError):
        drive.get(file_id)
    # Next get should succeed because times=1
    assert drive.get(file_id).id == file_id


def test_fake_drive_m6_features():
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock)

    f1 = drive.seed_folder("root_folder")
    file1 = drive.seed_file(f1, "f1.txt", b"data1")
    file2 = drive.seed_file(f1, "f2.txt", b"data2")

    # 1. 呼叫紀錄 (calls) 與 READ_OPS / WRITE_OPS
    drive.list_children(f1)
    drive.get(file1)
    drive.download_bytes(file1, max_bytes=100)
    assert len(drive.calls) == 3
    assert drive.calls[0] == ("list_children", f1)
    assert drive.calls[1] == ("get", file1)
    assert drive.calls[2] == ("download_bytes", file1)
    assert all(c[0] in FakeDrive.READ_OPS for c in drive.calls)
    assert not any(c[0] in FakeDrive.WRITE_OPS for c in drive.calls)

    # 2. inject_nth_read
    test_drive = FakeDrive(clock)
    tf1 = test_drive.seed_folder("f")
    tfile = test_drive.seed_file(tf1, "t.txt", b"abc")
    test_drive.inject_nth_read(1, ReadError)  # 第 2 次讀取（index 1）失敗
    test_drive.list_children(tf1)  # index 0: 成功
    with pytest.raises(ReadError):
        test_drive.get(tfile)      # index 1: 注入失敗
    test_drive.get(tfile)          # index 2: 成功

    # 3. set_checksum
    test_drive.set_checksum(tfile, None)
    assert test_drive.get(tfile).sha256 is None
    test_drive.set_checksum(tfile, "A" * 64)
    assert test_drive.get(tfile).sha256 == "a" * 64

    # 4. 嚴格檢查 (Strict checks)
    with pytest.raises(NotFound):
        test_drive.create("non_existent_folder", "f.txt", b"xyz")
    with pytest.raises(WriteError):
        test_drive.move("non_existent_file", from_parent=tf1, to_parent=tf1)
    with pytest.raises(WriteError):
        test_drive.move(tfile, from_parent="wrong_parent", to_parent=tf1)
    with pytest.raises(WriteError):
        test_drive.move(tfile, from_parent=tf1, to_parent="non_existent_folder")
    with pytest.raises(NotFound):
        test_drive.delete_permanently("non_existent_file")

    # 5. order 支援
    order_drive = FakeDrive(clock, order="reverse")
    of = order_drive.seed_folder("of")
    of1 = order_drive.seed_file(of, "1.txt", b"1")
    of2 = order_drive.seed_file(of, "2.txt", b"2")
    children = order_drive.list_children(of)
    assert [c.id for c in children] == [of2, of1]


def test_rclone_conf_token_smoke(tmp_path: Path):
    conf_file = tmp_path / "rclone.conf"
    conf_file.write_text(
        '[gdrive]\ntype = drive\nclient_id = test_cid\nclient_secret = test_sec\n'
        'token = {"access_token":"mock_access","expiry":"2099-01-01T00:00:00Z","refresh_token":"mock_ref"}\n'
    )
    token_prov = RcloneConfToken(conf_file, "gdrive")
    assert "test_cid" not in repr(token_prov)
    assert "mock_access" not in repr(token_prov)
    assert "mock_ref" not in repr(token_prov)
    assert repr(token_prov) == "<RcloneConfToken remote=gdrive>"

    # Access token cached
    assert token_prov.access_token() == "mock_access"


def test_h1_secret_never_leaked_in_exceptions(tmp_path: Path):
    """H1 驗收測試：斷言設定檔錯誤訊息、repr、traceback 絕不洩漏測試秘密字串。"""
    canary_pct = "CANARY_SECRET_PERCENT_xyz987"
    canary_syntax = "CANARY_SECRET_SYNTAX_abc123"
    canary_json = "CANARY_SECRET_JSON_badjson456"

    # 1. % 插值安全：含 % 之秘密值不觸發 InterpolationSyntaxError，且 repr 絕不洩漏秘密
    conf_pct_valid = tmp_path / "conf_pct_valid.conf"
    conf_pct_valid.write_text(
        f'[gdrive]\ntype = drive\nclient_id = cid\nclient_secret = bad%{canary_pct}%\n'
        f'token = {{"access_token":"a","refresh_token":"r"}}\n'
    )
    token_prov = RcloneConfToken(conf_pct_valid, "gdrive")
    assert canary_pct not in repr(token_prov)

    # 2. 含 % 之語法錯誤：行內包含 % 與秘密字串時，拋出之 ReadError 絕不含出錯行原文或秘密
    conf_pct_err = tmp_path / "conf_pct_err.conf"
    conf_pct_err.write_text(
        f'[gdrive]\ntype = drive\nclient_id = cid\nclient_secret = sec\n'
        f'broken_line_with_%_and_{canary_pct}\n'
    )
    with pytest.raises(ReadError) as exc_info1:
        RcloneConfToken(conf_pct_err, "gdrive")
    e1 = exc_info1.value
    assert canary_pct not in str(e1)
    assert canary_pct not in repr(e1)
    tb1 = "".join(traceback.format_exception(type(e1), e1, e1.__traceback__))
    assert canary_pct not in tb1
    assert e1.__cause__ is None

    # 3. 一般語法錯誤（例如 secrets-to-files 截斷或換行錯位，行內包含秘密）
    conf2 = tmp_path / "conf_syntax.conf"
    conf2.write_text(
        f'[gdrive]\ntype = drive\nclient_id = cid\nclient_secret = sec\n'
        f'broken_line_without_value_{canary_syntax}\n'
    )
    with pytest.raises(ReadError) as exc_info2:
        RcloneConfToken(conf2, "gdrive")
    e2 = exc_info2.value
    assert canary_syntax not in str(e2)
    assert canary_syntax not in repr(e2)
    tb2 = "".join(traceback.format_exception(type(e2), e2, e2.__traceback__))
    assert canary_syntax not in tb2
    assert e2.__cause__ is None

    # 4. Token JSON 格式損毀（JSON 內容含秘密）
    conf3 = tmp_path / "conf_json.conf"
    conf3.write_text(
        f'[gdrive]\ntype = drive\nclient_id = cid\nclient_secret = sec\n'
        f'token = {{"refresh_token": "{canary_json}", broken\n'
    )
    with pytest.raises(ReadError) as exc_info3:
        RcloneConfToken(conf3, "gdrive")
    e3 = exc_info3.value
    assert canary_json not in str(e3)
    assert canary_json not in repr(e3)
    tb3 = "".join(traceback.format_exception(type(e3), e3, e3.__traceback__))
    assert canary_json not in tb3
    assert e3.__cause__ is None


def test_http_drive_client_smoke():
    client = HttpDriveClient(lambda: "dummy_token")
    assert isinstance(client, DriveClient)
    assert client._get_token() == "dummy_token"


def test_http_drive_client_m1_create_fails_without_5xx_retry(monkeypatch: pytest.MonkeyPatch):
    """M1: create 非冪等，遇 5xx 絕不重試，立即 raise WriteError。"""
    call_count = 0

    def mock_urlopen(req, timeout=60):
        nonlocal call_count
        call_count += 1
        fp = io.BytesIO(b'{"error": {"code": 503, "message": "Backend Error"}}')
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {}, fp
        )

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    client = HttpDriveClient(lambda: "dummy_token", max_retries=3)
    with pytest.raises(WriteError):
        client.create("parent1", "file.txt", b"hello")
    # 僅呼叫 1 次，無重試
    assert call_count == 1


def test_http_drive_client_m1_delete_404_treated_as_success(monkeypatch: pytest.MonkeyPatch):
    """M1/M2: delete_permanently 遇 404 視為已刪除成功，不拋出例外。"""
    def mock_urlopen(req, timeout=60):
        fp = io.BytesIO(b'{"error": {"code": 404, "message": "File not found"}}')
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, fp)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    client = HttpDriveClient(lambda: "dummy_token")
    # 不應拋出 NotFound 或 WriteError
    client.delete_permanently("already_deleted_file_id")


def test_http_drive_client_m2_401_invalidates_token_and_retries(monkeypatch: pytest.MonkeyPatch):
    """M2: 遇到 401 時呼叫 token_provider.invalidate() 並重試一次。"""
    class MockTokenProvider:
        def __init__(self):
            self.invalidated = False
            self.count = 0

        def access_token(self):
            self.count += 1
            return f"token_{self.count}"

        def invalidate(self):
            self.invalidated = True

    token_prov = MockTokenProvider()
    attempts = 0

    class MockResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}
        def read(self):
            return b'{"id": "f1", "name": "f1.txt"}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=60):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            assert req.headers["Authorization"] == "Bearer token_1"
            fp = io.BytesIO(b'{"error": {"code": 401, "message": "Invalid Credentials"}}')
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, fp)
        # 第 2 次應該帶有刷新後的 token_2
        assert req.headers["Authorization"] == "Bearer token_2"
        return MockResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    client = HttpDriveClient(token_prov)
    df = client.get("f1")
    assert df.id == "f1"
    assert token_prov.invalidated is True
    assert attempts == 2


def test_http_drive_client_l_query_escape():
    """L: Drive 查詢字串跳脫測試（先跳脫 \\ 再跳脫 '）。"""
    from aistorage.drive.http import _escape_q
    assert _escape_q(r"folder\name'test") == r"folder\\name\'test"

