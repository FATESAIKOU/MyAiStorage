"""tests/unit/test_reading.py

tasks 2.4 單元測試：Agora Session 閱讀版共通格式驗證與輔助函式。
依據：
- g2-api-2.4.md
- g2-2.4-2.3-fixes.md
- design.md D10
- specs/agora/session-link（接續點）
- schemas/reading-version.schema.json
- schemas/reading-version.md（兩份範例）
"""

from __future__ import annotations

import copy
import pytest

from aistorage.reading import (
    check_continuation,
    messages_before,
    plain_text,
    validate_reading,
)
from aistorage.schema import FieldError


# ---------------------------------------------------------------------------
# 測試資料 Fixtures：依據 schemas/reading-version.md 兩份正式範例
# ---------------------------------------------------------------------------

@pytest.fixture
def example1_regular_session() -> dict:
    """範例 1：一般 Session（包含使用者提問、推理段落、工具呼叫與助理回覆）。"""
    return {
        "format": "aistorage.reading/v1",
        "session_id": "opencode:ses_20260927_001",
        "source": "opencode",
        "title": "診斷資料庫連線池問題",
        "parent_id": None,
        "snapshot_sha256": "4a7d1ed414474e4033ac29ccb8653d9b110e11894d7c0f16599b4f6cf4c93540",
        "in_progress": False,
        "messages": [
            {
                "message_id": "msg_001",
                "index": 0,
                "role": "user",
                "created_at": "2026-09-27T08:00:00Z",
                "completed": True,
                "reverted": False,
                "parts": [
                    {
                        "type": "text",
                        "text": "請檢視目前的連線池設定檔，並評估在 100 個並發查詢時是否足夠。",
                    }
                ],
            },
            {
                "message_id": "msg_002",
                "index": 1,
                "role": "assistant",
                "created_at": "2026-09-27T08:00:05Z",
                "completed": True,
                "reverted": False,
                "parts": [
                    {
                        "type": "reasoning",
                        "text": "使用者欲知連線池是否足以支撐 100 個並發，首先應讀取設定檔確認上限。",
                    },
                    {
                        "type": "text",
                        "text": "好的，我先讀取連線池設定檔。",
                    },
                    {
                        "type": "tool_call",
                        "name": "view_file",
                        "input_summary": "path: config/database.json",
                        "output_summary": '{"pool_size": 20, "max_overflow": 10, "timeout": 30}',
                        "child_session_id": None,
                    },
                    {
                        "type": "text",
                        "text": "目前的 `pool_size` 設為 20、`max_overflow` 為 10，最大容量僅 30。面對 100 個並發查詢時將發生排隊或逾時，建議調整至 50 並啟用連線複用。",
                    },
                ],
            },
        ],
    }


@pytest.fixture
def example2_session_with_subagents_and_reverts() -> dict:
    """範例 2：含子代理與撤銷的 Session（包含子代理派工、撤銷訊息、壓縮摘要）。"""
    return {
        "format": "aistorage.reading/v1",
        "session_id": "opencode:ses_20260927_002",
        "source": "opencode",
        "title": "大規模架構重構與子代理派工",
        "parent_id": None,
        "snapshot_sha256": "9b110e11894d7c0f16599b4f6cf4c935404a7d1ed414474e4033ac29ccb8653d",
        "in_progress": False,
        "messages": [
            {
                "message_id": "msg_010",
                "index": 0,
                "role": "user",
                "created_at": "2026-09-27T08:10:00Z",
                "completed": True,
                "reverted": False,
                "parts": [
                    {
                        "type": "text",
                        "text": "請啟動子代理去搜尋專案中所有使用舊版 API 的位置。",
                    }
                ],
            },
            {
                "message_id": "msg_011",
                "index": 1,
                "role": "assistant",
                "created_at": "2026-09-27T08:10:04Z",
                "completed": True,
                "reverted": False,
                "parts": [
                    {
                        "type": "text",
                        "text": "已派出研究型子代理進行搜尋。",
                    },
                    {
                        "type": "tool_call",
                        "name": "task",
                        "input_summary": "prompt: 搜尋專案內所有 import legacy_api 的檔案",
                        "output_summary": "搜尋完成，共發現 14 處調用。",
                        "child_session_id": "opencode:ses_20260927_child_01",
                    },
                    {
                        "type": "text",
                        "text": "子代理回報完畢，共發現 14 處調用，已整理清單於 reports/legacy.md。",
                    },
                ],
            },
            {
                "message_id": "msg_012",
                "index": 2,
                "role": "user",
                "created_at": "2026-09-27T08:15:00Z",
                "completed": True,
                "reverted": True,
                "parts": [
                    {
                        "type": "text",
                        "text": "請立刻刪除那 14 個檔案。（註：使用者隨後發覺有誤，執行了 /undo 撤銷）",
                    }
                ],
            },
            {
                "message_id": "msg_013",
                "index": 3,
                "role": "assistant",
                "created_at": "2026-09-27T08:15:02Z",
                "completed": False,
                "reverted": True,
                "parts": [
                    {
                        "type": "text",
                        "text": "準備開始刪除...（生成中斷並隨 /undo 撤銷）",
                    }
                ],
            },
            {
                "message_id": "msg_014",
                "index": 4,
                "role": "user",
                "created_at": "2026-09-27T08:16:00Z",
                "completed": True,
                "reverted": False,
                "parts": [
                    {
                        "type": "compaction",
                        "summary": "前情提要：已由子代理找出 14 處舊版 API 調用，現準備執行安全相容包裝而非直接刪除。",
                    },
                    {
                        "type": "text",
                        "text": "更正：不要刪除檔案，請替它們撰寫相容性轉接層 (adapter)。",
                    },
                ],
            },
            {
                "message_id": "msg_015",
                "index": 5,
                "role": "assistant",
                "created_at": "2026-09-27T08:16:15Z",
                "completed": True,
                "reverted": False,
                "parts": [
                    {
                        "type": "text",
                        "text": "收到更正。我將開始在 `src/compat/` 建立轉接層，保持向後相容。",
                    }
                ],
            },
        ],
    }


