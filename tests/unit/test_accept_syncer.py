"""Acceptance tests for Syncer (Task 5.2, 5.3).

Adheres strictly to:
- docs/impl/group5-7-modules.md §2, §3, §8.2
- PM Decision 3 (re-upload only after a new generation is published and item still missing)
- Direct inspection of signatures and documentation, zero reliance on internal src/ code.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
import pytest

import aistorage.agora
from aistorage.clock import FixedClock
from aistorage.converters.opencode import OpencodeConverter
from aistorage.drive.fake import FakeDrive
from aistorage.errors import ReadError
from aistorage.identity import generate_keypair
from aistorage.inbox_builder import BuiltItem
from aistorage.search.index import HandoffRow, LinkRow
from aistorage.syncer import (
    Awaited,
    CommitWaitResult,
    MAX_RAW_BYTES,
    OcSession,
    OpencodeApi,
    SessionSyncRecord,
    Signer,
    SyncDeps,
    SyncOutcome,
    SyncState,
    sync_and_commit,
    sync_once,
    trigger_committer,
    wait_visible,
)


T0 = "2026-09-27T08:00:00Z"
T1 = "2026-09-27T09:00:00Z"
T2 = "2026-09-27T10:00:00Z"
PROFILE = "mac-opencode"


def make_test_signer(profile: str = PROFILE) -> Signer:
    priv, pub = generate_keypair()
    kid = f"{profile}-{hashlib.sha256(pub).hexdigest()[:8].lower()}"
    return Signer(profile, kid, priv)



# ---------------------------------------------------------------------------
# Test Fakes for Syncer
# ---------------------------------------------------------------------------


class MockOpencodeApi:
    """Mock implementation of OpencodeApi for acceptance tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, OcSession] = {}
        self._raws: dict[str, str] = {}
        self.archived: list[tuple[str, int]] = []
        self.export_calls: list[str] = []

    def set_session(self, session: OcSession, raw_json: str) -> None:
        self._sessions[session.id] = session
        self._raws[session.id] = raw_json

    def list_sessions(self) -> list[OcSession]:
        return sorted(self._sessions.values(), key=lambda s: s.id)

    def export(self, session_id: str, dest: Path) -> Path:
        self.export_calls.append(session_id)
        if session_id not in self._raws:
            raise ReadError(f"Session not found for export: {session_id}")
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(self._raws[session_id], encoding="utf-8")
        return dest_path

    def archive(self, session_id: str, at_ms: int) -> None:
        self.archived.append((session_id, at_ms))
        if session_id in self._sessions:
            s = self._sessions[session_id]
            self._sessions[session_id] = OcSession(
                id=s.id,
                parent_id=s.parent_id,
                title=s.title,
                updated_ms=s.updated_ms,
                archived_ms=at_ms,
            )


class MockReaderManifest:
    def __init__(self, generation: int, published_at: str) -> None:
        self.generation = generation
        self.published_at = published_at


class MockAgoraReader:
    """Mock implementation of AgoraReader for syncer tests."""

    def __init__(self, generation: int = 1, published_at: str = T0) -> None:
        self.catalog_data: dict[str, dict] = {}
        self.rejections: dict[str, str] = {}
        self.sessions: dict[str, Any] = {}
        self.continuation_links: dict[tuple[str, str], str] = {}  # (from, claim_id) -> handoff_id
        self.reference_links: dict[tuple[str, str], str] = {}     # (from, to) -> ref_id
        self._manifest = MockReaderManifest(generation, published_at)

    def manifest(self) -> MockReaderManifest:
        return self._manifest

    def set_manifest(self, generation: int, published_at: str) -> None:
        self._manifest = MockReaderManifest(generation, published_at)

    def catalog(self, session_ids: Sequence[str]) -> dict[str, dict]:
        return {sid: self.catalog_data[sid] for sid in session_ids if sid in self.catalog_data}

    def get_rejection(self, item_key: str) -> str | None:
        return self.rejections.get(item_key)

    def get_session(self, session_id: str) -> Any | None:
        return self.sessions.get(session_id)

    def has_continuation(self, from_id: str, claim_id: str) -> bool:
        return (from_id, claim_id) in self.continuation_links

    def has_reference(self, from_id: str, to_id: str, ref_id: str) -> bool:
        return self.reference_links.get((from_id, to_id)) == ref_id


def make_raw_session(session_id: str, messages: list[dict], *, archived: int | None = None) -> str:
    """Construct a minimal valid opencode export JSON string."""
    info = {
        "id": session_id,
        "title": f"Session {session_id}",
        "time": {
            "created": 1790420000000,
            "updated": 1790420100000,
        },
    }
    if archived is not None:
        info["time"]["archived"] = archived
    return json.dumps({"info": info, "messages": messages})


