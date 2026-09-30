"""Smoke and unit tests for converters module (OpencodeConverter and registry)."""

import hashlib
import json
from pathlib import Path
import urllib.parse
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
    """驗證 opencode 轉換器之黃金樣本測試。

    型別出處依據 docs/spike/evidence/1.7k-opencode-types.md，
    涵蓋 text, reasoning, tool, file (RFC 2397 base64 data URL 及外部 URL),
    image, compaction, revert, sub-agent task 等完整型態。
    """
    raw_path = DATA_DIR / "basic.json"
    expected_path = DATA_DIR / "basic.reading.json"

    assert raw_path.is_file(), f"找不到黃金樣本輸入: {raw_path}"
    assert expected_path.is_file(), f"找不到黃金樣本預期輸出: {expected_path}"

    conv = get_converter("opencode")

    # 1. 測試 facts（含 R3 毫秒精度欄位）
    facts = conv.facts(raw_path)
    assert isinstance(facts, SessionFacts)
    assert facts.title == "opencode 轉換器黃金測試樣本"
    assert facts.created_at == "2026-09-26T10:53:20Z"
    assert facts.updated_at == "2026-09-26T11:01:40Z"
    assert facts.archived_at == "2026-09-26T11:03:20Z"
    assert facts.last_message_at == "2026-09-26T10:58:25Z"
    assert facts.archived_ms == 1790420600000
    assert facts.last_message_ms == 1790420305000
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
    """M2 & R5: 測試 revert 指標之 partID 規則、partID 之後的 part 丟棄，與不存在目標之例外。"""
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

    # 2. R5: 有 partID：target 訊息本身 reverted=False，自 partID 起丟棄後續段落，下一則起標記 reverted
    raw2 = {
        "info": {
            "id": "ses_rev",
            "time": {"created": 1790420000000},
            "revert": {"messageID": "m2", "partID": "p2"},
        },
        "messages": [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "1"}]},
            {
                "info": {"id": "m2", "role": "assistant", "time": {"created": 1790420005000, "completed": 1790420006000}},
                "parts": [
                    {"id": "p1", "type": "text", "text": "kept_part"},
                    {"id": "p2", "type": "text", "text": "reverted_part"},
                    {"id": "p3", "type": "text", "text": "also_reverted"},
                ],
            },
            {"info": {"id": "m3", "role": "user", "time": {"created": 1790420010000}}, "parts": [{"type": "text", "text": "3"}]},
        ],
    }
    f2 = tmp_path / "rev2.json"
    f2.write_text(json.dumps(raw2), encoding="utf-8")
    res2 = conv.convert(f2, session_id="opencode:ses_rev")
    assert res2["messages"][0]["reverted"] is False
    assert res2["messages"][1]["reverted"] is False
    # R5: m2 僅保留 p1，p2 與 p3 被丟棄
    assert len(res2["messages"][1]["parts"]) == 1
    assert res2["messages"][1]["parts"][0] == {"type": "text", "text": "kept_part"}
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


def test_opencode_facts_same_second_millisecond_difference(tmp_path: Path):
    """R3: 封存時間與新訊息在同一秒、但新訊息比封存時間晚 1 ms 時，毫秒精度欄位應正確區分。"""
    # 封存時間：1790420000500 ms (2026-09-26T10:53:20Z)
    # 新訊息完成時間：1790420000501 ms (同一秒內，差 1 ms)
    raw = {
        "info": {
            "id": "ses_same_sec",
            "time": {
                "created": 1790420000000,
                "archived": 1790420000500,
            },
        },
        "messages": [
            {
                "info": {
                    "id": "msg_001",
                    "role": "user",
                    "time": {"created": 1790420000000},
                },
                "parts": [{"type": "text", "text": "hello"}],
            },
            {
                "info": {
                    "id": "msg_002",
                    "role": "assistant",
                    "time": {
                        "created": 1790420000100,
                        "completed": 1790420000501,  # 晚 1 ms
                    },
                },
                "parts": [{"type": "text", "text": "world"}],
            },
        ],
    }
    f = tmp_path / "same_sec.json"
    f.write_text(json.dumps(raw), encoding="utf-8")

    conv = OpencodeConverter()
    facts = conv.facts(f)

    # 顯示用字串在同一秒被截斷成相同字串
    assert facts.archived_at == facts.last_message_at == "2026-09-26T10:53:20Z"

    # 但毫秒欄位精確記錄真實時間戳，3.9 判斷式 last_message_ms > archived_ms 成立（代表封存後有新訊息，應為 running）
    assert facts.archived_ms == 1790420000500
    assert facts.last_message_ms == 1790420000501
    assert facts.last_message_ms > facts.archived_ms


