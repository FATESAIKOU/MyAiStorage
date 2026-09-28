"""Smoke and unit tests for intake module (scan, evaluate, ledger)."""

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import pytest

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, FakeRawStorage
from aistorage.clock import FixedClock, format_rfc3339
from aistorage.drive.fake import FakeDrive
from aistorage.drive.model import DriveFile
from aistorage.errors import MismatchError, ReadError
from aistorage.identity import Registry, generate_keypair, validate_registry
from aistorage.inbox import sign_sidecar_bytes
from aistorage.intake import (
    LEDGER_CODES,
    Decision,
    DecisionKind,
    InboxItem,
    InboxScan,
    Ledger,
    count_shaped,
    evaluate,
    is_actionable,
    is_item_key_too_old,
    parse_ulid_timestamp_ms,
    scan_inboxes,
    sort_accepted_decisions,
    stamp_record,
    strict_json,
)
from aistorage.intake.evaluate import _check_reference_monotonicity
from aistorage.schema import generate_ulid


def test_scan_and_shaping_smoke():
    """測試收件匣掃描分組與 is_actionable / count_shaped 計數。"""
    drive = FakeDrive()
    inbox_fid = drive.seed_folder("inbox_worker1")

    ulid1 = generate_ulid()
    ulid2 = generate_ulid()

    # 1. 完整項目 1：sidecar, sig, raw
    raw1_bytes = b"hello raw 1"
    f1_raw = drive.seed_file(inbox_fid, f"{ulid1}.raw", raw1_bytes)
    f1_sidecar = drive.seed_file(inbox_fid, f"{ulid1}.sidecar.json", b"{}")
    f1_sig = drive.seed_file(inbox_fid, f"{ulid1}.sig", b"{}")

    # 2. 缺 sig 項目 2：只有 sidecar 與 raw
    f2_sidecar = drive.seed_file(inbox_fid, f"{ulid2}.sidecar.json", b"{}")
    f2_raw = drive.seed_file(inbox_fid, f"{ulid2}.raw", b"hello raw 2")

    # 3. 額外雜項檔案
    f_orphan = drive.seed_file(inbox_fid, "orphan.txt", b"junk")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [],
            }
        },
    }
    registry = Registry(registry_data)

    items = scan_inboxes(drive, registry)
    assert len(items) == 2
    assert len(items.items) == 2
    assert len(items.junk) == 1
    assert items.junk[0].name == "orphan.txt"

    items_by_key = {it.item_key: it for it in items}
    it1 = items_by_key[ulid1]
    assert is_actionable(it1) is True
    assert it1.sidecar is not None and it1.sig is not None and it1.raw is not None
    assert it1.extras == ()

    it2 = items_by_key[ulid2]
    assert is_actionable(it2) is False
    assert it2.sig is None and it2.sidecar is not None and it2.raw is not None

    assert count_shaped(items) == 1


def test_ledger_smoke(tmp_path: Path):
    """測試 Ledger 記錄、查詢與 ULID 過期判定。"""
    store_dir = tmp_path / "agora_store"
    store = AgoraStore(store_dir, raw_storage=FakeRawStorage())
    ledger = Ledger(store)

    ulid = generate_ulid()
    assert ledger.contains(ulid) is None

    # 寫入記錄
    ledger.record(
        ulid,
        item_id="opencode:ses_001",
        decision="accept",
        raw_sha256="abc123sha",
        at="2026-09-27T08:00:00Z",
    )

    entry = ledger.contains(ulid)
    assert entry is not None
    assert entry.item_key == ulid
    assert entry.item_id == "opencode:ses_001"
    assert entry.decision == "accept"
    assert entry.raw_sha256 == "abc123sha"
    assert entry.at == "2026-09-27T08:00:00Z"

    # 檢查實體檔案產生
    ledger_file = store_dir / "_committer" / "ledger" / "2026-09.jsonl"
    assert ledger_file.is_file()

    # 重新實例化可正確重載
    ledger2 = Ledger(store)
    entry2 = ledger2.contains(ulid)
    assert entry2 == entry

    # ULID 年齡判定
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
    # 建立 100 天前與 10 天前的 ULID
    now_ms = int(now.timestamp() * 1000)
    old_ms = now_ms - (100 * 86400 * 1000)
    recent_ms = now_ms - (10 * 86400 * 1000)

    old_ulid = generate_ulid(old_ms)
    recent_ulid = generate_ulid(recent_ms)

    assert is_item_key_too_old(old_ulid, now, retention_days=90) is True
    assert is_item_key_too_old(recent_ulid, now, retention_days=90) is False


def test_stamp_record_smoke():
    """測試 stamp_record 唯一蓋章入口。"""
    inbox_meta = {
        "id": "opencode:ses_test",
        "type": "session",
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "producer": "fake_producer",  # 應被剝除
    }

    stamped = stamp_record(inbox_meta, producer="profile:worker-1")
    assert stamped["producer"] == "profile:worker-1"
    assert stamped["case_id"] is None
    assert stamped["provenance"] is None
    assert stamped["id"] == "opencode:ses_test"

    # 非法 metadata 拋出 ValueError
    with pytest.raises(ValueError):
        stamp_record({"id": "no_type"}, producer="profile:worker-1")


def test_strict_json_smoke():
    """測試 strict_json 拒絕重複鍵名。"""
    valid_bytes = b'{"a": 1, "b": 2}'
    assert strict_json(valid_bytes) == {"a": 1, "b": 2}

    dup_bytes = b'{"a": 1, "a": 2}'
    with pytest.raises(ValueError, match="重複的鍵名"):
        strict_json(dup_bytes)


def _build_test_session_sidecar(
    item_key: str,
    raw_content: bytes,
    *,
    profile: str = "worker-1",
    session_id: str = "ses_12345",
    snapshot_at: str = "2026-09-27T08:00:00Z",
    created_at: str = "2026-09-27T08:00:00Z",
    updated_at: str = "2026-09-27T08:00:00Z",
) -> dict:
    raw_sha = hashlib.sha256(raw_content).hexdigest().lower()
    return {
        "format": "aistorage.inbox/v1",
        "item_key": item_key,
        "profile": profile,
        "metadata": {
            "id": f"opencode:{session_id}",
            "type": "session",
            "created_at": created_at,
            "updated_at": updated_at,
            "case_id": None,
            "provenance": None,
        },
        "raw": {
            "sha256": raw_sha,
            "size": len(raw_content),
        },
        "session": {
            "source": "opencode",
            "source_session_id": session_id,
            "snapshot_at": snapshot_at,
            "status": "running",
            "stopped_at": None,
            "in_progress": False,
            "parent_id": None,
        },
        "body": {},
    }


