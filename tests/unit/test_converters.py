"""Unit tests for aistorage.converters module (OpencodeConverter and registry).

Specifications:
- docs/impl/group3-modules.md §5 (轉換器層)
- schemas/reading-version.md 轉換對應表
- review-g3b.md & review-g3b-recheck.md (compaction 邊界、state.error、附件 data URL 與非 data URL、不捏造雜湊、in-progress 與 aborted、嚴格型別)
"""

from __future__ import annotations

import hashlib
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


def test_converter_registry():
    """驗證轉換器登錄表與 get_converter。"""
    conv = get_converter("opencode")
    assert isinstance(conv, Converter)
    assert isinstance(conv, OpencodeConverter)
    assert conv.source == "opencode"

    with pytest.raises(KeyError, match="未註冊"):
        get_converter("unknown_source")


def test_opencode_converter_golden_full():
    """驗證 opencode 完整黃金樣本 (golden_full.json)。"""
    raw_path = DATA_DIR / "golden_full.json"
    expected_path = DATA_DIR / "golden_full.reading.json"

    assert raw_path.is_file(), f"找不到黃金樣本輸入: {raw_path}"
    assert expected_path.is_file(), f"找不到黃金樣本預期輸出: {expected_path}"

    conv = get_converter("opencode")

    # 1. facts
    facts = conv.facts(raw_path)
    assert isinstance(facts, SessionFacts)
    assert facts.title == "Opencode Converter Golden Sample"
    assert facts.created_at == "2026-09-26T10:53:20Z"
    assert facts.updated_at == "2026-09-26T11:03:20Z"
    assert facts.archived_at == "2026-09-26T11:06:40Z"
    assert facts.last_message_at == "2026-09-26T10:58:30Z"
    assert facts.archived_ms == 1790420800000
    assert facts.last_message_ms == 1790420310000
    assert facts.in_progress is False
    assert facts.message_ids == ("msg_001", "msg_002", "msg_003", "msg_004", "msg_005")

    # 2. child_session_ids
    children = conv.child_session_ids(raw_path)
    assert children == ("opencode:ses_child_001",)

    # 3. convert & validate_reading
    converted = conv.convert(raw_path, session_id="opencode:ses_golden_001")
    errs = validate_reading(converted)
    assert errs == [], f"Reading validation errors: {errs}"

    # 4. 比對與預期黃金輸出完全一致
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert converted == expected


def test_compaction_only_assistant_summary_true(tmp_path: Path):
    """H1-a: 驗證 user 訊息帶 summary 物件不誤轉 compaction；僅 assistant 帶 info.summary is True 轉為 compaction。"""
    raw_file = tmp_path / "compaction_test.json"
    data = {
        "info": {
            "id": "ses_c1",
            "time": {"created": 1790420000000},
        },
        "messages": [
            {
                "info": {
                    "id": "msg_u",
                    "role": "user",
                    "summary": {"diffs": []},  # user 訊息上的 summary 物件
                    "time": {"created": 1790420000000},
                },
                "parts": [
                    {"type": "compaction", "auto": True},  # 壓縮標記，丟棄
                    {"type": "text", "text": "使用者提問"},
                ],
            },
            {
                "info": {
                    "id": "msg_a",
                    "role": "assistant",
                    "summary": True,  # assistant 摘要訊息
                    "time": {"created": 1790420010000, "completed": 1790420020000},
                },
                "parts": [
                    {"type": "text", "text": "這是上一輪的對話摘要內容。"},
                ],
            },
        ],
    }
    raw_file.write_text(json.dumps(data), encoding="utf-8")

    conv = get_converter("opencode")
    out = conv.convert(raw_file, session_id="opencode:ses_c1")

    # msg_u 的 text 依然是 text part（不可變成 compaction）
    msg_u = out["messages"][0]
    assert len(msg_u["parts"]) == 1
    assert msg_u["parts"][0]["type"] == "text"
    assert msg_u["parts"][0]["text"] == "使用者提問"

    # msg_a 的 text 轉為 compaction part
    msg_a = out["messages"][1]
    assert len(msg_a["parts"]) == 1
    assert msg_a["parts"][0]["type"] == "compaction"
    assert msg_a["parts"][0]["summary"] == "這是上一輪的對話摘要內容。"


