"""review-2bc0785 的 M1／M2／L：接續記錄的去重、上限與預留。

- **M1**：同一個新 session 對同一個被接續 session **只能有一條**接續 Link。
  claim 與 continuation 共用同一個檢查，所以兩種順序的結果一樣，不會出現
  「先記的那筆贏、第二筆變成 already」的情況。
- **M2**：每個 profile 每輪能建立的預留／接續數量有上限；預留出來的新 session
  狀態是 `reserved` 並帶期限（`reserved_until`），**期 1 不自動刪除**。
- **L**：接續目標只能是主 Session（與交接單一致）、已經有同一條 Link 時提早回傳、
  被拒時 Agora 裡不留預留殘留。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aistorage.agora import AgoraStore, FakeRawStorage
from aistorage.agora.apply import (
    DEFAULT_MAX_LINKS_PER_PROFILE_PER_ROUND,
    DEFAULT_RESERVED_TTL_DAYS,
    apply_claim,
    apply_continuation,
    apply_session,
)
from aistorage.agora.layout import continuation_link_dir
from aistorage.clock import FixedClock
from aistorage.converters.base import SessionFacts
from aistorage.intake import Decision, DecisionKind, InboxItem
from aistorage.schema import generate_ulid

PRODUCER = "profile:mac-opencode"
OTHER = "profile:other"
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


def _msg(mid: str, completed: bool = True, reverted: bool = False) -> dict:
    return {"message_id": mid, "completed": completed, "reverted": reverted}


def _write_raw(tmp_path: Path, name: str, messages: list[dict], *,
               title: str | None = None) -> tuple[Path, str, int]:
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
                 messages: list[dict], *, name: str,
                 parent_id: str | None = None,
                 at: str = "2026-09-27T08:00:00Z") -> str:
    """把一個 Session 套進真本，回傳它那份快照的雜湊。"""
    raw_p, sha, size = _write_raw(tmp_path, name, messages)
    src, sid = session_id.split(":", 1)
    record = {"id": session_id, "type": "session", "producer": PRODUCER,
              "created_at": at, "updated_at": at, "case_id": None,
              "provenance": None}
    sidecar = {
        "session": {"source": src, "source_session_id": sid, "snapshot_at": at,
                    "status": "running", "in_progress": False,
                    **({"parent_id": parent_id} if parent_id else {})},
        "raw": {"sha256": sha, "size": size}, "body": {},
    }
    result = apply_session(store, _dec(record, sidecar, raw_p), FakeConverter(), CLOCK)
    assert result.ok, result.code
    return sha


def _handoff(ulid: str, target_id: str, snap_sha: str, mid: str, *,
              content: str = "接手") -> tuple[dict, dict]:
    record = {"id": f"handoff:{ulid}", "type": "handoff", "producer": PRODUCER,
              "created_at": "2026-09-27T08:30:00Z", "updated_at": "2026-09-27T08:30:00Z",
              "case_id": None, "provenance": None}
    sidecar = {"body": {"target_session_id": target_id,
                        "continuation": {"snapshot_sha256": snap_sha,
                                         "message_id": mid},
                        "content": content}}
    return record, sidecar


def _claim(ulid: str, handoff_ulid: str, claimer: str,
           producer: str = PRODUCER,
           reserved: tuple[Path, str, int] | None = None) -> tuple[dict, dict]:
    record = {"id": f"claim:{ulid}", "type": "claim", "producer": producer,
              "created_at": "2026-09-27T08:40:00Z", "updated_at": "2026-09-27T08:40:00Z",
              "case_id": None, "provenance": None}
    sidecar: dict = {"body": {"handoff_id": f"handoff:{handoff_ulid}",
                              "claimer_session_id": claimer}}
    if reserved is not None:
        _raw_p, sha, size = reserved
        sidecar["raw"] = {"sha256": sha, "size": size}
        sidecar["session"] = {
            "source": claimer.split(":", 1)[0],
            "source_session_id": claimer.split(":", 1)[1],
            "snapshot_at": "2026-09-27T08:40:00Z", "status": "running",
            "in_progress": False, "parent_id": None, "reserving": True,
        }
    return record, sidecar


def _continuation(new_id: str, target_id: str, snap_sha: str, mid: str, *,
                  ulid: str | None = None,
                  reserved: tuple[Path, str, int] | None = None,
                  at: str = "2026-09-27T08:40:00Z",
                  producer: str = PRODUCER) -> tuple[dict, dict]:
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


def _apply_cont(store: AgoraStore, record: dict, sidecar: dict,
                reserved: tuple[Path, str, int] | None = None,
                *, producer: str = PRODUCER, **kw):
    raw_path = reserved[0] if reserved is not None else None
    return apply_continuation(
        store, _dec(record, sidecar, raw_path, producer), FakeConverter(), CLOCK, **kw)


def _apply_claim(store: AgoraStore, record: dict, sidecar: dict,
                 reserved: tuple[Path, str, int] | None = None,
                 *, producer: str = PRODUCER, **kw):
    raw_path = reserved[0] if reserved is not None else None
    return apply_claim(store, _dec(record, sidecar, raw_path, producer), CLOCK, **kw)


def _links_of(store: AgoraStore, new_id: str) -> list[dict]:
    base = store.worktree / continuation_link_dir(new_id)
    if not base.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(base.glob("*.json"))]


# ---------------------------------------------------------------------------
# M1：一個新 session 對同一個來源只有一條 Link（兩種順序）
# ---------------------------------------------------------------------------


def _handoff_on(store: AgoraStore, tmp_path: Path, target: str, name: str,
                mid: str = "m1") -> tuple[str, str]:
    """在目標 session 上放一張交接單，回傳（handoff ulid, 快照雜湊）。"""
    from aistorage.agora.apply import apply_handoff

    sha = _put_session(store, tmp_path, target, [_msg("m1"), _msg("m2")], name=name)
    ulid = generate_ulid()
    rec, sc = _handoff(ulid, target, sha, mid)
    result = apply_handoff(store, _dec(rec, sc), FakeConverter(), CLOCK)
    assert result.ok, result.code
    return ulid, sha


def test_claim_then_continuation_leaves_exactly_one_link(tmp_path: Path):
    """M1（順序一）：先 claim 一張交接單，再對同一個 session 直接接續 → 只有一條。"""
    store = _new_store(tmp_path)
    h_ulid, sha = _handoff_on(store, tmp_path, "opencode:s1", "s1.raw", mid="m2")

    reserved = _write_raw(tmp_path, "reserved-claim.json", [])
    claimed = _claim(generate_ulid(), h_ulid, "opencode:s2", reserved=reserved)
    assert _apply_claim(store, *claimed, reserved).ok
    assert len(_links_of(store, "opencode:s2")) == 1

    cont = _continuation("opencode:s2", "opencode:s1", sha, "m2")
    result = _apply_cont(store, *cont)
    assert not result.ok and result.code == "duplicate_link"
    assert len(_links_of(store, "opencode:s2")) == 1, "不得寫出第二條"


def test_continuation_then_claim_leaves_exactly_one_link(tmp_path: Path):
    """M1（順序二）：先直接接續，再認領同一個 session 的交接單 → 結果一樣。"""
    store = _new_store(tmp_path)
    h_ulid, sha = _handoff_on(store, tmp_path, "opencode:s1", "s1.raw", mid="m2")

    reserved = _write_raw(tmp_path, "reserved-cont.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m2", reserved=reserved)
    assert _apply_cont(store, *cont, reserved).ok
    assert len(_links_of(store, "opencode:s2")) == 1

    # 新 session 已經在 Agora 裡（上面那筆接續預留出來的），所以這次認領不帶預留
    claimed = _claim(generate_ulid(), h_ulid, "opencode:s2")
    result = _apply_claim(store, *claimed)
    assert not result.ok and result.code == "duplicate_link"
    assert len(_links_of(store, "opencode:s2")) == 1
    # 交接單仍然是「沒人認領」——被拒的認領不會把它標成已認領
    handoff = store.get_record(f"handoff:{h_ulid}")
    assert handoff is not None and handoff["claimed_by"] is None


def test_claim_of_a_second_handoff_of_the_same_session_is_rejected(tmp_path: Path):
    """M1：同一個新 session 認領**同一個來源**的第二張交接單 → `duplicate_link`。

    接續點相同也一樣拒：兩個起點指向同一個 session 就是同一件事（1→1 或 1→n），
    不是 n→1。要分岔就各自跑一次 checkout。
    """
    from aistorage.agora.apply import apply_handoff

    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                       name="s1.raw")
    ulids = []
    for content in ("工作甲", "工作乙"):          # 兩張單，同一個接續點
        ulid = generate_ulid()
        rec, sc = _handoff(ulid, "opencode:s1", sha, "m2", content=content)
        assert apply_handoff(store, _dec(rec, sc), FakeConverter(), CLOCK).ok
        ulids.append(ulid)

    reserved = _write_raw(tmp_path, "reserved-claim.json", [])
    first = _claim(generate_ulid(), ulids[0], "opencode:s2", reserved=reserved)
    assert _apply_claim(store, *first, reserved).ok
    second = _claim(generate_ulid(), ulids[1], "opencode:s2")
    result = _apply_claim(store, *second)
    assert not result.ok and result.code == "duplicate_link"
    assert len(_links_of(store, "opencode:s2")) == 1
    # 認領本身沒有進真本，交接單也仍然是「沒人認領」（別人還能接）
    assert store.get_record(second[0]["id"]) is None
    handoff = store.get_record(f"handoff:{ulids[1]}")
    assert handoff is not None and handoff["claimed_by"] is None


def test_claiming_different_sources_still_gives_one_link_each(tmp_path: Path):
    """n→1（統合）不受影響：不同來源各一條 Link。"""
    store = _new_store(tmp_path)
    sha_a = _put_session(store, tmp_path, "opencode:s2", [_msg("a1")], name="s2.raw")
    sha_b = _put_session(store, tmp_path, "opencode:s3", [_msg("b1")], name="s3.raw")

    # 直接走 apply_claim 需要先有交接單；這裡用 continuation 驗同一個規則的另一面
    reserved = _write_raw(tmp_path, "reserved.json", [])
    first = _continuation("opencode:s4", "opencode:s2", sha_a, "a1", reserved=reserved)
    assert _apply_cont(store, *first, reserved).ok
    second = _continuation("opencode:s4", "opencode:s3", sha_b, "b1")
    assert _apply_cont(store, *second).ok
    assert sorted(l["to"] for l in _links_of(store, "opencode:s4")) == [
        "opencode:s2", "opencode:s3"]


# ---------------------------------------------------------------------------
# M2：每個 profile 每輪的上限
# ---------------------------------------------------------------------------


def test_link_quota_is_enforced_per_profile_per_round(tmp_path: Path):
    """超過上限就明確拒收，寫入端看得到原因，而且**不留下預留**。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    for index in range(2):
        reserved = _write_raw(tmp_path, f"reserved{index}.json", [])
        cont = _continuation(f"opencode:new{index}", "opencode:s1", sha, "m1",
                             reserved=reserved)
        assert _apply_cont(store, *cont, reserved, max_links_per_round=2).ok

    reserved = _write_raw(tmp_path, "reserved-over.json", [])
    cont = _continuation("opencode:over", "opencode:s1", sha, "m1", reserved=reserved)
    result = _apply_cont(store, *cont, reserved, max_links_per_round=2)
    assert not result.ok and result.code == "link_quota_exceeded"
    assert store.get_session("opencode:over") is None, "被拒不留預留殘留"
    assert _links_of(store, "opencode:over") == []


