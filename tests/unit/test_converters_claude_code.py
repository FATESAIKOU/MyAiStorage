"""Acceptance tests for Claude Code Converter (Task 3.6).

Adheres strictly to:
- docs/impl/group3-modules.md §5, §8.2
- schemas/reading-version.md Claude Code conversion mapping table:
  - Latest leaf node expansion along main line
  - Abandoned branches marked as reverted: true
  - tool_use (assistant) + tool_result (user) paired into single tool_call
  - thinking block -> reasoning
  - compact_boundary -> compaction (summary: "")
  - Unknown record types handled gracefully as text ([未支援的紀錄型態：<type>])
  - sidechain records excluded from parent reading messages, recognized in child_session_ids
  - Base64 image and document computing verified sha256 and size
  - Non-base64 attachments converted to text note without fabricated hashes
  - 4,000 code-point summary truncation with …
  - in_progress and completed flags
  - ConversionError on structural corruptions
"""

import base64
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
from aistorage.converters.claude_code import ClaudeCodeConverter
from aistorage.reading import validate_reading


DATA_DIR = Path(__file__).parent / "data" / "converters" / "claude-code"
SESSION = "33333333-3333-4333-8333-000000000000"
SESSION_ID = f"claude-code:{SESSION}"


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def _user(uuid: str, parent: str | None, ts: str, content, **extra) -> dict:
    rec = {
        "parentUuid": parent,
        "isSidechain": False,
        "sessionId": SESSION,
        "type": "user",
        "message": {"role": "user", "content": content},
        "uuid": uuid,
        "timestamp": ts,
    }
    rec.update(extra)
    return rec


def _assistant(uuid: str, parent: str | None, ts: str, content, stop="end_turn", **extra) -> dict:
    rec = {
        "parentUuid": parent,
        "isSidechain": False,
        "sessionId": SESSION,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-5-20250929",
            "content": content,
            "stop_reason": stop,
        },
        "uuid": uuid,
        "timestamp": ts,
    }
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# 1. Golden Full Sample End-to-End Test
# ---------------------------------------------------------------------------


def test_claude_code_converter_golden_full():
    """驗證完整黃金樣本 (golden_full.jsonl) 逐欄比對與驗證規範相符。"""
    raw_path = DATA_DIR / "golden_full.jsonl"
    expected_path = DATA_DIR / "golden_full.reading.json"

    assert raw_path.is_file(), f"找不到黃金樣本輸入: {raw_path}"
    assert expected_path.is_file(), f"找不到黃金樣本預期輸出: {expected_path}"

    conv = get_converter("claude-code")
    assert isinstance(conv, ClaudeCodeConverter)
    assert conv.source == "claude-code"

    # 1. facts 驗證
    facts = conv.facts(raw_path)
    assert isinstance(facts, SessionFacts)
    assert facts.created_at == "2026-09-27T08:00:00Z"
    assert facts.updated_at == "2026-09-27T08:00:55Z"
    assert facts.last_message_at == "2026-09-27T08:00:55Z"
    assert facts.last_message_ms == 1790496055000
    assert facts.archived_at is None
    assert facts.in_progress is False
    assert len(facts.message_ids) == 12

    # 2. child_session_ids 驗證
    children = conv.child_session_ids(raw_path)
    assert children == ("claude-code:44444444-4444-4444-8444-000000000000",)

    # 3. convert 輸出並驗證 schema
    converted = conv.convert(raw_path, session_id=SESSION_ID)
    errs = validate_reading(converted)
    assert errs == [], f"Reading validation errors: {errs}"

    # 4. 比對與預期黃金輸出完全一致
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert converted == expected

    # 5. 驗證主幹與被撤銷分支（新語意：主幹沿最新葉節點展開；
    #    身為最新葉的未知型態紀錄留在主幹上、可見，只有捨棄的分支才 reverted）
    reverted_ids = [m["message_id"] for m in converted["messages"] if m["reverted"]]
    assert reverted_ids == ["rec-branch-001"]

    # 6. 驗證 sidechain 不在母 Session 閱讀版訊息中
    assert all("rec-sub-001" != m["message_id"] for m in converted["messages"])


# ---------------------------------------------------------------------------
# 2. Branching & Reverted Rules
# ---------------------------------------------------------------------------


