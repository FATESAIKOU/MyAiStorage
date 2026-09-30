"""impl2 M6：Session 之間只有四種關係（1→1、1→n、n→1、n↔m），**一律記錄**。

接續**不需要交接單**——交接單只是可選的便利。所以 `agora checkout` 以
`<session>[@<訊息>]` 為起點時送的是一筆**接續單**（continuation），提交流程收進
Agora 後建出接續 Link；起點是交接單時沿用 claim（claim 本身就代表接續）。

這支檔驗的是「四種關係各自留下一條正確的 Link」以及冪等：
- 1→1：一個新 session 對一個起點一條
- 1→n：同一個起點 checkout n 次，每個新 session 各一條
- n→1：一個新 session 對每個起點各一條
- n↔m：`agora read` 記的參考 Link（不承接工作），接續單不影響它

以及「接續點指向被釘住的快照」與「讀取介面兩個方向都看得到」。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aistorage.agora import AgoraStore, FakeRawStorage
from aistorage.agora.apply import apply_continuation, apply_reference, apply_session
from aistorage.agora.layout import continuation_link_dir
from aistorage.clock import FixedClock
from aistorage.converters.base import ConversionError, SessionFacts
from aistorage.intake import Decision, DecisionKind, InboxItem
from aistorage.publish.plan import collect_pinned_snapshots
from aistorage.publish.publisher import collect_links
from aistorage.schema import generate_ulid

PRODUCER = "profile:mac-opencode"
OTHER = "profile:evil"
CLOCK = FixedClock("2026-09-27T09:00:00Z")


class FakeConverter:
    """raw 是 `{"messages": [{message_id, completed, reverted}, ...]}`。"""

    source = "opencode"

    def _load(self, raw_path: Path) -> dict:
        return json.loads(Path(raw_path).read_bytes().decode("utf-8"))

    def facts(self, raw_path: Path) -> SessionFacts:
        data = self._load(raw_path)
        return SessionFacts(
            title=data.get("title"), created_at="2026-09-27T08:00:00Z",
            updated_at="2026-09-27T08:00:00Z",
            message_ids=tuple(m["message_id"] for m in data.get("messages", [])),
            archived_at=None, last_message_at=None, in_progress=False,
        )

    def convert(self, raw_path: Path, *, session_id: str, parent_id=None,
                snapshot_sha256=None) -> dict:
        raw_bytes = Path(raw_path).read_bytes()
        data = self._load(raw_path)
        return {
            "snapshot_sha256": hashlib.sha256(raw_bytes).hexdigest().lower(),
            "messages": [
                {"message_id": m["message_id"], "index": i,
                 "completed": m.get("completed", True),
                 "reverted": m.get("reverted", False)}
                for i, m in enumerate(data.get("messages", []))
            ],
        }

    def child_session_ids(self, raw_path: Path) -> tuple:
        return ()


class BoomConverter(FakeConverter):
    def convert(self, raw_path: Path, *, session_id: str, parent_id=None,
                snapshot_sha256=None) -> dict:
        raise ConversionError("轉換失敗")


def _msg(mid: str, completed: bool = True, reverted: bool = False) -> dict:
    return {"message_id": mid, "completed": completed, "reverted": reverted}


def _write_raw(tmp_path: Path, name: str, messages: list[dict], *,
               title: str | None = None
               ) -> tuple[Path, str, int]:
    path = tmp_path / name
    data = json.dumps({"messages": messages, "title": title},
                      sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest().lower(), len(data)


def _new_store(tmp_path: Path) -> AgoraStore:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")


def _dec(record: dict, sidecar: dict, raw_path: Path | None = None,
         producer: str = PRODUCER, item_key: str | None = None) -> Decision:
    record = {**record, "producer": producer}
    return Decision(
        kind=DecisionKind.ACCEPT,
        item=InboxItem(item_key=item_key or generate_ulid(),
                       inbox_folder_id="inbox-test"),
        code="ok", producer=producer, record_metadata=record, sidecar=sidecar,
        raw_path=raw_path,
    )


def _put_session(store: AgoraStore, tmp_path: Path, session_id: str,
                 messages: list[dict], *, name: str, conv: FakeConverter | None = None,
                 producer: str = PRODUCER, parent_id: str | None = None,
                 at: str = "2026-09-27T08:00:00Z") -> str:
    """把一個 Session 套進真本，回傳它那份快照的雜湊。"""
    conv = conv or FakeConverter()
    raw_p, sha, size = _write_raw(tmp_path, name, messages)
    src, sid = session_id.split(":", 1)
    record = {"id": session_id, "type": "session", "producer": producer,
              "created_at": at, "updated_at": at, "case_id": None,
              "provenance": None}
    sidecar = {
        "session": {"source": src, "source_session_id": sid, "snapshot_at": at,
                    "status": "running", "in_progress": False,
                    **({"parent_id": parent_id} if parent_id else {})},
        "raw": {"sha256": sha, "size": size}, "body": {},
    }
    result = apply_session(
        store, _dec(record, sidecar, raw_p, producer), conv, CLOCK)
    assert result.ok, result.code
    return sha


def _continuation(ulid: str | None, new_id: str, target_id: str, snap_sha: str,
                  mid: str, *, reserved: tuple[Path, str, int] | None = None,
                  at: str = "2026-09-27T08:40:00Z",
                  producer: str = PRODUCER) -> tuple[dict, dict]:
    """一筆接續單（`agora checkout` 以直接起點送出的那種）。

    `reserved` 有給就帶著**預留**（那個空的新 session 紀錄），沒有就假設新 session
    已經在 Agora 裡（例如它自己先同步過）。
    """
    ulid = ulid or generate_ulid()
    record = {"id": f"continuation:{ulid}", "type": "continuation",
              "producer": producer, "created_at": at, "updated_at": at,
              "case_id": None, "provenance": None}
    sidecar: dict = {
        "body": {"target_session_id": target_id, "new_session_id": new_id,
                 "continuation": {"snapshot_sha256": snap_sha, "message_id": mid}},
    }
    if reserved is not None:
        _raw_p, sha, size = reserved
        sidecar["raw"] = {"sha256": sha, "size": size}
        sidecar["session"] = {
            "source": new_id.split(":", 1)[0],
            "source_session_id": new_id.split(":", 1)[1],
            "snapshot_at": at, "status": "running", "in_progress": False,
            "parent_id": None, "reserving": True,
        }
    return record, sidecar


def _apply(store: AgoraStore, record: dict, sidecar: dict,
           reserved: tuple[Path, str, int] | None = None,
           *, conv: FakeConverter | None = None,
           producer: str = PRODUCER):
    raw_path = reserved[0] if reserved is not None else None
    return apply_continuation(
        store, _dec(record, sidecar, raw_path, producer),
        conv or FakeConverter(), CLOCK)


def _links_of(store: AgoraStore, new_id: str) -> list[dict]:
    base = store.worktree / continuation_link_dir(new_id)
    if not base.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(base.glob("*.json"))]


# ---------------------------------------------------------------------------
# 1→1
# ---------------------------------------------------------------------------


def test_one_to_one_direct_startpoint_records_a_continuation_link(tmp_path: Path):
    """1→1：沒有交接單也要有一條接續 Link，而且連帶生出那個空的新 session。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1",
                       [_msg("m1"), _msg("m2")], name="s1.raw")

    reserved = _write_raw(tmp_path, "reserved1.json", [], title="接著做")
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m2",
                            reserved=reserved)
    result = _apply(store, rec, sc, reserved)

    assert result.ok and result.code == "ok", result.code
    links = _links_of(store, "opencode:s2")
    assert len(links) == 1
    assert links[0]["from"] == "opencode:s2"
    assert links[0]["to"] == "opencode:s1"
    assert links[0]["continuation"] == {"snapshot_sha256": sha, "message_id": "m2"}
    # 預留出來的新 session 也在 Agora 裡（零則訊息）
    session = store.get_session("opencode:s2")
    assert session is not None and session.raw_sha256 == reserved[1]
    assert store.get_record(rec["id"]) is not None


