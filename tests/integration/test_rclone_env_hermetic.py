"""整合測試跑的時候，annex/git 子程序**不會**用到 `~/.config/rclone/rclone.conf`。

正式 run 36695731310 的死因：`get_git_env()` 沒有把 `RCLONE_CONFIG` 釘死，
`git-remote-annex` 於是去找它自己的預設設定路徑；runner 上沒有那份 →
`gdrive` remote 找不到 → 整輪 `ABORTED(annex.git.clone:ReadError)`。

同一個洞在本機是**隱形**的：`~/.config/rclone/rclone.conf` 通常存在，所以測試會
通過——但它可能指向與 `AISTORAGE_RCLONE_CONF` 不同的 Drive 根。

這一檔把那個隱形條件變成會喊的：整合測試跑起來的時候，annex 子程序拿到的
`RCLONE_CONFIG` 必須是**測試那份**，而且絕對不是使用者的預設設定檔。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aistorage.annex.git import _RCLONE_SENTINEL, get_git_env

#: rclone 在沒有 RCLONE_CONFIG 時會去讀的預設路徑（取最常見的兩個）。
RCLONE_DEFAULT_PATHS = (
    Path.home() / ".config" / "rclone" / "rclone.conf",
    Path.home() / ".rclone.conf",
)


@pytest.mark.integration
def test_harness_exports_the_test_rclone_conf() -> None:
    """conftest 的 session fixture 必須把測試那份匯出成 AISTORAGE_RCLONE_CONF。

    少了它，annex 子程序就沒有設定可用——這正是整合測試過去可能默默讀到
    使用者預設設定的原因。
    """
    from tests.integration.conftest import require_settings

    conf = Path(str(require_settings()["rclone_conf"]))
    assert os.environ.get("AISTORAGE_RCLONE_CONF") == str(conf), (
        "整合測試的 session fixture 沒有把 AISTORAGE_RCLONE_CONF 設成測試那份；"
        f"目前是 {os.environ.get('AISTORAGE_RCLONE_CONF')!r}，應為 {conf!r}")


@pytest.mark.integration
def test_subprocess_env_uses_the_test_conf() -> None:
    """annex 子程序拿到的 RCLONE_CONFIG == 測試那份，不是使用者的預設設定。"""
    from tests.integration.conftest import require_settings

    expected = str(require_settings()["rclone_conf"])
    got = get_git_env()["RCLONE_CONFIG"]
    assert got == expected, f"annex 子程序會讀 {got!r}，應為 {expected!r}"
    assert got != _RCLONE_SENTINEL


@pytest.mark.integration
def test_subprocess_env_never_points_at_the_default_rclone_conf() -> None:
    """無論有沒有設定，都不得指向 rclone 的預設探索路徑。

    兩種情況都要擋：測試那份恰好等於預設路徑（不該發生，但擋掉），以及
    設定缺失時的 sentinel（不存在，所以 rclone 一定讀不到）。
    """
    got = Path(get_git_env()["RCLONE_CONFIG"])
    for default in RCLONE_DEFAULT_PATHS:
        assert got != default, (
            f"annex 子程序指向 rclone 的預設設定 {default}——"
            "那可能連到別的 Drive 根")


@pytest.mark.integration
def test_default_rclone_conf_is_not_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 AISTORAGE_RCLONE_CONF 拿掉之後也不會去用預設設定。

    這是「整合測試不依賴 ~/.config/rclone/rclone.conf」的直接證明：
    拿掉唯一正確的設定來源之後，RCLONE_CONFIG 變成一個**不存在**的 sentinel，
    所以任何 code path 都不可能安靜地讀到使用者的預設設定。
    """
    monkeypatch.delenv("AISTORAGE_RCLONE_CONF", raising=False)
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    got = get_git_env()["RCLONE_CONFIG"]
    assert got == _RCLONE_SENTINEL
    assert not Path(got).exists()


@pytest.mark.integration
def test_ambient_rclone_config_cannot_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """外面設的 RCLONE_CONFIG 不能蓋掉測試那份。"""
    from tests.integration.conftest import require_settings

    monkeypatch.setenv("RCLONE_CONFIG", "/somebody/elses/rclone.conf")
    expected = str(require_settings()["rclone_conf"])
    assert get_git_env()["RCLONE_CONFIG"] == expected


@pytest.mark.integration
def test_rclone_can_actually_read_the_remote_with_this_env() -> None:
    """實測：帶著這個 env 跑 rclone，讀得到測試的 `gdrive` remote。

    這是整組測試的真正目的——不只是變數對了，而是 git-remote-annex 實際上
    拿得到它要的設定。用 `rclone config redacted` 讀**名稱**就好，不印任何值。
    """
    import shutil
    import subprocess

    if shutil.which("rclone") is None:
        pytest.fail("整合測試必須 FAIL 不是 skip：這台機器沒有 rclone")
    proc = subprocess.run(["rclone", "listremotes"], env=get_git_env(),
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"rclone listremotes 失敗: {proc.stderr.strip()}"
    remotes = {line.rstrip(":") for line in proc.stdout.splitlines() if line.strip()}
    assert "gdrive" in remotes, f"測試的 rclone 設定裡沒有 gdrive：{sorted(remotes)}"
