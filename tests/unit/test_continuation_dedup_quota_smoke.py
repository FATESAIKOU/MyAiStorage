"""接續記錄的去重、預留數量上限與預留本身（review-2bc0785 M1／M2／L、
review-1926cd3-142fd04 M3、review-55edd374 M3）。

- **M1**：同一個新 session 對同一個被接續 session **只能有一條**接續 Link。
  claim 與 continuation 共用同一個檢查，所以兩種順序的結果一樣，不會出現
  「先記的那筆贏、第二筆變成 already」的情況。
- **M2**：預留出來的新 session 狀態是 `reserved` 並帶期限（`reserved_until`），
  **期 1 不自動刪除**。
- **M3**：每個 profile 能同時掛著的**未結預留**（`status=reserved` 且沒有後續
  快照）有上限，**跨輪累計**、直接從真本算。輪數是住民自己觸發的，所以「每輪 N 筆」
  只是限速（PM 裁決，見 decision log 最後一條）。過了期限的預留不佔額度
  （review-55edd374 M3），而拒收訊息會說出是哪些預留佔著。
- **L**：接續目標只能是主 Session（與交接單一致）、已經有同一條 Link 時提早回傳、
  被拒時 Agora 裡不留預留殘留。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aistorage.agora import AgoraStore, FakeRawStorage
from aistorage.agora.apply import (
    DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE,
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
                *, producer: str = PRODUCER, clock: FixedClock = CLOCK, **kw):
    raw_path = reserved[0] if reserved is not None else None
    return apply_continuation(
        store, _dec(record, sidecar, raw_path, producer), FakeConverter(), clock, **kw)


def _apply_claim(store: AgoraStore, record: dict, sidecar: dict,
                 reserved: tuple[Path, str, int] | None = None,
                 *, producer: str = PRODUCER, clock: FixedClock = CLOCK, **kw):
    raw_path = reserved[0] if reserved is not None else None
    return apply_claim(store, _dec(record, sidecar, raw_path, producer), clock, **kw)


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
# M3：每個 profile 的未結預留上限（跨輪累計、從真本算）
# ---------------------------------------------------------------------------


def test_open_reservation_cap_is_enforced_and_leaves_nothing(tmp_path: Path):
    """超過上限就明確拒收，寫入端看得到原因，而且**不留下預留**。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    for index in range(2):
        reserved = _write_raw(tmp_path, f"reserved{index}.json", [])
        cont = _continuation(f"opencode:new{index}", "opencode:s1", sha, "m1",
                             reserved=reserved)
        assert _apply_cont(store, *cont, reserved, max_open_reservations=2).ok

    reserved = _write_raw(tmp_path, "reserved-over.json", [])
    cont = _continuation("opencode:over", "opencode:s1", sha, "m1", reserved=reserved)
    result = _apply_cont(store, *cont, reserved, max_open_reservations=2)
    assert not result.ok and result.code == "link_quota_exceeded"
    assert store.get_session("opencode:over") is None, "被拒不留預留殘留"
    assert _links_of(store, "opencode:over") == []


def test_open_reservation_cap_is_per_profile(tmp_path: Path):
    """上限是**每個 profile** 各自的：別人的預留不影響這個 profile。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    reserved_a = _write_raw(tmp_path, "a.json", [])
    cont_a = _continuation("opencode:new-a", "opencode:s1", sha, "m1",
                           reserved=reserved_a, producer=PRODUCER)
    assert _apply_cont(store, *cont_a, reserved_a, producer=PRODUCER,
                       max_open_reservations=1).ok

    reserved_b = _write_raw(tmp_path, "b.json", [])
    cont_b = _continuation("opencode:new-b", "opencode:s1", sha, "m1",
                           reserved=reserved_b, producer=OTHER)
    result = _apply_cont(store, *cont_b, reserved_b, producer=OTHER,
                         max_open_reservations=1)
    assert result.ok, (result.code, "另一個 profile 還有額度")

    # 同一個 profile 的第二筆就被擋下來
    reserved_c = _write_raw(tmp_path, "c.json", [])
    cont_c = _continuation("opencode:new-c", "opencode:s1", sha, "m1",
                           reserved=reserved_c, producer=PRODUCER)
    result_c = _apply_cont(store, *cont_c, reserved_c, producer=PRODUCER,
                           max_open_reservations=1)
    assert not result_c.ok and result_c.code == "link_quota_exceeded"
    assert store.get_session("opencode:new-c") is None


def test_idempotent_retry_does_not_consume_quota(tmp_path: Path):
    """重送（`already`）不佔額度——否則逾時重跑會把自己的額度吃掉。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_open_reservations=1).ok

    again = _continuation("opencode:s2", "opencode:s1", sha, "m1")
    result = _apply_cont(store, *again, max_open_reservations=1)
    assert result.ok and result.code == "already", result.code


