"""tests/unit/test_inbox.py

tasks 2.2 單元測試：收件匣項目格式、Ed25519 分離式簽章與驗章、黃金向量、Sidecar 驗證與完整性判定。
依據：
- g2-2.2-fixes.md、g2-api-2.2.md、review-2.2.md
- schemas/inbox-sidecar.schema.json
- design.md D2「收件匣項目的處理」、D3「產生者章」
- specs/common/identity 的簽章情境
- specs/agora/session-sync
"""

import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from aistorage.inbox import (
    check_raw,
    is_complete,
    sign_sidecar_bytes,
    validate_sidecar,
    verify_sidecar_bytes,
)
from aistorage.schema import FieldError

GOLDEN_PATH = Path(__file__).parent / "data" / "inbox-golden.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ed25519_keypair():
    """在測試內使用 cryptography 產生標準 Ed25519 金鑰對（32 bytes raw）。"""
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


@pytest.fixture
def base_session_sidecar():
    """提供一筆合法的 session 類型 sidecar 字典（不含 signature）。"""
    raw_content = b'{"mock": "session-raw-data"}'
    raw_sha = hashlib.sha256(raw_content).hexdigest().lower()
    raw_size = len(raw_content)

    return {
        "format": "aistorage.inbox/v1",
        "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "profile": "mac-opencode",
        "metadata": {
            "id": "opencode:ses_12345",
            "type": "session",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": "case-test",
            "provenance": None,
        },
        "raw": {
            "sha256": raw_sha,
            "size": raw_size,
        },
        "session": {
            "source": "opencode",
            "source_session_id": "ses_12345",
            "snapshot_at": "2026-09-27T08:00:00Z",
            "status": "running",
            "stopped_at": None,
            "in_progress": False,
            "parent_id": None,
        },
        "body": {},
    }


# ===========================================================================
# 1. 黃金測試向量 (Golden Vector)
# ===========================================================================

class TestGoldenVector:
    """讀取固定黃金向量資料檔，驗證分離式簽章與驗章之一致性。"""

    def test_golden_vector_signature_value(self):
        """依黃金向量之私鑰與 sidecar 位元組，產生之簽章值必須與 expected_sig 完全相同。"""
        assert GOLDEN_PATH.is_file(), f"找不到黃金向量資料檔: {GOLDEN_PATH}"
        data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        priv_bytes = bytes.fromhex(data["private_key_hex"])
        sidecar_bytes = data["sidecar_bytes_utf8"].encode("utf-8")
        key_id = data["key_id"]
        expected_sig = data["expected_sig"]

        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)
        assert sig == expected_sig
        assert sig["alg"] == "ed25519"
        assert sig["key_id"] == key_id
        assert sig["value"] == expected_sig["value"]

    def test_golden_vector_verification(self):
        """黃金向量之公鑰能正確驗證 expected_sig，並回傳匹配的 key_id。"""
        assert GOLDEN_PATH.is_file(), f"找不到黃金向量資料檔: {GOLDEN_PATH}"
        data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        pub_bytes = base64.b64decode(data["public_key_b64"], validate=True)
        sidecar_bytes = data["sidecar_bytes_utf8"].encode("utf-8")
        key_id = data["key_id"]
        expected_sig = data["expected_sig"]

        verified_key_id = verify_sidecar_bytes(
            sidecar_bytes, expected_sig, {key_id: pub_bytes}
        )
        assert verified_key_id == key_id

    def test_golden_vector_public_key_derivation(self):
        """黃金向量私鑰衍生之 Ed25519 公鑰必須與 public_key_b64 一致。"""
        assert GOLDEN_PATH.is_file()
        data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        priv_bytes = bytes.fromhex(data["private_key_hex"])
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
        derived_pub_bytes = priv.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        derived_b64 = base64.b64encode(derived_pub_bytes).decode("ascii")
        assert derived_b64 == data["public_key_b64"]

    def test_golden_negative_no_prefix_fails(self):
        """負向黃金向量：少了用途前綴的簽章值，verify_sidecar_bytes 必須失敗。"""
        assert GOLDEN_PATH.is_file()
        data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        pub_bytes = base64.b64decode(data["public_key_b64"], validate=True)
        sidecar_bytes = data["sidecar_bytes_utf8"].encode("utf-8")
        neg = data["negative_no_prefix"]
        neg_sig = neg["sig"]
        key_id = neg_sig["key_id"]

        result = verify_sidecar_bytes(
            sidecar_bytes, neg_sig, {key_id: pub_bytes}
        )
        assert result is None, "少了用途前綴之簽章不可通過驗證"


