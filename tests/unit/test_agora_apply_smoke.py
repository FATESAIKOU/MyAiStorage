"""Smoke tests for agora.apply (tasks 3.7-3.10 implementation side).

實作方維護：基本路徑＋review-g3e 要求的案例（H1 兩階段無殘留、H2 照收、
H3 持有者、M5 最後一則、M6 不存內文、stopped_at、真實轉換器、9.x 情境）。
驗收測試由測試方另寫。
"""

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Sequence

from aistorage.agora import FakeRawStorage, AgoraStore, GitRawStorage
from aistorage.agora.apply import (
    ApplyResult,
    apply_claim,
    apply_handoff,
    apply_reference,
    apply_rewrite,
    apply_session,
)
from aistorage.annex.git import SubprocessAnnexGit
from aistorage.clock import FixedClock
from aistorage.converters.base import ConversionError, SessionFacts
from aistorage.converters.opencode import OpencodeConverter
from aistorage.errors import MismatchError
from aistorage.intake import Decision, DecisionKind, InboxItem, sort_accepted_decisions
from aistorage.schema import generate_ulid
import pytest

PRODUCER = "profile:mac-opencode"
OTHER = "profile:evil"


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


class FactsBoomConverter(FakeConverter):
    def facts(self, raw_path: Path) -> SessionFacts:
        raise ConversionError("facts 炸了")


class ConvertBoomConverter(FakeConverter):
    def convert(self, raw_path: Path, *, session_id: str, parent_id=None, snapshot_sha256=None) -> dict:
        raise ConversionError("convert 炸了：含秘密的內文不該進 meta")


def _write_raw(tmp_path: Path, name: str, messages: list[dict], **kw) -> tuple[Path, str, int]:
    p = tmp_path / name
    payload = {"messages": messages, **kw}
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    p.write_bytes(data)
    return p, hashlib.sha256(data).hexdigest().lower(), len(data)


def _msg(mid: str, completed: bool = True, reverted: bool = False) -> dict:
    return {"message_id": mid, "completed": completed, "reverted": reverted}


def _write_opencode_export(tmp_path: Path, name: str, session_id: str,
                           title: str, texts: Sequence[str]) -> tuple[Path, str, int]:
    """寫一份**形狀真實**的 opencode 匯出檔（`agora checkout` 的預留就是這種）。"""
    native = session_id.split(":", 1)[1]
    messages = []
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "assistant"
        time = {"created": 1790420000000 + i * 1000}
        if role == "assistant":
            time["completed"] = time["created"] + 500
        messages.append({
            "info": {"id": f"msg_{native}_{i}", "sessionID": native,
                     "role": role, "time": time},
            "parts": [{"id": f"prt_{native}_{i}", "sessionID": native,
                       "messageID": f"msg_{native}_{i}", "type": "text",
                       "text": text}],
        })
    payload = {"info": {"id": native, "title": title,
                        "time": {"created": 1790420000000,
                                 "updated": 1790420001000}},
               "messages": messages}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path = tmp_path / name
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest().lower(), len(data)


def _dec(item_key: str, record: dict, sidecar: dict, raw_path: Path | None = None,
         producer: str = PRODUCER) -> Decision:
    record = {**record, "producer": producer}
    return Decision(
        kind=DecisionKind.ACCEPT,
        item=InboxItem(item_key=item_key, inbox_folder_id="inbox-test"),
        code="ok",
        producer=producer,
        record_metadata=record,
        sidecar=sidecar,
        raw_path=raw_path,
    )


def _session_parts(session_id: str, snapshot_at: str, sha: str, size: int,
                   producer: str = PRODUCER, parent_id: str | None = None,
                   in_progress: bool = False):
    src, sid = session_id.split(":", 1)
    record = {
        "id": session_id, "type": "session",
        "producer": producer,
        "created_at": "2026-09-27T08:00:00Z", "updated_at": snapshot_at,
        "case_id": None, "provenance": None,
    }
    sess = {"source": src, "source_session_id": sid,
            "snapshot_at": snapshot_at, "status": "running", "in_progress": in_progress}
    if parent_id is not None:
        sess["parent_id"] = parent_id
    sidecar = {
        "session": sess,
        "raw": {"sha256": sha, "size": size},
        "body": {},
    }
    return record, sidecar


def _handoff_parts(handoff_ulid: str, target: str, snap_sha: str, mid: str,
                   producer: str = PRODUCER) -> tuple[dict, dict]:
    record = {"id": f"handoff:{handoff_ulid}", "type": "handoff",
              "producer": producer,
              "created_at": "2026-09-27T08:30:00Z", "updated_at": "2026-09-27T08:30:00Z",
              "case_id": None, "provenance": None}
    sidecar = {"body": {"target_session_id": target,
                        "continuation": {"snapshot_sha256": snap_sha, "message_id": mid},
                        "content": "接手"}}
    return record, sidecar


def _claim_parts(claim_ulid: str, handoff_ulid: str, claimer: str,
                 producer: str = PRODUCER) -> tuple[dict, dict]:
    record = {"id": f"claim:{claim_ulid}", "type": "claim",
              "producer": producer,
              "created_at": "2026-09-27T08:40:00Z", "updated_at": "2026-09-27T08:40:00Z",
              "case_id": None, "provenance": None}
    sidecar = {"body": {"handoff_id": f"handoff:{handoff_ulid}",
                        "claimer_session_id": claimer}}
    return record, sidecar


def _ref_parts(ref_ulid: str, from_id: str, to_id: str, read_at: str,
               producer: str = PRODUCER) -> tuple[dict, dict]:
    record = {"id": f"reference:{ref_ulid}", "type": "reference",
              "producer": producer,
              "created_at": read_at, "updated_at": read_at,
              "case_id": None, "provenance": None}
    sidecar = {"body": {"from_session_id": from_id, "to_session_id": to_id,
                        "read_snapshot_at": read_at}}
    return record, sidecar