def test_default_open_reservation_cap_is_a_positive_number():
    """預設值必須存在且為正（呼叫端不給就是走預設）。"""
    assert DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE > 0


def test_open_reservation_cap_is_cumulative_across_rounds(tmp_path: Path):
    """上限是**跨輪累計**的：下一輪不會因為是新一輪就歸零。

    輪數是住民自己觸發的（`sync and commit` 會觸發 workflow），所以「每輪 N 筆」
    只是限速、總量沒有上限（review-1926cd3-142fd04 M3，PM 裁決）。新的
    `AgoraStore` 就是新的一輪（提交流程一輪開一個 store，ADR 0009），工作樹沿用，
    所以第一輪掛著的預留在第二輪**仍然算數**。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    for index in range(2):
        reserved = _write_raw(tmp_path, f"round1-{index}.json", [])
        cont = _continuation(f"opencode:r1-{index}", "opencode:s1", sha, "m1",
                             reserved=reserved)
        assert _apply_cont(store, *cont, reserved, max_open_reservations=2).ok

    over = _write_raw(tmp_path, "round1-over.json", [])
    cont = _continuation("opencode:r1-over", "opencode:s1", sha, "m1", reserved=over)
    assert _apply_cont(store, *cont, over, max_open_reservations=2).code \
        == "link_quota_exceeded"

    # 下一輪：新的 store、同一個工作樹 → 前一輪那兩筆仍然佔著額度
    store2 = _new_store(tmp_path)
    assert store2.get_session("opencode:s1") is not None, "真本沿用同一個工作樹"
    reserved2 = _write_raw(tmp_path, "round2.json", [])
    cont2 = _continuation("opencode:r2", "opencode:s1", sha, "m1", reserved=reserved2)
    result = _apply_cont(store2, *cont2, reserved2, max_open_reservations=2)
    assert not result.ok and result.code == "link_quota_exceeded", (
        "第二輪不該重新給一份額度")
    assert store2.get_session("opencode:r2") is None

    # 而不同 profile 的額度是分開算的，不會被別人上一輪的預留牽動
    store3 = _new_store(tmp_path)
    other = _write_raw(tmp_path, "round3.json", [])
    cont3 = _continuation("opencode:r3", "opencode:s1", sha, "m1",
                          reserved=other, producer=OTHER)
    assert _apply_cont(store3, *cont3, other, producer=OTHER,
                       max_open_reservations=1).ok


def test_a_reservation_stops_counting_once_a_follow_up_snapshot_arrives(tmp_path: Path):
    """預留有了第一份真實快照之後就不再計數，額度回到這個 profile 手上。

    否則正常開工的 profile 會被自己的歷史鎖死——預留只是「還沒人開工」，開了工
    就不該繼續佔著成本上限。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_open_reservations=1).ok

    blocked = _write_raw(tmp_path, "blocked.json", [])
    blocked_cont = _continuation("opencode:s3", "opencode:s1", sha, "m1",
                                 reserved=blocked)
    assert _apply_cont(store, *blocked_cont, blocked,
                       max_open_reservations=1).code == "link_quota_exceeded"

    # opencode:s2 真的開工了：狀態離開 reserved，也多了第二份快照
    raw_p, raw_sha, size = _write_raw(tmp_path, "s2.raw", [_msg("hi")], title="開工了")
    record = {"id": "opencode:s2", "type": "session", "producer": PRODUCER,
              "created_at": "2026-09-27T08:00:00Z", "updated_at": "2026-09-27T10:00:00Z",
              "case_id": None, "provenance": None}
    sidecar = {"session": {"source": "opencode", "source_session_id": "s2",
                           "snapshot_at": "2026-09-27T10:00:00Z", "status": "running",
                           "in_progress": False},
               "raw": {"sha256": raw_sha, "size": size}, "body": {}}
    assert apply_session(store, _dec(record, sidecar, raw_p), FakeConverter(), CLOCK).ok

    # 額度回來了
    later = _write_raw(tmp_path, "later.json", [])
    later_cont = _continuation("opencode:s4", "opencode:s1", sha, "m1",
                               reserved=later)
    assert _apply_cont(store, *later_cont, later, max_open_reservations=1).ok


