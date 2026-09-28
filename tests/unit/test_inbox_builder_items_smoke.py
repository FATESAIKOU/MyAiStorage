"""inbox_builder 其他型態（第 0 節）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

重點：每個型態組出來都要通過 `validate_sidecar` 與 `check_raw` 自查、簽章可驗，
上傳順序是 raw → sidecar → sig。範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aistorage.drive.fake import FakeDrive
from aistorage.inbox import sign_sidecar_bytes, verify_sidecar_bytes
from aistorage.inbox_builder import (
    InboxBuildError,
    build_artifact_item,
    build_claim_item,
    build_handoff_item,
    build_reference_item,
    load_private_key,
    serialize_json,
    upload_item,
)

T0 = "2026-09-27T08:00:00Z"
PROFILE = "mac-opencode"
SNAP = "a" * 64


@pytest.fixture
def signer(tmp_path: Path) -> tuple[bytes, str, bytes]:
    """測試用的 Ed25519 簽章金鑰（key_id 必須是 <profile>-<公鑰雜湊前8位>）。"""
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = f"{PROFILE}-{hashlib.sha256(pub).hexdigest()[:8]}"
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(raw)
    os.chmod(key_path, 0o600)
    assert load_private_key(key_path) == raw
    return raw, key_id, pub


def _verify(item, pub: bytes, key_id: str) -> None:
    """簽章可驗，而且涵蓋的是 sidecar 的原始位元組。"""
    assert verify_sidecar_bytes(item.sidecar_bytes, item.sig, {key_id: pub}) is not None
    # 位元組被重格式化就驗不過（簽章涵蓋 sidecar_bytes，不重新解析後再簽）
    reformatted = serialize_json(json.loads(item.sidecar_bytes.decode("utf-8")))
    assert reformatted == item.sidecar_bytes
    assert verify_sidecar_bytes(reformatted + b" ", item.sig, {key_id: pub}) is None


def test_handoff_item_shape_and_signature(signer):
    key, key_id, pub = signer
    item = build_handoff_item(
        target_session_id="opencode:ses_1",
        continuation={"snapshot_sha256": SNAP, "message_id": "m1"},
        body={"content": "接著做設定檔", "title": "設定檔", "next_steps": ["加白名單"]},
        profile=PROFILE, key=key, key_id=key_id, case_id="case-1",
        now=T0,
    )
    sc = item.sidecar
    assert item.item_type == "handoff" and item.item_id.startswith("handoff:")
    assert item.raw_path is None and sc["raw"] is None
    assert sc["body"]["target_session_id"] == "opencode:ses_1"
    assert sc["body"]["continuation"] == {"snapshot_sha256": SNAP, "message_id": "m1"}
    assert sc["body"]["content"] == "接著做設定檔"
    assert sc["body"]["title"] == "設定檔"  # 結構化內容原樣帶著
    assert sc["metadata"]["case_id"] == "case-1"
    assert sc["metadata"]["created_at"] == sc["metadata"]["updated_at"] == T0
    _verify(item, pub, key_id)


@pytest.mark.parametrize(
    "body",
    [
        {},                    # 交接說明必填
        {"content": "   "},    # 純空白不算
    ],
)
def test_handoff_rejects_missing_content(signer, body):
    key, key_id, _ = signer
    with pytest.raises(InboxBuildError):
        build_handoff_item(
            target_session_id="opencode:ses_1",
            continuation={"snapshot_sha256": SNAP, "message_id": "m1"},
            body=body, profile=PROFILE, key=key, key_id=key_id, now=T0,
        )


def test_handoff_rejects_malformed_target(signer):
    """目標必須是 Session id（保留型態的 id 不行）——由 sidecar schema 擋下。"""
    key, key_id, _ = signer
    with pytest.raises(InboxBuildError):
        build_handoff_item(
            target_session_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
            continuation={"snapshot_sha256": SNAP, "message_id": "m1"},
            body={"content": "x"}, profile=PROFILE, key=key, key_id=key_id, now=T0,
        )


def test_handoff_body_continuation_is_never_taken_from_body(signer):
    """body 裡的 continuation 不會蓋掉明確參數（接續點只認呼叫端給的那一個）。"""
    key, key_id, _ = signer
    item = build_handoff_item(
        target_session_id="opencode:ses_1",
        continuation={"snapshot_sha256": SNAP, "message_id": "m1"},
        body={"content": "x", "continuation": {"snapshot_sha256": "b" * 64,
                                               "message_id": "m9"}},
        profile=PROFILE, key=key, key_id=key_id, now=T0,
    )
    assert item.sidecar["body"]["continuation"] == {
        "snapshot_sha256": SNAP, "message_id": "m1"}


def test_handoff_rejects_bad_continuation(signer):
    key, key_id, _ = signer
    with pytest.raises(InboxBuildError):
        build_handoff_item(
            target_session_id="opencode:ses_1",
            continuation={"snapshot_sha256": "NOTHEX", "message_id": "m1"},
            body={"content": "x"}, profile=PROFILE, key=key, key_id=key_id, now=T0,
        )
    with pytest.raises(InboxBuildError):
        build_handoff_item(
            target_session_id="opencode:ses_1",
            continuation={"snapshot_sha256": SNAP, "message_id": ""},
            body={"content": "x"}, profile=PROFILE, key=key, key_id=key_id, now=T0,
        )


def test_claim_and_reference_items(signer):
    key, key_id, pub = signer
    handoff_id = "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"
    claim = build_claim_item(
        handoff_id=handoff_id, claimer_session_id="opencode:ses_2",
        profile=PROFILE, key=key, key_id=key_id, now=T0,
    )
    assert claim.item_type == "claim" and claim.item_id.startswith("claim:")
    assert claim.sidecar["body"] == {
        "handoff_id": handoff_id, "claimer_session_id": "opencode:ses_2"}
    assert claim.raw_path is None
    _verify(claim, pub, key_id)

    ref = build_reference_item(
        from_session_id="opencode:ses_1", to_session_id="opencode:ses_2",
        read_snapshot_at=T0, profile=PROFILE, key=key, key_id=key_id, now=T0,
    )
    assert ref.item_type == "reference" and ref.item_id.startswith("reference:")
    assert ref.sidecar["body"]["read_snapshot_at"] == T0
    _verify(ref, pub, key_id)


def test_claim_rejects_malformed_handoff_id(signer):
    key, key_id, _ = signer
    with pytest.raises(InboxBuildError):
        build_claim_item(handoff_id="not-a-handoff", claimer_session_id="opencode:ses_2",
                         profile=PROFILE, key=key, key_id=key_id, now=T0)
    with pytest.raises(InboxBuildError):
        build_claim_item(handoff_id="handoff:XYZ", claimer_session_id="opencode:ses_2",
                         profile=PROFILE, key=key, key_id=key_id, now=T0)


def test_artifact_link_and_contained(signer, tmp_path: Path):
    key, key_id, pub = signer
    link = build_artifact_item(
        kind="link", produced_by_session_id="opencode:ses_1", name="summary",
        link="https://example.invalid/report", profile=PROFILE, key=key, key_id=key_id,
        now=T0,
    )
    assert link.item_type == "artifact" and link.raw_path is None
    assert link.sidecar["body"]["link"] == "https://example.invalid/report"
    assert link.sidecar["body"]["kind"] == "link"
    _verify(link, pub, key_id)

    payload = tmp_path / "summary.pdf"
    payload.write_bytes(b"%PDF-1.4 fake content")
    contained = build_artifact_item(
        kind="contained", produced_by_session_id="opencode:ses_1", name="summary.pdf",
        content_type="application/pdf", raw_path=payload, repo="org/repo",
        path="docs/summary.pdf", profile=PROFILE, key=key, key_id=key_id, now=T0,
    )
    assert contained.raw_path == payload
    assert contained.raw_sha256 == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert contained.raw_size == payload.stat().st_size
    assert contained.sidecar["body"]["content_type"] == "application/pdf"
    assert contained.sidecar["body"]["link"] is None
    _verify(contained, pub, key_id)


def test_artifact_kind_rules(signer, tmp_path: Path):
    key, key_id, _ = signer
    with pytest.raises(InboxBuildError):  # link 必須給 link
        build_artifact_item(kind="link", produced_by_session_id="opencode:ses_1",
                            name="x", profile=PROFILE, key=key, key_id=key_id, now=T0)
    with pytest.raises(InboxBuildError):  # contained 必須給本體
        build_artifact_item(kind="contained", produced_by_session_id="opencode:ses_1",
                            name="x", content_type="application/pdf",
                            profile=PROFILE, key=key, key_id=key_id, now=T0)
    with pytest.raises(InboxBuildError):  # contained 必須給 content_type
        build_artifact_item(kind="contained", produced_by_session_id="opencode:ses_1",
                            name="x", raw_path=tmp_path / "nope.bin",
                            profile=PROFILE, key=key, key_id=key_id, now=T0)
    with pytest.raises(InboxBuildError):  # kind 只有兩種
        build_artifact_item(kind="inline", produced_by_session_id="opencode:ses_1",
                            name="x", profile=PROFILE, key=key, key_id=key_id, now=T0)


def test_key_id_must_match_profile(signer):
    key, key_id, _ = signer
    with pytest.raises(InboxBuildError):
        build_claim_item(handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                         claimer_session_id="opencode:ses_2",
                         profile="other-profile", key=key, key_id=key_id, now=T0)
    with pytest.raises(InboxBuildError):  # key_id 形狀不對
        build_claim_item(handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                         claimer_session_id="opencode:ses_2",
                         profile=PROFILE, key=key, key_id="mac-opencode", now=T0)


def test_upload_order_is_raw_sidecar_sig(signer, tmp_path: Path):
    """D2 的不可分單位：上傳順序固定 raw → sidecar → sig。"""
    key, key_id, _ = signer
    payload = tmp_path / "out.bin"
    payload.write_bytes(b"binary")
    items = [
        build_handoff_item(
            target_session_id="opencode:ses_1",
            continuation={"snapshot_sha256": SNAP, "message_id": "m1"},
            body={"content": "交出去"}, profile=PROFILE, key=key, key_id=key_id, now=T0),
        build_artifact_item(
            kind="contained", produced_by_session_id="opencode:ses_1", name="out.bin",
            content_type="application/octet-stream", raw_path=payload,
            profile=PROFILE, key=key, key_id=key_id, now=T0),
    ]
    drive = FakeDrive()
    folder = drive.seed_folder("inbox")
    for item in items:
        drive.calls.clear()
        ids = upload_item(drive, folder, item)
        names = [drive.get(i).name for i in ids]
        expected = [n for n in item.file_names() if not n.endswith(".raw")] if item.raw_path is None \
            else list(item.file_names())
        assert names == expected
        # 順序：raw 在最前，sig 在最後
        assert names[-1].endswith(".sig")
        if item.raw_path is not None:
            assert names[0].endswith(".raw")
    uploaded = sorted(f.name for f in drive.list_children(folder))
    assert len(uploaded) == 5  # handoff 2 個 ＋ artifact 3 個


def test_uploaded_sig_is_byte_identical(signer):
    key, key_id, pub = signer
    item = build_reference_item(
        from_session_id="opencode:ses_1", to_session_id="opencode:ses_2",
        read_snapshot_at=T0, profile=PROFILE, key=key, key_id=key_id, now=T0)
    drive = FakeDrive()
    folder = drive.seed_folder("inbox")
    ids = upload_item(drive, folder, item)
    sig_bytes = drive.download_bytes(ids[1], max_bytes=1 << 20)
    assert sig_bytes == serialize_json(item.sig)
    # 上傳後的位元組仍可通過驗章（端到端沒有重新格式化）
    sidecar_bytes = drive.download_bytes(ids[0], max_bytes=1 << 20)
    assert sidecar_bytes == item.sidecar_bytes
    assert verify_sidecar_bytes(sidecar_bytes, item.sig, {key_id: pub}) is not None
    assert item.sig == sign_sidecar_bytes(item.sidecar_bytes, key, key_id)


def test_builder_refuses_stopped_when_a_message_comes_after_the_archive(tmp_path: Path):
    """同步器之外，builder 自己也要擋「封存之後還有訊息被建立」的 stopped 宣告。

    這是底層的最後一道：就算呼叫端自己猜成 stopped，也不該產出這種項目
    （review-g5-6 H3）。
    """
    from aistorage.converters.base import SessionFacts
    from aistorage.inbox_builder import InboxBuildError, build_inbox_item

    raw = tmp_path / "raw.json"
    raw.write_text("{}", encoding="utf-8")
    key = bytes(range(32))
    facts = SessionFacts(
        title="主線", created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T09:00:00Z", message_ids=("m0",),
        archived_at="2026-09-27T08:30:00Z", last_message_at="2026-09-27T09:00:00Z",
        in_progress=True,                       # 回覆還在生成中
        archived_ms=1788000000000,
        last_message_ms=1788003600000,          # completed 在封存之後
        last_message_created_ms=1788001800000,  # created 在封存之後 → 新訊息
    )
    with pytest.raises(InboxBuildError) as e:
        build_inbox_item(
            raw, source="opencode", source_session_id="ses_1", facts=facts,
            profile="mac-opencode", key=key, key_id="mac-opencode-abcdef12",
            status="stopped", stopped_at="2026-09-27T09:00:00Z",
            snapshot_at="2026-09-27T09:00:00Z", now="2026-09-27T09:00:00Z",
            archive_ms=1788000000000,
        )
    assert "封存之後" in str(e.value)

    # created 在封存**之前**（宣告停止時生成中的那一則回覆）→ 允許 stopped
    ok = SessionFacts(
        title="主線", created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T09:00:00Z", message_ids=("m0",),
        archived_at="2026-09-27T08:30:00Z", last_message_at="2026-09-27T09:00:00Z",
        in_progress=True,
        archived_ms=1788000000000,
        last_message_ms=1788003600000,          # completed 在封存之後（舊的規則會誤判）
        last_message_created_ms=1787999000000,  # created 在封存之前
    )
    sidecar, _sig = build_inbox_item(
        raw, source="opencode", source_session_id="ses_1", facts=ok,
        profile="mac-opencode", key=key, key_id="mac-opencode-abcdef12",
        status="stopped", stopped_at="2026-09-27T09:00:00Z",
        snapshot_at="2026-09-27T09:00:00Z", now="2026-09-27T09:00:00Z",
        archive_ms=1788000000000,
    )
    assert json.loads(sidecar.decode("utf-8"))["session"]["status"] == "stopped"
