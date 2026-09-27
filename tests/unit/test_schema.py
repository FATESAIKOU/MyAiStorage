"""tests/unit/test_schema.py

tasks 2.1 單元測試：共通項目模型 Schema、ID 規則與分類器。
依據：
- specs/common/item-model
- g2-api-2.1.md
- g2-2.1-fixes.md 與 review-2.1.md
"""

import copy
import re
import time
import pytest

from aistorage.schema import (
    FieldError,
    classify_id,
    make_item_id,
    make_session_id,
    strip_claimed_producer,
    validate_inbox_metadata,
    validate_record_metadata,
)


# ---------------------------------------------------------------------------
# 測試資料 Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_inbox_metadata() -> dict:
    """提供一筆合法的收件匣 metadata（不含 producer；id 符合 session 命名規則）。"""
    return {
        "id": "opencode:sess-test-001",
        "type": "session",
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "case_id": "case-2026-spike",
        "provenance": "manual-import",
    }


@pytest.fixture
def valid_record_metadata() -> dict:
    """提供一筆合法的真本 metadata（含 producer；id 符合 session 命名規則）。"""
    return {
        "id": "opencode:sess-test-001",
        "type": "session",
        "producer": "mac-opencode",
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "case_id": "case-2026-spike",
        "provenance": "manual-import",
    }


# ===========================================================================
# 1. 缺少必填欄位測試（並確認錯誤指出欄位）
# ===========================================================================

class TestRequiredFields:
    """測試收件匣與真本 metadata 缺少必填欄位時的驗證行為。"""

    def test_inbox_valid_passes(self, valid_inbox_metadata):
        """合法收件匣 metadata 應通過驗證（回傳空 list）。"""
        errors = validate_inbox_metadata(valid_inbox_metadata)
        assert errors == [], f"預期通過，但得到錯誤: {errors}"

    def test_record_valid_passes(self, valid_record_metadata):
        """合法真本 metadata 應通過驗證（回傳空 list）。"""
        errors = validate_record_metadata(valid_record_metadata)
        assert errors == [], f"預期通過，但得到錯誤: {errors}"

    @pytest.mark.parametrize("missing_field", ["id", "type", "created_at", "updated_at"])
    def test_inbox_missing_single_required_field(self, valid_inbox_metadata, missing_field):
        """收件匣缺少任一必填欄位時必須失敗，且錯誤必須指出該欄位名稱。"""
        data = dict(valid_inbox_metadata)
        del data[missing_field]

        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1, f"缺少 {missing_field} 時應回報錯誤"

        err_fields = [e.field for e in errors]
        assert missing_field in err_fields, (
            f"缺少欄位 '{missing_field}'，但回報的欄位清單為 {err_fields}"
        )

        err = next(e for e in errors if e.field == missing_field)
        assert isinstance(err, FieldError)
        assert isinstance(err.field, str)
        assert isinstance(err.message, str)
        assert len(err.message.strip()) > 0

    @pytest.mark.parametrize(
        "missing_field",
        ["id", "type", "created_at", "updated_at", "producer", "case_id", "provenance"],
    )
    def test_record_missing_single_required_field(self, valid_record_metadata, missing_field):
        """真本缺少任一必填欄位（含 producer, case_id, provenance）時必須失敗。"""
        data = dict(valid_record_metadata)
        del data[missing_field]

        errors = validate_record_metadata(data)
        assert len(errors) >= 1, f"真本缺少 {missing_field} 時應回報錯誤"
        err_fields = [e.field for e in errors]
        assert missing_field in err_fields, (
            f"真本缺少欄位 '{missing_field}'，但回報的欄位清單為 {err_fields}"
        )

    def test_record_missing_producer_specifically(self, valid_record_metadata):
        """特別確認：真本 metadata 缺少 producer 必須回報 producer 欄位錯誤。"""
        data = dict(valid_record_metadata)
        del data["producer"]

        errors = validate_record_metadata(data)
        err_fields = [e.field for e in errors]
        assert "producer" in err_fields, "真本缺少 producer 時必須明確回報 field='producer'"

    def test_inbox_empty_dict_reports_all_required_fields(self):
        """收件匣傳入空 dict 時，必須回報所有缺少的必填欄位。"""
        errors = validate_inbox_metadata({})
        err_fields = {e.field for e in errors}
        expected_fields = {"id", "type", "created_at", "updated_at"}
        assert expected_fields.issubset(err_fields), (
            f"空物件應回報缺少 {expected_fields}，實際回報: {err_fields}"
        )

    def test_record_empty_dict_reports_all_required_fields(self):
        """真本傳入空 dict 時，必須回報所有缺少的必填欄位。"""
        errors = validate_record_metadata({})
        err_fields = {e.field for e in errors}
        expected_fields = {
            "id",
            "type",
            "created_at",
            "updated_at",
            "producer",
            "case_id",
            "provenance",
        }
        assert expected_fields.issubset(err_fields), (
            f"空物件應回報缺少 {expected_fields}，實際回報: {err_fields}"
        )


