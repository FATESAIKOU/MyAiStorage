"""Independent Acceptance Tests for Foundry (Task 7.1 - 7.4).

Adheres strictly to:
- docs/impl/group5-7-modules.md §6
- specs/foundry/catalog
- Catalog query by type / case_id / producer / time / producing session
- 100 MiB upper limit rejection (too_large)
- Fetch body by annex key (with sha256 checksum verification)
- Link type provenance (origin)
- Zero reliance on internal implementation details of src/
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aistorage.agora import layout as agora_layout
from aistorage.agora.store import AgoraStore, FakeRawStorage
from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import MismatchError
from aistorage.foundry.apply import apply_artifact
from aistorage.foundry.index import ArtifactRow, FoundryIndexMeta, build_foundry_index
from aistorage.foundry.store import FoundryStore
from aistorage.identity import Registry
from aistorage.inbox_builder import build_artifact_item, upload_item
from aistorage.intake.evaluate import DecisionKind, evaluate
from aistorage.intake.ledger import Ledger
from aistorage.intake.scan import scan_inboxes
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.reader.foundry import FoundryReader
from aistorage.schema import generate_ulid


T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"
T2 = "2026-09-28T10:00:00.000Z"
TEST_PRIV_KEY = b"F" * 32
PROFILE = "mac-worker"


@pytest.fixture
def foundry_env(tmp_path: Path):
    """建立測試所需之基礎環境、Registry、AgoraStore 與 FoundryStore。"""
    clock = FixedClock(T1)

    # 金鑰生成與註冊表建立
    priv = Ed25519PrivateKey.from_private_bytes(TEST_PRIV_KEY)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    pub_b64 = base64.b64encode(pub).decode("ascii")
    key_id = f"{PROFILE}-{hashlib.sha256(pub).hexdigest()[:8]}"

    drive = FakeDrive()
    inbox_fid = drive.seed_folder("inbox_test")

    registry = Registry({
        "format": "aistorage.registry/v1",
        "profiles": {
            PROFILE: {
                "holder": "agent-alice",
                "inbox_folder_ids": [inbox_fid],
                "allowed_types": ["artifact", "session"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "created_at": "2026-09-01T00:00:00Z",
                    }
                ],
            }
        },
    })

    # Agora 真本環境
    agora_wt = tmp_path / "agora_worktree"
    agora_wt.mkdir(parents=True, exist_ok=True)
    agora_git = FakeAnnexGit(workdir=agora_wt)
    agora_store = AgoraStore(
        agora_wt,
        raw_storage=FakeRawStorage(),
        git=agora_git,
        temp_dir=tmp_path / "agora_tmp",
    )

    # 預先在 Agora 中建立一個合法 Session
    valid_session_id = "opencode:ses_producer"
    session_record = {
        "id": valid_session_id,
        "type": "session",
        "source": "opencode",
        "source_session_id": "ses_producer",
        "title": "Producing Session",
        "producer": f"profile:{PROFILE}",
        "status": "running",
        "created_at": T0,
        "updated_at": T0,
        "snapshot_at": T0,
        "committed_at": T0,
        "last_item_key": generate_ulid(),
        "raw_sha256": "0" * 64,
        "raw_size": 100,
        "case_id": "case_100",
    }
    agora_store.put_json(agora_layout.session_meta_path(valid_session_id), session_record)

    # Foundry 真本環境
    foundry_wt = tmp_path / "foundry_worktree"
    foundry_wt.mkdir(parents=True, exist_ok=True)
    foundry_git = FakeAnnexGit(workdir=foundry_wt)
    foundry_store = FoundryStore(foundry_wt, git=foundry_git, temp_dir=tmp_path / "foundry_tmp")

    ledger = Ledger(agora_store)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    return {
        "clock": clock,
        "key": TEST_PRIV_KEY,
        "key_id": key_id,
        "registry": registry,
        "drive": drive,
        "agora_store": agora_store,
        "foundry_store": foundry_store,
        "ledger": ledger,
        "workdir": workdir,
        "session_id": valid_session_id,
        "inbox_folder_id": inbox_fid,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Acceptance Tests: Intake & Evaluate Artifact Dispatch
# ---------------------------------------------------------------------------


def _second_artifact_item(env):
    """再上傳一個新的 artifact 項目（第一個已被記錄成拒收，不能重用）。"""
    payload2 = env["tmp_path"] / "model2.bin"
    payload2.write_bytes(b"binary weights data 2")
    upload_item(
        env["drive"],
        env["inbox_folder_id"],
        build_artifact_item(
            kind="contained",
            produced_by_session_id=env["session_id"],
            name="model2.bin",
            content_type="application/octet-stream",
            raw_path=payload2,
            profile=PROFILE,
            key=env["key"],
            key_id=env["key_id"],
            clock=env["clock"],
        ),
    )
    others = [it for it in scan_inboxes(env["drive"], env["registry"]).items]
    return others[-1]


def test_evaluate_artifact_dispatch_defer_and_accept(foundry_env):
    """驗證收件匣 evaluate 對 artifact 的分派：未啟用 Foundry 時 REJECT，啟用時 ACCEPT。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    payload = env["tmp_path"] / "model.bin"
    payload.write_bytes(b"binary weights data")

    built_item = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="model.bin",
        content_type="application/octet-stream",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )

    upload_item(drive, env["inbox_folder_id"], built_item)
    inbox_items = scan_inboxes(drive, env["registry"]).items
    assert len(inbox_items) == 1
    item = inbox_items[0]

    # 1. 未啟用 Foundry -> REJECT(foundry_not_enabled)
    dec_disabled = evaluate(
        item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=False,
    )
    # PM 指示：Foundry 沒設定時 REJECT 而非 DEFER（DEFER 會永遠留在收件匣裡）
    assert dec_disabled.kind == DecisionKind.REJECT
    assert dec_disabled.code == "foundry_not_enabled"

    # 2. 同一個 item_key 再評估一次仍然是同一個拒收（真本記錄了拒收原因）
    dec_again = evaluate(
        item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )
    assert dec_again.kind == DecisionKind.REJECT
    assert dec_again.code == "foundry_not_enabled"

    # 3. 新項目 + 啟用 Foundry -> ACCEPT(ok)
    dec_accept = evaluate(
        _second_artifact_item(env),
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )
    assert dec_accept.kind == DecisionKind.ACCEPT
    assert dec_accept.code == "ok"
    assert dec_accept.producer == f"profile:{PROFILE}"
    assert dec_accept.raw_path is not None
    assert dec_accept.raw_path.is_file()