def test_link_quota_is_per_profile(tmp_path: Path):
    """額度是**每個 profile** 各自的：別人的額度滿了不影響這個 profile。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    reserved_a = _write_raw(tmp_path, "a.json", [])
    cont_a = _continuation("opencode:new-a", "opencode:s1", sha, "m1",
                           reserved=reserved_a, producer=PRODUCER)
    assert _apply_cont(store, *cont_a, reserved_a, producer=PRODUCER,
                       max_links_per_round=1).ok

    reserved_b = _write_raw(tmp_path, "b.json", [])
    cont_b = _continuation("opencode:new-b", "opencode:s1", sha, "m1",
                           reserved=reserved_b, producer=OTHER)
    result = _apply_cont(store, *cont_b, reserved_b, producer=OTHER,
                         max_links_per_round=1)
    assert result.ok, (result.code, "另一個 profile 還有額度")

    # 同一個 profile 的第二筆就被擋下來
    reserved_c = _write_raw(tmp_path, "c.json", [])
    cont_c = _continuation("opencode:new-c", "opencode:s1", sha, "m1",
                           reserved=reserved_c, producer=PRODUCER)
    assert not _apply_cont(store, *cont_c, reserved_c, producer=PRODUCER,
                           max_links_per_round=1).ok


def test_rejected_retry_does_not_consume_quota(tmp_path: Path):
    """重送（`already`）不佔額度——否則逾時重跑會把自己的額度吃掉。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_links_per_round=1).ok

    again = _continuation("opencode:s2", "opencode:s1", sha, "m1")
    result = _apply_cont(store, *again, max_links_per_round=1)
    assert result.ok and result.code == "already", result.code