# ===========================================================================
# 2. case_id / provenance 規則測試（PM 決定 2）
# ===========================================================================

class TestCaseIdAndProvenanceNull:
    """測試 spec 與 PM 決定 2：

    - 收件匣：case_id、provenance 可為 null 或省略（提交流程會補 null）。
    - 真本：case_id、provenance 鍵值必須存在（可為 null，但不得省略）。
    """

    def test_inbox_case_id_null_accepted(self, valid_inbox_metadata):
        """收件匣 case_id 為 None (JSON null) 應通過驗證。"""
        data = dict(valid_inbox_metadata, case_id=None)
        errors = validate_inbox_metadata(data)
        assert errors == [], f"case_id=None 應通過，但得到錯誤: {errors}"

    def test_inbox_provenance_null_accepted(self, valid_inbox_metadata):
        """收件匣 provenance 為 None (JSON null) 應通過驗證。"""
        data = dict(valid_inbox_metadata, provenance=None)
        errors = validate_inbox_metadata(data)
        assert errors == [], f"provenance=None 應通過，但得到錯誤: {errors}"

    def test_inbox_both_case_id_and_provenance_null_accepted(self, valid_inbox_metadata):
        """收件匣 case_id 與 provenance 同時為 None 應通過驗證。"""
        data = dict(valid_inbox_metadata, case_id=None, provenance=None)
        errors = validate_inbox_metadata(data)
        assert errors == [], f"case_id 與 provenance 均為 None 應通過，但得到: {errors}"

    def test_inbox_omitted_case_id_and_provenance_accepted(self, valid_inbox_metadata):
        """收件匣未提供 case_id 或 provenance 鍵值時應通過驗證。"""
        data = dict(valid_inbox_metadata)
        data.pop("case_id", None)
        data.pop("provenance", None)
        errors = validate_inbox_metadata(data)
        assert errors == [], f"省略 case_id 與 provenance 應通過，但得到: {errors}"

    def test_record_case_id_and_provenance_null_accepted(self, valid_record_metadata):
        """真本 case_id 與 provenance 鍵值存在且為 None 時應通過驗證。"""
        data = dict(valid_record_metadata, case_id=None, provenance=None)
        errors = validate_record_metadata(data)
        assert errors == [], f"真本 case_id 與 provenance 為 None 應通過，但得到: {errors}"

    def test_record_omitted_case_id_rejected(self, valid_record_metadata):
        """真本省略 case_id 鍵值應被拒絕（PM 決定 2：真本鍵必須存在）。"""
        data = dict(valid_record_metadata)
        del data["case_id"]
        errors = validate_record_metadata(data)
        assert len(errors) >= 1, "真本省略 case_id 必須失敗"
        assert any("case_id" in e.field for e in errors)

    def test_record_omitted_provenance_rejected(self, valid_record_metadata):
        """真本省略 provenance 鍵值應被拒絕（PM 決定 2：真本鍵必須存在）。"""
        data = dict(valid_record_metadata)
        del data["provenance"]
        errors = validate_record_metadata(data)
        assert len(errors) >= 1, "真本省略 provenance 必須失敗"
        assert any("provenance" in e.field for e in errors)

    def test_valid_string_case_id_and_provenance_accepted(self, valid_inbox_metadata):
        """提供合法字串之 case_id 與 provenance 應通過驗證。"""
        data = dict(
            valid_inbox_metadata,
            case_id="case-2026-tech-spike",
            provenance="sync:mac-opencode:sess-42",
        )
        errors = validate_inbox_metadata(data)
        assert errors == []