def _new_store(tmp_path: Path) -> AgoraStore:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")


def _apply_session_ok(store: AgoraStore, tmp_path: Path, session_id: str,
                      messages: list[dict], snapshot_at: str = "2026-09-27T08:00:00Z",
                      producer: str = PRODUCER, name: str = "s.raw",
                      parent_id: str | None = None, clock=None, conv=None) -> str:
    """建一個 session 快照並斷言成功，回傳 raw sha。"""
    conv = conv or FakeConverter()
    clock = clock or FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, name, messages)
    rec, sc = _session_parts(session_id, snapshot_at, sha, size,
                             producer=producer, parent_id=parent_id)
    r = apply_session(store, _dec(generate_ulid(), rec, sc, raw_p, producer), conv, clock)
    assert r.ok, r.code
    return sha


def _only_rejection(store: AgoraStore, result: ApplyResult, item_key: str,
                    before: list[str]):
    """H1：失敗時除了拒收檔之外沒有新增任何路徑（before 為本次 apply 前的快照）。"""
    assert not result.ok
    added = [p for p in store.changed_paths() if p not in set(before)]
    assert added == [f"_committer/rejections/{item_key}.json"]


# ---------------------------------------------------------------------------
# session 基本路徑
# ---------------------------------------------------------------------------

def test_session_apply_ok_and_idempotent(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, "s1.raw", [_msg("m1"), _msg("m2")])
    record, sidecar = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha, size)

    r1 = apply_session(store, _dec(generate_ulid(), record, sidecar, raw_p), conv, clock)
    assert r1.ok and r1.code == "ok"
    assert r1.rejected_at is None
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
    before = list(store.changed_paths())
    r = apply_session(store, _dec(key_old, rec_old, sc_old, raw_old), conv, clock)
    _only_rejection(store, r, key_old, before)
    assert r.code == "stale"
    assert r.rejected_at is not None and r.deletable_after is not None
    rej = store.worktree / f"_committer/rejections/{key_old}.json"
    assert json.loads(rej.read_text(encoding="utf-8"))["code"] == "stale"


def test_session_archived_then_new_message_running(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_a, sha_a, size_a = _write_raw(tmp_path, "a.raw", [_msg("m1")], archived_ms=1000, last_message_ms=900)
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha_a, size_a)
    assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_a), conv, clock).ok
    assert store.get_session("opencode:s1").status == "stopped"
    raw_b, sha_b, size_b = _write_raw(tmp_path, "b.raw", [_msg("m1"), _msg("m2")], archived_ms=1000, last_message_ms=2000)
    rec2, sc2 = _session_parts("opencode:s1", "2026-09-27T08:10:00Z", sha_b, size_b)
    assert apply_session(store, _dec(generate_ulid(), rec2, sc2, raw_b), conv, clock).ok
    assert store.get_session("opencode:s1").status == "running"