def test_evaluate_artifact_100_mib_limit_rejection(foundry_env):
    """驗證 100 MiB 上限拒收: 超過上限時 REJECT(too_large)。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    # 模擬超過上限的檔案（測試時傳入極限值 50 bytes，並構造大於 50 bytes 的檔案）
    payload = env["tmp_path"] / "large_dataset.bin"
    payload.write_bytes(b"A" * 150)

    built_item = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="large_dataset.bin",
        content_type="application/octet-stream",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )

    upload_item(drive, env["inbox_folder_id"], built_item)
    inbox_items = scan_inboxes(drive, env["registry"]).items
    item = inbox_items[0]

    dec = evaluate(
        item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        max_raw=50,  # 設定上限 50 位元組
        foundry_enabled=True,
    )
    assert dec.kind == DecisionKind.REJECT
    assert dec.code == "too_large"
    assert dec.authenticated is True


# ---------------------------------------------------------------------------
# Acceptance Tests: Apply Artifact (Contained & Link)
# ---------------------------------------------------------------------------


def test_apply_artifact_contained_and_validation(foundry_env):
    """驗證 apply_artifact 對 contained 產出入庫、驗證 produced_by_session_id 與持有者。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    payload = env["tmp_path"] / "chart.png"
    payload.write_bytes(b"\x89PNG\r\n\x1a\nfake_image_bytes")

    item = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="chart.png",
        content_type="image/png",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )
    upload_item(drive, env["inbox_folder_id"], item)
    inbox_item = scan_inboxes(drive, env["registry"]).items[0]
    dec = evaluate(
        inbox_item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )

    res = apply_artifact(env["foundry_store"], dec, env["agora_store"], env["clock"])
    assert res.ok is True
    assert res.code == "ok"
    assert any("catalog/" in p for p in res.paths)
    assert any("objects/" in p for p in res.paths)

    art_id = dec.record_metadata["id"]
    ulid = art_id.split(":", 1)[1]
    cat = env["foundry_store"].get_catalog(ulid)
    assert cat is not None
    assert cat["name"] == "chart.png"
    assert cat["kind"] == "contained"
    assert cat["annex_key"] is not None
    assert cat["annex_key"].startswith("SHA256E-")
    assert cat["produced_by_session_id"] == env["session_id"]