# ===========================================================================
# 3. 收件匣帶 producer 會被 strip 測試
# ===========================================================================

class TestStripClaimedProducer:
    """測試 spec 規定：寫入者自報的 producer 必須被忽略/去除，不可直接採納。"""

    def test_strip_claimed_producer_removes_producer(self, valid_inbox_metadata):
        """strip_claimed_producer 必須自 dict 中移除 producer 欄位。"""
        claimed = dict(valid_inbox_metadata, producer="human:fatesaikou")
        assert "producer" in claimed

        stripped = strip_claimed_producer(claimed)
        assert "producer" not in stripped
        assert stripped["id"] == claimed["id"]
        assert stripped["type"] == claimed["type"]
        assert stripped["created_at"] == claimed["created_at"]
        assert stripped["updated_at"] == claimed["updated_at"]

    def test_strip_claimed_producer_does_not_mutate_original(self, valid_inbox_metadata):
        """strip_claimed_producer 回傳新 dict，不得修改傳入的原物件。"""
        original = dict(valid_inbox_metadata, producer="worker/default")
        _ = strip_claimed_producer(original)
        assert "producer" in original, "原物件不應被修改（in-place mutation forbidden）"
        assert original["producer"] == "worker/default"

    def test_strip_claimed_producer_when_no_producer_present(self, valid_inbox_metadata):
        """原物件不含 producer 時，strip_claimed_producer 仍正常回傳無 producer 之 dict。"""
        assert "producer" not in valid_inbox_metadata
        stripped = strip_claimed_producer(valid_inbox_metadata)
        assert "producer" not in stripped
        assert stripped == valid_inbox_metadata

    def test_inbox_validation_with_producer_present(self, valid_inbox_metadata):
        """收件匣 metadata 即使帶了自報的 producer，依 spec 應被忽略而不致驗證崩潰。"""
        data = dict(valid_inbox_metadata, producer="untrusted-ai-claim")
        errors = validate_inbox_metadata(data)
        assert errors == [], f"帶 producer 之收件匣 metadata 應被忽略通過，但報錯: {errors}"


# ===========================================================================
# 4. 不認得的欄位被忽略測試
# ===========================================================================

class TestUnknownFields:
    """測試 spec 規定：metadata 可以擴充，讀取者與驗證器必須忽略不認得的欄位。"""

    def test_inbox_unknown_fields_ignored(self, valid_inbox_metadata):
        """收件匣包含未知擴充欄位時應通過驗證。"""
        data = dict(
            valid_inbox_metadata,
            privacy_tag="confidential",
            custom_annotation="test note",
            x_future_feature_flag=True,
            numeric_extension=999,
        )
        errors = validate_inbox_metadata(data)
        assert errors == [], f"未知欄位應被忽略，但得到錯誤: {errors}"

    def test_record_unknown_fields_ignored(self, valid_record_metadata):
        """真本包含未知擴充欄位時應通過驗證。"""
        data = dict(
            valid_record_metadata,
            llm_gateway_flags={"audit": True, "quota_tier": "pro"},
            unknown_extra_list=[1, 2, 3],
        )
        errors = validate_record_metadata(data)
        assert errors == [], f"真本未知欄位應被忽略，但得到錯誤: {errors}"