def test_session_stopped_at_kept_and_clock_fallback(tmp_path: Path):
    """L：已停止 Session 再收封存快照 → 沿用原本 stopped_at；
    sidecar 無 stopped_at → 用提交時鐘，不用來源端的 archived_at。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_a, sha_a, size_a = _write_raw(tmp_path, "a.raw", [_msg("m1")],
                                      archived_ms=1000, last_message_ms=900)
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha_a, size_a)
    sc["session"]["stopped_at"] = "2026-09-27T08:01:00Z"
    assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_a), conv, clock).ok
    assert store.get_session("opencode:s1").stopped_at == "2026-09-27T08:01:00Z"

    raw_b, sha_b, size_b = _write_raw(tmp_path, "b.raw", [_msg("m1"), _msg("m2")],
                                      archived_ms=1000, last_message_ms=900)
    rec2, sc2 = _session_parts("opencode:s1", "2026-09-27T08:10:00Z", sha_b, size_b)
    sc2["session"]["stopped_at"] = "2026-09-27T08:11:00Z"
    assert apply_session(store, _dec(generate_ulid(), rec2, sc2, raw_b), conv, clock).ok
    assert store.get_session("opencode:s1").stopped_at == "2026-09-27T08:01:00Z"

    raw_c, sha_c, size_c = _write_raw(tmp_path, "c.raw", [_msg("m1")],
                                      archived_ms=1000, last_message_ms=900)
    rec3, sc3 = _session_parts("opencode:s2", "2026-09-27T08:00:00Z", sha_c, size_c)
    assert "stopped_at" not in sc3["session"]
    assert apply_session(store, _dec(generate_ulid(), rec3, sc3, raw_c), conv, clock).ok
    got = store.get_session("opencode:s2")
    assert got.status == "stopped"
    assert got.stopped_at == "2026-09-27T09:00:00.000000Z"


def test_session_facts_failure_still_ingested(tmp_path: Path):
    """H2：facts 失敗也照收 raw，reading 標失敗，狀態沿用預設。"""
    store = _new_store(tmp_path)
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, "s.raw", [_msg("m1")])
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha, size,
                             in_progress=True)
    r = apply_session(store, _dec(generate_ulid(), rec, sc, raw_p),
                      FactsBoomConverter(), clock)
    assert r.ok, r.code
    meta = store.get_record("opencode:s1")
    assert meta["reading_status"] == "failed"
    assert meta["reading_error_code"] == "facts_error"
    assert meta["reading_error_message"] is None
    assert meta["status"] == "running"
    assert meta["in_progress"] is True
    assert len(store.snapshots("opencode:s1")) == 1


def test_session_convert_failure_records_code_only(tmp_path: Path):
    """M6：轉換失敗只存代碼，不存可能含內文的訊息。"""
    store = _new_store(tmp_path)
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha, size = _write_raw(tmp_path, "s.raw", [_msg("m1")])
    rec, sc = _session_parts("opencode:s1", "2026-09-27T08:00:00Z", sha, size)
    r = apply_session(store, _dec(generate_ulid(), rec, sc, raw_p),
                      ConvertBoomConverter(), clock)
    assert r.ok, r.code
    meta = store.get_record("opencode:s1")
    assert meta["reading_status"] == "failed"
    assert meta["reading_error_code"] == "conversion_error"
    assert meta["reading_error_message"] is None


def test_session_stale_writes_no_commit(tmp_path: Path):
    """H1：有 git 時，拒收不產生新 commit。"""
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-b", "main", "-q", str(worktree)], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "t@t"], check=True)
    (worktree / "init.txt").write_text("init")
    subprocess.run(["git", "-C", str(worktree), "add", "init.txt"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "init"], check=True)
    from aistorage.agora.store import GitRawStorage
    git = SubprocessAnnexGit(worktree)
    store = AgoraStore(worktree, GitRawStorage(worktree), git=git,
                       temp_dir=tmp_path / "tmp")
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")

    def _count() -> int:
        return int(subprocess.run(
            ["git", "-C", str(worktree), "rev-list", "--count", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip())

    _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                      clock=clock, conv=conv, name="n.raw",
                      snapshot_at="2026-09-27T08:05:00Z")
    assert _count() == 2
    raw_old, sha_old, size_old = _write_raw(tmp_path, "o.raw", [_msg("m1")])
    rec_old, sc_old = _session_parts("opencode:s1", "2026-09-27T08:00:00Z",
                                     sha_old, size_old)
    key = generate_ulid()
    r = apply_session(store, _dec(key, rec_old, sc_old, raw_old), conv, clock)
    assert not r.ok and r.code == "stale"
    assert _count() == 2
    assert store.changed_paths() == [f"_committer/rejections/{key}.json"]


# ---------------------------------------------------------------------------
# handoff
# ---------------------------------------------------------------------------

def test_handoff_ok_and_invalid(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                            clock=clock, conv=conv)

    h_ulid = generate_ulid()
    h_rec, h_sc = _handoff_parts(h_ulid, "opencode:s1", sha, "m2")
    r = apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock)
    assert r.ok
    saved = store.get_record(f"handoff:{h_ulid}")
    assert saved["claimed_by"] is None

    bad_key = generate_ulid()
    bad_rec, bad_sc = _handoff_parts(generate_ulid(), "opencode:s1", "0" * 64, "m2")
    rb = apply_handoff(store, _dec(bad_key, bad_rec, bad_sc), conv, clock)
    assert not rb.ok and rb.code == "invalid_continuation"
    assert (store.worktree / f"_committer/rejections/{bad_key}.json").is_file()

    sha2 = _apply_session_ok(store, tmp_path, "opencode:s2",
                             [_msg("m1"), _msg("m2", reverted=True)],
                             clock=clock, conv=conv, name="r.raw")
    rv_key = generate_ulid()
    rv_rec, rv_sc = _handoff_parts(generate_ulid(), "opencode:s2", sha2, "m2")
    rv = apply_handoff(store, _dec(rv_key, rv_rec, rv_sc), conv, clock)
    assert not rv.ok and rv.code == "invalid_continuation"


def test_handoff_must_point_to_last_completed(tmp_path: Path):
    """M5：接續點指到較早的已完成訊息（後面還有已完成的）→ 拒收，且無殘留。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                            clock=clock, conv=conv)
    key = generate_ulid()
    h_rec, h_sc = _handoff_parts(generate_ulid(), "opencode:s1", sha, "m1")
    before = list(store.changed_paths())
    r = apply_handoff(store, _dec(key, h_rec, h_sc), conv, clock)
    assert not r.ok and r.code == "invalid_continuation"
    _only_rejection(store, r, key, before)


