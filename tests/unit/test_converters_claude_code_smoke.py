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
    """黃金樣本依 review-g3f 的形狀統計撰寫（內容為虛構）。"""
    raw_path = DATA_DIR / "basic.jsonl"
    expected_path = DATA_DIR / "basic.reading.json"
    assert raw_path.is_file()
    assert expected_path.is_file()

    conv = get_converter("claude-code")

    facts = conv.facts(raw_path, session_id=SESSION_ID)
    assert isinstance(facts, SessionFacts)
    # M1：標題來自 custom-title（不是舊版的 summary 紀錄）
    assert facts.title == "Claude Code 轉換器黃金樣本"
    assert facts.created_at == "2026-09-27T08:00:00Z"
    assert facts.updated_at == "2026-09-27T08:03:20Z"
    assert facts.last_message_at == "2026-09-27T08:03:20Z"
    assert facts.archived_at is None
    assert facts.archived_ms is None
    # H3：最後一行寫到一半 → 仍在生成中
    assert facts.in_progress is True
    # 19 則：主幹 18 則 ＋ 被放棄的分支 1 則（分支排在最後）
    assert len(facts.message_ids) == 19
    assert facts.message_ids[0] == "11111111-1111-4111-8111-000000000001"
    assert facts.message_ids[-1] == "11111111-1111-4111-8111-000000000014"

    assert conv.child_session_ids(raw_path, session_id=SESSION_ID) == (
        "claude-code:22222222-2222-4222-8222-999999999999",
    )

    converted = conv.convert(raw_path, session_id=SESSION_ID)
    assert validate_reading(converted) == []
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert converted == expected

    # 只有被放棄的對話分支是 reverted；壓縮之前的歷史、書記性紀錄都在主幹上
    reverted = [m["message_id"] for m in converted["messages"] if m["reverted"]]
    assert reverted == ["11111111-1111-4111-8111-000000000014"]

    # 同一個 message.id 拆成三筆 → 合併成一則訊息（thinking/text/tool_use）
    merged = converted["messages"][1]
    assert merged["message_id"] == "11111111-1111-4111-8111-000000000002"
    assert [p["type"] for p in merged["parts"]] == ["reasoning", "text", "tool_call"]
    assert merged["completed"] is True

    # H2：compact_boundary 的 parentUuid 為 null，改走 logicalParentUuid，
    # 壓縮之前的歷史因此仍在主幹上
    compact = converted["messages"][6]
    assert compact["role"] == "system"
    assert compact["reverted"] is False
    assert compact["parts"] == [{"type": "compaction", "summary": ""}]
    # M3：isCompactSummary 的 user 訊息 → system 角色的 compaction 段落
    summary_msg = converted["messages"][7]
    assert summary_msg["role"] == "system"
    assert summary_msg["parts"][0]["type"] == "compaction"
    assert summary_msg["parts"][0]["summary"].startswith("前情提要：")

    # M2：書記性紀錄不製造雜訊（total_tokens_reminder／turn_duration／hook_success
    # 都不出現），queued_command 與 edited_text_file 轉成文字
    texts = [
        p.get("text", "")
        for m in converted["messages"]
        for p in m["parts"]
        if p["type"] == "text"
    ]
    assert not any("total_tokens_reminder" in t for t in texts)
    assert not any("hook_success" in t for t in texts)
    assert not any("turn_duration" in t for t in texts)
    assert any(t.startswith("[queued_command]") for t in texts)
    assert any(t.startswith("[edited_text_file]") and "legacy.py" in t for t in texts)
    # 未支援的型態仍然明確標記，不靜默丟棄
    assert any(t == "[未支援的附件型態：agent_memory_snapshot]" for t in texts)
    assert any(t == "[未支援的紀錄型態：x-session-stats]" for t in texts)
    # M3：isMeta 的 user 訊息 → system 角色
    assert any(
        m["role"] == "system" and m["parts"][0].get("text", "").startswith("<command-name>")
        for m in converted["messages"]
    )
    # 子代理紀錄不進入本 Session 的閱讀版
    assert all(
        not m["message_id"].startswith("22222222-") for m in converted["messages"]
    )