# ===========================================================================
# 2. 用途前綴 (Purpose Prefix)
# ===========================================================================

class TestPurposePrefix:
    """簽章涵蓋之位元組必須為 b'aistorage.inbox/v1\\n' + sidecar_bytes。"""

    def test_missing_purpose_prefix_fails_verification(self, ed25519_keypair):
        """直接對 sidecar 位元組簽章（少了用途前綴）時，verify_sidecar_bytes 必須失敗。"""
        priv_bytes, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"format":"aistorage.inbox/v1"}'
        key_id = "test-key-01"

        # 直接簽 sidecar_bytes，不加前綴
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
        raw_sig = priv.sign(sidecar_bytes)
        sig = {
            "alg": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(raw_sig).decode("ascii"),
        }

        # 驗章應失敗
        result = verify_sidecar_bytes(sidecar_bytes, sig, {key_id: pub_bytes})
        assert result is None, "未帶 aistorage.inbox/v1\\n 前綴之簽章不可通過驗證"

    def test_wrong_purpose_prefix_fails_verification(self, ed25519_keypair):
        """帶有錯誤用途前綴（例如 aistorage.other/v1\\n）時驗章失敗。"""
        priv_bytes, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"format":"aistorage.inbox/v1"}'
        key_id = "test-key-01"

        priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
        payload = b"aistorage.other/v1\n" + sidecar_bytes
        raw_sig = priv.sign(payload)
        sig = {
            "alg": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(raw_sig).decode("ascii"),
        }

        result = verify_sidecar_bytes(sidecar_bytes, sig, {key_id: pub_bytes})
        assert result is None, "錯誤用途前綴之簽章不可通過驗證"

    def test_correct_purpose_prefix_passes(self, ed25519_keypair):
        """正常使用 sign_sidecar_bytes 簽章，verify_sidecar_bytes 成功通過。"""
        priv_bytes, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"format":"aistorage.inbox/v1"}'
        key_id = "test-key-01"

        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)
        assert verify_sidecar_bytes(sidecar_bytes, sig, {key_id: pub_bytes}) == key_id


# ===========================================================================
# 3. 竄改 sidecar 原始位元組 (Byte Sensitivity)
# ===========================================================================