def test_handoff_unknown_target(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    key = generate_ulid()
    h_rec, h_sc = _handoff_parts(generate_ulid(), "opencode:ghost", "0" * 64, "m1")
    before = list(store.changed_paths())
    r = apply_handoff(store, _dec(key, h_rec, h_sc), conv, clock)
    assert not r.ok and r.code == "unknown_target"
    _only_rejection(store, r, key, before)


def test_handoff_not_holder(tmp_path: Path):
    """H3：別的 profile 替 Mac 的 Session 寫交接單 → 拒收。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                            clock=clock, conv=conv, producer=PRODUCER)
    key = generate_ulid()
    h_rec, h_sc = _handoff_parts(generate_ulid(), "opencode:s1", sha, "m1",
                                 producer=OTHER)
    before = list(store.changed_paths())
    r = apply_handoff(store, _dec(key, h_rec, h_sc, producer=OTHER), conv, clock)
    assert not r.ok and r.code == "not_holder"
    _only_rejection(store, r, key, before)


def test_handoff_uses_target_source_converter(tmp_path: Path):
    """L：目標 source 與傳入的轉換器不同時，依目標 source 取轉換器。"""
    store = _new_store(tmp_path)
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                            clock=clock, conv=FakeConverter())

    class OtherSourceConverter(FakeConverter):
        source = "test"

    key = generate_ulid()
    h_rec, h_sc = _handoff_parts(generate_ulid(), "opencode:s1", sha, "m1")
    # 傳入的轉換器是 test 來源，但目標是 opencode → 會用真正的 OpencodeConverter，
    # 它讀不懂測試夾具 → invalid_continuation（證明沒有用錯轉換器）。
    r = apply_handoff(store, _dec(key, h_rec, h_sc), OtherSourceConverter(), clock)
    assert not r.ok and r.code == "invalid_continuation"


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

def _setup_claim(store: AgoraStore, tmp_path: Path, clock, conv,
                 target: str = "opencode:s1", claimer: str = "opencode:child",
                 target_producer: str = PRODUCER, claimer_producer: str = PRODUCER,
                 claimer_parent: str | None = None) -> tuple[str, str]:
    sha_t = _apply_session_ok(store, tmp_path, target, [_msg("m1")],
                              clock=clock, conv=conv, name=f"{target.replace(':', '_')}.raw",
                              producer=target_producer)
    _apply_session_ok(store, tmp_path, claimer, [_msg("m1")],
                      clock=clock, conv=conv, name=f"{claimer.replace(':', '_')}.raw",
                      producer=claimer_producer, parent_id=claimer_parent)
    h_ulid = generate_ulid()
    h_rec, h_sc = _handoff_parts(h_ulid, target, sha_t, "m1", producer=target_producer)
    r = apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc, producer=target_producer),
                      conv, clock)
    assert r.ok, r.code
    return h_ulid, sha_t


def test_claim_once_and_converge(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")], clock=clock, conv=conv)
    _apply_session_ok(store, tmp_path, "opencode:s2", [_msg("m1")], clock=clock, conv=conv,
                      name="s2.raw")
    _apply_session_ok(store, tmp_path, "opencode:child", [_msg("m1")], clock=clock,
                      conv=conv, name="c.raw")

    def _handoff(target: str) -> str:
        sha = store.get_session(target).raw_sha256
        ulid = generate_ulid()
        h_rec, h_sc = _handoff_parts(ulid, target, sha, "m1")
        assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok
        return ulid

    h1, h2 = _handoff("opencode:s1"), _handoff("opencode:s2")

    def _claim(handoff_ulid: str, producer: str = PRODUCER):
        c_ulid = generate_ulid()
        c_rec, c_sc = _claim_parts(c_ulid, handoff_ulid, "opencode:child", producer)
        return apply_claim(store, _dec(generate_ulid(), c_rec, c_sc, producer), clock), c_ulid

    r1, _ = _claim(h1)
    assert r1.ok
    r2, _ = _claim(h2)
    assert r2.ok
    assert (store.worktree / f"links/continuation/opencode%3Achild/{h1}.json").is_file()
    assert (store.worktree / f"links/continuation/opencode%3Achild/{h2}.json").is_file()

    dup_key = generate_ulid()
    c_rec, c_sc = _claim_parts(generate_ulid(), h1, "opencode:child")
    before = list(store.changed_paths())
    rdup = apply_claim(store, _dec(dup_key, c_rec, c_sc), clock)
    assert not rdup.ok and rdup.code == "already_claimed"
    _only_rejection(store, rdup, dup_key, before)


def test_claim_not_holder(tmp_path: Path):
    """H3：冒充他人名義認領（claimer 的持有者與認領單 producer 不同）→ 拒收。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    h1, _ = _setup_claim(store, tmp_path, clock, conv)
    key = generate_ulid()
    c_rec, c_sc = _claim_parts(generate_ulid(), h1, "opencode:child", producer=OTHER)
    before = list(store.changed_paths())
    r = apply_claim(store, _dec(key, c_rec, c_sc, producer=OTHER), clock)
    assert not r.ok and r.code == "not_holder"
    _only_rejection(store, r, key, before)


def test_claim_from_subsession(tmp_path: Path):
    """H3（D10）：子 Session 認領 → 拒收。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    h1, _ = _setup_claim(store, tmp_path, clock, conv,
                         claimer="opencode:sub", claimer_parent="opencode:main")
    key = generate_ulid()
    c_rec, c_sc = _claim_parts(generate_ulid(), h1, "opencode:sub")
    before = list(store.changed_paths())
    r = apply_claim(store, _dec(key, c_rec, c_sc), clock)
    assert not r.ok and r.code == "claim_from_subsession"
    _only_rejection(store, r, key, before)


def test_claim_self_claim(tmp_path: Path):
    """H3：claimer 就是交接單的目標自己 → 拒收。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    h1, _ = _setup_claim(store, tmp_path, clock, conv, claimer="opencode:s1")
    # opencode:s1 同時是 target 與 claimer，且 producer 相同 → 走到 self_claim
    key = generate_ulid()
    c_rec, c_sc = _claim_parts(generate_ulid(), h1, "opencode:s1")
    before = list(store.changed_paths())
    r = apply_claim(store, _dec(key, c_rec, c_sc), clock)
    assert not r.ok and r.code == "self_claim"
    _only_rejection(store, r, key, before)


def test_claim_completes_after_interrupted_round(tmp_path: Path):
    """H1 可重入：handoff 已標 claimed_by 但 claim 檔還沒寫 → 重跑補齊為 ok。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    h1, _ = _setup_claim(store, tmp_path, clock, conv)
    c_ulid = generate_ulid()
    handoff = store.get_record(f"handoff:{h1}")
    handoff["claimed_by"] = {"claim_id": f"claim:{c_ulid}",
                             "session_id": "opencode:child", "at": "2026-09-27T09:00:00Z"}
    from aistorage.agora import layout
    store.put_json(layout.handoff_path(h1), handoff)
    c_rec, c_sc = _claim_parts(c_ulid, h1, "opencode:child")
    r = apply_claim(store, _dec(generate_ulid(), c_rec, c_sc), clock)
    assert r.ok, r.code
    assert store.get_record(f"handoff:{h1}")["claimed_by"]["claim_id"] == f"claim:{c_ulid}"
    assert (store.worktree / f"links/continuation/opencode%3Achild/{h1}.json").is_file()


def _reserving_claim_parts(claim_ulid: str, handoff_ulid: str, claimer: str,
                           raw: tuple[Path, str, int], *,
                           snapshot_at: str = "2026-09-27T08:40:00Z",
                           source: str = "opencode",
                           producer: str = PRODUCER) -> tuple[dict, dict]:
    """帶**預留**的 claim：自己帶著一個空的新 Session 紀錄（review-73dbf2c H2）。"""
    record = {"id": f"claim:{claim_ulid}", "type": "claim", "producer": producer,
              "created_at": snapshot_at, "updated_at": snapshot_at,
              "case_id": None, "provenance": None}
    _raw_p, sha, size = raw
    sidecar = {
        "body": {"handoff_id": f"handoff:{handoff_ulid}",
                 "claimer_session_id": claimer},
        "raw": {"sha256": sha, "size": size},
        "session": {"source": source, "source_session_id": claimer.split(":", 1)[1],
                    "snapshot_at": snapshot_at, "status": "running",
                    "in_progress": False, "parent_id": None, "reserving": True},
    }
    return record, sidecar


def test_reserving_claim_creates_the_empty_new_session(tmp_path: Path):
    """H2：帶預留的 claim 被接受時，那個**空**的新 session 與接續 Link 一起進真本。

    這是 `agora checkout` 預留新 session 的路徑：認領者還不存在於任何來源應用，
    所以它由這一筆認領生出来。預留的內容是**零則訊息**的空匯出檔。
    """
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha_t = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                              clock=clock, conv=conv)
    h_ulid = generate_ulid()
    h_rec, h_sc = _handoff_parts(h_ulid, "opencode:s1", sha_t, "m1")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok

    # 預留用的是**真實形狀**的 opencode 空匯出檔（`agora checkout` 造的那種），
    # 所以真的轉換器讀得動，標題也會被記下來
    reserved = _write_opencode_export(
        tmp_path, "reserved.raw", "opencode:new1", "接手甲的工作", [])
    c_ulid = generate_ulid()
    c_rec, c_sc = _reserving_claim_parts(c_ulid, h_ulid, "opencode:new1", reserved)
    r = apply_claim(store, _dec(generate_ulid(), c_rec, c_sc, reserved[0]), clock)

    assert r.ok, r.code
    session = store.get_session("opencode:new1")
    assert session is not None, "預留應該生出一個新 session 紀錄"
    # 預留**不是** running：還沒有人開工，帶著期限（fff6169／review-2bc0785 M2）
    assert session.status == "reserved", session.status
    assert session.extra.get("reserved_until"), "預留要帶期限（期 1 不自動刪）"
    assert session.raw_sha256 == reserved[1]
    assert session.producer == PRODUCER
    assert session.parent_id is None, "預留的是主 Session（接續只能由主 Session 發起）"
    assert session.title == "接手甲的工作", "標題要來自預留，不是來源 session"
    assert session.extra.get("reading_status") == "ok"
    assert session.extra.get("reserved_by") == f"claim:{c_ulid}"
    # 預留是空的：零則訊息
    reading = OpencodeConverter().convert(reserved[0], session_id="opencode:new1")
    assert reading["messages"] == []
    # 接續 Link 也一併建好
    assert (store.worktree
            / f"links/continuation/opencode%3Anew1/{h_ulid}.json").is_file()
    assert store.get_record(f"handoff:{h_ulid}")["claimed_by"]["session_id"] \
        == "opencode:new1"


def test_a_reservation_ends_even_when_the_facts_cannot_be_read(tmp_path: Path):
    """facts 失敗時判不出 running／stopped，但「不再只是預留」是確定的。

    正常路徑（facts 讀得出來 → running）由
    `test_continuation_dedup_quota_smoke.test_reservation_disappears_once_the_session_really_starts`
    涵蓋；這裡專門守 `apply_session` 那個「facts 失敗」的分支——它很容易在改
    `_build_reserved_session` 時被順手改掉。
    """
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha_t = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                              clock=clock, conv=conv)
    h_ulid = generate_ulid()
    h_rec, h_sc = _handoff_parts(h_ulid, "opencode:s1", sha_t, "m1")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok
    reserved = _write_opencode_export(
        tmp_path, "reserved.raw", "opencode:new1", "接手甲的工作", [])
    c_ulid = generate_ulid()
    c_rec, c_sc = _reserving_claim_parts(c_ulid, h_ulid, "opencode:new1", reserved)
    assert apply_claim(
        store, _dec(generate_ulid(), c_rec, c_sc, reserved[0]), clock).ok

    class _BrokenConverter(FakeConverter):
        def facts(self, path: Path):
            raise ValueError("壞掉的原始紀錄")

    # 這裡上傳的 raw 與預留那份**不同**（所以不是 no-op），而且轉不出 facts
    broken = _write_raw(tmp_path, "broken.raw", [_msg("m9")])
    rec, sc = _session_parts("opencode:new1", "2026-09-27T10:00:00Z",
                             broken[1], broken[2])
    r = apply_session(store, _dec(generate_ulid(), rec, sc, broken[0]),
                      _BrokenConverter(), clock)
    assert r.ok, r.code

    session = store.get_session("opencode:new1")
    assert session.status == "running", session.status
    assert session.extra.get("reading_status") == "failed"
    assert session.extra.get("reading_error_code") == "facts_error"


def test_rejected_reserving_claim_writes_nothing_but_the_rejection(tmp_path: Path):
    """H2：被拒時**不能**在 Agora 留下那個預留（否則就是沒有人接手的孤兒）。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha_t = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                              clock=clock, conv=conv)
    h1 = generate_ulid()
    h_rec, h_sc = _handoff_parts(h1, "opencode:s1", sha_t, "m1")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok
    # 另一個 Session 先把它認領走了 → 這一筆必然 already_claimed
    other = _write_raw(tmp_path, "other.raw", [])
    o_rec, o_sc = _reserving_claim_parts(generate_ulid(), h1, "opencode:other", other)
    assert apply_claim(store, _dec(generate_ulid(), o_rec, o_sc, other[0]), clock).ok

    reserved = _write_raw(tmp_path, "reserved2.raw", [])
    key = generate_ulid()
    c_rec, c_sc = _reserving_claim_parts(generate_ulid(), h1, "opencode:new2", reserved)
    before = list(store.changed_paths())
    r = apply_claim(store, _dec(key, c_rec, c_sc, reserved[0]), clock)

    assert not r.ok and r.code == "already_claimed"
    assert store.get_session("opencode:new2") is None, \
        "被拒的認領不能留下預留下來的 session"
    _only_rejection(store, r, key, before)


