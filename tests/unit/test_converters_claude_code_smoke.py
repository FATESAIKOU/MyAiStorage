"""Claude Code 轉換器（ClaudeCodeConverter）冒煙測試。

涵蓋：登錄、黃金樣本逐欄比對、樹狀分支與撤銷、tool_use/tool_result 配對、
thinking→reasoning、壓縮邊界標記、未知紀錄型態的寬容處理、子代理
child_session_id、base64 圖片／文件、摘要截斷，以及結構真的壞掉時的
ConversionError。
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from aistorage.converters import ConversionError, Converter, SessionFacts, get_converter
from aistorage.converters.claude_code import ClaudeCodeConverter
from aistorage.reading import validate_reading

DATA_DIR = Path(__file__).parent / "data" / "converters" / "claude-code"
SESSION = "11111111-1111-4111-8111-999999999999"
SESSION_ID = f"claude-code:{SESSION}"


def _write_jsonl(tmp_path: Path, records: list[dict], name: str = "raw.jsonl") -> Path:
    path = tmp_path / name
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


# ------------------------------------------------------------------ 登錄


def test_claude_code_registered_in_registry():
    conv = get_converter("claude-code")
    assert isinstance(conv, Converter)
    assert isinstance(conv, ClaudeCodeConverter)
    assert conv.source == "claude-code"


# ------------------------------------------------------------ 黃金樣本


def test_claude_code_golden_sample_matches_field_by_field():
    raw_path = DATA_DIR / "basic.jsonl"
    expected_path = DATA_DIR / "basic.reading.json"
    assert raw_path.is_file()
    assert expected_path.is_file()

    conv = get_converter("claude-code")

    facts = conv.facts(raw_path)
    assert isinstance(facts, SessionFacts)
    assert facts.title == "Claude Code 轉換器黃金樣本"
    assert facts.created_at == "2026-09-27T08:00:00Z"
    # updated_at／last_message_at 取全部紀錄（含分支與書記性紀錄）時間的最大值
    assert facts.updated_at == "2026-09-27T08:02:30Z"
    assert facts.last_message_at == "2026-09-27T08:02:30Z"
    assert facts.archived_at is None
    assert facts.archived_ms is None
    assert facts.in_progress is False
    # 閱讀版的順序＝主幹 11 則（主幹末梢是 assistant，不是尾端的書記性紀錄）
    #            ＋分支 2 則（被放棄的對話分支、未支援的紀錄型態），分支排在最後
    assert len(facts.message_ids) == 13
    assert facts.message_ids[0] == "11111111-1111-4111-8111-000000000001"
    assert facts.message_ids[-1] == "11111111-1111-4111-8111-00000000000f"

    assert conv.child_session_ids(raw_path) == (
        "claude-code:22222222-2222-4222-8222-999999999999",
    )

    converted = conv.convert(raw_path, session_id=SESSION_ID)
    assert validate_reading(converted) == []
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert converted == expected

    # 主幹訊息 reverted=false，被捨棄的分支與書記性紀錄 reverted=true
    reverted = [m["message_id"] for m in converted["messages"] if m["reverted"]]
    assert reverted == [
        "11111111-1111-4111-8111-000000000009",
        "11111111-1111-4111-8111-00000000000f",
    ]
    # 壓縮邊界標記在主幹上，轉成空的 compaction 段落
    compact = converted["messages"][6]
    assert compact["role"] == "system"
    assert compact["reverted"] is False
    assert compact["parts"] == [{"type": "compaction", "summary": ""}]
    # 未支援的紀錄型態：寬容輸出標記段落，不讓整份閱讀版失敗
    unknown = converted["messages"][12]
    assert unknown["role"] == "system"
    assert unknown["parts"] == [
        {"type": "text", "text": "[未支援的紀錄型態：x-session-stats]"}
    ]
    # 子代理紀錄不進入本 Session 的閱讀版
    assert all(
        not m["message_id"].startswith("22222222-") for m in converted["messages"]
    )


def test_snapshot_sha256_is_computed_by_converter(tmp_path: Path):
    raw = _write_jsonl(tmp_path, [_user("u1", None, "2026-09-27T08:00:00.000Z", "hi")])
    conv = ClaudeCodeConverter()
    expected_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    out = conv.convert(raw, session_id="claude-code:whatever")
    assert out["snapshot_sha256"] == expected_sha

    # 傳入相符的雜湊可以交叉檢查通過
    assert conv.convert(raw, session_id="claude-code:whatever", snapshot_sha256=expected_sha)
    with pytest.raises(ConversionError, match="snapshot_sha256"):
        conv.convert(raw, session_id="claude-code:whatever", snapshot_sha256="0" * 64)


# ---------------------------------------------------- 分支、配對、子代理


def test_branch_keeps_latest_leaf_as_main_line_and_marks_rest_reverted(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "起頭"),
        _user("u2a", "u1", "2026-09-27T08:01:00.000Z", "被放棄的分支"),
        _user("u2b", "u1", "2026-09-27T08:02:00.000Z", "被採用的分支"),
        _assistant("a1", "u2b", "2026-09-27T08:02:05.000Z", [{"type": "text", "text": "收尾"}]),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id="claude-code:x")

    assert [m["message_id"] for m in out["messages"]] == ["u1", "u2b", "a1", "u2a"]
    assert [m["reverted"] for m in out["messages"]] == [False, False, False, True]
    # 分支仍保留完整內容
    assert out["messages"][3]["parts"] == [{"type": "text", "text": "被放棄的分支"}]


def test_tool_use_and_tool_result_pair_into_one_tool_call(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "跑一下"),
        _assistant(
            "a1",
            "u1",
            "2026-09-27T08:00:01.000Z",
            [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
            stop="tool_use",
        ),
        _user(
            "u2",
            "a1",
            "2026-09-27T08:00:02.000Z",
            [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a\nb", "is_error": False}],
        ),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id="claude-code:x")

    tool_parts = [
        p for m in out["messages"] for p in m["parts"] if p["type"] == "tool_call"
    ]
    assert tool_parts == [{
        "type": "tool_call",
        "name": "Bash",
        "input_summary": '{"command": "ls"}',
        "output_summary": "a\nb",
        "child_session_id": None,
    }]
    # tool_result 不再重複輸出成第二個段落，只留下空文字段落
    assert out["messages"][2]["parts"] == [{"type": "text", "text": ""}]


def test_tool_error_marks_output_summary(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "跑一下"),
        _assistant(
            "a1",
            "u1",
            "2026-09-27T08:00:01.000Z",
            [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "nope"}}],
            stop="tool_use",
        ),
        _user(
            "u2",
            "a1",
            "2026-09-27T08:00:02.000Z",
            [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "command not found", "is_error": True}],
        ),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id="claude-code:x")
    part = out["messages"][1]["parts"][0]
    assert part["output_summary"] == "[ERROR] command not found"


def test_task_tool_records_child_session_id(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "派工"),
        _assistant(
            "a1",
            "u1",
            "2026-09-27T08:00:01.000Z",
            [{"type": "tool_use", "id": "toolu_1", "name": "Task", "input": {"prompt": "搜尋"}}],
            stop="tool_use",
        ),
        {
            "parentUuid": "a1",
            "isSidechain": True,
            "sessionId": "99999999-9999-4999-8999-000000000000",
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "子代理內部思考"}]},
            "uuid": "sub1",
            "timestamp": "2026-09-27T08:00:02.000Z",
        },
    ]
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    out = conv.convert(raw, session_id=SESSION_ID)

    assert out["messages"][1]["parts"][0]["child_session_id"] == (
        "claude-code:99999999-9999-4999-8999-000000000000"
    )
    assert conv.child_session_ids(raw) == (
        "claude-code:99999999-9999-4999-8999-000000000000",
    )
    # 子代理紀錄本身不進本 Session 的閱讀版
    assert all(m["message_id"] != "sub1" for m in out["messages"])


def test_child_session_id_from_task_metadata_and_self_reference_guard(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "派工"),
        _assistant(
            "a1",
            "u1",
            "2026-09-27T08:00:01.000Z",
            [{"type": "tool_use", "id": "toolu_1", "name": "Task", "input": {"prompt": "p"}}],
            stop="tool_use",
        ),
        _user(
            "u2",
            "a1",
            "2026-09-27T08:00:02.000Z",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "done <task_metadata>\nsession_id: abc12345-6789-4def-8123-456789abcdef\n</task_metadata>",
                    "is_error": False,
                }
            ],
        ),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id=SESSION_ID)
    assert out["messages"][1]["parts"][0]["child_session_id"] == (
        "claude-code:abc12345-6789-4def-8123-456789abcdef"
    )

    # 子代理不得指向本 Session 自己
    self_ref = _write_jsonl(
        tmp_path,
        [
            _user("v1", None, "2026-09-27T08:00:00.000Z", "派工"),
            _assistant(
                "v2",
                "v1",
                "2026-09-27T08:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_9",
                        "name": "Task",
                        "input": {"sessionId": SESSION, "prompt": "p"},
                    }
                ],
                stop="tool_use",
            ),
        ],
        name="self.jsonl",
    )
    out2 = ClaudeCodeConverter().convert(self_ref, session_id=SESSION_ID)
    assert out2["messages"][1]["parts"][0]["child_session_id"] is None


# ------------------------------------------------------------ 內容段落


def test_thinking_image_and_document_blocks(tmp_path: Path):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFWM0AAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
    )
    doc = b"%PDF-1.4\n% sample\n%%EOF\n"
    records = [
        _user(
            "u1",
            None,
            "2026-09-27T08:00:00.000Z",
            [
                {"type": "text", "text": "看圖"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(doc).decode(), "title": "spec.pdf"}},
                {"type": "document", "source": {"type": "text", "media_type": "application/pdf", "data": "已擷取文字"}},
            ],
        ),
        _assistant(
            "a1",
            "u1",
            "2026-09-27T08:00:01.000Z",
            [
                {"type": "thinking", "thinking": "先看圖再看文件", "signature": "sig"},
                {"type": "text", "text": "看完了"},
                {"type": "redacted_thinking", "data": "EmwK"},
            ],
        ),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id=SESSION_ID)
    parts = out["messages"][0]["parts"]

    assert parts[0] == {"type": "text", "text": "看圖"}
    assert parts[1] == {
        "type": "image",
        "media_type": "image/png",
        "sha256": hashlib.sha256(png).hexdigest(),
        "size": len(png),
    }
    assert parts[2] == {
        "type": "file",
        "media_type": "application/pdf",
        "sha256": hashlib.sha256(doc).hexdigest(),
        "size": len(doc),
        "name": "spec.pdf",
    }
    # 沒有 base64 位元組就不捏造雜湊
    assert parts[3] == {
        "type": "text",
        "text": "[附件：（無檔名） application/pdf，內容不在匯出中]",
    }

    a_parts = out["messages"][1]["parts"]
    assert a_parts[0] == {"type": "reasoning", "text": "先看圖再看文件"}
    assert a_parts[2] == {"type": "text", "text": "[未支援的段落型態：redacted_thinking]"}


def test_summaries_are_truncated_at_4000_codepoints(tmp_path: Path):
    long_out = "あ" * 4200  # 以 code point 計算，不是位元組
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "跑一下"),
        _assistant(
            "a1",
            "u1",
            "2026-09-27T08:00:01.000Z",
            [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "cat big"}}],
            stop="tool_use",
        ),
        _user(
            "u2",
            "a1",
            "2026-09-27T08:00:02.000Z",
            [{"type": "tool_result", "tool_use_id": "toolu_1", "content": long_out, "is_error": False}],
        ),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id="claude-code:x")
    summary = out["messages"][1]["parts"][0]["output_summary"]
    assert len(summary) == 4000
    assert summary.endswith("…")


def test_in_progress_when_last_assistant_has_no_stop_reason(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        _assistant("a1", "u1", "2026-09-27T08:00:01.000Z", [{"type": "text", "text": "生成中…"}], stop=None),
    ]
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    out = conv.convert(raw, session_id="claude-code:x")
    assert out["in_progress"] is True
    assert out["messages"][1]["completed"] is False
    assert conv.facts(raw).in_progress is True


# ------------------------------------------------------- 結構異常處理


@pytest.mark.parametrize(
    "records, match, structural",
    [
        # uuid 重複
        (
            [
                {"parentUuid": None, "type": "user", "message": {"role": "user", "content": "a"}, "uuid": "u1", "timestamp": "2026-09-27T08:00:00Z"},
                {"parentUuid": None, "type": "user", "message": {"role": "user", "content": "b"}, "uuid": "u1", "timestamp": "2026-09-27T08:00:01Z"},
            ],
            "uuid 重複",
            True,
        ),
        # 缺 uuid
        ([{"parentUuid": None, "type": "user", "message": {"role": "user", "content": "a"}, "timestamp": "2026-09-27T08:00:00Z"}], "缺少 uuid", True),
        # parentUuid 指向不存在的紀錄
        ([_user("u1", "nope", "2026-09-27T08:00:00.000Z", "a")], "parentUuid", True),
        # type 與 message.role 不一致
        (
            [{"parentUuid": None, "type": "assistant", "message": {"role": "user", "content": "a"}, "uuid": "u1", "timestamp": "2026-09-27T08:00:00Z"}],
            "不一致",
            True,
        ),
        # tool_use 缺 name（只有 convert 會走內容段落）
        (
            [
                _user("u1", None, "2026-09-27T08:00:00.000Z", "a"),
                _assistant("a1", "u1", "2026-09-27T08:00:01.000Z", [{"type": "tool_use", "id": "t1", "input": {}}], stop="tool_use"),
            ],
            "缺少 name",
            False,
        ),
        # image 缺 media_type（只有 convert 會走內容段落）
        (
            [
                _user("u1", None, "2026-09-27T08:00:00.000Z", [{"type": "image", "source": {"type": "base64", "data": "AA=="}}]),
            ],
            "media_type",
            False,
        ),
    ],
)
def test_structural_problems_raise_conversion_error(tmp_path: Path, records, match, structural):
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    with pytest.raises(ConversionError, match=match):
        conv.convert(raw, session_id="claude-code:x")
    if structural:
        with pytest.raises(ConversionError, match=match):
            conv.facts(raw)


def test_bad_json_line_and_empty_file(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ConversionError, match="JSON 解析失敗"):
        ClaudeCodeConverter().facts(bad)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ConversionError, match="沒有任何內容"):
        ClaudeCodeConverter().facts(empty)


def test_unknown_record_type_is_tolerated_not_raised(tmp_path: Path):
    """PM 決定 3：未知的紀錄型態轉成標記段落，不讓整份閱讀版失敗。"""
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        _assistant("a1", "u1", "2026-09-27T08:00:01.000Z", [{"type": "text", "text": "你好"}]),
        {
            "type": "telemetry",
            "parentUuid": "a1",
            "uuid": "x1",
            "timestamp": "2026-09-27T08:00:02.000Z",
        },
    ]
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    out = conv.convert(raw, session_id="claude-code:x")
    assert validate_reading(out) == []
    # 書記性紀錄不當主幹末端：主幹仍是最新的 assistant，整段對話不會被誤標撤銷
    assert [m["message_id"] for m in out["messages"]] == ["u1", "a1", "x1"]
    assert [m["reverted"] for m in out["messages"]] == [False, False, True]
    assert out["messages"][2]["role"] == "system"
    assert out["messages"][2]["parts"] == [
        {"type": "text", "text": "[未支援的紀錄型態：telemetry]"}
    ]
    assert conv.facts(raw).in_progress is False
    # 純書記性紀錄時不會 raise，只是全部算分支
    only_unknown = _write_jsonl(
        tmp_path,
        [{"type": "telemetry", "parentUuid": None, "uuid": "x1", "timestamp": "2026-09-27T08:00:02.000Z"}],
        name="only.jsonl",
    )
    out2 = conv.convert(only_unknown, session_id="claude-code:x")
    assert out2["messages"][0]["reverted"] is True


def test_compact_boundary_becomes_empty_compaction_part(tmp_path: Path):
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "長對話"),
        {
            "parentUuid": "u1",
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted · Continue from where you left off",
            "compactMetadata": {"trigger": "auto", "preTokens": 164213},
            "uuid": "c1",
            "timestamp": "2026-09-27T08:10:00.000Z",
            "level": "info",
        },
        _user("u2", "c1", "2026-09-27T08:10:05.000Z", "繼續"),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id="claude-code:x")
    compact = out["messages"][1]
    assert compact["message_id"] == "c1"
    assert compact["role"] == "system"
    assert compact["reverted"] is False
    assert compact["parts"] == [{"type": "compaction", "summary": ""}]


def test_parent_id_is_formatted_with_source_prefix(tmp_path: Path):
    raw = _write_jsonl(tmp_path, [_user("u1", None, "2026-09-27T08:00:00.000Z", "a")])
    conv = ClaudeCodeConverter()
    out = conv.convert(raw, session_id="claude-code:x", parent_id="parent-uuid")
    assert out["parent_id"] == "claude-code:parent-uuid"
    out2 = conv.convert(raw, session_id="claude-code:x", parent_id="claude-code:already")
    assert out2["parent_id"] == "claude-code:already"