def test_default_link_quota_is_a_positive_number():
    """預設值必須存在且為正（呼叫端不給就是走預設）。"""
    assert DEFAULT_MAX_LINKS_PER_PROFILE_PER_ROUND > 0


def test_link_quota_resets_in_the_next_round(tmp_path: Path):
    """上限是**每輪**的，不是這個 profile 累積的總量。

    新的 `AgoraStore` 就是新的一輪（提交流程一輪開一個 store，ADR 0009），所以真本
    裡已經有的那些接續記錄不該讓這個 profile 下一輪就沒額度可用。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    for index in range(2):
        reserved = _write_raw(tmp_path, f"round1-{index}.json", [])
        cont = _continuation(f"opencode:r1-{index}", "opencode:s1", sha, "m1",
                             reserved=reserved)
        assert _apply_cont(store, *cont, reserved, max_links_per_round=2).ok

    over = _write_raw(tmp_path, "round1-over.json", [])
    cont = _continuation("opencode:r1-over", "opencode:s1", sha, "m1", reserved=over)
    assert _apply_cont(store, *cont, over, max_links_per_round=2).code \
        == "link_quota_exceeded"

    # 下一輪：額度歸零，前一輪的兩筆不佔這輪的額度
    store2 = _new_store(tmp_path)
    assert store2.get_session("opencode:s1") is not None, "真本沿用同一個工作樹"
    for index in range(2):
        reserved = _write_raw(tmp_path, f"round2-{index}.json", [])
        cont = _continuation(f"opencode:r2-{index}", "opencode:s1", sha, "m1",
                             reserved=reserved)
        assert _apply_cont(store2, *cont, reserved, max_links_per_round=2).ok

    # 而不同 profile 的額度是分開算的，不會被上一輪的紀錄牽動
    store3 = _new_store(tmp_path)
    other = _write_raw(tmp_path, "round3.json", [])
    cont = _continuation("opencode:r3", "opencode:s1", sha, "m1",
                         reserved=other, producer=OTHER)
    assert _apply_cont(store3, *cont, other, producer=OTHER,
                       max_links_per_round=1).ok


# ---------------------------------------------------------------------------
# M2：預留的狀態與期限
# ---------------------------------------------------------------------------


def test_reserved_session_is_marked_reserved_with_a_deadline(tmp_path: Path):
    """預留出來的新 session 狀態是 `reserved` 並帶期限，不是 `running`。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [], title="接手甲")
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved).ok

    session = store.get_session("opencode:s2")
    assert session is not None
    assert session.status == "reserved"
    assert session.extra["reserved_until"] == "2026-10-04T09:00:00.000000Z"
    assert session.extra["reserved_by"] == cont[0]["id"]