def test_a_reservation_with_a_follow_up_snapshot_stops_counting_even_if_status_lags(
        tmp_path: Path):
    """快照已經有後續時就不再計數，**即使狀態欄位還寫著 `reserved`**。

    正常流程不會出現這種行（`apply_session` 收到新快照就會把狀態換成
    running／stopped），但期 1 不會自動刪除過期的預留，所以真本裡本來就會長期
    存在「狀態是預留」的列。判斷用兩個條件而不是只看狀態，理由正是不要讓一筆狀態
    欄位沒跟上來的預留永遠佔著額度。這裡繞過 `apply_session` 直接改快照歷史，把這個
    立場固定住。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_open_reservations=1).ok

    blocked = _write_raw(tmp_path, "blocked.json", [])
    blocked_cont = _continuation("opencode:s3", "opencode:s1", sha, "m1",
                                 reserved=blocked)
    assert _apply_cont(store, *blocked_cont, blocked,
                       max_open_reservations=1).code == "link_quota_exceeded"

    # 真本裡 opencode:s2 多了一份快照，但狀態欄位仍是 reserved
    snaps_p = store.worktree / "sessions" / "opencode" / "s2" / "snapshots.jsonl"
    entry = json.loads(snaps_p.read_text(encoding="utf-8").splitlines()[0])
    with snaps_p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**entry, "snapshot_sha256": "b" * 64}) + "\n")
    assert store.get_session("opencode:s2").status == "reserved"

    # 這一筆不再算未結預留 → 額度回來
    later = _write_raw(tmp_path, "later.json", [])
    later_cont = _continuation("opencode:s4", "opencode:s1", sha, "m1",
                               reserved=later)
    assert _apply_cont(store, *later_cont, later, max_open_reservations=1).ok


def test_a_link_without_a_new_reservation_does_not_consume_the_cap(tmp_path: Path):
    """不預留的接續不佔額度：它不讓未結預留數變多。

    被接續的 session 已經在 Agora 裡時（不是 `agora checkout` 預留出來的新
    session），這筆接續只多一條 Link，不多一個沒人開工的 Session。
    """
    from aistorage.agora.apply import _open_reservation_ids

    store = _new_store(tmp_path)
    sha_a = _put_session(store, tmp_path, "opencode:s2", [_msg("a1")], name="s2.raw")
    sha_b = _put_session(store, tmp_path, "opencode:s3", [_msg("b1")], name="s3.raw")

    # opencode:s4 已經在 Agora（不是預留），用它當新 session 接兩個來源（n→1）
    _put_session(store, tmp_path, "opencode:s4", [_msg("s4")], name="s4.raw")
    assert _open_reservation_ids(store, PRODUCER, CLOCK.now()) == []

    first = _continuation("opencode:s4", "opencode:s2", sha_a, "a1")
    assert _apply_cont(store, *first, max_open_reservations=1).ok
    second = _continuation("opencode:s4", "opencode:s3", sha_b, "b1")
    assert _apply_cont(store, *second, max_open_reservations=1).ok
    assert len(_links_of(store, "opencode:s4")) == 2


def test_a_cap_of_zero_or_less_is_a_programming_error(tmp_path: Path):
    """0 或負數**不**代表「不設上限」：apply 直接報錯，不會默默放行。

    設定檔在載入時就擋掉（`committer/config.py` 另有測試），這裡是第二道防線——
    一個打錯字不該讓成本上限整個消失。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    for bad in (0, -1):
        with pytest.raises(ValueError, match="max_open_reservations"):
            _apply_cont(store, *cont, reserved, max_open_reservations=bad)


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


# ---------------------------------------------------------------------------
# review-55edd374 M3：過期的預留不佔額度；拒收訊息說出是哪些預留佔著
# ---------------------------------------------------------------------------


def test_expired_reservations_stop_consuming_the_cap(tmp_path: Path):
    """過了 `reserved_until` 的預留不佔額度——放棄太多次 checkout 的 profile
    過了一週就自己好，不必管理者先動手抹（review-55edd374 M3）。

    這是 M3 指的「會卡住的地雷」：期 1 不自動刪過期的預留，所以原本這 20 筆會
    永遠算在裡面，之後每一次 checkout 都得到 `link_quota_exceeded`。
    """
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    reserved = _write_raw(tmp_path, "expired.json", [])
    cont = _continuation("opencode:old", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_open_reservations=1).ok

    # 期限還沒到：一樣擋得住
    soon = _write_raw(tmp_path, "soon.json", [])
    soon_cont = _continuation("opencode:soon", "opencode:s1", sha, "m1", reserved=soon)
    assert _apply_cont(store, *soon_cont, soon, max_open_reservations=1).code \
        == "link_quota_exceeded"

    # 期限過了（CLOCK 往後推一天以上，TTL 是 7 天）：額度回來了
    later_clock = FixedClock("2026-10-05T09:00:00Z")
    fresh = _write_raw(tmp_path, "fresh.json", [])
    fresh_cont = _continuation("opencode:fresh", "opencode:s1", sha, "m1",
                               reserved=fresh)
    assert _apply_cont(store, *fresh_cont, fresh, max_open_reservations=1,
                       clock=later_clock).ok