def test_apply_artifact_rejects_non_existent_session(foundry_env):
    """驗證 apply_artifact 拒絕不存在於 Agora 的 produced_by_session_id (session_not_found)。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    payload = env["tmp_path"] / "file.txt"
    payload.write_text("content")

    item = build_artifact_item(
        kind="contained",
        produced_by_session_id="opencode:non_existent_session_id",
        name="file.txt",
        content_type="text/plain",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )
    upload_item(drive, env["inbox_folder_id"], item)
    inbox_item = scan_inboxes(drive, env["registry"]).items[0]
    dec = evaluate(
        inbox_item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )

    res = apply_artifact(env["foundry_store"], dec, env["agora_store"], env["clock"])
    assert res.ok is False
    assert res.code == "session_not_found"


def test_apply_artifact_link_type_provenance(foundry_env):
    """驗證 link 型產出入庫時寫入出處 (repo, path, link)，且不建立 objects 實體檔案。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    item_link = build_artifact_item(
        kind="link",
        produced_by_session_id=env["session_id"],
        name="spec_document",
        link="https://github.com/my-org/my-project/blob/main/spec.md",
        repo="my-org/my-project",
        path="spec.md",
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )
    upload_item(drive, env["inbox_folder_id"], item_link)
    inbox_item = scan_inboxes(drive, env["registry"]).items[0]
    dec = evaluate(
        inbox_item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )

    res = apply_artifact(env["foundry_store"], dec, env["agora_store"], env["clock"])
    assert res.ok is True
    assert res.code == "ok"
    # link 型不應建立 objects/ 目錄物件
    assert not any("objects/" in p for p in res.paths)

    art_id = dec.record_metadata["id"]
    ulid = art_id.split(":", 1)[1]
    cat = env["foundry_store"].get_catalog(ulid)
    assert cat["kind"] == "link"
    assert cat["link"] == "https://github.com/my-org/my-project/blob/main/spec.md"
    assert cat["repo"] == "my-org/my-project"
    assert cat["path"] == "spec.md"


# ---------------------------------------------------------------------------
# Acceptance Tests: Foundry Index & FoundryReader (find & get)
# ---------------------------------------------------------------------------


def setup_foundry_readview(env: dict[str, Any], artifacts: list[ArtifactRow]) -> FoundryReader:
    """小幫手：建置 Foundry index 並配置 FoundryReader。"""
    tmp_path: Path = env["tmp_path"]
    drive: FakeDrive = env["drive"]
    clock: FixedClock = env["clock"]

    index_db_path = tmp_path / f"foundry_index_{generate_ulid()}.sqlite"
    build_foundry_index(
        index_db_path,
        artifacts=artifacts,
        meta=FoundryIndexMeta(
            generation=1,
            built_at=T1,
            foundry_main_sha="main_commit_123",
        ),
    )

    index_file = drive.create(env["inbox_folder_id"], "index-g1.sqlite", index_db_path)
    manifest_data = {
        "format": "aistorage.readview/v1",
        "element": "foundry",
        "generation": 1,
        "published_at": T1,
        "agora_main_sha": "main_commit_123",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {
            "id": index_file.id,
            "sha256": hashlib.sha256(index_db_path.read_bytes()).hexdigest(),
            "size": index_db_path.stat().st_size,
        },
        "files": [index_file.id],
        "retired": [],
    }
    manifest_file = drive.create(
        env["inbox_folder_id"],
        "readview-manifest.json",
        (json.dumps(manifest_data) + "\n").encode("utf-8"),
    )

    cfg = ReaderConfig(
        manifest_file_id=manifest_file.id,
        sa_key_path=tmp_path / "sa.json",
        cache_dir=tmp_path / "reader_cache",
    )
    rv_client = ReadViewClient(drive, cfg, clock=clock)
    return FoundryReader(rv_client, drive=drive, clock=clock)


