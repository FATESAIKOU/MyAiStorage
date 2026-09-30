"""清理腳本與 runner 殘留判定的單元測試。

全部是純邏輯（名字形狀、年齡、集合運算、摘要組裝），不碰真 Drive／真 pin repo。

年齡那一段是 review-55edd374 M2：`it-<ULID>` 的 ULID 前 10 碼就是建立時間，
所以只刪超過門檻的——另一個終端**正在跑**的整合測試用的是同一種前綴。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for extra in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from scripts.cleanup_integration_leftovers import (  # noqa: E402
    DEFAULT_MIN_AGE_HOURS,
    age_hours,
    group_prefix_folders,
    integration_ulid,
    is_integration_pin_entry,
    is_integration_prefix,
    is_old_enough,
    main,
    pin_entry_stem,
    strip_known_suffix,
)
from scripts.run_integration import (  # noqa: E402
    Summary,
    format_summary,
    partition_leftovers,
    set_baseline,
    strip_pin_suffix,
)

#: 真的 26 碼 ULID（Crockford base32：不含 I L O U），由 `generate_ulid()` 產生。
U = "01M3R64MXG7MWXYAT25EA4BC3A"
V = "01M3R64MXGT4H20RA95WB7KTPA"


def _created(ulid: str) -> datetime:
    """ULID 前 10 碼解出來的建立時間（測試裡的「現在」都從這裡推）。"""
    from aistorage.intake.ledger import parse_ulid_timestamp_ms

    ms = parse_ulid_timestamp_ms(ulid)
    assert ms is not None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _hours_ago(hours: float) -> str:
    """一個「hours 小時前建立」的 ULID。"""
    from aistorage.schema import generate_ulid

    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    return generate_ulid(int(when.timestamp() * 1000))


# ---------------------------------------------------------------------------
# Drive 前綴的名字形狀
# ---------------------------------------------------------------------------


def test_integration_prefix_accepts_only_it_ulid() -> None:
    assert is_integration_prefix(f"it-{U}")
    assert is_integration_prefix(f"it-{U}".lower())
    assert is_integration_prefix(f"it-erase-{U}")
    assert is_integration_prefix(f"it-{U}-inbox")
    assert is_integration_prefix(f"it-{U}-quarantine")
    assert is_integration_prefix(f"it-{U}-readview")


def test_integration_prefix_rejects_everything_else() -> None:
    # e2e 是單例環境，別人的線正在用 —— 絕對不能納入清理範圍
    assert not is_integration_prefix("e2e-01M3M1YGWMYS6C83J6NQ5TJ64J")
    assert not is_integration_prefix("syncer-1-inbox")
    assert not is_integration_prefix("probe-sandbox")
    # it- 開頭但不是 26 碼 ULID 的形狀
    assert not is_integration_prefix("it-short")
    assert not is_integration_prefix(f"it-{U}-extra-thing")
    assert not is_integration_prefix(f"it-{U}Z")
    assert not is_integration_prefix(U)
    assert not is_integration_prefix("")


def test_protected_prefixes_win_even_with_a_ulid_shape() -> None:
    """有人做成 `e2e-<ULID>` 時也一樣不碰。"""
    assert not is_integration_prefix(f"e2e-{U}")


def test_pin_entry_stem_strips_every_known_suffix() -> None:
    assert pin_entry_stem(f"it-{V}.json") == f"it-{V}"
    assert pin_entry_stem(f"it-{V}.keys") == f"it-{V}"
    assert pin_entry_stem(f"it-{V}.pending.json") == f"it-{V}"
    assert pin_entry_stem(f"it-{V}.pending.keys") == f"it-{V}"
    # 第 6 組抹除測試用 AdminLock 留下的維護旗標也要認得
    assert pin_entry_stem(f"it-{V}.maintenance") == f"it-{V}"
    # 認不得的副檔名就原樣回傳（不要猜）
    assert pin_entry_stem(f"it-{V}.weird") == f"it-{V}.weird"


def test_pin_entry_matching_uses_the_stem() -> None:
    assert is_integration_pin_entry(f"it-{V}.json")
    assert is_integration_pin_entry(f"it-{V}.pending.keys")
    assert not is_integration_pin_entry(f"agora-e2e-{U}.json")
    assert not is_integration_pin_entry("agora.json")


def test_maintenance_flag_of_an_integration_entry_is_cleaned() -> None:
    """`it-<ULID>.maintenance` 是抹除測試留下的旗標，算在清理範圍內。"""
    assert is_integration_pin_entry(f"it-{V}.maintenance")
    assert is_integration_pin_entry(f"it-erase-{V}.maintenance")


def test_maintenance_flag_of_e2e_is_never_touched() -> None:
    """e2e 的維護旗標絕對不能刪：e2e 環境是單例，別人的線正在用。"""
    assert not is_integration_pin_entry(f"agora-e2e-{U}.maintenance")
    assert not is_integration_pin_entry(f"e2e-{U}.maintenance")
    assert not is_integration_pin_entry("maintenance")


def test_maintenance_flag_with_a_foreign_repo_name_is_left_alone() -> None:
    """repo 名不在 `it-<ULID>`／`it-erase-<ULID>` 白名單裡就不動。"""
    assert not is_integration_pin_entry(f"agora-{U}.maintenance")
    assert not is_integration_pin_entry("it-probe1234abcd.maintenance")
    assert not is_integration_pin_entry("syncer-1.maintenance")


def test_strip_pin_suffix_matches_the_cleanup_script() -> None:
    """兩個模組對「條目名」的收斂必須一致（runner 的殘留比對靠它）。"""
    for name in (f"it-{V}.json", f"it-{V}.pending.json", f"it-{V}.keys",
                 f"it-{V}.maintenance"):
        assert strip_pin_suffix(name) == pin_entry_stem(name)


def test_group_prefix_folders_puts_siblings_together() -> None:
    groups = group_prefix_folders([
        f"it-{U}-quarantine", f"it-{U}", f"it-{U}-inbox",
        f"it-erase-{V}", "e2e-something", "syncer-1-inbox",
    ])
    # 排序依前綴名：`it-0…` 排在 `it-e…`（"0" < "e"）前面
    assert groups == [
        (f"it-{U}", [f"it-{U}", f"it-{U}-inbox", f"it-{U}-quarantine"]),
        (f"it-erase-{V}", [f"it-erase-{V}"]),
    ]


def test_group_prefix_folders_ignores_foreign_names() -> None:
    assert group_prefix_folders(["e2e-x", "syncer-1-inbox", "random"]) == []


# ---------------------------------------------------------------------------
# M2：年齡（review-55edd374 M2）——另一個終端正在跑的整合測試不能被刪
# ---------------------------------------------------------------------------


def test_strip_known_suffix_only_strips_the_three_known_ones() -> None:
    assert strip_known_suffix(f"it-{U}") == f"it-{U}"
    assert strip_known_suffix(f"it-{U}-inbox") == f"it-{U}"
    assert strip_known_suffix(f"it-erase-{U}-quarantine") == f"it-erase-{U}"
    assert strip_known_suffix(f"it-{U}-readview") == f"it-{U}"
    # 認不得的後綴原樣留著（形狀檢查會把它擋掉）
    assert strip_known_suffix(f"it-{U}-whatever") == f"it-{U}-whatever"


def test_integration_ulid_is_the_same_for_every_folder_of_one_run() -> None:
    """同一輪的 `it-<ULID>`／`-inbox`／`-quarantine` 必須算成同一個建立時間。"""
    assert integration_ulid(f"it-{U}") == U
    assert integration_ulid(f"it-{U}-inbox") == U
    assert integration_ulid(f"it-erase-{U}-quarantine") == U
    assert integration_ulid(f"e2e-{U}") is None
    assert integration_ulid("random") is None


def test_age_hours_comes_from_the_first_ten_ulid_chars() -> None:
    """年齡就是 ULID 前 10 碼的建立時間差，不是檔案時間也不是名稱順序。"""
    now = _created(U) + timedelta(hours=9)
    assert age_hours(f"it-{U}", now) == 9.0
    assert age_hours(f"it-{U}-inbox", now) == 9.0
    # 括號內的容差：浮點除出來會有最後一位的誤差
    assert abs(age_hours(f"it-erase-{V}", now) - 9.0) < 1e-6
    # 不是整合測試的名稱 → 沒有年齡可算
    assert age_hours("e2e-x", now) is None


def test_only_leftovers_older_than_the_threshold_are_old_enough() -> None:
    now = datetime.now(timezone.utc)
    old = _hours_ago(DEFAULT_MIN_AGE_HOURS + 1)
    young = _hours_ago(0.5)
    assert is_old_enough(f"it-{old}", now, DEFAULT_MIN_AGE_HOURS)
    assert not is_old_enough(f"it-{young}", now, DEFAULT_MIN_AGE_HOURS)


def test_the_age_threshold_is_inclusive_at_exactly_n_hours() -> None:
    """剛好滿 6 小時就算老夠（門檻是「至少」，不是「大於」）。"""
    exact = _hours_ago(DEFAULT_MIN_AGE_HOURS)
    boundary = _created(exact) + timedelta(hours=DEFAULT_MIN_AGE_HOURS)
    assert is_old_enough(f"it-{exact}", boundary, DEFAULT_MIN_AGE_HOURS)
    # 差 1 毫秒就還太新
    assert not is_old_enough(
        f"it-{exact}", boundary - timedelta(milliseconds=1), DEFAULT_MIN_AGE_HOURS)


def test_a_name_whose_age_cannot_be_read_is_never_old_enough() -> None:
    """看不出建立時間就當成太新：刪資料的腳本寧可少刪也不要刪錯。"""
    now = datetime.now(timezone.utc)
    assert not is_old_enough("it-not-a-ulid", now, 0.0)
    assert not is_old_enough("e2e-whatever", now, 0.0)


def test_plan_keeps_a_running_rounds_sandbox_out_of_the_delete_list(
    tmp_path, monkeypatch
) -> None:
    """另一個終端**正在跑**的整合測試（太新）不會進「會刪」，但要標出來。"""
    import scripts.cleanup_integration_leftovers as cleanup

    old = _hours_ago(30)
    young = _hours_ago(0.2)
    monkeypatch.setattr(
        cleanup, "drive_candidates",
        lambda settings: [
            (f"it-{old}", f"it-{old}", "fid-old"),
            (f"it-{old}-inbox", f"it-{old}-inbox", "fid-old-inbox"),
            (f"it-{young}", f"it-{young}", "fid-young"),
        ],
    )
    monkeypatch.setattr(
        cleanup, "pin_candidates",
        lambda settings, workdir: [f"it-{old}.json", f"it-{young}.keys"],
    )
    monkeypatch.setattr(cleanup, "_all_drive_names", lambda settings: ["e2e-x"])

    result = cleanup.plan(_FakeSettings(), tmp_path)

    assert [f[1] for f in result["folders"]] == [f"it-{old}", f"it-{old}-inbox"]
    assert result["pin_files"] == [f"it-{old}.json"]
    assert [name for _base, name, _h in result["young_folders"]] == [f"it-{young}"]
    assert [n for n, _h in result["young_pin_files"]] == [f"it-{young}.keys"]
    assert result["skipped"] == ["e2e-x"]
    # 分組只針對「會刪」的那一堆
    assert result["groups"] == [(f"it-{old}", [f"it-{old}", f"it-{old}-inbox"])]


def test_dry_run_marks_the_too_young_ones_with_their_age(tmp_path, monkeypatch,
                                                        capsys) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    young = _hours_ago(0.25)
    monkeypatch.setattr(
        cleanup, "plan",
        lambda *a, **k: {
            "folders": [], "pin_files": [],
            "young_folders": [(f"it-{young}", f"it-{young}", 0.25)],
            "young_pin_files": [(f"it-{young}.keys", 0.25)],
            "skipped": ["e2e-x"], "groups": [],
            "min_age_hours": DEFAULT_MIN_AGE_HOURS,
        },
    )
    monkeypatch.setattr(cleanup, "resolve_settings", lambda: _FakeSettings())

    assert cleanup.main([]) == 0
    out = capsys.readouterr().out
    assert "太新" in out and f"it-{young}" in out
    assert "分鐘前建立" in out


def test_confirm_refuses_to_delete_a_too_young_folder(tmp_path, monkeypatch) -> None:
    """就算呼叫端把太新的清單傳進來，刪之前也會自己再算一次年齡。"""
    import scripts.cleanup_integration_leftovers as cleanup

    young = _hours_ago(0.1)
    destroyed: list[str] = []

    class _Drive:
        def get(self, fid):
            raise AssertionError("太新的不該走到 get()")

    # 直接測真正的那個函式：把 Drive 換成假的
    # 清理腳本用 `from run_integration import …`（頂層模組名），所以要 patch
    # 同一個模組物件，不是 `scripts.run_integration`
    import run_integration as run_integration_mod

    import aistorage.drive.auth as auth_mod
    import aistorage.drive.http as http_mod

    monkeypatch.setattr(
        run_integration_mod, "read_test_folder_id", lambda env: "root")
    monkeypatch.setattr(auth_mod, "RcloneConfToken", lambda *a, **k: object())
    monkeypatch.setattr(http_mod, "HttpDriveClient", lambda *a, **k: _Drive())
    harness = type(sys)("tests.integration._harness")
    harness.destroy_tree = lambda drive, fid, root: destroyed.append(fid)
    monkeypatch.setitem(sys.modules, "tests.integration._harness", harness)

    result = cleanup.confirm_deletions(
        _FakeSettings(), [(f"it-{young}", f"it-{young}", "fid-young")])

    assert result["removed"] == []
    assert result["skipped"] and "太新" in result["skipped"][0]
    assert destroyed == []


def test_pin_push_failure_says_nothing_was_deleted(tmp_path, monkeypatch, capsys
                                                   ) -> None:
    """push 被拒時遠端一個條目都沒少，訊息要照這個事實講（不能說「已 stage」）。"""
    import scripts.cleanup_integration_leftovers as cleanup

    old = _hours_ago(30)
    monkeypatch.setattr(cleanup, "resolve_settings", lambda: _FakeSettings())
    monkeypatch.setattr(
        cleanup, "plan",
        lambda *a, **k: {
            "folders": [], "pin_files": [f"it-{old}.json"],
            "young_folders": [], "young_pin_files": [], "skipped": [],
            "groups": [], "min_age_hours": DEFAULT_MIN_AGE_HOURS,
        },
    )
    monkeypatch.setattr(
        cleanup, "confirm_deletions", lambda *a, **k: {"removed": [], "skipped": []})
    monkeypatch.setattr(
        cleanup, "confirm_pin_deletions",
        lambda *a, **k: {"removed": [], "error": "Permission denied",
                         "staged": [f"it-{old}.json"]},
    )

    assert cleanup.main(["--confirm"]) == 1
    out = capsys.readouterr().out
    assert "沒有任何東西被刪掉" in out
    assert "已 stage" not in out
    assert "Permission denied" in out


def test_negative_min_age_is_refused(monkeypatch) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    monkeypatch.setattr(cleanup, "resolve_settings", lambda: _FakeSettings())
    assert cleanup.main(["--min-age-hours", "-1"]) == 2


class _FakeSettings:
    """`plan`／`format_plan`／`confirm_*` 只會讀這幾個欄位。"""

    ids_env = "/tmp/ids.env"
    pin_repo_url = "git@example:MyAiStorage-pin-test.git"
    rclone_conf = "/tmp/rclone.conf"
    pin_key = "/tmp/pin_key"
    known_hosts = "/tmp/known_hosts"


# ---------------------------------------------------------------------------
# runner：這一輪 vs 更早
# ---------------------------------------------------------------------------


def test_partition_leftovers_separates_this_round_from_earlier() -> None:
    fresh, earlier = partition_leftovers(
        [f"it-{U}", f"it-{V}"], baseline=[f"it-{U}", "e2e-old"],
    )
    assert fresh == [f"it-{V}"]
    assert earlier == ["e2e-old", f"it-{U}"]


def test_partition_leftovers_with_empty_baseline_is_all_fresh() -> None:
    fresh, earlier = partition_leftovers([f"it-{U}"], baseline=[])
    assert fresh == [f"it-{U}"]
    assert earlier == []


def test_set_baseline_feeds_partition_leftovers() -> None:
    set_baseline({"drive": ["old-1"], "pin": []})
    fresh, earlier = partition_leftovers(["old-1", "new-1"], ["old-1"])
    assert fresh == ["new-1"]
    assert earlier == ["old-1"]
    set_baseline({"drive": [], "pin": []})  # 复位，避免影響其他測試


def test_strict_leftovers_only_fails_on_this_rounds_leftovers() -> None:
    """跑前就有的十幾組前綴不該讓每一場都紅。"""
    from scripts.run_integration import PhaseResult

    phases = [PhaseResult("integration", 0, passed=2)]
    only_earlier = Summary(
        phases=phases,
        leftovers={"drive": ["old"], "pin": []},
        fresh={"drive": [], "pin": []},
        earlier={"drive": ["old"], "pin": []},
        leftover_checked=True,
    )
    assert only_earlier.exit_code(strict_leftovers=True) == 0

    fresh_here = Summary(
        phases=phases,
        leftovers={"drive": ["new"], "pin": []},
        fresh={"drive": ["new"], "pin": []},
        earlier={"drive": ["old"], "pin": []},
        leftover_checked=True,
    )
    assert fresh_here.exit_code(strict_leftovers=True) == 1


def test_format_summary_labels_this_round_separately_from_earlier() -> None:
    from scripts.run_integration import PhaseResult

    text = format_summary(
        Summary(
            phases=[PhaseResult("integration", 0, passed=2, duration_s=10.0)],
            leftovers={"drive": ["old", "new"], "pin": []},
            fresh={"drive": ["new"], "pin": []},
            earlier={"drive": ["old"], "pin": []},
            leftover_checked=True,
            duration_s=11.0,
        ),
        strict_leftovers=False,
    )
    assert "這一輪的殘留" in text
    assert "舊" in text and "cleanup_integration_leftovers.py" in text
    # 舊的那筆只出現在「跑前就存在」那一段，不該被算成這一輪的
    fresh_section = text.split("這一輪的殘留")[1].split("跑前就存在")[0]
    assert "new" in fresh_section
    assert "old" not in fresh_section


def test_format_summary_says_no_fresh_leftovers() -> None:
    from scripts.run_integration import PhaseResult

    text = format_summary(
        Summary(
            phases=[PhaseResult("integration", 0, passed=1)],
            fresh={"drive": [], "pin": []},
            earlier={"drive": ["old"], "pin": []},
            leftover_checked=True,
        ),
        strict_leftovers=False,
    )
    assert "這一輪的殘留：無" in text


# ---------------------------------------------------------------------------
# CLI：預設絕不刪
# ---------------------------------------------------------------------------


def test_cleanup_cli_defaults_to_dry_run(tmp_path, capsys, monkeypatch) -> None:
    """沒有 --confirm 時不得呼叫任何刪除函式。"""
    import scripts.cleanup_integration_leftovers as cleanup

    called: list[str] = []
    monkeypatch.setattr(
        cleanup, "plan",
        lambda *a, **k: {
            "folders": [(f"it-{U}", f"it-{U}", "fid")],
            "pin_files": [f"it-{V}.json"],
            "young_folders": [],
            "young_pin_files": [],
            "skipped": ["e2e-x"],
            "groups": [(f"it-{U}", [f"it-{U}"])],
            "min_age_hours": DEFAULT_MIN_AGE_HOURS,
        },
    )
    monkeypatch.setattr(
        cleanup, "confirm_deletions",
        lambda *a, **k: called.append("drive") or {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        cleanup, "confirm_pin_deletions",
        lambda *a, **k: called.append("pin") or {"removed": [], "error": None},
    )

    assert cleanup.main([]) == 0
    assert called == [], "dry-run 竟然刪了東西"
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert f"it-{U}" in out
    assert "e2e-x" in out          # 不碰的也列出來，讓人知道邊界
    assert "--confirm" in out


def test_cleanup_cli_with_confirm_deletes(tmp_path, monkeypatch) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    calls: list[str] = []
    monkeypatch.setattr(
        cleanup, "plan",
        lambda *a, **k: {
            "folders": [(f"it-{U}", f"it-{U}", "fid")],
            "pin_files": [f"it-{V}.json"],
            "young_folders": [],
            "young_pin_files": [],
            "skipped": [],
            "groups": [(f"it-{U}", [f"it-{U}"])],
            "min_age_hours": DEFAULT_MIN_AGE_HOURS,
        },
    )
    monkeypatch.setattr(
        cleanup, "confirm_deletions",
        lambda *a, **k: (
            calls.append("drive") or {"removed": [f"it-{U}"], "skipped": []}),
    )
    monkeypatch.setattr(
        cleanup, "confirm_pin_deletions",
        lambda *a, **k: (
            calls.append("pin") or {"removed": [f"it-{V}.json"], "error": None}),
    )

    assert cleanup.main(["--confirm"]) == 0
    assert calls == ["drive", "pin"]


def test_cleanup_cli_with_confirm_reports_pin_failure(tmp_path, monkeypatch) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    monkeypatch.setattr(
        cleanup, "plan",
        lambda *a, **k: {
            "folders": [],
            "pin_files": [f"it-{V}.json"],
            "young_folders": [],
            "young_pin_files": [],
            "skipped": [],
            "groups": [],
            "min_age_hours": DEFAULT_MIN_AGE_HOURS,
        },
    )
    monkeypatch.setattr(
        cleanup, "confirm_deletions", lambda *a, **k: {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        cleanup, "confirm_pin_deletions",
        lambda *a, **k: ({"removed": [], "error": "Permission denied",
                          "staged": ["x.json"]}),
    )

    assert cleanup.main(["--confirm"]) == 1


def test_cleanup_cli_with_nothing_to_do(tmp_path, monkeypatch) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    monkeypatch.setattr(
        cleanup, "plan",
        lambda *a, **k: {
            "folders": [], "pin_files": [], "skipped": ["e2e-x"], "groups": [],
            "young_folders": [], "young_pin_files": [],
            "min_age_hours": DEFAULT_MIN_AGE_HOURS,
        },
    )
    assert cleanup.main(["--confirm"]) == 0


def test_main_is_importable_without_side_effects() -> None:
    """匯入時不該建立任何 Drive 連線（import 本身要便宜）。"""
    assert callable(main)
