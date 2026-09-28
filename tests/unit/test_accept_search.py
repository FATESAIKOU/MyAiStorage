"""Independent Acceptance Tests for Search (Task 4.2).

Adheres strictly to:
- schemas/searchindex.md
- Substring matching (包含 query 字串)
- ASCII case-insensitive (不分大小寫)
- Order by updated_at descending, tie-break by session_id
- Title hits count (Session title matches query text even without message match, matches=())
- 2-character queries use LIKE instead of trigram FTS with identical substring semantics
- handoffs table stores author_session_id and get_handoffs returns it
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from aistorage.search.index import (
    HandoffRow,
    IndexEntry,
    IndexMeta,
    build_index,
)
from aistorage.search.query import (
    Query,
    get_handoff,
    get_handoffs,
    get_session_row,
    search,
)


def _make_meta(generation: int = 1) -> IndexMeta:
    return IndexMeta(
        generation=generation,
        built_at="2026-09-27T10:00:00.000Z",
        agora_main_sha="main_sha_test",
        converter_versions={},
    )


def _make_entry(
    session_id: str,
    *,
    title: str = "Test Title",
    updated_at: str = "2026-09-27T10:00:00.000Z",
    parent_id: str | None = None,
    messages: list[dict] | None = None,
) -> IndexEntry:
    meta = {
        "session_id": session_id,
        "source": "opencode",
        "title": title,
        "producer": "alice",
        "case_id": "case-1",
        "status": "running",
        "stopped_at": None,
        "in_progress": False,
        "created_at": "2026-09-27T08:00:00.000Z",
        "updated_at": updated_at,
        "snapshot_at": updated_at,
        "raw_sha256": "a" * 64,
        "raw_size": 100,
        "parent_id": parent_id,
        "reading_status": "ok",
        "committed_at": updated_at,
    }
    reading = {"messages": messages} if messages is not None else None
    return IndexEntry(metadata=meta, snapshots=[], reading=reading, reading_ref=None)


def test_search_substring_matching_and_ascii_case_insensitive(tmp_path: Path):
    """驗證子字串比對與 ASCII 不分大小寫 (schemas/searchindex.md §3)。"""
    db_path = tmp_path / "search_test.sqlite"
    entries = [
        _make_entry(
            "s1",
            messages=[
                {
                    "message_id": "m1",
                    "index": 0,
                    "role": "user",
                    "parts": [{"type": "text", "text": "This is a QuickBrownFox jumping over the lazy dog"}],
                }
            ],
        ),
        _make_entry(
            "s2",
            messages=[
                {
                    "message_id": "m2",
                    "index": 0,
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "Unrelated message content"}],
                }
            ],
        ),
    ]
    build_index(db_path, entries=entries, meta=_make_meta())
    db = sqlite3.connect(str(db_path))

    # 1. 大寫、小寫、混合大小寫皆應命中 (ASCII 不分大小寫)
    for q in ["quick", "QUICK", "Quick", "BROWN", "brown", "Fox", "FOX"]:
        hits, _ = search(db, Query(text=q))
        assert len(hits) == 1, f"Query '{q}' should match s1"
        assert hits[0].session.session_id == "s1"
        assert len(hits[0].matches) == 1
        assert hits[0].matches[0].message_id == "m1"

    # 2. 子字串比對 (非完整單字，單字內部子字串如 'ckBrown')
    hits, _ = search(db, Query(text="ckBrown"))
    assert len(hits) == 1
    assert hits[0].session.session_id == "s1"


def test_search_ordering_by_updated_at_desc_and_session_id_tiebreak(tmp_path: Path):
    """驗證排序一律由 updated_at 由新到舊，平手時依 session_id (schemas/searchindex.md §5)。"""
    db_path = tmp_path / "order_test.sqlite"
    entries = [
        _make_entry(
            "session_c",
            updated_at="2026-09-27T08:00:00.000Z",  # 最舊
            messages=[{"message_id": "m1", "index": 0, "parts": [{"type": "text", "text": "common keyword"}]}],
        ),
        _make_entry(
            "session_b",
            updated_at="2026-09-27T10:00:00.000Z",  # 最新 (平手)
            messages=[{"message_id": "m2", "index": 0, "parts": [{"type": "text", "text": "common keyword"}]}],
        ),
        _make_entry(
            "session_a",
            updated_at="2026-09-27T10:00:00.000Z",  # 最新 (平手，session_id 字典序較前)
            messages=[{"message_id": "m3", "index": 0, "parts": [{"type": "text", "text": "common keyword"}]}],
        ),
    ]
    build_index(db_path, entries=entries, meta=_make_meta())
    db = sqlite3.connect(str(db_path))

    hits, _ = search(db, Query(text="common keyword"))
    hit_ids = [h.session.session_id for h in hits]

    # updated_at 較新的排前；平手時 session_a 在 session_b 之前；最後為 08:00 的 session_c
    assert hit_ids == ["session_a", "session_b", "session_c"]


def test_search_title_hits_without_message_match(tmp_path: Path):
    """驗證標題命中也算，只命中標題的 Session 回傳且 matches 為空 (schemas/searchindex.md §3)。"""
    db_path = tmp_path / "title_test.sqlite"
    entries = [
        _make_entry(
            "s_title_only",
            title="Database Architecture Guide",
            messages=[
                {
                    "message_id": "m1",
                    "index": 0,
                    "parts": [{"type": "text", "text": "Totally unrelated message body without the keyword"}],
                }
            ],
        ),
        _make_entry(
            "s_msg_match",
            title="Normal Session",
            messages=[
                {
                    "message_id": "m2",
                    "index": 0,
                    "parts": [{"type": "text", "text": "Contains Architecture keyword in message"}],
                }
            ],
        ),
    ]
    build_index(db_path, entries=entries, meta=_make_meta())
    db = sqlite3.connect(str(db_path))

    hits, _ = search(db, Query(text="Architecture"))
    assert len(hits) == 2

    # 找到只命中標題的 session
    title_hit = next(h for h in hits if h.session.session_id == "s_title_only")
    assert title_hit.session.title == "Database Architecture Guide"
    assert title_hit.matches == (), "只命中標題時 matches 必須為空"

    # 找到訊息命中的 session
    msg_hit = next(h for h in hits if h.session.session_id == "s_msg_match")
    assert len(msg_hit.matches) == 1
    assert msg_hit.matches[0].message_id == "m2"


def test_search_short_query_2_characters_uses_like(tmp_path: Path):
    """驗證未滿 3 字元走 LIKE，結果與 3 字元走 FTS 完全相同子字串語意 (schemas/searchindex.md §3)。"""
    db_path = tmp_path / "short_query_test.sqlite"
    entries = [
        _make_entry(
            "s_hi",
            messages=[
                {
                    "message_id": "m1",
                    "index": 0,
                    "parts": [{"type": "text", "text": "This is a hi-fi system test"}],
                }
            ],
        ),
        _make_entry(
            "s_other",
            messages=[
                {
                    "message_id": "m2",
                    "index": 0,
                    "parts": [{"type": "text", "text": "Completely different content"}],
                }
            ],
        ),
    ]
    build_index(db_path, entries=entries, meta=_make_meta())
    db = sqlite3.connect(str(db_path))

    # 2 字元查詢 "hi"
    hits_lower, _ = search(db, Query(text="hi"))
    assert len(hits_lower) == 1
    assert hits_lower[0].session.session_id == "s_hi"
    assert len(hits_lower[0].matches) == 1
    assert hits_lower[0].matches[0].message_id == "m1"

    # 2 字元 ASCII 不分大小寫 "HI"
    hits_upper, _ = search(db, Query(text="HI"))
    assert len(hits_upper) == 1
    assert hits_upper[0].session.session_id == "s_hi"

    # 1 字元查詢 "a"
    hits_1char, _ = search(db, Query(text="a"))
    assert any(h.session.session_id == "s_hi" for h in hits_1char)


def test_handoffs_author_session_id_storage_and_resolution(tmp_path: Path):
    """驗證 handoffs 表正確保存 author_session_id，並可依作者 Session 主從屬性判定 (schemas/searchindex.md §1)。"""
    db_path = tmp_path / "handoffs_test.sqlite"
    entries = [
        _make_entry("main_session", parent_id=None),
        _make_entry("sub_session", parent_id="main_session"),
    ]
    handoffs = [
        HandoffRow(
            handoff_id="h_main",
            target_session_id="main_session",
            snapshot_sha256="1" * 64,
            message_id="msg_1",
            producer="alice",
            created_at="2026-09-27T10:00:00.000Z",
            updated_at="2026-09-27T10:00:00.000Z",
            case_id="case-1",
            body_json='{"task": "continue work"}',
            claimed_by_claim_id=None,
            claimed_by_session_id=None,
            claimed_at=None,
            author_session_id="main_session",
        ),
        HandoffRow(
            handoff_id="h_sub",
            target_session_id="sub_session",
            snapshot_sha256="2" * 64,
            message_id="msg_2",
            producer="bob",
            created_at="2026-09-27T10:00:00.000Z",
            updated_at="2026-09-27T10:00:00.000Z",
            case_id="case-1",
            body_json='{"task": "subtask"}',
            claimed_by_claim_id=None,
            claimed_by_session_id=None,
            claimed_at=None,
            author_session_id="sub_session",
        ),
        HandoffRow(
            handoff_id="h_claimed",
            target_session_id="main_session",
            snapshot_sha256="3" * 64,
            message_id="msg_3",
            producer="alice",
            created_at="2026-09-27T10:00:00.000Z",
            updated_at="2026-09-27T10:00:00.000Z",
            case_id="case-1",
            body_json='{"task": "already done"}',
            claimed_by_claim_id="claim_1",
            claimed_by_session_id="claimed_session",
            claimed_at="2026-09-27T11:00:00.000Z",
            author_session_id="main_session",
        ),
    ]

    build_index(db_path, entries=entries, handoffs=handoffs, meta=_make_meta())
    db = sqlite3.connect(str(db_path))

    # 1. get_handoff 回傳 author_session_id 欄位
    h1 = get_handoff(db, "h_main")
    assert h1 is not None
    assert h1.author_session_id == "main_session"
    assert h1.target_session_id == "main_session"

    # 2. open_only=True 排除已認領的交接單 (h_claimed)
    open_handoffs = get_handoffs(db, open_only=True)
    open_ids = [h.handoff_id for h in open_handoffs]
    assert "h_claimed" not in open_ids
    assert "h_main" in open_ids
    assert "h_sub" in open_ids

    # 3. 驗證透過 get_session_row 能區分主 Session (parent_id is None) 與子 Session
    main_author = get_session_row(db, h1.author_session_id)
    assert main_author is not None
    assert main_author.parent_id is None  # 主 Session

    h2 = get_handoff(db, "h_sub")
    assert h2 is not None
    sub_author = get_session_row(db, h2.author_session_id)
    assert sub_author is not None
    assert sub_author.parent_id == "main_session"  # 子 Session
