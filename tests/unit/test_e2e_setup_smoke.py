"""第 9 組端到端設定腳本單元與冒煙測試 (tests/unit/test_e2e_setup_smoke.py)。

驗證 scripts/e2e_setup.py 的輔助函式、簽章金鑰生成、身分登錄檔合規性、
設定檔載入、父層防護斷言與 CLI 參數解析。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aistorage.committer.config import CommitterConfig
from aistorage.identity import load_registry
from aistorage.reader.config import ReaderConfig
from aistorage.syncer.config import SyncerConfig
from scripts.e2e_setup import (
    E2EEnvLocked,
    acquire_e2e_lock,
    destroy_tree,
    e2e_lock_holder,
    e2e_lock_path,
    main,
    read_sa_email,
    read_test_folder_id,
    release_e2e_lock,
    run_teardown,
    setup_signing_keys,
)


def test_read_test_folder_id(tmp_path: Path):
    env_file = tmp_path / "ids.env"
    env_file.write_text("TEST_FOLDER_ID=test_folder_12345\n", encoding="utf-8")
    assert read_test_folder_id(env_file) == "test_folder_12345"

    env_file.write_text("OTHER_ID=xyz\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="找不到 TEST_FOLDER_ID"):
        read_test_folder_id(env_file)


def test_read_sa_email(tmp_path: Path):
    sa_file = tmp_path / "sa-reader.json"
    sa_file.write_text(json.dumps({"client_email": "reader@example.com"}), encoding="utf-8")
    assert read_sa_email(sa_file) == "reader@example.com"

    sa_file.write_text(json.dumps({"other": "xyz"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="缺少 client_email"):
        read_sa_email(sa_file)


def test_setup_signing_keys_generates_valid_registry(tmp_path: Path):
    keys_dir = tmp_path / "test-keys"
    identity_path = tmp_path / "config" / "identity.e2e.json"
    inbox_id = "inbox_folder_123"

    key_id, pub_b64 = setup_signing_keys(
        keys_dir=keys_dir,
        inbox_folder_id=inbox_id,
        identity_path=identity_path,
        profile="mac-opencode",
    )

    assert key_id.startswith("mac-opencode-")
    assert len(key_id.split("-")[-1]) == 8
    assert pub_b64

    # 驗證私鑰檔案權限 0600
    key_file = keys_dir / "signing.key"
    assert key_file.is_file()
    assert key_file.stat().st_size == 32
    assert (key_file.stat().st_mode & 0o777) == 0o600

    # 驗證登錄檔可被 load_registry 成功載入（allow_example=False）
    reg = load_registry(identity_path, allow_example=False)
    active_keys = reg.active_public_keys("mac-opencode")
    assert key_id in active_keys


def test_committer_e2e_json_compatibility(tmp_path: Path):
    """驗證產出之 committer.e2e.json 結構能被 CommitterConfig.load 成功解析。"""
    cfg_file = tmp_path / "committer.e2e.json"
    identity_file = tmp_path / "identity.e2e.json"
    identity_file.write_text("{}", encoding="utf-8")

    payload = {
        "format": "aistorage.committer/v1",
        "repo": "agora",
        "repo_uuid": "00000000-0000-0000-0000-000000000001",
        "repo_url": "annex::uuid?rcloneremotename=gdrive",
        "prefix_folder_id": "agora_prefix_id",
        "quarantine_folder_id": "quarantine_id",
        "readview_folder_id": "readview_id",
        "readview_manifest_file_id": "manifest_id",
        "readview_rebuild_epoch": 0,
        "pin_repo_url": "git@github.com:FATESAIKOU/MyAiStorage-pin-test.git",
        "identity_registry_path": str(identity_file),
        "max_git_bundles": 10,
        "max_gc_per_run": 200,
        "max_raw_size": 52428800,
        "quarantine_retention_days": 7,
        "ledger_retention_months": 3,
        "prefix_levels": [],
        "github_repository": "FATESAIKOU/MyAiStorage-pin-test",
    }
    cfg_file.write_text(json.dumps(payload), encoding="utf-8")

    cfg = CommitterConfig.load(cfg_file)
    assert cfg.repo == "agora"
    assert cfg.repo_uuid == "00000000-0000-0000-0000-000000000001"
    assert cfg.prefix_folder_id == "agora_prefix_id"
    assert cfg.readview_manifest_file_id == "manifest_id"
    assert cfg.max_git_bundles == 10


def test_reader_e2e_json_compatibility(tmp_path: Path, monkeypatch):
    """驗證產出之 reader.e2e.json 結構能被 ReaderConfig 與 SyncerConfig 解析。"""
    reader_file = tmp_path / "reader.e2e.json"
    sa_file = tmp_path / "sa.json"
    sa_file.write_text("{}", encoding="utf-8")

    payload = {
        "format": "aistorage.reader/v1",
        "manifest_file_id": "manifest_file_123",
        "readview_folder_id": "readview_folder_123",
        "inbox_folder_ids": {
            "mac-opencode": "inbox_folder_123",
        },
        "sa_key_path": str(sa_file),
    }
    reader_file.write_text(json.dumps(payload), encoding="utf-8")

    # ReaderConfig
    rcfg = ReaderConfig.load(reader_file)
    assert rcfg.manifest_file_id == "manifest_file_123"
    assert rcfg.sa_key_path == sa_file

    # SyncerConfig
    monkeypatch.setenv("AISTORAGE_READER_CONFIG", str(reader_file))
    monkeypatch.delenv("AISTORAGE_INBOX_FOLDER_ID", raising=False)
    scfg = SyncerConfig.load()
    assert scfg.inbox_folder_id == "inbox_folder_123"


def test_destroy_tree_guard():
    """destroy_tree 必須驗證 allowed_parent，否則拒絕刪除。"""
    from unittest.mock import MagicMock
    from aistorage.drive.model import DriveFile

    mock_drive = MagicMock()
    mock_drive.get.return_value = DriveFile(
        id="folder_1",
        name="test",
        mime_type="application/vnd.google-apps.folder",
        parents=("wrong_parent",),
        size=0,
        sha256=None,
        md5=None,
        created_time="2026-09-28T00:00:00Z",
        modified_time="2026-09-28T00:00:00Z",
        trashed=False,
    )

    with pytest.raises(RuntimeError, match="不含允許之父層 expected_parent"):
        destroy_tree(mock_drive, "folder_1", allowed_parent="expected_parent")


class _SweepDrive:
    """只支援 sweep 需要的最小介面。"""

    def __init__(self, tree: dict) -> None:
        # {folder_id: [(name, id, is_folder), ...]}
        self.tree = tree
        self.deleted: list[str] = []

    def list_children(self, folder_id: str):
        from types import SimpleNamespace

        return [
            SimpleNamespace(id=i, name=n, is_folder=f, parents=(folder_id,))
            for n, i, f in self.tree.get(folder_id, [])
        ]

    def get(self, file_id: str):
        from types import SimpleNamespace

        for parent, kids in self.tree.items():
            for n, i, f in kids:
                if i == file_id:
                    return SimpleNamespace(id=i, name=n, is_folder=f, parents=(parent,))
        raise KeyError(file_id)

    def list_children_of(self, folder_id: str):
        return self.list_children(folder_id)

    def delete_permanently(self, file_id: str) -> None:
        self.deleted.append(file_id)
        for parent, kids in list(self.tree.items()):
            self.tree[parent] = [k for k in kids if k[1] != file_id]


def test_sweep_orphan_prefixes_only_deletes_e2e_folders(monkeypatch):
    """只清 `e2e-*` 前綴，而且只刪「不是留下來那一個」的。"""
    import scripts.e2e_setup as e2e

    drive = _SweepDrive({
        "root": [
            ("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", "f1", True),
            ("e2e-01BX5ZZKBKACTAV9WEVGEMMVRZ", "f2", True),
            ("e2e-01BX5ZZKBKACTAV9WEVGEMMVRZ-inbox", "f3", True),   # 不是前綴（名字不同開頭也要小心）
            ("it-01CCC", "f4", True),          # 別人的整合測試前綴
            ("syncer-1-inbox", "f5", True),    # 5.2 驗收留下的
        ],
        "f1": [("inbox", "f1a", True)],
        "f2": [],
    })
    monkeypatch.setattr(e2e, "destroy_tree",
                        lambda d, fid, allowed_parent, **kw: 1)
    report = e2e.sweep_orphan_prefixes(
        drive=drive, test_root_id="root", keep_prefix_id="f2",
    )
    # e2e-01BX5ZZKBKACTAV9WEVGEMMVRZ 被保留；e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV 被清掉
    # （e2e-01BX5ZZKBKACTAV9WEVGEMMVRZ-inbox 不符合前綴命名 → 不掃；it-*、syncer-* 也不掃）
    assert report["kept"] == ["e2e-01BX5ZZKBKACTAV9WEVGEMMVRZ"]
    assert len(report["removed"]) == 1 and report["removed"][0].startswith("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV")


def test_sweep_dry_run_does_not_delete(monkeypatch):
    import scripts.e2e_setup as e2e

    drive = _SweepDrive({"root": [("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", "f1", True)], "f1": []})
    calls: list[str] = []
    monkeypatch.setattr(e2e, "destroy_tree",
                        lambda d, fid, allowed_parent, **kw: calls.append(fid))
    report = e2e.sweep_orphan_prefixes(drive=drive, test_root_id="root",
                                      dry_run=True)
    assert [r.split("（")[0] for r in report["removed"]] == ["e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert calls == [], "dry-run 不可以刪"


def test_sweep_only_looks_at_the_direct_children(monkeypatch):
    """前綴底下的子資料夾（inbox／readview…）不是前綴，不該被掃到。"""
    import scripts.e2e_setup as e2e

    drive = _SweepDrive({
        "root": [("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", "f1", True)],
        "f1": [("inbox", "f1a", True), ("agora", "f1b", True)],
    })
    calls: list[str] = []
    monkeypatch.setattr(e2e, "destroy_tree",
                        lambda d, fid, allowed_parent, **kw: calls.append(fid) or 1)
    report = e2e.sweep_orphan_prefixes(drive=drive, test_root_id="root")
    assert calls == ["f1"]
    assert report["found"] == ["e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV"]


def test_sweep_keeps_everything_the_state_file_mentions(monkeypatch):
    """狀態檔裡記錄的 id（自訂前綴名稱時）也要保留，不能被掃掉。"""
    import scripts.e2e_setup as e2e

    drive = _SweepDrive({
        "root": [("my-custom-e2e", "f1", True), ("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", "f2", True)],
        "f1": [], "f2": [],
    })
    calls: list[str] = []
    monkeypatch.setattr(e2e, "destroy_tree",
                        lambda d, fid, allowed_parent, **kw: calls.append(fid) or 1)
    e2e.sweep_orphan_prefixes(drive=drive, test_root_id="root", keep_ids=["f1"])
    assert calls == ["f2"], "只有標準命名的孤兒該被清掉"


def test_sweep_refuses_when_parents_dont_match(monkeypatch):
    """刪之前一定 get() 確認 parents；不在允許父層就要拒絕。"""
    import scripts.e2e_setup as e2e

    class Weird(_SweepDrive):
        def get(self, file_id: str):
            from types import SimpleNamespace

            return SimpleNamespace(id=file_id, name="e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", is_folder=True,
                                   parents=("somewhere-else",))

    drive = Weird({"root": [("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", "f1", True)], "f1": []})
    with pytest.raises(RuntimeError, match="拒絕刪除"):
        e2e.sweep_orphan_prefixes(drive=drive, test_root_id="root")


def test_setup_calls_sweep_before_creating(monkeypatch):
    """setup 一開始就要清孤兒（不然中斷留下的垃圾會一直留著）。"""
    import scripts.e2e_setup as e2e

    calls: list[str] = []
    monkeypatch.setattr(e2e, "sweep_orphan_prefixes",
                        lambda **kw: calls.append("sweep") or {"found": [], "removed": [],
                                                               "kept": [], "dry_run": False})

    class Stop(Exception):
        pass

    class FakeDrive:
        def __init__(self, *a, **k) -> None:
            pass

        def create(self, *a, **k):
            raise Stop

    monkeypatch.setattr(e2e, "HttpDriveClient", FakeDrive)
    monkeypatch.setattr(e2e, "RcloneConfToken", lambda *a, **k: None)
    monkeypatch.setattr(e2e, "read_test_folder_id", lambda p: "root")
    monkeypatch.setattr(e2e, "read_sa_email", lambda p: "reader@example.com")
    monkeypatch.setattr(e2e, "_require_file", lambda p, d: p)
    with pytest.raises(Stop):
        e2e.run_setup(state_file=Path("/nonexistent/state.json"))
    # sweep 有被呼叫（即使後面因為假的 drive 停下來）
    assert calls == ["sweep"]


def test_repo_suffix_separates_e2e_pins_from_integration_tests():
    """`MyAiStorage-pin-test` 是共用的，e2e 的釘選值不能跟整合測試同名。

    實測過：e2e 用 `agora` 時，A 線的整合測試一輪就把 `.pin/agora.json` 蓋掉。
    """
    import inspect

    import scripts.e2e_setup as e2e

    assert e2e.DEFAULT_REPO_SUFFIX == "e2e"
    assert "repo_suffix" in inspect.signature(e2e.run_setup).parameters
    # 名字是算出來的（不寫死），而且 config 產出用的是帶後綴的那個
    src = Path(e2e.__file__).read_text(encoding="utf-8")
    assert 'f"agora-{repo_suffix}"' in src
    assert 'f"foundry-{repo_suffix}"' in src
    assert '"repo": agora_pin_name' in src
    assert '"repo": foundry_pin_name' in src
    assert '"repo": "agora",' not in src
    assert '"repo": "foundry",' not in src


# ── E-P4：測試 profile 與住民秘密目錄 ───────────────────────────────────


def test_e2e_profile_defaults_to_a_test_profile_and_rejects_production():
    import scripts.e2e_setup as e2e

    assert e2e.DEFAULT_E2E_PROFILE == "mac-opencode-test"
    assert e2e.resolve_e2e_profile({}) == "mac-opencode-test"
    # 可用環境變數覆寫
    assert e2e.resolve_e2e_profile({e2e.ENV_E2E_PROFILE: "other-test"}) == "other-test"
    # 正式 profile 一律拒絕（spec「期 1 的身分種類」）
    with pytest.raises(RuntimeError, match="不能用正式的"):
        e2e.resolve_e2e_profile({e2e.ENV_E2E_PROFILE: "mac-opencode"})


def test_secrets_root_is_not_the_production_one():
    import scripts.e2e_setup as e2e

    root = e2e.resolve_secrets_root({})
    assert root.name == "resident-e2e"
    assert root.resolve() != e2e.PRODUCTION_SECRETS_ROOT.resolve()
    with pytest.raises(RuntimeError, match="禁止使用正式"):
        e2e.resolve_secrets_root(
            {e2e.ENV_E2E_SECRETS_ROOT: str(e2e.PRODUCTION_SECRETS_ROOT)}
        )


def test_profile_dir_files_match_the_resident_whitelist(tmp_path: Path):
    """容器只掛白名單檔名，其他檔名一律拒絕啟動（run.sh）。"""
    import scripts.e2e_setup as e2e

    assert set(e2e.RESIDENT_WHITELIST) == {
        "rclone-worker.conf", "sa-reader.json", "signing.key", "reader.json",
        "gh-pat-actions.txt",
    }
    # 白名單的正則（與 resident/run.sh 同形）
    import re as _re

    rx = _re.compile(
        r"^(rclone-worker\.conf|sa-reader\.json|signing\.key|reader\.json"
        r"|gh-pat-actions\.txt|llm-[a-z0-9][a-z0-9_-]*\.key)$"
    )
    for name in (*e2e.RESIDENT_WHITELIST, "llm-opencode.key"):
        assert rx.match(name), f"{name} 不符合 resident/run.sh 的白名單"


def test_setup_resident_profile_dir_places_files_by_path(tmp_path: Path):
    """只以路徑複製／連結，不讀內容。"""
    import scripts.e2e_setup as e2e

    src = tmp_path / "src"
    src.mkdir()
    signing = src / "signing.key"
    signing.write_bytes(bytes(range(32)))
    signing.chmod(0o600)
    worker = src / "rclone-worker.conf"
    worker.write_text("[gdrive]\n")
    worker.chmod(0o600)
    sa = src / "sa-reader.json"
    sa.write_text('{"client_email":"reader@example.com"}')
    sa.chmod(0o600)
    llm = src / "llm-opencode.key"
    llm.write_text("not-a-real-key\n")
    llm.chmod(0o600)

    info = e2e.setup_resident_profile_dir(
        profile="mac-opencode-test",
        secrets_root=tmp_path / "resident-e2e",
        reader_config_payload={"format": "aistorage.reader/v1",
                               "manifest_file_id": "m1",
                               "inbox_folder_ids": {"mac-opencode-test": "inbox1"}},
        llm_key_source=llm,
        worker_conf=worker,
        sa_reader_key=sa,
        signing_key=signing,
        model="opencode/space-bunny-free",
    )
    profile_dir = Path(info["profile_dir"])
    assert profile_dir.name == "mac-opencode-test"
    # provider=opencode 是免金鑰 → **不放** llm-opencode.key（放佔位值會壞掉）
    assert set(info["files"]) == {
        "signing.key", "rclone-worker.conf", "sa-reader.json", "reader.json",
    }
    assert "免金鑰" in info["llm_note"]
    assert not (profile_dir / "llm-opencode.key").exists()
    # 預設是硬連結（或權限太寬時自動改成 0600 的複製）：對 resident/run.sh 的
    # stat 來看是 0600 的一般檔案（symlink 會顯示 lrwxr-xr-x 而被權限檢查擋下）
    for name in ("signing.key", "rclone-worker.conf", "sa-reader.json"):
        placed = profile_dir / name
        assert not placed.is_symlink(), f"{name} 不該是 symlink（run.sh 會擋）"
        assert placed.is_file()
        assert stat.S_IMODE(placed.stat().st_mode) & 0o077 == 0, f"{name} 權限過寬"
        assert info["placements"][name] == "hardlink", info["placements"][name]
    # reader.json 是實際寫出的非秘密設定
    reader = profile_dir / "reader.json"
    assert not reader.is_symlink()
    assert json.loads(reader.read_text(encoding="utf-8"))["inbox_folder_ids"] == {
        "mac-opencode-test": "inbox1"
    }


def test_symlink_is_opt_in_and_documented_as_rejected(tmp_path: Path):
    """symlink 只有在明確要求時才用，而且要記住 run.sh 會擋。"""
    import scripts.e2e_setup as e2e

    src = tmp_path / "src"
    src.mkdir()
    (src / "signing.key").write_bytes(bytes(range(32)))
    (src / "rclone-worker.conf").write_text("[gdrive]\n")
    (src / "sa-reader.json").write_text('{"client_email":"r@example.com"}')
    info = e2e.setup_resident_profile_dir(
        profile="p", secrets_root=tmp_path / "root",
        reader_config_payload={"manifest_file_id": "m"}, llm_key_source=None,
        worker_conf=src / "rclone-worker.conf",
        sa_reader_key=src / "sa-reader.json",
        signing_key=src / "signing.key", secrets_how="symlink",
        env={e2e.ENV_E2E_LLM_KEY: ""},
    )
    assert info["placements"]["signing.key"] == "symlink"
    # 用 BSD stat 的寫法看：symlink 一定是 lrwxr-xr-x → run.sh 會擋
    link = Path(info["profile_dir"]) / "signing.key"
    assert link.is_symlink()
    # symlink 本身帶 group/other 執行位元 → run.sh 的 group_other 檢查會擋
    assert stat.S_IMODE(link.lstat().st_mode) & 0o005 != 0
    src_txt = (Path(e2e.__file__)).read_text(encoding="utf-8")
    assert "會被 run.sh 擋" in src_txt or "被「權限過寬」擋下" in src_txt


def test_setup_resident_profile_dir_copy_mode(tmp_path: Path, monkeypatch):
    import scripts.e2e_setup as e2e

    # 正式 profile 目錄裡的同名檔（5.2 驗收的佔位值）不能被拿來當 fallback
    monkeypatch.setattr(e2e, "PRODUCTION_SECRETS_ROOT", tmp_path / "no-production")
    src = tmp_path / "src"
    src.mkdir()
    signing = src / "signing.key"
    signing.write_bytes(bytes(range(32)))
    (src / "rclone-worker.conf").write_text("[gdrive]\n")
    (src / "sa-reader.json").write_text('{"client_email":"r@example.com"}')
    info = e2e.setup_resident_profile_dir(
        profile="p", secrets_root=tmp_path / "root",
        reader_config_payload={"manifest_file_id": "m"},
        llm_key_source=None,
        worker_conf=src / "rclone-worker.conf",
        sa_reader_key=src / "sa-reader.json",
        signing_key=signing, copy_secrets=True,
        # provider 不在免金鑰清單 → 真的要金鑰
        env={e2e.ENV_E2E_LLM_KEY: "", e2e.KEYLESS_PROVIDERS_ENV: ""},
        model="ollama-cloud/deepseek",
    )
    # 沒有 LLM 金鑰來源 → 明確記下「要使用者提供」，不自己產生
    assert "llm-ollama-cloud.key" not in info["files"]
    assert e2e.ENV_E2E_LLM_KEY in info["llm_note"]
    assert info["placements"]["signing.key"] == "copy"
    assert (Path(info["profile_dir"]) / "signing.key").is_file()


def test_profile_dir_never_reads_secret_contents(tmp_path: Path, monkeypatch):
    """即使來源檔不存在也要明確報錯，而不是「讀看看」。"""
    import scripts.e2e_setup as e2e

    src = tmp_path / "src"
    src.mkdir()
    signing = src / "signing.key"
    signing.write_bytes(bytes(range(32)))
    with pytest.raises(RuntimeError, match="缺少必要檔案"):
        e2e.setup_resident_profile_dir(
            profile="p", secrets_root=tmp_path / "root",
            reader_config_payload={"manifest_file_id": "m"},
            llm_key_source=tmp_path / "nope.key",
            worker_conf=src / "nope", sa_reader_key=src / "nope",
            signing_key=signing,
        )


def test_reader_e2e_registers_only_the_test_profile(tmp_path: Path):
    """conftest 從 inbox_folder_ids 的唯一條目取 profile，所以只能有一個。"""
    import scripts.e2e_setup as e2e

    src = tmp_path / "src"
    src.mkdir()
    (src / "signing.key").write_bytes(bytes(range(32)))
    (src / "rclone-worker.conf").write_text("[gdrive]\n")
    (src / "sa-reader.json").write_text('{"client_email":"r@example.com"}')
    info = e2e.setup_resident_profile_dir(
        profile="mac-opencode-test", secrets_root=tmp_path / "root",
        reader_config_payload={"manifest_file_id": "m",
                               "inbox_folder_ids": {"mac-opencode-test": "i"}},
        llm_key_source=None,
        worker_conf=src / "rclone-worker.conf",
        sa_reader_key=src / "sa-reader.json",
        signing_key=src / "signing.key",
    )
    payload = json.loads((Path(info["profile_dir"]) / "reader.json").read_text(encoding="utf-8"))
    assert list(payload["inbox_folder_ids"]) == ["mac-opencode-test"]


def test_keyless_provider_removes_a_stale_placeholder(tmp_path: Path, monkeypatch):
    """免金鑰的 provider：既有的 llm-<provider>.key 要被移掉。

    放著一個佔位值比沒有更糟——實測 apiKey 指向佔位／不存在的檔案時，
    opencode 連 `POST /session` 都會回 400。
    """
    import scripts.e2e_setup as e2e

    monkeypatch.setattr(e2e, "PRODUCTION_SECRETS_ROOT", tmp_path / "no-production")
    src = tmp_path / "src"
    src.mkdir()
    (src / "signing.key").write_bytes(bytes(range(32)))
    (src / "rclone-worker.conf").write_text("[gdrive]\n")
    (src / "sa-reader.json").write_text('{"client_email":"r@example.com"}')
    root = tmp_path / "root"
    profile_dir = root / "mac-opencode-test"
    profile_dir.mkdir(parents=True)
    stale = profile_dir / "llm-opencode.key"
    stale.write_text("placeholder\n")

    info = e2e.setup_resident_profile_dir(
        profile="mac-opencode-test", secrets_root=root,
        reader_config_payload={"manifest_file_id": "m"}, llm_key_source=None,
        worker_conf=src / "rclone-worker.conf",
        sa_reader_key=src / "sa-reader.json",
        signing_key=src / "signing.key",
    )
    assert not stale.exists(), "佔位的金鑰檔必須被移除"
    assert "已移除" in info["llm_note"]


def test_keyless_provider_ignores_an_explicit_llm_key(tmp_path: Path, monkeypatch):
    """就算使用者給了 AISTORAGE_E2E_LLM_KEY，免金鑰的 provider 也不放。"""
    import scripts.e2e_setup as e2e

    monkeypatch.setattr(e2e, "PRODUCTION_SECRETS_ROOT", tmp_path / "no-production")
    src = tmp_path / "src"
    src.mkdir()
    (src / "signing.key").write_bytes(bytes(range(32)))
    (src / "rclone-worker.conf").write_text("[gdrive]\n")
    (src / "sa-reader.json").write_text('{"client_email":"r@example.com"}')
    given = src / "llm-opencode.key"
    given.write_text("real-ish\n")
    info = e2e.setup_resident_profile_dir(
        profile="mac-opencode-test", secrets_root=tmp_path / "root",
        reader_config_payload={"manifest_file_id": "m"}, llm_key_source=given,
        worker_conf=src / "rclone-worker.conf",
        sa_reader_key=src / "sa-reader.json",
        signing_key=src / "signing.key",
    )
    assert "llm-opencode.key" not in info["files"]
    assert "忽略" in info["llm_note"]


def test_keyless_provider_list_is_shared_with_run_sh():
    """e2e_setup、run.sh、entrypoint.sh 必須是同一份清單（同一個環境變數）。"""
    import scripts.e2e_setup as e2e

    assert e2e.KEYLESS_PROVIDERS_ENV == "AISTORAGE_KEYLESS_PROVIDERS"
    run_sh = (e2e.REPO_ROOT / "resident" / "run.sh").read_text(encoding="utf-8")
    entrypoint = (e2e.REPO_ROOT / "resident" / "image" / "entrypoint.sh").read_text(
        encoding="utf-8"
    )
    for text in (run_sh, entrypoint):
        assert "AISTORAGE_KEYLESS_PROVIDERS" in text
        assert "opencode" in text


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--teardown" in out
    assert "--recreate" in out
    assert "--sweep-orphans" in out
    assert "--profile" in out
    assert "--llm-key" in out
    assert "--secrets-root" in out


# ── e2e 環境鎖 ────────────────────────────────────────────────────────────────
# e2e 環境是單例（Drive 前綴、pin 釘選值、本機設定檔都只有一份）。
# 任何一條線跑 teardown／recreate 都會摧毀另一條線正在跑的東西，所以要鎖。


@pytest.fixture
def other_pid():
    """一個**別的行程**且確定還活著的 pid（用來模擬「別條線正在用」）。"""
    proc = subprocess.Popen(["sleep", "60"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def dead_pid() -> int:
    """一個確定已經死掉的 pid（開一個行程、拿到 pid、等它結束）。"""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _write_lock(root: Path, pid: int, action: str = "setup", host: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    e2e_lock_path(root).write_text(
        json.dumps({"pid": pid, "started_at": 1_700_000_000.0,
                    "host": host if host is not None else os.uname().nodename,
                    "action": action}),
        encoding="utf-8",
    )


def test_lock_file_lives_under_the_secrets_root(tmp_path: Path):
    root = tmp_path / "resident-e2e"
    root.mkdir()
    path = e2e_lock_path(root)
    assert path.parent == root
    assert path.name.startswith(".")  # 祕密目錄裡不要跟憑證檔混在一起被看到


def test_lock_records_pid_and_start_time(tmp_path: Path):
    acquire_e2e_lock(action="setup", secrets_root=tmp_path, now=1_700_000_000.0)
    data = json.loads(e2e_lock_path(tmp_path).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["started_at"] == 1_700_000_000.0
    assert data["action"] == "setup"
    assert data["host"]


def test_second_holder_is_refused_while_the_first_is_alive(
    tmp_path: Path, other_pid: int
):
    _write_lock(tmp_path, other_pid, action="pytest e2e")
    holder = e2e_lock_holder(tmp_path)
    assert holder is not None and holder["pid"] == other_pid

    with pytest.raises(E2EEnvLocked) as e:
        acquire_e2e_lock(action="teardown", secrets_root=tmp_path)
    msg = str(e.value)
    assert str(other_pid) in msg             # 誰在用
    assert "pytest e2e" in msg               # 對方在做什麼
    assert "--force-unlock" in msg           # 怎麼強制
    # 拒絕時不該動到對方的鎖
    assert json.loads(e2e_lock_path(tmp_path).read_text(encoding="utf-8"))["action"] == "pytest e2e"


def test_same_process_may_reenter(tmp_path: Path):
    """同一個行程重入不算「別人在用」（`--recreate` 內部會再呼叫 run_teardown）。"""
    acquire_e2e_lock(action="setup", secrets_root=tmp_path)
    acquire_e2e_lock(action="teardown", secrets_root=tmp_path)  # 不該被擋
    data = json.loads(e2e_lock_path(tmp_path).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert "forced_by" not in data           # 自己重入不算「強制搶過」


def test_dead_holder_lock_is_expired_and_can_be_taken_over(
    tmp_path: Path, dead_pid: int
):
    _write_lock(tmp_path, dead_pid, action="setup")
    assert e2e_lock_holder(tmp_path) is None      # 過期
    acquire_e2e_lock(action="teardown", secrets_root=tmp_path)  # 可以接手
    assert json.loads(e2e_lock_path(tmp_path).read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_lock_from_another_host_is_treated_as_expired(
    tmp_path: Path, other_pid: int
):
    _write_lock(tmp_path, other_pid, action="teardown", host="some-other-mac")
    # 別台機器留下的鎖驗證不了，不擋著本機（否則會永久鎖死）
    assert e2e_lock_holder(tmp_path) is None
    acquire_e2e_lock(action="setup", secrets_root=tmp_path)


def test_corrupt_lock_file_does_not_lock_forever(tmp_path: Path):
    e2e_lock_path(tmp_path).write_text("{ not json", encoding="utf-8")
    assert e2e_lock_holder(tmp_path) is None
    acquire_e2e_lock(action="setup", secrets_root=tmp_path)


def test_force_unlock_takes_over_and_records_it(tmp_path: Path, other_pid: int):
    _write_lock(tmp_path, other_pid, action="pytest e2e")
    acquire_e2e_lock(action="teardown", secrets_root=tmp_path, force=True)
    data = json.loads(e2e_lock_path(tmp_path).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["forced_by"]["prev_pid"] == other_pid  # 留痕


def test_release_only_removes_your_own_lock(tmp_path: Path, other_pid: int):
    # 鎖是別人的 → 我們不該把它刪掉
    _write_lock(tmp_path, other_pid, action="pytest e2e")
    assert release_e2e_lock(tmp_path) is False
    assert e2e_lock_path(tmp_path).is_file()

    # 是自己的 → 刪得掉
    _write_lock(tmp_path, os.getpid(), action="setup")
    assert release_e2e_lock(tmp_path) is True
    assert not e2e_lock_path(tmp_path).exists()


def test_release_is_idempotent(tmp_path: Path):
    assert release_e2e_lock(tmp_path) is False
    acquire_e2e_lock(action="setup", secrets_root=tmp_path)
    assert release_e2e_lock(tmp_path) is True
    assert release_e2e_lock(tmp_path) is False


def test_teardown_refuses_while_locked_before_touching_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_pid: int
):
    """真的 `run_teardown`：有人在用就拒絕，而且**在碰 Drive 之前**就拒絕。"""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"prefix_folder_id": "fld_abc", "prefix_name": "e2e-x"}),
        encoding="utf-8",
    )
    touched: list[str] = []
    monkeypatch.setattr("scripts.e2e_setup.read_test_folder_id", lambda *a, **k: "root_1")
    monkeypatch.setattr("scripts.e2e_setup.RcloneConfToken", lambda *a, **k: object())
    monkeypatch.setattr(
        "scripts.e2e_setup.HttpDriveClient",
        lambda *a, **k: touched.append("drive") or object(),
    )

    _write_lock(tmp_path, other_pid, action="pytest e2e")
    with pytest.raises(E2EEnvLocked):
        run_teardown(state_file=state, secrets_root=tmp_path)

    assert touched == []            # 沒有連上 Drive、沒有刪任何東西
    assert state.is_file()          # 本機設定也沒被動
    # 對方的鎖要原封不動留著（拒絕的人不該替別人解鎖）
    assert e2e_lock_holder(tmp_path) is not None


def test_teardown_takes_over_an_expired_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dead_pid: int
):
    """持有者已死 → 鎖過期 → teardown 照跑，並在結束後把鎖清掉。"""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"prefix_folder_id": "fld_abc", "prefix_name": "e2e-x"}),
        encoding="utf-8",
    )
    _write_lock(tmp_path, dead_pid, action="pytest e2e")
    removed: list[int] = []
    monkeypatch.setattr("scripts.e2e_setup.read_test_folder_id", lambda *a, **k: "root_1")
    monkeypatch.setattr("scripts.e2e_setup.RcloneConfToken", lambda *a, **k: object())
    monkeypatch.setattr("scripts.e2e_setup.destroy_tree",
                        lambda drive, folder_id, **kw: removed.append(1) or 3)

    assert run_teardown(state_file=state, secrets_root=tmp_path) == 3
    assert removed == [1]                       # 真的刪了
    assert not state.is_file()                  # 本機設定清掉
    assert not e2e_lock_path(tmp_path).exists() # 鎖也清掉


def test_cli_teardown_returns_75_while_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_pid: int
):
    """CLI 層：拒絕時回 75（EX_TEMPFAIL）並印訊息，不要丟 traceback。"""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"prefix_folder_id": "fld_abc", "prefix_name": "e2e-x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.e2e_setup.read_test_folder_id", lambda *a, **k: "root_1")
    monkeypatch.setattr("scripts.e2e_setup.resolve_secrets_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr("scripts.e2e_setup.RcloneConfToken", lambda *a, **k: object())
    monkeypatch.setattr(
        "scripts.e2e_setup.HttpDriveClient",
        lambda *a, **k: pytest.fail("被鎖擋下時不該連上 Drive"),
    )

    _write_lock(tmp_path, other_pid, action="pytest e2e")
    assert main(["--teardown", "--state-file", str(state)]) == 75
    assert state.is_file()
    # 別人的鎖不該被這次拒絕動到
    assert json.loads(e2e_lock_path(tmp_path).read_text(encoding="utf-8"))["pid"] == other_pid