def test_evaluate_pipeline_smoke(tmp_path: Path):
    """測試 evaluate 評估流水線：驗章、授權、格式、防重放、dedup、artifact DEFER。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    # 建立簽章金鑰與 Registry
    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session", "artifact", "claim", "reference"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)

    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    # 1. 缺 sig：未滿 24 小時 -> DEFER(orphan)
    ulid_orphan1 = generate_ulid()
    f_sidecar_recent = drive.seed_file(
        inbox_fid,
        f"{ulid_orphan1}.sidecar.json",
        b"{}",
        created_time="2026-09-27T08:00:00Z",  # 2 小時前
    )
    item_defer = InboxItem(
        item_key=ulid_orphan1,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sidecar_recent),
        sig=None,
        raw=None,
    )
    d_defer = evaluate(item_defer, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_defer.kind == DecisionKind.DEFER
    assert d_defer.code == "incomplete"

    # 2. 缺 sig：超過 24 小時 -> REJECT(orphan)
    ulid_orphan2 = generate_ulid()
    f_sidecar_old = drive.seed_file(
        inbox_fid,
        f"{ulid_orphan2}.sidecar.json",
        b"{}",
        created_time="2026-09-26T08:00:00Z",  # 26 小時前
    )
    item_reject_orphan = InboxItem(
        item_key=ulid_orphan2,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sidecar_old),
        sig=None,
        raw=None,
    )
    d_reject = evaluate(item_reject_orphan, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_reject.kind == DecisionKind.REJECT
    assert d_reject.code == "orphan"

    # 3. 正常 session 項目
    ulid_ok = generate_ulid()
    raw_ok_bytes = b'{"msg": "valid session"}'
    sidecar_ok = _build_test_session_sidecar(ulid_ok, raw_ok_bytes)
    sidecar_ok_bytes = json.dumps(sidecar_ok).encode("utf-8")
    sig_ok = sign_sidecar_bytes(sidecar_ok_bytes, priv_bytes, key_id)
    sig_ok_bytes = json.dumps(sig_ok).encode("utf-8")

    f_sc_ok = drive.seed_file(inbox_fid, f"{ulid_ok}.sidecar.json", sidecar_ok_bytes, created_time="2026-09-27T08:30:00Z")
    f_sig_ok = drive.seed_file(inbox_fid, f"{ulid_ok}.sig", sig_ok_bytes, created_time="2026-09-27T08:30:00Z")
    f_raw_ok = drive.seed_file(inbox_fid, f"{ulid_ok}.raw", raw_ok_bytes, created_time="2026-09-27T08:30:00Z")

    item_ok = InboxItem(
        item_key=ulid_ok,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sc_ok),
        sig=drive.get(f_sig_ok),
        raw=drive.get(f_raw_ok),
    )
    d_ok = evaluate(item_ok, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_ok.kind == DecisionKind.ACCEPT
    assert d_ok.code == "ok"
    assert d_ok.producer == "profile:worker-1"
    assert d_ok.record_metadata is not None
    assert d_ok.record_metadata["producer"] == "profile:worker-1"
    assert d_ok.raw_path is not None
    assert d_ok.raw_path.read_bytes() == raw_ok_bytes

    # 4. 簽章偽造或錯誤 -> REJECT(bad_signature)
    ulid_bad_sig = generate_ulid()
    bad_sig_dict = {"alg": "ed25519", "key_id": key_id, "value": "A" * 86 + "=="}
    f_sc_badsig = drive.seed_file(inbox_fid, f"{ulid_bad_sig}.sidecar.json", sidecar_ok_bytes)
    f_sig_badsig = drive.seed_file(inbox_fid, f"{ulid_bad_sig}.sig", json.dumps(bad_sig_dict).encode("utf-8"))
    f_raw_badsig = drive.seed_file(inbox_fid, f"{ulid_bad_sig}.raw", raw_ok_bytes)
    item_badsig = InboxItem(
        item_key=ulid_bad_sig,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sc_badsig),
        sig=drive.get(f_sig_badsig),
        raw=drive.get(f_raw_badsig),
    )
    d_badsig = evaluate(item_badsig, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_badsig.kind == DecisionKind.REJECT
    assert d_badsig.code == "bad_signature"

    # 5. artifact 項目 -> Foundry 未設定時 REJECT(foundry_not_enabled)
    ulid_art = generate_ulid()
    art_sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid_art,
        "profile": "worker-1",
        "metadata": {
            "id": f"artifact:{ulid_art}",
            "type": "artifact",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "raw": None,
        "body": {
            "kind": "link",
            "produced_by_session_id": "opencode:ses_12345",
            "content_type": "application/octet-stream",
            "link": "annex:SHA256E-s100--abc",
        },
    }
    art_sc_bytes = json.dumps(art_sidecar).encode("utf-8")
    art_sig = sign_sidecar_bytes(art_sc_bytes, priv_bytes, key_id)
    f_art_sc = drive.seed_file(inbox_fid, f"{ulid_art}.sidecar.json", art_sc_bytes)
    f_art_sig = drive.seed_file(inbox_fid, f"{ulid_art}.sig", json.dumps(art_sig).encode("utf-8"))
    item_art = InboxItem(
        item_key=ulid_art,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_art_sc),
        sig=drive.get(f_art_sig),
        raw=None,
    )
    d_art = evaluate(item_art, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    # Foundry 沒設定 → REJECT（PM 指示）：DEFER 會讓項目永遠留在收件匣裡
    assert d_art.kind == DecisionKind.REJECT
    assert d_art.code == "foundry_not_enabled"

    # 6. 重放檢核（Ledger 已有記錄）：同 raw sha -> ALREADY；不同 raw sha -> REJECT(replayed_item_key)
    # 記錄剛才的 item_ok
    raw_sha = hashlib.sha256(raw_ok_bytes).hexdigest().lower()
    ledger.record(ulid_ok, item_id="opencode:ses_12345", decision="accept", raw_sha256=raw_sha, at="2026-09-27T09:00:00Z")
    d_replay_same = evaluate(item_ok, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_replay_same.kind == DecisionKind.ALREADY
    assert d_replay_same.code == "already"

    # 模擬 ledger 有不同 raw_sha
    ulid_diff = generate_ulid()
    ledger.record(ulid_diff, item_id="opencode:ses_12345", decision="accept", raw_sha256="different_sha", at="2026-09-27T09:00:00Z")
    sidecar_diff = _build_test_session_sidecar(ulid_diff, raw_ok_bytes)
    diff_bytes = json.dumps(sidecar_diff).encode("utf-8")
    diff_sig = sign_sidecar_bytes(diff_bytes, priv_bytes, key_id)
    f_diff_sc = drive.seed_file(inbox_fid, f"{ulid_diff}.sidecar.json", diff_bytes)
    f_diff_sig = drive.seed_file(inbox_fid, f"{ulid_diff}.sig", json.dumps(diff_sig).encode("utf-8"))
    f_diff_raw = drive.seed_file(inbox_fid, f"{ulid_diff}.raw", raw_ok_bytes)
    item_diff = InboxItem(
        item_key=ulid_diff,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_diff_sc),
        sig=drive.get(f_diff_sig),
        raw=drive.get(f_diff_raw),
    )
    d_replay_diff = evaluate(item_diff, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_replay_diff.kind == DecisionKind.REJECT
    assert d_replay_diff.code == "replayed_item_key"


def test_sort_accepted_decisions_smoke():
    """測試 sort_accepted_decisions 排序：session(依時間) -> rewrite -> handoff -> claim -> reference。"""
    dummy_item = InboxItem(item_key="k", inbox_folder_id="f", sidecar=None, sig=None, raw=None)

    d_ref = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "reference", "updated_at": "2026-09-27T08:00:00Z"},
    )
    d_claim = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "claim", "updated_at": "2026-09-27T08:00:00Z"},
    )
    d_sess_late = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "session"},
        sidecar={"session": {"snapshot_at": "2026-09-27T08:30:00Z"}},
    )
    d_sess_early = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "session"},
        sidecar={"session": {"snapshot_at": "2026-09-27T08:10:00Z"}},
    )
    d_rewrite = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "rewrite", "updated_at": "2026-09-27T08:00:00Z"},
    )

    unordered = [d_ref, d_sess_late, d_claim, d_rewrite, d_sess_early]
    ordered = sort_accepted_decisions(unordered)

    assert ordered == [d_sess_early, d_sess_late, d_rewrite, d_claim, d_ref]


def test_evaluate_monotonicity_smoke(tmp_path: Path):
    """測試 evaluate 之單調性與防重放規則（session, reference, 其他）。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session", "reference", "claim"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)

    store_dir = tmp_path / "agora"
    store = AgoraStore(store_dir, raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    # 1. Session 單調性
    # 建立真本既有 Session meta.json
    sess_meta_dir = store_dir / "sessions" / "opencode" / "ses_mono"
    sess_meta_dir.mkdir(parents=True, exist_ok=True)
    raw_same = b"same raw"
    raw_same_sha = hashlib.sha256(raw_same).hexdigest().lower()
    existing_sess_meta = {
        "id": "opencode:ses_mono",
        "type": "session",
        "producer": "profile:worker-1",
        "created_at": "2026-09-27T07:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "case_id": None,
        "provenance": None,
        "status": "running",
        "snapshot_at": "2026-09-27T08:00:00Z",
        "raw_sha256": raw_same_sha,
        "raw_size": len(raw_same),
        "committed_at": "2026-09-27T08:05:00Z",
        "last_item_key": generate_ulid(),
    }
    (sess_meta_dir / "meta.json").write_text(json.dumps(existing_sess_meta), encoding="utf-8")

    # (A) 同一個 raw_sha256 -> ALREADY (dedup)
    ulid_same_sha = generate_ulid()
    sc_same = _build_test_session_sidecar(
        ulid_same_sha,
        raw_same,
        session_id="ses_mono",
        snapshot_at="2026-09-27T08:30:00Z",
    )
    sc_same["raw"]["sha256"] = raw_same_sha
    sc_same_bytes = json.dumps(sc_same).encode("utf-8")
    sig_same = sign_sidecar_bytes(sc_same_bytes, priv_bytes, key_id)
    f_sc = drive.seed_file(inbox_fid, f"{ulid_same_sha}.sidecar.json", sc_same_bytes)
    f_sig = drive.seed_file(inbox_fid, f"{ulid_same_sha}.sig", json.dumps(sig_same).encode("utf-8"))
    f_raw = drive.seed_file(inbox_fid, f"{ulid_same_sha}.raw", raw_same, sha256=raw_same_sha)
    it_same = InboxItem(item_key=ulid_same_sha, inbox_folder_id=inbox_fid, sidecar=drive.get(f_sc), sig=drive.get(f_sig), raw=drive.get(f_raw))
    d_same = evaluate(it_same, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_same.kind == DecisionKind.ALREADY

    # (B) snapshot_at <= 既有 snapshot_at -> REJECT(stale)
    ulid_stale = generate_ulid()
    raw_new = b"new raw diff sha"
    sc_stale = _build_test_session_sidecar(
        ulid_stale,
        raw_new,
        session_id="ses_mono",
        snapshot_at="2026-09-27T07:30:00Z",  # 比既有 08:00:00 舊
    )
    sc_stale_bytes = json.dumps(sc_stale).encode("utf-8")
    sig_stale = sign_sidecar_bytes(sc_stale_bytes, priv_bytes, key_id)
    f_sc2 = drive.seed_file(inbox_fid, f"{ulid_stale}.sidecar.json", sc_stale_bytes)
    f_sig2 = drive.seed_file(inbox_fid, f"{ulid_stale}.sig", json.dumps(sig_stale).encode("utf-8"))
    f_raw2 = drive.seed_file(inbox_fid, f"{ulid_stale}.raw", raw_new)
    it_stale = InboxItem(item_key=ulid_stale, inbox_folder_id=inbox_fid, sidecar=drive.get(f_sc2), sig=drive.get(f_sig2), raw=drive.get(f_raw2))
    d_stale = evaluate(it_stale, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_stale.kind == DecisionKind.REJECT
    assert d_stale.code == "stale"


def test_evaluate_edge_cases_smoke(tmp_path: Path):
    """測試 evaluate 異常與邊界條件：過期 ULID、未授權、大小超限、raw 不符。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)

    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    # 1. 100 天前的 ULID -> REJECT(too_old)
    old_ms = int((clock.now().timestamp() - 100 * 86400) * 1000)
    ulid_old = generate_ulid(old_ms)
    raw_bytes = b"sample raw"
    sc_old = _build_test_session_sidecar(ulid_old, raw_bytes)
    sc_bytes = json.dumps(sc_old).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, priv_bytes, key_id)
    f_sc = drive.seed_file(inbox_fid, f"{ulid_old}.sidecar.json", sc_bytes)
    f_sig = drive.seed_file(inbox_fid, f"{ulid_old}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = drive.seed_file(inbox_fid, f"{ulid_old}.raw", raw_bytes)
    it_old = InboxItem(item_key=ulid_old, inbox_folder_id=inbox_fid, sidecar=drive.get(f_sc), sig=drive.get(f_sig), raw=drive.get(f_raw))
    d_old = evaluate(it_old, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_old.kind == DecisionKind.REJECT
    assert d_old.code == "too_old"

    # 2. 未授權 profile (sidecar.profile != folder_profile) -> REJECT(unauthorized)
    ulid_unauth = generate_ulid()
    sc_unauth = _build_test_session_sidecar(ulid_unauth, raw_bytes, profile="worker-other")
    sc_unauth_bytes = json.dumps(sc_unauth).encode("utf-8")
    sig_unauth = sign_sidecar_bytes(sc_unauth_bytes, priv_bytes, key_id)
    f_sc_u = drive.seed_file(inbox_fid, f"{ulid_unauth}.sidecar.json", sc_unauth_bytes)
    f_sig_u = drive.seed_file(inbox_fid, f"{ulid_unauth}.sig", json.dumps(sig_unauth).encode("utf-8"))
    f_raw_u = drive.seed_file(inbox_fid, f"{ulid_unauth}.raw", raw_bytes)
    it_unauth = InboxItem(item_key=ulid_unauth, inbox_folder_id=inbox_fid, sidecar=drive.get(f_sc_u), sig=drive.get(f_sig_u), raw=drive.get(f_raw_u))
    d_unauth = evaluate(it_unauth, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_unauth.kind == DecisionKind.REJECT
    assert d_unauth.code == "unauthorized"

    # 3. raw 大小超過 max_raw -> REJECT(too_large)
    ulid_large = generate_ulid()
    raw_large = b"x" * 200
    sc_large = _build_test_session_sidecar(ulid_large, raw_large)
    sc_large_bytes = json.dumps(sc_large).encode("utf-8")
    sig_large = sign_sidecar_bytes(sc_large_bytes, priv_bytes, key_id)
    f_sc_l = drive.seed_file(inbox_fid, f"{ulid_large}.sidecar.json", sc_large_bytes)
    f_sig_l = drive.seed_file(inbox_fid, f"{ulid_large}.sig", json.dumps(sig_large).encode("utf-8"))
    f_raw_l = drive.seed_file(inbox_fid, f"{ulid_large}.raw", raw_large)
    it_large = InboxItem(item_key=ulid_large, inbox_folder_id=inbox_fid, sidecar=drive.get(f_sc_l), sig=drive.get(f_sig_l), raw=drive.get(f_raw_l))
    d_large = evaluate(it_large, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir, max_raw=100)
    assert d_large.kind == DecisionKind.REJECT
    assert d_large.code == "too_large"


def test_evaluate_sig_read_error_not_reject(tmp_path: Path):
    """H1: .sig 下載時發生 ReadError 不得被轉成 REJECT(bad_signature)，必須向上拋出。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()
    raw_bytes = b"hello raw"
    sidecar = _build_test_session_sidecar(ulid, raw_bytes)
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, priv_bytes, key_id)
    sig_bytes = json.dumps(sig).encode("utf-8")

    f_sc = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = drive.seed_file(inbox_fid, f"{ulid}.sig", sig_bytes)
    f_raw = drive.seed_file(inbox_fid, f"{ulid}.raw", raw_bytes)

    # 封裝 drive 使其在下載 sig 時注入 ReadError
    class FaultyDrive:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def download_bytes(self, file_id, max_bytes=None):
            if file_id == f_sig:
                raise ReadError("模擬 Google Drive 503 連線中斷")
            return self._inner.download_bytes(file_id, max_bytes=max_bytes)

    faulty_drive = FaultyDrive(drive)
    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sc),
        sig=drive.get(f_sig),
        raw=drive.get(f_raw),
    )

    with pytest.raises(ReadError, match="503"):
        evaluate(item, drive=faulty_drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)

    # 確保 _committer/rejections 沒有該 item_key 的拒收紀錄
    rej_file = tmp_path / "agora" / "_committer" / "rejections" / f"{ulid}.json"
    assert not rej_file.is_file()