class TestTamperSidecarBytes:
    """分離式簽章簽署檔案的原始位元組：竄改任何一個位元組都必須驗章失敗。"""

    def test_tamper_single_byte_fails(self, ed25519_keypair):
        """竄改 sidecar 中任意單一位元組，驗章必定失敗。"""
        priv_bytes, pub_bytes = ed25519_keypair
        key_id = "mac-opencode-2026-09"
        sidecar_bytes = b'{"format":"aistorage.inbox/v1","item_key":"01ARZ3NDEKTSV4RRFFQ69G5FAV"}'

        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)

        # 逐一測試竄改第 0 位元組、中間位元組、末尾位元組
        tampered_1 = bytearray(sidecar_bytes)
        tampered_1[0] = ord(b"[")
        assert verify_sidecar_bytes(bytes(tampered_1), sig, {key_id: pub_bytes}) is None

        tampered_2 = bytearray(sidecar_bytes)
        tampered_2[10] ^= 0x01
        assert verify_sidecar_bytes(bytes(tampered_2), sig, {key_id: pub_bytes}) is None

        tampered_3 = bytearray(sidecar_bytes)
        tampered_3[-1] = ord(b"]")
        assert verify_sidecar_bytes(bytes(tampered_3), sig, {key_id: pub_bytes}) is None

    def test_tamper_appending_whitespace_or_newline_fails(self, ed25519_keypair):
        """檔案末尾新增空白或換行符號改變原始位元組，驗章必須失敗。"""
        priv_bytes, pub_bytes = ed25519_keypair
        key_id = "mac-opencode-2026-09"
        sidecar_bytes = b'{"format":"aistorage.inbox/v1"}'

        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)

        assert verify_sidecar_bytes(sidecar_bytes + b"\n", sig, {key_id: pub_bytes}) is None
        assert verify_sidecar_bytes(sidecar_bytes + b" ", sig, {key_id: pub_bytes}) is None

    def test_tamper_reordering_json_keys_fails(self, ed25519_keypair):
        """更改 JSON 原始字串中的鍵順序（語意相同但位元組不同）驗章必須失敗。"""
        priv_bytes, pub_bytes = ed25519_keypair
        key_id = "mac-opencode-2026-09"

        bytes_order_1 = b'{"a":1,"b":2}'
        bytes_order_2 = b'{"b":2,"a":1}'

        sig = sign_sidecar_bytes(bytes_order_1, priv_bytes, key_id)
        assert verify_sidecar_bytes(bytes_order_2, sig, {key_id: pub_bytes}) is None

    def test_tamper_critical_fields_in_json(self, base_session_sidecar, ed25519_keypair):
        """竄改 raw.sha256、raw.size、profile 等重要欄位均導致驗章失敗。"""
        priv_bytes, pub_bytes = ed25519_keypair
        key_id = "mac-opencode-2026-09"

        original_bytes = json.dumps(base_session_sidecar).encode("utf-8")
        sig = sign_sidecar_bytes(original_bytes, priv_bytes, key_id)

        # 竄改 raw.sha256
        data_mod = copy.deepcopy(base_session_sidecar)
        data_mod["raw"]["sha256"] = "0" * 64
        assert verify_sidecar_bytes(json.dumps(data_mod).encode("utf-8"), sig, {key_id: pub_bytes}) is None

        # 竄改 raw.size
        data_mod2 = copy.deepcopy(base_session_sidecar)
        data_mod2["raw"]["size"] += 1
        assert verify_sidecar_bytes(json.dumps(data_mod2).encode("utf-8"), sig, {key_id: pub_bytes}) is None

        # 竄改 profile
        data_mod3 = copy.deepcopy(base_session_sidecar)
        data_mod3["profile"] = "worker/attacker"
        assert verify_sidecar_bytes(json.dumps(data_mod3).encode("utf-8"), sig, {key_id: pub_bytes}) is None


# ===========================================================================
# 4. 簽章演算法與格式邊界測試
# ===========================================================================

