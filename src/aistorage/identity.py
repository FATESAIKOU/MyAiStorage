"""AiStorage 身分與簽章金鑰登錄管理模組。

依據規格：
- g2-api-2.3.md、g2-2.4-2.3-fixes.md 與 review-2.1f-2.4-2.3.md
- schemas/identity-registry.schema.json
- design D2、D3
- specs/common/identity
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from jsonschema import Draft202012Validator

from aistorage.schema import (
    FieldError,
    _create_format_checker,
    _locate_schema_file,
)

_FORMAT_CHECKER = _create_format_checker()
_REGISTRY_SCHEMA_PATH = _locate_schema_file("identity-registry.schema.json")

with open(_REGISTRY_SCHEMA_PATH, encoding="utf-8") as _f:
    _REGISTRY_SCHEMA = json.load(_f)

_REGISTRY_VALIDATOR = Draft202012Validator(
    _REGISTRY_SCHEMA, format_checker=_FORMAT_CHECKER
)

_REQUIRED_PATTERN = re.compile(r"'([^']+)' is a required property")
_PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

ALLOWED_ITEM_TYPES = {
    "session",
    "handoff",
    "claim",
    "reference",
    "rewrite",
    "artifact",
}

ZERO_KEY_B64 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
ZERO_KEY_BYTES = b"\x00" * 32


def generate_keypair() -> tuple[bytes, bytes]:
    """產生標準 Ed25519 金鑰對。

    Returns:
        (private_key_bytes, public_key_bytes)：皆為 32 位元組原始 (raw) bytes。
    """
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def validate_registry(obj: dict, allow_example: bool = False) -> list[FieldError]:
    """驗證登錄檔資料是否符合規範與領域規則。

    檢查項目：
    1. 符合 schemas/identity-registry.schema.json 結構。
    2. key_id 在整份登錄檔中全域唯一。
    3. key_id 必須等於 <profile>-<sha256(public_key_raw32)前8小寫hex>。
    4. 狀態為 'revoked' 的金鑰必須具備非空之 revoked_at 時間戳。
    5. 狀態為 'active' 的金鑰 revoked_at 必須為 null。
    6. public_key 能正確解碼為 32 位元組之 Ed25519 公鑰。
    7. allowed_types 限定為 2.1 定義之項目型態。
    8. 若非範例檔（allow_example=False），拒絕全 0 範例公鑰。

    Args:
        obj: 登錄檔字典。
        allow_example: 是否放行全 0 範例公鑰（預設 False）。

    Returns:
        FieldError 清單；空清單表示驗證通過。
    """
    if not isinstance(obj, dict):
        return [FieldError(field="", message="登錄檔必須是字典 (dict)")]

    errors: list[FieldError] = []

    # 1. JSON Schema 基礎結構驗證
    for err in _REGISTRY_VALIDATOR.iter_errors(obj):
        path_str = ".".join(str(p) for p in err.path)
        if err.validator == "required":
            m = _REQUIRED_PATTERN.search(err.message)
            missing = m.group(1) if m else ""
            field_name = f"{path_str}.{missing}" if path_str else missing
            errors.append(FieldError(field=field_name, message=f"缺少必填欄位: {missing}"))
        else:
            errors.append(FieldError(field=path_str, message=err.message))

    # 2. 領域規則檢查：key_id 唯一性、指紋相符、revoked/active 規則、公鑰長度與非全0
    profiles = obj.get("profiles")
    if isinstance(profiles, dict):
        seen_key_ids: set[str] = set()

        for prof_name, prof_data in profiles.items():
            if not isinstance(prof_data, dict):
                continue

            # allowed_types 檢查
            allowed_types = prof_data.get("allowed_types")
            if isinstance(allowed_types, list):
                for t_idx, t in enumerate(allowed_types):
                    if t not in ALLOWED_ITEM_TYPES:
                        errors.append(
                            FieldError(
                                field=f"profiles.{prof_name}.allowed_types[{t_idx}]",
                                message=f"未知的項目型態: '{t}'，允許的型態為 {sorted(ALLOWED_ITEM_TYPES)}",
                            )
                        )

            signing_keys = prof_data.get("signing_keys")
            if not isinstance(signing_keys, list):
                continue

            for idx, key_info in enumerate(signing_keys):
                if not isinstance(key_info, dict):
                    continue

                field_prefix = f"profiles.{prof_name}.signing_keys[{idx}]"
                kid = key_info.get("key_id")
                if isinstance(kid, str) and kid:
                    if kid in seen_key_ids:
                        errors.append(
                            FieldError(
                                field=f"{field_prefix}.key_id",
                                message=f"重複的 key_id: {kid}",
                            )
                        )
                    else:
                        seen_key_ids.add(kid)

                status = key_info.get("status")
                revoked_at = key_info.get("revoked_at")
                if status == "revoked" and not revoked_at:
                    errors.append(
                        FieldError(
                            field=f"{field_prefix}.revoked_at",
                            message=f"金鑰 '{kid}' 狀態為 revoked 時，revoked_at 必填且不得為空",
                        )
                    )
                elif status == "active" and revoked_at is not None:
                    errors.append(
                        FieldError(
                            field=f"{field_prefix}.revoked_at",
                            message=f"金鑰 '{kid}' 狀態為 active 時，revoked_at 必須為 null",
                        )
                    )

                pub_b64 = key_info.get("public_key")
                raw_pub: bytes | None = None
                if isinstance(pub_b64, str):
                    try:
                        raw_pub = base64.b64decode(pub_b64, validate=True)
                        if len(raw_pub) != 32:
                            errors.append(
                                FieldError(
                                    field=f"{field_prefix}.public_key",
                                    message=f"金鑰 '{kid}' 公鑰解碼長度應為 32 位元組，實際為 {len(raw_pub)}",
                                )
                            )
                            raw_pub = None
                    except Exception as e:
                        errors.append(
                            FieldError(
                                field=f"{field_prefix}.public_key",
                                message=f"金鑰 '{kid}' 公鑰非合法 base64: {e}",
                            )
                        )

                # 指紋核對：key_id 必須等於 <profile>-<sha256(raw_pub)[:8]>
                if raw_pub is not None and isinstance(kid, str) and kid:
                    fingerprint = hashlib.sha256(raw_pub).hexdigest()[:8]
                    expected_kid = f"{prof_name}-{fingerprint}"
                    if kid != expected_kid:
                        errors.append(
                            FieldError(
                                field=f"{field_prefix}.key_id",
                                message=f"金鑰識別碼 '{kid}' 與公開金鑰指紋或 Profile 不符，應為 '{expected_kid}'",
                            )
                        )

                    # 拒絕全 0 公鑰（非範例檔）
                    if not allow_example:
                        if raw_pub == ZERO_KEY_BYTES or pub_b64 == ZERO_KEY_B64:
                            errors.append(
                                FieldError(
                                    field=f"{field_prefix}.public_key",
                                    message=f"正式登錄檔中禁止使用全 0 範例公鑰 ('{kid}')",
                                )
                            )

    return errors


class Registry:
    """身分與金鑰登錄檔操作類別。"""

    def __init__(self, data: dict):
        self._data = data
        self._profiles: dict[str, dict] = data.get("profiles", {})

    def active_public_keys(self, profile: str) -> dict[str, bytes]:
        """取得指定 profile 目前所有有效 (active) 的 Ed25519 公鑰。

        Args:
            profile: Profile 名稱。

        Returns:
            key_id -> Ed25519 32 位元組 raw 公鑰字典。
        """
        prof = self._profiles.get(profile)
        if not prof:
            return {}

        result: dict[str, bytes] = {}
        for key_info in prof.get("signing_keys", []):
            if key_info.get("status") == "active":
                kid = key_info.get("key_id")
                pub_b64 = key_info.get("public_key")
                if kid and pub_b64:
                    try:
                        raw = base64.b64decode(pub_b64, validate=True)
                        if len(raw) == 32:
                            result[kid] = raw
                    except Exception:
                        pass
        return result

    def inbox_folders(self) -> dict[str, str]:
        """取得收件匣資料夾 ID 與 Profile 的對應表。

        Returns:
            folder_id -> profile 名稱字典。
        """
        result: dict[str, str] = {}
        for prof_name, prof_data in self._profiles.items():
            for fid in prof_data.get("inbox_folder_ids", []):
                result[fid] = prof_name
        return result

    def authorize(
        self, sidecar: dict, public_keys_verified_key_id: str | None
    ) -> tuple[str | None, str | None]:
        """依據 sidecar 與簽章驗證結果進行身分授權。

        檢核邏輯：
        1. sidecar 格式防呆：必須為字典且包含合法之 metadata 與 profile，否則回傳 (None, "格式錯誤")。
        2. 必須通過簽章驗章（public_keys_verified_key_id 非 None）。
        3. sidecar 所宣稱的 profile 必須存在於登錄檔中。
        4. 驗證通過的金鑰 key_id 必須屬於該 profile。
        5. 該金鑰狀態必須為 active（不可為 revoked）。
        6. sidecar.metadata.type 必須在該 profile 之 allowed_types 清單內。

        Args:
            sidecar: 待處理之收件匣 sidecar 字典。
            public_keys_verified_key_id: 由 aistorage.inbox.verify_sidecar_bytes 驗證出的 key_id（失敗為 None）。

        Returns:
            成功時回傳 (producer, None)，其中 producer 為 'profile:<profile名>'；
            失敗時回傳 (None, 拒絕原因字串)。
        """
        if not isinstance(sidecar, dict):
            return None, "格式錯誤"

        profile_name = sidecar.get("profile")
        if not isinstance(profile_name, str) or not profile_name:
            return None, "格式錯誤"

        metadata = sidecar.get("metadata")
        if not isinstance(metadata, dict):
            return None, "格式錯誤"

        item_type = metadata.get("type")
        if not isinstance(item_type, str) or not item_type:
            return None, "格式錯誤"

        if public_keys_verified_key_id is None:
            return None, "未通過簽章驗證（無效簽章或金鑰未匹配）"

        if profile_name not in self._profiles:
            return None, f"Profile '{profile_name}' 未在身分登錄檔中註冊"

        prof_data = self._profiles[profile_name]
        signing_keys = prof_data.get("signing_keys", [])

        # 尋找匹配的 key 項目
        matched_key = None
        for k in signing_keys:
            if k.get("key_id") == public_keys_verified_key_id:
                matched_key = k
                break

        if matched_key is None:
            return None, f"金鑰 '{public_keys_verified_key_id}' 不屬於 profile '{profile_name}'"

        if matched_key.get("status") == "revoked":
            return None, f"金鑰 '{public_keys_verified_key_id}' 已被撤銷"

        if matched_key.get("status") != "active":
            return None, f"金鑰 '{public_keys_verified_key_id}' 狀態非 active"

        allowed_types = prof_data.get("allowed_types", [])
        if item_type not in allowed_types:
            return (
                None,
                f"項目型態 '{item_type}' 未獲 profile '{profile_name}' 授權 (允許型態: {allowed_types})",
            )

        # 授權通過：依規格格式蓋上產生者識別 profile:<name>
        return f"profile:{profile_name}", None


def load_registry(path: str | Path, allow_example: bool = False) -> Registry:
    """載入並驗證身分登錄檔。

    Args:
        path: identity.json 檔案路徑。
        allow_example: 是否放行範例公鑰（預設 False；CLI check 範例檔時明確傳 True）。

    Returns:
        Registry 物件。

    Raises:
        ValueError: 登錄檔格式或內容驗證失敗。
        FileNotFoundError: 找不到指定檔案。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"找不到身分登錄檔: {p}")

    with open(p, encoding="utf-8") as f:
        data = json.load(f)

    errors = validate_registry(data, allow_example=allow_example)
    if errors:
        msg = "; ".join(f"{e.field}: {e.message}" for e in errors)
        raise ValueError(f"身分登錄檔驗證失敗: {msg}")

    return Registry(data)