def test_reserving_claim_without_a_raw_is_rejected(tmp_path: Path):
    """預留宣稱了 raw 卻沒帶過來 → 明確拒收，不寫入。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha_t = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                              clock=clock, conv=conv)
    h1 = generate_ulid()
    h_rec, h_sc = _handoff_parts(h1, "opencode:s1", sha_t, "m1")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok

    reserved = _write_raw(tmp_path, "reserved3.raw", [])
    key = generate_ulid()
    c_rec, c_sc = _reserving_claim_parts(generate_ulid(), h1, "opencode:new3", reserved)
    before = list(store.changed_paths())
    r = apply_claim(store, _dec(key, c_rec, c_sc, None), clock)

    assert not r.ok and r.code == "invalid_format"
    assert store.get_session("opencode:new3") is None
    _only_rejection(store, r, key, before)


def test_reserving_claim_must_reserve_the_claimer_itself(tmp_path: Path):
    """預留的 session 必須**就是**那個 claimer：不能挾帶別的 session 的內容。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha_t = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                              clock=clock, conv=conv)
    h1 = generate_ulid()
    h_rec, h_sc = _handoff_parts(h1, "opencode:s1", sha_t, "m1")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok

    reserved = _write_raw(tmp_path, "reserved4.raw", [])
    key = generate_ulid()
    c_rec, c_sc = _reserving_claim_parts(generate_ulid(), h1, "opencode:claimer", reserved)
    # sidecar 說預留的是 opencode:someone_else，body 卻說 claimer 是 opencode:claimer
    c_sc["session"]["source_session_id"] = "someone_else"
    before = list(store.changed_paths())
    r = apply_claim(store, _dec(key, c_rec, c_sc, reserved[0]), clock)

    assert not r.ok and r.code == "invalid_format"
    assert store.get_session("opencode:someone_else") is None
    _only_rejection(store, r, key, before)


