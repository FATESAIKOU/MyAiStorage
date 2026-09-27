"""tests/unit/test_identity.py

tasks 2.3 單元測試：AiStorage Profile 身分登錄、金鑰管理與授權驗證。
依據：
- g2-api-2.3.md
- g2-test-2.3.md
- g2-2.4-2.3-fixes.md
- openspec/changes/establish-aistorage-phase1/design.md（D2、D3）
- openspec/changes/establish-aistorage-phase1/specs/common/identity/spec.md
- schemas/identity-registry.schema.json
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from aistorage.identity import (
    Registry,
    generate_keypair,
    load_registry,
    validate_registry,
)
from aistorage.inbox import (
    sign_sidecar_bytes,
    verify_sidecar_bytes,
)
from aistorage.schema import FieldError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def example_identity_path() -> Path:
    """指向專案內既有的 config/identity.example.json。"""
    repo_root = Path(__file__).resolve().parent.parent.parent
    path = repo_root / "config" / "identity.example.json"
    assert path.is_file(), f"範例登錄檔不存在: {path}"
    return path


@pytest.fixture
def example_registry_data(example_identity_path: Path) -> dict:
    """載入範例登錄檔字典資料供測試使用。"""
    with open(example_identity_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def generated_ed25519_keypair() -> tuple[bytes, bytes]:
    """產生一組隨機 Ed25519 金鑰對 (raw 32 bytes priv, raw 32 bytes pub)。"""
    return generate_keypair()


@pytest.fixture
def real_valid_registry_data(generated_ed25519_keypair: tuple[bytes, bytes]) -> dict:
    """產生包含真實 Ed25519 公鑰與匹配 key_id 的合法登錄檔資料。"""
    _, pub = generated_ed25519_keypair
    pub_hex8 = hashlib.sha256(pub).hexdigest()[:8]
    pub_b64 = base64.b64encode(pub).decode("ascii")
    return {
        "format": "aistorage.identity/v1",
        "profiles": {
            "mac-opencode": {
                "allowed_types": [
                    "session",
                    "handoff",
                    "claim",
                    "reference",
                    "rewrite",
                    "artifact",
                ],
                "signing_keys": [
                    {
                        "key_id": f"mac-opencode-{pub_hex8}",
                        "public_key": pub_b64,
                        "status": "active",
                        "added_at": "2026-09-27T08:00:00Z",
                        "revoked_at": None,
                    }
                ],
                "inbox_folder_ids": ["1Obn3Rj1Quyg1l_2YW0GhXE39FpETeyLj"],
            }
        },
    }


# ---------------------------------------------------------------------------
# 1. validate_registry 測試
# ---------------------------------------------------------------------------

class TestValidateRegistry:
    """測試 validate_registry 對身分登錄檔之結構與領域規則驗證。"""

    def test_valid_example_file(self, example_registry_data: dict):
        """標準範例登錄檔應完全通過驗證（allow_example=True 放行範例 dummy 公鑰）。"""
        errors = validate_registry(example_registry_data, allow_example=True)
        assert errors == [], f"範例登錄檔應通過驗證，但得到: {errors}"

    def test_non_dict_rejected(self):
        """傳入非 dict 物件應回傳 field='' 之 FieldError。"""
        for bad_input in [None, "string", 123, [1, 2, 3]]:
            errors = validate_registry(bad_input)
            assert len(errors) >= 1
            assert any(e.field == "" for e in errors)

    def test_required_root_fields(self, real_valid_registry_data: dict):
        """缺少 format 或 profiles 根欄位應回傳錯誤。"""
        for req in ["format", "profiles"]:
            data = copy.deepcopy(real_valid_registry_data)
            del data[req]
            errors = validate_registry(data)
            assert any(req in e.field for e in errors), f"缺少 {req} 應報錯: {errors}"

    def test_root_format_exact_match(self, real_valid_registry_data: dict):
        """format 不等於 'aistorage.identity/v1' 應被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["format"] = "aistorage.identity/v2"
        errors = validate_registry(data)
        assert any("format" in e.field for e in errors)

    def test_profiles_empty_dict_is_valid(self):
        """profiles 為空 dict 時符合結構要求（尚未註冊任何 profile）。"""
        data = {
            "format": "aistorage.identity/v1",
            "profiles": {},
        }
        assert validate_registry(data) == []

    @pytest.mark.parametrize(
        "valid_name",
        ["mac-opencode", "worker-default", "agent-01", "mobile", "a-b-c-1-2-3"],
    )
    def test_profile_name_format_valid(
        self, generated_ed25519_keypair: tuple[bytes, bytes], valid_name: str
    ):
        """符合 ^[a-z][a-z0-9-]*$ 之 profile 名稱應通過驗證。"""
        _, pub = generated_ed25519_keypair
        pub_hex8 = hashlib.sha256(pub).hexdigest()[:8]
        pub_b64 = base64.b64encode(pub).decode("ascii")
        data = {
            "format": "aistorage.identity/v1",
            "profiles": {
                valid_name: {
                    "allowed_types": ["session"],
                    "signing_keys": [
                        {
                            "key_id": f"{valid_name}-{pub_hex8}",
                            "public_key": pub_b64,
                            "status": "active",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": None,
                        }
                    ],
                    "inbox_folder_ids": ["folder-xyz"],
                }
            },
        }
        assert validate_registry(data) == []

    @pytest.mark.parametrize(
        "invalid_name",
        [
            "Mac-OpenCode",  # 大寫字母
            "worker/default",  # 斜線符號
            "123agent",  # 數字開頭
            "-profile",  # 連字號開頭
            "_profile",  # 底線開頭
            "profile_name",  # 包含底線
            "profile name",  # 包含空格
            "",  # 空字串
        ],
    )
    def test_profile_name_format_invalid(self, real_valid_registry_data: dict, invalid_name: str):
        """不符 ^[a-z][a-z0-9-]*$ 之 profile 名稱必須被拒絕。"""
        profile_content = real_valid_registry_data["profiles"]["mac-opencode"]
        data = {
            "format": "aistorage.identity/v1",
            "profiles": {invalid_name: copy.deepcopy(profile_content)},
        }
        errors = validate_registry(data)
        assert any("profiles" in e.field for e in errors), f"無效名稱 {invalid_name!r} 應被拒絕: {errors}"

    @pytest.mark.parametrize(
        "missing_field",
        ["allowed_types", "signing_keys", "inbox_folder_ids"],
    )
    def test_profile_required_fields(self, real_valid_registry_data: dict, missing_field: str):
        """Profile 缺少必填欄位應被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        del data["profiles"]["mac-opencode"][missing_field]
        errors = validate_registry(data)
        assert any(missing_field in e.field for e in errors)

    @pytest.mark.parametrize(
        "missing_key_field",
        ["key_id", "public_key", "status", "added_at", "revoked_at"],
    )
    def test_signing_key_required_fields(self, real_valid_registry_data: dict, missing_key_field: str):
        """金鑰項目缺少必填欄位應被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        del data["profiles"]["mac-opencode"]["signing_keys"][0][missing_key_field]
        errors = validate_registry(data)
        assert any(missing_key_field in e.field for e in errors)

    def test_signing_key_status_enum(self, real_valid_registry_data: dict):
        """金鑰 status 必須為 'active' 或 'revoked'。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["profiles"]["mac-opencode"]["signing_keys"][0]["status"] = "expired"
        errors = validate_registry(data)
        assert any("status" in e.field for e in errors)

    def test_signing_key_added_at_format(self, real_valid_registry_data: dict):
        """added_at 必須符合 RFC 3339 UTC 格式 (帶 Z)。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["profiles"]["mac-opencode"]["signing_keys"][0]["added_at"] = "2026-09-27 08:00:00"
        errors = validate_registry(data)
        assert any("added_at" in e.field for e in errors)

    def test_signing_key_key_id_not_empty(self, real_valid_registry_data: dict):
        """key_id 不得為空字串。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["profiles"]["mac-opencode"]["signing_keys"][0]["key_id"] = ""
        errors = validate_registry(data)
        assert any("key_id" in e.field for e in errors)

    def test_duplicate_key_id_within_same_profile(self, real_valid_registry_data: dict):
        """同一個 profile 內金鑰 key_id 重複必須被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        key1 = data["profiles"]["mac-opencode"]["signing_keys"][0]
        key2 = copy.deepcopy(key1)
        data["profiles"]["mac-opencode"]["signing_keys"].append(key2)
        errors = validate_registry(data)
        assert any("key_id" in e.field and "重複" in e.message for e in errors)

    def test_duplicate_key_id_across_profiles(self, real_valid_registry_data: dict):
        """跨 profile 出現相同 key_id 必須被拒絕（全域唯一性）。"""
        data = copy.deepcopy(real_valid_registry_data)
        dup_key = copy.deepcopy(data["profiles"]["mac-opencode"]["signing_keys"][0])
        data["profiles"]["another-profile"] = {
            "allowed_types": ["session"],
            "signing_keys": [dup_key],
            "inbox_folder_ids": ["folder-xyz"],
        }
        errors = validate_registry(data)
        assert any("key_id" in e.field and "重複" in e.message for e in errors)

    def test_key_id_fingerprint_mismatch_rejected(self, real_valid_registry_data: dict):
        """2.3 決定 1：key_id 必須為 <profile>-<sha256(pub)[:8]>，指紋不符時拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        # 故意將最後 8 碼指紋改為假 hex
        data["profiles"]["mac-opencode"]["signing_keys"][0]["key_id"] = "mac-opencode-deadbeef"
        errors = validate_registry(data)
        assert any("key_id" in e.field and ("不符" in e.message or "指紋" in e.message) for e in errors)

    def test_key_id_profile_prefix_mismatch_rejected(self, real_valid_registry_data: dict):
        """2.3 決定 1：key_id 前綴必須與所屬 profile 名稱相符。"""
        data = copy.deepcopy(real_valid_registry_data)
        key = data["profiles"]["mac-opencode"]["signing_keys"][0]
        hex8 = key["key_id"].split("-")[-1]
        key["key_id"] = f"other-profile-{hex8}"
        errors = validate_registry(data)
        assert any("key_id" in e.field for e in errors)

    def test_dummy_all_zero_key_rejected_in_non_example_file(self, real_valid_registry_data: dict):
        """2.3 決定 3：正式/非範例檔中（allow_example=False）禁止使用全 0 範例公鑰。"""
        data = copy.deepcopy(real_valid_registry_data)
        zero_pub = b"\x00" * 32
        zero_hex8 = hashlib.sha256(zero_pub).hexdigest()[:8]
        zero_b64 = base64.b64encode(zero_pub).decode("ascii")

        data["profiles"]["mac-opencode"]["signing_keys"][0]["key_id"] = f"mac-opencode-{zero_hex8}"
        data["profiles"]["mac-opencode"]["signing_keys"][0]["public_key"] = zero_b64

        # allow_example=False (預設) 應拒絕
        errors = validate_registry(data, allow_example=False)
        assert any("public_key" in e.field and "全 0" in e.message for e in errors)

        # allow_example=True 應放行
        errors_allowed = validate_registry(data, allow_example=True)
        assert errors_allowed == []

    @pytest.mark.parametrize("typo_type", ["sessoin", "unknown_type", "task", "foo"])
    def test_allowed_types_invalid_type_rejected(self, real_valid_registry_data: dict, typo_type: str):
        """2.3 決定 5：allowed_types 限定為 2.1 定義之項目型態，拼錯或未知型態必須被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["profiles"]["mac-opencode"]["allowed_types"] = [typo_type]
        errors = validate_registry(data)
        assert any("allowed_types" in e.field for e in errors)

    def test_revoked_key_missing_revoked_at_rejected(self, real_valid_registry_data: dict):
        """金鑰 status 為 revoked 但 revoked_at 為 null 時必須被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        key = data["profiles"]["mac-opencode"]["signing_keys"][0]
        key["status"] = "revoked"
        key["revoked_at"] = None
        errors = validate_registry(data)
        assert any("revoked_at" in e.field for e in errors)

    def test_revoked_key_with_timestamp_accepted(self, real_valid_registry_data: dict):
        """金鑰 status 為 revoked 且提供有效 revoked_at 時間戳時應通過驗證。"""
        data = copy.deepcopy(real_valid_registry_data)
        key = data["profiles"]["mac-opencode"]["signing_keys"][0]
        key["status"] = "revoked"
        key["revoked_at"] = "2026-09-27T09:00:00Z"
        errors = validate_registry(data)
        assert errors == []

    def test_active_key_with_revoked_at_rejected(self, real_valid_registry_data: dict):
        """2.3 決定 5：active 狀態金鑰的 revoked_at 必須為 null，帶時間戳必須被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        key = data["profiles"]["mac-opencode"]["signing_keys"][0]
        key["status"] = "active"
        key["revoked_at"] = "2026-09-27T09:00:00Z"
        errors = validate_registry(data)
        assert any("revoked_at" in e.field for e in errors)

    @pytest.mark.parametrize(
        "bad_key",
        [
            "not-base64-string!!",
            base64.b64encode(b"A" * 16).decode(),  # 只有 16 bytes
            base64.b64encode(b"A" * 64).decode(),  # 64 bytes
        ],
    )
    def test_public_key_not_32_bytes_base64_rejected(self, real_valid_registry_data: dict, bad_key: str):
        """公開金鑰若不是合法 Base64 或長度不是 Ed25519 32 位元組，必須被拒絕。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["profiles"]["mac-opencode"]["signing_keys"][0]["public_key"] = bad_key
        errors = validate_registry(data)
        assert any("public_key" in e.field for e in errors)

    def test_unknown_fields_rejected_by_schema(self, real_valid_registry_data: dict):
        """身分登錄檔為核心安全設定，schema 設定 strict additionalProperties: false，拒絕額外欄位。"""
        data = copy.deepcopy(real_valid_registry_data)
        data["x_unknown_root"] = "val"
        errors = validate_registry(data)
        assert len(errors) >= 1
        assert any("Additional properties" in e.message for e in errors)


# ---------------------------------------------------------------------------
# 2. load_registry 測試
# ---------------------------------------------------------------------------

class TestLoadRegistry:
    """測試 load_registry 載入與解析身分登錄檔。"""

    def test_load_example_registry(self, example_identity_path: Path):
        """成功載入範例登錄檔（明確傳入 allow_example=True 放行範例公鑰）並回傳 Registry 實例。"""
        reg = load_registry(example_identity_path, allow_example=True)
        assert isinstance(reg, Registry)

    def test_load_example_registry_default_rejected(self, example_identity_path: Path):
        """預設 allow_example=False 時載入範例登錄檔必須拋出 ValueError。"""
        with pytest.raises(ValueError, match="禁止使用全 0 範例公鑰"):
            load_registry(example_identity_path)

    def test_load_custom_valid_file(self, tmp_path: Path, real_valid_registry_data: dict):
        """成功載入自訂合法登錄檔。"""
        file_path = tmp_path / "identity.json"
        file_path.write_text(json.dumps(real_valid_registry_data), encoding="utf-8")
        reg = load_registry(file_path)
        assert isinstance(reg, Registry)

    def test_load_invalid_registry_raises_value_error(self, tmp_path: Path):
        """驗證失敗之登錄檔必須 raise ValueError（附錯誤訊息）。"""
        file_path = tmp_path / "invalid.json"
        file_path.write_text(json.dumps({"invalid": True}), encoding="utf-8")
        with pytest.raises(ValueError, match="身分登錄檔驗證失敗"):
            load_registry(file_path)

    def test_load_nonexistent_file_raises_error(self, tmp_path: Path):
        """載入不存在檔案時應拋出 FileNotFoundError。"""
        nonexistent = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError):
            load_registry(nonexistent)


# ---------------------------------------------------------------------------
# 3. Registry 方法測試 (active_public_keys, inbox_folders)
# ---------------------------------------------------------------------------

class TestRegistryMethods:
    """測試 Registry 查詢方法。"""

    def test_active_public_keys_filters_revoked(self):
        """active_public_keys 只回傳 active 狀態金鑰，排除 revoked 金鑰。"""
        raw_pub1 = b"\x01" * 32
        raw_pub2 = b"\x02" * 32
        hex1 = hashlib.sha256(raw_pub1).hexdigest()[:8]
        hex2 = hashlib.sha256(raw_pub2).hexdigest()[:8]
        data = {
            "format": "aistorage.identity/v1",
            "profiles": {
                "test-profile": {
                    "allowed_types": ["session"],
                    "signing_keys": [
                        {
                            "key_id": f"test-profile-{hex1}",
                            "public_key": base64.b64encode(raw_pub1).decode(),
                            "status": "active",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": None,
                        },
                        {
                            "key_id": f"test-profile-{hex2}",
                            "public_key": base64.b64encode(raw_pub2).decode(),
                            "status": "revoked",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": "2026-09-27T09:00:00Z",
                        },
                    ],
                    "inbox_folder_ids": ["folder-001"],
                }
            },
        }
        reg = Registry(data)
        keys = reg.active_public_keys("test-profile")
        assert f"test-profile-{hex1}" in keys
        assert keys[f"test-profile-{hex1}"] == raw_pub1
        assert f"test-profile-{hex2}" not in keys

    def test_active_public_keys_unknown_profile(self, real_valid_registry_data: dict):
        """查詢不存在之 profile 回傳空 dict。"""
        reg = Registry(real_valid_registry_data)
        assert reg.active_public_keys("nonexistent-profile") == {}

    def test_inbox_folders_mapping(self):
        """inbox_folders 正確建立 folder_id -> profile 雙向對照。"""
        data = {
            "format": "aistorage.identity/v1",
            "profiles": {
                "mac-opencode": {
                    "allowed_types": ["session"],
                    "signing_keys": [],
                    "inbox_folder_ids": ["folder-mac-1", "folder-mac-2"],
                },
                "worker-default": {
                    "allowed_types": ["session", "handoff"],
                    "signing_keys": [],
                    "inbox_folder_ids": ["folder-worker-1"],
                },
            },
        }
        reg = Registry(data)
        folders = reg.inbox_folders()
        assert folders == {
            "folder-mac-1": "mac-opencode",
            "folder-mac-2": "mac-opencode",
            "folder-worker-1": "worker-default",
        }


# ---------------------------------------------------------------------------
# 4. Registry.authorize 授權驗證測試
# ---------------------------------------------------------------------------

class TestAuthorize:
    """測試 Registry.authorize 之身分與授權判定。"""

    @pytest.fixture
    def test_registry(self) -> Registry:
        raw_pub = b"K" * 32
        hex8 = hashlib.sha256(raw_pub).hexdigest()[:8]
        data = {
            "format": "aistorage.identity/v1",
            "profiles": {
                "mac-opencode": {
                    "allowed_types": ["session", "handoff", "claim"],
                    "signing_keys": [
                        {
                            "key_id": f"mac-opencode-{hex8}",
                            "public_key": base64.b64encode(raw_pub).decode(),
                            "status": "active",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": None,
                        },
                        {
                            "key_id": "mac-opencode-deadbeef",
                            "public_key": base64.b64encode(raw_pub).decode(),
                            "status": "revoked",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": "2026-09-27T09:00:00Z",
                        },
                    ],
                    "inbox_folder_ids": ["folder-mac"],
                },
                "worker-default": {
                    "allowed_types": ["session"],
                    "signing_keys": [
                        {
                            "key_id": f"worker-default-{hex8}",
                            "public_key": base64.b64encode(raw_pub).decode(),
                            "status": "active",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": None,
                        }
                    ],
                    "inbox_folder_ids": ["folder-worker"],
                },
            },
        }
        return Registry(data)

    def test_authorize_success(self, test_registry: Registry):
        """合法 sidecar 且金鑰驗章通過時，回傳 ('profile:<profile 名>', None)。"""
        key_id = list(test_registry.active_public_keys("mac-opencode").keys())[0]
        sidecar = {
            "profile": "mac-opencode",
            "metadata": {"type": "session"},
        }
        producer, reason = test_registry.authorize(sidecar, key_id)
        assert producer == "profile:mac-opencode"
        assert reason is None

    def test_authorize_unverified_key_rejected(self, test_registry: Registry):
        """驗章失敗（key_id=None）必須被拒絕。"""
        sidecar = {
            "profile": "mac-opencode",
            "metadata": {"type": "session"},
        }
        producer, reason = test_registry.authorize(sidecar, None)
        assert producer is None
        assert reason is not None
        assert "驗證" in reason or "無效" in reason

    def test_authorize_impersonation_rejected(self, test_registry: Registry):
        """用 A 的金鑰簽章、但 sidecar.profile 宣稱 B 時必須拒絕（防止冒充）。"""
        worker_key_id = list(test_registry.active_public_keys("worker-default").keys())[0]
        sidecar = {
            "profile": "mac-opencode",  # 宣稱為 mac-opencode
            "metadata": {"type": "session"},
        }
        producer, reason = test_registry.authorize(sidecar, worker_key_id)
        assert producer is None
        assert reason is not None
        assert "不屬於" in reason

    def test_authorize_revoked_key_rejected(self, test_registry: Registry):
        """金鑰已被撤銷時必須被拒絕。"""
        sidecar = {
            "profile": "mac-opencode",
            "metadata": {"type": "session"},
        }
        producer, reason = test_registry.authorize(sidecar, "mac-opencode-deadbeef")
        assert producer is None
        assert reason is not None
        assert "撤銷" in reason

    def test_authorize_nonexistent_profile_rejected(self, test_registry: Registry):
        """Profile 未在登錄檔中註冊時必須被拒絕。"""
        sidecar = {
            "profile": "unknown-profile",
            "metadata": {"type": "session"},
        }
        key_id = list(test_registry.active_public_keys("mac-opencode").keys())[0]
        producer, reason = test_registry.authorize(sidecar, key_id)
        assert producer is None
        assert reason is not None
        assert "未在身分登錄檔中註冊" in reason or "不存在" in reason

    def test_authorize_unauthorized_type_rejected(self, test_registry: Registry):
        """項目 metadata.type 不在 allowed_types 清單內時必須被拒絕。"""
        worker_key_id = list(test_registry.active_public_keys("worker-default").keys())[0]
        sidecar = {
            "profile": "worker-default",  # 僅允許 session
            "metadata": {"type": "rewrite"},  # 嘗試提交 rewrite
        }
        producer, reason = test_registry.authorize(sidecar, worker_key_id)
        assert producer is None
        assert reason is not None
        assert "未獲" in reason or "授權" in reason

    def test_authorize_malformed_sidecar_returns_format_error(self, test_registry: Registry):
        """2.3 決定 5：authorize 對格式錯誤的 sidecar 回傳 (None, '格式錯誤')。"""
        key_id = list(test_registry.active_public_keys("mac-opencode").keys())[0]
        for bad_sidecar in [
            "not-a-dict",
            None,
            [],
            {},  # 缺少 profile 與 metadata
            {"profile": "mac-opencode"},  # 缺少 metadata
            {"profile": "mac-opencode", "metadata": {}},  # 缺少 metadata.type
            {"profile": "mac-opencode", "metadata": "not-a-dict"},
        ]:
            producer, reason = test_registry.authorize(bad_sidecar, key_id)
            assert producer is None
            assert reason == "格式錯誤"


# ---------------------------------------------------------------------------
# 5. 端到端測試 (generate_keypair -> sign -> verify -> authorize)
# ---------------------------------------------------------------------------

class TestEndToEndIdentityWorkflow:
    """端到端測試：產生金鑰對 -> sidecar 簽章 -> 驗章 -> Registry 授權。"""

    def test_end_to_end_sign_verify_authorize(self, generated_ed25519_keypair: tuple[bytes, bytes]):
        """完整模擬提交流程收件匣簽章與產生者授權流程。"""
        priv_bytes, pub_bytes = generated_ed25519_keypair
        profile_name = "test-e2e-worker"
        pub_hex8 = hashlib.sha256(pub_bytes).hexdigest()[:8]
        key_id = f"{profile_name}-{pub_hex8}"

        # 1. 登記公開金鑰至 Registry
        reg_data = {
            "format": "aistorage.identity/v1",
            "profiles": {
                profile_name: {
                    "allowed_types": ["session", "artifact"],
                    "signing_keys": [
                        {
                            "key_id": key_id,
                            "public_key": base64.b64encode(pub_bytes).decode("ascii"),
                            "status": "active",
                            "added_at": "2026-09-27T08:00:00Z",
                            "revoked_at": None,
                        }
                    ],
                    "inbox_folder_ids": ["folder-e2e-123"],
                }
            },
        }
        registry = Registry(reg_data)

        # 2. 寫入者產生 sidecar 內容與分離式簽章
        sidecar = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "profile": profile_name,
            "metadata": {
                "id": "session:opencode:ses_001",
                "type": "session",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": None,
            "body": {
                "snapshot_at": "2026-09-27T08:00:00Z",
                "session": {
                    "source": "opencode",
                    "in_progress": False,
                    "archived_at": None,
                    "parent_id": None,
                },
            },
        }
        sidecar_bytes = json.dumps(sidecar, separators=(",", ":")).encode("utf-8")
        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)

        # 3. 提交流程取得該 profile 之有效公鑰並驗章
        active_keys = registry.active_public_keys(profile_name)
        assert key_id in active_keys
        verified_key_id = verify_sidecar_bytes(sidecar_bytes, sig, active_keys)
        assert verified_key_id == key_id

        # 4. 提交流程授權並取得產生者識別
        producer, reason = registry.authorize(sidecar, verified_key_id)
        assert producer == f"profile:{profile_name}"
        assert reason is None


# ---------------------------------------------------------------------------
# 6. CLI 測試 (keygen, check)
# ---------------------------------------------------------------------------

class TestCLI:
    """測試 aistorage.identity 命令列介面。"""

    def test_keygen_creates_private_key_file_mode_600(self, tmp_path: Path):
        """keygen 命令建立之私鑰檔權限必須為 0600，且為 32 位元組。"""
        out_key_path = tmp_path / "test.key"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistorage.identity",
                "keygen",
                "--profile",
                "mac-opencode",
                "--out",
                str(out_key_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, f"keygen 執行失敗: {proc.stderr}"
        assert out_key_path.is_file()

        # 檢查私鑰檔長度
        assert out_key_path.stat().st_size == 32

        # 檢查私鑰檔權限為 0600 (owner rw only)
        mode = stat.S_IMODE(out_key_path.stat().st_mode)
        assert mode == 0o600, f"私鑰檔案權限應為 0600，實際為 {oct(mode)}"

    def test_keygen_creates_directory_mode_700(self, tmp_path: Path):
        """2.3 決定 2：keygen 建目錄時權限必須為 700。"""
        nested_dir = tmp_path / "keys_dir" / "secret"
        out_key_path = nested_dir / "mac-opencode.key"

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistorage.identity",
                "keygen",
                "--profile",
                "mac-opencode",
                "--out",
                str(out_key_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0
        assert nested_dir.is_dir()
        dir_mode = stat.S_IMODE(nested_dir.stat().st_mode)
        assert dir_mode == 0o700, f"目錄權限應為 0700，實際為 {oct(dir_mode)}"

    def test_keygen_checks_profile_format(self, tmp_path: Path):
        """2.3 決定 2：keygen 檢查 --profile 格式，無效格式退出非 0。"""
        out_key_path = tmp_path / "invalid.key"
        for bad_profile in ["Bad_Profile", "123agent", "worker/default", ""]:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "aistorage.identity",
                    "keygen",
                    "--profile",
                    bad_profile,
                    "--out",
                    str(out_key_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode != 0
            assert "不合法" in proc.stderr or "格式" in proc.stderr or "error" in proc.stderr.lower()

    def test_keygen_stdout_contains_key_id_and_pubkey_no_privkey(self, tmp_path: Path):
        """keygen 輸出僅印出 key_id 與公鑰，絕不印出私鑰。"""
        out_key_path = tmp_path / "test_sec.key"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistorage.identity",
                "keygen",
                "--profile",
                "worker-test",
                "--out",
                str(out_key_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0
        stdout = proc.stdout

        # 檢查包含 key_id 與 public_key
        assert "key_id:" in stdout
        assert "worker-test" in stdout
        assert "public_key:" in stdout

        # 讀取產生的私鑰 raw bytes，確認未以任何形式洩漏在 stdout / stderr
        priv_bytes = out_key_path.read_bytes()
        priv_b64 = base64.b64encode(priv_bytes).decode("ascii")
        priv_hex = priv_bytes.hex()

        assert priv_b64 not in stdout
        assert priv_hex not in stdout
        assert priv_b64 not in proc.stderr
        assert priv_hex not in proc.stderr

    def test_keygen_refuses_overwrite(self, tmp_path: Path):
        """若私鑰檔案已存在，keygen 必須拒絕覆寫並退出非 0。"""
        existing_key_path = tmp_path / "existing.key"
        original_content = b"ORIGINAL_KEY_CONTENT_DO_NOT_MOD"
        existing_key_path.write_bytes(original_content)

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistorage.identity",
                "keygen",
                "--profile",
                "mac-opencode",
                "--out",
                str(existing_key_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode != 0
        assert "拒絕覆寫" in proc.stderr or "已存在" in proc.stderr
        # 內容不得被竄改
        assert existing_key_path.read_bytes() == original_content

    def test_check_valid_registry_exit_0(self, example_identity_path: Path):
        """check 驗證合法範例登錄檔應回傳 0。"""
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistorage.identity",
                "check",
                str(example_identity_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "驗證通過" in proc.stdout

    def test_check_invalid_registry_exit_nonzero(self, tmp_path: Path):
        """check 驗證損毀/不合規登錄檔應回傳非 0 退出碼。"""
        bad_file = tmp_path / "bad_identity.json"
        bad_file.write_text(json.dumps({"format": "invalid"}), encoding="utf-8")

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistorage.identity",
                "check",
                str(bad_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert "失敗" in proc.stderr or "失敗" in proc.stdout