# ===========================================================================
# CLI 介面
# ===========================================================================

def _cli_keygen(args: argparse.Namespace) -> int:
    """執行 keygen 子命令：產生金鑰對並寫入指定私鑰檔案。"""
    profile = args.profile
    if not _PROFILE_PATTERN.match(profile):
        sys.stderr.write(
            f"錯誤: profile 名稱不合法 ('{profile}')，必須符合小寫連字號格式: ^[a-z][a-z0-9-]*$\n"
        )
        return 1

    out_path = Path(args.out).resolve()

    if out_path.exists():
        sys.stderr.write(f"錯誤: 私鑰檔案已存在，拒絕覆寫: {out_path}\n")
        return 1

    priv_bytes, pub_bytes = generate_keypair()

    out_dir = out_path.parent
    if not out_dir.exists():
        out_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    else:
        # 既有目錄：檢查權限是否過寬（只看 owner/group/other 位元）
        dir_mode = out_dir.stat().st_mode & 0o777
        if dir_mode & 0o077:
            sys.stderr.write(
                f"錯誤: 私鑰目錄 '{out_dir}' 權限過寬 ({oct(dir_mode)})，"
                f"請先手動執行 chmod 700 '{out_dir}' 再重試\n"
            )
            return 1

    # 以 0600 權限建立檔案，避免私鑰外洩
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(fd, "wb") as f:
        f.write(priv_bytes)

    # 確保權限嚴格為 600
    out_path.chmod(0o600)

    fingerprint = hashlib.sha256(pub_bytes).hexdigest()[:8]
    key_id = f"{profile}-{fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    # 嚴格遵循規格：僅輸出 key_id 與 base64 公鑰，絕不印出私鑰內容
    print(f"key_id: {key_id}")
    print(f"public_key: {pub_b64}")
    return 0


