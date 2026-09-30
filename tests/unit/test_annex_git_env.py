"""`get_git_env()` 一定要把 `RCLONE_CONFIG` 釘死（2026-09-30 正式 run 36695731310）。

`git-remote-annex` 與 rclone special remote 是**子程序**：它們只認 `RCLONE_CONFIG`
或自己的預設探索路徑（`$XDG_CONFIG_HOME/rclone/rclone.conf`、
`~/.config/rclone/rclone.conf`），**不會**讀 `AISTORAGE_RCLONE_CONF`。

少了 `RCLONE_CONFIG` 時：
- 在 runner 上：HOME 底下沒有 rclone.conf → `gdrive` remote 找不到 → 整輪
  `ABORTED(annex.git.clone:ReadError)`。
- 在本機上：`~/.config/rclone/rclone.conf` 通常**存在**，所以測試會過——但它可能
  指向與 `AISTORAGE_RCLONE_CONF` 不同的 Drive 根。這個洞在本機是隱形的。

所以這裡釘兩件事：帶著設定時要把它交給子程序；沒帶時要**明確失敗或明確不給**，
絕不退回預設。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aistorage.annex.git import (
    _RCLONE_SENTINEL,
    SubprocessAnnexGit,
    get_git_env,
)
from aistorage.errors import ReadError

ENV_VAR = "AISTORAGE_RCLONE_CONF"


@pytest.fixture
def no_rclone_conf(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """把 AISTORAGE_RCLONE_CONF 與 RCLONE_CONFIG 從環境裡拿掉。"""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# 有設定時：子程序拿得到
# ---------------------------------------------------------------------------


def test_env_var_is_passed_as_rclone_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "/tmp/rclone-committer.conf")
    assert get_git_env()["RCLONE_CONFIG"] == "/tmp/rclone-committer.conf"


def test_explicit_argument_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "/tmp/from-env.conf")
    env = get_git_env("/tmp/explicit.conf")
    assert env["RCLONE_CONFIG"] == "/tmp/explicit.conf"


def test_path_object_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert get_git_env(Path("/tmp/x.conf"))["RCLONE_CONFIG"] == "/tmp/x.conf"


def test_rclone_config_is_not_overwritten_by_inherited_rclone_config(
    monkeypatch: pytest.MonkeyPatch) -> None:
    """繼承來的 RCLONE_CONFIG（別人設的）不能贏過 AISTORAGE_RCLONE_CONF。"""
    monkeypatch.setenv("RCLONE_CONFIG", "/somebody/elses/rclone.conf")
    monkeypatch.setenv(ENV_VAR, "/tmp/ours.conf")
    assert get_git_env()["RCLONE_CONFIG"] == "/tmp/ours.conf"


def test_existing_isolation_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """加 RCLONE_CONFIG 不得影響原本就有的三道隔離。"""
    monkeypatch.setenv(ENV_VAR, "/tmp/rclone.conf")
    env = get_git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


# ---------------------------------------------------------------------------
# 沒設定時：絕不退回預設 rclone.conf
# ---------------------------------------------------------------------------


def test_unset_never_falls_back_to_default_config(
        no_rclone_conf: pytest.MonkeyPatch) -> None:
    """沒設定時 RCLONE_CONFIG 必須指向一個**不存在**的路徑。

    不能只是「不設 RCLONE_CONFIG」——那樣 rclone 就會走自己的探索路徑、讀到
    `~/.config/rclone/rclone.conf`。那個檔案存在與否決定了這個洞是「紅燈」還是
    「靜靜地連到錯的 Drive 根」，必須兩種情況都擋掉。
    """
    env = get_git_env()
    assert env["RCLONE_CONFIG"] == _RCLONE_SENTINEL
    assert not Path(env["RCLONE_CONFIG"]).exists()


def test_unset_with_require_raises(no_rclone_conf: pytest.MonkeyPatch) -> None:
    with pytest.raises(ReadError) as exc:
        get_git_env(require_rclone_conf=True)
    msg = str(exc.value)
    assert ENV_VAR in msg, "錯誤訊息要指明要設哪個環境變數"
    assert "RCLONE_CONFIG" in msg, "要說明為什麼子程序只認 RCLONE_CONFIG"


def test_empty_env_var_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """空字串等於沒設定（`os.environ.get(...) or ""` 那一層）。"""
    monkeypatch.setenv(ENV_VAR, "")
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    assert get_git_env()["RCLONE_CONFIG"] == _RCLONE_SENTINEL
    with pytest.raises(ReadError):
        get_git_env(require_rclone_conf=True)


def test_require_is_satisfied_by_explicit_argument(
        no_rclone_conf: pytest.MonkeyPatch) -> None:
    env = get_git_env("/tmp/explicit.conf", require_rclone_conf=True)
    assert env["RCLONE_CONFIG"] == "/tmp/explicit.conf"


# ---------------------------------------------------------------------------
# 呼叫端：annex 路徑必須要求設定
# ---------------------------------------------------------------------------


def test_subprocess_annex_git_passes_the_config_to_subprocesses(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SubprocessAnnexGit 的子程序要拿得到 RCLONE_CONFIG。

    這是正式 run 36695731310 的形狀：dict 裡有設定，但子程序沒拿到 → annex clone
    找不到 `gdrive` → `ABORTED(annex.git.clone:ReadError)`。
    """
    monkeypatch.setenv(ENV_VAR, "/tmp/rclone.conf")
    (tmp_path / "annex").mkdir()
    inst = SubprocessAnnexGit(tmp_path / "annex", allow_unsafe_workdir=True)
    assert inst._get_env()["RCLONE_CONFIG"] == "/tmp/rclone.conf"


