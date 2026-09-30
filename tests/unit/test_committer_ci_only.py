"""提交流程只能由 Actions 跑——`docs/runbooks/deploy.md` 步驟 12 的那句話不能過期。

手冊上寫的是「本機只跑 `prescan`，不要跑 `committer run --dry-run`」。那是因為
`integrity/pin.py` 對正式 pin repo（`MyAiStorage-pin`）有硬性檢查：
`GITHUB_ACTIONS != "true"` 且沒有 `allow_production` 就丟 `PermissionError`。

關鍵在於**這道檢查在建 `GitPinStore` 的時候就觸發**，`--dry-run` 擋不住——
`--dry-run` 只擋後續的寫入動作，擋不住「準備寫入身分」。所以就算日後有人加了
`--dry-run` 的旁路，那句話也必須跟著改；這裡把它釘住。

（值全部是假的。這一檔不碰網路、不碰任何真實設定檔。）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aistorage.committer import __main__ as committer_main
from aistorage.committer.config import CommitterConfig
from aistorage.errors import ReadError
from aistorage.integrity.pin import GitPinStore

#: 正式 pin repo 的 URL 形式（`integrity/pin.py` 就是對這個結尾做比對）。
PRODUCTION_PIN_URL = "git@github.com:FATESAIKOU/MyAiStorage-pin.git"

#: 假的 rclone conf：只需要能被 `RcloneConfToken` 解析的三個欄位（token 要是 JSON）。
FAKE_RCLONE_CONF = """[gdrive]
type = drive
client_id = dummy-client-id
client_secret = dummy-client-secret
token = {"access_token": "dummy", "token_type": "Bearer", "refresh_token": "dummy"}
"""

@pytest.fixture
def not_ci(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    return monkeypatch


@pytest.fixture
def production_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                      ) -> tuple[CommitterConfig, Path]:
    """一份指向正式 pin repo 的設定檔（全部欄位都是假的）。回傳 (設定, 路徑)。"""
    conf = tmp_path / "rclone.conf"
    conf.write_text(FAKE_RCLONE_CONF, encoding="utf-8")
    registry = tmp_path / "identity.json"
    registry.write_text("{}", encoding="utf-8")
    key = tmp_path / "pin.key"
    key.write_text("dummy\n", encoding="utf-8")

    cfg_path = tmp_path / "committer.json"
    cfg_path.write_text(json.dumps({
        "format": "aistorage.committer/v1",
        "repo": "agora",
        "repo_uuid": "00000000-0000-0000-0000-0000000000ff",
        "repo_url": "annex::00000000-0000-0000-0000-0000000000ff",
        "prefix_folder_id": "1AAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "quarantine_folder_id": "1BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "identity_registry_path": str(registry),
        "pin_repo_url": PRODUCTION_PIN_URL,
    }), encoding="utf-8")

    monkeypatch.setenv("AISTORAGE_RCLONE_CONF", str(conf))
    monkeypatch.setenv("AISTORAGE_PIN_KEY", str(key))
    return CommitterConfig.load(cfg_path), cfg_path


# ---------------------------------------------------------------------------
# 正式 pin repo：非 CI 環境禁止寫入（--dry-run 也一樣）
# ---------------------------------------------------------------------------


def test_production_pin_store_rejected_outside_ci(
        not_ci: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """本機建正式 pin repo 的寫入身分就會被擋（在建構子裡，沒有任何網路動作）。"""
    with pytest.raises(PermissionError, match="正式 pin repo"):
        GitPinStore(PRODUCTION_PIN_URL, tmp_path / "work")


def test_run_subcommand_cannot_opt_into_production(
        not_ci: pytest.MonkeyPatch,
        production_config: tuple[CommitterConfig, Path]) -> None:
    """`committer run` 沒有 `allow_production` 逃生門。

    這是手冊那句話的根據：`run` 呼叫 `build_production_deps(cfg)` **不帶**
    `allow_production`，所以用正式設定檔在本機一定在建 pin store 時就失敗，
    跟 `--dry-run` 無關。
    """
    _cfg, cfg_path = production_config
    seen: dict[str, object] = {}

    def fake_build(cfg, **kwargs):
        seen.update(kwargs)
        return object()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(committer_main, "build_production_deps", fake_build)
    monkey.setattr(committer_main, "run",
                   lambda cfg, deps, dry_run=False: type("R", (), {"ok": True})())
    try:
        rc = committer_main.main(["run", "--config", str(cfg_path)])
    finally:
        monkey.undo()

    assert rc == 0
    assert not seen, f"committer run 不得傳 allow_production，實際傳了 {seen}"


def test_run_subcommand_has_no_i_am_admin_flag(
        production_config: tuple[CommitterConfig, Path]) -> None:
    """`run` 連參數都沒有——要在本機寫正式 pin repo 只能走 `init-pin --i-am-admin`。"""
    _cfg, cfg_path = production_config
    with pytest.raises(SystemExit):
        committer_main.main(["run", "--i-am-admin", "--config", str(cfg_path)])


def test_init_pin_does_have_the_escape_hatch(tmp_path: Path) -> None:
    """對照組：`init-pin` 真的有 `--i-am-admin`（部署步驟 4 靠它）。

    指標不存在所以會在讀設定檔時失敗——重點是 argparse **接受**了這個旗標
    （不是 SystemExit）。
    """
    with pytest.raises(ReadError):
        committer_main.main(["init-pin", "--confirm", "--i-am-admin",
                             "--config", str(tmp_path / "nope.json")])


# ---------------------------------------------------------------------------
# prescan：手冊說本機該用的那個，而且它不碰 pin repo
# ---------------------------------------------------------------------------


def test_prescan_works_outside_ci_and_never_builds_a_pin_store(
        not_ci: pytest.MonkeyPatch,
        production_config: tuple[CommitterConfig, Path]) -> None:
    """`prescan` 在非 CI 環境可以用，而且 `pins is None`——不碰 pin repo。

    這是手冊「本機只跑 prescan」那一句成立的另一半：它之所以能在本機跑，是因為
    它根本不需要寫入釘選值的身分。
    """
    cfg, _cfg_path = production_config

    def boom(*_a, **_k):
        raise AssertionError("prescan 不得建立 GitPinStore")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(committer_main, "GitPinStore", boom)
    monkey.setattr(committer_main, "load_registry", lambda *a, **k: object())
    try:
        deps = committer_main.build_prescan_deps(cfg)
    finally:
        monkey.undo()

    assert deps.pins is None
    assert deps.git_factory is None
