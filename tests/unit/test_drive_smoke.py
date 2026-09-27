"""Smoke tests for drive module (DriveFile, DriveClient, FakeDrive, RcloneConfToken, HttpDriveClient)."""

from pathlib import Path
import pytest

from aistorage.clock import FixedClock
from aistorage.drive import (
    DriveClient,
    DriveFile,
    FakeDrive,
    HttpDriveClient,
    RcloneConfToken,
)
from aistorage.errors import NotFound, ReadError, TooLarge


def test_drive_model_smoke():
    df = DriveFile(
        id="f1",
        name="test.txt",
        mime_type="text/plain",
        parents=("p1",),
        size=10,
        sha256="abc",
        md5="123",
        created_time="2026-09-27T08:00:00Z",
        modified_time="2026-09-27T08:00:00Z",
        trashed=False,
    )
    assert df.id == "f1"
    assert not df.is_folder

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
    file_id = drive.seed_file(folder_id, "hello.txt", b"hello world")

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
    assert new_file.size == 11
    updated = drive.update_content(new_file.id, b"updated content")
    assert updated.size == 15

    new_folder = drive.seed_folder("other_folder")
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


def test_http_drive_client_smoke():
    client = HttpDriveClient(lambda: "dummy_token")
    assert isinstance(client, DriveClient)
    assert client._get_token() == "dummy_token"