def test_repeated_reserving_claim_is_idempotent(tmp_path: Path):
    """H1：同一個 claim id 重送（`--resume`）→ 冪等，不會生出第二個 session。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha_t = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1")],
                              clock=clock, conv=conv)
    h1 = generate_ulid()
    h_rec, h_sc = _handoff_parts(h1, "opencode:s1", sha_t, "m1")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok

    reserved = _write_raw(tmp_path, "reserved5.raw", [])
    c_ulid = generate_ulid()
    c_rec, c_sc = _reserving_claim_parts(c_ulid, h1, "opencode:new4", reserved)
    r1 = apply_claim(store, _dec(generate_ulid(), c_rec, c_sc, reserved[0]), clock)
    assert r1.ok and r1.code == "ok"

    # --resume：同一個 claim id、同一個 item_key、同一份 raw
    r2 = apply_claim(store, _dec(generate_ulid(), c_rec, c_sc, reserved[0]), clock)
    assert r2.ok and r2.code == "already"
    assert len(store.snapshots("opencode:new4")) == 1, "不該多出第二份快照"


# ---------------------------------------------------------------------------
# reference
# ---------------------------------------------------------------------------

def test_reference_single_link_monotonic(tmp_path: Path):
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    _apply_session_ok(store, tmp_path, "opencode:a", [_msg("m1")], clock=clock,
                      conv=conv, name="a.raw")
    _apply_session_ok(store, tmp_path, "opencode:b", [_msg("m1")], clock=clock,
                      conv=conv, name="b.raw")

    def _ref(read_at: str, producer: str = PRODUCER):
        r_ulid = generate_ulid()
        r_rec, r_sc = _ref_parts(r_ulid, "opencode:a", "opencode:b", read_at, producer)
        return apply_reference(store, _dec(generate_ulid(), r_rec, r_sc, producer), clock)

    assert _ref("2026-09-27T08:00:00Z").ok
    old_key = generate_ulid()
    r_rec = {"id": f"reference:{generate_ulid()}", "type": "reference",
             "producer": PRODUCER,
             "created_at": "2026-09-27T07:00:00Z", "updated_at": "2026-09-27T07:00:00Z",
             "case_id": None, "provenance": None}
    r_sc = {"body": {"from_session_id": "opencode:a", "to_session_id": "opencode:b",
                     "read_snapshot_at": "2026-09-27T07:00:00Z"}}
    before = list(store.changed_paths())
    rold = apply_reference(store, _dec(old_key, r_rec, r_sc), clock)
    assert not rold.ok and rold.code == "stale"
    _only_rejection(store, rold, old_key, before)
    assert _ref("2026-09-27T08:10:00.000000Z").ok
    link_p = store.worktree / "links/reference/opencode%3Aa/opencode%3Ab.json"
    assert json.loads(link_p.read_text(encoding="utf-8"))["read_snapshot_at"] == \
        "2026-09-27T08:10:00.000000Z"


def test_reference_not_holder_and_unknown_target(tmp_path: Path):
    """H3：冒 from 的名義建參考 → not_holder；to 不存在 → unknown_target。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    _apply_session_ok(store, tmp_path, "opencode:a", [_msg("m1")], clock=clock,
                      conv=conv, name="a.raw")
    _apply_session_ok(store, tmp_path, "opencode:b", [_msg("m1")], clock=clock,
                      conv=conv, name="b.raw")

    key1 = generate_ulid()
    r_rec, r_sc = _ref_parts(generate_ulid(), "opencode:a", "opencode:b",
                             "2026-09-27T08:00:00Z", producer=OTHER)
    before = list(store.changed_paths())
    r1 = apply_reference(store, _dec(key1, r_rec, r_sc, producer=OTHER), clock)
    assert not r1.ok and r1.code == "not_holder"
    _only_rejection(store, r1, key1, before)

    key2 = generate_ulid()
    r_rec2, r_sc2 = _ref_parts(generate_ulid(), "opencode:a", "opencode:ghost",
                               "2026-09-27T08:00:00Z")
    before = list(store.changed_paths())
    r2 = apply_reference(store, _dec(key2, r_rec2, r_sc2), clock)
    assert not r2.ok and r2.code == "unknown_target"
    _only_rejection(store, r2, key2, before)