def test_ledger_fail_closed_on_corrupted_data(tmp_path: Path):
    """H2: 清冊損毀或存在非規格檔案時必須中止（拋出 MismatchError）。"""
    store_dir = tmp_path / "agora"
    store = AgoraStore(store_dir, raw_storage=FakeRawStorage())
    ledger_dir = store_dir / "_committer" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)

    # 1. 損毀的 jsonl 行
    corrupted_file = ledger_dir / "2026-09.jsonl"
    corrupted_file.write_text(
        '{"item_key": "01ABC", "item_id": "ses_1", "decision": "ok", "at": "2026-09-27T08:00:00Z"}\n{bad json line}\n',
        encoding="utf-8",
    )

    ledger = Ledger(store)
    with pytest.raises(MismatchError, match="2026-09.jsonl 第 2 行解析失敗"):
        ledger.contains("01ABC")

    # 2. 清冊目錄包含非規格檔名
    corrupted_file.write_text('{"item_key": "01ABC", "item_id": "ses_1", "decision": "ok", "at": "2026-09-27T08:00:00Z"}\n', encoding="utf-8")
    extra_bad_file = ledger_dir / "bad_file.txt"
    extra_bad_file.write_text("junk", encoding="utf-8")

    ledger2 = Ledger(store)
    with pytest.raises(MismatchError, match="非規格檔案: bad_file.txt"):
        ledger2.contains("01ABC")


