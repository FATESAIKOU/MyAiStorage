"""拒收原因發佈（publish/rejections）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aistorage.agora.store import AgoraStore, FakeRawStorage
from aistorage.drive.model import GOOGLE_FOLDER_MIME
from aistorage.errors import MismatchError
from aistorage.intake.evaluate import Decision, DecisionKind
from aistorage.intake.scan import InboxItem
from aistorage.publish.rejections import (
    RejectionRow,
    collect_rejections,
    collect_run_rejections,
    collect_true_copy_rejections,
)
from aistorage.schema import generate_ulid

T0 = "2026-09-27T08:00:00.000Z"
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID2 = "01BRZ3NDEKTSV4RRFFQ69G5FAV"


def _store(tmp_path: Path) -> AgoraStore:
    return AgoraStore(
        worktree=tmp_path / "wt",
        raw_storage=FakeRawStorage(),
        git=None,
        temp_dir=tmp_path / "store_tmp",
    )


def _drive_file(file_id: str, created_time: str, name: str = "x.json"):
    from aistorage.drive.model import DriveFile

    return DriveFile(
        id=file_id,
        name=name,
        mime_type="application/json",
        parents=("inbox-1",),
        size=10,
        sha256="a" * 64,
        md5="b" * 32,
        created_time=created_time,
        modified_time=created_time,
        trashed=False,
    )


def _reject(item: InboxItem, code: str, *, authenticated: bool) -> Decision:
    return Decision(
        kind=DecisionKind.REJECT,
        item=item,
        code=code,
        authenticated=authenticated,
        rejected_at=T0,
    )


def test_true_copy_rejections_are_read_without_content(tmp_path: Path):
    store = _store(tmp_path)
    store.put_json(
        f"_committer/rejections/{ULID}.json",
        {"code": "stale", "at": T0, "item_id": "opencode:ses_1"},
    )
    store.put_json(
        f"_committer/rejections/{ULID2}.json",
        {"code": "too_old", "at": T0},
    )

    rows = collect_true_copy_rejections(store)
    assert [(r.item_key, r.code, r.item_id, r.authenticated) for r in rows] == [
        (ULID, "stale", "opencode:ses_1", True),
        (ULID2, "too_old", None, True),
    ]
    # 只有代碼、時間、item 與 detail，沒有任何欄位裝載內容
    # （detail 目前只帶公開的 session id，見 review-55edd374 M3）
    assert set(rows[0].to_dict()) == {
        "item_key", "code", "at", "item_id", "authenticated", "detail"
    }
    assert rows[0].detail is None and rows[1].detail is None


def test_true_copy_rejection_carries_its_detail(tmp_path: Path):
    """真本拒收紀錄裡的 `detail` 會被帶進讀取視圖（寫入端要靠它知道被什麼擋住）。"""
    store = _store(tmp_path)
    store.put_json(
        f"_committer/rejections/{ULID}.json",
        {"code": "link_quota_exceeded", "at": T0, "item_id": None,
         "detail": "佔住額度的預留：opencode:ses_a"},
    )
    store.put_json(
        f"_committer/rejections/{ULID2}.json",
        {"code": "link_quota_exceeded", "at": T0, "detail": ""},   # 壞型別 → None
    )

    rows = {r.item_key: r for r in collect_true_copy_rejections(store)}
    assert rows[ULID].detail == "佔住額度的預留：opencode:ses_a"
    assert rows[ULID2].detail is None


def test_corrupt_true_copy_rejection_aborts(tmp_path: Path):
    store = _store(tmp_path)
    p = store.worktree / "_committer" / "rejections" / f"{ULID}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(MismatchError):
        collect_true_copy_rejections(store)


def test_pre_auth_rejection_uses_earliest_created_time(tmp_path: Path):
    """PM 決定 4：驗章前的拒收用項目檔案最早的 created_time，
    deletable_after = rejected_at + 24h 才會到期（第 13 步才刪得掉）。"""
    item = InboxItem(
        item_key=ULID,
        inbox_folder_id="inbox-1",
        sidecars=(_drive_file("f-sidecar", "2026-09-25T01:00:00.000Z"),),
        sigs=(_drive_file("f-sig", "2026-09-26T01:00:00.000Z"),),
    )
    rows = collect_run_rejections([_reject(item, "bad_signature", authenticated=False)])
    assert len(rows) == 1
    assert rows[0].at == "2026-09-25T01:00:00.000000Z"  # 不是「當下」
    assert rows[0].item_id is None
    assert rows[0].authenticated is False


def test_authenticated_run_rejections_come_from_the_true_copy_only(tmp_path: Path):
    """驗章後的拒收已寫進真本，不該再從本輪決策收一次（否則有兩個時間來源）。"""
    store = _store(tmp_path)
    store.put_json(
        f"_committer/rejections/{ULID}.json",
        {"code": "stale", "at": T0, "item_id": "opencode:ses_1"},
    )
    item = InboxItem(
        item_key=ULID,
        inbox_folder_id="inbox-1",
        sidecars=(_drive_file("f-sidecar", "2026-09-25T01:00:00.000Z"),),
        sigs=(_drive_file("f-sig", "2026-09-25T01:00:00.000Z"),),
    )
    assert collect_run_rejections([_reject(item, "stale", authenticated=True)]) == []
    merged = collect_rejections(store, [_reject(item, "stale", authenticated=True)])
    assert len(merged) == 1 and merged[0].item_id == "opencode:ses_1"


def test_collect_merges_sorted_and_deduped(tmp_path: Path):
    store = _store(tmp_path)
    store.put_json(
        f"_committer/rejections/{ULID2}.json", {"code": "too_old", "at": T0}
    )
    item = InboxItem(
        item_key=ULID,
        inbox_folder_id="inbox-1",
        sidecars=(_drive_file("f-sidecar", "2026-09-25T01:00:00.000Z"),),
        sigs=(_drive_file("f-sig", "2026-09-25T01:00:00.000Z"),),
    )
    rows = collect_rejections(store, [_reject(item, "orphan", authenticated=False)])
    assert [r.item_key for r in rows] == [ULID, ULID2]  # 依 item_key 排序
    assert rows[0].code == "orphan" and rows[1].code == "too_old"


def test_accepted_decisions_are_never_published_as_rejections(tmp_path: Path):
    store = _store(tmp_path)
    item = InboxItem(
        item_key=ULID,
        inbox_folder_id="inbox-1",
        sidecars=(_drive_file("f-sidecar", "2026-09-25T01:00:00.000Z"),),
    )
    accept = Decision(kind=DecisionKind.ACCEPT, item=item, code="ok", authenticated=True)
    assert collect_rejections(store, [accept]) == []


def test_rejection_row_roundtrip_shape():
    row = RejectionRow(item_key=ULID, code="orphan", at=T0)
    assert row.to_dict() == {
        "item_key": ULID,
        "code": "orphan",
        "at": T0,
        "item_id": None,
        "authenticated": 0,
        "detail": None,
    }
    assert json.loads(json.dumps(row.to_dict()))["code"] == "orphan"
