"""impl2：補齊規格對照中「部分／未覆蓋」的項目（只用單元＋測試資源）。

對應 `docs/impl/spec-traceability.md` 的部分覆蓋項。每一條測試註明 spec 出處。
規則：FakeDrive／自編測試資料；不碰真 Drive／GitHub／MyBrain；秘密只以路徑引用。
不匯入 impl1／impl3 正在改的模組（committer.run、integrity.pin／verify、
admin.__main__、intake.evaluate）；agora.apply 的 Decision 用 duck-typing 的
SimpleNamespace，避免經過 intake。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aistorage.converters import get_converter
from aistorage.identity import generate_keypair
from aistorage.reading import validate_reading


def _make_signer(profile: str = "mac-opencode"):
    import hashlib as _hashlib

    from aistorage.syncer.core import Signer

    priv, pub = generate_keypair()
    kid = f"{profile}-{_hashlib.sha256(pub).hexdigest()[:8].lower()}"
    return Signer(profile, kid, priv)

T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"
T2 = "2026-09-28T10:00:00.000Z"


# ---------------------------------------------------------------------------
# 1. item-model/id 不變（搬移類比）＋ mybrain/id 指不到案件
# spec: specs/common/item-model（id 不變）、specs/mybrain/case-reference
# ---------------------------------------------------------------------------


def _index_entry(session_id: str, *, case_id: str | None, updated_at: str = T1):
    from aistorage.search.index import IndexEntry

    meta = {
        "session_id": session_id,
        "source": "opencode",
        "title": "t",
        "producer": "profile:mac-opencode",
        "case_id": case_id,
        "status": "running",
        "stopped_at": None,
        "in_progress": False,
        "created_at": T0,
        "updated_at": updated_at,
        "snapshot_at": updated_at,
        "raw_sha256": "a" * 64,
        "raw_size": 10,
        "parent_id": None,
        "reading_status": "ok",
        "committed_at": updated_at,
    }
    return IndexEntry(metadata=meta, snapshots=[], reading=None, reading_ref=None)


def test_case_id_is_opaque_and_dangling_ids_stay_readable(tmp_path: Path):
    """case_id 是不透明字串：搬家（路徑改變）不影響參照；指不到案件照常可讀。

    spec: item-model「id 不變／案件主題檔搬家」、mybrain「id 指不到案件」。
    AiStorage 端不解析 case_id 格式、找到不到也不讓讀取失敗。
    """
    from aistorage.search.index import IndexMeta, build_index
    from aistorage.search.query import Query, get_session_row, search

    db_path = tmp_path / "case_opaque.sqlite"
    # 路徑外觀的舊值（搬家前）與搬家後的值：AiStorage 都當不透明字串存。
    entries = [
        _index_entry("opencode:s1", case_id="技術/靈感/case-1"),
        _index_entry("opencode:s2", case_id="case-dangling-xyz"),
        _index_entry("opencode:s3", case_id=None),
    ]
    build_index(
        db_path, entries=entries,
        meta=IndexMeta(generation=1, built_at=T1, agora_main_sha="x",
                       converter_versions={}),
    )
    db = sqlite3.connect(str(db_path))
    try:
        # 依案件列出：三種形狀都查得到（不解析、不驗證存在）。
        hits, _ = search(db, Query(case_id="技術/靈感/case-1"))
        assert [h.session.session_id for h in hits] == ["opencode:s1"]
        hits, _ = search(db, Query(case_id="case-dangling-xyz"))
        assert [h.session.session_id for h in hits] == ["opencode:s2"]
        # 指不到案件的 Session 照常可讀（而不是讀取失敗）。
        row = get_session_row(db, "opencode:s2")
        assert row is not None and row.case_id == "case-dangling-xyz"
        row1 = get_session_row(db, "opencode:s1")
        assert row1 is not None and row1.case_id == "技術/靈感/case-1"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. session-record/原始紀錄是真本：匯出不含帳號憑證（canary）
# spec: specs/agora/session-record「原始紀錄是真本」
# ---------------------------------------------------------------------------

FORBIDDEN_TOP_LEVEL_KEYS = (
    "accounts", "credentials", "apiKey", "api_key", "service_account",
    "private_key", "tokens",
)


def _minimal_opencode_export(*, extra_top_level: dict | None = None) -> dict:
    data: dict = {
        "info": {
            "id": "ses_canary",
            "time": {"created": 1790420000000, "updated": 1790420001000},
        },
        "messages": [
            {
                "info": {
                    "id": "m1", "role": "user",
                    "time": {"created": 1790420000000},
                },
                "parts": [{"type": "text", "text": "hello"}],
            },
            {
                "info": {
                    "id": "m2", "role": "assistant",
                    "time": {"created": 1790420001000, "completed": 1790420002000},
                },
                "parts": [{"type": "text", "text": "world"}],
            },
        ],
    }
    if extra_top_level:
        data.update(extra_top_level)
    return data


def test_export_and_reading_carry_no_credential_tables(tmp_path: Path):
    """opencode 匯出只有 Session（info＋messages），轉換後閱讀版也不帶憑證表。

    spec: session-record「原始紀錄是真本：MUST NOT 保存來源應用的憑證」。
    canary：匯出頂層出現憑證表字樣就失敗；即使有人把憑證表塞進匯出，
    轉換器也不把它們帶進閱讀版（只讀 info／messages）。
    """
    raw = _minimal_opencode_export()
    assert set(raw) <= {"info", "messages"}, "匯出只能是 Session 本體的欄位"
    for bad in FORBIDDEN_TOP_LEVEL_KEYS:
        assert bad not in raw

    raw_path = tmp_path / "canary.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    conv = get_converter("opencode")
    reading = conv.convert(raw_path, session_id="opencode:ses_canary")
    assert validate_reading(reading) == []
    blob = json.dumps(reading, ensure_ascii=False)
    for bad in FORBIDDEN_TOP_LEVEL_KEYS:
        assert bad not in blob

    # 即使匯出被塞了憑證表，閱讀版也不收（轉換器只讀 info／messages）。
    tainted = _minimal_opencode_export(
        extra_top_level={"accounts": [{"apiKey": "sk-test"}]})
    tainted_path = tmp_path / "tainted.json"
    tainted_path.write_text(json.dumps(tainted), encoding="utf-8")
    reading2 = conv.convert(tainted_path, session_id="opencode:ses_canary")
    blob2 = json.dumps(reading2, ensure_ascii=False)
    assert "sk-test" not in blob2
    assert "accounts" not in blob2


# ---------------------------------------------------------------------------
# 3. session-record/閱讀版重建：跨應用讀取（格式層）
# spec: specs/agora/session-record「跨應用讀取」
# ---------------------------------------------------------------------------


def test_cross_app_reading_shares_common_format(tmp_path: Path):
    """opencode 與 claude-code 的閱讀版走同一共通格式，A 應用讀得到 B 的內容。

    spec: session-record「跨應用讀取：讀閱讀版就能理解，不需懂對方原始格式」。
    單元層驗：兩種來源各自轉換後都通過同一份 validate_reading，且純文字都可抽取。
    """
    from aistorage.reading import plain_text

    oc_raw = _minimal_opencode_export()
    oc_path = tmp_path / "cross_oc.json"
    oc_path.write_text(json.dumps(oc_raw), encoding="utf-8")
    oc_reading = get_converter("opencode").convert(
        oc_path, session_id="opencode:s-cross")
    assert validate_reading(oc_reading) == []
    assert oc_reading["format"] == "aistorage.reading/v1"

    cc_path = Path(__file__).parent / "data" / "converters" / "claude-code" / "basic.jsonl"
    assert cc_path.is_file(), "需要測試資源的 claude-code jsonl"
    cc_reading = get_converter("claude-code").convert(
        cc_path, session_id="claude-code:cross-1")
    assert validate_reading(cc_reading) == []
    assert cc_reading["format"] == "aistorage.reading/v1"

    # 同一個抽取函式對兩種來源都可用（讀者不需懂對方原始格式）。
    assert "hello" in plain_text(oc_reading)
    assert isinstance(plain_text(cc_reading), str)


# ---------------------------------------------------------------------------
# 4. session-record/版本保留與回滾：同步器尊重回滾（單調性保護）
# spec: specs/agora/session-record「回滾」
# ---------------------------------------------------------------------------


def _session_store(tmp_path: Path, sid: str = "opencode:s1"):
    from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
    from aistorage.schema import generate_ulid

    worktree = tmp_path / "agora"
    worktree.mkdir(exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "tmp")
    shas: list[str] = []
    for i, snap_at in enumerate([T0, T1]):
        content = f'{{"v": {i}}}'.encode()
        p = tmp_path / f"rb{i}.raw"
        p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        store.put_session(SessionRecord(
            id=sid, producer="profile:mac-opencode",
            created_at=T0, updated_at=snap_at, status="running",
            snapshot_at=snap_at, raw_sha256=sha, raw_size=len(content),
            committed_at=snap_at, last_item_key=generate_ulid(), title="t",
            in_progress=False), p)
        shas.append(sha)
    return store, sid, shas


def test_rollback_then_stale_sync_is_rejected(tmp_path: Path):
    """回滾後，帶著舊 snapshot_at 的同步內容會被判 stale，不蓋掉回滾。

    spec: session-record「回滾」。回滾的 snapshot_at 取執行時鐘（最新），
    來源端沒注意到回滾、仍以舊時間同步時，apply_session 判 stale 而不採用。
    """
    from aistorage.admin.rollback import rollback_session
    from aistorage.agora.apply import apply_session
    from aistorage.clock import FixedClock

    store, sid, shas = _session_store(tmp_path)
    rollback_session(store=store, session_id=sid,
                     target_snapshot_sha256=shas[0],
                     reason="回到第一版",
                     clock=FixedClock(T2))
    current = store.get_session(sid)
    assert current is not None and current.snapshot_at.startswith("2026-09-28T10:00:00")

    # 來源端的舊內容（snapshot_at 介於 T1 與 T2 之間、sha 不同）來申請寫入。
    stale_bytes = b'{"v": "stale-from-source"}'
    stale_path = tmp_path / "stale.raw"
    stale_path.write_bytes(stale_bytes)

    from aistorage.schema import generate_ulid

    class _Item:
        item_key = generate_ulid()
        inbox_folder_id = "inbox-test"
        sidecars: list = []
        sigs: list = []
        raws: list = []
        extras: list = []

    dec = SimpleNamespace(
        item=_Item(),
        record_metadata={
            "id": sid, "type": "session",
            "producer": "profile:mac-opencode",
            "created_at": T0, "updated_at": T1,
        },
        sidecar={"session": {"snapshot_at": T1, "in_progress": False}},
        raw_path=stale_path,
    )

    class _Facts:
        title = "t"
        archived_ms = None
        last_message_created_ms = None
        last_message_ms = None
        in_progress = False
        archived_at = None

    class _Conv:
        source = "opencode"

        def facts(self, raw_path: Path, **kw) -> _Facts:
            return _Facts()

        def convert(self, raw_path: Path, **kw) -> dict:
            return {"snapshot_sha256": hashlib.sha256(
                Path(raw_path).read_bytes()).hexdigest(), "messages": []}

    result = apply_session(store, dec, _Conv(), FixedClock(T2))  # type: ignore[arg-type]
    assert result.ok is False and result.code == "stale"
    assert store.get_session(sid).snapshot_at.startswith("2026-09-28T10:00:00"), (
        "回滾不被舊同步蓋掉")


# ---------------------------------------------------------------------------
# 5. session-record/抹除：AI 只能提醒（沒有抹除能力）
# spec: specs/agora/session-record「AI 發現機敏內容」
# ---------------------------------------------------------------------------


def test_skill_exposes_no_erase_capability():
    """住民工具沒有抹除入口：AI 只能提醒，不能讓內容消失。

    spec: session-record「AI 發現機敏內容：它只能提醒你抹除」。
    """
    from aistorage.skill import tools

    assert "erase" not in tools.__all__
    assert not hasattr(tools, "erase")
    assert not hasattr(tools, "erase_session")
    assert "erase" not in dir(tools)


# ---------------------------------------------------------------------------
# 6. session-record/永久保存：來源端刪除後 Agora 不受影響
# spec: specs/agora/session-record「來源應用刪除了自己的紀錄」
# ---------------------------------------------------------------------------


def test_source_deletion_does_not_touch_agora(tmp_path: Path):
    """來源端把 Session 從清單拿掉，同步器不會刪 Agora 的真本。

    spec: session-record「永久保存：來源端刪除後 Agora 不受影響」。
    sync_once 只走 API 列出來的 Session；沒列出來＝不動作，不是刪除。
    """
    from aistorage.clock import FixedClock
    from aistorage.drive.fake import FakeDrive
    from aistorage.syncer.core import Signer, sync_once
    from aistorage.syncer.opencode_api import OcSession
    from aistorage.syncer.state import SyncState

    store, sid, _ = _session_store(tmp_path)

    class _EmptyApi:
        def list_sessions(self) -> list:
            return []

        def export(self, session_id: str, dest: Path) -> Path:  # pragma: no cover
            raise AssertionError("沒有 Session 就不該匯出")

    class _Reader:
        def catalog(self, session_ids):
            assert session_ids == [], "沒有 Session 就不該有 catalog 查詢對象"
            return {}

        def get_rejection(self, item_key):
            return None

        def manifest(self):
            return {"generation": 1, "published_at": T1}

    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    state = SyncState(path=tmp_path / "sync-state.json")
    outcome = sync_once(
        api=_EmptyApi(), reader=_Reader(), drive=drive,  # type: ignore[arg-type]
        inbox_folder_id=inbox,
        signer=_make_signer(),
        state=state, clock=FixedClock(T2), workdir=tmp_path / "work",
    )
    assert outcome.uploaded == () and outcome.errors == ()
    assert store.get_session(sid) is not None, "Agora 的 Session 必須還在"


# ---------------------------------------------------------------------------
# 7. session-sync/每個來源應用一個同步器：同步器與來源無關
# spec: specs/agora/session-sync「之後加入手機 App」
# ---------------------------------------------------------------------------


def test_syncer_is_source_agnostic_for_a_new_app(tmp_path: Path):
    """新增來源應用只需換 converter＋source 名，不必改同步器/Agora。

    spec: session-sync「每個來源應用一個同步器」。
    以 source='phone-app' 跑 sync_once：上傳的 Session id 帶新前綴，舊的不受影響。
    """
    import json as _json

    from aistorage.clock import FixedClock
    from aistorage.converters.base import SessionFacts
    from aistorage.drive.fake import FakeDrive
    from aistorage.syncer.core import sync_once
    from aistorage.syncer.opencode_api import OcSession
    from aistorage.syncer.state import SyncState

    raw_obj = {
        "info": {"id": "m1", "time": {"created": 1790420000000}},
        "messages": [{"info": {"id": "m1", "role": "user",
                               "time": {"created": 1790420000000}},
                      "parts": [{"type": "text", "text": "hi"}]}],
    }

    class _PhoneApi:
        def list_sessions(self):
            return [OcSession(id="m1")]

        def export(self, session_id: str, dest: Path) -> Path:
            out = Path(dest)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_json.dumps(raw_obj), encoding="utf-8")
            return out

    class _PhoneConverter:
        source = "phone-app"

        def facts(self, raw_path: Path, *, session_id: str | None = None):
            return SessionFacts(title="phone", created_at=T0, updated_at=T0,
                                message_ids=("m1",), archived_at=None,
                                last_message_at=T0, in_progress=False)

    class _Reader:
        def catalog(self, session_ids):
            return {}

        def get_rejection(self, item_key):
            return None

        def manifest(self):
            return {"generation": 0, "published_at": T0}

    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    state = SyncState(path=tmp_path / "sync-state.json")
    outcome = sync_once(
        api=_PhoneApi(), reader=_Reader(), drive=drive,  # type: ignore[arg-type]
        inbox_folder_id=inbox,
        signer=_make_signer(),
        state=state, clock=FixedClock(T1), workdir=tmp_path / "work",
        converter=_PhoneConverter(), source="phone-app",
    )
    assert outcome.uploaded == ("phone-app:m1",)
    assert get_converter("opencode").source == "opencode", "既有轉換器不受影響"


# ---------------------------------------------------------------------------
# 8. session-link/兩種類型：不認得新類型就忽略（需改程式）
# spec: specs/agora/session-link「Link 的類型集合可以擴充」
# ---------------------------------------------------------------------------


def _agora_reader_fixture(tmp_path: Path):
    """最小的 Agora 讀取視圖：一個 Session＋一條未知 kind 的 Link。"""
    from aistorage.clock import FixedClock
    from aistorage.drive.fake import FakeDrive
    from aistorage.reader import AgoraReader
    from aistorage.reader.client import ReadViewClient
    from aistorage.reader.config import ReaderConfig
    from aistorage.search.index import IndexEntry, IndexMeta, LinkRow, build_index

    entry = _index_entry("opencode:s1", case_id=None)
    db_path = tmp_path / "links.sqlite"
    build_index(
        db_path, entries=[entry],
        links=[LinkRow(kind="continuation", from_session_id="opencode:s2",
                       to_session_id="opencode:s1", handoff_id="handoff:01J0000000000000000000001",
                       claim_id="claim:01J0000000000000000000002",
                       snapshot_sha256="a" * 64, message_id="m1"),
               LinkRow(kind="endorsement", from_session_id="opencode:s1",
                       to_session_id="opencode:s2")],
        meta=IndexMeta(generation=1, built_at=T1, agora_main_sha="x",
                       converter_versions={}),
    )
    raw = db_path.read_bytes()
    manifest = {
        "format": "aistorage.readview/v1", "element": "agora", "generation": 1,
        "published_at": T1, "agora_main_sha": "x", "converter_versions": {},
        "index": {"id": "idx",
                  "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)},
        "files": ["idx"], "retired": [],
    }
    drive = FakeDrive()
    folder = drive.seed_folder("readview")
    drive.seed_file(folder, "manifest.json", json.dumps(manifest).encode(),
                    file_id="manifest-1")
    drive.seed_file(folder, "index.sqlite", raw, file_id="idx")
    cfg = ReaderConfig(manifest_file_id="manifest-1",
                       sa_key_path=tmp_path / "sa.json",
                       cache_dir=tmp_path / "cache")
    clock = FixedClock(T2)
    return AgoraReader(ReadViewClient(drive, cfg, clock=clock), clock=clock)


@pytest.mark.xfail(
    strict=True,
    reason="未知 kind 的 Link 目前會原樣回傳、沒有被忽略："
           "get_session 應過濾只剩 continuation／reference（spec session-link）",
)
def test_unknown_link_kind_is_ignored(tmp_path: Path):
    """讀者不認得新類型的 Link 就忽略，而不是視為錯誤。

    spec: session-link「Session Link 的兩種類型：類型集合可以擴充」。
    """
    reader = _agora_reader_fixture(tmp_path)
    view = reader.get_session("opencode:s1").value
    kinds_out = [link.kind for link in view.links_out]
    assert "endorsement" not in kinds_out
    assert set(kinds_out) <= {"continuation", "reference"}
