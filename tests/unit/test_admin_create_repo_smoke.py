"""admin create-repo 的單元測試（tasks 7.1）。

只測純邏輯（計畫、名稱驗證、冪等）與 CLI 接線；真的 git-annex push 由 7.1
的 Drive 實建驗證，不在單元測試裡跑。
"""

from __future__ import annotations

import pytest

from aistorage.admin.create_repo import (
    AGORA_LARGEFILES,
    FOUNDRY_LARGEFILES,
    check_prefix_name,
    largefiles_for,
    plan_create_repo,
    schema_version_for,
)
from aistorage.drive.fake import FakeDrive


def test_largefiles_rules_match_review_findings() -> None:
    """annex 收檔規則必須是實測結論（F-H2／group5-7 6.1）。"""
    assert largefiles_for("foundry") == FOUNDRY_LARGEFILES
    assert FOUNDRY_LARGEFILES == "include=objects/*/*"
    assert largefiles_for("agora") == AGORA_LARGEFILES
    with pytest.raises(ValueError):
        largefiles_for("mybrain")


def test_schema_versions() -> None:
    assert schema_version_for("foundry") == "foundry/v1"
    assert schema_version_for("agora") == "agora/v1"
    with pytest.raises(ValueError):
        schema_version_for("unknown")


def test_prefix_name_validation() -> None:
    assert check_prefix_name("foundry71-abc") == "foundry71-abc"
    for bad in ("", "AB", "a", "has space", "UPPER", "a/b", "x" * 65,
                "1a", "a-", "-a"):
        # "1a" 太短（<3）；其餘含非法字元或超長
        with pytest.raises(ValueError):
            check_prefix_name(bad)


def test_plan_is_read_only_and_detects_clash() -> None:
    drive = FakeDrive()
    root = drive.seed_folder("test-root")
    plan = plan_create_repo(drive, root, "foundry71-plan1", element="foundry")
    assert plan.already_exists is False
    assert plan.rcloneprefix == "foundry71-plan1"
    assert plan.quarantine_name == "foundry71-plan1-quarantine"
    assert plan.largefiles == FOUNDRY_LARGEFILES
    # dry-run 不寫入任何東西
    assert drive.list_children(root) == []

    # 建了之後再計畫就應該看到衝突（冪等：重跑同名即停）
    drive.seed_folder("foundry71-plan1", parent=root)
    plan2 = plan_create_repo(drive, root, "foundry71-plan1", element="foundry")
    assert plan2.already_exists is True


def test_plan_quarantine_clash_also_blocks() -> None:
    drive = FakeDrive()
    root = drive.seed_folder("test-root")
    drive.seed_folder("foundry71-plan2-quarantine", parent=root)
    plan = plan_create_repo(drive, root, "foundry71-plan2", element="foundry")
    assert plan.already_exists is True


def test_create_repo_cli_wired() -> None:
    from aistorage.admin.__main__ import build_parser

    args = build_parser().parse_args([
        "create-repo", "--element", "foundry",
        "--prefix-name", "foundry71-cli",
        "--test-root-id", "root123",
        "--rclone-conf", "/tmp/fake.conf",
        "--dry-run",
    ])
    assert args.command == "create-repo"
    assert args.element == "foundry"
    assert args.prefix_name == "foundry71-cli"
    assert args.dry_run is True
    assert args.confirm is False