def test_tool_error_from_state_error(tmp_path: Path):
    """H1-b: 驗證工具錯誤時，錯誤訊息取自 state.error 而非 state.output。"""
    raw_file = tmp_path / "tool_error.json"
    data = {
        "info": {
            "id": "ses_err",
            "time": {"created": 1790420000000},
        },
        "messages": [
            {
                "info": {
                    "id": "msg_a",
                    "role": "assistant",
                    "time": {"created": 1790420000000, "completed": 1790420010000},
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "read_file",
                        "state": {
                            "status": "error",
                            "input": "non_exist.py",
                            "error": "CustomError: File not found on disk",
                        },
                    }
                ],
            }
        ],
    }
    raw_file.write_text(json.dumps(data), encoding="utf-8")

    conv = get_converter("opencode")
    out = conv.convert(raw_file, session_id="opencode:ses_err")
    tool_part = out["messages"][0]["parts"][0]
    assert tool_part["type"] == "tool_call"
    assert "[ERROR]" in tool_part["output_summary"]
    assert "CustomError: File not found on disk" in tool_part["output_summary"]


def test_attachment_data_url_and_non_data_url(tmp_path: Path):
    """H1-c: 驗證 data: URL 解析真實雜湊與大小，非 data: URL 轉文字標示且絕不捏造雜湊。"""
    raw_file = tmp_path / "attachments.json"
    # data:text/plain;base64,dGVzdCBkYXRh -> "test data" (9 bytes, sha256: 916f0027a575074ce72a331777c3478d6513f786a591bd8b5f0a4023c915f013)
    raw_data = {
        "info": {"id": "ses_att", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "msg_u", "role": "user", "time": {"created": 1790420000000}},
                "parts": [
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "filename": "hello.txt",
                        "url": "data:text/plain;base64,dGVzdCBkYXRh",
                    },
                    {
                        "type": "file",
                        "mime": "application/zip",
                        "filename": "archive.zip",
                        "url": "https://example.com/download/archive.zip",  # 非 data: URL
                    },
                ],
            }
        ],
    }
    raw_file.write_text(json.dumps(raw_data), encoding="utf-8")

    conv = get_converter("opencode")
    out = conv.convert(raw_file, session_id="opencode:ses_att")
    parts = out["messages"][0]["parts"]

    # 1. data: URL -> 計算出真實雜湊與大小
    assert parts[0]["type"] == "file"
    assert parts[0]["media_type"] == "text/plain"
    assert parts[0]["name"] == "hello.txt"
    assert parts[0]["size"] == 9
    assert parts[0]["sha256"] == "916f0027a575074ce72a331777c3478d6513f786a591bd892da1a577bf2335f9"

    # 2. 非 data: URL -> 轉為文字標示，絕不放 0*64 假雜湊
    assert parts[1]["type"] == "text"
    assert "[附件：archive.zip application/zip，內容不在匯出中]" in parts[1]["text"]


