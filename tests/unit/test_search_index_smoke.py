"""4.2 搜尋索引的冒煙測試（實作方撰寫；驗收由測試方另寫）。

範例內容一律自編，不碰真實 Session。
"""

from pathlib import Path

import pytest

from aistorage.search import index as idx_mod
from aistorage.search.index import (
    Hit,
    IndexEntry,
    IndexStats,
    build_index,
    search,
)


def _meta(session_id: str, **kw) -> dict:
    base = {
        "session_id": session_id,
        "source": "opencode",
        "title": f"標題 {session_id}",
        "producer": "profile:mac-opencode",
        "case_id": None,
        "status": "running",
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "snapshot_at": "2026-09-27T08:00:00Z",
        "parent_id": None,
    }
    base.update(kw)
    return base


def _reading(*texts: str, title: str | None = None) -> dict:
    return {
        "format": "aistorage.reading/v1",
        "session_id": "opencode:s1",
        "source": "opencode",
        "title": title,
        "parent_id": None,
        "snapshot_sha256": "0" * 64,
        "in_progress": False,
        "messages": [
            {
                "message_id": f"m{i}",
                "index": i,
                "role": "user" if i % 2 == 0 else "assistant",
                "created_at": "2026-09-27T08:00:00Z",
                "completed": True,
                "reverted": False,
                "parts": [{"type": "text", "text": t}],
            }
            for i, t in enumerate(texts)
        ],
    }


def _demo_entries() -> list[IndexEntry]:
    return [
        IndexEntry(
            _meta("opencode:s1", title="接續點設計討論", status="stopped",
                  updated_at="2026-09-27T09:00:00Z",
                  snapshot_at="2026-09-27T09:00:00Z"),
            _reading("我們決定了接續點的格式", "交接單由持有者發起",
                     title="接續點設計討論"),
        ),
        IndexEntry(
            _meta("opencode:s2", title="引き継ぎメモ", source="claude-code",
                  producer="profile:other", case_id="case-1",
                  updated_at="2026-09-26T08:00:00Z",
                  snapshot_at="2026-09-26T08:00:00Z"),
            _reading("引き継ぎ内容を確認する", "hello world from Tokyo"),
        ),
        IndexEntry(
            _meta("opencode:s3", title="轉換失敗的 Session"),
            None,  # 轉換失敗：只有 metadata
        ),
    ]


def test_build_and_search_cjk_english(tmp_path: Path):
    p = tmp_path / "view" / "search.sqlite3"
    stats = build_index(p, _demo_entries())
    assert isinstance(stats, IndexStats)
    assert stats.sessions == 3 and not stats.over_threshold
    assert p.is_file()

    hits = search(p, text="接續點")
    assert [h.session_id for h in hits] == ["opencode:s1"]
    assert all(isinstance(h, Hit) and h.matched_by == "fts" for h in hits)
    assert hits[0].snapshot_at == "2026-09-27T09:00:00Z"
    assert hits[0].snippet

    assert [h.session_id for h in search(p, text="引き継ぎ")] == ["opencode:s2"]
    assert [h.session_id for h in search(p, text="hello")] == ["opencode:s2"]
    assert search(p, text="不存在的詞語串") == []


def test_short_text_falls_back_to_like(tmp_path: Path):
    p = tmp_path / "search.sqlite3"
    build_index(p, _demo_entries())
    hits = search(p, text="接續")  # 2 字元走 LIKE
    assert [h.session_id for h in hits] == ["opencode:s1"]
    assert hits[0].matched_by == "like"
    assert "接續" in (hits[0].snippet or "")


def test_filters_and_combination(tmp_path: Path):
    p = tmp_path / "search.sqlite3"
    build_index(p, _demo_entries())

    assert {h.session_id for h in search(p, filters={"source": "claude-code"})} == {"opencode:s2"}
    assert {h.session_id for h in search(p, filters={"status": "stopped"})} == {"opencode:s1"}
    assert {h.session_id for h in search(p, filters={"case_id": "case-1"})} == {"opencode:s2"}
    assert {h.session_id for h in search(p, filters={"producer": "profile:other"})} == {"opencode:s2"}
    assert {h.session_id for h in search(
        p, filters={"updated_after": "2026-09-27T00:00:00Z"})} == {"opencode:s1", "opencode:s3"}
    assert {h.session_id for h in search(
        p, filters={"updated_before": "2026-09-27T00:00:00Z"})} == {"opencode:s2"}

    # 全文＋篩選組合：有交接單三字但限定來源不符 → 空
    assert search(p, text="交接單", filters={"source": "claude-code"}) == []
    assert [h.session_id for h in search(
        p, text="交接單", filters={"status": "stopped"})] == ["opencode:s1"]

    # 純篩選的 matched_by 與 snippet
    hits = search(p, filters={"status": "stopped"})
    assert hits[0].matched_by == "filter" and hits[0].snippet is None


def test_failed_conversion_still_filterable(tmp_path: Path):
    p = tmp_path / "search.sqlite3"
    build_index(p, _demo_entries())
    hits = search(p, filters={"source": "opencode", "status": "running"})
    assert [h.session_id for h in hits] == ["opencode:s3"]
    assert hits[0].title == "轉換失敗的 Session"


def test_rebuild_is_atomic_and_over_threshold_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / "search.sqlite3"
    build_index(p, _demo_entries())
    first_size = p.stat().st_size
    stats = build_index(p, _demo_entries()[:1])
    assert stats.sessions == 1
    assert [h.session_id for h in search(p, text="接續點")] == ["opencode:s1"]
    assert not list(tmp_path.glob("*.tmp"))

    monkeypatch.setattr(idx_mod, "INDEX_SIZE_THRESHOLD", 10)
    stats = build_index(p, _demo_entries())
    assert stats.bytes >= first_size and stats.over_threshold


def test_search_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        search(tmp_path / "nope.sqlite3", text="接續點")