def test_local_annex_ops_do_not_require_rclone_conf(
        no_rclone_conf: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """純本機 annex 操作不得因為沒有 rclone 設定而壞掉。

    `_get_env()` 不設 `require_rclone_conf`：本機 repo 的 git-annex 指令不碰 rclone。
    （要碰遠端的那幾個入口——production run 的 clone——本來就會先被
    `build_production_deps` 的「找不到 rclone.conf」擋下，那裡的訊息更清楚。）
    但 RCLONE_CONFIG 仍然被釘成 sentinel，**不會** fallback。
    """
    (tmp_path / "annex").mkdir()
    inst = SubprocessAnnexGit(tmp_path / "annex")
    env = inst._get_env()
    assert env["RCLONE_CONFIG"] == _RCLONE_SENTINEL
    assert not Path(env["RCLONE_CONFIG"]).exists()


def test_require_is_available_for_callers_that_know_they_need_it(
        no_rclone_conf: pytest.MonkeyPatch) -> None:
    """`require_rclone_conf=True` 保留給「確定會 shell out 到 rclone」的呼叫端。

    目前 codebase 沒有任何呼叫端需要它（見上面那支的說明），但機制本身要留著，
    而且訊息必須指明要設哪個環境變數——這是使用者要求的一部分。
    """
    with pytest.raises(ReadError) as exc:
        get_git_env(require_rclone_conf=True)
    assert ENV_VAR in str(exc.value)
    assert "RCLONE_CONFIG" in str(exc.value)


def test_pin_repo_env_does_not_require_rclone_conf(
        no_rclone_conf: pytest.MonkeyPatch) -> None:
    """pin repo 走 GitHub SSH，本來就不需要 rclone——不得因為它壞掉。

    這是 `get_git_env()` 預設**不**報錯的原因：純 git 的呼叫端不該被 rclone 的
    設定綁架，但它同樣不會 fallback 到預設設定（見上一組測試）。
    """
    from aistorage.integrity import pin as pin_mod

    env = pin_mod.get_git_env()
    assert env["RCLONE_CONFIG"] == _RCLONE_SENTINEL


# ---------------------------------------------------------------------------
# 迴歸：真的子程序看得見 RCLONE_CONFIG
# ---------------------------------------------------------------------------


def test_real_subprocess_inherits_rclone_config(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """用真的子程序確認，不是只確認 dict 內容。

    抓的就是正式 run 36695731310 那個形狀：dict 裡有設定，但子程序沒拿到。
    """
    marker = "rclone-config-marker-42"
    conf = Path(os.environ.get("TMPDIR", "/tmp")) / f"{marker}.conf"
    conf.write_text("[gdrive]\ntype = drive\n")
    monkeypatch.setenv(ENV_VAR, str(conf))
    try:
        import subprocess

        proc = subprocess.run(
            ["python3", "-c", "import os,sys; sys.stdout.write(os.environ.get('RCLONE_CONFIG',''))"],
            env=get_git_env(), capture_output=True, text=True, check=True)
        assert proc.stdout == str(conf)
    finally:
        conf.unlink(missing_ok=True)