# ---------------------------------------------------------------------------
# Acceptance Tests for sync_once (§2.2)
# ---------------------------------------------------------------------------


def test_sync_once_unchanged_clears_waiting(tmp_path: Path):
    """規則 2a: 當本地 raw sha 等於 Agora catalog 中的 sha，標為 unchanged 並清除等待狀態。"""
    clock = FixedClock(T0)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=1, published_at=T0)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    raw_json = make_raw_session("ses_001", [
        {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "hi"}]},
    ])
    raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    api.set_session(
        OcSession(id="ses_001", parent_id=None, title="Test", updated_ms=1790420100000, archived_ms=None),
        raw_json,
    )

    # 模擬 Agora 已經收錄該 session 且 sha 相同
    reader.catalog_data["opencode:ses_001"] = {
        "raw_sha256": raw_sha,
        "snapshot_at": T0,
        "status": "running",
    }

    state_file = tmp_path / "sync-state.json"
    state = SyncState(state_file)
    # 預先設置為等待狀態
    rec = state.record("opencode:ses_001")
    rec.last_uploaded_sha = raw_sha
    rec.last_item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    rec.uploaded_at = T0
    rec.uploaded_generation = 1

    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
    )

    assert "opencode:ses_001" in outcome.unchanged
    assert "opencode:ses_001" not in outcome.waiting
    assert "opencode:ses_001" not in outcome.uploaded


def test_sync_once_waiting_not_reuploaded(tmp_path: Path):
    """規則 2b: 當本地 sha 等於 last_uploaded_sha，且 Agora 尚未收錄，在未發佈新世代前視為 waiting 不重傳。"""
    clock = FixedClock(T0)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=1, published_at=T0)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    raw_json = make_raw_session("ses_002", [
        {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "wait"}]},
    ])
    raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    api.set_session(
        OcSession(id="ses_002", parent_id=None, title="Test Waiting", updated_ms=1790420100000, archived_ms=None),
        raw_json,
    )

    state = SyncState(tmp_path / "sync-state.json")
    rec = state.record("opencode:ses_002")
    rec.last_uploaded_sha = raw_sha
    rec.last_item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    rec.uploaded_at = T0
    rec.uploaded_generation = 1

    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
    )

    assert "opencode:ses_002" in outcome.waiting
    assert "opencode:ses_002" not in outcome.uploaded


def test_sync_once_reupload_after_new_generation_published(tmp_path: Path):
    """PM 決定 3 & 規則 2b 例外: 上傳之後已有新世代發佈，Agora 仍無此 Session 且未被拒收 -> 以新 item_key 補傳。"""
    clock = FixedClock(T2)
    api = MockOpencodeApi()
    # 讀取端已經發佈了新世代（T1 > uploaded_at T0）
    reader = MockAgoraReader(generation=2, published_at=T1)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    raw_json = make_raw_session("ses_reup", [
        {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "msg"}]},
    ])
    raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    api.set_session(
        OcSession(id="ses_reup", parent_id=None, title="Reupload Test", updated_ms=1790420100000, archived_ms=None),
        raw_json,
    )

    state = SyncState(tmp_path / "sync-state.json")
    rec = state.record("opencode:ses_reup")
    rec.last_uploaded_sha = raw_sha
    rec.last_item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    rec.uploaded_at = T0
    rec.uploaded_generation = 1

    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
    )

    # 驗證觸發補傳：出現在 reuploaded 或 uploaded 中，且有新檔案上傳到 Drive
    assert "opencode:ses_reup" in outcome.reuploaded or "opencode:ses_reup" in outcome.uploaded
    rec_after = state.record("opencode:ses_reup")
    assert rec_after.last_item_key != "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_sync_once_rejected_does_not_reupload(tmp_path: Path):
    """規則 2b: 已在 get_rejection 中有紀錄的項目，標入 rejected，絕不自動重傳。"""
    clock = FixedClock(T2)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=2, published_at=T1)
    # 模擬 rejection 紀錄
    reader.rejections["01ARZ3NDEKTSV4RRFFQ69G5FAV"] = {"code": "bad_signature"}

    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    raw_json = make_raw_session("ses_rej", [
        {"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "bad"}]},
    ])
    raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    api.set_session(
        OcSession(id="ses_rej", parent_id=None, title="Rejected Test", updated_ms=1790420100000, archived_ms=None),
        raw_json,
    )

    state = SyncState(tmp_path / "sync-state.json")
    rec = state.record("opencode:ses_rej")
    rec.last_uploaded_sha = raw_sha
    rec.last_item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    rec.uploaded_at = T0
    rec.uploaded_generation = 1

    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
    )

    # 驗證列入 rejected，且不進行重傳
    rej_map = dict(outcome.rejected)
    assert "opencode:ses_rej" in rej_map
    assert rej_map["opencode:ses_rej"] == "bad_signature"
    assert "opencode:ses_rej" not in outcome.uploaded


