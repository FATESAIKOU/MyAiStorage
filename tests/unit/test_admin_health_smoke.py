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
        held_files_known=True,
        manifest_conflict_known=True,
        quarantined_pinned_keys_known=True,
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


def _pin_with_pending(*annex_keys: str):
    """釘選值落後遠端、而且 pending 還在的 pin store（impl1 現場的形狀）。"""
    import hashlib

    from aistorage.integrity.pin import MemoryPinStore, PinPending, PinState

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    official = hashlib.sha256(b"official\n").hexdigest()
    return MemoryPinStore(
        initial_state=PinState(
            repo="agora", repo_uuid=uuid, refs={"refs/heads/main": "a" * 40},
            manifest_sha256=official, prev_manifest_sha256=None,
            active_bundles=(), removed_bundles=frozenset(),
            annex_keys=frozenset(), promoted_at="2026-09-27T09:00:00.000Z",
            run_id="1",
        ),
        initial_pending=PinPending(
            repo="agora", base_manifest_sha256=official,
            refs={"refs/heads/main": "b" * 40},
            annex_keys=frozenset(annex_keys),
            written_at="2026-09-27T09:30:00.000Z", run_id="2",
        ),
    )


def test_health_reports_unbacked_prefix_files_with_names() -> None:
    """review-1926cd3 L：前綴裡釘選值背書不了的檔案放超過一輪 → 報出並列檔名。

    這些檔案在真本前綴裡（HOLD＝有人背書、等釘選值轉正；待判斷＝還沒有人
    讀過內容），提交流程不會搬它們，所以只能靠健康檢查讓人看到。判定用的是
    提交流程同一個 `plan_sweep`，兩邊的規則不會各說各話。
    """
    import hashlib

    from aistorage.admin.health import CollectSources, collect_health
    from aistorage.clock import FixedClock

    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    drive = FakeDrive()
    prefix = drive.seed_folder("agora-prefix")
    # 釘選值落後遠端：前綴裡的主 manifest 沒有任何 KEEP 候選
    drive.seed_file(prefix, f"GITMANIFEST--{uuid}", b"ahead-of-the-pin\n")
    # pending 記著、所以有背書的新 annex 物件（HOLD）
    held_body = b"orphan"
    held_key = f"SHA256E-s{len(held_body)}--{hashlib.sha256(held_body).hexdigest()}"
    drive.seed_file(prefix, held_key, held_body)
    # 已經放很久的一份（超過 HELD_STALE_HOURS）
    old_sha = hashlib.sha256(b"ancient").hexdigest()
    old_body = b"ancient"
    old_key = f"SHA256E-s{len(old_body)}--{old_sha}"
    drive.seed_file(
        prefix, old_key, old_body, created_time="2026-09-01T00:00:00.000000Z",
    )
    data = collect_health(
        CollectSources(
            drive=drive, pins=_pin_with_pending(held_key, old_key),
            repo="agora", prefix_folder_id=prefix,
        ),
        clock=FixedClock("2026-09-27T10:00:00Z"),
    )
    # 檔名後面帶著 Drive 的建立時間（review-903d7e2 M1）：只給檔名時分不出哪
    # 一份才是真的（前綴裡可能有 rclone 自己留下的重複 manifest）。
    assert any(x.startswith(held_key + "@") for x in data.held_files)
    assert any(x.startswith(old_key + "@") for x in data.held_stale_files)
    assert any(x.startswith(f"GITMANIFEST--{uuid}@") for x in data.held_files)
    assert all("@" in x for x in data.held_files)

    checks = run_health(data, now=NOW)
    c = _by_name(checks, "held_files")
    assert c.status == "warn"
    assert any(old_key in v for v in c.value.split("、"))
    assert summarize(checks) == "warn"


def test_health_prefix_holding_nothing_is_ok() -> None:
    """前綴乾淨時這項是 ok（不是 warn）：否則每次健康檢查都在報假警。"""
    from aistorage.admin.health import CollectSources, collect_health
    from aistorage.clock import FixedClock

    drive = FakeDrive()
    prefix = drive.seed_folder("agora-prefix")
    drive.seed_file(prefix, "unrelated.txt", b"not in the canonical prefix")
    data = collect_health(
        CollectSources(
            drive=drive, pins=_pin_with_pending(), repo="agora",
            prefix_folder_id=prefix,
        ),
        clock=FixedClock("2026-09-27T10:00:00Z"),
    )
    assert data.held_files == [] and data.held_stale_files == []
    assert _by_name(run_health(data, now=NOW), "held_files").status == "ok"


