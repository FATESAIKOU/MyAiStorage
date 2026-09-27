"""Smoke tests for agora.apply (tasks 3.7-3.10 implementation side).

只寫冒煙：每種型態一次成功＋關鍵拒絕各一。驗收測試由測試方另寫。
"""

import hashlib
import json
from pathlib import Path

from aistorage.agora import FakeRawStorage, AgoraStore
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
from aistorage.schema import generate_ulid


class FakeConverter:
    """測試用轉換器：raw 為 JSON {"messages": [...], "archived_ms":..,
    "last_message_ms":.., "title":..}；message 需含 message_id/completed/reverted。"""

    source = "opencode"

    def _load(self, raw_path: Path) -> dict:
        return json.loads(Path(raw_path).read_bytes().decode("utf-8"))

    def facts(self, raw_path: Path) -> SessionFacts:
        data = self._load(raw_path)
        msgs = data.get("messages", [])
        return SessionFacts(
            title=data.get("title"),
            created_at="2026-09-27T08:00:00Z",
            updated_at="2026-09-27T08:00:00Z",
            message_ids=tuple(m["message_id"] for m in msgs),
            archived_at=None,
            last_message_at=None,
            in_progress=False,
            archived_ms=data.get("archived_ms"),
            last_message_ms=data.get("last_message_ms"),
        )

    def convert(self, raw_path: Path, *, session_id: str, parent_id=None, snapshot_sha256=None) -> dict:
        raw_bytes = Path(raw_path).read_bytes()
        sha = hashlib.sha256(raw_bytes).hexdigest().lower()
        data = self._load(raw_path)
        messages = []
        for i, m in enumerate(data.get("messages", [])):
            messages.append({
                "message_id": m["message_id"],
                "index": i,
                "completed": m.get("completed", True),
                "reverted": m.get("reverted", False),
            })
        return {"snapshot_sha256": sha, "messages": messages}

    def child_session_ids(self, raw_path: Path) -> tuple:
        return ()


def _write_raw(tmp_path: Path, name: str, messages: list[dict], **kw) -> tuple[Path, str, int]:
    p = tmp_path / name
    payload = {"messages": messages, **kw}
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    p.write_bytes(data)
    return p, hashlib.sha256(data).hexdigest().lower(), len(data)


def _msg(mid: str, completed: bool = True, reverted: bool = False) -> dict:
    return {"message_id": mid, "completed": completed, "reverted": reverted}


def _dec(item_key: str, record: dict, sidecar: dict, raw_path: Path | None = None) -> Decision:
    return Decision(
        kind=DecisionKind.ACCEPT,
        item=InboxItem(item_key=item_key, inbox_folder_id="inbox-test"),
        code="ok",
        producer="profile:mac-opencode",
        record_metadata=record,
        sidecar=sidecar,
        raw_path=raw_path,
    )


def _session_parts(session_id: str, snapshot_at: str, sha: str, size: int):
    src, sid = session_id.split(":", 1)
    record = {
        "id": session_id, "type": "session",
        "producer": "profile:mac-opencode",
        "created_at": "2026-09-27T08:00:00Z", "updated_at": snapshot_at,
        "case_id": None, "provenance": None,
    }
    sidecar = {
        "session": {"source": src, "source_session_id": sid,
                    "snapshot_at": snapshot_at, "status": "running", "in_progress": False},
        "raw": {"sha256": sha, "size": size},
        "body": {},
    }
    return record, sidecar


def _new_store(tmp_path: Path) -> AgoraStore:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")


def test_session_apply_ok_and_idempotent(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, "s1.raw", [_msg("m1"), _msg("m2")])
    record, sidecar = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha, size)

    r1 = apply_session(store, _dec(generate_ulid(), record, sidecar, raw_p), conv, clock)
    assert r1.ok and r1.code == "ok"
    assert store.get_session("opencode:s1").status == "running"
    assert len(store.snapshots("opencode:s1")) == 1

    r2 = apply_session(store, _dec(generate_ulid(), record, sidecar, raw_p), conv, clock)
    assert r2.ok and r2.code == "already"
    assert len(store.snapshots("opencode:s1")) == 1


