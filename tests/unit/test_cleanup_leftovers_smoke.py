"""清理腳本與 runner 殘留判定的單元測試。

全部是純邏輯（名字形狀、集合運算、摘要組裝），不碰真 Drive／真 pin repo。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for extra in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from scripts.cleanup_integration_leftovers import (  # noqa: E402
    group_prefix_folders,
    is_integration_pin_entry,
    is_integration_prefix,
    main,
    pin_entry_stem,
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


def test_pin_entry_stem_strips_all_four_suffixes() -> None:
    assert pin_entry_stem(f"it-{V}.json") == f"it-{V}"
    assert pin_entry_stem(f"it-{V}.keys") == f"it-{V}"
    assert pin_entry_stem(f"it-{V}.pending.json") == f"it-{V}"
    assert pin_entry_stem(f"it-{V}.pending.keys") == f"it-{V}"
    # 認不得的副檔名就原樣回傳（不要猜）
    assert pin_entry_stem(f"it-{V}.weird") == f"it-{V}.weird"


def test_pin_entry_matching_uses_the_stem() -> None:
    assert is_integration_pin_entry(f"it-{V}.json")
    assert is_integration_pin_entry(f"it-{V}.pending.keys")
    assert not is_integration_pin_entry(f"agora-e2e-{U}.json")
    assert not is_integration_pin_entry("agora.json")


def test_strip_pin_suffix_matches_the_cleanup_script() -> None:
    """兩個模組對「條目名」的收斂必須一致（runner 的殘留比對靠它）。"""
    for name in (f"it-{V}.json", f"it-{V}.pending.json", f"it-{V}.keys"):
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
        lambda settings, workdir: {
            "folders": [(f"it-{U}", f"it-{U}", "fid")],
            "pin_files": [f"it-{V}.json"],
            "skipped": ["e2e-x"],
            "groups": [(f"it-{U}", [f"it-{U}"])],
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
        lambda settings, workdir: {
            "folders": [(f"it-{U}", f"it-{U}", "fid")],
            "pin_files": [f"it-{V}.json"],
            "skipped": [],
            "groups": [(f"it-{U}", [f"it-{U}"])],
        },
    )
    monkeypatch.setattr(
        cleanup, "confirm_deletions",
        lambda settings, folders: (
            calls.append("drive") or {"removed": [f"it-{U}"], "skipped": []}),
    )
    monkeypatch.setattr(
        cleanup, "confirm_pin_deletions",
        lambda settings, names: (
            calls.append("pin") or {"removed": [f"it-{V}.json"], "error": None}),
    )

    assert cleanup.main(["--confirm"]) == 0
    assert calls == ["drive", "pin"]


def test_cleanup_cli_with_confirm_reports_pin_failure(tmp_path, monkeypatch) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    monkeypatch.setattr(
        cleanup, "plan",
        lambda settings, workdir: {
            "folders": [],
            "pin_files": [f"it-{V}.json"],
            "skipped": [],
            "groups": [],
        },
    )
    monkeypatch.setattr(
        cleanup, "confirm_deletions", lambda s, f: {"removed": [], "skipped": []},
    )
    monkeypatch.setattr(
        cleanup, "confirm_pin_deletions",
        lambda s, n: {"removed": [], "error": "Permission denied", "staged": ["x.json"]},
    )

    assert cleanup.main(["--confirm"]) == 1


def test_cleanup_cli_with_nothing_to_do(tmp_path, monkeypatch) -> None:
    import scripts.cleanup_integration_leftovers as cleanup

    monkeypatch.setattr(
        cleanup, "plan",
        lambda settings, workdir: {
            "folders": [], "pin_files": [], "skipped": ["e2e-x"], "groups": [],
        },
    )
    assert cleanup.main(["--confirm"]) == 0


def test_main_is_importable_without_side_effects() -> None:
    """匯入時不該建立任何 Drive 連線（import 本身要便宜）。"""
    assert callable(main)
