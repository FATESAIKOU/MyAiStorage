"""第 9 組 e2e_setup 的驗收測試（測試方；只依介面與 spec）。

驗收重點（PM 指定）：
1. **冪等**：重跑 setup 直接重用現有環境（不重建、不新增 Drive 資源、不重寫
   設定）；`recreate=True` 先清掉舊的再建新的。
2. **teardown 只刪自己的前綴**：狀態檔指到的那一棵樹；同一層的其他 `e2e-*`、
   整合測試的 `it-*` 與不相干的檔案都不動。本機只刪自己產出的設定檔，
   測試 profile 的秘密目錄保留。
3. **產出的設定只有非秘密 id**：reader 設定的每個值都是 Drive id、路徑或格式
   字串；收件匣對應只有測試 profile；committer 設定不含任何「秘密味」欄位名。
4. **秘密只以路徑出現**：來源祕密（worker conf、SA 金鑰、LLM 金鑰、簽章私鑰）
   的內容絕不出現在任何產出的設定檔裡；它們只被放到測試 profile 的秘密目錄。

全程用 `FakeDrive`（記憶體）與假的 annex／pin 協作；不碰真的 Drive、GitHub
或任何真實憑證。祕密來源檔的內容一律自己編。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.e2e_setup as e2e
from aistorage.drive.fake import FakeDrive
from aistorage.errors import NotFound

#: 祕密來源檔的哨兵字串：產出的設定裡絕不該出現它們。
SECRET_WORKER = "SUPERSECRET-WORKER-9c1f"
SECRET_SA = "SUPERSECRET-SA-7ab3"
SECRET_LLM = "SUPERSECRET-LLM-51d0"

PROFILE = "mac-test"
#: 需要金鑰的 provider（`opencode` 是免金鑰的，那條路徑另外測）。
MODEL = "ollama-cloud/test-model"


# ---------------------------------------------------------------------------
# 假的環境
# ---------------------------------------------------------------------------


def _fake_build_annex(**kwargs: Any) -> e2e.AnnexSetup:
    """假的 git-annex repo 建立：只回傳介面要的欄位，不碰 git／Drive。"""
    name = kwargs["repo_name"]
    return e2e.AnnexSetup(
        prefix=kwargs["prefix"],
        uuid=f"uuid-{name}",
        url=f"annex::{name}",
        main_sha="a" * 40,
        annex_sha="b" * 40,
        max_git_bundles=int(kwargs.get("max_git_bundles", 10)),
    )


def _fake_setup_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[FakeDrive, dict[str, Path]]:
    """把 run_setup 的重協作換成假的；祕密來源只以路徑提供。

    回傳 `(drive, out)`；`out` 是產出檔案的路徑（都在 tmp_path 底下）。
    """
    drive = FakeDrive()
    src = tmp_path / "sources"
    src.mkdir()
    worker = src / "rclone-worker.conf"
    worker.write_text(f"[gdrive]\ntoken = {SECRET_WORKER}\n", encoding="utf-8")
    sa = src / "sa-reader.json"
    sa.write_text(
        json.dumps({"client_email": "reader@example.com", "private_key": SECRET_SA}),
        encoding="utf-8",
    )
    llm = src / "llm-ollama-cloud.key"
    llm.write_text(SECRET_LLM + "\n", encoding="utf-8")
    committer_conf = src / "rclone-committer-test.conf"
    committer_conf.write_text("[gdrive]\n", encoding="utf-8")
    pin_key = src / "pin-test.key"
    pin_key.write_text("pin-deploy-key-placeholder", encoding="utf-8")
    known_hosts = src / "github_known_hosts"
    known_hosts.write_text("github.com ssh-ed25519 AAAA\n", encoding="utf-8")
    ids = src / "ids.env"
    ids.write_text("TEST_FOLDER_ID=root\n", encoding="utf-8")

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    out = {
        "state": tmp_path / "e2e_state.json",
        "committer": cfg_dir / "committer.e2e.json",
        "foundry": cfg_dir / "committer.foundry.e2e.json",
        "identity": cfg_dir / "identity.e2e.json",
        "reader_root": tmp_path / "reader.e2e.json",
        "reader_cfg": cfg_dir / "reader.e2e.json",
        "keys_dir": tmp_path / "test-keys",
        "secrets_root": tmp_path / "resident-e2e",
        "llm": llm,
    }

    monkeypatch.setenv("RCLONE_CONFIG", "not-used")
    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(e2e, "IDS_ENV", ids)
    monkeypatch.setattr(e2e, "COMMITTER_CONF", committer_conf)
    monkeypatch.setattr(e2e, "WORKER_CONF", worker)
    monkeypatch.setattr(e2e, "SA_READER_KEY", sa)
    monkeypatch.setattr(e2e, "PIN_KEY", pin_key)
    monkeypatch.setattr(e2e, "KNOWN_HOSTS", known_hosts)
    monkeypatch.setattr(e2e, "TEST_KEYS_DIR", out["keys_dir"])
    monkeypatch.setattr(e2e, "COMMITTER_E2E_JSON", out["committer"])
    monkeypatch.setattr(e2e, "COMMITTER_FOUNDRY_E2E_JSON", out["foundry"])
    monkeypatch.setattr(e2e, "IDENTITY_E2E_JSON", out["identity"])
    monkeypatch.setattr(e2e, "READER_E2E_JSON", out["reader_root"])
    monkeypatch.setattr(e2e, "READER_CONFIG_E2E_JSON", out["reader_cfg"])

    monkeypatch.setattr(e2e, "HttpDriveClient", lambda *a, **k: drive)
    monkeypatch.setattr(e2e, "RcloneConfToken", lambda *a, **k: object())
    monkeypatch.setattr(e2e, "build_annex_repo", _fake_build_annex)
    monkeypatch.setattr(e2e, "GitPinStore", lambda **k: object())
    monkeypatch.setattr(
        e2e,
        "init_pin_cli",
        lambda cfg, deps, confirm: SimpleNamespace(
            manifest_sha256="0" * 64, annex_keys=frozenset()
        ),
    )
    monkeypatch.setattr(e2e, "share_folder_with_reader", lambda *a, **k: None)
    return drive, out


def _run_setup(drive: FakeDrive, out: dict[str, Path], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "state_file": out["state"],
        "profile": PROFILE,
        "secrets_root": out["secrets_root"],
        "model": MODEL,
        "llm_key_source": out["llm"],
    }
    kwargs.update(overrides)
    return e2e.run_setup(**kwargs)


def _patch_teardown_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drive: FakeDrive
) -> dict[str, Path]:
    """run_teardown 需要的最小介面：假的 Drive／token、tmp 的本機設定路徑。"""
    ids = tmp_path / "ids.env"
    ids.write_text("TEST_FOLDER_ID=root\n", encoding="utf-8")
    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(e2e, "IDS_ENV", ids)
    monkeypatch.setattr(e2e, "HttpDriveClient", lambda *a, **k: drive)
    monkeypatch.setattr(e2e, "RcloneConfToken", lambda *a, **k: object())
    local: dict[str, Path] = {}
    for attr in (
        "COMMITTER_E2E_JSON",
        "COMMITTER_FOUNDRY_E2E_JSON",
        "IDENTITY_E2E_JSON",
        "READER_E2E_JSON",
        "READER_CONFIG_E2E_JSON",
    ):
        path = tmp_path / f"{attr.lower()}.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(e2e, attr, path)
        local[attr] = path
    return local


def _config_files(out: dict[str, Path]) -> list[Path]:
    """容器／提交流程讀的設定檔（狀態檔是本機帳本，不算）。"""
    return [out["committer"], out["foundry"], out["identity"], out["reader_root"], out["reader_cfg"]]


def _walk_keys(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_keys(value)


# ---------------------------------------------------------------------------
# 冪等
# ---------------------------------------------------------------------------


def test_setup_is_idempotent_and_reuses_the_existing_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """重跑 setup 直接回傳同一份狀態：不重建 repo、不新增 Drive 資源。"""
    drive, out = _fake_setup_env(monkeypatch, tmp_path)

    calls: list[str] = []
    original = e2e.build_annex_repo

    def _counting_build(**kwargs: Any) -> e2e.AnnexSetup:
        calls.append(kwargs["repo_name"])
        return original(**kwargs)

    monkeypatch.setattr(e2e, "build_annex_repo", _counting_build)

    state1 = _run_setup(drive, out)
    snapshot1 = {fid: f.name for fid, f in drive.snapshot()["files"].items()}
    assert sorted(calls) == ["agora", "foundry"], "第一次要真的建兩個 repo"

    calls.clear()
    state2 = _run_setup(drive, out)
    snapshot2 = {fid: f.name for fid, f in drive.snapshot()["files"].items()}

    assert state2 == state1, "重跑必須回傳同一份狀態（不重建）"
    assert snapshot2 == snapshot1, "重跑不得新增或改動任何 Drive 項目"
    assert calls == [], "重跑不得再建任何 git-annex repo"
    assert state2["prefix_folder_id"] == state1["prefix_folder_id"]
    # 收件匣只有一個（不是每次 setup 都建一個新的）
    inboxes = [name for name in snapshot1.values() if name == "inbox"]
    assert len(inboxes) == 1


def test_recreate_tears_down_then_creates_a_fresh_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`recreate=True` 先跑 teardown（刪舊的前綴）再建一個新的。"""
    drive, out = _fake_setup_env(monkeypatch, tmp_path)
    state1 = _run_setup(drive, out)
    old_prefix = state1["prefix_folder_id"]

    seen: list[dict[str, Any]] = []
    original = e2e.run_teardown

    def _recording_teardown(**kwargs: Any) -> int:
        seen.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(e2e, "run_teardown", _recording_teardown)

    state2 = _run_setup(drive, out, recreate=True)

    assert seen and seen[0]["state_file"] == out["state"], "recreate 必須先 teardown"
    assert state2["prefix_folder_id"] != old_prefix, "recreate 必須建新前綴"
    with pytest.raises(NotFound):
        drive.get(old_prefix)
    assert drive.get(state2["prefix_folder_id"]).is_folder


