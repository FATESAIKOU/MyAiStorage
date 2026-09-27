"""3.11 手動匯入的驗收測試（C 線撰寫，未看實作，只依文件與簽名）。

對照：docs/impl/group3-modules.md §7.4（spec「單一 Session 手動匯入」、
與同步器共用 build_inbox_item、重複匯入冪等）。
匯出樣本使用已 commit 的測試資料，不碰真實 Session。
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aistorage.converters import get_converter
from aistorage.drive.fake import FakeDrive
from aistorage.importer import ImportResult, import_session, main
from aistorage.inbox import validate_sidecar, verify_sidecar_bytes
from aistorage.inbox_builder import (
    InboxBuildError,
    build_inbox_item,
    detect_source_session_id,
    load_private_key,
    read_item_key,
)

OP_EXPORT = Path("tests/unit/data/converters/opencode/basic.json")
CC_JSONL = Path("tests/unit/data/converters/claude-code/basic.jsonl")
PROFILE = "mac-opencode"


@pytest.fixture()
def keypair(tmp_path: Path) -> tuple[bytes, bytes, Path, str]:
    """產生 Ed25519 簽章金鑰：回傳 (私鑰, 公鑰, 600 私鑰檔, 合法 key_id)。

    key_id 規則（與 inbox_builder 一致）：<profile>-<公鑰雜湊前8位小寫hex>。
    """
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    key_path = tmp_path / "test.key"
    key_path.write_bytes(seed)
    os.chmod(key_path, 0o600)
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    key_id = f"{PROFILE}-{hashlib.sha256(pub).hexdigest()[:8].lower()}"
    return seed, pub, key_path, key_id


def test_build_inbox_item_opencode_shape(keypair) -> None:
    """§7.4：共用函式產出的 sidecar＋sig 形狀正確，可驗章。"""
    seed, pub, _, KEY_ID = keypair
    raw = OP_EXPORT.read_bytes()
    facts = get_converter("opencode").facts(OP_EXPORT)
    source_session_id = detect_source_session_id("opencode", OP_EXPORT)
    sidecar_bytes, sig = build_inbox_item(
        OP_EXPORT, source="opencode", source_session_id=source_session_id,
        facts=facts, profile=PROFILE, key=seed, key_id=KEY_ID)
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert validate_sidecar(sidecar) == []
    assert verify_sidecar_bytes(sidecar_bytes, sig, {KEY_ID: pub}) == KEY_ID
    assert read_item_key(sidecar_bytes) == sidecar["item_key"]
    assert sidecar["metadata"]["id"] == f"opencode:{source_session_id}"
    assert sidecar["raw"] == {"sha256": hashlib.sha256(raw).hexdigest(),
                              "size": len(raw)}


def test_build_inbox_item_claude_code_shape(keypair) -> None:
    seed, pub, _, KEY_ID = keypair
    raw = CC_JSONL.read_bytes()
    facts = get_converter("claude-code").facts(CC_JSONL)
    source_session_id = detect_source_session_id("claude-code", CC_JSONL)
    assert source_session_id  # 自動推測出非空 id
    sidecar_bytes, sig = build_inbox_item(
        CC_JSONL, source="claude-code", source_session_id=source_session_id,
        facts=facts, profile=PROFILE, key=seed, key_id=KEY_ID)
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert validate_sidecar(sidecar) == []
    assert verify_sidecar_bytes(sidecar_bytes, sig, {KEY_ID: pub}) == KEY_ID
    assert sidecar["metadata"]["id"] == f"claude-code:{source_session_id}"


def test_import_session_out_dir_opencode(tmp_path: Path, keypair) -> None:
    """import_session 寫出 .raw／.sidecar.json／.sig 三檔，結果欄位完整。"""
    _, pub, key_path, KEY_ID = keypair
    out = tmp_path / "out"
    result = import_session(
        "opencode", OP_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out)
    assert isinstance(result, ImportResult)
    assert result.item_id.startswith("opencode:")
    assert result.destination == f"out-dir:{out}"
    assert len(result.written) == 3
    names = sorted(Path(p).name for p in result.written)
    assert names[0].endswith(".raw") and names[1].endswith(".sidecar.json")
    assert names[2].endswith(".sig")
    assert names[0].split(".")[0] == result.item_key
    sidecar = json.loads((out / names[1]).read_bytes().decode("utf-8"))
    assert validate_sidecar(sidecar, expected_item_key=result.item_key) == []
    sig = json.loads((out / names[2]).read_bytes().decode("utf-8"))
    assert verify_sidecar_bytes((out / names[1]).read_bytes(), sig, {KEY_ID: pub}) == KEY_ID
    assert result.raw_sha256 == hashlib.sha256(OP_EXPORT.read_bytes()).hexdigest()


def test_import_session_out_dir_claude_code(tmp_path: Path, keypair) -> None:
    _, pub, key_path, KEY_ID = keypair
    out = tmp_path / "out"
    result = import_session(
        "claude-code", CC_JSONL, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out)
    assert result.item_id.startswith("claude-code:")
    assert len(result.written) == 3


def test_import_repeat_same_item_key(tmp_path: Path, keypair) -> None:
    """重複匯入：同 item_key、同內容（ALREADY 的前提）。"""
    _, _, key_path, KEY_ID = keypair
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    fixed_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    first = import_session(
        "opencode", OP_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out1, item_key=fixed_key)
    second = import_session(
        "opencode", OP_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out2, item_key=fixed_key)
    assert first.item_key == second.item_key == fixed_key
    assert first.item_id == second.item_id
    assert first.raw_sha256 == second.raw_sha256


def test_import_options_override(tmp_path: Path, keypair) -> None:
    """session-id／stopped／parent-id 覆寫反映在 sidecar。"""
    _, _, key_path, KEY_ID = keypair
    out = tmp_path / "out"
    result = import_session(
        "opencode", OP_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out, source_session_id="custom-ses-1",
        parent_id="opencode:parent-1", status="stopped",
        stopped_at="2026-09-27T08:00:00Z")
    assert result.item_id == "opencode:custom-ses-1"
    assert result.status == "stopped"
    sidecar_path = next(p for p in result.written if p.endswith(".sidecar.json"))
    sidecar = json.loads((out / sidecar_path).read_bytes().decode("utf-8"))
    assert sidecar["metadata"]["id"] == "opencode:custom-ses-1"
    assert sidecar["session"]["status"] == "stopped"
    assert sidecar["session"]["stopped_at"] == "2026-09-27T08:00:00Z"
    assert sidecar["session"]["parent_id"] == "opencode:parent-1"


def test_import_inbox_folder_upload(tmp_path: Path, keypair) -> None:
    """inbox-folder 路徑：三檔上傳進 Drive 收件匣。"""
    _, _, key_path, KEY_ID = keypair
    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    result = import_session(
        "opencode", OP_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, inbox_folder_id=inbox, drive=drive)
    children = drive.list_children(inbox)
    assert len(children) == 3
    assert sorted(f.name.split(".", 1)[1] for f in children) == [
        "raw", "sidecar.json", "sig"]
    assert all(f.name.startswith(result.item_key) for f in children)


def test_cli_main_out_dir(tmp_path: Path, keypair, capsys) -> None:
    """CLI：opencode --out-dir 回傳 0 並寫檔；缺參數 argparse exit 2；壞檔非零。"""
    _, _, key_path, KEY_ID = keypair
    out = tmp_path / "out"
    rc = main(["opencode", "--export", str(OP_EXPORT), "--key", str(key_path),
               "--key-id", KEY_ID, "--profile", PROFILE, "--out-dir", str(out)])
    assert rc == 0
    assert len(list(out.iterdir())) == 3

    with pytest.raises(SystemExit) as exc:
        main(["opencode", "--export", str(OP_EXPORT)])
    assert exc.value.code == 2

    rc = main(["opencode", "--export", str(tmp_path / "missing.json"),
               "--key", str(key_path), "--key-id", KEY_ID,
               "--profile", PROFILE, "--out-dir", str(tmp_path / "o2")])
    assert rc != 0


def test_key_permission_enforced(tmp_path: Path) -> None:
    """私鑰檔權限過寬必須拒絕（不讀內容）。"""
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    loose = tmp_path / "loose.key"
    loose.write_bytes(seed)  # 預設 644
    with pytest.raises(InboxBuildError):
        load_private_key(loose)