class TestSignAndVerifyEdgeCases:
    """測試 alg 非 ed25519、長度異常、格式損壞等負向案例。"""

    def test_unsupported_alg_rejected(self, ed25519_keypair):
        """alg 非 ed25519（如 rsa、secp256k1）時 verify_sidecar_bytes 回傳 None。"""
        priv_bytes, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"msg":"hello"}'
        key_id = "key-01"

        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)
        for bad_alg in ["rsa", "secp256k1", "ed25519-ph", "none", ""]:
            sig_bad = dict(sig, alg=bad_alg)
            assert verify_sidecar_bytes(sidecar_bytes, sig_bad, {key_id: pub_bytes}) is None

    def test_invalid_signature_length_rejected(self, ed25519_keypair):
        """簽章解碼後長度非 64 位元組時 verify_sidecar_bytes 回傳 None。"""
        priv_bytes, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"msg":"hello"}'
        key_id = "key-01"

        # 32 位元組
        short_sig = base64.b64encode(b"a" * 32).decode("ascii")
        assert verify_sidecar_bytes(
            sidecar_bytes, {"alg": "ed25519", "key_id": key_id, "value": short_sig}, {key_id: pub_bytes}
        ) is None

        # 65 位元組
        long_sig = base64.b64encode(b"a" * 65).decode("ascii")
        assert verify_sidecar_bytes(
            sidecar_bytes, {"alg": "ed25519", "key_id": key_id, "value": long_sig}, {key_id: pub_bytes}
        ) is None

    def test_corrupted_base64_in_sig_rejected(self, ed25519_keypair):
        """sig.value 為非法 base64 時回傳 None。"""
        _, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"msg":"hello"}'
        key_id = "key-01"

        bad_sig = {"alg": "ed25519", "key_id": key_id, "value": "!!!not-valid-base64!!!"}
        assert verify_sidecar_bytes(sidecar_bytes, bad_sig, {key_id: pub_bytes}) is None

    def test_unknown_or_missing_key_id_rejected(self, ed25519_keypair):
        """key_id 不在 public_keys 中或為空時回傳 None。"""
        priv_bytes, pub_bytes = ed25519_keypair
        sidecar_bytes = b'{"msg":"hello"}'

        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, "unknown-key")
        assert verify_sidecar_bytes(sidecar_bytes, sig, {"registered-key": pub_bytes}) is None
        assert verify_sidecar_bytes(sidecar_bytes, sig, {}) is None

    def test_wrong_public_key_rejected(self, ed25519_keypair):
        """公鑰不匹配時回傳 None。"""
        priv_bytes, _ = ed25519_keypair
        other_priv = ed25519.Ed25519PrivateKey.generate()
        other_pub_bytes = other_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        sidecar_bytes = b'{"msg":"hello"}'
        key_id = "key-01"
        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)

        assert verify_sidecar_bytes(sidecar_bytes, sig, {key_id: other_pub_bytes}) is None

    def test_invalid_public_key_bytes_rejected(self, ed25519_keypair):
        """提供之公鑰長度非 32 位元組時回傳 None。"""
        priv_bytes, _ = ed25519_keypair
        sidecar_bytes = b'{"msg":"hello"}'
        key_id = "key-01"
        sig = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id)

        assert verify_sidecar_bytes(sidecar_bytes, sig, {key_id: b"too-short"}) is None
        assert verify_sidecar_bytes(sidecar_bytes, sig, {key_id: b"a" * 64}) is None

    def test_sign_sidecar_bytes_argument_validation(self, ed25519_keypair):
        """sign_sidecar_bytes 參數型態檢驗。"""
        priv_bytes, _ = ed25519_keypair

        with pytest.raises(TypeError):
            sign_sidecar_bytes("not bytes", priv_bytes, "k1")  # type: ignore

        with pytest.raises(ValueError):
            sign_sidecar_bytes(b"data", b"invalid-len", "k1")

        with pytest.raises(ValueError):
            sign_sidecar_bytes(b"data", priv_bytes, "")


# ===========================================================================
# 5. validate_sidecar 檔名與 expected_item_key 比對
# ===========================================================================

class TestValidateSidecarExpectedItemKey:
    """validate_sidecar 可傳入 expected_item_key 檢查檔名與內文一致性（防重放 H3）。"""

    def test_expected_item_key_matches(self, base_session_sidecar):
        """sidecar.item_key 與 expected_item_key 相符時通過。"""
        errors = validate_sidecar(
            base_session_sidecar, expected_item_key=base_session_sidecar["item_key"]
        )
        assert errors == []

    def test_expected_item_key_mismatch_fails(self, base_session_sidecar):
        """sidecar.item_key 與 expected_item_key 不符時回報錯誤。"""
        errors = validate_sidecar(
            base_session_sidecar, expected_item_key="01OTHERKEY0000000000000000"
        )
        assert len(errors) >= 1
        err_fields = [e.field for e in errors]
        assert any("item_key" in f for f in err_fields)

    def test_expected_item_key_none_ignored(self, base_session_sidecar):
        """expected_item_key 為 None 時不核對。"""
        errors = validate_sidecar(base_session_sidecar, expected_item_key=None)
        assert errors == []


# ===========================================================================
# 6. Session 特別規則：id 一致性、parent_id 形狀、stopped 規則
# ===========================================================================