def test_foundry_reader_find_by_type_case_producer_time_and_session(foundry_env):
    """驗證 FoundryReader.find: 依型態、所屬案件、產生者、時間、產出它的 Session 查詢。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    obj_file = drive.create(env["inbox_folder_id"], "f1.bin", b"content1")
    art1 = ArtifactRow(
        artifact_id=f"artifact:{generate_ulid()}",
        kind="contained",
        content_type="application/pdf",
        name="doc.pdf",
        producer="agent-alice",
        case_id="case_alpha",
        produced_by_session_id="opencode:session_1",
        created_at="2026-09-28T08:30:00.000Z",
        updated_at="2026-09-28T08:30:00.000Z",
        size=8,
        sha256=hashlib.sha256(b"content1").hexdigest(),
        annex_key="SHA256E-s8--content1.pdf",
        object_file_id=obj_file.id,
    )

    art2 = ArtifactRow(
        artifact_id=f"artifact:{generate_ulid()}",
        kind="link",
        content_type="text/markdown",
        name="notes.md",
        producer="agent-bob",
        case_id="case_beta",
        produced_by_session_id="opencode:session_2",
        created_at="2026-09-28T09:30:00.000Z",
        updated_at="2026-09-28T09:30:00.000Z",
        link="https://github.com/my-org/repo/blob/main/notes.md",
    )

    reader = setup_foundry_readview(env, [art1, art2])

    # 1. 依 kind / type 查詢
    res_kind = reader.find(kind="contained").value
    assert len(res_kind) == 1
    assert res_kind[0].artifact.name == "doc.pdf"

    res_type = reader.find(content_type="text/markdown").value
    assert len(res_type) == 1
    assert res_type[0].artifact.name == "notes.md"

    # 2. 依 case_id 查詢
    res_case = reader.find(case_id="case_alpha").value
    assert len(res_case) == 1
    assert res_case[0].artifact.artifact_id == art1.artifact_id

    # 3. 依 producer 查詢
    res_prod = reader.find(producer="agent-bob").value
    assert len(res_prod) == 1
    assert res_prod[0].artifact.producer == "agent-bob"

    # 4. 依 session_id (產出它的 Session) 查詢
    res_sess = reader.find(session_id="opencode:session_1").value
    assert len(res_sess) == 1
    assert res_sess[0].artifact.produced_by_session_id == "opencode:session_1"

    # 5. 依時間 (since / until) 查詢
    res_time = reader.find(since="2026-09-28T09:00:00.000Z").value
    assert len(res_time) == 1
    assert res_time[0].artifact.name == "notes.md"


def test_foundry_reader_get_contained_by_annex_key_with_hash_verification(foundry_env):
    """驗證 FoundryReader.get: 依 annex key / object_file_id 取得本體並驗證雜湊一致性。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]
    tmp_path: Path = env["tmp_path"]

    content = b"correct artifact payload data 12345"
    sha = hashlib.sha256(content).hexdigest()
    obj_file = drive.create(env["inbox_folder_id"], "object.bin", content)

    art_id = f"artifact:{generate_ulid()}"
    art = ArtifactRow(
        artifact_id=art_id,
        kind="contained",
        content_type="application/octet-stream",
        name="weights.bin",
        producer="agent-alice",
        case_id="case_100",
        produced_by_session_id=env["session_id"],
        created_at=T1,
        updated_at=T1,
        size=len(content),
        sha256=sha,
        annex_key=f"SHA256E-s{len(content)}--{sha}.bin",
        object_file_id=obj_file.id,
    )

    reader = setup_foundry_readview(env, [art])

    # 1. 下載至檔案目的地
    dest = tmp_path / "fetched_weights.bin"
    res = reader.get(art_id, dest=dest)
    assert res.value.kind == "contained"
    assert res.value.path == dest
    assert dest.read_bytes() == content

    # 2. 直接讀入記憶體
    res_mem = reader.get(art_id)
    assert res_mem.value.kind == "contained"
    assert res_mem.value.data == content


def test_foundry_reader_get_corrupted_object_raises_mismatch(foundry_env):
    """驗證 FoundryReader.get: 當特殊遠端物件之雜湊與 index 不符時拋出 MismatchError。"""
    env = foundry_env
    drive: FakeDrive = env["drive"]

    # 遠端放的是損毀的內容
    corrupted_file = drive.create(env["inbox_folder_id"], "bad.bin", b"tampered data")

    art_id = f"artifact:{generate_ulid()}"
    art = ArtifactRow(
        artifact_id=art_id,
        kind="contained",
        content_type="application/octet-stream",
        name="secure.bin",
        producer="agent-alice",
        case_id="case_100",
        produced_by_session_id=env["session_id"],
        created_at=T1,
        updated_at=T1,
        size=13,
        sha256="f" * 64,  # 不相符之預期雜湊
        annex_key=f"SHA256E-s13--{'f'*64}.bin",
        object_file_id=corrupted_file.id,
    )

    reader = setup_foundry_readview(env, [art])

    with pytest.raises(MismatchError, match="雜湊不符"):
        reader.get(art_id)


def test_foundry_reader_get_link_origin(foundry_env):
    """驗證 FoundryReader.get: link 型產出回傳出處 (origin)。"""
    env = foundry_env

    art_id = f"artifact:{generate_ulid()}"
    art = ArtifactRow(
        artifact_id=art_id,
        kind="link",
        content_type="text/plain",
        name="external_repo",
        producer="agent-alice",
        case_id="case_100",
        produced_by_session_id=env["session_id"],
        created_at=T1,
        updated_at=T1,
        link="https://github.com/external/repo",
        repo="external/repo",
        path="README.md",
    )

    reader = setup_foundry_readview(env, [art])

    res = reader.get(art_id)
    assert res.value.kind == "link"
    assert res.value.origin is not None
    assert res.value.origin["link"] == "https://github.com/external/repo"
    assert res.value.origin["repo"] == "external/repo"
    assert res.value.origin["path"] == "README.md"