def test_continuation_may_start_from_a_message_other_than_the_last(tmp_path: Path):
    """直接起點可以停在快照裡的**任何**已完成訊息（交接單才必須是最後一則）。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1",
                       [_msg("m1"), _msg("m2"), _msg("m3")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1",
                            reserved=reserved)
    assert _apply(store, rec, sc, reserved).ok
    assert _links_of(store, "opencode:s2")[0]["continuation"]["message_id"] == "m1"


# ---------------------------------------------------------------------------
# 1→n
# ---------------------------------------------------------------------------


def test_one_to_n_gives_each_new_session_its_own_link(tmp_path: Path):
    """1→n：同一個起點 checkout n 次，n 個新 session 各有一條接續 Link。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1",
                       [_msg("m1"), _msg("m2")], name="s1.raw")

    for index in range(3):
        new_id = f"opencode:split{index}"
        reserved = _write_raw(tmp_path, f"reserved{index}.json", [])
        rec, sc = _continuation(None, new_id, "opencode:s1", sha, "m2",
                                reserved=reserved)
        assert _apply(store, rec, sc, reserved).ok
        links = _links_of(store, new_id)
        assert len(links) == 1
        assert links[0]["to"] == "opencode:s1"

    # 被接續的 S1 一點都沒變，而且三條 Link 都指著它（讀取端反向查得到）
    incoming = [r for r in collect_links(store)
                if r.kind == "continuation" and r.to_session_id == "opencode:s1"]
    assert sorted(r.from_session_id for r in incoming) == [
        "opencode:split0", "opencode:split1", "opencode:split2"]