class TestValidateSessionSpecialRules:
    """session 的 metadata.id 必須等於 <source>:<source_session_id>；parent_id 格式；stopped_at。"""

    def test_session_id_matches_source_and_source_session_id(self, base_session_sidecar):
        """metadata.id 等於 <source>:<source_session_id> 時通過。"""
        assert validate_sidecar(base_session_sidecar) == []

    def test_session_id_mismatch_fails(self, base_session_sidecar):
        """metadata.id 與 source:source_session_id 不一致時報錯。"""
        data = copy.deepcopy(base_session_sidecar)
        data["metadata"]["id"] = "opencode:ses_mismatched"
        data["session"]["source_session_id"] = "ses_original"

        errors = validate_sidecar(data)
        assert len(errors) >= 1
        err_fields = [e.field for e in errors]
        assert any("id" in f for f in err_fields)

    @pytest.mark.parametrize("bad_source", [
        "session", "handoff", "claim", "reference", "rewrite", "artifact", "open:code", "open code"
    ])
    def test_session_source_cannot_be_reserved_or_invalid(self, base_session_sidecar, bad_source):
        """session.source 不得為保留字，不得含冒號或空白。"""
        data = copy.deepcopy(base_session_sidecar)
        data["session"]["source"] = bad_source
        data["metadata"]["id"] = f"{bad_source}:{data['session']['source_session_id']}"
        errors = validate_sidecar(data)
        assert len(errors) >= 1

    def test_session_parent_id_shape(self, base_session_sidecar):
        """parent_id 為 None 或完整 <source>:<id> 通過；非法形狀報錯。"""
        data = copy.deepcopy(base_session_sidecar)
        data["session"]["parent_id"] = None
        assert validate_sidecar(data) == []

        data["session"]["parent_id"] = "opencode:ses_parent_01"
        assert validate_sidecar(data) == []

        # 非法 parent_id：缺少 source 前綴
        data["session"]["parent_id"] = "pure_parent_id"
        errors = validate_sidecar(data)
        assert len(errors) >= 1

    def test_session_stopped_must_have_stopped_at(self, base_session_sidecar):
        """status=stopped 且 stopped_at=None 時必須報錯。"""
        data = copy.deepcopy(base_session_sidecar)
        data["session"]["status"] = "stopped"
        data["session"]["stopped_at"] = None

        errors = validate_sidecar(data)
        assert len(errors) >= 1
        assert any("stopped_at" in e.field for e in errors)

        data["session"]["stopped_at"] = "2026-09-27T08:30:00Z"
        assert validate_sidecar(data) == []


# ===========================================================================
# 7. raw.sha256 僅允許小寫 Hex
# ===========================================================================

class TestRawSha256Hex:
    """raw.sha256 必須嚴格為 64 字元小寫 hex。"""

    def test_uppercase_hex_rejected(self, base_session_sidecar):
        """raw.sha256 含有大寫字母時 validate_sidecar 報錯。"""
        data = copy.deepcopy(base_session_sidecar)
        data["raw"]["sha256"] = data["raw"]["sha256"].upper()

        errors = validate_sidecar(data)
        assert len(errors) >= 1
        assert any(
            "sha256" in e.field or "raw" in e.field or "sha256" in e.message
            for e in errors
        )

    def test_lowercase_hex_accepted(self, base_session_sidecar):
        """raw.sha256 為 64 位小寫 hex 時通過。"""
        data = copy.deepcopy(base_session_sidecar)
        data["raw"]["sha256"] = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert validate_sidecar(data) == []


# ===========================================================================
# 8. 各型態 body 的負向案例
# ===========================================================================