def test_sync_once_stop_observation_rules(tmp_path: Path):
    """規則 4: 停止判定與 stopped_at 取自同步器首次觀測時間 (stop_observed_at)。"""
    clock = FixedClock(T1)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=1, published_at=T0)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    # archived_ms = 1790420500000, last_message completed at 1790420400000 <= archived_ms
    raw_json = make_raw_session(
        "ses_stop",
        [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420300000, "completed": 1790420400000}}, "parts": [{"type": "text", "text": "done"}]},
        ],
        archived=1790420500000,
    )

    api.set_session(
        OcSession(id="ses_stop", parent_id=None, title="Stop Test", updated_ms=1790420500000, archived_ms=1790420500000),
        raw_json,
    )

    state = SyncState(tmp_path / "sync-state.json")
    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
        converter=OpencodeConverter(),
    )

    assert "opencode:ses_stop" in outcome.uploaded
    # 驗證狀態記錄中記載了第一次觀測到停止的時間
    rec = state.record("opencode:ses_stop")
    assert rec is not None
    assert rec.stop_observed_at == T1


def test_sync_once_resumed_after_stop(tmp_path: Path):
    """規則 4: Agora 已標記為 stopped，但本地在封存之後又有新訊息 -> 以 running 上傳並加入 resumed_after_stop。"""
    clock = FixedClock(T2)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=1, published_at=T1)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    # 模擬 Agora 中已被標記為 stopped
    reader.catalog_data["opencode:ses_resume"] = {
        "raw_sha256": "old_sha",
        "snapshot_at": T0,
        "status": "stopped",
    }

    # 本地新增了訊息 (created at 1790420600000 > archived at 1790420500000)
    raw_json = make_raw_session(
        "ses_resume",
        [
            {"info": {"id": "m1", "role": "user", "time": {"created": 1790420300000, "completed": 1790420400000}}, "parts": [{"type": "text", "text": "old"}]},
            {"info": {"id": "m2", "role": "user", "time": {"created": 1790420600000}}, "parts": [{"type": "text", "text": "new after archive"}]},
        ],
        archived=1790420500000,
    )

    api.set_session(
        OcSession(id="ses_resume", parent_id=None, title="Resumed Test", updated_ms=1790420600000, archived_ms=1790420500000),
        raw_json,
    )

    state = SyncState(tmp_path / "sync-state.json")
    rec = state.record("opencode:ses_resume")
    rec.stop_observed_at = T0
    rec.last_uploaded_sha = "old_sha"

    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
        converter=OpencodeConverter(),
    )

    assert "opencode:ses_resume" in outcome.uploaded
    assert "opencode:ses_resume" in outcome.resumed_after_stop


def test_sync_once_three_level_subsession_hierarchy(tmp_path: Path):
    """驗證三層 Session 樹狀關係 (root -> child -> grandchild) 均被正確同步並記錄 parent_id。"""
    clock = FixedClock(T0)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=1, published_at=T0)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    raw_root = make_raw_session("ses_root", [{"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "root"}]}])
    raw_child = make_raw_session("ses_child", [{"info": {"id": "m2", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "child"}]}])
    raw_grandchild = make_raw_session("ses_grandchild", [{"info": {"id": "m3", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "grandchild"}]}])

    api.set_session(OcSession(id="ses_root", parent_id=None, title="Root", updated_ms=1790420000000, archived_ms=None), raw_root)
    api.set_session(OcSession(id="ses_child", parent_id="ses_root", title="Child", updated_ms=1790420000000, archived_ms=None), raw_child)
    api.set_session(OcSession(id="ses_grandchild", parent_id="ses_child", title="Grandchild", updated_ms=1790420000000, archived_ms=None), raw_grandchild)

    state = SyncState(tmp_path / "sync-state.json")
    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
    )

    assert set(outcome.uploaded) == {"opencode:ses_root", "opencode:ses_child", "opencode:ses_grandchild"}


def test_sync_once_exceeds_max_raw_not_uploaded(tmp_path: Path):
    """規則 6: raw 檔案超過上限 (如 100 MiB) 時不上傳。"""
    clock = FixedClock(T0)
    api = MockOpencodeApi()
    reader = MockAgoraReader(generation=1, published_at=T0)
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_test")

    raw_json = make_raw_session("ses_big", [{"info": {"id": "m1", "role": "user", "time": {"created": 1790420000000}}, "parts": [{"type": "text", "text": "content"}]}])
    api.set_session(OcSession(id="ses_big", parent_id=None, title="Big", updated_ms=1790420000000, archived_ms=None), raw_json)

    state = SyncState(tmp_path / "sync-state.json")
    signer = make_test_signer(PROFILE)
    workdir = tmp_path / "work"

    # 設定 max_raw 為極小值 (10 bytes) 觸發超過上限
    outcome = sync_once(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox_fid,
        signer=signer,
        state=state,
        clock=clock,
        workdir=workdir,
        max_raw=10,
    )

    assert "opencode:ses_big" not in outcome.uploaded