# ---------------------------------------------------------------------------
# n→1
# ---------------------------------------------------------------------------


def test_n_to_one_gives_the_new_session_one_link_per_start_point(tmp_path: Path):
    """n→1：同一個新 session 對每個起點各一條 Link。"""
    store = _new_store(tmp_path)
    sha_a = _put_session(store, tmp_path, "opencode:s2",
                         [_msg("a1")], name="s2.raw")
    sha_b = _put_session(store, tmp_path, "opencode:s3",
                         [_msg("b1"), _msg("b2")], name="s3.raw")

    reserved = _write_raw(tmp_path, "reserved.json", [])
    first = _continuation(None, "opencode:s4", "opencode:s2", sha_a, "a1",
                          reserved=reserved)
    assert _apply(store, first[0], first[1], reserved).ok
    second = _continuation(None, "opencode:s4", "opencode:s3", sha_b, "b2")
    assert _apply(store, second[0], second[1]).ok

    links = _links_of(store, "opencode:s4")
    assert len(links) == 2
    assert sorted(l["to"] for l in links) == ["opencode:s2", "opencode:s3"]
    assert sorted(l["continuation"]["message_id"] for l in links) == ["a1", "b2"]


# ---------------------------------------------------------------------------
# n↔m
# ---------------------------------------------------------------------------


def test_n_to_m_keeps_reference_links_and_never_creates_a_continuation(tmp_path: Path):
    """n↔m：互相參照走參考 Link——只記讀到哪，不承接工作，也沒有接續點。"""
    store = _new_store(tmp_path)
    _put_session(store, tmp_path, "opencode:s2", [_msg("m1")], name="s2.raw")
    _put_session(store, tmp_path, "opencode:s3", [_msg("m1")], name="s3.raw")

    def _reference(ulid: str, from_id: str, to_id: str, read_at: str) -> None:
        record = {"id": f"reference:{ulid}", "type": "reference", "producer": PRODUCER,
                  "created_at": read_at, "updated_at": read_at,
                  "case_id": None, "provenance": None}
        sidecar = {"body": {"from_session_id": from_id, "to_session_id": to_id,
                            "read_snapshot_at": read_at}}
        assert apply_reference(store, _dec(record, sidecar), CLOCK).ok

    _reference(generate_ulid(), "opencode:s2", "opencode:s3", "2026-09-27T08:50:00Z")
    # 再參考一次同一對 → 單調性把它更新成最新讀到的快照時間，不會多一條
    _reference(generate_ulid(), "opencode:s2", "opencode:s3", "2026-09-27T08:59:00Z")
    _reference(generate_ulid(), "opencode:s3", "opencode:s2", "2026-09-27T08:59:00Z")

    refs = [r for r in collect_links(store) if r.kind == "reference"]
    assert len(refs) == 2, "同一對 Session 之間只保留一條參考 Link"
    assert {(r.from_session_id, r.to_session_id) for r in refs} == {
        ("opencode:s2", "opencode:s3"), ("opencode:s3", "opencode:s2")}
    assert all(r.snapshot_sha256 is None for r in refs), "參考沒有接續點"
    assert all(r.message_id is None for r in refs)


# ---------------------------------------------------------------------------
# 接續點、冪等、拒收
# ---------------------------------------------------------------------------