def test_golden_sample_has_the_real_shapes():
    """樣本本身必須保有真實形狀：沒有 uuid 的中繼紀錄、最後一行寫到一半。"""
    raw = (DATA_DIR / "basic.jsonl").read_bytes()
    lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]
    records = []
    for i, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            assert i == len(lines) - 1, "只有最後一行允許寫到一半"

    types = [r.get("type") for r in records]
    for expected in (
        "custom-title", "ai-title", "mode", "permission-mode", "atis-latch",
        "queue-operation", "last-prompt", "file-history-snapshot",
        "file-history-delta", "cost-state", "attachment",
    ):
        assert expected in types, f"樣本缺少 {expected} 紀錄"
    # 這些型態在真實檔案裡都沒有 uuid
    for r in records:
        if r.get("type") in ("custom-title", "ai-title", "mode", "permission-mode",
                             "atis-latch", "queue-operation", "last-prompt",
                             "file-history-snapshot", "file-history-delta", "cost-state"):
            assert "uuid" not in r
    assert any(r.get("type") == "system" and r.get("subtype") == "turn_duration" for r in records)
    assert any(r.get("isCompactSummary") is True for r in records)
    assert any(r.get("isMeta") is True for r in records)
    boundaries = [
        r for r in records
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
    ]
    assert boundaries and all(r["parentUuid"] is None for r in boundaries)
    assert all("logicalParentUuid" in r for r in boundaries)
    # 同一個 message.id 拆成多筆
    ids = [r["message"]["id"] for r in records
           if r.get("type") == "assistant" and isinstance(r.get("message"), dict)]
    assert ids.count("msg_01") == 3
    # 附件至少三種子型態
    sub = [r["attachment"]["type"] for r in records if r.get("type") == "attachment"]
    assert len(set(sub)) >= 3
    # H3：檔案必須不以換行結尾（最後一行寫到一半）
    assert not raw.endswith(b"\n")


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
    assert conv.child_session_ids(raw, session_id=SESSION_ID) == (
        "claude-code:99999999-9999-4999-8999-000000000000",
    )
    # 子代理紀錄本身不進本 Session 的閱讀版
    assert all(m["message_id"] != "sub1" for m in out["messages"])


