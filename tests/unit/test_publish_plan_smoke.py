"""發佈計畫（publish/plan）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.errors import MismatchError
from aistorage.publish.plan import (
    collect_snapshot_targets,
    plan_publish,
)
from aistorage.readview.model import FileRef, Manifest, initial_manifest
from aistorage.schema import generate_ulid

T0 = "2026-09-27T08:00:00.000Z"
T1 = "2026-09-27T09:00:00.000Z"
PRODUCER = "profile:mac-opencode"
VERSIONS = {"opencode": "1"}


def _store(tmp_path: Path) -> AgoraStore:
    return AgoraStore(
        worktree=tmp_path / "wt",
        raw_storage=FakeRawStorage(),
        git=None,
        temp_dir=tmp_path / "store_tmp",
    )


def _add_session(
    store: AgoraStore,
    session_id: str,
    texts: tuple[str, ...],
    *,
    snapshot_at: str = T0,
    title: str | None = None,
    producer: str = PRODUCER,
) -> str:
    """在真本放入一個 Session 的一個快照，回傳 snapshot_sha256。"""
    raw = json.dumps({"texts": list(texts), "title": title}, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    raw_path = store._temp_dir / f"seed-{sha}.raw"
    raw_path.write_bytes(raw)
    rec = SessionRecord(
        id=session_id,
        producer=producer,
        created_at=T0,
        updated_at=snapshot_at,
        status="stopped",
        snapshot_at=snapshot_at,
        raw_sha256=sha,
        raw_size=len(raw),
        committed_at=snapshot_at,
        last_item_key=generate_ulid(),
        stopped_at=snapshot_at,
        title=title,
    )
    store.put_session(rec, raw_path)
    return sha


def _handoff(store: AgoraStore, target_session_id: str, snapshot_sha256: str) -> str:
    """寫一張釘住某個快照的交接單，回傳 handoff id。"""
    ulid = generate_ulid()
    handoff_id = f"handoff:{ulid}"
    store.put_json(
        layout.handoff_path(ulid),
        {
            "id": handoff_id,
            "type": "handoff",
            "producer": PRODUCER,
            "created_at": T0,
            "updated_at": T0,
            "case_id": None,
            "provenance": None,
            "body": {
                "target_session_id": target_session_id,
                "continuation": {
                    "snapshot_sha256": snapshot_sha256,
                    "message_id": "m0",
                },
                "content": "交接說明（自編測試內容）",
            },
            "claimed_by": None,
            "committed_at": T0,
        },
    )
    return handoff_id


def _prev_manifest(
    *, generation: int, files: tuple[str, ...], index_id: str, retired=()
) -> Manifest:
    return Manifest(
        format="aistorage.readview/v1",
        element="agora",
        generation=generation,
        published_at=T0,
        agora_main_sha="main-1",
        converter_versions=dict(VERSIONS),
        index=FileRef(index_id, "1" * 64, 100),
        files=files,
        retired=tuple(retired),
    )


def _ref(fid: str) -> FileRef:
    return FileRef(id=fid, sha256="2" * 64, size=5)


def test_targets_are_latest_snapshot_of_each_session(tmp_path: Path):
    store = _store(tmp_path)
    s1 = _add_session(store, "opencode:ses_1", ("甲",), title="甲的 Session")
    s2 = _add_session(store, "opencode:ses_2", ("乙",))

    targets = collect_snapshot_targets(store)
    assert [(t.session_id, t.snapshot_sha256, t.is_latest) for t in targets] == [
        ("opencode:ses_1", s1, True),
        ("opencode:ses_2", s2, True),
    ]


def test_first_publish_uploads_every_reading(tmp_path: Path):
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_2", ("乙",))

    plan = plan_publish(store, None, {}, VERSIONS)
    assert len(plan.readings_new) == 2 and plan.readings_keep == ()
    assert plan.generation == 1 and not plan.full_rebuild
    assert plan.retire_now == () and plan.delete_now == ()
    assert plan.switch_index and not plan.skipped


def test_no_change_means_nothing_to_upload(tmp_path: Path):
    store = _store(tmp_path)
    sha = _add_session(store, "opencode:ses_1", ("甲",))
    prev = _prev_manifest(generation=1, files=("idx-1", "rid-1"), index_id="idx-1")
    prev_readings = {("opencode:ses_1", sha): _ref("rid-1")}

    plan = plan_publish(store, prev, prev_readings, VERSIONS)
    assert plan.readings_new == ()
    assert [r.key for r in plan.readings_keep] == [("opencode:ses_1", sha)]
    # 舊 index 一定被退役（新的要等上傳完才知道 id），但還不能刪
    assert plan.retire_now == ("idx-1",)
    assert plan.delete_now == ()


def test_plan_is_skipped_when_nothing_to_publish_or_retire(tmp_path: Path):
    store = _store(tmp_path)
    prev = _prev_manifest(generation=2, files=(), index_id="idx-2")
    assert plan_publish(store, prev, {}, VERSIONS).skipped


def test_only_changed_session_is_re_uploaded(tmp_path: Path):
    store = _store(tmp_path)
    old_s1 = _add_session(store, "opencode:ses_1", ("甲",))
    s2 = _add_session(store, "opencode:ses_2", ("乙",))
    new_s1 = _add_session(store, "opencode:ses_1", ("甲", "丙"), snapshot_at=T1)

    prev = _prev_manifest(
        generation=1, files=("idx-1", "rid-1a", "rid-2"), index_id="idx-1"
    )
    prev_readings = {
        ("opencode:ses_1", old_s1): _ref("rid-1a"),
        ("opencode:ses_2", s2): _ref("rid-2"),
    }
    plan = plan_publish(store, prev, prev_readings, VERSIONS)
    assert [r.key for r in plan.readings_new] == [("opencode:ses_1", new_s1)]
    assert [r.key for r in plan.readings_keep] == [("opencode:ses_2", s2)]
    assert plan.retire_now == ("idx-1", "rid-1a")


def test_pinned_snapshot_stays_a_target(tmp_path: Path):
    store = _store(tmp_path)
    old = _add_session(store, "opencode:ses_1", ("甲",))
    latest = _add_session(store, "opencode:ses_1", ("甲", "乙"), snapshot_at=T1)
    _handoff(store, "opencode:ses_1", old)

    targets = collect_snapshot_targets(store)
    assert [(t.snapshot_sha256, t.is_latest, t.pinned) for t in targets] == [
        (latest, True, False),
        (old, False, True),
    ]

    prev = _prev_manifest(generation=1, files=("idx-1",), index_id="idx-1")
    plan = plan_publish(store, prev, {}, VERSIONS)
    assert len(plan.readings_new) == 2  # 最新 ＋ 被釘住的那一份


def test_retired_files_deleted_only_one_generation_later(tmp_path: Path):
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    prev = _prev_manifest(
        generation=5, files=("idx-5", "new"), index_id="idx-5", retired=(("old", 4),)
    )
    plan = plan_publish(store, prev, {}, VERSIONS)
    assert plan.generation == 6
    assert plan.delete_now == ("old",)  # 4 < 6 − 1：退役滿一個世代才刪

    # 刪除失敗時檔案仍在 retired 裡，下一輪會再試一次（冪等）
    prev2 = _prev_manifest(generation=6, files=("idx-5", "new"), index_id="idx-5",
                           retired=(("old", 4),))
    plan2 = plan_publish(store, prev2, {}, VERSIONS)
    assert plan2.delete_now == ("old",)


def test_converter_version_change_triggers_full_rebuild(tmp_path: Path):
    store = _store(tmp_path)
    sha = _add_session(store, "opencode:ses_1", ("甲",))
    prev = _prev_manifest(generation=1, files=("idx-1", "rid-1"), index_id="idx-1")
    prev_readings = {("opencode:ses_1", sha): _ref("rid-1")}

    plan = plan_publish(store, prev, prev_readings, {"opencode": "2"})
    assert plan.full_rebuild
    assert len(plan.readings_new) == 1 and plan.readings_keep == ()


def test_force_full_rebuild_reuploads_everything(tmp_path: Path):
    store = _store(tmp_path)
    sha = _add_session(store, "opencode:ses_1", ("甲",))
    prev = _prev_manifest(generation=1, files=("idx-1", "rid-1"), index_id="idx-1")
    plan = plan_publish(
        store, prev, {("opencode:ses_1", sha): _ref("rid-1")}, VERSIONS, force_full=True
    )
    assert plan.full_rebuild and len(plan.readings_new) == 1 and plan.readings_keep == ()


def test_batching_does_not_switch_index(tmp_path: Path):
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_2", ("乙",))

    plan = plan_publish(store, None, {}, VERSIONS, max_new_readings=1)
    assert len(plan.readings_new) == 1
    assert plan.batch_remaining == 1
    assert not plan.switch_index
    assert plan.retire_now == () and plan.delete_now == ()


def test_initial_manifest_is_treated_as_no_previous_generation(tmp_path: Path):
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    prev = initial_manifest(published_at=T0)
    assert prev.is_initial
    plan = plan_publish(store, prev, {}, VERSIONS)
    assert plan.generation == 1 and len(plan.readings_new) == 1


def test_pinned_snapshot_missing_from_history_aborts(tmp_path: Path):
    store = _store(tmp_path)
    _add_session(store, "opencode:ses_1", ("甲",))
    _handoff(store, "opencode:ses_1", "f" * 64)
    with pytest.raises(MismatchError):
        collect_snapshot_targets(store)


def test_publish_plan_reads_pinned_snapshot_from_continuation_link(tmp_path: Path):
    store = _store(tmp_path)
    old = _add_session(store, "opencode:ses_1", ("甲",))
    _add_session(store, "opencode:ses_1", ("甲", "乙"), snapshot_at=T1)
    ulid = generate_ulid()
    store.put_json(
        layout.continuation_link_path("opencode:ses_2", ulid),
        {
            "from": "opencode:ses_2",
            "to": "opencode:ses_1",
            "continuation": {"snapshot_sha256": old, "message_id": "m0"},
            "handoff_id": f"handoff:{ulid}",
            "claim_id": f"claim:{ulid}",
        },
    )
    targets = collect_snapshot_targets(store)
    assert [t.snapshot_sha256 for t in targets if not t.is_latest] == [old]
    assert all(t.pinned for t in targets if not t.is_latest)


def test_unknown_session_meta_is_not_silently_skipped(tmp_path: Path):
    store = _store(tmp_path)
    store.put_json("sessions/opencode/ghost/meta.json", {"id": "opencode:ghost"})
    with pytest.raises(MismatchError):
        collect_snapshot_targets(store)