def test_reservation_disappears_once_the_session_really_starts(tmp_path: Path):
    """有人真的載入那個 session（第一份真實快照）之後，它就是普通的工作中 session。"""
    store = _new_store(tmp_path)
    sha1 = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha1, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved).ok

    raw_p, sha, size = _write_raw(tmp_path, "s2.raw", [_msg("hi")], title="開工了")
    record = {"id": "opencode:s2", "type": "session", "producer": PRODUCER,
              "created_at": "2026-09-27T08:00:00Z", "updated_at": "2026-09-27T10:00:00Z",
              "case_id": None, "provenance": None}
    sidecar = {"session": {"source": "opencode", "source_session_id": "s2",
                           "snapshot_at": "2026-09-27T10:00:00Z", "status": "running",
                           "in_progress": False},
               "raw": {"sha256": sha, "size": size}, "body": {}}
    assert apply_session(store, _dec(record, sidecar, raw_p), FakeConverter(), CLOCK).ok

    session = store.get_session("opencode:s2")
    assert session.status == "running"
    assert "reserved_until" not in session.extra
    assert "reserved_by" not in session.extra


def test_reserved_ttl_default_is_a_finite_number_of_days():
    assert 0 < DEFAULT_RESERVED_TTL_DAYS <= 90


# ---------------------------------------------------------------------------
# L：接續目標只能是主 Session、提早回傳、被拒不留殘留
# ---------------------------------------------------------------------------