def test_in_progress_and_aborted(tmp_path: Path):
    """M3 / R6: 測試生成中（未帶 time.completed）與使用者中止（MessageAbortedError）之判定。"""
    conv = get_converter("opencode")

    # 1. 生成中 (in_progress = True)
    in_prog_file = tmp_path / "in_progress.json"
    in_prog_data = {
        "info": {"id": "ses_p1", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "msg_u", "role": "user", "time": {"created": 1790420000000}},
                "parts": [{"type": "text", "text": "問句"}],
            },
            {
                "info": {
                    "id": "msg_a",
                    "role": "assistant",
                    "time": {"created": 1790420010000},  # 缺少 completed
                },
                "parts": [{"type": "text", "text": "生成了一半的回答..."}],
            },
        ],
    }
    in_prog_file.write_text(json.dumps(in_prog_data), encoding="utf-8")

    facts_prog = conv.facts(in_prog_file)
    assert facts_prog.in_progress is True
    out_prog = conv.convert(in_prog_file, session_id="opencode:ses_p1")
    assert out_prog["in_progress"] is True
    assert out_prog["messages"][1]["completed"] is False

    # 2. 使用者中止生成 (in_progress 應判定為 False，非運作中)
    abort_file = tmp_path / "aborted.json"
    abort_data = {
        "info": {"id": "ses_p2", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "msg_u", "role": "user", "time": {"created": 1790420000000}},
                "parts": [{"type": "text", "text": "問句"}],
            },
            {
                "info": {
                    "id": "msg_a",
                    "role": "assistant",
                    "time": {"created": 1790420010000},
                    "error": {"name": "MessageAbortedError", "message": "User aborted"},
                },
                "parts": [{"type": "text", "text": "生成了一半被中止"}],
            },
        ],
    }
    abort_file.write_text(json.dumps(abort_data), encoding="utf-8")

    facts_abort = conv.facts(abort_file)
    assert facts_abort.in_progress is False
    out_abort = conv.convert(abort_file, session_id="opencode:ses_p2")
    assert out_abort["in_progress"] is False


def test_strictness_and_conversion_error(tmp_path: Path):
    """H2: 驗證嚴格型別檢查，缺少必要欄位或非法角色時拋出 ConversionError。"""
    conv = get_converter("opencode")

    # 1. 缺少訊息 id
    f1 = tmp_path / "no_id.json"
    f1.write_text(json.dumps({
        "info": {"id": "s1", "time": {"created": 1790420000000}},
        "messages": [{"info": {"role": "user", "time": {"created": 1790420000000}}, "parts": []}],
    }))
    with pytest.raises(ConversionError):
        conv.convert(f1, session_id="opencode:s1")

    # 2. 非法角色
    f2 = tmp_path / "bad_role.json"
    f2.write_text(json.dumps({
        "info": {"id": "s2", "time": {"created": 1790420000000}},
        "messages": [{"info": {"id": "m1", "role": "bot_unknown", "time": {"created": 1790420000000}}, "parts": []}],
    }))
    with pytest.raises(ConversionError):
        conv.convert(f2, session_id="opencode:s2")

    # 3. 缺少 time.created
    f3 = tmp_path / "no_time.json"
    f3.write_text(json.dumps({
        "info": {"id": "s3", "time": {"created": 1790420000000}},
        "messages": [{"info": {"id": "m1", "role": "user", "time": {}}, "parts": []}],
    }))
    with pytest.raises(ConversionError):
        conv.convert(f3, session_id="opencode:s3")

    # 4. 未支援的段落型態轉為文字標示，明確丟棄型態（step-start, patch）則丟棄
    f4 = tmp_path / "parts_test.json"
    f4.write_text(json.dumps({
        "info": {"id": "s4", "time": {"created": 1790420000000}},
        "messages": [{
            "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
            "parts": [
                {"type": "step-start"},  # DROP
                {"type": "patch"},       # DROP
                {"type": "custom_agent_future_part"},  # 未支援型態 -> 轉文字標示
            ],
        }],
    }))
    out = conv.convert(f4, session_id="opencode:s4")
    m_parts = out["messages"][0]["parts"]
    assert len(m_parts) == 1
    assert m_parts[0]["type"] == "text"
    assert "[未支援的段落型態：custom_agent_future_part]" in m_parts[0]["text"]