def test_held_files_check_levels() -> None:
    # 查不到 → warn（M7：查不到 ≠ 正常）
    assert _by_name(run_health(HealthData(), now=NOW), "held_files").status == "warn"
    clean = run_health(HealthData(held_files_known=True), now=NOW)
    assert _by_name(clean, "held_files").status == "ok"
    fresh = run_health(
        HealthData(held_files_known=True, held_files=["GITMANIFEST--x"]), now=NOW
    )
    assert _by_name(fresh, "held_files").status == "ok"
    stale = run_health(
        HealthData(
            held_files_known=True, held_files=["GITMANIFEST--x"],
            held_stale_files=["GITMANIFEST--x"],
        ),
        now=NOW,
    )
    assert _by_name(stale, "held_files").status == "warn"
    assert "GITMANIFEST--x" in _by_name(stale, "held_files").value


def test_collect_health_from_fake_sources(tmp_path) -> None:
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


# ------------------------------------------- M2：持續注入同名 manifest 的警示


def test_manifest_conflict_is_warned_and_persistent_injection_fails() -> None:
    """M2（review-5d4dd52）：同名但內容不等於正式值的主 manifest 要看得見。

    這是 ADR 0008 殘餘風險 (1) 已接受的「暫停」：住民在每一輪的第 4 步之後放一份
    位元組不同的同名 manifest，`verify_clone`／`precheck` 就每一輪中止。`H1` 的
    `expected_manifest_sha256` **救不了這條**——clone 是用檔名找檔案的，
    git-remote-annex 與 rclone 都沒辦法用 file id 做 clone。所以系統只能暫停，
    而暫停必須看得見、而且要分得出「一次手滑」與「持續注入」。
    """
    import hashlib
    from aistorage.admin.health import CollectSources, collect_health
    from aistorage.clock import FixedClock
    from aistorage.integrity.pin import MemoryPinStore, PinState

    uuid = "u-uuid"
    manifest_name = f"GITMANIFEST--{uuid}"
    official = b"GITBUNDLE-a\n"
    pins = MemoryPinStore(initial_state=PinState(
        repo="agora", repo_uuid=uuid, refs={"refs/heads/main": "a" * 40},
        manifest_sha256=hashlib.sha256(official).hexdigest(),
        prev_manifest_sha256=None, active_bundles=(), removed_bundles=frozenset(),
        annex_keys=frozenset(), promoted_at="2026-09-27T09:00:00.000Z", run_id="1"))
    drive = FakeDrive()
    prefix = drive.seed_folder("agora-prefix")
    drive.seed_file(prefix, manifest_name, official, created_time="2026-09-27T08:00:00Z")
    variant = b"GITBUNDLE-a\n\n"          # 位元組不同，但一樣能解析
    assert hashlib.sha256(variant) != hashlib.sha256(official)
    drive.seed_file(
        prefix, manifest_name, variant, created_time="2026-09-27T09:30:00Z",
    )

    data = collect_health(
        CollectSources(drive=drive, pins=pins, repo="agora", prefix_folder_id=prefix),
        clock=FixedClock("2026-09-27T10:00:00Z"),
    )
    assert data.manifest_conflict_known is True
    assert len(data.manifest_conflicts) == 1
    assert data.manifest_conflict_stale == [], "30 分鐘還不到一輪，不算持續注入"
    fresh = _by_name(run_health(data, now=NOW), "manifest_conflict")
    assert fresh.status == "warn"
    assert "持續注入" in fresh.hint

    # 活過一整輪（> HELD_STALE_HOURS）→ 有人在一再放回來 → fail，而且要說怎麼處置
    aged = collect_health(
        CollectSources(drive=drive, pins=pins, repo="agora", prefix_folder_id=prefix),
        clock=FixedClock("2026-09-28T10:00:00Z"),
    )
    assert aged.manifest_conflict_stale == aged.manifest_conflicts
    stale = _by_name(run_health(aged, now=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)),
                     "manifest_conflict")
    assert stale.status == "fail"
    assert "撤銷" in stale.hint and "簽章金鑰" in stale.hint
    assert summarize(run_health(aged, now=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))) == "fail"


def test_manifest_conflict_is_ok_when_only_one_distinct_content() -> None:
    """健康前綴本來就可能有兩份同名同內容的 manifest（rclone 造成的）——那不是警示。"""
    import hashlib
    from aistorage.admin.health import manifest_conflicts_in_prefix
    from aistorage.integrity.pin import PinState

    uuid = "u-uuid"
    name = f"GITMANIFEST--{uuid}"
    official = b"GITBUNDLE-a\n"
    state = PinState(
        repo="agora", repo_uuid=uuid, refs={"refs/heads/main": "a" * 40},
        manifest_sha256=hashlib.sha256(official).hexdigest(),
        prev_manifest_sha256=None, active_bundles=(), removed_bundles=frozenset(),
        annex_keys=frozenset(), promoted_at="2026-09-27T09:00:00.000Z", run_id="1")
    drive = FakeDrive()
    prefix = drive.seed_folder("agora-prefix")
    drive.seed_file(prefix, name, official, created_time="2026-09-27T08:00:00Z")
    drive.seed_file(prefix, name, official, created_time="2026-09-27T08:00:30Z")
    conflicts, stale, known = manifest_conflicts_in_prefix(
        drive, prefix, state, now=NOW)
    assert (conflicts, stale, known) == ([], [], True)
    # 讀不到（沒有設定檔 id／pin）→ known=False，判定時是 warn 而不是「無」
    assert manifest_conflicts_in_prefix(drive, "", state, now=NOW) == ([], [], False)