def test_session_stale_rejected_and_published(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_new, sha_new, size_new = _write_raw(tmp_path, "n.raw", [_msg("m1"), _msg("m2")])
    rec_new, sc_new = _session_parts("opencode:s1", "2026-09-27T08:05:00Z", sha_new, size_new)
    assert apply_session(store, _dec(generate_ulid(), rec_new, sc_new, raw_new), conv, clock).ok

    raw_old, sha_old, size_old = _write_raw(tmp_path, "o.raw", [_msg("m1")])
    rec_old, sc_old = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha_old, size_old)
    key_old = generate_ulid()
    r = apply_session(store, _dec(key_old, rec_old, sc_old, raw_old), conv, clock)
    assert not r.ok and r.code == "stale"
    rej = store.worktree / f"_committer/rejections/{key_old}.json"
    assert rej.is_file() and json.loads(rej.read_text(encoding="utf-8"))["code"] == "stale"


def test_session_archived_then_new_message_running(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    # 封存且無新訊息 → stopped
    raw_a, sha_a, size_a = _write_raw(tmp_path, "a.raw", [_msg("m1")], archived_ms=1000, last_message_ms=900)
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha_a, size_a)
    assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_a), conv, clock).ok
    assert store.get_session("opencode:s1").status == "stopped"
    # 封存後又有新訊息 → 回到 running
    raw_b, sha_b, size_b = _write_raw(tmp_path, "b.raw", [_msg("m1"), _msg("m2")], archived_ms=1000, last_message_ms=2000)
    rec2, sc2 = _session_parts("opencode:s1", "2026-09-27T08:10:00Z", sha_b, size_b)
    assert apply_session(store, _dec(generate_ulid(), rec2, sc2, raw_b), conv, clock).ok
    assert store.get_session("opencode:s1").status == "running"


def test_handoff_ok_and_invalid(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, "s1.raw", [_msg("m1"), _msg("m2")])
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha, size)
    assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_p), conv, clock).ok

    h_ulid = generate_ulid()
    h_rec = {"id": f"handoff:{h_ulid}", "type": "handoff",
             "producer": "profile:mac-opencode",
             "created_at": "2026-09-27T08:30:00Z", "updated_at": "2026-09-27T08:30:00Z",
             "case_id": None, "provenance": None}
    h_sc = {"body": {"target_session_id": "opencode:s1",
                     "continuation": {"snapshot_sha256": sha, "message_id": "m2"},
                     "content": "接手"}}
    r = apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv)
    assert r.ok
    saved = store.get_record(f"handoff:{h_ulid}")
    assert saved["claimed_by"] is None

    # 接續點快照不在歷史裡 → 拒收並發佈原因
    bad_key = generate_ulid()
    bad_ulid = generate_ulid()
    bad_rec = {**h_rec, "id": f"handoff:{bad_ulid}"}
    bad_sc = {"body": {"target_session_id": "opencode:s1",
                       "continuation": {"snapshot_sha256": "0" * 64, "message_id": "m2"},
                       "content": "壞"}}
    rb = apply_handoff(store, _dec(bad_key, bad_rec, bad_sc), conv)
    assert not rb.ok and rb.code == "invalid_continuation"
    assert (store.worktree / f"_committer/rejections/{bad_key}.json").is_file()

    # 已撤銷的訊息 → 拒收
    raw_r, sha_r, size_r = _write_raw(tmp_path, "r.raw", [_msg("m1"), _msg("m2", reverted=True)])
    rec_r, sc_r = _session_parts("opencode:s2", "2026-09-27T08:00:00Z", sha_r, size_r)
    assert apply_session(store, _dec(generate_ulid(), rec_r, sc_r, raw_r), conv, clock).ok
    rv_key = generate_ulid()
    rv_rec = {**h_rec, "id": f"handoff:{generate_ulid()}"}
    rv_sc = {"body": {"target_session_id": "opencode:s2",
                      "continuation": {"snapshot_sha256": sha_r, "message_id": "m2"},
                      "content": "撤銷"}}
    rv = apply_handoff(store, _dec(rv_key, rv_rec, rv_sc), conv)
    assert not rv.ok and rv.code == "invalid_continuation"