class TestNegativeBodyCases:
    """各 type 之 body 必填與合法性負向檢核。"""

    def test_claim_missing_fields(self):
        """claim 缺少 handoff_id 或 claimer_session_id 時報錯。"""
        base_claim = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAC",
            "profile": "mac-opencode",
            "metadata": {
                "id": "claim:01ARZ3NDEKTSV4RRFFQ69G5FAC",
                "type": "claim",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": None,
            "body": {
                "handoff_id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAH",
                "claimer_session_id": "opencode:ses_claimer",
            },
        }
        assert validate_sidecar(base_claim) == []

        # 缺 handoff_id
        c1 = copy.deepcopy(base_claim)
        del c1["body"]["handoff_id"]
        errors = validate_sidecar(c1)
        assert len(errors) >= 1
        assert any("handoff_id" in e.field for e in errors)

        # 缺 claimer_session_id
        c2 = copy.deepcopy(base_claim)
        del c2["body"]["claimer_session_id"]
        errors = validate_sidecar(c2)
        assert len(errors) >= 1
        assert any("claimer_session_id" in e.field for e in errors)

    def test_reference_missing_fields(self):
        """reference 缺少 from_session_id, to_session_id, read_snapshot_at 時報錯。"""
        base_ref = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAR",
            "profile": "mac-opencode",
            "metadata": {
                "id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FAR",
                "type": "reference",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": None,
            "body": {
                "from_session_id": "opencode:ses_01",
                "to_session_id": "opencode:ses_02",
                "read_snapshot_at": "2026-09-27T07:59:00Z",
            },
        }
        assert validate_sidecar(base_ref) == []

        for field in ["from_session_id", "to_session_id", "read_snapshot_at"]:
            r = copy.deepcopy(base_ref)
            del r["body"][field]
            errors = validate_sidecar(r)
            assert len(errors) >= 1
            assert any(field in e.field for e in errors)

    def test_rewrite_missing_fields(self):
        """rewrite 缺少 target_session_id, base_snapshot_sha256, reason 時報錯。"""
        base_rewrite = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
            "profile": "mac-opencode",
            "metadata": {
                "id": "rewrite:01ARZ3NDEKTSV4RRFFQ69G5FAW",
                "type": "rewrite",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": {
                "sha256": "0" * 64,
                "size": 100,
            },
            "body": {
                "target_session_id": "opencode:ses_01",
                "base_snapshot_sha256": "1" * 64,
                "reason": "遮蔽洩漏資訊",
            },
        }
        assert validate_sidecar(base_rewrite) == []

        for field in ["target_session_id", "base_snapshot_sha256", "reason"]:
            rw = copy.deepcopy(base_rewrite)
            del rw["body"][field]
            errors = validate_sidecar(rw)
            assert len(errors) >= 1
            assert any(field in e.field for e in errors)

        # 缺少 raw
        rw_no_raw = copy.deepcopy(base_rewrite)
        rw_no_raw["raw"] = None
        errors = validate_sidecar(rw_no_raw)
        assert len(errors) >= 1
        assert any("raw" in e.field for e in errors)

    def test_handoff_missing_fields(self):
        """handoff 缺少 continuation 或其子欄位時報錯。"""
        base_handoff = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAH",
            "profile": "mac-opencode",
            "metadata": {
                "id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAH",
                "type": "handoff",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": None,
            "body": {
                "target_session_id": "opencode:ses_02",
                "continuation": {
                    "snapshot_sha256": "a" * 64,
                    "message_id": "msg_01",
                },
                "content": "請接續執行",
            },
        }
        assert validate_sidecar(base_handoff) == []

        # 缺 continuation
        h1 = copy.deepcopy(base_handoff)
        del h1["body"]["continuation"]
        assert len(validate_sidecar(h1)) >= 1

        # continuation 缺 message_id
        h2 = copy.deepcopy(base_handoff)
        del h2["body"]["continuation"]["message_id"]
        assert len(validate_sidecar(h2)) >= 1

        # 缺 content
        h3 = copy.deepcopy(base_handoff)
        del h3["body"]["content"]
        assert len(validate_sidecar(h3)) >= 1

    def test_artifact_negative_cases(self):
        """artifact 的 kind 不合法、link 型缺 link、contained 型缺 raw 或缺 content_type。"""
        base_art = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAA",
            "profile": "mac-opencode",
            "metadata": {
                "id": "artifact:01ARZ3NDEKTSV4RRFFQ69G5FAA",
                "type": "artifact",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": None,
            "body": {
                "kind": "link",
                "link": "https://example.com/doc.pdf",
                "produced_by_session_id": "opencode:ses_01",
                "filename": "doc.pdf",
                "content_type": "application/pdf",
                "repo": "org/repo",
                "path": "docs/doc.pdf",
            },
        }
        assert validate_sidecar(base_art) == []

        # kind 不合法
        a_bad_kind = copy.deepcopy(base_art)
        a_bad_kind["body"]["kind"] = "unsupported_kind"
        assert len(validate_sidecar(a_bad_kind)) >= 1

        # link 型沒有 link
        a_no_link = copy.deepcopy(base_art)
        del a_no_link["body"]["link"]
        assert len(validate_sidecar(a_no_link)) >= 1

        # contained 型缺少 content_type
        a_contained = copy.deepcopy(base_art)
        a_contained["body"]["kind"] = "contained"
        a_contained["raw"] = {"sha256": "0" * 64, "size": 100}
        del a_contained["body"]["content_type"]
        assert len(validate_sidecar(a_contained)) >= 1

        # contained 型缺少 raw
        a_contained_no_raw = copy.deepcopy(base_art)
        a_contained_no_raw["body"]["kind"] = "contained"
        a_contained_no_raw["raw"] = None
        assert len(validate_sidecar(a_contained_no_raw)) >= 1


