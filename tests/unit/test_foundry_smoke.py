"""Foundry 最小實作冒煙測試（Group 7 tasks 7.2〜7.4）。

測試範圍：
1. 項目格式與組裝（build_artifact_item：link 與 contained）
2. evaluate 分派（未啟用時 DEFER、啟用時 ACCEPT、>100 MiB 拒收 too_large）
3. apply_artifact（Session 存在性檢查、持有者驗證、contained 物件入庫與 catalog JSON 寫入）
4. Foundry index 建置（build_foundry_index）與讀取端（FoundryReader.find、FoundryReader.get）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aistorage.agora.store import AgoraStore, FakeRawStorage
from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import FixedClock, format_rfc3339
from aistorage.drive.fake import FakeDrive
from aistorage.drive.model import DriveFile
from aistorage.errors import MismatchError
from aistorage.foundry.apply import apply_artifact
from aistorage.foundry.index import ArtifactRow, FoundryIndexMeta, build_foundry_index
from aistorage.foundry.layout import catalog_path, object_path, sanitize_filename
from aistorage.foundry.store import FoundryStore
from aistorage.identity import Registry
from aistorage.inbox import sign_sidecar_bytes
from aistorage.inbox_builder import build_artifact_item, upload_item
from aistorage.intake.evaluate import DecisionKind, evaluate, sort_accepted_decisions
from aistorage.intake.ledger import Ledger
from aistorage.intake.scan import scan_inboxes
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.reader.foundry import FoundryReader
from aistorage.schema import generate_ulid

# 測試用 Ed25519 金鑰對（固定測試金鑰，32 位元組）
TEST_PRIV_KEY = b"K" * 32
PROFILE = "test-worker"


@pytest.fixture
def test_setup(tmp_path: Path):
    """建立測試所需之基礎環境與 Registry。"""
    clock = FixedClock("2026-09-28T09:00:00.000Z")

    priv = Ed25519PrivateKey.from_private_bytes(TEST_PRIV_KEY)
    raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    import base64
    pub_b64 = base64.b64encode(pub).decode("ascii")
    pub_sha_prefix = hashlib.sha256(pub).hexdigest()[:8]
    key_id = f"{PROFILE}-{pub_sha_prefix}"

    drive = FakeDrive()
    inbox_fid = drive.seed_folder("inbox_test")

    reg_data = {
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
                        "revoked_at": None,
                    }
                ],
            }
        },
    }
    registry = Registry(reg_data)

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

    from aistorage.agora import layout as agora_layout

    # 在 Agora 內先植入一個合法 Session（作為產出的 produced_by_session_id）
    valid_session_id = "opencode:session_test_1"
    session_record = {
        "id": valid_session_id,
        "type": "session",
        "source": "opencode",
        "source_session_id": "session_test_1",
        "title": "Test Session",
        "producer": f"profile:{PROFILE}",
        "status": "running",
        "created_at": "2026-09-28T08:00:00.000Z",
        "updated_at": "2026-09-28T08:30:00.000Z",
        "snapshot_at": "2026-09-28T08:30:00.000Z",
        "committed_at": "2026-09-28T08:30:00.000Z",
        "last_item_key": "01M3JJ8E928CKBJSVXC9FRY6KF",
        "raw_sha256": "0" * 64,
        "raw_size": 100,
        "case_id": "case_101",
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


def test_sanitize_filename():
    """驗證檔名安全化工具。"""
    assert sanitize_filename("normal_file.txt") == "normal_file.txt"
    assert sanitize_filename("../../../etc/passwd") == "passwd"
    assert sanitize_filename("special:name*?.bin") == "special_name__.bin"
    assert sanitize_filename("") == "object.bin"
    assert sanitize_filename("..") == "object.bin"


def test_build_artifact_item(test_setup):
    """驗證組裝 artifact 項目（link 與 contained）。"""
    env = test_setup
    payload = env["tmp_path"] / "data.csv"
    payload.write_text("a,b,c\n1,2,3\n")

    # 1. Contained artifact
    item_contained = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="data.csv",
        content_type="text/csv",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        description="CSV Report",
        clock=env["clock"],
    )
    assert item_contained.item_type == "artifact"
    assert item_contained.raw_path == payload
    assert item_contained.sidecar["body"]["kind"] == "contained"
    assert item_contained.sidecar["body"]["name"] == "data.csv"
    assert item_contained.sidecar["body"]["filename"] == "data.csv"
    assert item_contained.sidecar["body"]["description"] == "CSV Report"
    assert item_contained.sidecar["body"]["produced_by_session_id"] == env["session_id"]

    # 2. Link artifact
    item_link = build_artifact_item(
        kind="link",
        produced_by_session_id=env["session_id"],
        name="external_doc",
        link="https://example.com/doc",
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        repo="my-org/my-repo",
        path="docs/spec.md",
        clock=env["clock"],
    )
    assert item_link.item_type == "artifact"
    assert item_link.raw_path is None
    assert item_link.sidecar["body"]["kind"] == "link"
    assert item_link.sidecar["body"]["link"] == "https://example.com/doc"
    assert item_link.sidecar["body"]["repo"] == "my-org/my-repo"
    assert item_link.sidecar["body"]["path"] == "docs/spec.md"


def test_evaluate_artifact_dispatch(test_setup):
    """驗證 evaluate 對 artifact 的分派：未啟用 REJECT、啟用時 ACCEPT。

    注意兩者用的是**不同的項目**：REJECT 會把拒收原因寫進真本
    （`_committer/rejections/<item_key>.json`），所以同一個 item_key 再評估一次
    一定還是同一個拒收（g3d 的設計：避免同一個項目每一輪重新評估）。寫入者要重試
    就得重新上傳一個新的項目。
    """
    env = test_setup
    drive = env["drive"]

    payload = env["tmp_path"] / "report.pdf"
    payload.write_bytes(b"%PDF-fake-content")

    built_item = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="report.pdf",
        content_type="application/pdf",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )

    upload_item(drive, env["inbox_folder_id"], built_item)
    inbox_items = scan_inboxes(drive, env["registry"]).items
    assert len(inbox_items) == 1
    inbox_item = inbox_items[0]

    # 1. 未啟用 Foundry（預設 foundry_enabled=False）-> REJECT(foundry_not_enabled)
    dec_disabled = evaluate(
        inbox_item,
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

    # 同一個項目再評估一次（即使 Foundry 已啟用）→ 仍然是同一個拒收（真本有記錄）
    dec_again = evaluate(
        inbox_item,
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

    # 2. 換一個新項目，Foundry 啟用（foundry_enabled=True）-> ACCEPT(ok)
    payload2 = env["tmp_path"] / "report2.pdf"
    payload2.write_bytes(b"%PDF-fake-content-2")
    upload_item(
        drive,
        env["inbox_folder_id"],
        build_artifact_item(
            kind="contained",
            produced_by_session_id=env["session_id"],
            name="report2.pdf",
            content_type="application/pdf",
            raw_path=payload2,
            profile=PROFILE,
            key=env["key"],
            key_id=env["key_id"],
            clock=env["clock"],
        ),
    )
    fresh_items = [
        it for it in scan_inboxes(drive, env["registry"]).items
        if it.item_key != inbox_item.item_key
    ]
    assert len(fresh_items) == 1
    dec_accept = evaluate(
        fresh_items[0],
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

    # 驗證 sort_accepted_decisions 排序支援 artifact（次序在 6）
    sorted_decs = sort_accepted_decisions([dec_accept])
    assert len(sorted_decs) == 1


def test_evaluate_artifact_too_large(test_setup):
    """驗證 contained artifact 超過上限時被拒收 (too_large)。"""
    env = test_setup
    drive = env["drive"]

    payload = env["tmp_path"] / "big.bin"
    payload.write_bytes(b"x" * 200)

    built_item = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="big.bin",
        content_type="application/octet-stream",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )

    upload_item(drive, env["inbox_folder_id"], built_item)
    inbox_items = scan_inboxes(drive, env["registry"]).items
    inbox_item = inbox_items[0]

    # 設定 max_raw = 100 位元組（小於 200 位元組）
    dec = evaluate(
        inbox_item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        max_raw=100,
        foundry_enabled=True,
    )
    assert dec.kind == DecisionKind.REJECT
    assert dec.code == "too_large"
    assert dec.authenticated is True


def test_apply_artifact_contained_and_link(test_setup):
    """驗證 apply_artifact 之兩階段檢驗與寫入。"""
    env = test_setup
    drive = env["drive"]

    payload = env["tmp_path"] / "output.json"
    payload.write_text('{"result": 42}')

    # 1. 成功套用 contained artifact
    item_contained = build_artifact_item(
        kind="contained",
        produced_by_session_id=env["session_id"],
        name="output.json",
        content_type="application/json",
        raw_path=payload,
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )
    upload_item(drive, env["inbox_folder_id"], item_contained)
    inbox_items = scan_inboxes(drive, env["registry"]).items
    dec = evaluate(
        inbox_items[0],
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )
    assert dec.kind == DecisionKind.ACCEPT

    res = apply_artifact(env["foundry_store"], dec, env["agora_store"], env["clock"])
    assert res.ok is True
    assert res.code == "ok"
    assert any("catalog/" in p for p in res.paths)
    assert any("objects/" in p for p in res.paths)

    art_id = dec.record_metadata["id"]
    ulid = art_id.split(":", 1)[1]
    cat = env["foundry_store"].get_catalog(ulid)
    assert cat is not None
    assert cat["name"] == "output.json"
    assert cat["kind"] == "contained"
    assert cat["annex_key"] is not None
    assert cat["annex_key"].startswith("SHA256E-s14--")
    assert cat["produced_by_session_id"] == env["session_id"]
    assert cat["producer"] == f"profile:{PROFILE}"

    # 2. 測試 session_not_found
    dec_bad_sess = Mock(
        item=dec.item,
        sidecar={
            "metadata": {"id": f"artifact:{generate_ulid()}"},
            "body": {
                "kind": "contained",
                "produced_by_session_id": "opencode:non_existent_session",
                "name": "out.bin",
            },
        },
        record_metadata={"id": f"artifact:{generate_ulid()}"},
        producer="agent-alice",
        raw_path=payload,
    )
    res_bad = apply_artifact(env["foundry_store"], dec_bad_sess, env["agora_store"], env["clock"])
    assert res_bad.ok is False
    assert res_bad.code == "session_not_found"

    # 3. 測試 unauthorized（產出者不是 Session 持有者）
    dec_unauth = Mock(
        item=dec.item,
        sidecar={
            "metadata": {"id": f"artifact:{generate_ulid()}"},
            "body": {
                "kind": "contained",
                "produced_by_session_id": env["session_id"],
                "name": "out.bin",
            },
        },
        record_metadata={"id": f"artifact:{generate_ulid()}"},
        producer="agent-bob",  # 與 session producer "agent-alice" 不符
        raw_path=payload,
    )
    res_unauth = apply_artifact(env["foundry_store"], dec_unauth, env["agora_store"], env["clock"])
    assert res_unauth.ok is False
    assert res_unauth.code == "unauthorized"

    # 4. 成功套用 link artifact
    item_link = build_artifact_item(
        kind="link",
        produced_by_session_id=env["session_id"],
        name="wiki_link",
        link="https://wiki.example.com",
        profile=PROFILE,
        key=env["key"],
        key_id=env["key_id"],
        clock=env["clock"],
    )
    upload_item(drive, env["inbox_folder_id"], item_link)
    inbox_items = scan_inboxes(drive, env["registry"]).items
    # 找到剛上傳的 link item
    link_item = [i for i in inbox_items if i.item_key == item_link.item_key][0]
    dec_link = evaluate(
        link_item,
        drive=drive,
        registry=env["registry"],
        store=env["agora_store"],
        ledger=env["ledger"],
        clock=env["clock"],
        workdir=env["workdir"],
        foundry_enabled=True,
    )
    assert dec_link.kind == DecisionKind.ACCEPT
    res_link = apply_artifact(env["foundry_store"], dec_link, env["agora_store"], env["clock"])
    assert res_link.ok is True
    assert res_link.code == "ok"
    # link 產出不應包含 objects 路徑
    assert not any("objects/" in p for p in res_link.paths)


def test_foundry_index_and_reader(test_setup):
    """驗證 Foundry 索引建置與 FoundryReader 查詢及取檔。"""
    env = test_setup
    tmp_path = env["tmp_path"]
    clock = env["clock"]
    drive = env["drive"]

    # 準備一個 contained 物件至 Drive
    obj_content = b"model binary weights 12345"
    obj_sha = hashlib.sha256(obj_content).hexdigest()
    drive_obj = drive.create(env["inbox_folder_id"], "object.bin", obj_content)

    art1_ulid = generate_ulid()
    art1_id = f"artifact:{art1_ulid}"
    art1 = ArtifactRow(
        artifact_id=art1_id,
        kind="contained",
        content_type="application/octet-stream",
        name="weights.bin",
        producer="agent-alice",
        case_id="case_101",
        produced_by_session_id=env["session_id"],
        created_at="2026-09-28T09:00:00.000Z",
        updated_at="2026-09-28T09:00:00.000Z",
        size=len(obj_content),
        sha256=obj_sha,
        annex_key=f"SHA256E-s{len(obj_content)}--{obj_sha}.bin",
        object_file_id=drive_obj.id,
    )

    art2_ulid = generate_ulid()
    art2_id = f"artifact:{art2_ulid}"
    art2 = ArtifactRow(
        artifact_id=art2_id,
        kind="link",
        content_type="text/markdown",
        name="docs",
        producer="agent-bob",
        case_id="case_202",
        produced_by_session_id="opencode:session_test_2",
        created_at="2026-09-28T09:10:00.000Z",
        updated_at="2026-09-28T09:10:00.000Z",
        link="https://github.com/org/repo/blob/main/README.md",
        repo="org/repo",
        path="README.md",
    )

    # 1. 建置 Foundry 索引
    index_db_path = tmp_path / "foundry_index.sqlite"
    stats = build_foundry_index(
        index_db_path,
        artifacts=[art1, art2],
        meta=FoundryIndexMeta(
            generation=1,
            built_at="2026-09-28T09:15:00.000Z",
            foundry_main_sha="main_commit_abc",
        ),
    )
    assert stats.sessions == 2
    assert index_db_path.is_file()

    # 2. 上傳 index 與 manifest 至 Drive
    index_sha = hashlib.sha256(index_db_path.read_bytes()).hexdigest()
    index_file = drive.create(env["inbox_folder_id"], "index-g1.sqlite", index_db_path)

    manifest_data = {
        "format": "aistorage.readview/v1",
        "element": "foundry",
        "generation": 1,
        "published_at": "2026-09-28T09:15:00.000Z",
        "agora_main_sha": "main_commit_abc",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {
            "id": index_file.id,
            "sha256": index_sha,
            "size": index_db_path.stat().st_size,
        },
        "files": [index_file.id, drive_obj.id],
        "retired": [],
    }
    manifest_bytes = (json.dumps(manifest_data) + "\n").encode("utf-8")
    manifest_file = drive.create(env["inbox_folder_id"], "readview-manifest.json", manifest_bytes)

    # 3. 初始化 ReadViewClient 與 FoundryReader
    cfg = ReaderConfig(
        manifest_file_id=manifest_file.id,
        sa_key_path=tmp_path / "sa.json",
        cache_dir=tmp_path / "reader_cache",
    )
    rv_client = ReadViewClient(drive, cfg, clock=clock)
    reader = FoundryReader(rv_client, drive=drive, clock=clock)

    # 4. 驗證 find 查詢
    # (a) 依 kind 查詢
    found_contained = reader.find(kind="contained").value
    assert len(found_contained) == 1
    assert found_contained[0].artifact.artifact_id == art1_id

    # (b) 依 producer 查詢
    found_bob = reader.find(producer="agent-bob").value
    assert len(found_bob) == 1
    assert found_bob[0].artifact.artifact_id == art2_id

    # (c) 依 session_id 查詢
    found_sess = reader.find(session_id=env["session_id"]).value
    assert len(found_sess) == 1
    assert found_sess[0].artifact.name == "weights.bin"

    # 5. 驗證 get 讀取
    # (a) Contained artifact: 驗證下載與雜湊比對
    dest_file = tmp_path / "downloaded_weights.bin"
    res_get1 = reader.get(art1_id, dest=dest_file)
    assert res_get1.value.kind == "contained"
    assert res_get1.value.path == dest_file
    assert dest_file.read_bytes() == obj_content

    # In-memory download without dest
    res_mem = reader.get(art1_id)
    assert res_mem.value.data == obj_content

    # (b) Link artifact: 回傳出處字典
    res_get2 = reader.get(art2_id)
    assert res_get2.value.kind == "link"
    assert res_get2.value.origin["link"] == "https://github.com/org/repo/blob/main/README.md"
    assert res_get2.value.origin["repo"] == "org/repo"
    assert res_get2.value.origin["path"] == "README.md"

    # (c) 不存在的產出 -> KeyError
    with pytest.raises(KeyError):
        reader.get("artifact:NON_EXISTENT")

    # (d) 測試內容雜湊不符 -> MismatchError
    corrupted_obj = drive.create(env["inbox_folder_id"], "corrupted.bin", b"corrupted content")
    art_corrupted_id = f"artifact:{generate_ulid()}"
    art_corrupted = ArtifactRow(
        artifact_id=art_corrupted_id,
        kind="contained",
        content_type="application/octet-stream",
        name="bad.bin",
        producer="agent-alice",
        case_id="case_101",
        produced_by_session_id=env["session_id"],
        created_at="2026-09-28T09:20:00.000Z",
        updated_at="2026-09-28T09:20:00.000Z",
        size=10,
        sha256="0" * 64,  # 不符合之假雜湊
        object_file_id=corrupted_obj.id,
    )
    index_db_path2 = tmp_path / "foundry_index_g2.sqlite"
    build_foundry_index(
        index_db_path2,
        artifacts=[art1, art2, art_corrupted],
        meta=FoundryIndexMeta(
            generation=2,
            built_at="2026-09-28T09:20:00.000Z",
            foundry_main_sha="main_commit_abc",
        ),
    )
    index_sha2 = hashlib.sha256(index_db_path2.read_bytes()).hexdigest()
    index_file2 = drive.create(env["inbox_folder_id"], "index-g2.sqlite", index_db_path2)
    manifest_data2 = {
        "format": "aistorage.readview/v1",
        "element": "foundry",
        "generation": 2,
        "published_at": "2026-09-28T09:20:00.000Z",
        "agora_main_sha": "main_commit_abc",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {
            "id": index_file2.id,
            "sha256": index_sha2,
            "size": index_db_path2.stat().st_size,
        },
        "files": [index_file2.id, drive_obj.id, corrupted_obj.id],
        "retired": [],
    }
    drive.update_content(
        manifest_file.id,
        (json.dumps(manifest_data2) + "\n").encode("utf-8"),
    )
    with pytest.raises(MismatchError):
        reader.get(art_corrupted_id)


def test_foundry_store_operations(test_setup):
    """驗證 FoundryStore 的 catalog 列舉與物件儲存。"""
    env = test_setup
    store = env["foundry_store"]
    tmp_path = env["tmp_path"]

    # 1. 初始 catalog 為空
    assert store.list_catalog() == []
    assert store.get_catalog("01NONEXISTENT00000000000000") is None

    # 2. 寫入多筆 catalog
    ulid1 = "01ARZ3NDEKTSV4RRFFQ69G5FA1"
    ulid2 = "01ARZ3NDEKTSV4RRFFQ69G5FA2"
    store.put_catalog(ulid1, {"id": f"artifact:{ulid1}", "name": "one"})
    store.put_catalog(ulid2, {"id": f"artifact:{ulid2}", "name": "two"})

    entries = store.list_catalog()
    assert len(entries) == 2
    assert entries[0]["name"] == "one"
    assert entries[1]["name"] == "two"

    # 3. put_contained_object 檔案不存在時拋錯
    with pytest.raises(FileNotFoundError):
        store.put_contained_object(ulid1, "missing.bin", tmp_path / "not_there.bin")


def test_apply_artifact_edge_cases(test_setup):
    """驗證 apply_artifact 各種防護與邊界條件。"""
    env = test_setup
    store_foundry = env["foundry_store"]
    agora_store = env["agora_store"]
    clock = env["clock"]
    tmp_path = env["tmp_path"]

    # 1. kind="contained" 但 raw_path 為 None -> raw_mismatch
    dec_no_raw = Mock(
        item=Mock(sidecars=[], sigs=[], raws=[], item_key=generate_ulid(), inbox_folder_id="inbox_1"),
        sidecar={
            "metadata": {"id": f"artifact:{generate_ulid()}"},
            "body": {
                "kind": "contained",
                "produced_by_session_id": env["session_id"],
                "name": "data.bin",
            },
        },
        record_metadata={"id": f"artifact:{generate_ulid()}"},
        producer=f"profile:{PROFILE}",
        raw_path=None,
    )
    res = apply_artifact(store_foundry, dec_no_raw, agora_store, clock)
    assert res.ok is False
    assert res.code == "raw_mismatch"

    # 2. kind="contained" 宣告 sha 與實際 sha 不符 -> raw_mismatch
    real_file = tmp_path / "mismatch.bin"
    real_file.write_bytes(b"hello world")
    dec_sha_mismatch = Mock(
        item=Mock(sidecars=[], sigs=[], raws=[], item_key=generate_ulid(), inbox_folder_id="inbox_1"),
        sidecar={
            "metadata": {"id": f"artifact:{generate_ulid()}"},
            "body": {
                "kind": "contained",
                "produced_by_session_id": env["session_id"],
                "name": "data.bin",
            },
            "raw": {"sha256": "f" * 64, "size": 11},
        },
        record_metadata={"id": f"artifact:{generate_ulid()}"},
        producer=f"profile:{PROFILE}",
        raw_path=real_file,
    )
    res = apply_artifact(store_foundry, dec_sha_mismatch, agora_store, clock)
    assert res.ok is False
    assert res.code == "raw_mismatch"

    # 3. kind="link" 但缺少 link 且缺少 repo+path -> invalid_format
    dec_bad_link = Mock(
        item=Mock(sidecars=[], sigs=[], raws=[], item_key=generate_ulid(), inbox_folder_id="inbox_1"),
        sidecar={
            "metadata": {"id": f"artifact:{generate_ulid()}"},
            "body": {
                "kind": "link",
                "produced_by_session_id": env["session_id"],
                "name": "doc",
            },
        },
        record_metadata={"id": f"artifact:{generate_ulid()}"},
        producer=f"profile:{PROFILE}",
        raw_path=None,
    )
    res = apply_artifact(store_foundry, dec_bad_link, agora_store, clock)
    assert res.ok is False
    assert res.code == "invalid_format"



# ---------------------------------------------------------------------------
# review F-H2／F-H3：收容產出真的進 annex、object_file_id 由發佈端解析
# ---------------------------------------------------------------------------


def _foundry_annex_repo(tmp_path: Path) -> tuple[Path, "object"]:
    """真的 git-annex repo（暫存目錄內）。"""
    import subprocess

    from aistorage.annex.git import SubprocessAnnexGit, get_git_env

    workdir = tmp_path / "foundry"
    workdir.mkdir()
    env = get_git_env()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=workdir, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workdir, env=env, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=workdir, env=env, check=True)
    (workdir / "README.md").write_text("foundry\n")
    subprocess.run(["git", "add", "."], cwd=workdir, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=workdir, env=env, check=True)
    subprocess.run(["git", "annex", "init", "aistorage-it"], cwd=workdir, env=env,
                   check=True, capture_output=True)
    return workdir, SubprocessAnnexGit(workdir)


def test_foundry_contained_object_really_goes_into_annex(tmp_path: Path) -> None:
    """F-H2：收容產出進 annex，key 由 git-annex 決定（不是自己算的）。"""
    import hashlib
    import subprocess

    from aistorage.annex.git import get_git_env
    from aistorage.annex.git import SubprocessAnnexGit
    from aistorage.errors import WriteError
    from aistorage.foundry.store import LARGEFILES_OBJECTS, FoundryStore

    workdir, git = _foundry_annex_repo(tmp_path)
    store = FoundryStore(workdir, git=git, temp_dir=tmp_path / "tmp")

    # largefiles 規則涵蓋 objects/**，但不含 catalog
    rule = subprocess.run(["git", "-C", str(workdir), "config", "annex.largefiles"],
                          capture_output=True, text=True, env=get_git_env(), check=True)
    assert rule.stdout.strip() == LARGEFILES_OBJECTS

    src = tmp_path / "report.pdf"
    body = b"%PDF-1.4 fake content for annex"
    src.write_bytes(body)
    ulid = "01ABCDEF2345GHJKLMNPQRS"
    key = store.put_contained_object(ulid, "report.pdf", src)

    # key 由 git-annex 產生：大小與 sha256 對得上，副檔名取自工作樹檔名
    assert key == f"SHA256E-s{len(body)}--{hashlib.sha256(body).hexdigest()}.pdf"
    assert key in store.keys()
    assert git.lookupkey(f"objects/{ulid}/report.pdf") == key
    # catalog 進得去（JSON 仍是 git blob，不進 annex）
    store.put_catalog(ulid, {"id": f"artifact:{ulid}", "annex_key": key})
    assert git.lookupkey(f"catalog/{ulid}.json") is None

    # 規則涵蓋不到時必須 raise（不准默默留在 git blob）：另起一個工作樹，
    # 規則只涵蓋 catalog（largefiles 對已在 index 的檔案無效，不能就地換規則）
    import subprocess

    from aistorage.annex.git import get_git_env

    other_dir = tmp_path / "foundry-narrow"
    other_dir.mkdir()
    env = get_git_env()
    for cmd in (["init", "-b", "main", "-q"], ["config", "user.name", "t"],
                ["config", "user.email", "t@t.invalid"]):
        subprocess.run(["git", "-C", str(other_dir), *cmd], env=env, check=True,
                       capture_output=True)
    (other_dir / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(other_dir), "add", "."], env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(other_dir), "commit", "-qm", "seed"], env=env,
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other_dir), "annex", "init", "aistorage-it"],
                   env=env, check=True, capture_output=True)
    narrow = FoundryStore(other_dir, git=SubprocessAnnexGit(other_dir),
                          largefiles="include=catalog/*", temp_dir=tmp_path / "tmp2")
    stray = tmp_path / "stray.bin"
    stray.write_bytes(b"stray")
    with pytest.raises(WriteError, match="沒有進 git-annex"):
        narrow.put_contained_object(ulid, "stray.txt", stray)


def test_resolve_object_file_ids_matches_drive_by_key() -> None:
    """F-H3：發佈端把 key 解析成 Drive file id；對不上就不發佈並回報原因。"""
    from aistorage.drive.fake import FakeDrive
    from aistorage.foundry.index import ArtifactRow, resolve_object_file_ids

    drive = FakeDrive()
    prefix = drive.seed_folder("foundry")
    body = b"payload"
    key = f"SHA256E-s7--{'a' * 64}.pdf"
    object_id = drive.seed_file(prefix, key, body)
    other_key = f"SHA256E-s7--{'b' * 64}.pdf"
    drive.seed_file(prefix, other_key, body)

    rows = [
        ArtifactRow(artifact_id="artifact:1", kind="pdf", name="a.pdf",
                    producer="profile:mac-opencode",
                    produced_by_session_id="opencode:s1",
                    created_at="2026-09-27T08:00:00Z",
                    updated_at="2026-09-27T08:00:00Z",
                    size=len(body), sha256=None, annex_key=key, repo="foundry",
                    path=f"objects/1/a.pdf"),
        ArtifactRow(artifact_id="artifact:2", kind="pdf", name="b.pdf",
                    producer="profile:mac-opencode",
                    produced_by_session_id="opencode:s1",
                    created_at="2026-09-27T08:00:00Z",
                    updated_at="2026-09-27T08:00:00Z",
                    size=len(body), sha256=None, annex_key=other_key),
        ArtifactRow(artifact_id="artifact:3", kind="pdf", name="c.pdf",
                    producer="profile:mac-opencode",
                    produced_by_session_id="opencode:s1",
                    created_at="2026-09-27T08:00:00Z",
                    updated_at="2026-09-27T08:00:00Z", annex_key="SHA256E-s1--" + "c" * 64),
        ArtifactRow(artifact_id="artifact:4", kind="pdf", name="d.pdf",
                    producer="profile:mac-opencode",
                    produced_by_session_id="opencode:s1",
                    created_at="2026-09-27T08:00:00Z",
                    updated_at="2026-09-27T08:00:00Z"),
    ]
    ok, issues = resolve_object_file_ids(
        rows, drive=drive, prefix_folder_id=prefix,
        allowed_keys=[key, other_key])
    assert [r.artifact_id for r in ok] == ["artifact:1", "artifact:2"]
    assert ok[0].object_file_id == object_id
    codes = {i.artifact_id: i.code for i in issues}
    # artifact:3 的 key 既不在 pin 也不在 Drive：先被 pin 檢查擋下（fail-closed）
    assert codes["artifact:3"] == "key_not_in_pin"
    assert codes["artifact:4"] == "missing_annex_key"

    # 不在 pin 的 key 也要擋掉（fail-closed）
    ok2, issues2 = resolve_object_file_ids(
        rows[:1], drive=drive, prefix_folder_id=prefix, allowed_keys=[])
    assert not ok2
    assert issues2[0].code == "key_not_in_pin"


def test_resolve_object_file_ids_checks_checksum_and_size() -> None:
    from aistorage.drive.fake import FakeDrive
    from aistorage.foundry.index import ArtifactRow, resolve_object_file_ids

    drive = FakeDrive()
    prefix = drive.seed_folder("foundry")
    body = b"payload"
    key = f"SHA256E-s7--{'a' * 64}.pdf"
    drive.seed_file(prefix, key, body)
    base = dict(kind="pdf", producer="profile:mac-opencode",
                produced_by_session_id="opencode:s1",
                created_at="2026-09-27T08:00:00Z",
                updated_at="2026-09-27T08:00:00Z", annex_key=key, repo="foundry")
    ok, issues = resolve_object_file_ids(
        [ArtifactRow(artifact_id="artifact:1", name="a.pdf", sha256="d" * 64,
                     size=len(body), **base)],
        drive=drive, prefix_folder_id=prefix)
    assert not ok
    assert issues[0].code in ("checksum_mismatch", "size_mismatch")