# ---------------------------------------------------------------------------
# teardown 只刪自己的前綴
# ---------------------------------------------------------------------------


def test_teardown_deletes_only_the_prefix_from_the_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """只刪狀態檔指到的那一棵樹；同層其他前綴與檔案原封不動。"""
    drive = FakeDrive()
    target = drive.seed_folder("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", parent="root")
    agora = drive.seed_folder("agora", parent=target)
    drive.seed_file(agora, "GITBUNDLE--x", b"target-bundle")
    inbox = drive.seed_folder("inbox", parent=target)
    drive.seed_file(inbox, "item.raw", b"target-raw")
    other_prefix = drive.seed_folder("e2e-01BX5ZZKBKACTAV9WEVGEMMVRZ", parent="root")
    drive.seed_file(other_prefix, "bundle", b"other-bundle")
    it_prefix = drive.seed_folder("it-01CCCCCCCCCCCCCCCCCCCCCCCC", parent="root")
    drive.seed_file("root", "notes.txt", b"unrelated")

    local = _patch_teardown_env(monkeypatch, tmp_path, drive)
    # 測試 profile 的秘密目錄：teardown 之後必須保留
    secret_file = tmp_path / "resident-e2e" / PROFILE / "signing.key"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_bytes(b"s" * 32)

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"prefix_folder_id": target, "prefix_name": "e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV"}),
        encoding="utf-8",
    )

    removed = e2e.run_teardown(state_file=state_file)

    assert removed == 5, "應刪掉前綴＋兩個子資料夾＋兩個檔案"
    with pytest.raises(NotFound):
        drive.get(target)
    assert drive.get(other_prefix).name.startswith("e2e-01BX"), "別人的 e2e 前綴不得被刪"
    assert drive.get(it_prefix).is_folder, "整合測試的 it-* 前綴不得被刪"
    assert any(f.name == "notes.txt" for f in drive.list_children("root"))
    for path in local.values():
        assert not path.exists(), f"本機設定檔應該被清掉：{path}"
    assert secret_file.is_file(), "測試 profile 的秘密目錄要保留（冪等重用）"