def test_continuation_point_must_point_at_a_pinned_snapshot(tmp_path: Path):
    """接續點要指向**被接續 Session 既有的一份快照**，而且那一則已完成、未撤銷。

    被釘住：Link 寫進去之後，發佈階段就會把那份快照發出來（否則 checkout 交出去的
    原始紀錄在讀取視圖裡沒有對應的東西）。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])

    # 快照不在被接續 Session 的歷史裡
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", "0" * 64, "m1",
                            reserved=reserved)
    r = _apply(store, rec, sc, reserved)
    assert not r.ok and r.code == "invalid_continuation"
    assert store.get_session("opencode:s2") is None, "被拒不能留下預留"

    # 訊息未完成 → 不能當接續點（它可能還會被改寫）
    _put_session(store, tmp_path, "opencode:s3", [_msg("m1", completed=False)],
                 name="s3.raw")
    unfinished = _write_raw(tmp_path, "s3b.raw", [_msg("m1", completed=False)])[1]
    _put_session(store, tmp_path, "opencode:s3", [_msg("m1", completed=False)],
                 name="s3c.raw", at="2026-09-27T08:30:00Z")
    rec, sc = _continuation(None, "opencode:s4", "opencode:s3", unfinished, "m1",
                            reserved=reserved)
    assert not _apply(store, rec, sc, reserved).ok
    assert store.get_session("opencode:s4") is None

    # 正常的一筆：那份快照被釘住
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1",
                            reserved=reserved)
    assert _apply(store, rec, sc, reserved).ok
    pinned = collect_pinned_snapshots(store)
    assert pinned["opencode:s1"] == {sha}


def test_repeated_continuation_is_idempotent_even_with_a_new_item_id(tmp_path: Path):
    """同一個新 session 對同一個起點只能有一條；重複送要冪等。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])

    ulid = generate_ulid()
    first = _continuation(ulid, "opencode:s2", "opencode:s1", sha, "m1",
                          reserved=reserved)
    assert _apply(store, first[0], first[1], reserved).ok

    # 換一個 item id 重送（沒有本機記錄時的預設路徑）→ 不會多一條 Link
    second = _continuation(generate_ulid(), "opencode:s2", "opencode:s1", sha, "m1")
    again = _apply(store, second[0], second[1])
    assert again.ok and again.code == "already"
    assert len(_links_of(store, "opencode:s2")) == 1

    # 同一個 id 重送（`--resume` 沿用同一筆）→ 冪等
    same = _apply(store, first[0], first[1], reserved)
    assert same.ok and same.code == "already"
    assert len(_links_of(store, "opencode:s2")) == 1
    assert len(store.snapshots("opencode:s2")) == 1, "不該多出第二份快照"


def test_second_continuation_to_the_same_session_with_another_point_is_rejected(
        tmp_path: Path):
    """同一個新 session 對同一個被接續 session **只能有一條**接續 Link。"""
    store = _new_store(tmp_path)
    _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1a.raw")
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                       name="s1b.raw", at="2026-09-27T08:30:00Z")
    reserved = _write_raw(tmp_path, "reserved.json", [])

    first = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1",
                          reserved=reserved)
    assert _apply(store, first[0], first[1], reserved).ok

    other = _continuation(None, "opencode:s2", "opencode:s1", sha, "m2")
    r = _apply(store, other[0], other[1])
    assert not r.ok and r.code == "duplicate_link"
    assert len(_links_of(store, "opencode:s2")) == 1


def test_continuation_must_come_from_the_new_session_holder(tmp_path: Path):
    """H3：別人不能替一個 session 宣告接續（會被它認領別人的工作）。

    新 session 已經在 Agora 裡（持有者是 PRODUCER）時才有這個判斷；帶預留的那一筆
    是它自己的建立者，生產者自然一致（同 claim 的預留路徑）。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    _put_session(store, tmp_path, "opencode:s2", [_msg("m1")], name="s2.raw")
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1",
                            producer=OTHER)
    r = _apply(store, rec, sc, producer=OTHER)
    assert not r.ok and r.code == "not_holder"
    assert not (store.worktree / continuation_link_dir("opencode:s2")).exists()
    assert store.get_record(rec["id"]) is None


def test_continuation_to_a_session_that_does_not_exist_is_rejected(tmp_path: Path):
    """沒有預留、而新 session 又不在 Agora → 明確拒收（不能凭空造一個 session）。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1")
    r = _apply(store, rec, sc)
    assert not r.ok and r.code == "unknown_claimer"
    assert store.get_session("opencode:s2") is None


