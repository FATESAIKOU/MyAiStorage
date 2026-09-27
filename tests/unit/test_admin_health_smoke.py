"""6.3 健康檢查的冒煙測試（只寫冒煙；判定表全覆蓋，資料來源用假的）。"""

import plistlib
from datetime import datetime, timezone

from aistorage.admin.health import (
    HealthData,
    launchd_plist,
    quarantine_usage,
    run_health,
    summarize,
)
from aistorage.drive.fake import FakeDrive

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


def _by_name(checks, name: str):
    return next(c for c in checks if c.name == name)


def test_all_ok_and_summary() -> None:
    data = HealthData(
        tokens_ok={"committer": True, "worker": True},
        workflow_enabled=True,
        last_success_at="2026-09-27T09:00:00.000Z",
        schedule_interval_ok=True,
        actions_minutes_2w=100.0,
        quota_limit=1000, quota_usage=100,
        index_bytes=1000, readview_files=10,
        manifest_main_sha="a", pin_main_sha="a",
        prune_ok=True)
    checks = run_health(data, now=NOW)
    assert summarize(checks) == "ok"
    assert all(c.status == "ok" for c in checks)


def test_token_workflow_aborts_and_stale() -> None:
    data = HealthData(
        tokens_ok={"committer": False, "worker": True},
        workflow_enabled=False,
        consecutive_aborts=4,
        last_success_at="2026-09-25T00:00:00.000Z",
        cancelled_runs=2)
    checks = run_health(data, now=NOW)
    assert summarize(checks) == "fail"
    assert _by_name(checks, "token").status == "fail"
    assert "committer" in _by_name(checks, "token").value
    assert _by_name(checks, "workflow").status == "fail"
    assert _by_name(checks, "aborts").status == "fail"
    assert _by_name(checks, "last_success").status == "fail"
    assert _by_name(checks, "runs").status == "warn"


def test_warn_thresholds() -> None:
    data = HealthData(
        actions_minutes_2w=350.0,
        quota_limit=1000, quota_usage=900,
        index_bytes=60 * 1024 * 1024, readview_files=6000,
        manifest_main_sha="a", pin_main_sha="b",
        syncer_waiting=2, syncer_rejected=1,
        quarantine_growing=True, quarantine_files=3, quarantine_bytes=30,
        schedule_interval_ok=False, prune_ok=False,
        last_success_at="2026-09-26T20:00:00.000Z")
    checks = run_health(data, now=NOW)
    assert summarize(checks) == "warn"
    for name in ("actions_minutes", "quota", "readview_size", "readview_lag",
                 "syncer", "quarantine", "schedule", "prune", "last_success"):
        assert _by_name(checks, name).status == "warn", name
    assert "300" in _by_name(checks, "actions_minutes").hint


def test_quota_fail() -> None:
    data = HealthData(quota_limit=1000, quota_usage=990)
    assert _by_name(run_health(data, now=NOW), "quota").status == "fail"


def test_quarantine_usage_counts() -> None:
    drive = FakeDrive()
    q = drive.seed_folder("quarantine")
    day = drive.seed_folder("2026-09-27", parent=q)
    drive.seed_file(day, "a.txt", b"12345")
    drive.seed_file(day, "b.txt", b"123")
    assert quarantine_usage(drive, q) == (2, 8)


def test_launchd_plist_content_not_installed(tmp_path) -> None:
    """plist 內容正確（XML 可解析、6 小時），且測試不會安裝任何東西。"""
    text = launchd_plist(working_directory="/tmp/x")
    doc = plistlib.loads(text.encode("utf-8"))
    assert doc["Label"] == "local.aistorage.health"
    assert doc["StartInterval"] == 21600
    assert doc["ProgramArguments"][-1] == "health"
    assert doc["RunAtLoad"] is False
    home = tmp_path / "Library" / "LaunchAgents"
    assert not (home / "local.aistorage.health.plist").exists()
