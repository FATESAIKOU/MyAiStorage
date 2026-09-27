"""Smoke and unit tests for converters module (OpencodeConverter and registry)."""

import json
from pathlib import Path
import pytest

from aistorage.converters import (
    CONVERTERS,
    ConversionError,
    Converter,
    SessionFacts,
    get_converter,
)
from aistorage.converters.opencode import OpencodeConverter
from aistorage.reading import validate_reading

DATA_DIR = Path(__file__).parent / "data" / "converters" / "opencode"


def test_converter_registry_smoke():
    conv = get_converter("opencode")
    assert isinstance(conv, Converter)
    assert isinstance(conv, OpencodeConverter)
    assert conv.source == "opencode"

    with pytest.raises(KeyError, match="未註冊"):
        get_converter("nonexistent_source")


def test_opencode_converter_basic_golden_sample():
    raw_path = DATA_DIR / "basic.json"
    expected_path = DATA_DIR / "basic.reading.json"

    assert raw_path.is_file(), f"找不到黃金樣本輸入: {raw_path}"
    assert expected_path.is_file(), f"找不到黃金樣本預期輸出: {expected_path}"

    conv = get_converter("opencode")

    # 1. 測試 facts
    facts = conv.facts(raw_path)
    assert isinstance(facts, SessionFacts)
    assert facts.title == "opencode 轉換器黃金測試樣本"
    assert facts.created_at == "2026-09-26T10:53:20Z"
    assert facts.updated_at == "2026-09-26T11:01:40Z"
    assert facts.archived_at == "2026-09-26T11:03:20Z"
    assert facts.last_message_at == "2026-09-26T10:58:25Z"
    assert facts.in_progress is False
    assert facts.message_ids == ("msg_001", "msg_002", "msg_003", "msg_004")

    # 2. 測試 child_session_ids
    children = conv.child_session_ids(raw_path)
    assert children == ("opencode:ses_sub_001",)

    # 3. 測試 convert（M1 & PM 決定 4: snapshot_sha256 自動由轉換器自檔案計算）
    converted = conv.convert(
        raw_path,
        session_id="opencode:ses_opencode_basic_001",
        parent_id=None,
    )

    # 驗證閱讀版結構符合規範
    errs = validate_reading(converted)
    assert errs == []

    # 4. 比對與預期黃金輸出完全一致
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert converted == expected


def test_opencode_compaction_and_summary_rules(tmp_path: Path):
    """H1-a: 測試 user 訊息上的 compaction part 被丟棄；assistant 的 info.summary is True 將 text 轉為 compaction。"""
    raw_data = {
        "info": {
            "id": "ses_compact",
            "time": {"created": 1790420000000},
        },
        "messages": [
            {
                "info": {
                    "id": "msg_u",
                    "role": "user",
                    "summary": {"diffs": []},  # H1-a: user 上即便有 summary 物件，也不得誤判為壓縮訊息
                    "time": {"created": 1790420000000},
                },
                "parts": [
                    {"type": "compaction", "auto": True},  # 邊界標記，丟棄
                    {"type": "text", "text": "使用者一般提問"},
                ],
            },
            {
                "info": {
                    "id": "msg_a",
                    "role": "assistant",
                    "summary": True,  # 嚴格布林 True
                    "time": {"created": 1790420005000, "completed": 1790420010000},
                },
                "parts": [
                    {"type": "reasoning", "text": "思考總結過程"},
                    {"type": "text", "text": "這是一段對話壓縮總結摘要"},
                ],
            },
        ],
    }
    raw_file = tmp_path / "compact.json"
    raw_file.write_text(json.dumps(raw_data), encoding="utf-8")

    conv = OpencodeConverter()
    res = conv.convert(raw_file, session_id="opencode:ses_compact")
    msgs = res["messages"]

    # user 訊息中 compaction 被過濾，僅保留 text
    assert len(msgs[0]["parts"]) == 1
    assert msgs[0]["parts"][0] == {"type": "text", "text": "使用者一般提問"}

    # assistant 訊息中 reasoning 保持，text 轉為 compaction
    assert len(msgs[1]["parts"]) == 2
    assert msgs[1]["parts"][0] == {"type": "reasoning", "text": "思考總結過程"}
    assert msgs[1]["parts"][1] == {"type": "compaction", "summary": "這是一段對話壓縮總結摘要"}