def test_continuation_to_itself_is_rejected(tmp_path: Path):
    """不能接續自己（那不是接續，是同一個 session 自己長大）。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    rec, sc = _continuation(None, "opencode:s1", "opencode:s1", sha, "m1")
    r = _apply(store, rec, sc)
    assert not r.ok and r.code == "self_continuation"


def test_continuation_from_a_subsession_is_rejected(tmp_path: Path):
    """接續只能由主 Session 發起。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    _put_session(store, tmp_path, "opencode:sub", [_msg("m1")], name="sub.raw",
                 parent_id="opencode:s1")
    rec, sc = _continuation(None, "opencode:sub", "opencode:s1", sha, "m1")
    r = _apply(store, rec, sc)
    assert not r.ok and r.code == "continuation_from_subsession"


def test_continuation_to_an_unknown_session_is_rejected(tmp_path: Path):
    store = _new_store(tmp_path)
    rec, sc = _continuation(None, "opencode:s2", "opencode:不存在", "0" * 64, "m1")
    r = _apply(store, rec, sc)
    assert not r.ok and r.code == "unknown_target"


def test_conversion_failure_is_a_rejection_not_a_crash(tmp_path: Path):
    """轉不出閱讀版就明確拒收（寫入階段不會被走到，所以真本不會有半套東西）。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1",
                            reserved=reserved)
    r = _apply(store, rec, sc, reserved, conv=BoomConverter())
    assert not r.ok and r.code == "invalid_continuation"
    assert store.get_session("opencode:s2") is None
    assert not (store.worktree / continuation_link_dir("opencode:s2")).exists()


# ---------------------------------------------------------------------------
# 讀取介面：兩個方向都看得到
# ---------------------------------------------------------------------------


def test_reader_sees_the_link_from_both_sides(tmp_path: Path):
    """`agora show` 讀的 `links` 索引裡，正向（links_out）與反向（links_in）都有。"""
    from aistorage.search.index import LinkRow, build_index, IndexMeta
    from aistorage.search.query import get_links

    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    rec, sc = _continuation(None, "opencode:s2", "opencode:s1", sha, "m1",
                            reserved=reserved)
    assert _apply(store, rec, sc, reserved).ok

    rows = [LinkRow(kind=r.kind, from_session_id=r.from_session_id,
                    to_session_id=r.to_session_id, handoff_id=r.handoff_id,
                    claim_id=r.claim_id, snapshot_sha256=r.snapshot_sha256,
                    message_id=r.message_id, reference_id=r.reference_id,
                    read_snapshot_at=r.read_snapshot_at)
            for r in collect_links(store)]
    index = tmp_path / "index.sqlite"
    build_index(index, entries=(), links=rows,
                meta=IndexMeta(generation=1, built_at="2026-09-27T09:00:00Z",
                               agora_main_sha="x" * 40))

    import sqlite3
    con = sqlite3.connect(str(index))
    try:
        out_s1, in_s1 = get_links(con, "opencode:s1")
        out_s2, in_s2 = get_links(con, "opencode:s2")
    finally:
        con.close()

    assert [l.kind for l in in_s1] == ["continuation"], "S1 看得到被誰接續"
    assert [(l.from_session_id, l.to_session_id, l.snapshot_sha256, l.message_id)
            for l in in_s1] == [("opencode:s2", "opencode:s1", sha, "m1")]
    assert [(l.from_session_id, l.to_session_id) for l in out_s2] == [
        ("opencode:s2", "opencode:s1")], "S2 看得到自己接續了誰、接續點在哪"
    assert out_s1 == []


@pytest.mark.parametrize("relationship", ["1_to_1", "1_to_n", "n_to_1"])
def test_每種接續關係都留下一條_link(tmp_path: Path, relationship: str):
    """三種接續關係（1→1、1→n、n→1）每一種都留下預期數目的接續 Link。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    plan = {
        "1_to_1": [("opencode:s2", "opencode:s1")],
        "1_to_n": [("opencode:s2", "opencode:s1"), ("opencode:s3", "opencode:s1")],
        "n_to_1": [("opencode:s4", "opencode:s1"), ("opencode:s4", "opencode:s2")],
    }[relationship]
    _put_session(store, tmp_path, "opencode:s2", [_msg("m1")], name="s2.raw")

    seen_new: set[str] = set()
    for new_id, target_id in plan:
        reserved = None
        if new_id not in seen_new:
            reserved = _write_raw(tmp_path, f"reserved-{new_id}.json", [])
            seen_new.add(new_id)
        rec, sc = _continuation(None, new_id, target_id, sha, "m1",
                                reserved=reserved)
        assert _apply(store, rec, sc, reserved).ok

    links = [r for r in collect_links(store) if r.kind == "continuation"]
    assert len(links) == len(plan)
    assert sorted((r.from_session_id, r.to_session_id) for r in links) == sorted(plan)