def test_branching_keeps_latest_leaf_and_marks_abandoned_as_reverted(tmp_path: Path):
    """樹狀分支：沿最新葉節點展開為主幹 (reverted: false)，其餘分支保留並標記為 reverted: true。"""
    raw_path = _write_jsonl(
        tmp_path / "branch.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "root request"),
            _user("u2_abandoned", "u1", "2026-09-27T08:01:00.000Z", "old branch user"),
            _assistant("a2_abandoned", "u2_abandoned", "2026-09-27T08:01:05.000Z", [{"type": "text", "text": "old branch answer"}]),
            _user("u2_active", "u1", "2026-09-27T08:02:00.000Z", "active branch user"),
            _assistant("a2_active", "u2_active", "2026-09-27T08:02:10.000Z", [{"type": "text", "text": "active branch final"}]),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id="claude-code:branch-test")

    # 主幹依據最新葉節點 a2_active 展開：u1 -> u2_active -> a2_active
    # 被捨棄的分支 u2_abandoned, a2_abandoned 排在後面並標記為 reverted=True
    msgs_by_id = {m["message_id"]: m for m in out["messages"]}
    assert msgs_by_id["u1"]["reverted"] is False
    assert msgs_by_id["u2_active"]["reverted"] is False
    assert msgs_by_id["a2_active"]["reverted"] is False

    assert msgs_by_id["u2_abandoned"]["reverted"] is True
    assert msgs_by_id["a2_abandoned"]["reverted"] is True


# ---------------------------------------------------------------------------
# 3. Tool Use & Tool Result Pairing
# ---------------------------------------------------------------------------


def test_tool_use_and_tool_result_pairing(tmp_path: Path):
    """tool_use (assistant) 與 tool_result (user) 依 tool_use_id 配對合併為單一 tool_call 段落。"""
    raw_path = _write_jsonl(
        tmp_path / "tool_pair.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "run tool"),
            _assistant(
                "a1",
                "u1",
                "2026-09-27T08:00:01.000Z",
                [{"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls -l"}}],
                stop="tool_use",
            ),
            _user(
                "u2",
                "a1",
                "2026-09-27T08:00:02.000Z",
                [{"type": "tool_result", "tool_use_id": "tu_1", "content": "file1.py\nfile2.py", "is_error": False}],
            ),
            _assistant(
                "a2",
                "u2",
                "2026-09-27T08:00:03.000Z",
                [{"type": "text", "text": "Files listed successfully."}],
            ),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id="claude-code:tool-test")

    # a1 的 parts 包含合併後的 tool_call
    a1_msg = [m for m in out["messages"] if m["message_id"] == "a1"][0]
    tool_part = [p for p in a1_msg["parts"] if p["type"] == "tool_call"][0]
    assert tool_part["name"] == "Bash"
    assert tool_part["input_summary"] == '{"command": "ls -l"}'
    assert tool_part["output_summary"] == "file1.py\nfile2.py"

    # u2 僅有被配對之 tool_result，清空為空文字段落
    u2_msg = [m for m in out["messages"] if m["message_id"] == "u2"][0]
    assert u2_msg["parts"] == [{"type": "text", "text": ""}]


def test_tool_use_error_marked_with_error_prefix(tmp_path: Path):
    """tool_result is_error=True 時，output_summary 需加上 [ERROR] 前綴。"""
    raw_path = _write_jsonl(
        tmp_path / "tool_err.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "run bad tool"),
            _assistant(
                "a1",
                "u1",
                "2026-09-27T08:00:01.000Z",
                [{"type": "tool_use", "id": "tu_err", "name": "Bash", "input": {"command": "invalid_cmd"}}],
                stop="tool_use",
            ),
            _user(
                "u2",
                "a1",
                "2026-09-27T08:00:02.000Z",
                [{"type": "tool_result", "tool_use_id": "tu_err", "content": "command not found: invalid_cmd", "is_error": True}],
            ),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id="claude-code:tool-err")
    a1_msg = [m for m in out["messages"] if m["message_id"] == "a1"][0]
    part = a1_msg["parts"][0]
    assert part["type"] == "tool_call"
    assert part["output_summary"].startswith("[ERROR] command not found: invalid_cmd")


# ---------------------------------------------------------------------------
# 4. Thinking, Compaction & Unknown Record Types
# ---------------------------------------------------------------------------


def test_thinking_to_reasoning(tmp_path: Path):
    """thinking block 對應轉換為 reasoning 段落。"""
    raw_path = _write_jsonl(
        tmp_path / "thinking.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "question"),
            _assistant(
                "a1",
                "u1",
                "2026-09-27T08:00:01.000Z",
                [
                    {"type": "thinking", "thinking": "Internal reasoning process...", "signature": "sig_abc"},
                    {"type": "text", "text": "Final answer."},
                ],
            ),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id="claude-code:thinking")
    a1_msg = [m for m in out["messages"] if m["message_id"] == "a1"][0]
    assert a1_msg["parts"][0] == {"type": "reasoning", "text": "Internal reasoning process..."}
    assert a1_msg["parts"][1] == {"type": "text", "text": "Final answer."}


