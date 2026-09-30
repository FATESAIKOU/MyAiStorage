"""4.2 搜尋索引的冒煙測試（實作方撰寫；驗收由測試方另寫）。

一半是單元冒煙，一半是黃金測資 runner（TS 用戶端共用同一份測資）。
範例內容一律自編，不碰真實 Session。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from aistorage.search import index as index_mod
from aistorage.search.index import (
    HandoffRow,
    IndexEntry,
    IndexMeta,
    IndexStats,
    LinkRow,
    RejectionRow,
    build_index,
    dump_tables,
)
from aistorage.search.query import (
    Query,
    get_handoffs,
    get_links,
    get_reading_ref,
    get_rejection,
    get_session_row,
    search,
)

GOLDEN_DIR = Path(__file__).parent / "data" / "search" / "golden"


def _mini_meta(session_id: str, **kw) -> dict:
    base = {
        "session_id": session_id,
        "source": "opencode",
        "title": f"標題 {session_id}",
        "producer": "profile:mac-opencode",
        "case_id": None,
        "status": "running",
        "stopped_at": None,
        "in_progress": False,
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "snapshot_at": "2026-09-27T08:00:00Z",
        "raw_sha256": "0" * 64,
        "raw_size": 10,
        "parent_id": None,
        "reading_status": "ok",
        "reading_error_code": None,
        "committed_at": "2026-09-27T08:01:00Z",
    }
    base.update(kw)
    return base


def _mini_reading(*texts: str) -> dict:
    return {
        "messages": [
            {
                "message_id": f"m{i}",
                "index": i,
                "completed": True,
                "reverted": False,
                "parts": [{"type": "text", "text": t}],
            }
            for i, t in enumerate(texts)
        ]
    }


def _mini_entry(session_id: str, *texts: str, **kw) -> IndexEntry:
    sha = "a" * 64
    return IndexEntry(
        metadata=_mini_meta(session_id, **kw),
        snapshots=[{
            "snapshot_sha256": sha,
            "snapshot_at": kw.get("snapshot_at", "2026-09-27T08:00:00Z"),
            "committed_at": "2026-09-27T08:01:00Z",
            "via": "sync",
            "file_id": f"file-{session_id}",
            "sha256": sha,
            "size": 10,
        }],
        reading=_mini_reading(*texts),
        reading_ref={"snapshot_sha256": sha, "file_id": f"file-{session_id}",
                     "sha256": sha, "size": 10},
    )


def _meta(**kw) -> IndexMeta:
    base = {"generation": 1, "built_at": "2026-09-27T09:00:00Z",
            "agora_main_sha": "abc", "converter_versions": {"opencode": "1"}}
    base.update(kw)
    return IndexMeta(**base)


def test_build_and_query_basics(tmp_path: Path):
    p = tmp_path / "idx.sqlite3"
    stats = build_index(
        p,
        entries=[
            _mini_entry("opencode:s1", "接續點格式確定", "交接單處理"),
            _mini_entry("opencode:s2", "hello world"),
        ],
        meta=_meta(),
    )
    assert isinstance(stats, IndexStats)
    assert stats.sessions == 2 and not stats.over_threshold
    assert stats.file_count == 3  # 2 reading＋index 自己

    con = sqlite3.connect(str(p))
    try:
        hits, nxt = search(con, Query(text="接續點"))
        assert [h.session.session_id for h in hits] == ["opencode:s1"]
        assert [m.message_id for m in hits[0].matches] == ["m0"]
        assert nxt is None

        hits, _ = search(con, Query(text="接續"))  # 2 字走 LIKE
        assert [h.session.session_id for h in hits] == ["opencode:s1"]

        hits, _ = search(con, Query(text="hello", source="opencode"))
        assert [h.session.session_id for h in hits] == ["opencode:s2"]

        assert search(con, Query(text="不存在的詞語串"))[0] == []
    finally:
        con.close()


def test_version_check_rejects_old_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 33, 0))
    with pytest.raises(RuntimeError, match="3.34"):
        build_index(tmp_path / "x.sqlite3", entries=[], meta=_meta())


def test_dump_tables_deterministic(tmp_path: Path):
    p1 = tmp_path / "a.sqlite3"
    p2 = tmp_path / "b.sqlite3"
    entries = [_mini_entry("opencode:s1", "接續點"), _mini_entry("opencode:s2", "hello")]
    build_index(p1, entries=entries, meta=_meta())
    build_index(p2, entries=entries, meta=_meta())
    assert dump_tables(p1) == dump_tables(p2)
    assert set(dump_tables(p1)) >= {"meta", "sessions", "message_fts", "links"}


def test_get_helpers(tmp_path: Path):
    p = tmp_path / "idx.sqlite3"
    build_index(
        p,
        entries=[_mini_entry("opencode:s1", "接續點")],
        links=[LinkRow(kind="reference", from_session_id="opencode:s1",
                       to_session_id="opencode:s2",
                       reference_id="reference:X",
                       read_snapshot_at="2026-09-27T08:00:00Z")],
        handoffs=[HandoffRow(
            handoff_id="handoff:X", target_session_id="opencode:s1",
            snapshot_sha256="a" * 64, message_id="m0",
            producer="profile:mac-opencode",
            created_at="2026-09-27T08:00:00Z", updated_at="2026-09-27T08:00:00Z",
            body_json="{}")],
        rejections=[RejectionRow(item_key="K", code="orphan",
                                 at="2026-09-27T08:00:00Z", authenticated=False)],
        meta=_meta(),
    )
    con = sqlite3.connect(str(p))
    try:
        row = get_session_row(con, "opencode:s1")
        assert row is not None and row.title == "標題 opencode:s1"
        assert get_session_row(con, "opencode:nope") is None

        out, inc = get_links(con, "opencode:s1")
        assert [l.to_session_id for l in out] == ["opencode:s2"]
        assert inc == []
        _, inc2 = get_links(con, "opencode:s2")
        assert [l.from_session_id for l in inc2] == ["opencode:s1"]

        assert [h.handoff_id for h in get_handoffs(con)] == ["handoff:X"]
        assert get_handoffs(con, open_only=True) != []
        assert get_handoffs(con, target_session_id="opencode:s2") == []

        ref = get_reading_ref(con, "opencode:s1")
        assert ref is not None and ref.is_latest and ref.file_id == "file-opencode:s1"
        assert get_reading_ref(con, "opencode:s1", "f" * 64) is None

        rej = get_rejection(con, "K")
        assert rej is not None and rej.code == "orphan" and not rej.authenticated
        assert get_rejection(con, "missing") is None
    finally:
        con.close()


def test_over_threshold_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(index_mod, "INDEX_SIZE_THRESHOLD", 10)
    stats = build_index(tmp_path / "x.sqlite3",
                        entries=[_mini_entry("opencode:s1", "x")], meta=_meta())
    assert stats.over_threshold


# ---------------------------------------------------------------------------
# 黃金測資 runner（TS 用戶端共用 tests/unit/data/search/golden/）
# ---------------------------------------------------------------------------

def _load_golden() -> tuple[dict, dict]:
    entries = json.loads((GOLDEN_DIR / "entries.json").read_text(encoding="utf-8"))
    queries = json.loads((GOLDEN_DIR / "queries.json").read_text(encoding="utf-8"))
    return entries, queries


def _build_golden(tmp_path: Path) -> Path:
    data, _ = _load_golden()
    meta = data["meta"]
    entries = [
        IndexEntry(metadata=e["metadata"], snapshots=e.get("snapshots", []),
                   reading=e.get("reading"), reading_ref=e.get("reading_ref"))
        for e in data["entries"]
    ]
    links = [LinkRow(**l) for l in data.get("links", [])]
    handoffs = [HandoffRow(**h) for h in data.get("handoffs", [])]
    rejections = [RejectionRow(**r) for r in data.get("rejections", [])]
    p = tmp_path / "golden.sqlite3"
    stats = build_index(
        p, entries=entries, links=links, handoffs=handoffs,
        rejections=rejections,
        meta=IndexMeta(generation=meta["generation"], built_at=meta["built_at"],
                       agora_main_sha=meta["agora_main_sha"],
                       converter_versions=meta["converter_versions"]),
    )
    assert stats.sessions == 3
    return p


def test_golden_build(tmp_path: Path):
    p = _build_golden(tmp_path)
    con = sqlite3.connect(str(p))
    try:
        assert con.execute("SELECT value FROM meta WHERE key='format'").fetchone() == \
            ("aistorage.searchindex/v1",)
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone() == (3,)
        assert con.execute("SELECT COUNT(*) FROM message_fts").fetchone() == (5,)
        assert con.execute("SELECT COUNT(*) FROM links").fetchone() == (3,)
        assert con.execute("SELECT COUNT(*) FROM handoffs").fetchone() == (2,)
        assert con.execute("SELECT COUNT(*) FROM rejections").fetchone() == (2,)
        assert get_reading_ref(con, "opencode:s3") is None  # 轉換失敗無 reading
        assert [h.handoff_id for h in get_handoffs(con, open_only=True)] == \
            ["handoff:01ARZ3NDEKTSV4RRFFQ69G5FAZ"]
    finally:
        con.close()


def test_golden_queries(tmp_path: Path):
    _, queries = _load_golden()
    p = _build_golden(tmp_path)
    con = sqlite3.connect(str(p))
    try:
        for case in queries["queries"]:
            qd = dict(case["query"])
            if qd.get("cursor") is not None:
                qd["cursor"] = tuple(qd["cursor"])
            hits, nxt = search(con, Query(**qd))
            exp = case["expected"]
            assert [h.session.session_id for h in hits] == exp["sessions"], case["name"]
            for h in hits:
                assert [m.message_id for m in h.matches] == \
                    exp["matches"].get(h.session.session_id, []), case["name"]
                for m in h.matches:
                    assert m.snippet, case["name"]
            # matched_by 是除錯資訊，不列入跨實作比對（見 searchindex.md）
            assert (list(nxt) if nxt else None) == exp.get("next_cursor"), case["name"]
    finally:
        con.close()