def test_reference_corrupt_index_aborts(tmp_path: Path):
    """M3：參考索引損毀 → MismatchError 中止，不轉 REJECT。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    _apply_session_ok(store, tmp_path, "opencode:a", [_msg("m1")], clock=clock,
                      conv=conv, name="a.raw")
    _apply_session_ok(store, tmp_path, "opencode:b", [_msg("m1")], clock=clock,
                      conv=conv, name="b.raw")
    from aistorage.agora import layout
    store.put_json(layout.reference_link_path("opencode:a", "opencode:b"),
                   {"broken": "not-json-shape"})
    r_rec, r_sc = _ref_parts(generate_ulid(), "opencode:a", "opencode:b",
                             "2026-09-27T08:00:00Z")
    with pytest.raises(MismatchError):
        apply_reference(store, _dec(generate_ulid(), r_rec, r_sc), clock)


# ---------------------------------------------------------------------------
# rewrite：期 1 不提供
# ---------------------------------------------------------------------------

def test_rewrite_always_rejected(tmp_path: Path):
    """PM 決定：期 1 拿掉改寫，一律 REJECT(rewrite_not_supported)，真本不動。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    sha = _apply_session_ok(store, tmp_path, "opencode:s1", [_msg("m1"), _msg("m2")],
                            clock=clock, conv=conv)
    new_p, new_sha, new_size = _write_raw(tmp_path, "rw.raw", [_msg("m1"), _msg("m2")])
    rw_ulid = generate_ulid()
    rw_rec = {"id": f"rewrite:{rw_ulid}", "type": "rewrite",
              "producer": PRODUCER,
              "created_at": "2026-09-27T08:20:00Z", "updated_at": "2026-09-27T08:20:00Z",
              "case_id": None, "provenance": None}
    rw_sc = {"raw": {"sha256": new_sha, "size": new_size},
             "body": {"target_session_id": "opencode:s1",
                      "base_snapshot_sha256": sha, "reason": "遮蔽"}}
    key = generate_ulid()
    before = list(store.changed_paths())
    r = apply_rewrite(store, _dec(key, rw_rec, rw_sc, new_p), conv, clock)
    assert isinstance(r, ApplyResult)
    assert not r.ok and r.code == "rewrite_not_supported"
    assert r.rejected_at is not None
    _only_rejection(store, r, key, before)
    snaps = [s.snapshot_sha256 for s in store.snapshots("opencode:s1")]
    assert snaps == [sha]


# ---------------------------------------------------------------------------
# 真實轉換器（OpencodeConverter＋黃金樣本）
# ---------------------------------------------------------------------------

def _golden_raw(tmp_path: Path) -> tuple[Path, str]:
    src = Path("tests/unit/data/converters/opencode/basic.json")
    raw = src.read_bytes()
    p = tmp_path / "golden.raw"
    p.write_bytes(raw)
    return p, hashlib.sha256(raw).hexdigest().lower()


def test_golden_opencode_continuation(tmp_path: Path):
    """L：3.7 接續點驗證接上真實轉換器（revert 的訊息不可當接續點）。"""
    store = _new_store(tmp_path)
    conv = OpencodeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    raw_p, sha = _golden_raw(tmp_path)
    size = raw_p.stat().st_size
    rec, sc = _session_parts("opencode:ses_opencode_basic_001",
                             "2026-09-27T08:00:00Z", sha, size)
    assert apply_session(store, _dec(generate_ulid(), rec, sc, raw_p), conv, clock).ok

    # msg_002 是最後一則已完成且未撤銷 → ok
    h_rec, h_sc = _handoff_parts(generate_ulid(), "opencode:ses_opencode_basic_001",
                                 sha, "msg_002")
    assert apply_handoff(store, _dec(generate_ulid(), h_rec, h_sc), conv, clock).ok
    # msg_001 後面還有已完成的 → 不是最後一則 → 拒收
    k1 = generate_ulid()
    h1_rec, h1_sc = _handoff_parts(generate_ulid(), "opencode:ses_opencode_basic_001",
                                   sha, "msg_001")
    r1 = apply_handoff(store, _dec(k1, h1_rec, h1_sc), conv, clock)
    assert not r1.ok and r1.code == "invalid_continuation"
    # msg_003 已撤銷 → 拒收
    k2 = generate_ulid()
    h2_rec, h2_sc = _handoff_parts(generate_ulid(), "opencode:ses_opencode_basic_001",
                                   sha, "msg_003")
    r2 = apply_handoff(store, _dec(k2, h2_rec, h2_sc), conv, clock)
    assert not r2.ok and r2.code == "invalid_continuation"


# ---------------------------------------------------------------------------
# 9.x 單元對照：分裂／統合／相互參照（一輪 sort＋依型態 apply）
# ---------------------------------------------------------------------------

