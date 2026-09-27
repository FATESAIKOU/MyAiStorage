"""Acceptance tests for Inbox Processing (Tasks 3.3, 3.4).

Adheres strictly to:
- docs/impl/group3-modules.md §4, §5, §8.2
- Decision table for intake/evaluate:
  - Missing sig: recent (<=24h) -> DEFER, old (>24h) -> REJECT(orphan)
  - Bad signature -> REJECT(bad_signature)
  - Duplicate key in sidecar -> REJECT(invalid_format)
  - item_key mismatch -> REJECT(invalid_format)
  - Profile impersonation -> REJECT(unauthorized)
  - Revoked key -> REJECT(unauthorized / bad_signature)
  - Disallowed type -> REJECT(unauthorized)
  - raw too large -> REJECT(too_large) without downloading raw
  - raw hash mismatch -> REJECT(raw_mismatch)
  - Replay (different raw sha in ledger) -> REJECT(replayed_item_key)
  - ALREADY (same raw sha in ledger or existing session) -> ALREADY(already)
  - Collision -> REJECT(collision)
  - Artifact -> DEFER(foundry_not_enabled)
  - .sig ReadError cannot become REJECT (must bubble up / abort)
  - Ledger corruption aborts (raises MismatchError)
  - Monotonicity checks (stale)
  - Dispatch ordering
"""

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pytest

# Ensure agora is imported first to avoid circular import between agora.apply and intake.evaluate
import aistorage.agora
from aistorage.agora import AgoraStore, FakeRawStorage, layout, SessionRecord
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.drive.model import DriveFile
from aistorage.errors import MismatchError, ReadError
from aistorage.identity import Registry, generate_keypair
from aistorage.inbox import sign_sidecar_bytes
from aistorage.intake import (
    Decision,
    DecisionKind,
    InboxItem,
    Ledger,
    count_shaped,
    evaluate,
    is_actionable,
    scan_inboxes,
    sort_accepted_decisions,
    stamp_record,
    strict_json,
)
from aistorage.schema import generate_ulid


# ---------------------------------------------------------------------------
# Fixture & Helper Functions
# ---------------------------------------------------------------------------