def test_child_session_id_uses_only_structured_sources(tmp_path: Path):
    """review-g3f L2：只採結構化的來源，不從 tool_result 的文字內容推測 session id。"""
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
                    # 文字內容裡雖然有 session id，但那是模型可見的輸出，不採信
                    "content": "done <task_metadata>\nsession_id: deadbeef-1111-4222-8333-444444444444\n</task_metadata>",
                    "is_error": False,
                }
            ],
            toolUseResult={"type": "text", "sessionId": "abc12345-6789-4def-8123-456789abcdef"},
        ),
    ]
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    out = conv.convert(raw, session_id=SESSION_ID)
    assert out["messages"][1]["parts"][0]["child_session_id"] == (
        "claude-code:abc12345-6789-4def-8123-456789abcdef"
    )
    assert conv.child_session_ids(raw, session_id=SESSION_ID) == (
        "claude-code:abc12345-6789-4def-8123-456789abcdef",
    )

    # 只有文字內容、沒有結構化欄位 → 不推測，回 null
    text_only = _write_jsonl(
        tmp_path,
        [
            _user("v1", None, "2026-09-27T08:00:00.000Z", "派工"),
            _assistant(
                "v2",
                "v1",
                "2026-09-27T08:00:01.000Z",
                [{"type": "tool_use", "id": "toolu_1", "name": "Task", "input": {"prompt": "p"}}],
                stop="tool_use",
            ),
            _user(
                "v3",
                "v2",
                "2026-09-27T08:00:02.000Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "<task_metadata>session_id: deadbeef-1111-4222-8333-444444444444</task_metadata>",
                        "is_error": False,
                    }
                ],
            ),
        ],
        name="text_only.jsonl",
    )
    out2 = conv.convert(text_only, session_id=SESSION_ID)
    assert out2["messages"][1]["parts"][0]["child_session_id"] is None

    # 子代理不得指向本 Session 自己
    self_ref = _write_jsonl(
        tmp_path,
        [
            _user("w1", None, "2026-09-27T08:00:00.000Z", "派工"),
            _assistant(
                "w2",
                "w1",
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
    out3 = conv.convert(self_ref, session_id=SESSION_ID)
    assert out3["messages"][1]["parts"][0]["child_session_id"] is None


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
    assert conv.facts(raw, session_id=SESSION_ID).in_progress is True


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
        ([_user("u1", "nope", "2026-09-27T08:00:00.000Z", "a")], "找不到對應的主幹紀錄", True),
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
            conv.facts(raw, session_id=SESSION_ID)


def test_bad_json_line_and_empty_file(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ConversionError, match="JSON 解析失敗"):
        ClaudeCodeConverter().facts(bad, session_id=SESSION_ID)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ConversionError, match="沒有任何內容"):
        ClaudeCodeConverter().facts(empty, session_id=SESSION_ID)


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
    # 它接在對話之後 → 屬於主幹的延伸，不是被放棄的分支
    assert [m["reverted"] for m in out["messages"]] == [False, False, False]
    assert out["messages"][2]["role"] == "system"
    assert out["messages"][2]["parts"] == [
        {"type": "text", "text": "[未支援的紀錄型態：telemetry]"}
    ]
    assert conv.facts(raw, session_id=SESSION_ID).in_progress is False
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


# --------------------------------------------- review-g3f H1／H2／H3／M1／M2


def test_metadata_records_without_uuid_are_skipped_not_errors(tmp_path: Path):
    """H1：沒有 uuid 的中繼資料紀錄是最常見的紀錄之一，不能讓整份轉換失敗。"""
    records = [
        # 第一筆就是中繼資料：真實檔案長這樣
        {"type": "queue-operation", "sessionId": SESSION, "operation": "enqueue"},
        {"type": "mode", "mode": "default", "sessionId": SESSION},
        {"type": "custom-title", "title": "中繼標題", "sessionId": SESSION},
        _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        _assistant("a1", "u1", "2026-09-27T08:00:01.000Z", [{"type": "text", "text": "你好"}]),
        {"type": "cost-state", "sessionId": SESSION, "cost": {"totalUSD": 0.1}},
    ]
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    out = conv.convert(raw, session_id=SESSION_ID)
    assert validate_reading(out) == []
    assert [m["message_id"] for m in out["messages"]] == ["u1", "a1"]
    assert out["title"] == "中繼標題"

    # 但 user／assistant／system 缺 uuid 仍然是結構損毀，必須報錯
    for broken in (
        {"type": "user", "message": {"role": "user", "content": "a"}, "timestamp": "2026-09-27T08:00:00Z"},
        {"type": "assistant", "message": {"role": "assistant", "content": []}, "timestamp": "2026-09-27T08:00:00Z"},
        {"type": "system", "content": "a", "timestamp": "2026-09-27T08:00:00Z"},
    ):
        bad = _write_jsonl(tmp_path, [broken], name="bad_uuid.jsonl")
        with pytest.raises(ConversionError, match="缺少 uuid"):
            conv.convert(bad, session_id=SESSION_ID)


def test_compact_boundary_uses_logical_parent_uuid(tmp_path: Path):
    """H2：compact_boundary 的 parentUuid 為 null，壓縮之前的歷史不能變成分支。"""
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "第一段"),
        _assistant("a1", "u1", "2026-09-27T08:00:01.000Z", [{"type": "text", "text": "回覆一"}]),
        {
            "parentUuid": None,
            "isSidechain": False,
            "sessionId": SESSION,
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "logicalParentUuid": "a1",
            "uuid": "c1",
            "timestamp": "2026-09-27T08:10:00.000Z",
        },
        _user("u2", "c1", "2026-09-27T08:10:01.000Z", "第二段", isCompactSummary=True),
        _user("u3", "u2", "2026-09-27T08:20:00.000Z", "繼續"),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id=SESSION_ID)
    assert [m["message_id"] for m in out["messages"]] == ["u1", "a1", "c1", "u2", "u3"]
    # 壓縮之前的兩則都還在主幹上，可以當接續點
    assert [m["reverted"] for m in out["messages"]] == [False] * 5

    # logicalParentUuid 指向不存在的紀錄 → 結構斷鏈，必須報錯
    dangling = _write_jsonl(
        tmp_path,
        [
            _user("v1", None, "2026-09-27T08:00:00.000Z", "嗨"),
            {
                "parentUuid": None,
                "type": "system",
                "subtype": "compact_boundary",
                "content": "x",
                "logicalParentUuid": "nope",
                "uuid": "c9",
                "timestamp": "2026-09-27T08:10:00.000Z",
            },
        ],
        name="dangling.jsonl",
    )
    with pytest.raises(ConversionError, match="找不到對應的主幹紀錄"):
        ClaudeCodeConverter().convert(dangling, session_id=SESSION_ID)