def test_expired_reservations_are_still_shown_and_not_deleted(tmp_path: Path):
    """不佔額度 ≠ 消失：過期的預留仍然在真本裡，仍然帶著期限等人清理。"""
    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "kept.json", [])
    cont = _continuation("opencode:kept", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_open_reservations=1).ok

    later_clock = FixedClock("2026-10-05T09:00:00Z")
    fresh = _write_raw(tmp_path, "fresh.json", [])
    fresh_cont = _continuation("opencode:fresh", "opencode:s1", sha, "m1",
                               reserved=fresh)
    assert _apply_cont(store, *fresh_cont, fresh, max_open_reservations=1,
                       clock=later_clock).ok

    kept = store.get_session("opencode:kept")
    assert kept is not None, "期 1 不會自動刪過期的預留"
    assert kept.status == "reserved"
    assert kept.extra["reserved_until"] == "2026-10-04T09:00:00.000000Z"


def test_a_reservation_without_a_readable_deadline_still_consumes_the_cap(
        tmp_path: Path):
    """期限讀不到就當作還沒過期（上限是保護，壞掉就放行等於把它關掉）。"""
    import json

    from aistorage.agora import layout
    from aistorage.agora.apply import _open_reservation_ids

    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")
    reserved = _write_raw(tmp_path, "reserved.json", [])
    cont = _continuation("opencode:s2", "opencode:s1", sha, "m1", reserved=reserved)
    assert _apply_cont(store, *cont, reserved, max_open_reservations=1).ok

    # 繞過 apply_session：把期限欄弄壞，看它是不是還算數
    meta_rel = layout.session_meta_path("opencode:s2")
    meta = json.loads((store.worktree / meta_rel).read_text(encoding="utf-8"))
    meta["reserved_until"] = "不是時間"
    (store.worktree / meta_rel).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    later_clock = FixedClock("2026-11-01T09:00:00Z")
    assert _open_reservation_ids(store, PRODUCER, later_clock.now()) == ["opencode:s2"]
    blocked = _write_raw(tmp_path, "blocked.json", [])
    blocked_cont = _continuation("opencode:s3", "opencode:s1", sha, "m1",
                                 reserved=blocked)
    assert _apply_cont(store, *blocked_cont, blocked, max_open_reservations=1,
                       clock=later_clock).code == "link_quota_exceeded"


def test_quota_rejection_says_which_reservations_hold_the_quota(tmp_path: Path):
    """`link_quota_exceeded` 的訊息要說出是哪些預留佔住額度（review-55edd374 M3）。

    沒有這段的話，寫入端只看到一個代碼，既不知道被什麼擋住、也不知道該去看哪裡。
    """
    from aistorage.publish.rejections import collect_true_copy_rejections

    store = _new_store(tmp_path)
    sha = _put_session(store, tmp_path, "opencode:s1", [_msg("m1")], name="s1.raw")

    holders = []
    for index in range(2):
        sid = f"opencode:hold{index}"
        holders.append(sid)
        reserved = _write_raw(tmp_path, f"hold{index}.json", [])
        cont = _continuation(sid, "opencode:s1", sha, "m1", reserved=reserved)
        assert _apply_cont(store, *cont, reserved, max_open_reservations=2).ok

    reserved = _write_raw(tmp_path, "blocked.json", [])
    cont = _continuation("opencode:blocked", "opencode:s1", sha, "m1",
                         reserved=reserved)
    result = _apply_cont(store, *cont, reserved, max_open_reservations=2)

    assert not result.ok and result.code == "link_quota_exceeded"
    assert result.detail is not None
    for sid in holders:
        assert sid in result.detail, result.detail
    assert "opencode:blocked" not in result.detail, "被拒的那筆不算佔住額度"
    assert "2" in result.detail and "20" not in result.detail

    # 訊息跟著拒收紀錄進真本，也跟著發佈到讀取視圖（寫入端讀得到）
    rows = {r.item_key: r for r in collect_true_copy_rejections(store)}
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row.code == "link_quota_exceeded"
    assert row.detail == result.detail
    for sid in holders:
        assert sid in (row.detail or "")


def test_quota_detail_truncates_a_long_list_of_holders(tmp_path: Path):
    """佔住額度的預留太多時只列前幾個，但要說明還有幾個沒列。"""
    from aistorage.agora.apply import QUOTA_DETAIL_LIMIT, _quota_detail

    ids = [f"opencode:hold{i:02d}" for i in range(QUOTA_DETAIL_LIMIT + 2)]
    detail = _quota_detail(ids, cap=20)
    for sid in ids[:QUOTA_DETAIL_LIMIT]:
        assert sid in detail
    assert f"opencode:hold{QUOTA_DETAIL_LIMIT:02d}" not in detail
    assert "另外 2 筆" in detail
    assert "20" in detail