# ===========================================================================
# 9. 超過上限 (100 MiB / 104,857,600 bytes)
# ===========================================================================

class TestMaxRawSizeLimits:
    """測試本體大小上限：宣告大小與 check_raw 串流檢查。"""

    def test_schema_path_rejects_over_100_mib(self, base_session_sidecar):
        """在 sidecar.raw.size 宣告超過 104,857,600 bytes 時 validate_sidecar 報錯。"""
        # Session
        sess = copy.deepcopy(base_session_sidecar)
        sess["raw"]["size"] = 104_857_601
        errors = validate_sidecar(sess)
        assert len(errors) >= 1
        assert any("size" in e.field or "raw" in e.field for e in errors)

        # 恰好等於 100 MiB 通過
        sess["raw"]["size"] = 104_857_600
        assert validate_sidecar(sess) == []

        # Rewrite
        rw = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
            "profile": "mac-opencode",
            "metadata": {
                "id": "rewrite:01ARZ3NDEKTSV4RRFFQ69G5FAW",
                "type": "rewrite",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": {"sha256": "0" * 64, "size": 104_857_601},
            "body": {
                "target_session_id": "opencode:ses_01",
                "base_snapshot_sha256": "1" * 64,
                "reason": "超過上限",
            },
        }
        assert len(validate_sidecar(rw)) >= 1

        # Contained Artifact
        art = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAA",
            "profile": "mac-opencode",
            "metadata": {
                "id": "artifact:01ARZ3NDEKTSV4RRFFQ69G5FAA",
                "type": "artifact",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
            },
            "raw": {"sha256": "0" * 64, "size": 104_857_601},
            "body": {
                "kind": "contained",
                "content_type": "application/pdf",
                "produced_by_session_id": "opencode:ses_01",
            },
        }
        assert len(validate_sidecar(art)) >= 1

    def test_check_raw_file_object_streaming_and_limit(self, base_session_sidecar):
        """check_raw 支援以二進位檔案物件串流計算雜湊與限制檢查。"""
        raw_bytes = b'{"session": "stream-test"}'
        sidecar = copy.deepcopy(base_session_sidecar)
        sidecar["raw"]["size"] = len(raw_bytes)
        sidecar["raw"]["sha256"] = hashlib.sha256(raw_bytes).hexdigest().lower()

        # 1. 以 BytesIO 正常串流比對通過
        bio = io.BytesIO(raw_bytes)
        assert check_raw(sidecar, bio) == []

        # 2. 測試自訂上限：當 max_size 小於實際大小時報錯
        bio2 = io.BytesIO(raw_bytes)
        errors = check_raw(sidecar, bio2, max_size=len(raw_bytes) - 1)
        assert len(errors) >= 1
        assert any("size" in e.field for e in errors)

        # 3. 虛擬大檔案物件（模擬超過 100 MiB 串流）
        class LargeDummyFile:
            def __init__(self, total_size):
                self.total_size = total_size
                self.read_so_far = 0

            def read(self, chunk_size=65536):
                if self.read_so_far >= self.total_size:
                    return b""
                n = min(chunk_size, self.total_size - self.read_so_far)
                self.read_so_far += n
                return b"0" * n

        dummy_large = LargeDummyFile(104_857_601)
        sidecar_large = copy.deepcopy(base_session_sidecar)
        sidecar_large["raw"]["size"] = 104_857_601
        sidecar_large["raw"]["sha256"] = hashlib.sha256(b"0" * 104_857_601).hexdigest().lower()

        errors = check_raw(sidecar_large, dummy_large)
        assert len(errors) >= 1
        assert any("size" in e.field for e in errors)
        assert dummy_large.read_so_far <= 104_857_600 + 65536

        # 4. 超大檔案 (10 GiB) 串流：必須在累積超過上限時立刻中斷，不能全部讀完
        dummy_huge = LargeDummyFile(10 * 1024 * 1024 * 1024)
        errors_huge = check_raw(sidecar_large, dummy_huge, max_size=104_857_600)
        assert len(errors_huge) >= 1
        assert any("size" in e.field for e in errors_huge)
        assert dummy_huge.read_so_far <= 104_857_600 + 65536