# ===========================================================================
# 5. role / role_version 選填測試
# ===========================================================================

class TestOptionalRoleFields:
    """測試 g2-api-2.1 規定：role 與 role_version 為預留選填欄位，期 1 不驗證內容。"""

    def test_inbox_without_role_fields_passes(self, valid_inbox_metadata):
        """不帶 role 與 role_version 應通過驗證。"""
        assert "role" not in valid_inbox_metadata
        assert "role_version" not in valid_inbox_metadata
        assert validate_inbox_metadata(valid_inbox_metadata) == []

    def test_inbox_with_role_only(self, valid_inbox_metadata):
        """僅帶 role 應通過驗證。"""
        data = dict(valid_inbox_metadata, role="architect")
        assert validate_inbox_metadata(data) == []

    def test_inbox_with_role_version_only(self, valid_inbox_metadata):
        """僅帶 role_version 應通過驗證。"""
        data = dict(valid_inbox_metadata, role_version="v2.1.0")
        assert validate_inbox_metadata(data) == []

    def test_inbox_with_both_role_and_role_version(self, valid_inbox_metadata):
        """同時帶 role 與 role_version 應通過驗證。"""
        data = dict(valid_inbox_metadata, role="executor", role_version="2026.09")
        assert validate_inbox_metadata(data) == []

    def test_record_with_both_role_and_role_version(self, valid_record_metadata):
        """真本帶 role 與 role_version 亦應通過驗證。"""
        data = dict(valid_record_metadata, role="reviewer", role_version="1.0")
        assert validate_record_metadata(data) == []


# ===========================================================================
# 6. 時間格式邊界與嚴格 UTC 驗證測試 (M1 & PM 決定 3)
# ===========================================================================

class TestDateTimeValidation:
    """測試 PM 決定 3：時間格式嚴格限制 RFC 3339 UTC ('Z')，排除非 UTC、基本格式與非法日期。"""

    @pytest.mark.parametrize("valid_ts", [
        "2026-09-27T08:00:00Z",
        "2026-09-27T08:00:00.123Z",
        "2026-09-27T08:00:00.123456Z",
        "2026-01-01T00:00:00Z",
        "2026-12-31T23:59:59Z",
    ])
    def test_valid_utc_datetime_passes(self, valid_inbox_metadata, valid_ts):
        """合法 UTC Z 時間應通過驗證。"""
        data = dict(valid_inbox_metadata, created_at=valid_ts, updated_at=valid_ts)
        assert validate_inbox_metadata(data) == []

    @pytest.mark.parametrize("invalid_ts,desc", [
        ("2026-09-27T08:00:00+09:00", "非 UTC 時區位移 (+09:00)"),
        ("2026-09-27T08:00:00-05:00", "非 UTC 時區位移 (-05:00)"),
        ("2026-09-27 08:00:00Z", "空白分隔而非 T 分隔"),
        ("2026-09-27T08:00Z", "缺少秒數部分"),
        ("20260927T080000Z", "ISO 8601 基本格式而非擴展格式"),
        ("2026-W39-7T08:00:00Z", "週日期表示法"),
        ("2026-02-30T08:00:00Z", "2 月 30 日（不存在的日期）"),
        ("2026-13-01T08:00:00Z", "13 月（不存在的月份）"),
        ("2026-04-31T08:00:00Z", "4 月 31 日（4 月只有 30 天）"),
        ("", "空字串"),
        ("not-a-datetime", "非時間格式字串"),
    ])
    def test_invalid_datetime_rejected(self, valid_inbox_metadata, invalid_ts, desc):
        """各類不合法或非 UTC Z 時間必須被拒絕，且指出欄位。"""
        data = dict(valid_inbox_metadata, created_at=invalid_ts)
        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1, f"應拒絕 {desc} ({invalid_ts})"
        err_fields = [e.field for e in errors]
        assert "created_at" in err_fields, f"錯誤應指出 created_at，實際為 {err_fields}"

    def test_created_at_greater_than_updated_at_not_rejected(self, valid_inbox_metadata):
        """PM 決定 3：時鐘漂移不以 created_at <= updated_at 作為拒收條件。"""
        data = dict(
            valid_inbox_metadata,
            created_at="2026-09-27T08:05:00Z",
            updated_at="2026-09-27T08:00:00Z",  # 早於 created_at
        )
        assert validate_inbox_metadata(data) == []


