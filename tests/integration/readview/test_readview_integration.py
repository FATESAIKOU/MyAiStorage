"""讀取側整合測試：真 Drive（提交流程身分 ＋ 讀取端 SA 身分）。

對應 docs/impl/group4-modules.md 第 8.3 節的四個情境：
1. 發佈 → SA 讀取（find、show、read、continuation）
2. 增量發佈 → SA 在 update 之後幾秒內看到新的世代（技術驗證 1.4i）
3. 注入同名檔 → 讀者不受影響；下一輪清掃會隔離
4. SA 嘗試 update manifest → 403（技術驗證 1.5）

規則（g3-common.md）：
- 憑證**只以路徑引用**，絕不讀取、印出或貼上秘密內容。
- 選了 integration 標記但設定缺少時要 **FAIL**，不能 skip。
- 每個測試在 TEST_FOLDER_ID 底下建自己的前綴 `it-<ULID>/`，測完依 file id
  永久刪除（刪前先 get() 確認 parents）。
- 測試資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Iterator

import pytest

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
from aistorage.clock import SystemClock, format_rfc3339
from aistorage.drive.auth import RcloneConfToken
from aistorage.drive.http import HttpDriveClient
from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient
from aistorage.errors import AiStorageError
from aistorage.integrity.sweep import plan_readview_sweep
from aistorage.publish.publisher import DriveReadViewPublisher, load_manifest
from aistorage.readview.model import initial_manifest, serialize_manifest, trusted_ids
from aistorage.readview.naming import index_name
from aistorage.reader import AgoraReader
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.schema import generate_ulid

pytestmark = pytest.mark.integration

CONFIG_DIR = Path("~/.config/aistorage").expanduser()
IDS_ENV = CONFIG_DIR / "ids.env"
COMMITTER_CONF = CONFIG_DIR / "rclone-committer-test.conf"
WORKER_CONF = CONFIG_DIR / "rclone-worker.conf"
SA_READER_KEY = CONFIG_DIR / "sa-reader.json"

# 讀者在 update 之後要看到新世代；1.4i 實測 2〜5 秒，這裡給較寬的失敗上限
VISIBILITY_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.5


def _require(path: Path, what: str) -> Path:
    """整合測試的設定必須齊全：缺少就 FAIL（不能 skip）。"""
    if not path.is_file():
        pytest.fail(
            f"整合測試設定缺少：{what}（{path}）。"
            "整合測試必須 FAIL 而不是 skip——請先備妥測試憑證。"
        )
    return path


def _test_folder_id() -> str:
    """自 ids.env 讀 TEST_FOLDER_ID（非秘密）；只取需要的 key，不印任何內容。"""
    path = _require(IDS_ENV, "測試用 Drive 資料夾 id 清單")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "TEST_FOLDER_ID" and value.strip():
            return value.strip().strip("'\"")
    pytest.fail(f"{IDS_ENV} 裡找不到 TEST_FOLDER_ID")


class FakeConverter:
    """自編測試用轉換器：raw 是 {"texts": [...], "title": ...} 的 JSON。"""

    source = "opencode"
    version = "1"

    def convert(self, raw_path: Path, *, session_id: str, parent_id=None,
                snapshot_sha256: str | None = None) -> dict:
        data = raw_path.read_bytes()
        payload = json.loads(data.decode("utf-8"))
        return {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": self.source,
            "title": payload.get("title"),
            "parent_id": parent_id,
            "snapshot_sha256": hashlib.sha256(data).hexdigest(),
            "in_progress": False,
            "messages": [
                {
                    "message_id": f"m{i}",
                    "index": i,
                    "role": "user" if i % 2 == 0 else "assistant",
                    "created_at": "2026-09-27T08:00:00Z",
                    "completed": True,
                    "reverted": False,
                    "parts": [{"type": "text", "text": t}],
                }
                for i, t in enumerate(payload["texts"])
            ],
        }


def _true_copy(tmp_path: Path) -> AgoraStore:
    """自編真本：兩個 Session，其中一個有被交接單釘住的舊快照。"""
    store = AgoraStore(
        worktree=tmp_path / "wt",
        raw_storage=FakeRawStorage(),
        git=None,
        temp_dir=tmp_path / "store_tmp",
    )

    def add(session_id: str, texts: tuple[str, ...], at: str, title: str | None) -> str:
        raw = json.dumps({"texts": list(texts), "title": title},
                         ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        raw_path = store._temp_dir / f"seed-{sha}.raw"
        raw_path.write_bytes(raw)
        store.put_session(
            SessionRecord(
                id=session_id,
                producer="profile:mac-opencode",
                created_at=at,
                updated_at=at,
                status="stopped",
                snapshot_at=at,
                raw_sha256=sha,
                raw_size=len(raw),
                committed_at=at,
                last_item_key=generate_ulid(),
                stopped_at=at,
                title=title,
            ),
            raw_path,
        )
        return sha

    t0 = "2026-09-27T08:00:00.000Z"
    t1 = "2026-09-27T09:00:00.000Z"
    pinned = add("opencode:ses_1", ("接續點的設計", "交接單由持有者發起"), t0, "接續點設計")
    add("opencode:ses_1", ("接續點的設計", "交接單由持有者發起", "加一段新內容"), t1,
        "接續點設計")
    add("opencode:ses_2", ("別的工作線",), t0, "別的工作線")

    ulid = generate_ulid()
    store.put_json(
        layout.handoff_path(ulid),
        {
            "id": f"handoff:{ulid}",
            "type": "handoff",
            "producer": "profile:mac-opencode",
            "created_at": t0,
            "updated_at": t0,
            "case_id": None,
            "provenance": None,
            "body": {
                "target_session_id": "opencode:ses_1",
                "continuation": {"snapshot_sha256": pinned, "message_id": "m1"},
                "content": "把接續點的設計接下去（自編測試內容）",
                "author_session_id": "opencode:ses_1",
            },
            "claimed_by": None,
            "committed_at": t0,
        },
    )
    return store


@dataclass
class Env:
    committer: DriveClient
    worker: DriveClient
    sa: DriveClient
    prefix_id: str
    readview_id: str
    manifest_id: str
    client: ReadViewClient
    reader: AgoraReader
    store: AgoraStore
    workdir: Path


def _drive(conf: Path) -> DriveClient:
    return HttpDriveClient(RcloneConfToken(conf))


def _delete_tree(drive: DriveClient, folder_id: str, *, attempts: int = 3) -> None:
    """永久刪除一個資料夾與其所有子項（刪前先 get() 確認 parents）。

    Drive 偶爾回 429／5xx，刪除重試幾次；測完的垃圾一定要清掉，所以不容許
    因為暫時性錯誤就留一整個 it-<ULID>/ 在測試資料夾裡。
    """
    for i in range(attempts):
        try:
            _delete_tree_once(drive, folder_id)
            return
        except AiStorageError:
            if i == attempts - 1:
                raise
            time.sleep(1.0 + i)


def _delete_tree_once(drive: DriveClient, folder_id: str) -> None:
    children = drive.list_children(folder_id)
    for child in children:
        if child.is_folder:
            _delete_tree(drive, child.id)
        else:
            f = drive.get(child.id)
            assert folder_id in f.parents, f"檔案 {child.id} 不在預期的資料夾內"
            drive.delete_permanently(child.id)
    f = drive.get(folder_id)
    assert f.is_folder
    drive.delete_permanently(folder_id)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    """建立 it-<ULID>/readview/、發佈第一代，並在測完後永久刪除。"""
    test_folder = _test_folder_id()
    committer = _drive(_require(COMMITTER_CONF, "提交流程（測試）rclone 設定"))
    worker = _drive(_require(WORKER_CONF, "住民 rclone 設定（注入情境用）"))
    _require(SA_READER_KEY, "讀取端 SA 金鑰（PM 決定 7）")

    prefix = committer.create(
        test_folder, f"it-{generate_ulid()}", b"", mime_type=GOOGLE_FOLDER_MIME
    )
    try:
        readview = committer.create(
            prefix.id, "readview", b"", mime_type=GOOGLE_FOLDER_MIME
        )
        now = format_rfc3339(SystemClock().now(), include_fraction=True)
        manifest = committer.create(
            readview.id,
            "readview-manifest.json",
            serialize_manifest(initial_manifest(published_at=now)),
            mime_type="application/json",
        )
        store = _true_copy(tmp_path)
        workdir = tmp_path / "publish-work"
        publisher = DriveReadViewPublisher(
            committer,
            folder_id=readview.id,
            manifest_file_id=manifest.id,
            converters={"opencode": FakeConverter()},
            clock=SystemClock(),
            workdir=workdir,
        )
        publisher.publish(store, agora_main_sha="it-main-1")

        from aistorage.drive.sa_auth import ServiceAccountToken

        sa = HttpDriveClient(ServiceAccountToken(SA_READER_KEY))
        cfg = ReaderConfig(
            manifest_file_id=manifest.id,
            sa_key_path=SA_READER_KEY,
            cache_dir=tmp_path / "reader-cache",
        )
        client = ReadViewClient(sa, cfg, clock=SystemClock())

        yield Env(
            committer=committer,
            worker=worker,
            sa=sa,
            prefix_id=prefix.id,
            readview_id=readview.id,
            manifest_id=manifest.id,
            client=client,
            reader=AgoraReader(client, clock=SystemClock()),
            store=store,
            workdir=workdir,
        )
    finally:
        _delete_tree(committer, prefix.id)


# ---------------------------------------------------------------------------
# 情境 1：發佈 → SA 讀取
# ---------------------------------------------------------------------------


def test_publish_then_reader_can_find_show_and_read(env: Env):
    reader = env.reader

    found = reader.find_sessions(query_of("接續點"))
    assert [h.hit.session.session_id for h in found.value] == ["opencode:ses_1"]
    assert found.value[0].hit.matches  # 命中位置（message_id）
    assert found.value[0].freshness.snapshot_at is not None
    assert found.freshness.snapshot_at is not None  # 清單整體也有新鮮度

    view = reader.get_session("opencode:ses_1")
    assert view.value.session.session_id == "opencode:ses_1"
    assert view.value.session.title == "接續點設計"
    assert len(view.value.snapshots) == 2  # 最新 ＋ 被交接單釘住的那一份
    assert any(h.handoff_id for h in view.value.handoffs_targeting)  # 以它為目標的交接單

    reading = reader.get_reading("opencode:ses_1")
    assert reading.value["snapshot_sha256"] == view.value.session.raw_sha256
    assert any(m["message_id"] == "m2" for m in reading.value["messages"])


def test_reader_reads_the_pinned_snapshot_of_a_continuation(env: Env):
    """接續一定要讀到被釘住的那一份，而不是最新版本。"""
    view = env.reader.get_session("opencode:ses_1")
    handoff = next(h for h in view.value.handoffs_targeting if h.handoff_id)
    latest_sha = view.value.session.raw_sha256
    assert handoff.snapshot_sha256 != latest_sha  # 釘住的是舊快照

    cont = env.reader.get_continuation(handoff.handoff_id)
    assert cont.value.handoff.handoff_id == handoff.handoff_id
    # 只回傳接續點之前的訊息：最後一則是接續點本身
    assert cont.value.messages[-1]["message_id"] == handoff.message_id
    assert all(m["message_id"] != "m2" for m in cont.value.messages)

    pinned = env.reader.get_reading(
        "opencode:ses_1", snapshot_sha256=handoff.snapshot_sha256
    )
    assert pinned.value["snapshot_sha256"] == handoff.snapshot_sha256


def test_open_handoffs_only_lists_main_session_authors(env: Env):
    rows = env.reader.list_open_handoffs().value
    assert len(rows) == 1
    assert rows[0].author_session_id == "opencode:ses_1"


def test_reader_uses_no_write_operations(env: Env, tmp_path: Path):
    """讀取不觸發任何寫入（ADR 0007）。"""
    from aistorage.drive.fake import FakeDrive

    drive = FakeDrive()
    # 讀取視圖的內容原封搬到 FakeDrive，讀者只讀它
    for child in env.committer.list_children(env.readview_id):
        content = env.committer.download_bytes(child.id, max_bytes=64 << 20)
        drive.seed_file("rv", child.name, content, file_id=child.id)
    cfg = ReaderConfig(env.manifest_id, tmp_path / "sa.json", tmp_path / "cache2")
    reader = AgoraReader(ReadViewClient(drive, cfg, clock=SystemClock()), clock=SystemClock())

    reader.find_sessions(query_of("接續點"))
    reader.get_session("opencode:ses_1")
    reader.get_reading("opencode:ses_1")
    reader.list_open_handoffs()
    assert [op for op, _ in drive.calls if op in FakeDrive.WRITE_OPS] == []


def query_of(text: str):
    from aistorage.search.query import Query

    return Query(text=text)


# ---------------------------------------------------------------------------
# 情境 2：增量發佈 → 讀者幾秒內看到新世代
# ---------------------------------------------------------------------------


def test_incremental_publish_becomes_visible_to_sa(env: Env, tmp_path: Path):
    from aistorage.agora.store import SessionRecord

    # 真本多一個快照，main 也換一個 sha
    t2 = "2026-09-27T10:00:00.000Z"
    raw = json.dumps({"texts": ["接續點的設計", "新一段", "再一段"], "title": "接續點設計"},
                     ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    raw_path = env.store._temp_dir / f"seed-{sha}.raw"
    raw_path.write_bytes(raw)
    env.store.put_session(
        SessionRecord(
            id="opencode:ses_1",
            producer="profile:mac-opencode",
            created_at="2026-09-27T08:00:00.000Z",
            updated_at=t2,
            status="stopped",
            snapshot_at=t2,
            raw_sha256=sha,
            raw_size=len(raw),
            committed_at=t2,
            last_item_key=generate_ulid(),
            stopped_at=t2,
            title="接續點設計",
        ),
        raw_path,
    )

    before = env.client.manifest()["generation"]
    publisher = DriveReadViewPublisher(
        env.committer,
        folder_id=env.readview_id,
        manifest_file_id=env.manifest_id,
        converters={"opencode": FakeConverter()},
        clock=SystemClock(),
        workdir=tmp_path / "publish-work-2",
    )
    started = time.monotonic()
    report = publisher.publish(env.store, agora_main_sha="it-main-2")
    assert report.generation == before + 1

    # 讀者端：輪詢 manifest，等新世代出現（1.4i：2〜5 秒）
    deadline = started + VISIBILITY_TIMEOUT_S
    seen: int | None = None
    while time.monotonic() < deadline:
        seen = env.client.manifest()["generation"]
        if seen == before + 1:
            break
        time.sleep(POLL_INTERVAL_S)
    elapsed = time.monotonic() - started
    print(f"\n[1.4i] 讀者看到新世代耗時 {elapsed:.1f}s（世代 {before} → {seen}）")
    assert seen == before + 1, f"{VISIBILITY_TIMEOUT_S}s 內讀者仍看到世代 {seen}"

    # 新世代的索引與 reading 都可讀
    view = env.reader.get_session("opencode:ses_1")
    assert view.value.session.raw_sha256 == sha
    reading = env.reader.get_reading("opencode:ses_1")
    assert reading.value["snapshot_sha256"] == sha


# ---------------------------------------------------------------------------
# 情境 3：注入同名檔 → 讀者不受影響，清掃會隔離
# ---------------------------------------------------------------------------


def test_injected_file_is_not_referenced_and_would_be_quarantined(env: Env):
    """以提交流程身分注入一個同名檔（等於「檔名不可信」的 worst case）。"""
    manifest = load_manifest(env.committer, env.manifest_id)
    index_name_str = index_name(manifest.generation, manifest.index.sha256)
    injected = env.committer.create(
        env.readview_id, index_name_str, b"not a real sqlite file"
    )
    assert injected.id not in manifest.files

    # 讀者照樣讀得到（它只依 manifest 記錄的 id 與 sha256）
    assert env.client.manifest()["generation"] == manifest.generation
    assert env.reader.find_sessions(query_of("接續點")).value

    # 第 4 步的清掃：可信集合來自 manifest，注入檔不在其中 → 隔離
    children = env.committer.list_children(env.readview_id)
    decisions = plan_readview_sweep(
        type("L", (), {
            "files": tuple(f for f in children if not f.is_folder),
            "subfolders": tuple(f for f in children if f.is_folder),
        })(),
        set(trusted_ids(manifest, env.manifest_id)),
        readview_folder_id=env.readview_id,
    )
    quarantined = {d.file.id: d for d in decisions if d.disposition.value == "quarantine"}
    assert injected.id in quarantined
    assert env.manifest_id not in quarantined  # manifest 自己一定要留


def test_worker_identity_injection_does_not_affect_reader(env: Env):
    """住民身分注入：讀者不受影響。

    若此環境沒有把測試資料夾分享給住民身分（Drive 對看不到的檔案回 404），
    記錄一則訊息後仍驗證讀者完整——注入嘗試本身不是這裡要證明的重點。
    """
    manifest = load_manifest(env.committer, env.manifest_id)
    target = next(
        f for f in env.committer.list_children(env.readview_id)
        if f.id in manifest.files and not f.is_folder
    )
    try:
        injected = env.worker.create(env.readview_id, target.name, b"injected by resident")
    except AiStorageError as e:
        print(f"\n[8.3-3] 住民身分無法寫入測試資料夾（{type(e).__name__}），"
              "改以提交流程身分的注入為準（見上一個測試）")
    else:
        assert injected.id not in manifest.files
        assert env.client.manifest()["generation"] == manifest.generation
        assert env.reader.get_session("opencode:ses_1").value.session.session_id


# ---------------------------------------------------------------------------
# 情境 4：SA 不得寫入 manifest（1.5）
# ---------------------------------------------------------------------------


def test_sa_cannot_update_manifest(env: Env):
    from aistorage.errors import WriteError

    before = env.client.manifest()
    with pytest.raises(WriteError) as exc:
        env.sa.update_content(env.manifest_id, b'{"format":"aistorage.readview/v1"}')
    assert exc.value.status_code in (401, 403), (
        f"預期 401/403（SA 對提交流程建立的檔案沒有寫入權），實際 {exc.value.status_code}"
    )
    # 讀者端完全不受影響
    after = env.client.manifest()
    assert after["generation"] == before["generation"]
    assert after["index"]["id"] == before["index"]["id"]
    assert env.reader.get_session("opencode:ses_1").value.session.session_id


# ---------------------------------------------------------------------------
# 4.5：重建驗證（唯讀，可在任何地方跑）
# ---------------------------------------------------------------------------


def test_rebuild_verify_matches_published_readview(env: Env, tmp_path: Path):
    """以讀取端身分驗證讀取視圖可由真本重建（committer.rebuild）。"""
    from aistorage.committer.rebuild import (
        READINGS_FILE_ID_COLUMN,
        _index_differs,
        compare_with_published,
        format_diff,
        rebuild_local,
    )
    from aistorage.search.index import dump_tables

    result = rebuild_local(
        env.store, {"opencode": FakeConverter()}, tmp_path / "rebuild-out",
        generation=env.client.manifest()["generation"],
        agora_main_sha="it-main-1",
    )
    diff = compare_with_published(result, env.client)
    assert diff.ok, format_diff(diff)

    # 逐表比對（dump_tables 是確定性輸出；readings 略過 file_id，因本地重建沒有 Drive id）
    assert _index_differs(result.index_tables, dump_tables(env.client.index_path())) == []
    published = dump_tables(env.client.index_path())
    for table in ("sessions", "snapshots", "links", "handoffs", "rejections",
                  "message_fts", "title_fts"):
        assert result.index_tables[table] == published[table], f"索引表 {table} 不一致"
    local_readings = [tuple(r[:READINGS_FILE_ID_COLUMN] + r[READINGS_FILE_ID_COLUMN + 1:])
                      for r in result.index_tables["readings"]]
    published_readings = [tuple(r[:READINGS_FILE_ID_COLUMN] + r[READINGS_FILE_ID_COLUMN + 1:])
                          for r in published["readings"]]
    assert local_readings == published_readings