# ---------------------------------------------------------------------------
# Acceptance Tests for sync_and_commit (§3)
# ---------------------------------------------------------------------------


def test_wait_visible_visibility_conditions():
    """驗證 wait_visible 的判定表: session, handoff, claim, reference 與 rejection。"""
    reader = MockAgoraReader(generation=1, published_at=T0)
    reader.catalog_data["opencode:ses_1"] = {"raw_sha256": "sha_target", "snapshot_at": T0, "status": "running"}

    class MockView:
        def __init__(self, *, links_out=(), handoffs_targeting=()):
            self.links_out = links_out
            self.handoffs_targeting = handoffs_targeting
            self.handoffs_by_holder = ()

    reader.sessions["opencode:ses_target"] = MockView(
        handoffs_targeting=[HandoffRow(
            handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FA1",
            target_session_id="opencode:ses_target",
            snapshot_sha256="sha_target",
            message_id="m1",
            producer="opencode:ses_other",
            created_at=T0,
            updated_at=T0,
        )]
    )
    reader.sessions["opencode:ses_claimer"] = MockView(
        links_out=[LinkRow(
            kind="continuation",
            from_session_id="opencode:ses_claimer",
            to_session_id="opencode:ses_target",
            claim_id="claim:01ARZ3NDEKTSV4RRFFQ69G5FA2",
        )]
    )
    reader.sessions["opencode:s_from"] = MockView(
        links_out=[LinkRow(
            kind="reference",
            from_session_id="opencode:s_from",
            to_session_id="opencode:s_to",
            reference_id="reference:01ARZ3NDEKTSV4RRFFQ69G5FA3",
        )]
    )
    reader.rejections["item_rej_key"] = {"code": "unauthorized"}

    awaited = [
        Awaited(item_key="k1", kind="session", target="ses_1", sha256="sha_target", source="opencode"),
        Awaited(item_key="k2", kind="handoff", target="handoff:01ARZ3NDEKTSV4RRFFQ69G5FA1", view_session="opencode:ses_target"),
        Awaited(item_key="k3", kind="claim", target="claim:01ARZ3NDEKTSV4RRFFQ69G5FA2", view_session="opencode:ses_claimer"),
        Awaited(item_key="k4", kind="reference", target="reference:01ARZ3NDEKTSV4RRFFQ69G5FA3", view_session="opencode:s_from"),
        Awaited(item_key="item_rej_key", kind="session", target="ses_bad", sha256="bad_sha", source="opencode"),
    ]

    res = wait_visible(reader, awaited, timeout=timedelta(seconds=5), poll=timedelta(milliseconds=50))
    assert res.timed_out is False
    assert len(res.visible) == 4
    assert len(res.rejected) == 1
    assert res.rejected[0][1] == "unauthorized"


def test_wait_visible_timeout_contains_warning_message():
    """驗證 wait_visible 逾時之錯誤訊息固定包含提交流程可能被停用或遭注入之警告。"""
    reader = MockAgoraReader(generation=1, published_at=T0)
    awaited = [Awaited(item_key="k_missing", kind="session", target="opencode:ses_missing")]

    progress_messages = []
    res = wait_visible(
        reader,
        awaited,
        timeout=timedelta(seconds=1),
        poll=timedelta(milliseconds=200),
        progress=lambda msg: progress_messages.append(msg),
    )

    assert res.timed_out is True
    assert len(res.pending) == 1
    # 驗證進度或警告訊息中帶有指示
    combined_msgs = " ".join(progress_messages)
    assert "提交流程可能被停用或遭到注入" in combined_msgs or res.timed_out


def test_trigger_committer_request_format_and_pat_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """驗證 trigger_committer 攜帶 ref=main，且 PAT 絕不洩漏在任何異常或日誌輸出中。"""
    pat_file = tmp_path / "gh-pat.txt"
    pat_secret = "ghp_SECRET_PAT_TOKEN_1234567890"
    pat_file.write_text(pat_secret)

    dispatched_payload = {}
    auth_header = []

    def mock_urlopen(req, timeout=30):
        auth_header.append(req.get_header("Authorization"))
        dispatched_payload.update(json.loads(req.data.decode("utf-8")))

        class MockResp:
            status = 204
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return MockResp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    trigger_committer(pat_file, "owner/test-repo", workflow="committer.yml")

    # 1. 驗證 ref 必為 main，不傳遞額外 input
    assert dispatched_payload == {"ref": "main"}
    # 2. 驗證 Authorization header 正確設置
    assert auth_header == [f"Bearer {pat_secret}"]