def test_evaluate_candidate_sidecars_and_sigs(tmp_path: Path):
    """M1: 同一 part 多個候選檔案（sidecars × sigs），逐一嘗試直到驗章授權成功。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()
    raw_bytes = b"valid raw bytes"
    sidecar_ok = _build_test_session_sidecar(ulid, raw_bytes)
    sc_ok_bytes = json.dumps(sidecar_ok).encode("utf-8")
    sig_ok = sign_sidecar_bytes(sc_ok_bytes, priv_bytes, key_id)
    sig_ok_bytes = json.dumps(sig_ok).encode("utf-8")

    # 1st 候選：偽造或損毀之 sidecar 與 sig
    bad_sc_bytes = b'{"format": "invalid"}'
    bad_sig_bytes = b'{"alg": "none"}'
    f_sc_bad = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", bad_sc_bytes)
    f_sig_bad = drive.seed_file(inbox_fid, f"{ulid}.sig", bad_sig_bytes)

    # 2nd 候選：合法之 sidecar 與 sig
    f_sc_ok = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", sc_ok_bytes)
    f_sig_ok = drive.seed_file(inbox_fid, f"{ulid}.sig", sig_ok_bytes)
    f_raw = drive.seed_file(inbox_fid, f"{ulid}.raw", raw_bytes)

    scan_res = scan_inboxes(drive, registry)
    assert len(scan_res.items) == 1
    item = scan_res.items[0]
    assert len(item.sidecars) == 2
    assert len(item.sigs) == 2

    # 評估時應逐一嘗試並成功匹配合法組合
    d = evaluate(item, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d.kind == DecisionKind.ACCEPT
    assert d.code == "ok"


def test_ledger_codes_and_rejection_lifecycle(tmp_path: Path):
    """M1, M2: LEDGER_CODES 定義、清冊命中原 REJECT 沿用、rejections 快取免重複下載。"""
    assert LEDGER_CODES == frozenset({
        "ok",
        "already",
        "stale",
        "collision",
        "raw_mismatch",
        "too_old",
        "replayed_item_key",
        "too_large",
    })
    assert "orphan" not in LEDGER_CODES
    assert "bad_signature" not in LEDGER_CODES
    assert "invalid_format" not in LEDGER_CODES
    assert "unauthorized" not in LEDGER_CODES

    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()
    raw_bytes = b"sample raw"
    raw_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
    sidecar = _build_test_session_sidecar(ulid, raw_bytes)
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, priv_bytes, key_id)

    f_sc = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = drive.seed_file(inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = drive.seed_file(inbox_fid, f"{ulid}.raw", raw_bytes)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sc),
        sig=drive.get(f_sig),
        raw=drive.get(f_raw),
    )

    # 1. 在清冊中登記原先為 REJECT (stale)
    rejected_time = "2026-09-27T08:00:00Z"
    ledger.record(ulid, item_id="opencode:ses_12345", decision="stale", raw_sha256=raw_sha, at=rejected_time)

    # 當同一項目再次 evaluate 時，應沿用 REJECT("stale") 而非 ALREADY
    d = evaluate(item, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "stale"
    assert d.rejected_at == rejected_time
    assert d.deletable_after is not None

    # 2. 測試 rejections 快取命中（直接短路回傳，不重新下載）
    ulid2 = generate_ulid()
    rej_file = tmp_path / "agora" / "_committer" / "rejections" / f"{ulid2}.json"
    rej_file.parent.mkdir(parents=True, exist_ok=True)
    rej_file.write_text(
        json.dumps({
            "code": "stale",
            "at": "2026-09-27T07:00:00Z",
            "inbox_folder_id": inbox_fid,
            "candidate_ids": [],
        }),
        encoding="utf-8",
    )

    item2 = InboxItem(
        item_key=ulid2,
        inbox_folder_id=inbox_fid,
        sidecar=None,
        sig=None,
        raw=None,
    )
    d2 = evaluate(item2, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d2.kind == DecisionKind.REJECT
    assert d2.code == "stale"
    assert d2.authenticated is True
    assert d2.rejected_at == "2026-09-27T07:00:00Z"
    assert d2.deletable_after == datetime(2026, 9, 28, 7, 0, 0, tzinfo=timezone.utc)


def test_evaluate_timestamps_with_subseconds(tmp_path: Path):
    """M3: 時間一律轉為 datetime 比較，避免同一秒內不同字串長度精度顛倒順序。"""
    dummy_item = InboxItem(item_key="k", inbox_folder_id="f", sidecar=None, sig=None, raw=None)

    # 測試 sort_accepted_decisions 在同一秒內含毫秒與不含毫秒之排序
    d_early = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "session"},
        sidecar={"session": {"snapshot_at": "2026-09-27T08:00:00Z"}},
    )
    d_late = Decision(
        kind=DecisionKind.ACCEPT,
        item=dummy_item,
        code="ok",
        record_metadata={"type": "session"},
        sidecar={"session": {"snapshot_at": "2026-09-27T08:00:00.123Z"}},
    )

    # 即使以字串排序 ".123Z" 會排在 "Z" 前面，但轉成 datetime 後 08:00:00Z 必須在 08:00:00.123Z 前面
    sorted_res = sort_accepted_decisions([d_late, d_early])
    assert sorted_res == [d_early, d_late]


def test_evaluate_reference_link_monotonicity(tmp_path: Path):
    """M7: reference 單調性依 links/reference/<from>/<to>.json 索引比對。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["reference"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    # 建立既有 reference link 索引
    from_sess = "opencode:ses_from"
    to_sess = "opencode:ses_to"
    link_rel = layout.reference_link_path(from_sess, to_sess)
    existing_link_content = {
        "reference_id": f"reference:{generate_ulid()}",
        "read_snapshot_at": "2026-09-27T08:30:00Z",
    }
    store.put_json(link_rel, existing_link_content)

    # 建立一筆較舊（例如 08:00:00Z）但具有全新 ULID 的 reference sidecar
    ulid = generate_ulid()
    ref_sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid,
        "profile": "worker-1",
        "metadata": {
            "id": f"reference:{ulid}",
            "type": "reference",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "raw": None,
        "body": {
            "from_session_id": from_sess,
            "to_session_id": to_sess,
            "read_snapshot_at": "2026-09-27T08:00:00Z",  # 舊於 08:30:00Z
        },
    }
    sc_bytes = json.dumps(ref_sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, priv_bytes, key_id)

    f_sc = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = drive.seed_file(inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sc),
        sig=drive.get(f_sig),
        raw=None,
    )
    d = evaluate(item, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "stale"


def test_registry_rejects_duplicate_inbox_folder_ids():
    """L: validate_registry 拒絕多個 profile 共用同一個 inbox_folder_id。"""
    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": ["shared_folder_id"],
                "allowed_types": ["session"],
                "signing_keys": [],
            },
            "worker-2": {
                "inbox_folder_ids": ["shared_folder_id"],
                "allowed_types": ["session"],
                "signing_keys": [],
            },
        },
    }
    errors = validate_registry(registry_data)
    assert any("重複的 inbox_folder_id" in e.message for e in errors)