def test_teardown_refuses_a_prefix_whose_parents_do_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """刪除前一定 `get()` 確認 parents；不是自己測資夾底下的就拒絕。"""
    drive = FakeDrive()
    foreign = drive.seed_folder("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", parent="somewhere-else")
    _patch_teardown_env(monkeypatch, tmp_path, drive)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"prefix_folder_id": foreign}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="拒絕刪除"):
        e2e.run_teardown(state_file=state_file)
    assert drive.get(foreign).is_folder, "拒絕之後不得刪掉任何東西"


def test_teardown_without_a_target_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """沒有狀態檔也沒有指定 id → 回 0，不動任何資源。"""
    drive = FakeDrive()
    drive.seed_folder("e2e-01ARZ3NDEKTSV4RRFFQ69G5FAV", parent="root")
    _patch_teardown_env(monkeypatch, tmp_path, drive)

    removed = e2e.run_teardown(state_file=tmp_path / "no-such-state.json")

    assert removed == 0
    assert len(drive.snapshot()["files"]) == 1


# ---------------------------------------------------------------------------
# 產出的設定只有非秘密 id；秘密只以路徑出現
# ---------------------------------------------------------------------------


def test_written_configs_contain_only_ids_paths_and_format_strings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """reader／committer 設定裡的每個值都是 id、路徑或格式字串。"""
    drive, out = _fake_setup_env(monkeypatch, tmp_path)
    state = _run_setup(drive, out)

    reader_cfg = json.loads(out["reader_cfg"].read_text(encoding="utf-8"))
    assert set(reader_cfg) == {
        "format",
        "manifest_file_id",
        "readview_folder_id",
        "inbox_folder_ids",
        "sa_key_path",
        "foundry_manifest_file_id",
        "foundry_readview_folder_id",
    }
    assert reader_cfg["format"] == "aistorage.reader/v1"
    assert list(reader_cfg["inbox_folder_ids"]) == [PROFILE], "只能有測試 profile"
    # 每個 id 都真的指向 FakeDrive 裡的物件（不是祕密、不是任意字串）
    for key in (
        "manifest_file_id",
        "readview_folder_id",
        "foundry_manifest_file_id",
        "foundry_readview_folder_id",
    ):
        drive.get(reader_cfg[key])
    inbox = drive.get(reader_cfg["inbox_folder_ids"][PROFILE])
    assert inbox.is_folder and inbox.name == "inbox"
    # SA 金鑰只以路徑出現（不是內容）
    assert reader_cfg["sa_key_path"].endswith("sa-reader.json")
    assert "{" not in reader_cfg["sa_key_path"]
    # 兩個 reader 設定檔（repo 根目錄與 config/）內容一致
    assert out["reader_root"].read_text(encoding="utf-8") == out["reader_cfg"].read_text(
        encoding="utf-8"
    )

    committer_cfg = json.loads(out["committer"].read_text(encoding="utf-8"))
    assert committer_cfg["repo"] == "agora-e2e", "e2e 的釘選值要和整合測試分開"
    assert committer_cfg["foundry"]["repo"] == "foundry-e2e"
    assert committer_cfg["repo_url"].startswith("annex::")
    assert committer_cfg["identity_registry_path"] == "config/identity.e2e.json"
    assert state["prefix_folder_id"] == drive.get(state["prefix_folder_id"]).id

    # 沒有任何「秘密味」的欄位名（token／secret／password／private_key…）
    for path in _config_files(out):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in _walk_keys(payload):
            assert not re.search(r"(secret|password|token|private)", str(key), re.I), (
                f"{path.name} 出現可疑欄位：{key}"
            )