def test_claim_once_and_converge(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    for sid in ("opencode:s1", "opencode:s2", "opencode:child"):
        raw_p, sha, size = _write_raw(tmp_path, f"{sid.replace(':', '_')}.raw", [_msg("m1")])
        rec, sc = _session_parts(sid, "2026-09-27T08:00:00Z", sha, size)
        assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_p), conv, clock).ok

    def _handoff(target: str, sha_of: str) -> str:
        ulid = generate_ulid()
        h_rec = {"id": f"handoff:{ulid}", "type": "handoff",
                 "producer": "profile:mac-opencode",
                 "created_at": "2026-09-27T08:30:00Z", "updated_at": "2026-09-27T08:30:00Z",
                 "case_id": None, "provenance": None}
        h_sc = {"body": {"target_session_id": target,
                         "continuation": {"snapshot_sha256": sha_of, "message_id": "m1"},
                         "content": "x"}}
        assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv).ok
        return ulid

    sha1 = store.get_session("opencode:s1").raw_sha256
    sha2 = store.get_session("opencode:s2").raw_sha256
    h1, h2 = _handoff("opencode:s1", sha1), _handoff("opencode:s2", sha2)

    def _claim(handoff_ulid: str) -> tuple:
        c_ulid = generate_ulid()
        c_rec = {"id": f"claim:{c_ulid}", "type": "claim",
                 "producer": "profile:mac-opencode",
                 "created_at": "2026-09-27T08:40:00Z", "updated_at": "2026-09-27T08:40:00Z",
                 "case_id": None, "provenance": None}
        c_sc = {"body": {"handoff_id": f"handoff:{handoff_ulid}",
                         "claimer_session_id": "opencode:child"}}
        return apply_claim(store, _dec(generate_ulid(), c_rec, c_sc)), c_ulid

    r1, _ = _claim(h1)
    assert r1.ok
    # 同一 Session 認領第二張（統合）→ ok，兩條 Link
    r2, _ = _claim(h2)
    assert r2.ok
    assert (store.worktree / f"links/continuation/opencode%3Achild/{h1}.json").is_file()
    assert (store.worktree / f"links/continuation/opencode%3Achild/{h2}.json").is_file()
    # 重複認領同一張 → 拒收
    dup_key = generate_ulid()
    c_rec = {"id": f"claim:{generate_ulid()}", "type": "claim",
             "producer": "profile:mac-opencode",
             "created_at": "2026-09-27T08:50:00Z", "updated_at": "2026-09-27T08:50:00Z",
             "case_id": None, "provenance": None}
    c_sc = {"body": {"handoff_id": f"handoff:{h1}", "claimer_session_id": "opencode:child"}}
    rdup = apply_claim(store, _dec(dup_key, c_rec, c_sc))
    assert not rdup.ok and rdup.code == "already_claimed"
    assert (store.worktree / f"_committer/rejections/{dup_key}.json").is_file()