def test_compact_boundary_to_compaction(tmp_path: Path):
    """系統壓縮邊界標記 (subtype: compact_boundary) 對應為 type: compaction (summary: "")。"""
    raw_path = _write_jsonl(
        tmp_path / "compact.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "first"),
            {
                "uuid": "sys_comp",
                "parentUuid": "u1",
                "sessionId": SESSION,
                "isSidechain": False,
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": "2026-09-27T08:00:10.000Z",
                "content": "Conversation compacted",
            },
            _user("u2", "sys_comp", "2026-09-27T08:00:20.000Z", "second"),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id="claude-code:compact")
    sys_msg = [m for m in out["messages"] if m["message_id"] == "sys_comp"][0]
    assert sys_msg["role"] == "system"
    assert sys_msg["parts"] == [{"type": "compaction", "summary": ""}]


def test_unknown_record_type_graceful_handling(tmp_path: Path):
    """未知的紀錄型態轉為文字段落 [未支援的紀錄型態：<type>]，不使整份閱讀版崩潰。

    新語意（Group 3 決定＋reading-version.md）：未知型態以 system 角色輸出
    可見文字；身為最新葉時留在主幹上（reverted 為 False），只有捨棄的分支
    才標 reverted。
    """
    raw_path = _write_jsonl(
        tmp_path / "unknown.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "first"),
            _assistant("a1", "u1", "2026-09-27T08:00:05.000Z", [{"type": "text", "text": "response"}]),
            {
                "uuid": "rec_future",
                "parentUuid": "a1",
                "sessionId": SESSION,
                "isSidechain": False,
                "type": "x-future-metric",
                "timestamp": "2026-09-27T08:00:10.000Z",
                "data": {"metrics": [1, 2, 3]},
            },
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id="claude-code:unknown")
    assert validate_reading(out) == []
    unknown_msg = [m for m in out["messages"] if m["message_id"] == "rec_future"][0]
    assert unknown_msg["role"] == "system"
    assert unknown_msg["reverted"] is False
    assert unknown_msg["parts"] == [{"type": "text", "text": "[未支援的紀錄型態：x-future-metric]"}]


# ---------------------------------------------------------------------------
# 5. Sidechain & Subagent Exclusion
# ---------------------------------------------------------------------------


def test_sidechain_subagent_exclusion(tmp_path: Path):
    """isSidechain: True 之子代理紀錄不進入母 Session 閱讀版訊息中，但能被認出 child_session_id。"""
    sub_session_id = "55555555-5555-4555-8555-000000000000"
    raw_path = _write_jsonl(
        tmp_path / "sidechain.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "dispatch subagent"),
            _assistant(
                "a1",
                "u1",
                "2026-09-27T08:00:01.000Z",
                [{"type": "tool_use", "id": "tu_task", "name": "Task", "input": {"prompt": "do something"}}],
                stop="tool_use",
            ),
            {
                "uuid": "subagent_msg_01",
                "parentUuid": "a1",
                "sessionId": sub_session_id,
                "isSidechain": True,
                "type": "assistant",
                "timestamp": "2026-09-27T08:00:02.000Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Subagent step 1"}]},
            },
            _user(
                "u2",
                "a1",
                "2026-09-27T08:00:05.000Z",
                [{"type": "tool_result", "tool_use_id": "tu_task", "content": "Done", "is_error": False}],
            ),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id=SESSION_ID)

    # 1. 驗證母 Session 閱讀版訊息完全不包含 subagent_msg_01
    msg_ids = [m["message_id"] for m in out["messages"]]
    assert "subagent_msg_01" not in msg_ids

    # 2. 驗證 child_session_ids 正確取得
    children = conv.child_session_ids(raw_path)
    assert children == (f"claude-code:{sub_session_id}",)


# ---------------------------------------------------------------------------
# 6. Base64 Attachments & Hash Verification
# ---------------------------------------------------------------------------