def test_continuation_to_a_subsession_is_rejected(tmp_path: Path):
    """被接續的目標也必須是主 Session（與交接單一致）。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:sub", [_msg("m1")], name="sub.raw",
                       parent_id="opencode:main")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:sub", sha, "m1", reserved=reserved)
    result = _apply_cont(store, *cont, reserved)
    assert not result.ok and result.code == "continuation_to_subsession"
    assert store.get_session("opencode:s2") is None, "被拒不留預留殘留"
    assert _links_of(store, "opencode:s2") == []


def test_claim_of_a_handoff_on_a_subsession_is_rejected(tmp_path: Path):
    """交接單掛在子 session 上時，認領（=接續）同樣拒收。"""
    from aistorage.agora.apply import apply_handoff

    store = _new_store(tmp_path)
    _put_session(store, tmp_path, "opencode:main", [_msg("m1")], name="main.raw")
    sha = _put_session(store, tmp_path, "opencode:sub", [_msg("m1")], name="sub.raw",
                       parent_id="opencode:main")
    rec, sc = _handoff(generate_ulid(), "opencode:sub", sha, "m1")
    assert apply_handoff(store, _dec(rec, sc), FakeConverter(), CLOCK).ok

    reserved = _write_raw(tmp_path, "reserved-claim.json", [])
    result = _apply_claim(store, *_claim(generate_ulid(), rec["id"].split(":")[1],
                                         "opencode:s2", reserved=reserved), reserved)
    assert not result.ok and result.code == "continuation_to_subsession"
    assert store.get_session("opencode:s2") is None
    assert _links_of(store, "opencode:s2") == []


def test_already_recorded_link_returns_without_converting_again(tmp_path: Path):
    """L：已經有同一條 Link 時**提早回傳**，不必再轉換那份快照一次。

    用一個會爆的轉換器證明「沒有再轉換」：既有那條 Link 一樣成立。
    """
    class BoomConverter(FakeConverter):
        def convert(self, raw_path, *, session_id, parent_id=None,
                    snapshot_sha256=None):
            raise AssertionError("不該再轉換一次")

    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved).ok

    again = _continuation("opencode:s2", "opencode:s1", sha, "m1")
    result = apply_continuation(
        store, _dec(again[0], again[1], None), BoomConverter(), CLOCK)
    assert result.ok and result.code == "already", result.code
    assert len(_links_of(store, "opencode:s2")) == 1


def test_rejected_continuation_leaves_no_reservation(tmp_path: Path):
    """被拒的接續單不留任何預留殘留（每一條拒收路徑都查一遍）。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                       name="s1.raw")
    _put_session(store, tmp_path, "opencode:sub", [_msg("m1")], name="sub.raw",
                 parent_id="opencode:s1")
    # 讓 opencode:s2 已經在接續 opencode:s1（接續點 m1）
    first_reserved = _write_raw(tmp_path, "first.json", [])
    first = _continuation("opencode:s2", "opencode:s1", sha, "m1",
                          reserved=first_reserved)
    assert _apply_cont(store, *first, first_reserved).ok

    cases = {
        # 接續點不在被接續 session 的快照歷史裡
        "invalid_continuation": ("opencode:reject-a", "opencode:s1", "0" * 64, "m1"),
        # 目標不存在
        "unknown_target": ("opencode:reject-b", "opencode:ghost", sha, "m1"),
        # 被接續的目標是子 session
        "continuation_to_subsession": ("opencode:reject-c", "opencode:sub", sha, "m1"),
        # 同一個新 session 對同一個來源的第二個起點
        "duplicate_link": ("opencode:s2", "opencode:s1", sha, "m2"),
    }
    for code, (new_id, target, snap, mid) in cases.items():
        reserved = _write_raw(tmp_path, f"{code}.json", [])
        cont = _continuation(new_id, target, snap, mid, reserved=reserved)
        result = _apply_cont(store, *cont, reserved)
        assert not result.ok and result.code == code, (code, result.code)
        if new_id != "opencode:s2":
            assert store.get_session(new_id) is None, f"{code} 留下了預留"
            assert _links_of(store, new_id) == []
            assert not (store.worktree / continuation_link_dir(new_id)).exists()
    assert len(_links_of(store, "opencode:s2")) == 1, "既有那條不受影響"

    # 冒充別人的名義建立接續（預留的是一個已經存在、屬於別人的 session）
    cont = _continuation("opencode:stolen", "opencode:s1", sha, "m1", producer=OTHER)
    result = _apply_cont(store, *cont, None, producer=OTHER)
    assert not result.ok and result.code == "unknown_claimer"
    assert _links_of(store, "opencode:stolen") == []