def test_revert_rules(tmp_path: Path):
    """M2 / 1.7c: 驗證 info.revert 標記與無效 revert 拋錯。"""
    conv = get_converter("opencode")

    # 1. 正常 revert 到 msg_2，其後的 msg_3 標為 reverted=True
    f_rev = tmp_path / "revert_ok.json"
    f_rev.write_text(json.dumps({
        "info": {
            "id": "s_rev",
            "revert": {"messageID": "msg_2"},
            "time": {"created": 1790420000000},
        },
        "messages": [
            {"info": {"id": "msg_1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "1"}]},
            {"info": {"id": "msg_2", "role": "assistant", "time": {"created": 1790420010000, "completed": 1790420015000}}, "parts": [{"type": "text", "text": "2"}]},
            {"info": {"id": "msg_3", "role": "user", "time": {"created": 1790420020000}}, "parts": [{"type": "text", "text": "3"}]},
        ],
    }))
    out = conv.convert(f_rev, session_id="opencode:s_rev")
    msgs = out["messages"]
    assert msgs[0]["reverted"] is False
    assert msgs[1]["reverted"] is True
    assert msgs[2]["reverted"] is True

    # 2. 帶 partID 的 revert：該則訊息保留有效不標記 reverted（自 partID 起丟棄後續段落），自下一則起標記 reverted
    f_rev_part = tmp_path / "revert_part.json"
    f_rev_part.write_text(json.dumps({
        "info": {
            "id": "s_rev_part",
            "revert": {"messageID": "msg_2", "partID": "prt_2b"},
            "time": {"created": 1790420000000},
        },
        "messages": [
            {"info": {"id": "msg_1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "1"}]},
            {
                "info": {"id": "msg_2", "role": "assistant", "time": {"created": 1790420010000, "completed": 1790420015000}},
                "parts": [
                    {"id": "prt_2a", "type": "text", "text": "keep this"},
                    {"id": "prt_2b", "type": "text", "text": "drop this"},
                ],
            },
            {"info": {"id": "msg_3", "role": "user", "time": {"created": 1790420020000}}, "parts": [{"type": "text", "text": "3"}]},
        ],
    }))
    out_part = conv.convert(f_rev_part, session_id="opencode:s_rev_part")
    msgs_part = out_part["messages"]
    assert msgs_part[0]["reverted"] is False
    assert msgs_part[1]["reverted"] is False
    assert msgs_part[2]["reverted"] is True
    # 驗證 msg_2 中 prt_2b 之後的段落被丟棄
    assert len(msgs_part[1]["parts"]) == 1
    assert msgs_part[1]["parts"][0]["text"] == "keep this"

    # 2. 指向不存在之 messageID 必須拋出 ConversionError
    f_bad_rev = tmp_path / "revert_bad.json"
    f_bad_rev.write_text(json.dumps({
        "info": {
            "id": "s_bad_rev",
            "revert": {"messageID": "non_existent_msg_id"},
            "time": {"created": 1790420000000},
        },
        "messages": [
            {"info": {"id": "msg_1", "role": "user", "time": {"created": 1790420000000}}, "parts": []},
        ],
    }))
    with pytest.raises(ConversionError):
        conv.convert(f_bad_rev, session_id="opencode:s_bad_rev")


def test_facts_millisecond_accuracy(tmp_path: Path):
    """R3: 驗證 facts 之 archived_ms 與 last_message_ms 在同一秒內仍能正確精準比較。"""
    conv = get_converter("opencode")
    f = tmp_path / "same_sec.json"
    # 同一秒：封存 1000 ms，最新訊息 1500 ms（後者較晚，應為運作中）
    f.write_text(json.dumps({
        "info": {
            "id": "s_sec",
            "time": {
                "created": 1790420000000,
                "archived": 1790420001000,
            },
        },
        "messages": [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420001500}}, "parts": [{"type": "text", "text": "new"}]},
        ],
    }))
    facts = conv.facts(f)
    assert facts.archived_ms == 1790420001000
    assert facts.last_message_ms == 1790420001500
    assert facts.last_message_ms > facts.archived_ms


def test_truncation_boundary_4000(tmp_path: Path):
    """L: 驗證工具輸入、輸出截斷剛好在 4,000 code point（含結尾 …）。"""
    conv = get_converter("opencode")
    f = tmp_path / "long_tool.json"
    long_input = "A" * 5000
    long_output = "B" * 6000

    f.write_text(json.dumps({
        "info": {"id": "s_long", "time": {"created": 1790420000000}},
        "messages": [
            {
                "info": {"id": "m1", "role": "assistant", "time": {"created": 1790420000000, "completed": 1790420010000}},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "large_tool",
                        "state": {
                            "status": "success",
                            "input": long_input,
                            "output": long_output,
                        },
                    }
                ],
            }
        ],
    }))
    out = conv.convert(f, session_id="opencode:s_long")
    tc = out["messages"][0]["parts"][0]
    assert len(tc["input_summary"]) == 4000
    assert tc["input_summary"].endswith("…")
    assert len(tc["output_summary"]) == 4000
    assert tc["output_summary"].endswith("…")