def test_base64_image_and_document_attachments(tmp_path: Path):
    """驗證 Base64 圖片與文件計算真實雜湊與大小，非 Base64 轉為文字標示且絕不捏造雜湊。"""
    png_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    png_sha = hashlib.sha256(png_bytes).hexdigest().lower()

    pdf_bytes = b"%PDF-1.4\n% attachment sample\n%%EOF\n"
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest().lower()

    raw_path = _write_jsonl(
        tmp_path / "attachments.jsonl",
        [
            _user(
                "u1",
                None,
                "2026-09-27T08:00:00.000Z",
                [
                    {"type": "text", "text": "Attachments test"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png_bytes).decode()}},
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode(), "title": "doc.pdf"}},
                    {"type": "document", "source": {"type": "text", "media_type": "application/pdf", "data": "offline content"}},
                ],
            )
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id=SESSION_ID)
    parts = out["messages"][0]["parts"]

    # 1. 圖片
    assert parts[1]["type"] == "image"
    assert parts[1]["media_type"] == "image/png"
    assert parts[1]["sha256"] == png_sha
    assert parts[1]["size"] == len(png_bytes)

    # 2. 文件 (Base64)
    assert parts[2]["type"] == "file"
    assert parts[2]["media_type"] == "application/pdf"
    assert parts[2]["sha256"] == pdf_sha
    assert parts[2]["size"] == len(pdf_bytes)
    assert parts[2]["name"] == "doc.pdf"

    # 3. 非 Base64 文件 -> 轉文字標示，絕不放 "0"*64
    assert parts[3]["type"] == "text"
    assert "[附件：（無檔名） application/pdf，內容不在匯出中]" in parts[3]["text"]


# ---------------------------------------------------------------------------
# 7. Truncation, In-Progress & ConversionError
# ---------------------------------------------------------------------------


def test_summaries_truncated_at_4000_codepoints(tmp_path: Path):
    """工具輸入、輸出以 code point 計截斷於 4,000 字（含結尾 …）。"""
    long_output = "🔥" * 4500  # 4500 個 Unicode code point
    raw_path = _write_jsonl(
        tmp_path / "trunc.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "run"),
            _assistant(
                "a1",
                "u1",
                "2026-09-27T08:00:01.000Z",
                [{"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"cmd": "test"}}],
                stop="tool_use",
            ),
            _user(
                "u2",
                "a1",
                "2026-09-27T08:00:02.000Z",
                [{"type": "tool_result", "tool_use_id": "tu_1", "content": long_output, "is_error": False}],
            ),
        ],
    )
    conv = ClaudeCodeConverter()
    out = conv.convert(raw_path, session_id=SESSION_ID)
    tool_part = out["messages"][1]["parts"][0]
    assert len(tool_part["output_summary"]) == 4000
    assert tool_part["output_summary"].endswith("…")


def test_in_progress_and_completed_flags(tmp_path: Path):
    """最後一則 assistant 缺乏 stop_reason (或為 None) 判定為生成中 (in_progress=True, completed=False)。"""
    raw_path = _write_jsonl(
        tmp_path / "in_prog.jsonl",
        [
            _user("u1", None, "2026-09-27T08:00:00.000Z", "question"),
            _assistant("a1", "u1", "2026-09-27T08:00:05.000Z", [{"type": "text", "text": "generating..."}], stop=None),
        ],
    )
    conv = ClaudeCodeConverter()
    facts = conv.facts(raw_path)
    assert facts.in_progress is True

    out = conv.convert(raw_path, session_id=SESSION_ID)
    assert out["in_progress"] is True
    assert out["messages"][1]["completed"] is False


@pytest.mark.parametrize(
    "records",
    [
        # 1. uuid 重複
        [
            {"parentUuid": None, "type": "user", "message": {"role": "user", "content": "1"}, "uuid": "dup_u", "timestamp": "2026-09-27T08:00:00Z"},
            {"parentUuid": None, "type": "user", "message": {"role": "user", "content": "2"}, "uuid": "dup_u", "timestamp": "2026-09-27T08:00:01Z"},
        ],
        # 2. 缺少 uuid
        [
            {"parentUuid": None, "type": "user", "message": {"role": "user", "content": "1"}, "timestamp": "2026-09-27T08:00:00Z"},
        ],
        # 3. parentUuid 指向不存在的節點
        [
            _user("u1", "non_existent_parent", "2026-09-27T08:00:00Z", "hi"),
        ],
        # 4. type 與 message.role 不一致
        [
            {"parentUuid": None, "type": "assistant", "message": {"role": "user", "content": "1"}, "uuid": "u1", "timestamp": "2026-09-27T08:00:00Z"},
        ],
    ],
)
def test_conversion_error_on_corrupted_records(tmp_path: Path, records: list[dict]):
    """結構毀損（uuid 重複、缺少 uuid、無效 parentUuid、角色不一致）必須嚴格拋出
    ConversionError。只比對例外型別，不比對訊息字串。"""
    raw_path = _write_jsonl(tmp_path / "bad.jsonl", records)
    conv = ClaudeCodeConverter()
    with pytest.raises(ConversionError):
        conv.convert(raw_path, session_id=SESSION_ID)