def _dispatch(store: AgoraStore, decisions: list[Decision], conv, clock) -> dict[str, ApplyResult]:
    out: dict[str, ApplyResult] = {}
    for d in sort_accepted_decisions(decisions):
        t = (d.record_metadata or {}).get("type")
        item_key = d.item.item_key
        if t == "session":
            out[item_key] = apply_session(store, d, conv, clock)
        elif t == "handoff":
            out[item_key] = apply_handoff(store, d, conv, clock)
        elif t == "claim":
            out[item_key] = apply_claim(store, d, clock)
        elif t == "reference":
            out[item_key] = apply_reference(store, d, clock)
        else:
            raise AssertionError(f"未預期的型態: {t}")
    return out


def _session_dec(tmp_path: Path, name: str, session_id: str, messages: list[dict],
                 snapshot_at: str, producer: str = PRODUCER) -> Decision:
    raw_p, sha, size = _write_raw(tmp_path, f"{name}.raw", messages)
    rec, sc = _session_parts(session_id, snapshot_at, sha, size, producer=producer)
    return _dec(generate_ulid(), rec, sc, raw_p, producer)


def test_split_one_to_two(tmp_path: Path):
    """9.1 對照：S1 同步＋兩張交接單同一批，S2、S3 各認領一張。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    batch = [_session_dec(tmp_path, "s1", "opencode:s1", [_msg("m1"), _msg("m2")],
                          "2026-09-27T08:00:00Z")]
    sha = hashlib.sha256((tmp_path / "s1.raw").read_bytes()).hexdigest()
    h1 = generate_ulid()
    r1, s1 = _handoff_parts(h1, "opencode:s1", sha, "m2")
    batch.append(_dec(generate_ulid(), r1, s1))
    h2 = generate_ulid()
    r2, s2 = _handoff_parts(h2, "opencode:s1", sha, "m2")
    batch.append(_dec(generate_ulid(), r2, s2))
    res = _dispatch(store, batch, conv, clock)
    assert all(r.ok for r in res.values()), {k: v.code for k, v in res.items()}

    for new_id, h in (("opencode:s2", h1), ("opencode:s3", h2)):
        batch2 = [_session_dec(tmp_path, new_id.replace(":", "_"), new_id,
                               [_msg("m1")], "2026-09-27T08:30:00Z")]
        c_rec, c_sc = _claim_parts(generate_ulid(), h, new_id)
        batch2.append(_dec(generate_ulid(), c_rec, c_sc))
        res2 = _dispatch(store, batch2, conv, clock)
        assert all(r.ok for r in res2.values()), {k: v.code for k, v in res2.items()}

    assert store.get_session("opencode:s1").status == "running"  # S1 不受影響
    assert (store.worktree / f"links/continuation/opencode%3As2/{h1}.json").is_file()
    assert (store.worktree / f"links/continuation/opencode%3As3/{h2}.json").is_file()


def test_converge_two_to_one(tmp_path: Path):
    """9.2 對照：S2、S3 各交出一張，S4 認領兩張。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    batch = [
        _session_dec(tmp_path, "s2", "opencode:s2", [_msg("m1")], "2026-09-27T08:00:00Z"),
        _session_dec(tmp_path, "s3", "opencode:s3", [_msg("m1")], "2026-09-27T08:00:00Z"),
        _session_dec(tmp_path, "s4", "opencode:s4", [_msg("m1")], "2026-09-27T08:30:00Z"),
    ]
    for sid, name in (("opencode:s2", "s2"), ("opencode:s3", "s3")):
        sha = hashlib.sha256((tmp_path / f"{name}.raw").read_bytes()).hexdigest()
        h = generate_ulid()
        r, s = _handoff_parts(h, sid, sha, "m1")
        batch.append(_dec(generate_ulid(), r, s))
        c_rec, c_sc = _claim_parts(generate_ulid(), h, "opencode:s4")
        batch.append(_dec(generate_ulid(), c_rec, c_sc))
    res = _dispatch(store, batch, conv, clock)
    assert all(r.ok for r in res.values()), {k: v.code for k, v in res.items()}
    links = list((store.worktree / "links/continuation/opencode%3As4").iterdir())
    assert len(links) == 2


def test_mutual_reference_keeps_single_link(tmp_path: Path):
    """9.3 對照：S2↔S3 反覆互相參考，各自只留一條，記最新快照時間。"""
    store = _new_store(tmp_path)
    conv = FakeConverter()
    clock = FixedClock("2026-09-27T09:00:00Z")
    batch = [
        _session_dec(tmp_path, "s2", "opencode:s2", [_msg("m1")], "2026-09-27T08:00:00Z"),
        _session_dec(tmp_path, "s3", "opencode:s3", [_msg("m1")], "2026-09-27T08:00:00Z"),
    ]
    for frm, to, at in [("opencode:s2", "opencode:s3", "2026-09-27T08:01:00Z"),
                        ("opencode:s3", "opencode:s2", "2026-09-27T08:02:00Z"),
                        ("opencode:s2", "opencode:s3", "2026-09-27T08:03:00Z"),
                        ("opencode:s3", "opencode:s2", "2026-09-27T08:04:00Z")]:
        r_rec, r_sc = _ref_parts(generate_ulid(), frm, to, at)
        batch.append(_dec(generate_ulid(), r_rec, r_sc))
    res = _dispatch(store, batch, conv, clock)
    assert all(r.ok for r in res.values()), {k: v.code for k, v in res.items()}
    l1 = json.loads((store.worktree / "links/reference/opencode%3As2/opencode%3As3.json")
                    .read_text(encoding="utf-8"))
    l2 = json.loads((store.worktree / "links/reference/opencode%3As3/opencode%3As2.json")
                    .read_text(encoding="utf-8"))
    assert l1["read_snapshot_at"] == "2026-09-27T08:03:00Z"
    assert l2["read_snapshot_at"] == "2026-09-27T08:04:00Z"
    # 被參考的一方沒有任何變化
    assert store.get_session("opencode:s2").raw_sha256 == \
        hashlib.sha256((tmp_path / "s2.raw").read_bytes()).hexdigest()