def test_spoofing_worker_key_into_other_inbox(tmp_path: Path):
    """spec identity 冒充情境：worker 的金鑰簽的項目放進 mac 的收件匣。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_mac = drive.seed_folder("inbox_mac")
    inbox_worker = drive.seed_folder("inbox_worker")

    priv_worker, pub_worker = generate_keypair()
    fp_worker = hashlib.sha256(pub_worker).hexdigest().lower()[:8]
    kid_worker = f"worker-1-{fp_worker}"

    priv_mac, pub_mac = generate_keypair()
    fp_mac = hashlib.sha256(pub_mac).hexdigest().lower()[:8]
    kid_mac = f"mac-{fp_mac}"

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "mac": {
                "inbox_folder_ids": [inbox_mac],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": kid_mac,
                        "public_key": base64.b64encode(pub_mac).decode("ascii"),
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            },
            "worker-1": {
                "inbox_folder_ids": [inbox_worker],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": kid_worker,
                        "public_key": base64.b64encode(pub_worker).decode("ascii"),
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            },
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()
    raw_bytes = b"worker data"
    # worker 簽名
    sidecar = _build_test_session_sidecar(ulid, raw_bytes, profile="worker-1")
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, priv_worker, kid_worker)

    # 放入 mac 的收件匣
    f_sc = drive.seed_file(inbox_mac, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = drive.seed_file(inbox_mac, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = drive.seed_file(inbox_mac, f"{ulid}.raw", raw_bytes)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_mac,
        sidecar=drive.get(f_sc),
        sig=drive.get(f_sig),
        raw=drive.get(f_raw),
    )
    d = evaluate(item, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d.kind == DecisionKind.REJECT
    # mac 的收件匣中只認可 mac profile 之公鑰；worker 金鑰無法通過驗章
    assert d.code in ("bad_signature", "unauthorized")


def test_revoked_key_signed_item(tmp_path: Path):
    """spec identity 撤銷情境：Registry 撤銷之後舊金鑰簽的項目必須拒收。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "revoked",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": "2026-09-26T00:00:00Z",
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()
    raw_bytes = b"sample raw"
    sidecar = _build_test_session_sidecar(ulid, raw_bytes)
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, priv_bytes, key_id)

    f_sc = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = drive.seed_file(inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = drive.seed_file(inbox_fid, f"{ulid}.raw", raw_bytes)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_fid,
        sidecar=drive.get(f_sc),
        sig=drive.get(f_sig),
        raw=drive.get(f_raw),
    )
    d = evaluate(item, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d.kind == DecisionKind.REJECT
    assert d.code in ("bad_signature", "unauthorized")


def test_evaluate_cross_inbox_rejection_isolation(tmp_path: Path):
    """R1: 資料夾 A 放垃圾的 K、資料夾 B 放合法的 K，且 A 先被評估，B 必須 ACCEPT。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_a = drive.seed_folder("inbox_worker1_a")
    inbox_b = drive.seed_folder("inbox_worker1_b")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_a, inbox_b],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()

    # 在 inbox_a 放置偽造/垃圾簽章之檔案
    bad_sc_bytes = b'{"format": "aistorage.inbox/v1", "item_key": "' + ulid.encode() + b'"}'
    bad_sig_bytes = json.dumps({"format": "aistorage.sig/v1", "key_id": key_id, "signature": "bad"}).encode("utf-8")
    f_sc_bad = drive.seed_file(inbox_a, f"{ulid}.sidecar.json", bad_sc_bytes)
    f_sig_bad = drive.seed_file(inbox_a, f"{ulid}.sig", bad_sig_bytes)

    # 在 inbox_b 放置合法簽章與合法 raw
    raw_content = b"valid session content"
    good_sc = _build_test_session_sidecar(ulid, raw_content, session_id="ses_cross_inbox")
    good_sc_bytes = json.dumps(good_sc).encode("utf-8")
    good_sig = sign_sidecar_bytes(good_sc_bytes, priv_bytes, key_id)
    f_sc_good = drive.seed_file(inbox_b, f"{ulid}.sidecar.json", good_sc_bytes)
    f_sig_good = drive.seed_file(inbox_b, f"{ulid}.sig", json.dumps(good_sig).encode("utf-8"))
    f_raw_good = drive.seed_file(inbox_b, f"{ulid}.raw", raw_content)

    item_a = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_a,
        sidecars=(drive.get(f_sc_bad),),
        sigs=(drive.get(f_sig_bad),),
    )
    item_b = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_b,
        sidecars=(drive.get(f_sc_good),),
        sigs=(drive.get(f_sig_good),),
        raws=(drive.get(f_raw_good),),
    )

    # 1. 先評估資料夾 A 的垃圾項目 -> REJECT (bad_signature)，且未通過驗章 (authenticated=False)
    d_a = evaluate(item_a, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_a.kind == DecisionKind.REJECT
    assert d_a.code in ("bad_signature", "invalid_format")
    assert d_a.authenticated is False

    # 驗章前的拒收絕不可寫入真本 _committer/rejections 快取
    rej_file = tmp_path / "agora" / "_committer" / "rejections" / f"{ulid}.json"
    assert not rej_file.is_file()

    # 2. 接著評估資料夾 B 的合法項目 -> 絕不受 A 影響，必須 ACCEPT！
    d_b = evaluate(item_b, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d_b.kind == DecisionKind.ACCEPT
    assert d_b.code == "ok"
    assert d_b.authenticated is True


def test_evaluate_same_inbox_rejection_recovery(tmp_path: Path):
    """R1: 同一收件匣資料夾，垃圾 K 在上一輪被拒收，下一輪合法的 K 出現了，必須 ACCEPT。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    ulid = generate_ulid()

    # 第一輪：只有垃圾檔案
    bad_sc_bytes = b'{"format": "aistorage.inbox/v1", "item_key": "' + ulid.encode() + b'"}'
    bad_sig_bytes = json.dumps({"format": "aistorage.sig/v1", "key_id": key_id, "signature": "bad"}).encode("utf-8")
    f_sc_bad = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", bad_sc_bytes)
    f_sig_bad = drive.seed_file(inbox_fid, f"{ulid}.sig", bad_sig_bytes)

    item_round1 = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_fid,
        sidecars=(drive.get(f_sc_bad),),
        sigs=(drive.get(f_sig_bad),),
    )
    d1 = evaluate(item_round1, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d1.kind == DecisionKind.REJECT
    assert d1.authenticated is False

    # 驗章前拒收不寫入真本快取
    rej_file = tmp_path / "agora" / "_committer" / "rejections" / f"{ulid}.json"
    assert not rej_file.is_file()

    # 第二輪：合法的檔案上傳進來（作為新的候選檔案）
    raw_content = b"recovered content"
    good_sc = _build_test_session_sidecar(ulid, raw_content, session_id="ses_recovered")
    good_sc_bytes = json.dumps(good_sc).encode("utf-8")
    good_sig = sign_sidecar_bytes(good_sc_bytes, priv_bytes, key_id)
    f_sc_good = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", good_sc_bytes)
    f_sig_good = drive.seed_file(inbox_fid, f"{ulid}.sig", json.dumps(good_sig).encode("utf-8"))
    f_raw_good = drive.seed_file(inbox_fid, f"{ulid}.raw", raw_content)

    item_round2 = InboxItem(
        item_key=ulid,
        inbox_folder_id=inbox_fid,
        sidecars=(drive.get(f_sc_bad), drive.get(f_sc_good)),
        sigs=(drive.get(f_sig_bad), drive.get(f_sig_good)),
        raws=(drive.get(f_raw_good),),
    )

    d2 = evaluate(item_round2, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d2.kind == DecisionKind.ACCEPT
    assert d2.code == "ok"
    assert d2.authenticated is True


def test_evaluate_pre_auth_too_large_codes(tmp_path: Path):
    """R2: 驗章前的 sidecar (>1MiB) 與 sig (>4KiB) 過大使用 sidecar_too_large / sig_too_large，authenticated=False。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    # 1. sidecar 超過 1 MiB
    ulid1 = generate_ulid()
    huge_sidecar_bytes = b"x" * (1024 * 1024 + 10)
    f_huge_sc = drive.seed_file(inbox_fid, f"{ulid1}.sidecar.json", huge_sidecar_bytes)
    f_dummy_sig = drive.seed_file(inbox_fid, f"{ulid1}.sig", b"{}")
    it_huge_sc = InboxItem(item_key=ulid1, inbox_folder_id=inbox_fid, sidecars=(drive.get(f_huge_sc),), sigs=(drive.get(f_dummy_sig),))
    d1 = evaluate(it_huge_sc, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d1.kind == DecisionKind.REJECT
    assert d1.code == "sidecar_too_large"
    assert d1.authenticated is False
    assert d1.code not in LEDGER_CODES

    # 2. sig 超過 4 KiB
    ulid2 = generate_ulid()
    f_normal_sc = drive.seed_file(inbox_fid, f"{ulid2}.sidecar.json", b"{}")
    huge_sig_bytes = b"x" * (4 * 1024 + 10)
    f_huge_sig = drive.seed_file(inbox_fid, f"{ulid2}.sig", huge_sig_bytes)
    it_huge_sig = InboxItem(item_key=ulid2, inbox_folder_id=inbox_fid, sidecars=(drive.get(f_normal_sc),), sigs=(drive.get(f_huge_sig),))
    d2 = evaluate(it_huge_sig, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d2.kind == DecisionKind.REJECT
    assert d2.code == "sig_too_large"
    assert d2.authenticated is False
    assert d2.code not in LEDGER_CODES


def test_evaluate_reference_monotonicity_fail_closed(tmp_path: Path):
    """R3: reference 單調性檢查輸入不合法判 REJECT(invalid_format)，索引檔案損毀判 MismatchError 中止。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["reference"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    def make_ref_item(ulid: str, body: dict) -> InboxItem:
        sc = {
            "format": "aistorage.inbox/v1",
            "item_key": ulid,
            "profile": "worker-1",
            "metadata": {
                "id": f"reference:{ulid}",
                "type": "reference",
                "created_at": "2026-09-27T08:00:00Z",
                "updated_at": "2026-09-27T08:00:00Z",
                "case_id": None,
                "provenance": None,
            },
            "raw": None,
            "body": body,
        }
        sc_bytes = json.dumps(sc).encode("utf-8")
        sig = sign_sidecar_bytes(sc_bytes, priv_bytes, key_id)
        f_sc = drive.seed_file(inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
        f_sig = drive.seed_file(inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
        return InboxItem(
            item_key=ulid,
            inbox_folder_id=inbox_fid,
            sidecars=(drive.get(f_sc),),
            sigs=(drive.get(f_sig),),
        )

    # 1. 現有索引已有較新 snapshot (10:00:00Z)，新進 snapshot 較舊 (08:00:00Z) -> REJECT(stale)
    link_path = tmp_path / "agora" / layout.reference_link_path("opencode:ses_from", "opencode:ses_to")
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.write_text(json.dumps({"read_snapshot_at": "2026-09-27T10:00:00Z"}), encoding="utf-8")

    ulid1 = generate_ulid()
    it_stale = make_ref_item(ulid1, {"from_session_id": "opencode:ses_from", "to_session_id": "opencode:ses_to", "read_snapshot_at": "2026-09-27T08:00:00Z"})
    d1 = evaluate(it_stale, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert d1.kind == DecisionKind.REJECT
    assert d1.code == "stale"
    assert d1.authenticated is True

    # 2. 真本現有 links/reference 索引檔損毀 -> raise MismatchError 中止
    ulid2 = generate_ulid()
    link_path.write_text("corrupted json {", encoding="utf-8")

    it_corrupted_link = make_ref_item(ulid2, {"from_session_id": "opencode:ses_from", "to_session_id": "opencode:ses_to", "read_snapshot_at": "2026-09-27T11:00:00Z"})
    with pytest.raises(MismatchError, match="參考 Link 索引檔案損毀"):
        evaluate(it_corrupted_link, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)

    # 3. 測試 _check_reference_monotonicity 函式防禦不合法輸入 -> REJECT(invalid_format)
    dummy_decision_fn = lambda code, **kwargs: Decision(kind=DecisionKind.REJECT, item=it_stale, code=code, authenticated=True)
    d_bad_time = _check_reference_monotonicity(
        {"body": {"from_session_id": "opencode:a", "to_session_id": "opencode:b", "read_snapshot_at": "invalid-date"}},
        store,
        dummy_decision_fn,
    )
    assert d_bad_time is not None
    assert d_bad_time.code == "invalid_format"
    assert d_bad_time.authenticated is True

    d_bad_sess = _check_reference_monotonicity(
        {"body": {"from_session_id": ".", "to_session_id": "opencode:b", "read_snapshot_at": "2026-09-27T08:00:00Z"}},
        store,
        dummy_decision_fn,
    )
    assert d_bad_sess is not None
    assert d_bad_sess.code == "invalid_format"
    assert d_bad_sess.authenticated is True


def test_rejection_cache_isolation_cross_folders(tmp_path: Path):
    """R1: 測試資料夾 A 放垃圾的 K、資料夾 B 放合法的 K，而且 A 先被評估，B 必須 ACCEPT。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_a = drive.seed_folder("inbox_a")
    inbox_b = drive.seed_folder("inbox_b")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-b-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-a": {
                "inbox_folder_ids": [inbox_a],
                "allowed_types": ["session"],
                "signing_keys": [],
            },
            "worker-b": {
                "inbox_folder_ids": [inbox_b],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            },
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    target_ulid = generate_ulid()

    # 資料夾 A：放無效簽章的垃圾 K
    f_junk_sc = drive.seed_file(inbox_a, f"{target_ulid}.sidecar.json", b'{"format":"aistorage.inbox/v1"}')
    f_junk_sig = drive.seed_file(inbox_a, f"{target_ulid}.sig", b'{"key_id":"unknown","signature":"deadbeef"}')
    f_junk_raw = drive.seed_file(inbox_a, f"{target_ulid}.raw", b"junk raw")
    it_a = InboxItem(
        item_key=target_ulid,
        inbox_folder_id=inbox_a,
        sidecars=(drive.get(f_junk_sc),),
        sigs=(drive.get(f_junk_sig),),
        raws=(drive.get(f_junk_raw),),
    )

    # 先評估 A（未通過驗章，回傳 REJECT，且不應污染真本 rejections 快取）
    dec_a = evaluate(it_a, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert dec_a.kind == DecisionKind.REJECT
    assert dec_a.authenticated is False

    # 資料夾 B：放合法的 K
    raw_content = b'{"hello": "world"}'
    raw_sha = hashlib.sha256(raw_content).hexdigest().lower()
    sc_b = _build_test_session_sidecar(target_ulid, raw_content, session_id="ses_b", profile="worker-b")
    sc_b_bytes = json.dumps(sc_b).encode("utf-8")
    sig_b = sign_sidecar_bytes(sc_b_bytes, priv_bytes, key_id)
    f_b_sc = drive.seed_file(inbox_b, f"{target_ulid}.sidecar.json", sc_b_bytes)
    f_b_sig = drive.seed_file(inbox_b, f"{target_ulid}.sig", json.dumps(sig_b).encode("utf-8"))
    f_b_raw = drive.seed_file(inbox_b, f"{target_ulid}.raw", raw_content, sha256=raw_sha)
    it_b = InboxItem(
        item_key=target_ulid,
        inbox_folder_id=inbox_b,
        sidecars=(drive.get(f_b_sc),),
        sigs=(drive.get(f_b_sig),),
        raws=(drive.get(f_b_raw),),
    )

    # 評估 B：必須不受 A 的影響，成功 ACCEPT
    dec_b = evaluate(it_b, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert dec_b.kind == DecisionKind.ACCEPT
    assert dec_b.authenticated is True


def test_rejection_cache_new_candidates_in_same_folder(tmp_path: Path):
    """R1: 同一個資料夾，垃圾的 K 在上一輪被拒收，這一輪合法的 K 出現了，必須 ACCEPT。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_fid = drive.seed_folder("inbox_worker1")

    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"worker-1-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry_data = {
        "format": "aistorage.registry/v1",
        "profiles": {
            "worker-1": {
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            },
        },
    }
    registry = Registry(registry_data)
    store = AgoraStore(tmp_path / "agora", raw_storage=FakeRawStorage())
    ledger = Ledger(store)
    workdir = tmp_path / "work"

    target_ulid = generate_ulid()

    # 第 1 輪：只有垃圾檔（偽造簽章）
    f_junk_sc = drive.seed_file(inbox_fid, f"{target_ulid}.sidecar.json", b'{"format":"aistorage.inbox/v1"}')
    f_junk_sig = drive.seed_file(inbox_fid, f"{target_ulid}.sig", b'{"key_id":"worker-1-bad","signature":"deadbeef"}')
    it_round1 = InboxItem(
        item_key=target_ulid,
        inbox_folder_id=inbox_fid,
        sidecars=(drive.get(f_junk_sc),),
        sigs=(drive.get(f_junk_sig),),
    )
    dec1 = evaluate(it_round1, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert dec1.kind == DecisionKind.REJECT
    assert dec1.authenticated is False

    # 第 2 輪：同一個資料夾裡出現了合法的候選檔案（例如合法同名檔案被寫入）
    raw_content = b'{"hello": "valid"}'
    raw_sha = hashlib.sha256(raw_content).hexdigest().lower()
    sc_valid = _build_test_session_sidecar(target_ulid, raw_content, session_id="ses_valid", profile="worker-1")
    sc_valid_bytes = json.dumps(sc_valid).encode("utf-8")
    sig_valid = sign_sidecar_bytes(sc_valid_bytes, priv_bytes, key_id)

    f_valid_sc = drive.seed_file(inbox_fid, f"{target_ulid}.sidecar.json", sc_valid_bytes)
    f_valid_sig = drive.seed_file(inbox_fid, f"{target_ulid}.sig", json.dumps(sig_valid).encode("utf-8"))
    f_valid_raw = drive.seed_file(inbox_fid, f"{target_ulid}.raw", raw_content, sha256=raw_sha)

    it_round2 = InboxItem(
        item_key=target_ulid,
        inbox_folder_id=inbox_fid,
        sidecars=(drive.get(f_junk_sc), drive.get(f_valid_sc)),
        sigs=(drive.get(f_junk_sig), drive.get(f_valid_sig)),
        raws=(drive.get(f_valid_raw),),
    )
    dec2 = evaluate(it_round2, drive=drive, registry=registry, store=store, ledger=ledger, clock=clock, workdir=workdir)
    assert dec2.kind == DecisionKind.ACCEPT
    assert dec2.authenticated is True