def _cli_check(args: argparse.Namespace) -> int:
    """執行 check 子命令：檢驗指定的登錄檔格式與內容。"""
    path = Path(args.path)
    allow_example = path.name == "identity.example.json" or path.name.endswith(".example.json")
    try:
        reg = load_registry(path, allow_example=allow_example)
        print(f"登錄檔驗證通過: {path}")
        print(f"註冊之 Profile 數量: {len(reg._profiles)}")
        for name, data in reg._profiles.items():
            active_count = len(reg.active_public_keys(name))
            inbox_count = len(data.get("inbox_folder_ids", []))
            print(f"  - {name}: {active_count} active key(s), {inbox_count} inbox folder(s)")
        return 0
    except Exception as e:
        sys.stderr.write(f"登錄檔檢核失敗: {e}\n")
        return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。"""
    parser = argparse.ArgumentParser(
        prog="aistorage.identity", description="AiStorage 身分與簽章金鑰管理工具"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # keygen
    p_keygen = subparsers.add_parser("keygen", help="產生 Ed25519 簽章金鑰對")
    p_keygen.add_argument("--profile", required=True, help="Profile 名稱")
    p_keygen.add_argument("--out", required=True, help="私鑰儲存檔案路徑 (權限 600)")
    p_keygen.set_defaults(func=_cli_keygen)

    # check
    p_check = subparsers.add_parser("check", help="驗證身分登錄檔有效性")
    p_check.add_argument("path", help="登錄檔路徑 (例如 config/identity.json)")
    p_check.set_defaults(func=_cli_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