def test_opencode_data_url_non_base64_and_invalid(tmp_path: Path):
    """R4: RFC 2397 data: URL 測試：非 base64 百分比編碼、不合法 base64 字元與 MIME 不一致。"""
    conv = OpencodeConverter()

    # 1. 非 base64 百分比編碼 data URL
    raw_text = "Hello, World! 繁體中文測試"
    encoded_text = urllib.parse.quote(raw_text)
    raw1 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
                "parts": [
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "filename": "hello.txt",
                        "url": f"data:text/plain,{encoded_text}",
                    }
                ],
            }
        ],
    }
    f1 = tmp_path / "data_pct.json"
    f1.write_text(json.dumps(raw1), encoding="utf-8")

    res1 = conv.convert(f1, session_id="opencode:s1")
    file_part = res1["messages"][0]["parts"][0]
    expected_bytes = raw_text.encode("utf-8")
    assert file_part["type"] == "file"
    assert file_part["media_type"] == "text/plain"
    assert file_part["size"] == len(expected_bytes)
    assert file_part["sha256"] == hashlib.sha256(expected_bytes).hexdigest().lower()
    assert file_part["name"] == "hello.txt"

    # 2. 含有不合法字元的 base64 (validate=True 拋出 ConversionError)
    raw2 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
                "parts": [
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "filename": "bad.txt",
                        "url": "data:text/plain;base64,invalid!@#$characters",
                    }
                ],
            }
        ],
    }
    f2 = tmp_path / "bad_b64.json"
    f2.write_text(json.dumps(raw2), encoding="utf-8")
    with pytest.raises(ConversionError, match="Base64 解碼失敗"):
        conv.convert(f2, session_id="opencode:s1")

    # 3. data URL 之 header MIME 與 part.mime 不一致時拋出 ConversionError
    raw3 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
                "parts": [
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "filename": "mismatch.txt",
                        "url": "data:image/png;base64,eyJrZXkiOiAidmFsdWUifQ==",
                    }
                ],
            }
        ],
    }
    f3 = tmp_path / "mismatch_mime.json"
    f3.write_text(json.dumps(raw3), encoding="utf-8")
    with pytest.raises(ConversionError, match="不一致"):
        conv.convert(f3, session_id="opencode:s1")


def test_opencode_in_progress_true_when_assistant_incomplete(tmp_path: Path):
    """R6: 當最新訊息為 assistant 且 completed 尚未設定（且未中止）時，in_progress 為 True。"""
    raw = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "hi"}]},
            {
                "info": {
                    "id": "m2",
                    "role": "assistant",
                    "time": {"created": 1790420005000},  # 缺少 completed
                },
                "parts": [{"type": "text", "text": "generating..."}],
            },
        ],
    }
    f = tmp_path / "in_progress.json"
    f.write_text(json.dumps(raw), encoding="utf-8")

    conv = OpencodeConverter()
    facts = conv.facts(f)
    assert facts.in_progress is True

    res = conv.convert(f, session_id="opencode:s1")
    assert res["in_progress"] is True
    assert res["messages"][-1]["completed"] is False


def test_opencode_in_progress_false_when_assistant_aborted(tmp_path: Path):
    """R6: 當最新訊息為 assistant 且帶有 MessageAbortedError 時，判定為已中止（in_progress 為 False）。"""
    raw = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "hi"}]},
            {
                "info": {
                    "id": "m2",
                    "role": "assistant",
                    "time": {"created": 1790420005000},  # 缺少 completed
                    "error": {
                        "name": "MessageAbortedError",
                        "message": "Message was aborted by user",
                    },
                },
                "parts": [{"type": "text", "text": "aborted text"}],
            },
        ],
    }
    f = tmp_path / "aborted.json"
    f.write_text(json.dumps(raw), encoding="utf-8")

    conv = OpencodeConverter()
    facts = conv.facts(f)
    assert facts.in_progress is False

    res = conv.convert(f, session_id="opencode:s1")
    assert res["in_progress"] is False
    assert res["messages"][-1]["completed"] is False


def test_opencode_parts_and_aliases_strict_validation(tmp_path: Path):
    """R2, R7, R8: parts 非 list、part 非 dict、file 缺少必填 mime、訊息使用非真實欄位別名時嚴格拋出 ConversionError。"""
    conv = OpencodeConverter()

    # 1. R8: parts 不是 list
    raw1 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [{"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": "not_a_list"}],
    }
    f1 = tmp_path / "non_list_parts.json"
    f1.write_text(json.dumps(raw1), encoding="utf-8")
    with pytest.raises(ConversionError, match="缺少 parts 清單"):
        conv.convert(f1, session_id="opencode:s1")

    # 2. R8: part 不是 dict
    raw2 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [{"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [123]}],
    }
    f2 = tmp_path / "non_dict_part.json"
    f2.write_text(json.dumps(raw2), encoding="utf-8")
    with pytest.raises(ConversionError, match="part 必須是 JSON 字典"):
        conv.convert(f2, session_id="opencode:s1")

    # 3. R2: file part 缺少必填之 mime 欄位（不得捏造預設值）
    raw3 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
                "parts": [{"type": "file", "url": "data:,abc"}],
            }
        ],
    }
    f3 = tmp_path / "missing_mime.json"
    f3.write_text(json.dumps(raw3), encoding="utf-8")
    with pytest.raises(ConversionError, match="缺少必填之 mime 欄位"):
        conv.convert(f3, session_id="opencode:s1")

    # 4. R2: 訊息 id 僅存在於頂層而缺少 info.id
    raw4 = {
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "id": "top_level_id_only",  # 缺少 info.id
                "info": {"role": "user", "time": {"created": 1790420000000}},
                "parts": [{"type": "text", "text": "test"}],
            }
        ],
    }
    f4 = tmp_path / "top_level_id.json"
    f4.write_text(json.dumps(raw4), encoding="utf-8")
    with pytest.raises(ConversionError, match="缺少 id 欄位"):
        conv.convert(f4, session_id="opencode:s1")

    # 5. R7: child_session_ids 檔案損毀拋出 ConversionError
    bad_json_file = tmp_path / "bad.json"
    bad_json_file.write_text("{broken json", encoding="utf-8")
    with pytest.raises(ConversionError, match="JSON 解析失敗"):
        conv.child_session_ids(bad_json_file)