def test_opencode_revert_with_and_without_part_id(tmp_path: Path):
    """M2: 測試 revert 指標之 partID 規則與不存在目標之例外。"""
    conv = OpencodeConverter()

    # 1. 無 partID：target 訊息本身與之後均被標記為 reverted
    raw1 = {
        "info": {
            "id": "ses_rev",
            "time": {"created": 1790420000000},
            "revert": {"messageID": "m2"},
        },
        "messages": [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "1"}]},
            {"info": {"id": "m2", "role": "assistant", "time": {"created": 1790420005000}}, "parts": [{"type": "text", "text": "2"}]},
            {"info": {"id": "m3", "role": "user", "time": {"created": 1790420010000}}, "parts": [{"type": "text", "text": "3"}]},
        ],
    }
    f1 = tmp_path / "rev1.json"
    f1.write_text(json.dumps(raw1), encoding="utf-8")
    res1 = conv.convert(f1, session_id="opencode:ses_rev")
    assert res1["messages"][0]["reverted"] is False
    assert res1["messages"][1]["reverted"] is True
    assert res1["messages"][2]["reverted"] is True

    # 2. 有 partID：target 訊息本身 reverted=False，之後的訊息 reverted=True
    raw2 = dict(raw1)
    raw2["info"] = {"id": "ses_rev", "time": {"created": 1790420000000}, "revert": {"messageID": "m2", "partID": "p_sub"}}
    f2 = tmp_path / "rev2.json"
    f2.write_text(json.dumps(raw2), encoding="utf-8")
    res2 = conv.convert(f2, session_id="opencode:ses_rev")
    assert res2["messages"][0]["reverted"] is False
    assert res2["messages"][1]["reverted"] is False
    assert res2["messages"][2]["reverted"] is True

    # 3. 指標指到不存在之 messageID -> ConversionError
    raw3 = dict(raw1)
    raw3["info"] = {"id": "ses_rev", "time": {"created": 1790420000000}, "revert": {"messageID": "non_existent"}}
    f3 = tmp_path / "rev3.json"
    f3.write_text(json.dumps(raw3), encoding="utf-8")
    with pytest.raises(ConversionError, match="不存在於會話紀錄中"):
        conv.convert(f3, session_id="opencode:ses_rev")


def test_opencode_parent_id_cross_check(tmp_path: Path):
    """M4: parent_id 參數與原始紀錄交叉檢查。"""
    raw = {
        "info": {"id": "s1", "parentID": "parent_123", "time": {"created": 1790420000000}},
        "messages": [{"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "hi"}]}],
    }
    f = tmp_path / "parent.json"
    f.write_text(json.dumps(raw), encoding="utf-8")

    conv = OpencodeConverter()
    # 兩者吻合成功
    res = conv.convert(f, session_id="opencode:s1", parent_id="opencode:parent_123")
    assert res["parent_id"] == "opencode:parent_123"

    # 不一致時拋出 ConversionError
    with pytest.raises(ConversionError, match="不一致"):
        conv.convert(f, session_id="opencode:s1", parent_id="opencode:different_parent")


def test_opencode_long_input_summary_truncation(tmp_path: Path):
    """L: 工具摘要長度截斷以 code point 為準，上限 4,000 字元含 … 標記。"""
    long_str = "🌟" * 4005
    raw = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "m1", "role": "assistant", "time": {"created": 1790420000000, "completed": 1790420001000}},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": long_str, "output": long_str},
                    }
                ],
            }
        ],
    }
    f = tmp_path / "long.json"
    f.write_text(json.dumps(raw), encoding="utf-8")

    conv = OpencodeConverter()
    res = conv.convert(f, session_id="opencode:s1")
    tool_part = res["messages"][0]["parts"][0]
    assert len(tool_part["input_summary"]) == 4000
    assert tool_part["input_summary"].endswith("…")
    assert len(tool_part["output_summary"]) == 4000
    assert tool_part["output_summary"].endswith("…")


def test_opencode_strict_validation_errors(tmp_path: Path):
    """H2: 結構不符或必要欄位缺失拋出 ConversionError。"""
    conv = OpencodeConverter()

    # 1. 訊息缺少 id
    raw1 = {"info": {"id": "s1", "time": {"created": 1790420000000}}, "messages": [{"info": {"role": "user", "time": {"created": 1790420000000}}, "parts": []}]}
    f1 = tmp_path / "err1.json"
    f1.write_text(json.dumps(raw1), encoding="utf-8")
    with pytest.raises(ConversionError, match="缺少 id"):
        conv.convert(f1, session_id="opencode:s1")

    # 2. 角色不認得
    raw2 = {"info": {"id": "s1", "time": {"created": 1790420000000}}, "messages": [{"info": {"id": "m1", "role": "bot", "time": {"created": 1790420000000}}, "parts": []}]}
    f2 = tmp_path / "err2.json"
    f2.write_text(json.dumps(raw2), encoding="utf-8")
    with pytest.raises(ConversionError, match="角色無效"):
        conv.convert(f2, session_id="opencode:s1")