def test_half_written_last_line_is_tolerated(tmp_path: Path):
    """H3：生成中 Session 的最後一行可能寫到一半，不能讓整份快照失敗。"""
    good = json.dumps(_user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"), ensure_ascii=False)
    partial = '{"parentUuid":"u1","type":"assistant","message":{"role":"assistant"'

    truncated = tmp_path / "truncated.jsonl"
    truncated.write_bytes((good + "\n" + partial).encode("utf-8"))
    out = ClaudeCodeConverter().convert(truncated, session_id=SESSION_ID)
    assert [m["message_id"] for m in out["messages"]] == ["u1"]
    assert out["in_progress"] is True

    # 中間任何一行壞掉 → 仍然報錯
    middle = tmp_path / "middle.jsonl"
    middle.write_bytes((good + "\n" + partial + "\n" + good + "\n").encode("utf-8"))
    with pytest.raises(ConversionError, match="第 2 行 JSON 解析失敗"):
        ClaudeCodeConverter().convert(middle, session_id=SESSION_ID)

    # 檔案有結尾換行卻壞掉 → 不是「寫到一半」，照樣報錯
    newline_tail = tmp_path / "newline_tail.jsonl"
    newline_tail.write_bytes((good + "\n" + partial + "\n").encode("utf-8"))
    with pytest.raises(ConversionError, match="第 2 行 JSON 解析失敗"):
        ClaudeCodeConverter().convert(newline_tail, session_id=SESSION_ID)


def test_title_priority_custom_then_ai_then_summary(tmp_path: Path):
    """M1：目前版本沒有 summary 紀錄，標題來自 custom-title／ai-title。"""
    conv = ClaudeCodeConverter()
    both = _write_jsonl(
        tmp_path,
        [
            {"type": "custom-title", "title": "手動標題", "sessionId": SESSION},
            {"type": "ai-title", "title": "機器標題", "sessionId": SESSION},
            _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        ],
        name="both.jsonl",
    )
    assert conv.facts(both, session_id=SESSION_ID).title == "手動標題"

    ai_only = _write_jsonl(
        tmp_path,
        [
            {"type": "ai-title", "title": "機器標題", "sessionId": SESSION},
            _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        ],
        name="ai.jsonl",
    )
    assert conv.facts(ai_only, session_id=SESSION_ID).title == "機器標題"

    # 相容舊版：summary 紀錄（取 summary 欄位）
    legacy = _write_jsonl(
        tmp_path,
        [
            {"type": "summary", "summary": "舊版標題", "leafUuid": "u1"},
            _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        ],
        name="legacy.jsonl",
    )
    assert conv.facts(legacy, session_id=SESSION_ID).title == "舊版標題"
    # 都沒有 → null（不捏造標題）
    neither = _write_jsonl(tmp_path, [_user("u1", None, "2026-09-27T08:00:00.000Z", "嗨")], name="none.jsonl")
    assert conv.facts(neither, session_id=SESSION_ID).title is None


def test_attachment_treatment_table_and_chain_continuity(tmp_path: Path):
    """M2：書記性的 attachment 丟棄但維持鏈的連續；有意義的轉成文字。"""
    def attachment(uuid, parent, sub, ts, extra=None):
        att = {"type": sub}
        att.update(extra or {})
        return {
            "parentUuid": parent, "isSidechain": False, "sessionId": SESSION,
            "type": "attachment", "attachment": att,
            "uuid": uuid, "timestamp": ts,
        }

    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        attachment("a_drop1", "u1", "total_tokens_reminder", "2026-09-27T08:00:01.000Z"),
        attachment("a_drop2", "a_drop1", "deferred_tools_record", "2026-09-27T08:00:02.000Z"),
        attachment("a_drop3", "a_drop2", "hook_success", "2026-09-27T08:00:03.000Z", {"output": "lint clean"}),
        attachment("a_keep", "a_drop3", "queued_command", "2026-09-27T08:00:04.000Z", {"command": "/status"}),
        # 子節點的 parent 是被丟棄的 attachment：鏈必須接得起來
        _assistant("x1", "a_keep", "2026-09-27T08:00:05.000Z", [{"type": "text", "text": "收到"}]),
        attachment("a_mark", "x1", "some_future_kind", "2026-09-27T08:00:06.000Z"),
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id=SESSION_ID)
    assert validate_reading(out) == []
    ids = [m["message_id"] for m in out["messages"]]
    assert ids == ["u1", "a_keep", "x1", "a_mark"]
    assert all(m["reverted"] is False for m in out["messages"])
    texts = [p["text"] for m in out["messages"] for p in m["parts"] if p["type"] == "text"]
    assert any(t.startswith("[queued_command]") and "/status" in t for t in texts)
    assert any(t == "[未支援的附件型態：some_future_kind]" for t in texts)
    # 被丟棄的 attachment 完全不出現（包含 hook 的輸出）
    assert not any("lint clean" in t for t in texts)
    assert not any("total_tokens_reminder" in t for t in texts)

    # 沒有內部型態的 attachment 視為書記性，丟棄
    no_type = _write_jsonl(
        tmp_path,
        [
            _user("v1", None, "2026-09-27T08:00:00.000Z", "嗨"),
            {"parentUuid": "v1", "type": "attachment", "attachment": {},
             "uuid": "v2", "timestamp": "2026-09-27T08:00:01.000Z", "sessionId": SESSION},
        ],
        name="no_type.jsonl",
    )
    assert [m["message_id"] for m in ClaudeCodeConverter().convert(no_type, session_id=SESSION_ID)["messages"]] == ["v1"]


