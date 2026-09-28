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


def test_unknown_is_warn_not_ok() -> None:
    """M7：查不到就是 warn（fail-open 修掉）。"""
    checks = run_health(HealthData(), now=NOW)
    by_name = {c.name: c.status for c in checks}
    for name in ("token", "workflow", "schedule", "actions_minutes", "quota",
                 "readview_size", "readview_lag", "last_success", "prune"):
        assert by_name[name] == "warn", name
    assert summarize(checks) == "warn"


def test_maintenance_flag_is_reported() -> None:
    checks = run_health(HealthData(workflow_enabled=False, maintenance=True), now=NOW)
    by_name = {c.name: c.status for c in checks}
    assert by_name["maintenance"] == "warn"
    # 維護中時 workflow 被停用是預期行為
    assert by_name["workflow"] == "ok"
    # 沒有旗標卻被停用 → fail
    checks2 = run_health(HealthData(workflow_enabled=False, maintenance=False),
                         now=NOW)
    by2 = {c.name: c.status for c in checks2}
    assert by2["workflow"] == "fail" and "maintenance" not in by2


def test_collect_health_from_fake_sources(tmp_path) -> None:
    """collect_health 真的去取資料；取不到的留 None。"""
    from aistorage.admin.health import CollectSources, collect_health
    from aistorage.clock import FixedClock
    from aistorage.drive.fake import FakeDrive
    from aistorage.integrity.pin import MemoryPinStore, PinState

    drive = FakeDrive()
    readview = drive.seed_folder("readview")
    drive.seed_file(readview, "index.sqlite", b"x" * 100)
    quarantine = drive.seed_folder("quarantine")
    day = drive.seed_folder("2026-09-27", parent=quarantine)
    drive.seed_file(day, "q1", b"qq")

    class _Pins(MemoryPinStore):
        def read_text(self, relpath: str) -> str | None:
            return super().read_text(relpath)

    pins = _Pins(initial_state=PinState(
        repo="agora", repo_uuid="u", refs={"refs/heads/main": "a" * 40},
        manifest_sha256="m" * 64, prev_manifest_sha256=None,
        active_bundles=(), removed_bundles=frozenset(), annex_keys=frozenset(),
        promoted_at="2026-09-27T09:00:00.000Z", run_id="1"))
    sources = CollectSources(
        drive=drive, pins=pins, repo="agora", prefix_folder_id=readview,
        readview_folder_id=readview, quarantine_folder_id=quarantine,
        quota_provider=lambda: {"storageQuota": {"limit": 1000, "usage": 100}},
        tokens={"committer": True})
    data = collect_health(sources, clock=FixedClock("2026-09-27T10:00:00Z"))
    assert data.last_success_at == "2026-09-27T09:00:00.000Z"
    assert data.quota_limit == 1000 and data.quota_usage == 100
    assert data.readview_files == 1 and data.index_bytes == 100
    assert (data.quarantine_files, data.quarantine_bytes) == (1, 2)
    assert data.maintenance is False
    # pin 讀不到 → None → warn
    broken = CollectSources(drive=drive, pins=MemoryPinStore(), repo="agora")
    data2 = collect_health(broken, clock=FixedClock("2026-09-27T10:00:00Z"))
    assert data2.last_success_at is None
    by_name = {c.name: c.status for c in run_health(data2, now=NOW)}
    assert by_name["last_success"] == "warn"
    assert by_name["quota"] == "warn"