# ===========================================================================
# 10. check_raw 雜湊、大小與必要性案例
# ===========================================================================

class TestCheckRawCases:
    """測試 check_raw 對不同型態與雜湊不符之檢核。"""

    def test_check_raw_session_requires_raw(self, base_session_sidecar):
        """session 必須提供 raw；raw=None 報錯。"""
        errors = check_raw(base_session_sidecar, None)
        assert len(errors) >= 1
        assert any("raw" in e.field for e in errors)

    def test_check_raw_sha256_mismatch(self, base_session_sidecar):
        """raw 內容與 sidecar sha256 不符時報錯。"""
        errors = check_raw(base_session_sidecar, b"tampered-content")
        assert len(errors) >= 1
        assert any("sha256" in e.field for e in errors)

    def test_check_raw_unexpected_raw_in_handoff(self):
        """handoff 等不應包含 raw 的型態，若傳入 raw 應報錯。"""
        sidecar = {
            "format": "aistorage.inbox/v1",
            "item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAH",
            "profile": "mac-opencode",
            "metadata": {"type": "handoff", "id": "handoff:01"},
            "raw": None,
            "body": {},
        }
        errors = check_raw(sidecar, b"unexpected-raw-data")
        assert len(errors) >= 1
        assert any("raw" in e.field for e in errors)

        # 傳入 None 則通過
        assert check_raw(sidecar, None) == []


# ===========================================================================
# 11. is_complete 新規則測試
# ===========================================================================

class TestIsComplete:
    """測試 is_complete 新規則：必須有 .sig；有 raw 需求時需有 .raw；不讀本地檔案。"""

    def test_is_complete_missing_sig_fails(self):
        """沒有 .sig 檔案時一律回傳 False（即使有 sidecar.json 與 raw）。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        names = {f"{item_key}.sidecar.json", f"{item_key}.raw"}
        sidecar = {"format": "aistorage.inbox/v1", "raw": {"size": 10, "sha256": "0" * 64}}

        assert is_complete(names, item_key, sidecar) is False

    def test_is_complete_missing_sidecar_fails(self):
        """沒有 .sidecar.json 檔案時回傳 False。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        names = {f"{item_key}.sig", f"{item_key}.raw"}
        assert is_complete(names, item_key) is False

    def test_is_complete_only_raw_fails(self):
        """只有 .raw 檔案時回傳 False。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        names = {f"{item_key}.raw"}
        assert is_complete(names, item_key) is False

    def test_is_complete_sidecar_requires_raw_but_missing_raw_fails(self):
        """sidecar 載明需要 raw 但收件匣無 .raw 檔時回傳 False。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        names = {f"{item_key}.sidecar.json", f"{item_key}.sig"}
        sidecar = {"format": "aistorage.inbox/v1", "raw": {"size": 10, "sha256": "0" * 64}}

        assert is_complete(names, item_key, sidecar) is False

    def test_is_complete_sidecar_requires_raw_with_all_present_passes(self):
        """sidecar 載明需要 raw 且 .sig、.sidecar.json、.raw 均存在時回傳 True。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        names = {f"{item_key}.sidecar.json", f"{item_key}.sig", f"{item_key}.raw"}
        sidecar = {"format": "aistorage.inbox/v1", "raw": {"size": 10, "sha256": "0" * 64}}

        assert is_complete(names, item_key, sidecar) is True

    def test_is_complete_no_raw_required_passes_without_raw(self):
        """sidecar 無 raw（如 handoff、claim）且有 .sig 與 .sidecar.json 時回傳 True。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAH"
        names = {f"{item_key}.sidecar.json", f"{item_key}.sig"}
        sidecar = {"format": "aistorage.inbox/v1", "raw": None}

        assert is_complete(names, item_key, sidecar) is True

    def test_is_complete_handles_paths_in_names(self):
        """names 中包含目錄路徑時能正確取 basename 判定。"""
        item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAH"
        names = {f"inbox/folder/{item_key}.sidecar.json", f"inbox/folder/{item_key}.sig"}
        sidecar = {"format": "aistorage.inbox/v1", "raw": None}

        assert is_complete(names, item_key, sidecar) is True