def test_reference_single_link_monotonic(tmp_path: Path):
    store = _new_store(tmp_path)

    def _ref(from_id: str, to_id: str, read_at: str) -> tuple:
        r_ulid = generate_ulid()
        r_rec = {"id": f"reference:{r_ulid}", "type": "reference",
                 "producer": "profile:mac-opencode",
                 "created_at": read_at, "updated_at": read_at,
                 "case_id": None, "provenance": None}
        r_sc = {"body": {"from_session_id": from_id, "to_session_id": to_id,
                         "read_snapshot_at": read_at}}
        return apply_reference(store, _dec(generate_ulid(), r_rec, r_sc)), r_ulid

    assert _ref("opencode:a", "opencode:b", "2026-09-27T08:00:00Z")[0].ok
    # 更舊的時間 → 拒收
    old_key = generate_ulid()
    r_rec = {"id": f"reference:{generate_ulid()}", "type": "reference",
             "producer": "profile:mac-opencode",
             "created_at": "2026-09-27T07:00:00Z", "updated_at": "2026-09-27T07:00:00Z",
             "case_id": None, "provenance": None}
    r_sc = {"body": {"from_session_id": "opencode:a", "to_session_id": "opencode:b",
                     "read_snapshot_at": "2026-09-27T07:00:00Z"}}
    rold = apply_reference(store, _dec(old_key, r_rec, r_sc))
    assert not rold.ok and rold.code == "stale"
    # 更新的時間 → 同一檔案更新，只留一條
    assert _ref("opencode:a", "opencode:b", "2026-09-27T08:10:00.000000Z")[0].ok
    link_p = store.worktree / "links/reference/opencode%3Aa/opencode%3Ab.json"
    assert link_p.is_file()
    assert json.loads(link_p.read_text(encoding="utf-8"))["read_snapshot_at"] == "2026-09-27T08:10:00.000000Z"


def test_rewrite_ok_and_position_changed(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, "s1.raw", [_msg("m1"), _msg("m2")])
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha, size)
    assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_p), conv, clock).ok

    # 改寫：同位置、內容遮蔽（id 序列相同）→ ok，舊快照仍在
    new_p, new_sha, new_size = _write_raw(tmp_path, "s1rw.raw", [_msg("m1"), _msg("m2")], note="redacted")
    rw_ulid = generate_ulid()
    rw_rec = {"id": f"rewrite:{rw_ulid}", "type": "rewrite",
              "producer": "profile:mac-opencode",
              "created_at": "2026-09-27T08:20:00Z", "updated_at": "2026-09-27T08:20:00Z",
              "case_id": None, "provenance": None}
    rw_sc = {"raw": {"sha256": new_sha, "size": new_size},
             "body": {"target_session_id": "opencode:s1",
                      "base_snapshot_sha256": sha, "reason": "遮蔽"}}
    r = apply_rewrite(store, _dec(generate_ulid(), rw_rec, rw_sc, new_p), conv)
    assert r.ok
    snaps = [s.snapshot_sha256 for s in store.snapshots("opencode:s1")]
    assert sha in snaps and new_sha in snaps  # 舊快照保留，接續點不受影響

    # 改寫改變位置（刪掉 m1）→ 拒收
    bad_p, bad_sha, bad_size = _write_raw(tmp_path, "s1bad.raw", [_msg("m2")])
    bad_key = generate_ulid()
    bad_rec = {"id": f"rewrite:{generate_ulid()}", "type": "rewrite",
               "producer": "profile:mac-opencode",
               "created_at": "2026-09-27T08:30:00Z", "updated_at": "2026-09-27T08:30:00Z",
               "case_id": None, "provenance": None}
    bad_sc = {"raw": {"sha256": bad_sha, "size": bad_size},
              "body": {"target_session_id": "opencode:s1",
                       "base_snapshot_sha256": new_sha, "reason": "刪"}}
    rbad = apply_rewrite(store, _dec(bad_key, bad_rec, bad_sc, bad_p), conv)
    assert not rbad.ok and rbad.code == "position_changed"

    # base 不是最新 → 拒收
    stale_key = generate_ulid()
    stale_rec = {"id": f"rewrite:{generate_ulid()}", "type": "rewrite",
                 "producer": "profile:mac-opencode",
                 "created_at": "2026-09-27T08:40:00Z", "updated_at": "2026-09-27T08:40:00Z",
                 "case_id": None, "provenance": None}
    stale_sc = {"raw": {"sha256": bad_sha, "size": bad_size},
                "body": {"target_session_id": "opencode:s1",
                         "base_snapshot_sha256": sha, "reason": "舊 base"}}
    rstale = apply_rewrite(store, _dec(stale_key, stale_rec, stale_sc, bad_p), conv)
    assert not rstale.ok and rstale.code == "stale"