# ===========================================================================
# 1. 範例驗證測試（兩份正式範例皆必須通過）
# ===========================================================================

class TestReadingExamplesValidation:
    """測試 schemas/reading-version.md 提供的兩份正式範例均能通過 validate_reading。"""

    def test_example1_regular_session_passes(self, example1_regular_session):
        errors = validate_reading(example1_regular_session)
        assert errors == [], f"範例 1 預期驗證通過，但得到錯誤: {errors}"

    def test_example2_session_with_subagents_and_reverts_passes(
        self, example2_session_with_subagents_and_reverts
    ):
        errors = validate_reading(example2_session_with_subagents_and_reverts)
        assert errors == [], f"範例 2 預期驗證通過，但得到錯誤: {errors}"


# ===========================================================================
# 2. validate_reading 必填欄位與型別測試
# ===========================================================================

class TestValidateReadingSchemaAndTypes:
    """測試閱讀版頂層、訊息層、內容段落層之必填欄位與型別限制。"""

    @pytest.mark.parametrize(
        "missing_field",
        ["format", "session_id", "source", "snapshot_sha256", "in_progress", "messages"],
    )
    def test_missing_top_level_required_fields(self, example1_regular_session, missing_field):
        data = copy.deepcopy(example1_regular_session)
        del data[missing_field]
        errors = validate_reading(data)
        assert len(errors) >= 1, f"頂層缺少 {missing_field} 時應回報錯誤"
        assert any(missing_field in e.field for e in errors)

    def test_in_progress_must_be_boolean(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        data["in_progress"] = "not-a-bool"
        errors = validate_reading(data)
        assert any("in_progress" in e.field for e in errors)

    def test_non_dict_input_returns_empty_field_error(self):
        for bad_input in [None, [], "not-a-dict", 12345]:
            errors = validate_reading(bad_input)
            assert len(errors) >= 1
            assert errors[0].field == ""
            assert isinstance(errors[0], FieldError)

    def test_format_value_must_be_exact(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        data["format"] = "aistorage.reading/v2"
        errors = validate_reading(data)
        assert len(errors) >= 1
        assert any("format" in e.field for e in errors)

    def test_snapshot_sha256_must_be_lowercase_hex(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        # 大寫 HEX 應被拒絕
        data["snapshot_sha256"] = "4A7D1ED414474E4033AC29CCB8653D9B110E11894D7C0F16599B4F6CF4C93540"
        errors = validate_reading(data)
        assert any("snapshot_sha256" in e.field for e in errors)

    def test_title_and_parent_id_can_be_null_or_string(self, example1_regular_session):
        # 兩者皆為 null
        data = copy.deepcopy(example1_regular_session)
        data["title"] = None
        data["parent_id"] = None
        assert validate_reading(data) == []

        # 兩者皆為有效字串
        data["title"] = "有效的對話標題"
        data["parent_id"] = "opencode:parent_ses_001"
        assert validate_reading(data) == []

    @pytest.mark.parametrize(
        "missing_field",
        ["message_id", "index", "role", "completed", "reverted", "parts"],
    )
    def test_missing_message_required_fields(self, example1_regular_session, missing_field):
        data = copy.deepcopy(example1_regular_session)
        del data["messages"][0][missing_field]
        errors = validate_reading(data)
        assert len(errors) >= 1
        assert any(missing_field in e.field for e in errors)

    def test_invalid_role_rejected(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        data["messages"][0]["role"] = "superadmin"
        errors = validate_reading(data)
        assert len(errors) >= 1
        assert any("role" in e.field for e in errors)

    def test_message_created_at_null_accepted(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        data["messages"][0]["created_at"] = None
        assert validate_reading(data) == []

    def test_image_part_fields_and_types(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        image_part = {
            "type": "image",
            "media_type": "image/png",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "size": 15420,
        }
        data["messages"][0]["parts"].append(image_part)
        assert validate_reading(data) == []

        # 缺少 size 應失敗
        bad_image = copy.deepcopy(data)
        del bad_image["messages"][0]["parts"][-1]["size"]
        assert len(validate_reading(bad_image)) >= 1

    def test_tool_call_part_fields(self, example1_regular_session):
        data = copy.deepcopy(example1_regular_session)
        # child_session_id 為字串
        data["messages"][1]["parts"][2]["child_session_id"] = "opencode:ses_child_01"
        assert validate_reading(data) == []

        # 缺少 name 應失敗
        bad_tool = copy.deepcopy(data)
        del bad_tool["messages"][1]["parts"][2]["name"]
        assert len(validate_reading(bad_tool)) >= 1

    def test_reasoning_part_fields_and_types(self, example1_regular_session):
        """2.4 決定 4：支援 reasoning 推理段落。"""
        data = copy.deepcopy(example1_regular_session)
        reasoning_part = {
            "type": "reasoning",
            "text": "這是思考與推理過程分析。",
        }
        data["messages"][0]["parts"].append(reasoning_part)
        assert validate_reading(data) == []

        # 缺少 text 應失敗
        bad_reasoning = copy.deepcopy(data)
        del bad_reasoning["messages"][0]["parts"][-1]["text"]
        assert len(validate_reading(bad_reasoning)) >= 1

    def test_file_part_fields_and_types(self, example1_regular_session):
        """2.4 決定 4：支援 file 檔案段落（media_type, sha256, size, name）。"""
        data = copy.deepcopy(example1_regular_session)
        file_part = {
            "type": "file",
            "name": "data.csv",
            "media_type": "text/csv",
            "sha256": "a" * 64,
            "size": 2048,
        }
        data["messages"][0]["parts"].append(file_part)
        assert validate_reading(data) == []

        # 缺少 name 應失敗
        bad_file = copy.deepcopy(data)
        del bad_file["messages"][0]["parts"][-1]["name"]
        assert len(validate_reading(bad_file)) >= 1

    def test_message_id_uniqueness_rejected(self, example1_regular_session):
        """2.4 決定 3：整份閱讀版中的 message_id 必須唯一。"""
        data = copy.deepcopy(example1_regular_session)
        # 將 msg_002 改成與 msg_001 相同
        data["messages"][1]["message_id"] = "msg_001"
        errors = validate_reading(data)
        assert any("message_id" in e.field and "重複" in e.message for e in errors)

    def test_index_must_start_at_zero(self, example1_regular_session):
        """2.4 決定 3：index 必須從 0 起算。"""
        data = copy.deepcopy(example1_regular_session)
        data["messages"][0]["index"] = 1
        data["messages"][1]["index"] = 2
        errors = validate_reading(data)
        assert any("index" in e.field and "0" in e.message for e in errors)

    def test_index_must_be_strictly_consecutive(self, example1_regular_session):
        """2.4 決定 3：index 必須連續且嚴格遞增（無跳號）。"""
        data = copy.deepcopy(example1_regular_session)
        data["messages"][1]["index"] = 5  # 跳號
        errors = validate_reading(data)
        assert any("index" in e.field and ("連續" in e.message or "跳號" in e.message or "遞增" in e.message) for e in errors)


# ===========================================================================
# 3. messages_before 測試（接續點抽取邏輯）
# ===========================================================================

class TestMessagesBefore:
    """測試 messages_before 依接續點定位、邊界包含、錯誤處理與 reverted 過濾。"""

    def test_messages_before_includes_target_message(self, example1_regular_session):
        """messages_before 必須包含目標 message_id 該則訊息（index <= target）。"""
        sha = example1_regular_session["snapshot_sha256"]
        msgs = messages_before(example1_regular_session, "msg_001", snapshot_sha256=sha)
        assert len(msgs) == 1
        assert msgs[0]["message_id"] == "msg_001"

        msgs_all = messages_before(example1_regular_session, "msg_002", snapshot_sha256=sha)
        assert len(msgs_all) == 2
        assert [m["message_id"] for m in msgs_all] == ["msg_001", "msg_002"]

    def test_messages_before_non_existent_id_raises_key_error(
        self, example1_regular_session
    ):
        """若找不到指定的 message_id，必須丟出 KeyError。"""
        sha = example1_regular_session["snapshot_sha256"]
        with pytest.raises(KeyError):
            messages_before(example1_regular_session, "msg_non_existent_999", snapshot_sha256=sha)

    def test_messages_before_snapshot_sha256_mismatch_raises_value_error(
        self, example1_regular_session
    ):
        """2.4 決定 1：snapshot_sha256 不符時 raise ValueError。"""
        with pytest.raises(ValueError, match="快照雜湊不符"):
            messages_before(
                example1_regular_session,
                "msg_001",
                snapshot_sha256="0000000000000000000000000000000000000000000000000000000000000000",
            )

    def test_messages_before_target_incomplete_raises_value_error(
        self, example2_session_with_subagents_and_reverts
    ):
        """2.4 決定 1：目標訊息 completed: false 時 raise ValueError（不得作為接續點）。"""
        sha = example2_session_with_subagents_and_reverts["snapshot_sha256"]
        # msg_013 是 completed: false 的訊息
        with pytest.raises(ValueError, match="未完成"):
            messages_before(
                example2_session_with_subagents_and_reverts,
                "msg_013",
                snapshot_sha256=sha,
            )

    def test_messages_before_target_reverted_raises_value_error(
        self, example2_session_with_subagents_and_reverts
    ):
        """2.4 決定 1：目標訊息 reverted: true 時 raise ValueError（不得默默過濾）。"""
        sha = example2_session_with_subagents_and_reverts["snapshot_sha256"]
        # msg_012 是 reverted: true 的訊息
        with pytest.raises(ValueError, match="撤銷"):
            messages_before(
                example2_session_with_subagents_and_reverts,
                "msg_012",
                snapshot_sha256=sha,
            )

    def test_messages_before_excludes_reverted_by_default(
        self, example2_session_with_subagents_and_reverts
    ):
        """預設 (include_reverted=False) 必須自動排除已撤銷 (reverted=True) 的前置訊息。"""
        sha = example2_session_with_subagents_and_reverts["snapshot_sha256"]
        msgs = messages_before(
            example2_session_with_subagents_and_reverts,
            "msg_014",
            snapshot_sha256=sha,
        )
        msg_ids = [m["message_id"] for m in msgs]
        assert msg_ids == ["msg_010", "msg_011", "msg_014"]
        assert "msg_012" not in msg_ids
        assert "msg_013" not in msg_ids

    def test_messages_before_includes_reverted_when_flag_true(
        self, example2_session_with_subagents_and_reverts
    ):
        """當 include_reverted=True 時，完整保留目標之前包含撤銷訊息的所有訊息。"""
        sha = example2_session_with_subagents_and_reverts["snapshot_sha256"]
        msgs = messages_before(
            example2_session_with_subagents_and_reverts,
            "msg_014",
            snapshot_sha256=sha,
            include_reverted=True,
        )
        msg_ids = [m["message_id"] for m in msgs]
        assert msg_ids == ["msg_010", "msg_011", "msg_012", "msg_013", "msg_014"]


# ===========================================================================
# 4. check_continuation 測試（交接單接續點檢查）
# ===========================================================================

class TestCheckContinuation:
    """測試 check_continuation 檢查交接單 continuation 是否合法有效。"""

    def test_check_continuation_valid(self, example1_regular_session):
        """合法接續點通過驗證，回傳空清單。"""
        continuation = {
            "snapshot_sha256": example1_regular_session["snapshot_sha256"],
            "message_id": "msg_001",
        }
        errors = check_continuation(example1_regular_session, continuation)
        assert errors == []

    def test_check_continuation_snapshot_mismatch(self, example1_regular_session):
        """快照不符時回報錯誤。"""
        continuation = {
            "snapshot_sha256": "f" * 64,
            "message_id": "msg_001",
        }
        errors = check_continuation(example1_regular_session, continuation)
        assert any("snapshot_sha256" in e.field for e in errors)

    def test_check_continuation_message_id_not_found(self, example1_regular_session):
        """指定之 message_id 不存在時回報錯誤。"""
        continuation = {
            "snapshot_sha256": example1_regular_session["snapshot_sha256"],
            "message_id": "non_existent_msg",
        }
        errors = check_continuation(example1_regular_session, continuation)
        assert any("message_id" in e.field for e in errors)

    def test_check_continuation_target_incomplete_rejected(
        self, example2_session_with_subagents_and_reverts
    ):
        """目標訊息 completed == False 時必須被拒絕。"""
        continuation = {
            "snapshot_sha256": example2_session_with_subagents_and_reverts["snapshot_sha256"],
            "message_id": "msg_013",  # completed: false
        }
        errors = check_continuation(example2_session_with_subagents_and_reverts, continuation)
        assert any("message_id" in e.field and ("尚未" in e.message or "未完成" in e.message or "completed" in e.message) for e in errors)


    def test_check_continuation_target_reverted_rejected(
        self, example2_session_with_subagents_and_reverts
    ):
        """目標訊息 reverted == True 時必須被拒絕。"""
        continuation = {
            "snapshot_sha256": example2_session_with_subagents_and_reverts["snapshot_sha256"],
            "message_id": "msg_012",  # reverted: true
        }
        errors = check_continuation(example2_session_with_subagents_and_reverts, continuation)
        assert any("message_id" in e.field and "撤銷" in e.message for e in errors)


# ===========================================================================
# 5. plain_text 測試（全文檢索純文字串接）
# ===========================================================================

class TestPlainText:
    """測試 plain_text 產生給全文搜尋索引使用之純文字字串。"""

    def test_plain_text_includes_text_and_summaries(self, example1_regular_session):
        """plain_text 必須串接 text 與工具呼叫之輸入/輸出摘要。"""
        text = plain_text(example1_regular_session)

        # 包含使用者提問文字
        assert "請檢視目前的連線池設定檔" in text
        # 包含助理回覆文字
        assert "好的，我先讀取連線池設定檔" in text
        # 包含工具輸入摘要 (input_summary)
        assert "config/database.json" in text
        # 包含工具輸出摘要 (output_summary)
        assert "pool_size" in text
        assert "max_overflow" in text

    def test_plain_text_excludes_reasoning_by_default(self, example1_regular_session):
        """2.4 決定 4：plain_text 預設不含 reasoning 推理文字。"""
        text = plain_text(example1_regular_session)
        assert "使用者欲知連線池是否足以支撐" not in text

    def test_plain_text_includes_reasoning_when_flag_true(self, example1_regular_session):
        """當 include_reasoning=True 時，plain_text 包含 reasoning 推理文字。"""
        text = plain_text(example1_regular_session, include_reasoning=True)
        assert "使用者欲知連線池是否足以支撐" in text

    def test_plain_text_includes_compaction_summary(
        self, example2_session_with_subagents_and_reverts
    ):
        """plain_text 必須包含 compaction 段落的 summary。"""
        text = plain_text(example2_session_with_subagents_and_reverts)
        assert "前情提要" in text
        assert "安全相容包裝" in text

    def test_plain_text_excludes_images_and_files(self, example1_regular_session):
        """plain_text 必須排除圖片與檔案段落的二進位或中繼資訊。"""
        data = copy.deepcopy(example1_regular_session)
        image_part = {
            "type": "image",
            "media_type": "image/png",
            "sha256": "unique_image_sha256_hash_12345",
            "size": 99999,
        }
        file_part = {
            "type": "file",
            "name": "secret_data.csv",
            "media_type": "text/csv",
            "sha256": "unique_file_sha256_hash_67890",
            "size": 54321,
        }
        data["messages"][0]["parts"].extend([image_part, file_part])

        text = plain_text(data)
        assert "unique_image_sha256_hash_12345" not in text
        assert "unique_file_sha256_hash_67890" not in text
        assert "image/png" not in text
        assert "99999" not in text

    def test_plain_text_excludes_reverted_by_default(
        self, example2_session_with_subagents_and_reverts
    ):
        """預設 (include_reverted=False) 必須排除 reverted 訊息中的內容。"""
        text = plain_text(example2_session_with_subagents_and_reverts)

        # 正常訊息文字
        assert "請啟動子代理" in text
        assert "更正：不要刪除檔案" in text

        # 撤銷訊息文字 (msg_012: 請立刻刪除那 14 個檔案；msg_013: 準備開始刪除)
        assert "請立刻刪除那 14 個檔案" not in text
        assert "準備開始刪除" not in text

    def test_plain_text_includes_reverted_when_flag_true(
        self, example2_session_with_subagents_and_reverts
    ):
        """當 include_reverted=True 時，必須一併納入被撤銷訊息中的文字。"""
        text = plain_text(
            example2_session_with_subagents_and_reverts,
            include_reverted=True,
        )
        assert "請立刻刪除那 14 個檔案" in text
        assert "準備開始刪除" in text