def test_rejected_claim_leaves_no_reservation(tmp_path: Path):
    """被拒的認領同樣不留預留（交接單也仍然是「沒人認領」）。"""
    from aistorage.agora.apply import apply_handoff

    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    h1, h2 = generate_ulid(), generate_ulid()
    for ulid in (h1, h2):
        rec, sc = _handoff(ulid, "opencode:s1", sha, "m1")
        assert apply_handoff(store, _dec(rec, sc), FakeConverter(), CLOCK).ok

    # opencode:new1 先接走了 h1（也就是接續了 opencode:s1）
    reserved_first = _write_raw(tmp_path, "first-claim.json", [])
    first = _claim(generate_ulid(), h1, "opencode:new1", reserved=reserved_first)
    assert _apply_claim(store, *first, reserved_first).ok

    # 同一個新 session 再認領同一個來源的 h2 → 拒收，預留不會留下來
    reserved = _write_raw(tmp_path, "reserved-second.json", [])
    result = _apply_claim(store, *_claim(generate_ulid(), h2, "opencode:new1"), reserved)
    assert not result.ok and result.code == "duplicate_link"
    assert _links_of(store, "opencode:new1") == [] or \
        len(_links_of(store, "opencode:new1")) == 1
    # 交接單仍然是「沒人認領」，別人還能接
    handoff = store.get_record(f"handoff:{h2}")
    assert handoff is not None and handoff["claimed_by"] is None