# ===========================================================================
# 7. ID 規則、命名空間與型態一致性測試 (M2 & PM 決定 1)
# ===========================================================================

class TestIdRulesAndNamespace:
    r"""測試 PM 決定 1：

    - make_session_id：拒絕保留名（handoff, claim, reference, rewrite, artifact, session）當 source。
    - make_item_id：拒絕 'session' 當型態。
    - validate 依 type 檢查 id：session 為 ^[^:\s]+:\S+$；其他為 ^<type>:[0-9A-HJKMNP-TV-Z]{26}$。
    - 拒絕空字串、全空白字串之 id。
    """

    # --- make_session_id ---

    def test_make_session_id_valid(self):
        """make_session_id('<source>', '<source_session_id>') 回傳 '<source>:<source_session_id>'。"""
        assert make_session_id("opencode", "sess-12345") == "opencode:sess-12345"
        assert make_session_id("chatgpt", "conv-uuid-abc") == "chatgpt:conv-uuid-abc"

    def test_make_session_id_with_colon_in_source_session_id(self):
        """source_session_id 允許包含冒號（例如外部系統自帶前綴）。"""
        result = make_session_id("opencode", "thread:subthread:99")
        assert result == "opencode:thread:subthread:99"

    @pytest.mark.parametrize("reserved_source", [
        "session", "handoff", "claim", "reference", "rewrite", "artifact"
    ])
    def test_make_session_id_reserved_source_raises_value_error(self, reserved_source):
        """PM 決定 1：make_session_id 拒絕保留的項目型態名作為 source。"""
        with pytest.raises(ValueError):
            make_session_id(reserved_source, "sess-001")

    def test_make_session_id_empty_source_raises_value_error(self):
        """source 為空字串時必須 raise ValueError。"""
        with pytest.raises(ValueError):
            make_session_id("", "sess-001")

    def test_make_session_id_whitespace_source_raises_value_error(self):
        """source 包含空白時必須 raise ValueError。"""
        with pytest.raises(ValueError):
            make_session_id("open code", "sess-001")

    def test_make_session_id_empty_source_session_id_raises_value_error(self):
        """source_session_id 為空字串時必須 raise ValueError。"""
        with pytest.raises(ValueError):
            make_session_id("opencode", "")

    def test_make_session_id_colon_in_source_raises_value_error(self):
        """source 包含冒號 ':' 時必須 raise ValueError。"""
        with pytest.raises(ValueError):
            make_session_id("open:code", "sess-001")

    # --- make_item_id ---

    @pytest.mark.parametrize("item_type", [
        "handoff", "claim", "reference", "rewrite", "artifact"
    ])
    def test_make_item_id_valid_types(self, item_type):
        """make_item_id 回傳 '<type>:<ULID>'，其中 ULID 為 26 字 Crockford Base32。"""
        item_id = make_item_id(item_type)
        prefix, sep, ulid = item_id.partition(":")

        assert sep == ":", "ID 必須包含分隔符冒號"
        assert prefix == item_type, f"前綴應為 {item_type}"
        assert len(ulid) == 26, f"ULID 長度必須恰為 26 字元，實際長度為 {len(ulid)} ({ulid})"

        crockford_pattern = re.compile(r"^[0-9A-HJ-KM-NP-TV-Z]{26}$", re.IGNORECASE)
        assert crockford_pattern.match(ulid), f"ULID '{ulid}' 不符合 Crockford Base32 格式"

    def test_make_item_id_session_type_raises_value_error(self):
        """PM 決定 1：make_item_id 拒絕 'session' 型態（Session 必須用 make_session_id）。"""
        with pytest.raises(ValueError):
            make_item_id("session")

    def test_make_item_id_empty_type_raises_value_error(self):
        """item_type 為空字串時必須 raise ValueError。"""
        with pytest.raises(ValueError):
            make_item_id("")

    def test_make_item_id_time_sortable(self):
        """ULID 具時間可排序性。"""
        id1 = make_item_id("artifact")
        time.sleep(0.005)
        id2 = make_item_id("artifact")
        time.sleep(0.005)
        id3 = make_item_id("artifact")

        ulid1 = id1.split(":", 1)[1]
        ulid2 = id2.split(":", 1)[1]
        ulid3 = id3.split(":", 1)[1]

        assert ulid1 < ulid2 < ulid3

    def test_make_item_id_uniqueness(self):
        """快速連續生成 100 個 ID 必須全數唯一不重複。"""
        ids = [make_item_id("claim") for _ in range(100)]
        assert len(set(ids)) == 100

    # --- 驗證器對 id 形狀與型態一致性的檢查 ---

    def test_id_with_only_whitespace_rejected(self, valid_inbox_metadata):
        """PM 決定 1：只有空白的 id 必須被拒絕。"""
        data = dict(valid_inbox_metadata, id="   ")
        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1
        assert any("id" in e.field for e in errors)

    @pytest.mark.parametrize("mismatched_type,mismatched_id", [
        ("handoff", "claim:01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("claim", "reference:01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("reference", "artifact:01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("session", "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        ("artifact", "opencode:ses_12345"),
    ])
    def test_id_prefix_mismatched_with_type_rejected(
        self, valid_inbox_metadata, mismatched_type, mismatched_id
    ):
        """M2：id 前綴與 type 不一致時必須被拒絕。"""
        data = dict(valid_inbox_metadata, type=mismatched_type, id=mismatched_id)
        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1, f"type={mismatched_type} 與 id={mismatched_id} 不一致應被拒絕"
        assert any("id" in e.field for e in errors)

    @pytest.mark.parametrize("reserved_source", [
        "session", "handoff", "claim", "reference", "rewrite", "artifact"
    ])
    def test_session_id_with_reserved_source_name_rejected(
        self, valid_inbox_metadata, reserved_source
    ):
        """PM 決定 1：Session 的 source 不得使用保留型態名稱。"""
        data = dict(valid_inbox_metadata, type="session", id=f"{reserved_source}:ses_001")
        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1
        assert any("id" in e.field for e in errors)

    def test_item_id_invalid_ulid_format_rejected(self, valid_inbox_metadata):
        """非 session 項目之 ID 必須為 26 字 Crockford Base32。"""
        # 長度不足
        data = dict(valid_inbox_metadata, type="handoff", id="handoff:SHORTULID")
        assert len(validate_inbox_metadata(data)) >= 1
        # 含非法字元 (如 I, L, O, U)
        data = dict(valid_inbox_metadata, type="handoff", id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAU")
        assert len(validate_inbox_metadata(data)) >= 1


# ===========================================================================
# 8. Type 欄位規則測試 (M5 & PM 決定 5)
# ===========================================================================

class TestTypeFieldRules:
    """測試收件匣與真本之 type 規則：

    - 收件匣：維持封閉 enum（session, handoff, claim, reference, rewrite, artifact）。
    - 真本：改為 pattern: ^[a-z][a-z_]*$。
    """

    def test_inbox_unknown_type_rejected(self, valid_inbox_metadata):
        """收件匣傳入未知之 type（如 'foo'）必須被拒絕，且指出 field='type'。"""
        data = dict(valid_inbox_metadata, type="foo", id="foo:01ARZ3NDEKTSV4RRFFQ69G5FAV")
        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1
        assert any("type" in e.field for e in errors)

    def test_record_extensible_type_pattern(self, valid_record_metadata):
        """PM 決定 5：真本 type 符合 ^[a-z][a-z_]*$ 應被 schema 允許（型態清單由提交流程檢驗）。"""
        data = dict(valid_record_metadata, type="custom_event", id="custom_event:01ARZ3NDEKTSV4RRFFQ69G5FAV")
        errors = validate_record_metadata(data)
        # 真本若採用 pattern 則不會因 enum 不在清單而報錯
        type_errors = [e for e in errors if e.field == "type"]
        assert type_errors == [], f"真本 type 應接受小寫 snake_case pattern，但報錯: {type_errors}"

    def test_record_invalid_type_pattern_rejected(self, valid_record_metadata):
        """真本 type 若含大寫或非法字元應被拒絕。"""
        data = dict(valid_record_metadata, type="Custom-Event", id="Custom-Event:01ARZ3NDEKTSV4RRFFQ69G5FAV")
        errors = validate_record_metadata(data)
        assert len(errors) >= 1
        assert any("type" in e.field for e in errors)


# ===========================================================================
# 9. 欄位型態錯誤與非 dict 輸入測試 (M5)
# ===========================================================================

class TestFieldTypesAndInputTypes:
    """測試欄位型態錯誤時的報錯，以及傳入非 dict 時回傳 field='' 錯誤。"""

    @pytest.mark.parametrize("field,bad_val", [
        ("id", 12345),
        ("type", 999),
        ("case_id", 123),
        ("provenance", True),
        ("created_at", 123456789),
        ("updated_at", False),
    ])
    def test_inbox_field_type_error_reported(self, valid_inbox_metadata, field, bad_val):
        """欄位型態錯誤時必須被拒絕，且 field 指出該欄位。"""
        data = dict(valid_inbox_metadata)
        data[field] = bad_val
        errors = validate_inbox_metadata(data)
        assert len(errors) >= 1
        assert any(field in e.field for e in errors), f"錯誤應指出 {field}"

    @pytest.mark.parametrize("bad_input", [
        None,
        ["not", "a", "dict"],
        "string-metadata",
        12345,
    ])
    def test_non_dict_input_returns_empty_field_error(self, bad_input):
        """傳入非 dict 輸入時，不可丟出例外，應回傳包含 field='' 的錯誤物件。"""
        inbox_errs = validate_inbox_metadata(bad_input)
        assert isinstance(inbox_errs, list)
        assert len(inbox_errs) >= 1
        assert any(e.field == "" for e in inbox_errs)

        record_errs = validate_record_metadata(bad_input)
        assert isinstance(record_errs, list)
        assert len(record_errs) >= 1
        assert any(e.field == "" for e in record_errs)


# ===========================================================================
# 10. classify_id 測試（new / update / collision / ValueError）
# ===========================================================================

class TestClassifyId:
    """測試 classify_id 對新項目、同一項目更新、撞號 (collision) 以及 ValueError 情況。"""

    def test_classify_id_new_when_no_existing(self):
        """existing 為 None 時，判定為 'new'。"""
        incoming = {
            "id": "opencode:sess-001",
            "type": "session",
            "producer": "mac-opencode",
        }
        assert classify_id(None, incoming) == "new"

    def test_classify_id_update_when_same_id_producer_type(self):
        """同 id、同 producer、同 type 時，判定為 'update'。"""
        existing = {
            "id": "opencode:sess-001",
            "type": "session",
            "producer": "mac-opencode",
            "updated_at": "2026-09-27T08:00:00Z",
        }
        incoming = {
            "id": "opencode:sess-001",
            "type": "session",
            "producer": "mac-opencode",
            "updated_at": "2026-09-27T09:00:00Z",
        }
        assert classify_id(existing, incoming) == "update"

    def test_classify_id_collision_different_producer(self):
        """同 id 但 producer 不同時，判定為 'collision'（撞號拒收）。"""
        existing = {
            "id": "opencode:sess-001",
            "type": "session",
            "producer": "mac-opencode",
        }
        incoming = {
            "id": "opencode:sess-001",
            "type": "session",
            "producer": "worker/default",
        }
        assert classify_id(existing, incoming) == "collision"

    def test_classify_id_collision_different_type(self):
        """同 id 但 type 不同時，判定為 'collision'（撞號拒收）。"""
        existing = {
            "id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "type": "handoff",
            "producer": "mac-opencode",
        }
        incoming = {
            "id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "type": "claim",
            "producer": "mac-opencode",
        }
        assert classify_id(existing, incoming) == "collision"

    def test_classify_id_collision_different_producer_and_type(self):
        """同 id 但 producer 與 type 均不同時，判定為 'collision'。"""
        existing = {
            "id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "type": "reference",
            "producer": "mac-opencode",
        }
        incoming = {
            "id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "type": "rewrite",
            "producer": "other-agent",
        }
        assert classify_id(existing, incoming) == "collision"

    def test_classify_id_different_ids_raises_value_error(self):
        """L1 & PM 決定：existing 與 incoming 的 id 不同時必須 raise ValueError。"""
        existing = {
            "id": "opencode:sess-001",
            "type": "session",
            "producer": "mac-opencode",
        }
        incoming = {
            "id": "opencode:sess-002",
            "type": "session",
            "producer": "mac-opencode",
        }
        with pytest.raises(ValueError):
            classify_id(existing, incoming)

    @pytest.mark.parametrize("bad_existing,bad_incoming", [
        ({"id": "opencode:001"}, {"id": "opencode:001", "producer": "mac"}),
        ({"id": "opencode:001", "producer": "mac"}, {"id": "opencode:001"}),
        ({"id": "opencode:001", "producer": None}, {"id": "opencode:001", "producer": "mac"}),
        ({"id": "opencode:001", "producer": ""}, {"id": "opencode:001", "producer": "mac"}),
    ])
    def test_classify_id_missing_producer_raises_value_error(self, bad_existing, bad_incoming):
        """L1 & PM 決定：任一方缺少有效 producer 時必須 raise ValueError。"""
        with pytest.raises(ValueError):
            classify_id(bad_existing, bad_incoming)


# ===========================================================================
# 8. Schema 定位與打包載入測試（review-2.1 L3）
# ===========================================================================

class TestSchemaLocate:
    """測試 schema 檔案定位機制（importlib.resources 與 AISTORAGE_SCHEMA_DIR）。"""

    def test_locate_schema_file_default_finds_schemas(self):
        """預設環境下能正確定位 schema 檔案。"""
        from aistorage.schema import _locate_schema_file
        path = _locate_schema_file("metadata-inbox.schema.json")
        assert path.is_file()
        assert path.name == "metadata-inbox.schema.json"

    def test_locate_schema_file_env_override(self, tmp_path, monkeypatch):
        """AISTORAGE_SCHEMA_DIR 覆寫時，優先自該目錄載入。"""
        from aistorage.schema import _locate_schema_file
        custom_schema = tmp_path / "metadata-inbox.schema.json"
        custom_schema.write_text('{"title": "custom"}', encoding="utf-8")

        monkeypatch.setenv("AISTORAGE_SCHEMA_DIR", str(tmp_path))
        path = _locate_schema_file("metadata-inbox.schema.json")
        assert path == custom_schema
        assert path.read_text(encoding="utf-8") == '{"title": "custom"}'

    def test_locate_schema_file_env_override_not_found(self, tmp_path, monkeypatch):
        """AISTORAGE_SCHEMA_DIR 覆寫但檔案不存在時，拋出 FileNotFoundError。"""
        from aistorage.schema import _locate_schema_file
        monkeypatch.setenv("AISTORAGE_SCHEMA_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError, match="AISTORAGE_SCHEMA_DIR"):
            _locate_schema_file("metadata-inbox.schema.json")