def test_secrets_only_appear_as_paths_never_as_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """來源祕密的內容絕不出現在產出的設定檔；它們只被放到測試 profile 目錄。"""
    drive, out = _fake_setup_env(monkeypatch, tmp_path)
    state = _run_setup(drive, out)

    private_key = (out["keys_dir"] / "signing.key").read_bytes()
    secret_variants = [
        SECRET_WORKER.encode("utf-8"),
        SECRET_SA.encode("utf-8"),
        SECRET_LLM.encode("utf-8"),
        private_key,
        private_key.hex().encode("ascii"),
        base64.b64encode(private_key),
    ]
    for path in _config_files(out):
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        for variant in secret_variants:
            assert variant not in raw, f"{path.name} 出現了祕密內容"
        assert '"private_key"' not in text, f"{path.name} 不該有 private_key 欄位"
        assert '"api_key"' not in text and '"token"' not in text

    # 容器讀的 reader.json（非秘密）也只有 id／路徑
    profile_reader = Path(state["profile_dir"]) / "reader.json"
    profile_payload = json.loads(profile_reader.read_text(encoding="utf-8"))
    assert profile_payload == json.loads(out["reader_cfg"].read_text(encoding="utf-8"))

    # 祕密被放到測試 profile 的祕密目錄（檔案本身），不是寫進設定
    profile_dir = Path(state["profile_dir"])
    assert profile_dir.name == PROFILE
    assert profile_dir != e2e.PRODUCTION_SECRETS_ROOT
    for name in ("rclone-worker.conf", "sa-reader.json", "llm-ollama-cloud.key", "signing.key"):
        assert (profile_dir / name).is_file(), f"秘密目錄缺少 {name}"
    # 目錄裡不得有白名單以外的檔名（容器會拒絕啟動）
    allowed = re.compile(
        r"^(rclone-worker\.conf|sa-reader\.json|signing\.key|reader\.json"
        r"|gh-pat-actions\.txt|llm-[a-z0-9][a-z0-9_-]*\.key)$"
    )
    for child in profile_dir.iterdir():
        assert allowed.match(child.name), f"白名單外的檔名：{child.name}"


def test_keyless_provider_does_not_get_a_placeholder_llm_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """免金鑰的 provider（opencode）：即使給了 LLM 金鑰來源也不放 llm-*.key。

    佔位／無效的金鑰會讓 opencode 連 `POST /session` 都 400（實測），所以
    setup 必須忽略它；需要金鑰的 provider 才放（另一個測試驗那條）。
    """
    drive, out = _fake_setup_env(monkeypatch, tmp_path)
    state = _run_setup(drive, out, model="opencode/space-bunny-free")

    profile_dir = Path(state["profile_dir"])
    llm_names = sorted(p.name for p in profile_dir.iterdir() if p.name.startswith("llm-"))
    assert llm_names == [], f"免金鑰的 provider 不該有 llm-*.key：{llm_names}"
    assert (profile_dir / "signing.key").is_file()
    assert (profile_dir / "rclone-worker.conf").is_file()
