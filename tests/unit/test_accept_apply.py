"""Independent Acceptance Tests for Agora Apply (Task 3.7 - 3.10).

Adheres strictly to:
- Design D10
- docs/impl/group3-modules.md §6.2
- Holder checks (rec.producer == target/claimer/from producer)
- Can only claim once (already_claimed) & Consolidation (same session can claim multiple handoffs)
- Reference Link monotonicity (read_snapshot_at strictly increases, regressed timestamp -> stale)
- Rewrite always rejected (rewrite_not_supported)
- Session status resumption (archived -> stopped; new message after archive -> running)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import pytest

from aistorage.agora import AgoraStore, FakeRawStorage
from aistorage.agora.apply import (
    apply_claim,
    apply_handoff,
    apply_reference,
    apply_rewrite,
    apply_session,
)
from aistorage.clock import FixedClock
from aistorage.converters.base import SessionFacts
from aistorage.intake import Decision, DecisionKind, InboxItem


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

def _make_inbox_item(item_key: str) -> InboxItem:
    return InboxItem(
        item_key=item_key,
        inbox_folder_id="test_inbox",
        sidecar=None,
        sig=None,
        raw=None,
        extras=(),
        sidecars=(),
        sigs=(),
        raws=(),
    )


def _make_session_meta(
    session_id: str,
    producer: str,
    *,
    parent_id: str | None = None,
    status: str = "running",
    snapshot_at: str = "2026-09-27T08:00:00.000Z",
) -> dict:
    return {
        "id": session_id,
        "type": "session",
        "producer": producer,
        "created_at": "2026-09-27T08:00:00.000Z",
        "updated_at": "2026-09-27T08:00:00.000Z",
        "status": status,
        "snapshot_at": snapshot_at,
        "raw_sha256": "0" * 64,
        "raw_size": 100,
        "committed_at": "2026-09-27T08:00:00.000Z",
        "last_item_key": "01ARZ3NDEKTSV4RRFFQ69G5F00",
        "parent_id": parent_id,
        "in_progress": False,
    }


class _MockConverter:
    source = "opencode"

    def __init__(self, facts: SessionFacts | None = None) -> None:
        self._facts = facts

    def facts(self, p: Path) -> SessionFacts:
        if self._facts is None:
            return SessionFacts(
                title="Mock Session",
                created_at=None,
                updated_at=None,
                message_ids=(),
                archived_at=None,
                last_message_at=None,
                in_progress=False,
                archived_ms=None,
                last_message_ms=None,
            )
        return self._facts

    def convert(self, p: Path, **kw) -> dict:
        return {"messages": []}


# ---------------------------------------------------------------------------
# Acceptance Tests
# ---------------------------------------------------------------------------

def test_apply_rewrite_always_rejected(tmp_path: Path):
    """驗證改寫提案一律拒收 (docs/impl/group3-modules.md §6.2: rewrite_not_supported)。"""
    store = AgoraStore(tmp_path, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T10:00:00.000Z")

    item_key = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
    item = _make_inbox_item(item_key)
    dec = Decision(
        kind=DecisionKind.ACCEPT,
        item=item,
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"type": "rewrite", "id": f"rewrite:{item_key}"},
        sidecar={},
        raw_path=None,
    )

    res = apply_rewrite(store, dec, _MockConverter(), clock)
    assert res.ok is False
    assert res.code == "rewrite_not_supported"
    assert f"_committer/rejections/{item_key}.json" in res.paths


def test_apply_handoff_holder_check_and_target_existence(tmp_path: Path):
    """驗證交接單持有者檢查與目標 Session 存在性 (docs/impl/group3-modules.md §6.2)。"""
    store = AgoraStore(tmp_path, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T10:00:00.000Z")

    # 1. 目標 Session 存在，持有者為 alice
    store.put_json("sessions/opencode/target_ses/meta.json", _make_session_meta("opencode:target_ses", "alice"))

    # 2. 非持有者 (bob != alice) 發起交接單 -> not_holder
    item_bob = _make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FH1")
    dec_bob = Decision(
        kind=DecisionKind.ACCEPT,
        item=item_bob,
        code="ACCEPTED",
        authenticated=True,
        producer="bob",
        record_metadata={"id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FH1", "producer": "bob"},
        sidecar={
            "body": {
                "target_session_id": "opencode:target_ses",
                "continuation": {"snapshot_sha256": "1" * 64, "message_id": "m1"},
            }
        },
        raw_path=None,
    )
    res_bob = apply_handoff(store, dec_bob, _MockConverter(), clock)
    assert res_bob.ok is False
    assert res_bob.code == "not_holder"

    # 3. 目標 Session 不存在 -> unknown_target
    item_unknown = _make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FH2")
    dec_unknown = Decision(
        kind=DecisionKind.ACCEPT,
        item=item_unknown,
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FH2", "producer": "alice"},
        sidecar={
            "body": {
                "target_session_id": "opencode:nonexistent_ses",
                "continuation": {"snapshot_sha256": "1" * 64, "message_id": "m1"},
            }
        },
        raw_path=None,
    )
    res_unknown = apply_handoff(store, dec_unknown, _MockConverter(), clock)
    assert res_unknown.ok is False
    assert res_unknown.code == "unknown_target"


def test_apply_claim_single_claim_and_consolidation(tmp_path: Path):
    """驗證交接單只能認領一次與認領統合 (docs/impl/group3-modules.md §6.2)。"""
    store = AgoraStore(tmp_path, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T10:00:00.000Z")

    # 建立兩張交接單
    h_ulid1 = "01ARZ3NDEKTSV4RRFFQ69G5FH1"
    h_ulid2 = "01ARZ3NDEKTSV4RRFFQ69G5FH2"
    store.put_json(
        f"handoffs/{h_ulid1}.json",
        {
            "id": f"handoff:{h_ulid1}",
            "producer": "alice",
            "body": {
                "target_session_id": "opencode:target_1",
                "continuation": {"snapshot_sha256": "1" * 64, "message_id": "m1"},
            },
            "claimed_by": None,
        },
    )
    store.put_json(
        f"handoffs/{h_ulid2}.json",
        {
            "id": f"handoff:{h_ulid2}",
            "producer": "alice",
            "body": {
                "target_session_id": "opencode:target_2",
                "continuation": {"snapshot_sha256": "2" * 64, "message_id": "m2"},
            },
            "claimed_by": None,
        },
    )

    # 建立主認領者 Session (producer: bob)
    store.put_json("sessions/opencode/claimer_main/meta.json", _make_session_meta("opencode:claimer_main", "bob"))

    # 1. 第一次認領交接單 1 -> 成功
    c_ulid1 = "01ARZ3NDEKTSV4RRFFQ69G5FC1"
    dec_c1 = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item(c_ulid1),
        code="ACCEPTED",
        authenticated=True,
        producer="bob",
        record_metadata={"id": f"claim:{c_ulid1}", "producer": "bob"},
        sidecar={"body": {"handoff_id": f"handoff:{h_ulid1}", "claimer_session_id": "opencode:claimer_main"}},
    )
    res_c1 = apply_claim(store, dec_c1, clock)
    assert res_c1.ok is True
    assert res_c1.code == "ok"

    # 2. 再次認領交接單 1 -> already_claimed 拒收
    c_ulid1_again = "01ARZ3NDEKTSV4RRFFQ69G5FC2"
    dec_c1_again = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item(c_ulid1_again),
        code="ACCEPTED",
        authenticated=True,
        producer="bob",
        record_metadata={"id": f"claim:{c_ulid1_again}", "producer": "bob"},
        sidecar={"body": {"handoff_id": f"handoff:{h_ulid1}", "claimer_session_id": "opencode:claimer_main"}},
    )
    res_c1_again = apply_claim(store, dec_c1_again, clock)
    assert res_c1_again.ok is False
    assert res_c1_again.code == "already_claimed"

    # 3. 統合: 同一主 Session 認領另一張交接單 2 -> 成功
    c_ulid2 = "01ARZ3NDEKTSV4RRFFQ69G5FC3"
    dec_c2 = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item(c_ulid2),
        code="ACCEPTED",
        authenticated=True,
        producer="bob",
        record_metadata={"id": f"claim:{c_ulid2}", "producer": "bob"},
        sidecar={"body": {"handoff_id": f"handoff:{h_ulid2}", "claimer_session_id": "opencode:claimer_main"}},
    )
    res_c2 = apply_claim(store, dec_c2, clock)
    assert res_c2.ok is True
    assert res_c2.code == "ok"


def test_apply_claim_session_constraints(tmp_path: Path):
    """驗證認領限制: 子 Session 認領、自我認領、認領者持有者不符 (docs/impl/group3-modules.md §6.2)。"""
    store = AgoraStore(tmp_path, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T10:00:00.000Z")

    h_ulid = "01ARZ3NDEKTSV4RRFFQ69G5FH0"
    store.put_json(
        f"handoffs/{h_ulid}.json",
        {
            "id": f"handoff:{h_ulid}",
            "producer": "alice",
            "body": {
                "target_session_id": "opencode:target_main",
                "continuation": {"snapshot_sha256": "1" * 64, "message_id": "m1"},
            },
            "claimed_by": None,
        },
    )

    # 主 Session 與子 Session
    store.put_json("sessions/opencode/target_main/meta.json", _make_session_meta("opencode:target_main", "alice"))
    store.put_json("sessions/opencode/bob_main/meta.json", _make_session_meta("opencode:bob_main", "bob"))
    store.put_json("sessions/opencode/bob_sub/meta.json", _make_session_meta("opencode:bob_sub", "bob", parent_id="opencode:bob_main"))

    # 1. 子 Session 認領 -> claim_from_subsession
    dec_sub = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FC1"),
        code="ACCEPTED",
        authenticated=True,
        producer="bob",
        record_metadata={"id": "claim:01ARZ3NDEKTSV4RRFFQ69G5FC1", "producer": "bob"},
        sidecar={"body": {"handoff_id": f"handoff:{h_ulid}", "claimer_session_id": "opencode:bob_sub"}},
    )
    res_sub = apply_claim(store, dec_sub, clock)
    assert res_sub.ok is False
    assert res_sub.code == "claim_from_subsession"

    # 2. 自我認領 (認領者即目標 Session) -> self_claim
    dec_self = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FC2"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"id": "claim:01ARZ3NDEKTSV4RRFFQ69G5FC2", "producer": "alice"},
        sidecar={"body": {"handoff_id": f"handoff:{h_ulid}", "claimer_session_id": "opencode:target_main"}},
    )
    res_self = apply_claim(store, dec_self, clock)
    assert res_self.ok is False
    assert res_self.code == "self_claim"

    # 3. 認領者持有者不符 (producer=charlie, but session producer=bob) -> not_holder
    dec_bad_holder = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FC3"),
        code="ACCEPTED",
        authenticated=True,
        producer="charlie",
        record_metadata={"id": "claim:01ARZ3NDEKTSV4RRFFQ69G5FC3", "producer": "charlie"},
        sidecar={"body": {"handoff_id": f"handoff:{h_ulid}", "claimer_session_id": "opencode:bob_main"}},
    )
    res_bad_holder = apply_claim(store, dec_bad_holder, clock)
    assert res_bad_holder.ok is False
    assert res_bad_holder.code == "not_holder"


def test_apply_reference_holder_and_monotonicity(tmp_path: Path):
    """驗證參考 Link 持有者檢查與時間單調性 (docs/impl/group3-modules.md §6.2)。"""
    store = AgoraStore(tmp_path, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T10:00:00.000Z")

    store.put_json("sessions/opencode/s_from/meta.json", _make_session_meta("opencode:s_from", "alice"))
    store.put_json("sessions/opencode/s_to/meta.json", _make_session_meta("opencode:s_to", "bob"))

    # 1. 持有者不符 -> not_holder
    dec_not_holder = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FR1"),
        code="ACCEPTED",
        authenticated=True,
        producer="charlie",
        record_metadata={"id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FR1", "producer": "charlie"},
        sidecar={"body": {"from_session_id": "opencode:s_from", "to_session_id": "opencode:s_to", "read_snapshot_at": "2026-09-27T09:00:00.000Z"}},
    )
    res_nh = apply_reference(store, dec_not_holder, clock)
    assert res_nh.ok is False
    assert res_nh.code == "not_holder"

    # 2. 目標不存在 -> unknown_target
    dec_ut = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FR2"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FR2", "producer": "alice"},
        sidecar={"body": {"from_session_id": "opencode:s_from", "to_session_id": "opencode:unknown_to", "read_snapshot_at": "2026-09-27T09:00:00.000Z"}},
    )
    res_ut = apply_reference(store, dec_ut, clock)
    assert res_ut.ok is False
    assert res_ut.code == "unknown_target"

    # 3. 第一次參考建立於 10:00 -> 成功
    dec_ref1 = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FR3"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FR3", "producer": "alice"},
        sidecar={"body": {"from_session_id": "opencode:s_from", "to_session_id": "opencode:s_to", "read_snapshot_at": "2026-09-27T10:00:00.000Z"}},
    )
    res_r1 = apply_reference(store, dec_ref1, clock)
    assert res_r1.ok is True

    # 4. 第二次參考推進到 11:00 (單調遞增) -> 成功
    dec_ref2 = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FR4"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FR4", "producer": "alice"},
        sidecar={"body": {"from_session_id": "opencode:s_from", "to_session_id": "opencode:s_to", "read_snapshot_at": "2026-09-27T11:00:00.000Z"}},
    )
    res_r2 = apply_reference(store, dec_ref2, clock)
    assert res_r2.ok is True

    # 5. 第三次參考時間倒退至 09:30 (< 11:00) -> stale 拒收
    dec_stale = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FR5"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={"id": "reference:01ARZ3NDEKTSV4RRFFQ69G5FR5", "producer": "alice"},
        sidecar={"body": {"from_session_id": "opencode:s_from", "to_session_id": "opencode:s_to", "read_snapshot_at": "2026-09-27T09:30:00.000Z"}},
    )
    res_stale = apply_reference(store, dec_stale, clock)
    assert res_stale.ok is False
    assert res_stale.code == "stale"


def test_apply_session_archived_and_resumed_to_running(tmp_path: Path):
    """驗證封存狀態判斷與後續新訊息喚醒 (docs/impl/group3-modules.md §6.2 3.9 節)。"""
    store = AgoraStore(tmp_path, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    clock = FixedClock("2026-09-27T10:00:00.000Z")

    # 快照 1: 已封存，且最後訊息不晚於封存時間 -> stopped
    raw1 = tmp_path / "raw1"
    raw1.write_text("session raw v1")
    sha1 = hashlib.sha256(b"session raw v1").hexdigest()

    dec1 = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FS1"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={
            "id": "opencode:session_resume",
            "producer": "alice",
            "created_at": "2026-09-27T08:00:00.000Z",
            "updated_at": "2026-09-27T09:00:00.000Z",
        },
        sidecar={
            "session": {"snapshot_at": "2026-09-27T09:00:00.000Z"},
            "raw": {"sha256": sha1, "size": len(b"session raw v1")},
        },
        raw_path=raw1,
    )
    facts1 = SessionFacts(
        title="Resume Test",
        created_at="2026-09-27T08:00:00.000Z",
        updated_at="2026-09-27T09:00:00.000Z",
        message_ids=("m1",),
        archived_at="2026-09-27T09:00:00.000Z",
        last_message_at="2026-09-27T08:55:00.000Z",
        in_progress=False,
        archived_ms=1000,
        last_message_ms=900,  # last_message_ms <= archived_ms -> stopped
    )
    res1 = apply_session(store, dec1, _MockConverter(facts1), clock)
    assert res1.ok is True
    s1 = store.get_session("opencode:session_resume")
    assert s1 is not None
    assert s1.status == "stopped"

    # 快照 2: 封存之後有新訊息抵達 -> 回到 running
    raw2 = tmp_path / "raw2"
    raw2.write_text("session raw v2 with new user prompt")
    sha2 = hashlib.sha256(b"session raw v2 with new user prompt").hexdigest()

    dec2 = Decision(
        kind=DecisionKind.ACCEPT,
        item=_make_inbox_item("01ARZ3NDEKTSV4RRFFQ69G5FS2"),
        code="ACCEPTED",
        authenticated=True,
        producer="alice",
        record_metadata={
            "id": "opencode:session_resume",
            "producer": "alice",
            "created_at": "2026-09-27T08:00:00.000Z",
            "updated_at": "2026-09-27T10:00:00.000Z",
        },
        sidecar={
            "session": {"snapshot_at": "2026-09-27T10:00:00.000Z"},
            "raw": {"sha256": sha2, "size": len(b"session raw v2 with new user prompt")},
        },
        raw_path=raw2,
    )
    facts2 = SessionFacts(
        title="Resume Test",
        created_at="2026-09-27T08:00:00.000Z",
        updated_at="2026-09-27T10:00:00.000Z",
        message_ids=("m1", "m2"),
        archived_at="2026-09-27T09:00:00.000Z",
        last_message_at="2026-09-27T09:50:00.000Z",
        in_progress=False,
        archived_ms=1000,
        last_message_ms=1200,  # last_message_ms > archived_ms -> resumed to running
    )
    res2 = apply_session(store, dec2, _MockConverter(facts2), clock)
    assert res2.ok is True
    s2 = store.get_session("opencode:session_resume")
    assert s2 is not None
    assert s2.status == "running"
    assert s2.stopped_at is None
