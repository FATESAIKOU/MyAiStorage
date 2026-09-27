"""AiStorage 收件匣項目格式、sidecar 驗證與 Ed25519 分離式簽章模組。

依據規格：
- g2-api-2.2.md、g2-2.2-fixes.md 與 review-2.2.md
- schemas/inbox-sidecar.schema.json
- design D2（收件匣項目與不可分單位）、D3（產生者章與 profile 綁定）、D4、D10
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, BinaryIO

from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft202012Validator

from aistorage.schema import (
    FieldError,
    _create_format_checker,
    _locate_schema_file,
    validate_inbox_metadata,
)

INBOX_SIG_PREFIX = b"aistorage.inbox/v1\n"
DEFAULT_MAX_RAW_SIZE = 104_857_600  # 100 MiB

_FORMAT_CHECKER = _create_format_checker()
_SIDECAR_SCHEMA_PATH = _locate_schema_file("inbox-sidecar.schema.json")

with open(_SIDECAR_SCHEMA_PATH, encoding="utf-8") as _f:
    _SIDECAR_SCHEMA = json.load(_f)

_SIDECAR_VALIDATOR = Draft202012Validator(
    _SIDECAR_SCHEMA, format_checker=_FORMAT_CHECKER
)

_REQUIRED_PATTERN = re.compile(r"'([^']+)' is a required property")


def sign_sidecar_bytes(sidecar_bytes: bytes, private_key: bytes, key_id: str) -> dict:
    """使用 Ed25519 私鑰對 sidecar 原始位元組進行分離式簽章。

    簽章涵蓋之位元組為: b"aistorage.inbox/v1\\n" + sidecar_bytes。

    Args:
        sidecar_bytes: sidecar 檔案的原始位元組
        private_key: 32 位元組 Ed25519 私鑰原始位元組 (raw bytes)
        key_id: 簽章金鑰識別碼

    Returns:
        簽章字典物件: {"alg": "ed25519", "key_id": key_id, "value": "<base64>"}
    """
    if not isinstance(sidecar_bytes, (bytes, bytearray)):
        raise TypeError("sidecar_bytes 必須是 bytes")
    if not isinstance(private_key, (bytes, bytearray)) or len(private_key) != 32:
        raise ValueError("private_key 必須是 32 位元組的 bytes")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ValueError("key_id 必須是非空字串")

    payload = INBOX_SIG_PREFIX + bytes(sidecar_bytes)
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(private_key))
    sig_bytes = priv.sign(payload)
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

    return {
        "alg": "ed25519",
        "key_id": key_id,
        "value": sig_b64,
    }


def verify_sidecar_bytes(
    sidecar_bytes: bytes, sig: dict, public_keys: dict[str, bytes]
) -> str | None:
    """驗證 sidecar 檔案原始位元組之分離式 Ed25519 簽章。

    簽章涵蓋之位元組為: b"aistorage.inbox/v1\\n" + sidecar_bytes。

    Args:
        sidecar_bytes: sidecar 檔案的原始位元組
        sig: 簽章字典物件（包含 alg, key_id, value）
        public_keys: key_id -> Ed25519 公開金鑰（32 位元組 raw bytes）的映射表

    Returns:
        驗章成功回傳 key_id，失敗或無效時回傳 None。
    """
    if (
        not isinstance(sidecar_bytes, (bytes, bytearray))
        or not isinstance(sig, dict)
        or not isinstance(public_keys, dict)
    ):
        return None

    if sig.get("alg") != "ed25519":
        return None

    key_id = sig.get("key_id")
    if not isinstance(key_id, str) or key_id not in public_keys:
        return None

    sig_b64 = sig.get("value")
    if not isinstance(sig_b64, str):
        return None

    try:
        sig_bytes = base64.b64decode(sig_b64, validate=True)
    except Exception:
        return None

    if len(sig_bytes) != 64:
        return None

    pub_bytes = public_keys.get(key_id)
    if not isinstance(pub_bytes, (bytes, bytearray)) or len(pub_bytes) != 32:
        return None

    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes(pub_bytes))
        payload = INBOX_SIG_PREFIX + bytes(sidecar_bytes)
        pub.verify(sig_bytes, payload)
        return key_id
    except Exception:
        return None



def validate_sidecar(
    sidecar: dict, expected_item_key: str | None = None
) -> list[FieldError]:
    """驗證 sidecar 的形狀與必填欄位（含 2.1 metadata）。

    Args:
        sidecar: 待驗證的 sidecar 字典
        expected_item_key: 若提供，核對 sidecar 內的 item_key 是否等於預期值

    Returns:
        FieldError 清單；空清單表示驗證通過。
    """
    if not isinstance(sidecar, dict):
        return [FieldError(field="", message="sidecar 必須是字典 (dict)")]

    errors: list[FieldError] = []
    seen_keys: set[tuple[str, str]] = set()

    def add_err(f: str, msg: str):
        key = (f, msg)
        if key not in seen_keys:
            seen_keys.add(key)
            errors.append(FieldError(field=f, message=msg))

    # 1. 核對 expected_item_key
    if expected_item_key is not None:
        if sidecar.get("item_key") != expected_item_key:
            add_err(
                "item_key",
                f"sidecar item_key ('{sidecar.get('item_key')}') 與預期檔名 key ('{expected_item_key}') 不符",
            )

    # 2. JSON Schema 驗證
    for err in _SIDECAR_VALIDATOR.iter_errors(sidecar):
        path_str = ".".join(str(p) for p in err.path)
        if err.validator == "required":
            m = _REQUIRED_PATTERN.search(err.message)
            missing = m.group(1) if m else ""
            field_name = f"{path_str}.{missing}" if path_str else missing
            msg = f"缺少必填欄位: {missing}"
            add_err(field_name, msg)
            if missing and missing != field_name:
                add_err(missing, msg)
        else:
            field_name = path_str or (err.path[-1] if err.path else "")
            add_err(str(field_name), err.message)

    # 3. 驗證 2.1 metadata
    metadata = sidecar.get("metadata")
    if isinstance(metadata, dict):
        for meta_err in validate_inbox_metadata(metadata):
            msg = meta_err.message
            add_err(meta_err.field, msg)
            add_err(f"metadata.{meta_err.field}", msg)

        # 4. Session 額外約束：metadata.id 必須完全等於 <source>:<source_session_id>
        if metadata.get("type") == "session":
            session_info = sidecar.get("session")
            if isinstance(session_info, dict):
                src = session_info.get("source")
                src_sess_id = session_info.get("source_session_id")
                expected_id = f"{src}:{src_sess_id}"
                actual_id = metadata.get("id")
                if actual_id != expected_id:
                    msg = f"Session id ('{actual_id}') 必須等於 <source>:<source_session_id> ('{expected_id}')"
                    add_err("id", msg)
                    add_err("metadata.id", msg)

    # 5. status=stopped 時 stopped_at 必填 RFC 3339 字串
    session_info = sidecar.get("session")
    if isinstance(session_info, dict) and session_info.get("status") == "stopped":
        stopped_at = session_info.get("stopped_at")
        if (
            stopped_at is None
            or not isinstance(stopped_at, str)
            or not stopped_at.strip()
        ):
            msg = "Session 狀態為 stopped 時，stopped_at 必填且不得為空"
            add_err("session.stopped_at", msg)
            add_err("stopped_at", msg)

    return errors


def check_raw(
    sidecar: dict,
    raw: bytes | BinaryIO | Any | None,
    max_size: int = DEFAULT_MAX_RAW_SIZE,
) -> list[FieldError]:
    """檢查 raw 本體與 sidecar 的一致性（sha256 與 size 是否相符、上限與缺少檢查）。

    支援串流雜湊計算（接受 bytes 或具備 read 方法的二進位檔案物件）。

    規則：
    - session、rewrite、以及 kind='contained' 的 artifact 必須有 raw。
    - handoff、claim、reference、以及 kind='link' 的 artifact 不應有 raw（raw 為 None）。
    - session、rewrite、contained artifact 的 raw 大小上限預設為 100 MiB (104,857,600 位元組)。
    - 若 sidecar 載明 raw，其 sha256（小寫 hex）與 size 必須與實際 raw 完全吻合。
    """
    if not isinstance(sidecar, dict):
        return [FieldError(field="", message="sidecar 必須是字典 (dict)")]

    errors: list[FieldError] = []
    metadata = sidecar.get("metadata", {})
    item_type = metadata.get("type") if isinstance(metadata, dict) else None
    body = sidecar.get("body", {})

    # 判斷是否需要 raw 本體
    needs_raw = False
    if item_type in ("session", "rewrite"):
        needs_raw = True
    elif item_type == "artifact":
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind == "contained":
            needs_raw = True
        elif kind == "link":
            needs_raw = False
    elif item_type in ("handoff", "claim", "reference"):
        needs_raw = False
    else:
        if sidecar.get("raw") is not None:
            needs_raw = True

    raw_meta = sidecar.get("raw")

    # 情況 A：未提供 raw 本體
    if raw is None:
        if needs_raw:
            errors.append(FieldError(field="raw", message="該項目型態需要 raw 本體，但未提供 raw"))
        elif raw_meta is not None:
            errors.append(FieldError(field="raw", message="sidecar 載明了 raw metadata，但未提供 raw 本體"))
        return errors

    # 情況 B：提供了 raw 本體
    if not needs_raw and item_type in ("handoff", "claim", "reference"):
        errors.append(FieldError(field="raw", message="該項目型態不應包含 raw 本體，但傳入了 raw"))

    # 串流讀取計算 size 與 sha256
    hasher = hashlib.sha256()
    actual_size = 0
    exceeded = False

    if isinstance(raw, (bytes, bytearray)):
        actual_size = len(raw)
        if actual_size > max_size:
            exceeded = True
        hasher.update(raw)
    elif hasattr(raw, "read"):
        chunk_size = 65536
        while True:
            chunk = raw.read(chunk_size)
            if not chunk:
                break
            actual_size += len(chunk)
            hasher.update(chunk)
            # 累積超過上限立即停止讀取
            if actual_size > max_size:
                exceeded = True
                break
    else:
        errors.append(FieldError(field="raw", message="raw 本體必須是 bytes 或檔案物件 (file-like)"))
        return errors

    # 超過上限時直接報錯，不再比對 sha256（串流已截斷）
    if exceeded:
        msg = f"raw 大小超過 {max_size} 位元組上限（已讀 {actual_size} 位元組後停止）"
        errors.append(FieldError(field="raw.size", message=msg))
        errors.append(FieldError(field="size", message=msg))
        return errors

    actual_sha256 = hasher.hexdigest().lower()

    if raw_meta is None or not isinstance(raw_meta, dict):
        errors.append(FieldError(field="raw", message="提供 raw 本體時，sidecar 必須包含 raw 描述物件"))
    else:
        expected_size = raw_meta.get("size")
        if expected_size is None or expected_size != actual_size:
            msg = f"raw 大小不符：預期 {expected_size}，實際 {actual_size}"
            errors.append(FieldError(field="raw.size", message=msg))
            errors.append(FieldError(field="size", message=msg))

        expected_sha256 = raw_meta.get("sha256")
        if expected_sha256 is None or expected_sha256.lower() != actual_sha256:
            msg = f"raw sha256 不符：預期 {expected_sha256}，實際 {actual_sha256}"
            errors.append(FieldError(field="raw.sha256", message=msg))
            errors.append(FieldError(field="sha256", message=msg))

    return errors


def is_complete(
    names: set[str], item_key: str, sidecar: dict | None = None
) -> bool:
    """判斷收件匣項目是否完整（必須有 .sig 與 .sidecar.json，需要時亦需 .raw）。

    依據規格：
    - 沒有 .sig 就回傳 False。
    - 沒有 .sidecar.json 就回傳 False。
    - 若 sidecar 載明需要 raw（或型態需要），則亦必須有 .raw。
    - 純以傳入之 names、item_key、sidecar 判定，不讀取任何本地檔案。
    """
    if not isinstance(names, (set, list, tuple)) or not isinstance(item_key, str):
        return False

    target_sig = f"{item_key}.sig"
    target_sidecar = f"{item_key}.sidecar.json"
    target_raw = f"{item_key}.raw"

    # 1. 檢查 .sig
    has_sig = any(os.path.basename(n) == target_sig or n == target_sig for n in names)
    if not has_sig:
        return False

    # 2. 檢查 .sidecar.json
    has_sidecar = any(
        os.path.basename(n) == target_sidecar or n == target_sidecar for n in names
    )
    if not has_sidecar:
        return False

    # 3. 檢查 .raw（若 sidecar 載明需要）
    if sidecar is not None and sidecar.get("raw") is not None:
        has_raw = any(
            os.path.basename(n) == target_raw or n == target_raw for n in names
        )
        if not has_raw:
            return False

    return True
