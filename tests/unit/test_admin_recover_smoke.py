"""6.4 復原的冒煙測試（只寫冒煙；完整演練只在整合測試做）。"""

import pytest

from aistorage.admin import AdminError
from aistorage.admin.recover import (
    detect_remote_state,
    plan_recover,
    plan_recover_from_drive,
)
from aistorage.drive.fake import FakeDrive

UUID = "11111111-2222-3333-4444-555555555555"


def test_plan_modes() -> None:
    present = plan_recover(manifest_present=True, bak_present=True)
    assert present.mode == "from-clone"
    assert "不需要復原" in present.steps[0]

    bak = plan_recover(manifest_present=False, bak_present=True)
    assert bak.mode == "bak-only"
    assert any("BAK_RECOVERY" in s for s in bak.steps)

    with pytest.raises(AdminError, match="from-clone"):
        plan_recover(manifest_present=False, bak_present=False)

    clone = plan_recover(manifest_present=False, bak_present=False,
                         new_prefix="new-prefix-id")
    assert clone.mode == "from-clone"
    assert clone.detail["new_prefix"] == "new-prefix-id"
    assert any("init-pin" in s for s in clone.steps)
    assert any("readview_rebuild_epoch" in s for s in clone.steps)


def test_detect_and_plan_from_drive() -> None:
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    assert detect_remote_state(drive, prefix, UUID) == (False, False)
    plan = plan_recover_from_drive(drive, prefix, UUID,
                                   new_prefix="new-prefix-id")
    assert plan.mode == "from-clone"

    drive.seed_file(prefix, f"GITMANIFEST--{UUID}.bak", b"bak")
    assert detect_remote_state(drive, prefix, UUID) == (False, True)
    assert plan_recover_from_drive(drive, prefix, UUID).mode == "bak-only"

    drive.seed_file(prefix, f"GITMANIFEST--{UUID}", b"manifest")
    assert detect_remote_state(drive, prefix, UUID) == (True, True)
    assert plan_recover_from_drive(drive, prefix, UUID).mode == "from-clone"
