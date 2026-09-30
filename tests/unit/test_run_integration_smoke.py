"""scripts/run_integration.py 的單元測試：preflight 缺項訊息與參數解析。

不碰真 Drive／真 git-annex／真 pin repo：設定檔用 tmp_path 造假的，
外部程式的 `which` 與 `run` 都注入假的。秘密一律只驗「檔案在不在」，
這裡沒有任何秘密值。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_integration import (
    Settings,
    Summary,
    build_parser,
    check_credentials,
    check_tools,
    diff_names,
    format_issues,
    format_summary,
    parse_junit,
    preflight,
    pytest_argv,
    read_test_folder_id,
    resolve_settings,
)


def _settings(tmp_path: Path, **overrides: Path | str) -> Settings:
    base = Settings(
        ids_env=tmp_path / "ids.env",
        rclone_conf=tmp_path / "rclone-committer-test.conf",
        pin_key=tmp_path / "pin-test.key",
        known_hosts=tmp_path / "github_known_hosts",
        pin_repo_url="git@github.com:FATESAIKOU/MyAiStorage-pin-test.git",
    )
    return Settings(**{**base.__dict__, **overrides})


def _complete(tmp_path: Path) -> Settings:
    """四個憑證檔都在、ids.env 有 TEST_FOLDER_ID。"""
    settings = _settings(tmp_path)
    settings.ids_env.write_text("TEST_FOLDER_ID=folder-abc\n", encoding="utf-8")
    settings.rclone_conf.write_text("[gdrive]\n", encoding="utf-8")
    settings.pin_key.write_text("PRIVATE KEY\n", encoding="utf-8")
    settings.known_hosts.write_text("github.com ssh-ed25519 AAAA\n", encoding="utf-8")
    return settings


def _all_tools_present() -> tuple[object, object]:
    which = lambda name: f"/usr/local/bin/{name}"
    run = lambda argv: subprocess.CompletedProcess(argv, 0, "v1.2.3\n", "")
    return which, run


# ---------------------------------------------------------------------------
# 參數解析
# ---------------------------------------------------------------------------


def test_parser_defaults_to_integration_only() -> None:
    args = build_parser().parse_args([])
    assert args.only is None
    assert args.include_e2e is False
    assert args.skip_leftovers is False
    assert args.strict_leftovers is False


def test_parser_reads_every_option() -> None:
    args = build_parser().parse_args(
        ["--only", "three_rounds", "--include-e2e", "--skip-leftovers", "--strict-leftovers"]
    )
    assert args.only == "three_rounds"
    assert args.include_e2e is True
    assert args.skip_leftovers is True
    assert args.strict_leftovers is True


def test_parser_rejects_unknown_option() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--parallel"])


def test_only_allows_empty_string_to_pass_through() -> None:
    """`--only ""` 是允許的（pytest 的 -k 空字串等於不過濾），不該被偷偷改掉。"""
    assert build_parser().parse_args(["--only", ""]).only == ""


# ---------------------------------------------------------------------------
# preflight：缺什麼就講什麼
# ---------------------------------------------------------------------------


def test_preflight_ok_when_everything_present(tmp_path: Path) -> None:
    which, run = _all_tools_present()
    result = preflight(_complete(tmp_path), which=which, run=run)
    assert result.ok
    assert result.problems == ()
    assert result.test_folder_id == "folder-abc"
    assert len(result.tool_versions) == 4
    assert all("v1.2.3" in line for line in result.tool_versions)


def test_preflight_names_the_missing_credential_file(tmp_path: Path) -> None:
    settings = _complete(tmp_path)
    settings.rclone_conf.unlink()
    problems, folder_id = check_credentials(settings)
    assert folder_id == "folder-abc"
    assert len(problems) == 1
    message = problems[0]
    assert str(settings.rclone_conf) in message
    assert "rclone 設定檔" in message
    assert "找不到" in message


def test_preflight_reports_every_missing_file_at_once(tmp_path: Path) -> None:
    """四個都缺時要一次講四個，不要講一個跑一次。"""
    problems, folder_id = check_credentials(_settings(tmp_path))
    assert folder_id is None
    assert len(problems) == 4
    joined = "\n".join(problems)
    for what in ("ids.env", "rclone-committer-test.conf", "pin-test.key", "github_known_hosts"):
        assert what in joined


def test_preflight_says_ids_env_has_no_test_folder_id(tmp_path: Path) -> None:
    settings = _complete(tmp_path)
    settings.ids_env.write_text("# 空的\nOTHER=1\n", encoding="utf-8")
    problems, folder_id = check_credentials(settings)
    assert folder_id is None
    assert any("沒有 TEST_FOLDER_ID" in p for p in problems)


def test_test_folder_id_comes_from_env_over_ids_file(tmp_path: Path) -> None:
    settings = _complete(tmp_path)
    settings.ids_env.write_text("# 空的\n", encoding="utf-8")
    problems, folder_id = check_credentials(settings, {"TEST_FOLDER_ID": "from-env"})
    assert folder_id == "from-env"
    assert not [p for p in problems if "TEST_FOLDER_ID" in p]


def test_read_test_folder_id_tolerates_spaces(tmp_path: Path) -> None:
    path = tmp_path / "ids.env"
    path.write_text("  TEST_FOLDER_ID =  folder-xyz  \n", encoding="utf-8")
    assert read_test_folder_id(path) == "folder-xyz"


def test_read_test_folder_id_returns_none_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "ids.env"
    path.write_text("A=1\n", encoding="utf-8")
    assert read_test_folder_id(path) is None
    assert read_test_folder_id(tmp_path / "missing.env") is None


def test_preflight_names_the_missing_tool(tmp_path: Path) -> None:
    which = lambda name: None if name == "rclone" else f"/usr/local/bin/{name}"
    run = lambda argv: subprocess.CompletedProcess(argv, 0, "v1\n", "")
    problems, versions = check_tools(which=which, run=run)
    assert [p for p in problems if "rclone" in p and "brew install" in p]
    assert any("rclone" not in line for line in versions)


def test_preflight_reports_tool_that_fails_to_run(tmp_path: Path) -> None:
    which = lambda name: f"/usr/local/bin/{name}"
    run = lambda argv: subprocess.CompletedProcess(argv, 3, "", "boom")
    problems, _ = check_tools(which=which, run=run)
    assert len(problems) == 4
    assert all("rc=3" in p for p in problems)


def test_preflight_combines_credential_and_tool_problems(tmp_path: Path) -> None:
    which = lambda name: None
    run = lambda argv: subprocess.CompletedProcess(argv, 0, "v1\n", "")
    result = preflight(_settings(tmp_path), which=which, run=run)
    assert not result.ok
    assert len(result.problems) == 4 + len(
        [p for p in check_tools(which=which, run=run)[0]]
    )


def test_preflight_never_prints_file_contents(tmp_path: Path) -> None:
    """訊息裡只能出現路徑，不能出現檔案內容。"""
    secret = "SUPER-SECRET-TOKEN-VALUE"
    settings = _complete(tmp_path)
    settings.pin_key.write_text(secret, encoding="utf-8")
    settings.rclone_conf.write_text(f"token = {secret}\n", encoding="utf-8")
    problems, _ = check_credentials(settings)
    combined = " ".join(problems)
    assert secret not in combined


def test_format_issues_points_at_the_env_overrides() -> None:
    text = format_issues(["找不到 rclone 設定檔：/x/y.conf"])
    assert "/x/y.conf" in text
    assert "docs/testing.md" in text
    for var in (
        "AISTORAGE_TEST_IDS",
        "AISTORAGE_TEST_RCLONE_CONF",
        "AISTORAGE_TEST_PIN_KEY",
        "AISTORAGE_TEST_KNOWN_HOSTS",
        "AISTORAGE_TEST_PIN_REPO",
    ):
        assert var in text


# ---------------------------------------------------------------------------
# 設定解析
# ---------------------------------------------------------------------------


def test_resolve_settings_defaults_to_config_dir() -> None:
    settings = resolve_settings({})
    assert settings.ids_env == Path.home() / ".config" / "aistorage" / "ids.env"
    assert settings.rclone_conf == Path.home() / ".config" / "aistorage" / "rclone-committer-test.conf"
    assert settings.pin_key == Path.home() / ".config" / "aistorage" / "pin-test.key"
    assert settings.known_hosts == REPO_ROOT / "config" / "github_known_hosts"
    assert settings.pin_repo_url.endswith("MyAiStorage-pin-test.git")


def test_resolve_settings_honours_env_overrides(tmp_path: Path) -> None:
    settings = resolve_settings(
        {
            "AISTORAGE_TEST_IDS": str(tmp_path / "i.env"),
            "AISTORAGE_TEST_RCLONE_CONF": str(tmp_path / "r.conf"),
            "AISTORAGE_TEST_PIN_KEY": str(tmp_path / "k.key"),
            "AISTORAGE_TEST_KNOWN_HOSTS": str(tmp_path / "kh"),
            "AISTORAGE_TEST_PIN_REPO": "file:///tmp/pin.git",
        }
    )
    assert settings.ids_env == tmp_path / "i.env"
    assert settings.rclone_conf == tmp_path / "r.conf"
    assert settings.pin_key == tmp_path / "k.key"
    assert settings.known_hosts == tmp_path / "kh"
    assert settings.pin_repo_url == "file:///tmp/pin.git"


def test_resolve_settings_ignores_empty_env_values() -> None:
    settings = resolve_settings({"AISTORAGE_TEST_IDS": ""})
    assert settings.ids_env == Path.home() / ".config" / "aistorage" / "ids.env"


# ---------------------------------------------------------------------------
# 殘留比對
# ---------------------------------------------------------------------------


def test_diff_names_returns_only_new_entries() -> None:
    assert diff_names(["a", "b"], ["b", "c"]) == ["c"]


def test_diff_names_is_deduped_and_sorted() -> None:
    assert diff_names([], ["c", "a", "c", "b"]) == ["a", "b", "c"]


def test_diff_names_is_empty_when_nothing_new() -> None:
    assert diff_names(["a"], ["a"]) == []
    assert diff_names(["a"], []) == []


# ---------------------------------------------------------------------------
# 摘要與 exit code
# ---------------------------------------------------------------------------


def test_junit_argv_targets_the_right_marker_and_has_no_locals() -> None:
    """N8：不能開 -l／rich traceback（區域變數可能含秘密）。"""
    integration = pytest_argv("integration", only=None, junit=Path("/tmp/x.xml"))
    assert integration[1:3] == ["-m", "pytest"]
    assert "tests/integration" in integration
    # pytest 的 -m 標記在路徑之後（第一個 -m 是 python -m pytest）
    marker = integration.index("-m", 3)
    assert integration[marker + 1] == "integration"
    assert "--tb=short" in integration
    assert "--show-locals" not in integration
    assert "-l" not in integration

    e2e = pytest_argv("e2e", only="three_rounds", junit=Path("/tmp/y.xml"))
    assert "tests/e2e" in e2e
    assert e2e[e2e.index("-k") + 1] == "three_rounds"
    assert e2e[e2e.index("-m", 3) + 1] == "e2e"


def test_parse_junit_counts_failures_and_skips(tmp_path: Path) -> None:
    xml = tmp_path / "j.xml"
    xml.write_text(
        '<testsuites><testsuite tests="5" failures="1" errors="1" skipped="1"/></testsuites>',
        encoding="utf-8",
    )
    assert parse_junit(xml) == (2, 2, 1)


def test_parse_junit_tolerates_missing_or_broken_file(tmp_path: Path) -> None:
    assert parse_junit(tmp_path / "nope.xml") == (0, 0, 0)
    broken = tmp_path / "b.xml"
    broken.write_text("not xml", encoding="utf-8")
    assert parse_junit(broken) == (0, 0, 0)


def test_summary_exit_code_zero_when_all_passed() -> None:
    from scripts.run_integration import PhaseResult

    summary = Summary(phases=[PhaseResult("integration", 0, passed=12, duration_s=3.0)])
    assert summary.exit_code() == 0
    assert summary.exit_code(strict_leftovers=True) == 0


def test_summary_exit_code_one_when_a_phase_failed() -> None:
    from scripts.run_integration import PhaseResult

    summary = Summary(phases=[PhaseResult("integration", 1, passed=11, failed=1)])
    assert summary.exit_code() == 1


def test_summary_exit_code_strict_leftovers() -> None:
    from scripts.run_integration import PhaseResult

    summary = Summary(
        phases=[PhaseResult("integration", 0, passed=3)],
        leftovers={"drive": ["it-01ABC"], "pin": []},
        leftover_checked=True,
    )
    assert summary.exit_code() == 0
    assert summary.exit_code(strict_leftovers=True) == 1


def test_format_summary_reports_counts_and_leftovers() -> None:
    from scripts.run_integration import PhaseResult

    text = format_summary(
        Summary(
            phases=[PhaseResult("integration", 1, passed=4, failed=2, skipped=1, duration_s=61.0)],
            leftovers={"drive": ["it-01ABC"], "pin": []},
            leftover_checked=True,
            duration_s=62.5,
        ),
        strict_leftovers=False,
    )
    assert "過 4" in text and "失敗 2" in text and "略過 1" in text
    assert "it-01ABC" in text
    assert "總時間 62.5s" in text
    assert "結果：失敗" in text


def test_format_summary_says_no_leftovers_when_clean() -> None:
    from scripts.run_integration import PhaseResult

    text = format_summary(
        Summary(
            phases=[PhaseResult("integration", 0, passed=1)],
            leftovers={"drive": [], "pin": []},
            leftover_checked=True,
        ),
        strict_leftovers=False,
    )
    assert "殘留：無" in text
    assert "結果：全部通過" in text


def test_format_summary_notes_when_leftovers_were_not_checked() -> None:
    assert "沒檢查" in format_summary(Summary(), strict_leftovers=False)