def test_data_url_variations_and_prune(tmp_path: Path):
    """R4 / 1.7d: 測試 percent-encoded data: URL、非法 base64、mime 不一致拋錯，以及 prune 標記忽略。"""
    conv = get_converter("opencode")

    # 1. percent-encoded data: URL (非 base64)
    f_pct = tmp_path / "pct.json"
    f_pct.write_text(json.dumps({
        "info": {"id": "s_pct", "time": {"created": 1790420000000}},
        "messages": [{
            "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
            "parts": [{
                "type": "file",
                "mime": "text/plain",
                "filename": "hello.txt",
                "url": "data:text/plain,hello%20world",
            }],
        }],
    }))
    out_pct = conv.convert(f_pct, session_id="opencode:s_pct")
    p0 = out_pct["messages"][0]["parts"][0]
    assert p0["type"] == "file"
    assert p0["sha256"] == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert p0["size"] == 11

    # 2. 非法 base64 字元拋出 ConversionError
    f_bad_b64 = tmp_path / "bad_b64.json"
    f_bad_b64.write_text(json.dumps({
        "info": {"id": "s_bad_b64", "time": {"created": 1790420000000}},
        "messages": [{
            "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
            "parts": [{
                "type": "file",
                "mime": "text/plain",
                "filename": "bad.txt",
                "url": "data:text/plain;base64,@@@invalid_base64!!!",
            }],
        }],
    }))
    with pytest.raises(ConversionError):
        conv.convert(f_bad_b64, session_id="opencode:s_bad_b64")

    # 3. mime 與 data: URL header 不一致拋出 ConversionError
    f_mismatch_mime = tmp_path / "mismatch_mime.json"
    f_mismatch_mime.write_text(json.dumps({
        "info": {"id": "s_mismatch", "time": {"created": 1790420000000}},
        "messages": [{
            "info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}},
            "parts": [{
                "type": "file",
                "mime": "text/plain",
                "filename": "mismatch.txt",
                "url": "data:image/png;base64,aGVsbG8=",
            }],
        }],
    }))
    with pytest.raises(ConversionError):
        conv.convert(f_mismatch_mime, session_id="opencode:s_mismatch")

    # 4. prune 標記 (state.time.compacted) 忽略，內容照常輸出
    f_prune = tmp_path / "prune.json"
    f_prune.write_text(json.dumps({
        "info": {"id": "s_prune", "time": {"created": 1790420000000}},
        "messages": [{
            "info": {"id": "m1", "role": "assistant", "time": {"created": 1790420000000, "completed": 1790420010000}},
            "parts": [{
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "success",
                    "input": "echo prune_test",
                    "output": "prune_test",
                    "time": {
                        "created": 1790420001000,
                        "completed": 1790420002000,
                        "compacted": 1790420050000,  # prune 標記
                    },
                },
            }],
        }],
    }))
    out_prune = conv.convert(f_prune, session_id="opencode:s_prune")
    assert out_prune["messages"][0]["parts"][0]["type"] == "tool_call"