def test_turn_duration_system_records_are_dropped(tmp_path: Path):
    """M2：turn_duration 沒有 content，留下來只會變成空白的 system 訊息。"""
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        {
            "parentUuid": "u1", "isSidechain": False, "sessionId": SESSION,
            "type": "system", "subtype": "turn_duration", "durationMs": 4210,
            "uuid": "s1", "timestamp": "2026-09-27T08:00:01.000Z",
        },
        {
            "parentUuid": "s1", "isSidechain": False, "sessionId": SESSION,
            "type": "system", "subtype": "local_command", "content": "/clear",
            "uuid": "s2", "timestamp": "2026-09-27T08:00:02.000Z",
        },
    ]
    raw = _write_jsonl(tmp_path, records)
    out = ClaudeCodeConverter().convert(raw, session_id=SESSION_ID)
    assert [m["message_id"] for m in out["messages"]] == ["u1", "s2"]
    assert out["messages"][1]["parts"] == [{"type": "text", "text": "/clear"}]


def test_session_id_is_optional_and_never_guessed(tmp_path: Path):
    """L3：沒傳 session_id 也不推測（原本會用「最常見的 sessionId」猜）。"""
    records = [
        _user("u1", None, "2026-09-27T08:00:00.000Z", "嗨"),
        _assistant("a1", "u1", "2026-09-27T08:00:01.000Z", [{"type": "text", "text": "你好"}]),
    ]
    raw = _write_jsonl(tmp_path, records)
    conv = ClaudeCodeConverter()
    # 不傳也能運作（apply.py 目前的呼叫方式）
    assert conv.facts(raw).message_ids == ("u1", "a1")
    assert conv.child_session_ids(raw) == ()
    assert conv.convert(raw, session_id=SESSION_ID)["messages"][0]["message_id"] == "u1"
    # 空字串仍然是不合法的輸入
    with pytest.raises(ConversionError, match="session_id"):
        conv.facts(raw, session_id="   ")