class IntakeTestEnv:
    """Helper environment for intake evaluate tests."""

    def __init__(self, tmp_path: Path, now_str: str = "2026-09-27T10:00:00Z"):
        self.clock = FixedClock(now_str)
        self.drive = FakeDrive(clock=self.clock)
        self.inbox_fid = self.drive.seed_folder("inbox_worker_1")
        self.tmp_path = tmp_path
        self.workdir = tmp_path / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)

        # Generate keypair for profile:worker-1
        self.priv_bytes, self.pub_bytes = generate_keypair()
        pub_fingerprint = hashlib.sha256(self.pub_bytes).hexdigest().lower()[:8]
        self.key_id = f"worker-1-{pub_fingerprint}"
        self.pub_b64 = base64.b64encode(self.pub_bytes).decode("ascii")

        self.registry_data = {
            "format": "aistorage.registry/v1",
            "profiles": {
                "worker-1": {
                    "inbox_folder_ids": [self.inbox_fid],
                    "allowed_types": ["session", "artifact", "claim", "reference", "handoff", "rewrite"],
                    "signing_keys": [
                        {
                            "key_id": self.key_id,
                            "public_key": self.pub_b64,
                            "status": "active",
                            "created_at": "2026-09-20T00:00:00Z",
                            "revoked_at": None,
                        }
                    ],
                }
            },
        }
        self.registry = Registry(self.registry_data)
        self.store = AgoraStore(tmp_path / "agora_store", raw_storage=FakeRawStorage())
        self.ledger = Ledger(self.store)

    def build_session_sidecar(
        self,
        item_key: str,
        raw_content: bytes,
        *,
        profile: str = "worker-1",
        session_id: str = "ses_test_001",
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

    def seed_valid_session_item(
        self,
        raw_content: bytes = b'{"messages": ["hello valid"]}',
        *,
        item_key: str | None = None,
        session_id: str = "ses_test_001",
        snapshot_at: str = "2026-09-27T08:00:00Z",
        created_time: str = "2026-09-27T08:30:00Z",
    ) -> tuple[InboxItem, dict, bytes]:
        ulid = item_key or generate_ulid()
        sidecar = self.build_session_sidecar(
            ulid,
            raw_content,
            session_id=session_id,
            snapshot_at=snapshot_at,
        )
        sidecar_bytes = json.dumps(sidecar).encode("utf-8")
        sig = sign_sidecar_bytes(sidecar_bytes, self.priv_bytes, self.key_id)
        sig_bytes = json.dumps(sig).encode("utf-8")

        f_sc = self.drive.seed_file(self.inbox_fid, f"{ulid}.sidecar.json", sidecar_bytes, created_time=created_time)
        f_sig = self.drive.seed_file(self.inbox_fid, f"{ulid}.sig", sig_bytes, created_time=created_time)
        f_raw = self.drive.seed_file(self.inbox_fid, f"{ulid}.raw", raw_content, created_time=created_time)

        item = InboxItem(
            item_key=ulid,
            inbox_folder_id=self.inbox_fid,
            sidecar=self.drive.get(f_sc),
            sig=self.drive.get(f_sig),
            raw=self.drive.get(f_raw),
        )
        return item, sidecar, raw_content

    def evaluate_item(self, item: InboxItem, max_raw: int = 104857600) -> Decision:
        return evaluate(
            item,
            drive=self.drive,
            registry=self.registry,
            store=self.store,
            ledger=self.ledger,
            clock=self.clock,
            workdir=self.workdir,
            max_raw=max_raw,
        )


# ---------------------------------------------------------------------------
# Decision Table Tests (§4.2, §8.2)
# ---------------------------------------------------------------------------


def test_evaluate_missing_sig_recent_defers(tmp_path: Path):
    """決策表 1: 缺少 .sig 檔案，但建立時間未滿 24 小時 -> DEFER(incomplete)。"""
    env = IntakeTestEnv(tmp_path, now_str="2026-09-27T10:00:00Z")
    ulid = generate_ulid()

    # 2 小時前建立 (2026-09-27T08:00:00Z)
    f_sc = env.drive.seed_file(
        env.inbox_fid,
        f"{ulid}.sidecar.json",
        b"{}",
        created_time="2026-09-27T08:00:00Z",
    )
    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=None,
        raw=None,
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.DEFER
    assert d.code == "incomplete"


def test_evaluate_missing_sig_old_rejects_as_orphan(tmp_path: Path):
    """決策表 2: 缺少 .sig 檔案，建立時間超過 24 小時 -> REJECT(orphan)。"""
    env = IntakeTestEnv(tmp_path, now_str="2026-09-27T10:00:00Z")
    ulid = generate_ulid()

    # 26 小時前建立 (2026-09-26T08:00:00Z)
    f_sc = env.drive.seed_file(
        env.inbox_fid,
        f"{ulid}.sidecar.json",
        b"{}",
        created_time="2026-09-26T08:00:00Z",
    )
    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=None,
        raw=None,
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "orphan"


def test_evaluate_bad_signature_rejects(tmp_path: Path):
    """決策表 3: 簽章錯誤或偽造 -> REJECT(bad_signature)。"""
    env = IntakeTestEnv(tmp_path)
    ulid = generate_ulid()

    raw_bytes = b"content"
    sidecar = env.build_session_sidecar(ulid, raw_bytes)
    sidecar_bytes = json.dumps(sidecar).encode("utf-8")

    # 偽造簽章值
    fake_sig = {"alg": "ed25519", "key_id": env.key_id, "value": "A" * 86 + "=="}
    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", sidecar_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(fake_sig).encode("utf-8"))
    f_raw = env.drive.seed_file(env.inbox_fid, f"{ulid}.raw", raw_bytes)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=env.drive.get(f_raw),
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "bad_signature"


def test_evaluate_duplicate_key_in_sidecar_rejects_as_invalid_format(tmp_path: Path):
    """決策表 4: sidecar JSON 含重複鍵名（strict_json 拒絕）-> REJECT(invalid_format)。"""
    env = IntakeTestEnv(tmp_path)
    ulid = generate_ulid()

    # 構造含重複鍵名的 sidecar json
    raw_dup_json = (
        b'{"format": "aistorage.inbox/v1", "item_key": "' + ulid.encode() + b'", '
        b'"item_key": "duplicate_key_val", "profile": "worker-1"}'
    )
    sig = sign_sidecar_bytes(raw_dup_json, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", raw_dup_json)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=None,
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "invalid_format"


def test_evaluate_item_key_mismatch_rejects_as_invalid_format(tmp_path: Path):
    """決策表 5: sidecar["item_key"] 與檔案名所帶的 item_key 不一致 -> REJECT(invalid_format)。"""
    env = IntakeTestEnv(tmp_path)
    ulid_file = generate_ulid()
    ulid_inside = generate_ulid()

    raw_bytes = b"sample raw"
    # sidecar 內部的 item_key 與檔名不同
    sidecar = env.build_session_sidecar(ulid_inside, raw_bytes)
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid_file}.sidecar.json", sc_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid_file}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = env.drive.seed_file(env.inbox_fid, f"{ulid_file}.raw", raw_bytes)

    item = InboxItem(
        item_key=ulid_file,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=env.drive.get(f_raw),
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "invalid_format"


def test_evaluate_profile_impersonation_rejects_as_unauthorized(tmp_path: Path):
    """決策表 6: 冒充 profile（sidecar["profile"] != folder_profile）-> REJECT(unauthorized)。"""
    env = IntakeTestEnv(tmp_path)
    ulid = generate_ulid()

    raw_bytes = b"sample raw"
    # sidecar 標示 worker-2，但此 inbox_folder 屬於 worker-1
    sidecar = env.build_session_sidecar(ulid, raw_bytes, profile="worker-2")
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = env.drive.seed_file(env.inbox_fid, f"{ulid}.raw", raw_bytes)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=env.drive.get(f_raw),
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code in ("unauthorized", "bad_signature")


def test_evaluate_revoked_key_rejects_as_unauthorized(tmp_path: Path):
    """決策表 7: 已撤銷之金鑰簽章 -> REJECT(unauthorized / bad_signature)。"""
    env = IntakeTestEnv(tmp_path)
    # 將金鑰標記為 revoked
    env.registry_data["profiles"]["worker-1"]["signing_keys"][0]["status"] = "revoked"
    env.registry_data["profiles"]["worker-1"]["signing_keys"][0]["revoked_at"] = "2026-09-25T00:00:00Z"
    env.registry = Registry(env.registry_data)

    item, _, _ = env.seed_valid_session_item()
    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code in ("unauthorized", "bad_signature")


def test_evaluate_disallowed_type_rejects_as_unauthorized(tmp_path: Path):
    """決策表 8: 紀錄 type 不在 profile 的 allowed_types 中 -> REJECT(unauthorized)。"""
    env = IntakeTestEnv(tmp_path)
    # worker-1 僅允許 session
    env.registry_data["profiles"]["worker-1"]["allowed_types"] = ["session"]
    env.registry = Registry(env.registry_data)

    ulid = generate_ulid()
    claim_sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid,
        "profile": "worker-1",
        "metadata": {
            "id": f"claim:{ulid}",
            "type": "claim",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "raw": None,
        "body": {
            "handoff_id": f"handoff:{ulid}",
            "claimer_session_id": "opencode:ses_123",
            "reason": "claim disallowed test",
        },
    }
    sc_bytes = json.dumps(claim_sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=None,
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "unauthorized"


def test_evaluate_raw_too_large_rejects_without_downloading(tmp_path: Path):
    """決策表 9: raw.size > max_raw 時直接拒絕，且絕對不呼叫 drive.download。"""
    env = IntakeTestEnv(tmp_path)
    ulid = generate_ulid()

    raw_content = b"x" * 2048
    sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid,
        "profile": "worker-1",
        "metadata": {
            "id": "opencode:ses_huge",
            "type": "session",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "raw": {
            "sha256": hashlib.sha256(raw_content).hexdigest().lower(),
            "size": len(raw_content),
        },
        "session": {
            "source": "opencode",
            "source_session_id": "ses_huge",
            "snapshot_at": "2026-09-27T08:00:00Z",
            "status": "running",
            "stopped_at": None,
            "in_progress": False,
            "parent_id": None,
        },
        "body": {},
    }
    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = env.drive.seed_file(env.inbox_fid, f"{ulid}.raw", raw_content)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=env.drive.get(f_raw),
    )

    # 執行評估，設定 max_raw 為 1024 位元組 (raw 大小 2048 > 1024)
    d = env.evaluate_item(item, max_raw=1024)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "too_large"

    # 驗證：絕對沒有對 raw 檔案呼叫 download 下載
    raw_downloaded = any(op == "download" and fid == f_raw for op, fid in env.drive.calls)
    assert not raw_downloaded, "評估 too_large 時不得下載 raw 檔案"


def test_evaluate_raw_hash_mismatch_rejects(tmp_path: Path):
    """決策表 10: 下載後比對 raw 內容雜湊不符 -> REJECT(raw_mismatch)。"""
    env = IntakeTestEnv(tmp_path)
    ulid = generate_ulid()

    actual_raw = b"actual raw content"
    # sidecar 宣稱錯誤的 sha256
    sidecar = env.build_session_sidecar(ulid, actual_raw)
    sidecar["raw"]["sha256"] = "e" * 64

    sc_bytes = json.dumps(sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))
    f_raw = env.drive.seed_file(env.inbox_fid, f"{ulid}.raw", actual_raw)

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=env.drive.get(f_raw),
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "raw_mismatch"


def test_evaluate_replay_with_different_hash_rejects(tmp_path: Path):
    """決策表 11: 清冊中已有相同 item_key 但 raw 雜湊不同 -> REJECT(replayed_item_key)。"""
    env = IntakeTestEnv(tmp_path)
    item, _, raw_bytes = env.seed_valid_session_item()

    # 清冊中記錄相同的 item_key，但不同的 raw_sha256
    env.ledger.record(
        item.item_key,
        item_id="opencode:ses_test_001",
        decision="accept",
        raw_sha256="different_raw_sha256",
        at="2026-09-27T09:00:00Z",
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "replayed_item_key"


def test_evaluate_already_accepted_via_ledger_and_store(tmp_path: Path):
    """決策表 12: 重複上傳已收進之相同內容 -> ALREADY(already)。

    - 情況 A: 清冊中已記錄該 item_key 且 raw 雜湊完全相同。
    - 情況 B: 全新 item_key 但 store 中該 Session 已有完全相同之 raw_sha256（3.4 dedup）。
    """
    env = IntakeTestEnv(tmp_path)

    # 1. 情況 A: 清冊中完全相符
    item_a, _, raw_a = env.seed_valid_session_item(raw_content=b"content a")
    raw_a_sha = hashlib.sha256(raw_a).hexdigest().lower()
    env.ledger.record(
        item_a.item_key,
        item_id="opencode:ses_test_001",
        decision="accept",
        raw_sha256=raw_a_sha,
        at="2026-09-27T09:00:00Z",
    )
    d_a = env.evaluate_item(item_a)
    assert d_a.kind == DecisionKind.ALREADY
    assert d_a.code == "already"

    # 2. 情況 B: 全新 item_key，但 store 既有 Session 之 raw_sha256 完全相同 (dedup)
    raw_b = b"content b"
    raw_b_sha = hashlib.sha256(raw_b).hexdigest().lower()
    # 先在 store 建立既有 session
    raw_b_file = tmp_path / "raw_b.json"
    raw_b_file.write_bytes(raw_b)
    rec_b = SessionRecord(
        id="opencode:ses_test_002",
        producer="profile:worker-1",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:00:00Z",
        status="running",
        snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=raw_b_sha,
        raw_size=len(raw_b),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FA1",
    )
    env.store.put_session(rec_b, raw_b_file)

    # 上傳全新 item_key 的項目，但內容與 ses_test_002 完全一致
    item_b, _, _ = env.seed_valid_session_item(
        raw_content=raw_b,
        session_id="ses_test_002",
        snapshot_at="2026-09-27T08:05:00Z",
    )
    d_b = env.evaluate_item(item_b)
    assert d_b.kind == DecisionKind.ALREADY
    assert d_b.code == "already"


def test_evaluate_collision_rejects(tmp_path: Path):
    """決策表 13: 與真本既有紀錄發生撞號衝突（如不同 producer 或型態不符）-> REJECT(collision)。"""
    env = IntakeTestEnv(tmp_path)

    # 在 store 預先放入一筆由不同 producer (profile:alien-worker) 擁有的既有紀錄
    existing_rec = {
        "id": "opencode:ses_collision",
        "type": "session",
        "producer": "profile:alien-worker",
        "created_at": "2026-09-27T08:00:00Z",
        "updated_at": "2026-09-27T08:00:00Z",
        "case_id": None,
        "provenance": None,
    }
    env.store.put_json("sessions/opencode/ses_collision/meta.json", existing_rec)

    # 嘗試以 worker-1 身份上傳同一個 session id
    item, _, _ = env.seed_valid_session_item(session_id="ses_collision")
    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "collision"


def test_evaluate_artifact_defers_as_foundry_not_enabled(tmp_path: Path):
    """決策表 14: artifact 項目一律 DEFER(foundry_not_enabled)（保留在收件匣，第 7 組處理）。"""
    env = IntakeTestEnv(tmp_path)
    ulid = generate_ulid()

    art_sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid,
        "profile": "worker-1",
        "metadata": {
            "id": f"artifact:{ulid}",
            "type": "artifact",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "raw": None,
        "body": {
            "kind": "link",
            "produced_by_session_id": "opencode:ses_123",
            "content_type": "application/octet-stream",
            "link": "annex:SHA256E-s100--dummy",
        },
    }
    sc_bytes = json.dumps(art_sidecar).encode("utf-8")
    sig = sign_sidecar_bytes(sc_bytes, env.priv_bytes, env.key_id)

    f_sc = env.drive.seed_file(env.inbox_fid, f"{ulid}.sidecar.json", sc_bytes)
    f_sig = env.drive.seed_file(env.inbox_fid, f"{ulid}.sig", json.dumps(sig).encode("utf-8"))

    item = InboxItem(
        item_key=ulid,
        inbox_folder_id=env.inbox_fid,
        sidecar=env.drive.get(f_sc),
        sig=env.drive.get(f_sig),
        raw=None,
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.DEFER
    assert d.code == "foundry_not_enabled"


def test_evaluate_sig_read_error_bubbles_up_and_does_not_reject(tmp_path: Path):
    """決策表 15: 下載 .sig 時發生 ReadError，絕對不得捕獲轉為 REJECT，必須向外拋出中止本輪。"""
    env = IntakeTestEnv(tmp_path)
    item, _, _ = env.seed_valid_session_item()

    # 注入：下載 sig 檔案時拋出 ReadError
    env.drive.inject("download_bytes", item.sig.id, error=ReadError, times=1)

    with pytest.raises(ReadError):
        env.evaluate_item(item)


def test_ledger_corruption_aborts(tmp_path: Path):
    """決策表 16: 清冊檔案毀損（非合法 JSONL）時必須拋出 MismatchError 中止整個處理流程。"""
    store_dir = tmp_path / "agora"
    store = AgoraStore(store_dir, raw_storage=FakeRawStorage())
    bad_ledger_file = store_dir / "_committer" / "ledger" / "2026-09.jsonl"
    bad_ledger_file.parent.mkdir(parents=True, exist_ok=True)
    bad_ledger_file.write_text("THIS IS NOT JSONL CORRUPTED DATA\n", encoding="utf-8")

    ledger = Ledger(store)
    with pytest.raises(MismatchError, match="清冊檔案.*解析失敗"):
        ledger.contains("01ARZ3NDEKTSV4RRFFQ69G5FAV")


def test_evaluate_stale_monotonicity_rejects(tmp_path: Path):
    """單調性防重放: 新快照 snapshot_at <= 既有快照 snapshot_at -> REJECT(stale)。"""
    env = IntakeTestEnv(tmp_path)

    # 既有快照 snapshot_at = 08:30:00Z
    raw_old = b"existing raw"
    raw_old_file = tmp_path / "old.json"
    raw_old_file.write_bytes(raw_old)
    rec_old = SessionRecord(
        id="opencode:ses_stale",
        producer="profile:worker-1",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T08:30:00Z",
        status="running",
        snapshot_at="2026-09-27T08:30:00Z",
        raw_sha256=hashlib.sha256(raw_old).hexdigest().lower(),
        raw_size=len(raw_old),
        committed_at="2026-09-27T08:31:00Z",
        last_item_key="01ARZ3NDEKTSV4RRFFQ69G5FA1",
    )
    env.store.put_session(rec_old, raw_old_file)

    # 上傳較舊快照 snapshot_at = 08:15:00Z（內容不同）
    item, _, _ = env.seed_valid_session_item(
        raw_content=b"older raw content",
        session_id="ses_stale",
        snapshot_at="2026-09-27T08:15:00Z",
    )

    d = env.evaluate_item(item)
    assert d.kind == DecisionKind.REJECT
    assert d.code == "stale"


def test_sort_accepted_decisions_ordering():
    """驗證分派順序: session(按 snapshot_at 遞增) -> rewrite -> handoff -> claim -> reference。"""
    dummy_item = InboxItem("k", "f", None, None, None)

    d_ref = Decision(DecisionKind.ACCEPT, dummy_item, "ok", record_metadata={"type": "reference", "updated_at": "2026-09-27T08:00:00Z"})
    d_claim = Decision(DecisionKind.ACCEPT, dummy_item, "ok", record_metadata={"type": "claim", "updated_at": "2026-09-27T08:00:00Z"})
    d_handoff = Decision(DecisionKind.ACCEPT, dummy_item, "ok", record_metadata={"type": "handoff", "updated_at": "2026-09-27T08:00:00Z"})
    d_rewrite = Decision(DecisionKind.ACCEPT, dummy_item, "ok", record_metadata={"type": "rewrite", "updated_at": "2026-09-27T08:00:00Z"})
    d_ses_2 = Decision(DecisionKind.ACCEPT, dummy_item, "ok", record_metadata={"type": "session"}, sidecar={"session": {"snapshot_at": "2026-09-27T08:20:00Z"}})
    d_ses_1 = Decision(DecisionKind.ACCEPT, dummy_item, "ok", record_metadata={"type": "session"}, sidecar={"session": {"snapshot_at": "2026-09-27T08:10:00Z"}})

    unordered = [d_ref, d_claim, d_ses_2, d_rewrite, d_handoff, d_ses_1]
    sorted_res = sort_accepted_decisions(unordered)

    assert sorted_res == [d_ses_1, d_ses_2, d_rewrite, d_handoff, d_claim, d_ref]
