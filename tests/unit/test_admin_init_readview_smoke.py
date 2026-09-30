"""admin init-readview 的冒煙測試（只寫冒煙；全程 FakeDrive，不碰真實 Drive）。"""

import json
from pathlib import Path

import pytest

from aistorage.admin import AdminError
from aistorage.admin.init_readview import (
    MANIFEST_NAME,
    InitReadviewPlan,
    init_readview,
    plan_init_readview,
)
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.readview.model import parse_manifest

SA = "spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com"


def _rv() -> tuple[FakeDrive, str]:
    drive = FakeDrive()
    return drive, drive.seed_folder("readview")


def test_dry_run_does_not_write() -> None:
    drive, folder = _rv()
    plan = init_readview(drive, folder, sa_email=SA, confirm=False,
                         clock=FixedClock("2026-09-28T10:00:00Z"))
    assert isinstance(plan, InitReadviewPlan)
    assert plan.blocked is False
    assert plan.existing_manifest_id is None
    assert plan.manifest_name == MANIFEST_NAME
    assert plan.to_dict()["will_share_folder"] is True
    # 什麼都沒建立、也沒分享
    assert drive.list_children(folder) == []
    assert drive.shares_of(folder) == []


def test_confirm_creates_generation_zero_manifest() -> None:
    drive, folder = _rv()
    result = init_readview(drive, folder, sa_email=SA, confirm=True,
                           clock=FixedClock("2026-09-28T10:00:00Z"))
    assert result.generation == 0
    files = [c for c in drive.list_children(folder) if not c.is_folder]
    assert [c.name for c in files] == [MANIFEST_NAME]
    assert files[0].id == result.manifest_file_id

    manifest = parse_manifest(
        drive.download_bytes(result.manifest_file_id, max_bytes=1 << 20))
    assert manifest.generation == 0
    assert manifest.index is None
    assert manifest.agora_main_sha == "unborn"
    assert manifest.element == "agora"
    assert manifest.published_at == "2026-09-28T10:00:00.000000Z"

    # 分享的是「資料夾」（讀取端要讀 manifest、index、readings），角色 reader
    assert drive.shares_of(folder) == [(SA, "reader")]
    assert drive.shares_of(result.manifest_file_id) == []


def test_refuses_to_overwrite_existing_manifest() -> None:
    drive, folder = _rv()
    first = init_readview(drive, folder, confirm=True,
                          clock=FixedClock("2026-09-28T10:00:00Z"))
    before = drive.download_bytes(first.manifest_file_id, max_bytes=1 << 20)

    plan = plan_init_readview(drive, folder)
    assert plan.blocked is True
    assert plan.existing_manifest_id == first.manifest_file_id
    assert plan.existing_manifest_name == MANIFEST_NAME

    with pytest.raises(AdminError, match="拒絕覆寫"):
        init_readview(drive, folder, confirm=True,
                      clock=FixedClock("2026-09-28T11:00:00Z"))
    # 原檔沒有被動到
    assert drive.download_bytes(first.manifest_file_id, max_bytes=1 << 20) == before
    assert len([c for c in drive.list_children(folder) if not c.is_folder]) == 1


def test_detects_manifest_with_other_name() -> None:
    """1.4i 的經驗：Drive 允許同名檔，所以也要看內容能不能解析。"""
    drive, folder = _rv()
    other = drive.create(folder, "readview-manifest.json", b'{"format": "nope"}',
                         mime_type="application/json")
    plan = plan_init_readview(drive, folder)
    assert plan.existing_manifest_id is None      # 壞掉的不算
    good = init_readview(drive, folder, confirm=True,
                         clock=FixedClock("2026-09-28T10:00:00Z"))
    plan2 = plan_init_readview(drive, folder)
    assert plan2.existing_manifest_id == good.manifest_file_id
    assert plan2.existing_manifest_name == MANIFEST_NAME
    assert other.id != good.manifest_file_id


def test_without_sa_email_no_share() -> None:
    drive, folder = _rv()
    result = init_readview(drive, folder, confirm=True,
                           clock=FixedClock("2026-09-28T10:00:00Z"))
    assert result.shared_with is None
    assert result.permission_id is None
    assert drive.shares_of(folder) == []
    assert result.manifest_file_id


def test_bad_inputs() -> None:
    drive, folder = _rv()
    with pytest.raises(AdminError, match="讀不到讀取視圖資料夾"):
        plan_init_readview(drive, "folder_does_not_exist")
    with pytest.raises(AdminError, match="不是資料夾"):
        plan_init_readview(drive, drive.create(folder, "x.txt", b"x").id)
    with pytest.raises(AdminError, match="不是 email"):
        plan_init_readview(drive, folder, sa_email="not-an-email")


def test_cli_dry_run_and_missing_config(capsys) -> None:
    from aistorage.admin.__main__ import main

    # 設定檔不存在 → 明確報錯（rc 1），不會假裝成功
    rc = main(["init-readview", "--folder-id", "f1", "--config", "/nonexistent.json"])
    assert rc == 1
    assert "設定檔" in capsys.readouterr().err

    # 沒有管理憑證時是 not_wired（rc 2），不是靜靜跳過
    cfg = Path("/tmp/aistorage-admin-irv-test.json")
    cfg.write_text(json.dumps({
        "format": "aistorage.committer/v1",
        "repo": "agora", "repo_uuid": "u", "repo_url": "annex::u",
        "prefix_folder_id": "p", "quarantine_folder_id": "q",
        "identity_registry_path": str(cfg),
    }))
    rc = main(["init-readview", "--folder-id", "f1", "--config", str(cfg)])
    assert rc == 1                       # 缺管理憑證 → AdminError（rc 1）
    assert "rclone.conf" in capsys.readouterr().err
    cfg.unlink()