def test_health_reports_pinned_keys_left_in_quarantine() -> None:
    """M1（review-final）b：隔離區裡還留著釘選值記載的 annex key → warn 並列 key。

    提交流程第 5 步會把這種 key **自動**搬回前綴（內容定址，搬回來的一定是對的位元
    組），所以正常情況下這一項應該是「無」。看得到就代表自癒還沒跑，或者同一個 key
    **反覆**被誤隔離（impl1 那一類）——而隔離區 7 天後會被 purge，拖不得。
    """
    import hashlib

    from aistorage.admin.health import CollectSources, collect_health
    from aistorage.clock import FixedClock
    from aistorage.integrity.pin import MemoryPinStore, PinState

    body = b"the canonical raw record"
    key = f"SHA256E-s{len(body)}--{hashlib.sha256(body).hexdigest()}"
    tampered = f"SHA256E-s{len(body)}--{hashlib.sha256(b'other bytes').hexdigest()}"

    drive = FakeDrive()
    prefix = drive.seed_folder("agora-prefix")
    quarantine = drive.seed_folder("quarantine")
    day = drive.seed_folder("2026-09-27", parent=quarantine)
    # 真的那份被隔離了（自癒的對象）
    drive.seed_file(day, key, body, created_time="2026-09-27T03:00:00Z")
    # 同名但位元組不同的一份：不是自癒的對象，不該被報出來
    drive.seed_file(day, tampered, b"other bytes", created_time="2026-09-27T03:00:00Z")

    pins = MemoryPinStore(initial_state=PinState(
        repo="agora", repo_uuid="01234567-89ab-cdef-0123-456789abcdef",
        refs={"refs/heads/main": "a" * 40},
        manifest_sha256=hashlib.sha256(b"official\n").hexdigest(),
        prev_manifest_sha256=None, active_bundles=(), removed_bundles=frozenset(),
        annex_keys=frozenset({key, tampered}),
        promoted_at="2026-09-27T09:00:00.000Z", run_id="1",
    ))
    data = collect_health(
        CollectSources(
            drive=drive, pins=pins, repo="agora", prefix_folder_id=prefix,
            quarantine_folder_id=quarantine,
        ),
        clock=FixedClock("2026-09-27T10:00:00Z"),
    )

    assert data.quarantined_pinned_keys_known is True
    assert [x.split("@")[0] for x in data.quarantined_pinned_keys] == [key], (
        "只認 sha256 與 size 都相符的那一份；位元組不同的同名檔不算")
    check = _by_name(run_health(data, now=NOW), "quarantined_pinned_keys")
    assert check.status == "warn"
    assert key in check.value
    assert "purge" in (check.hint or "")


def test_quarantined_pinned_keys_check_levels() -> None:
    """查不到 → warn（M7）；前綴裡有同一個 key → ok（已經搬回去了）。"""
    assert _by_name(run_health(HealthData(), now=NOW), "quarantined_pinned_keys").status \
        == "warn"
    clean = run_health(
        HealthData(quarantined_pinned_keys_known=True), now=NOW
    )
    assert _by_name(clean, "quarantined_pinned_keys").status == "ok"


def test_quarantined_pinned_keys_is_empty_once_the_key_is_back_in_the_prefix() -> None:
    """自癒搬回之後隔離區裡就沒有它了 → 這一項回到 ok（不是一直報 warn）。"""
    import hashlib

    from aistorage.admin.health import quarantined_pinned_keys_in
    from aistorage.integrity.pin import PinState

    body = b"the canonical raw record"
    key = f"SHA256E-s{len(body)}--{hashlib.sha256(body).hexdigest()}"
    state = PinState(
        repo="agora", repo_uuid="u", refs={}, manifest_sha256="m" * 64,
        prev_manifest_sha256=None, active_bundles=(), removed_bundles=frozenset(),
        annex_keys=frozenset({key}), promoted_at="2026-09-27T09:00:00.000Z", run_id="1",
    )
    drive = FakeDrive()
    prefix = drive.seed_folder("agora-prefix")
    quarantine = drive.seed_folder("quarantine")

    assert quarantined_pinned_keys_in(drive, quarantine, prefix, state) == ([], True)
    drive.seed_file(quarantine, key, body, created_time="2026-09-27T03:00:00Z")
    hits, known = quarantined_pinned_keys_in(drive, quarantine, prefix, state)
    assert known and len(hits) == 1
    # 搬回前綴（自癒做的那件事）之後就不再是問題
    drive.move(drive.find_by_name(quarantine, key)[0].id,
               from_parent=quarantine, to_parent=prefix)
    assert quarantined_pinned_keys_in(drive, quarantine, prefix, state) == ([], True)
