"""第 9 組追溯表的三個優先缺口（驗收測試；只依介面與文件）。

對應 `docs/impl/spec-traceability.md` 的「建議優先補的三個」：

1. **Foundry 永久保存**（`specs/foundry/catalog`）：GC 只回收 bundle，
   annex 物件（Agora 的原始紀錄與 Foundry 的收容產出）永遠不被刪。
   驗證方式：把 annex 物件混進清掃的列舉清單，斷言 `plan_sweep` 只給
   KEEP／NEED_CONTENT_CHECK（永不 QUARANTINE／GC），而 `gc_removed`
   只刪 `removed_bundles` 裡的 bundle、不碰 annex 物件。

2. **worker 的 Drive 憑證無法執行抹除或刪除真本**（`specs/common/identity`）：
   管理路徑（抹除的計畫與執行）只接受管理身分；worker 的憑證會被擋下
   （Drive 對真本檔案回 403／404）。用 FakeDrive 注入 403／404 與
   `WriteError(status_code=403)` 模擬。

3. **Foundry 讀取的每筆結果附快照時間與新鮮度**（`specs/foundry/catalog`）：
   `find` 的每一筆與 `get` 都要附 `snapshot_at` 與 `freshness`。

規則：FakeDrive 與自編測試資料；不碰真 Drive／GitHub／MyBrain；祕密只以
路徑引用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.erase import (
    EraseTarget,
    check_plan_parents,
    delete_groups_for,
    plan_erase,
)
from aistorage.agora.store import AgoraStore, FakeRawStorage
from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import NotFound, WriteError
from aistorage.foundry.index import ArtifactRow, FoundryIndexMeta, build_foundry_index
from aistorage.integrity.gc import collect_removed_bundles, gc_removed
from aistorage.integrity.pin import PinState
from aistorage.integrity.settle import RepoListing
from aistorage.integrity.sweep import Disposition, plan_sweep
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.reader.foundry import FoundryReader

T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"
T2 = "2026-09-28T10:00:00.000Z"
UUID = "11111111-2222-3333-4444-555555555555"
PROFILE = "mac-worker"
TEST_PRIV_KEY = b"G" * 32


# ---------------------------------------------------------------------------
# 共用小工具
# ---------------------------------------------------------------------------


def _manifest_bytes(*names: str) -> bytes:
    return ("\n".join(names) + "\n").encode("utf-8")


def _pin_state(*, active: tuple[str, ...], removed: frozenset[str] = frozenset(),
               annex_keys: frozenset[str] = frozenset(),
               manifest: bytes | None = None) -> PinState:
    return PinState(
        repo="agora", repo_uuid=UUID, refs={"refs/heads/main": "a" * 40},
        manifest_sha256=hashlib.sha256(manifest or _manifest_bytes(*active)).hexdigest(),
        prev_manifest_sha256=None, active_bundles=active, removed_bundles=removed,
        annex_keys=annex_keys, promoted_at=T0, run_id="run-1")


def _listing(drive: FakeDrive, prefix: str) -> RepoListing:
    children = drive.list_children(prefix)
    return RepoListing(
        prefix_folder_id=prefix,
        files=tuple(c for c in children if not c.is_folder),
        subfolders=tuple(c for c in children if c.is_folder),
    )


def _annex_name(content: bytes, ext: str = "") -> str:
    """git-annex SHA256E 的 key 檔名（大小與 sha256 相符）。"""
    return f"SHA256E-s{len(content)}--{hashlib.sha256(content).hexdigest()}{ext}"


# ===========================================================================
# (1) Foundry 永久保存：GC 只回收 bundle；annex 物件永遠不被刪
# ===========================================================================


def test_gc_only_reclaims_bundles_and_never_touches_annex_objects(tmp_path: Path):
    """提交流程的 GC：annex 物件（Agora raw 與 Foundry 收容產出）不得被回收。

    - `plan_sweep` 對 annex 物件的處置只會是 KEEP（或 NEED_CONTENT_CHECK），
      永遠不會是 GC 或 QUARANTINE。
    - `gc_removed` 只刪 `removed_bundles` 裡的 GITBUNDLE；annex 物件即使
      被誤放進候選清單，也必須在防呆檢查被擋下（MismatchError），不能刪掉。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")

    raw_content = b"agora raw snapshot payload"
    artifact_content = b"foundry contained artifact payload"
    raw_key = _annex_name(raw_content)
    artifact_key = _annex_name(artifact_content, ".pdf")
    raw_file_id = drive.seed_file(prefix, raw_key, raw_content)
    artifact_file_id = drive.seed_file(prefix, artifact_key, artifact_content)

    # bundle 檔名內嵌大小與雜湊必須和內容相符，plan_sweep 才會留它
    active_bytes = b"a" * 10
    removed_bytes = b"b" * 10
    active_bundle = (f"GITBUNDLE-s{len(active_bytes)}--{UUID}-"
                     f"{hashlib.sha256(active_bytes).hexdigest()}")
    removed_bundle = (f"GITBUNDLE-s{len(removed_bytes)}--{UUID}-"
                      f"{hashlib.sha256(removed_bytes).hexdigest()}")
    active_file_id = drive.seed_file(prefix, active_bundle, active_bytes)
    removed_file_id = drive.seed_file(prefix, removed_bundle, removed_bytes)

    state = _pin_state(
        active=(active_bundle,), removed=frozenset({removed_bundle}),
        annex_keys=frozenset({raw_key, artifact_key}),
    )
    listing = _listing(drive, prefix)

    # 1. 清掃計畫：annex 物件一律 KEEP，bundle 依 active／removed 分流
    decisions = {d.file.name: d.disposition for d in plan_sweep(
        listing, state, repo_uuid=UUID)}
    assert decisions[raw_key] == Disposition.KEEP, "Agora 的 annex 原始紀錄必須保留"
    assert decisions[artifact_key] == Disposition.KEEP, "Foundry 的收容產出必須保留"
    assert decisions[active_bundle] == Disposition.KEEP
    assert decisions[removed_bundle] == Disposition.GC, "只有 removed bundle 才能回收"
    assert Disposition.GC not in (decisions[raw_key], decisions[artifact_key]), (
        "annex 物件絕不能被標成 GC（永久保存）")
    assert Disposition.QUARANTINE not in (decisions[raw_key], decisions[artifact_key]), (
        "annex 物件不在 removed 清單裡，不該被隔離")

    # 2. GC：只刪 removed bundle；兩個 annex 物件原封不動
    to_gc = collect_removed_bundles(listing, state)
    assert [f.name for f in to_gc] == [removed_bundle]
    deleted = gc_removed(to_gc, drive, prefix_folder_id=prefix, state=state)
    assert deleted == 1
    with pytest.raises(NotFound):
        drive.get(removed_file_id)
    for fid in (raw_file_id, artifact_file_id, active_file_id):
        assert drive.get(fid).id == fid, "annex 物件與 active bundle 都必須還在"

    # 3. 誤放 annex 物件進 GC 候選：防呆檢查必須擋下，不能刪
    with pytest.raises(Exception) as excinfo:
        gc_removed([drive.get(raw_file_id)], drive,
                   prefix_folder_id=prefix, state=state)
    assert "不在釘選值之已移除清單中" in str(excinfo.value)
    assert drive.get(raw_file_id).id == raw_file_id, "防呆擋下之後 annex 物件必須還在"
    assert drive.get(artifact_file_id).id == artifact_file_id


def test_foundry_contained_object_survives_committer_gc(tmp_path: Path):
    """收容產出（Foundry objects/）不會被提交流程的 GC 掃到／刪掉。

    Foundry 的佈局是 `objects/<ULID>/<檔名>`（子目錄），不是平鋪的 annex key；
    提交流程第 12 步只回收 GITBUNDLE，不動 objects 子樹。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("foundry-prefix")
    objects = drive.seed_folder("objects", parent=prefix)
    ulid_dir = drive.seed_folder("01ABCDEF2345GHJKLMNPQRS", parent=objects)
    payload = b"contained report bytes"
    object_file_id = drive.seed_file(ulid_dir, "report.pdf", payload)
    catalog_file_id = drive.seed_file(prefix, "catalog.json", b'{"a":1}')

    state = _pin_state(active=(), removed=frozenset())
    listing = _listing(drive, prefix)
    # 提交流程第 12 步的 GC 只認 `removed_bundles`：沒有 removed bundle 時，
    # 回收清單是空的，收容產出本體不會被回收。
    assert collect_removed_bundles(listing, state) == []
    assert gc_removed([], drive, prefix_folder_id=prefix, state=state) == 0
    assert drive.get(object_file_id).id == object_file_id, "收容產出本體必須還在"
    assert drive.get(catalog_file_id).id == catalog_file_id
    # 即使把 annex 物件誤放進候選，防呆也會擋下（parent 不符或不在 removed 清單）
    with pytest.raises(Exception) as excinfo:
        gc_removed([drive.get(object_file_id)], drive,
                   prefix_folder_id=prefix, state=state)
    assert "回收防呆檢查失敗" in str(excinfo.value)
    assert drive.get(object_file_id).id == object_file_id


# ===========================================================================
# (2) worker 的憑證無法抹除或刪除真本
# ===========================================================================


class _DeniedDrive(FakeDrive):
    """FakeDrive 加上「worker 憑證被拒」：刪除／改寫真本一律 403。"""

    def __init__(self, *, status: int = 403) -> None:
        super().__init__()
        self.status = status

    def delete_permanently(self, file_id: str) -> None:
        raise WriteError(f"Drive 寫入被拒（HTTP {self.status}）", status_code=self.status)

    def update_content(self, file_id: str, content: Any) -> Any:  # type: ignore[override]
        raise WriteError(f"Drive 寫入被拒（HTTP {self.status}）", status_code=self.status)


def test_worker_credentials_cannot_delete_true_copy_files(tmp_path: Path):
    """worker 的憑證刪不了真本（403）：防呆先擋、真的刪除也失敗。"""
    drive = _DeniedDrive(status=403)
    prefix = drive.seed_folder("prefix")
    key = _annex_name(b"raw")
    raw_id = drive.seed_file(prefix, key, b"raw")

    state = _pin_state(active=(), annex_keys=frozenset({key}))
    # 1. annex 物件（不在 removed 清單）→ 防呆擋下，不會嘗試刪除
    with pytest.raises(Exception) as guard:
        gc_removed([drive.get(raw_id)], drive, prefix_folder_id=prefix, state=state)
    assert "不在釘選值之已移除清單中" in str(guard.value)
    assert drive.get(raw_id).id == raw_id, "防呆擋下之後真本檔案必須還在"

    # 2. 真的進入刪除路徑（bundle 已在 removed 清單）→ 403 被記錄，檔案仍在
    bundle_bytes = b"c" * 9
    bundle = (f"GITBUNDLE-s{len(bundle_bytes)}--{UUID}-"
              f"{hashlib.sha256(bundle_bytes).hexdigest()}")
    drive.seed_file(prefix, bundle, bundle_bytes)
    state2 = _pin_state(active=(), removed=frozenset({bundle}))
    listing = _listing(drive, prefix)
    to_gc = collect_removed_bundles(listing, state2)
    assert [f.name for f in to_gc] == [bundle]
    # gc_removed 盡力而為：WriteError 被捕捉，回傳刪除數 0（不是刪成功）
    assert gc_removed(to_gc, drive, prefix_folder_id=prefix, state=state2) == 0
    assert any(f.name == bundle for f in drive.list_children(prefix)), (
        "worker 的 403 之下，真本 bundle 必須還在")


def test_worker_credentials_give_404_on_repo_folders(tmp_path: Path):
    """worker 看不到真本前綴（404）：抹除計畫必須拒絕，而不是當它不存在。"""
    committer_drive = FakeDrive()
    prefix = committer_drive.seed_folder("prefix")
    bundle_bytes = b"d" * 11
    bundle = (f"GITBUNDLE-s{len(bundle_bytes)}--{UUID}-"
              f"{hashlib.sha256(bundle_bytes).hexdigest()}")
    committer_drive.seed_file(prefix, bundle, bundle_bytes)

    store_dir = tmp_path / "agora"
    store_dir.mkdir()
    (store_dir / "sessions").mkdir()
    git = FakeAnnexGit(workdir=store_dir)

    class _NoAccess:
        """worker 的視角：對真本前綴一律 404。"""

        def __init__(self) -> None:
            self.calls = 0

        def list_children(self, folder_id: str):
            self.calls += 1
            raise NotFound(f"找不到資料夾: {folder_id}（HTTP 404）")

        def get(self, file_id: str):
            raise NotFound(f"找不到檔案: {file_id}（HTTP 404）")

        def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
            raise NotFound("HTTP 404")

        def delete_permanently(self, file_id: str) -> None:
            raise WriteError("HTTP 403", status_code=403)

        def find_by_name(self, parent_id: str, name: str):
            raise NotFound("HTTP 404")

    admin = AdminDeps(
        drive=_NoAccess(),  # type: ignore[arg-type]
        clock=FixedClock(T1), workdir=tmp_path / "w",
        repo="agora", repo_uuid=UUID, prefix_folder_id=prefix,
        quarantine_folder_id=committer_drive.seed_folder("q"),
    )
    store = AgoraStore(store_dir, raw_storage=FakeRawStorage(), git=git,
                       temp_dir=tmp_path / "tmp")
    with pytest.raises(NotFound):
        plan_erase([EraseTarget(kind="session", session_id="opencode:ses_1")],
                   admin=admin, store=store)


def test_erase_plan_refuses_files_outside_the_allowed_parents():
    """抹除計畫的防呆：要刪的檔案不在允許的 parent 底下 → 拒絕整個計畫。

    這保證即使有人（或別的憑證）把檔案塞進清單，提交流程的管理路徑也不會刪。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    elsewhere = drive.seed_folder("elsewhere")
    outsider_id = drive.seed_file(elsewhere, "GITBUNDLE-weird", b"x")

    from aistorage.admin.remote import DeleteGroup

    group = DeleteGroup(name="repo", file_ids=(outsider_id,),
                        parent_roots=(prefix,))
    with pytest.raises(AdminError) as excinfo:
        check_plan_parents(drive, [group])
    assert "不在合法 parent 根" in str(excinfo.value)
    assert drive.get(outsider_id).id == outsider_id, "拒絕之後不得刪除"


def test_erase_plan_only_deletes_its_own_categories(tmp_path: Path):
    """抹除只碰真本前綴裡自己的類別；無關檔案不在計畫內。"""
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    bundle_bytes = b"e" * 12
    bundle = (f"GITBUNDLE-s{len(bundle_bytes)}--{UUID}-"
              f"{hashlib.sha256(bundle_bytes).hexdigest()}")
    manifest = "GITMANIFEST--" + UUID
    unrelated_id = drive.seed_file(prefix, "notes.txt", b"unrelated")
    drive.seed_file(prefix, bundle, bundle_bytes)
    drive.seed_file(prefix, manifest, _manifest_bytes(bundle))

    store_dir = tmp_path / "agora"
    store_dir.mkdir()
    (store_dir / "sessions").mkdir()
    git = FakeAnnexGit(workdir=store_dir)
    admin = AdminDeps(
        drive=drive, clock=FixedClock(T1), workdir=tmp_path / "w",
        repo="agora", repo_uuid=UUID, prefix_folder_id=prefix,
        quarantine_folder_id=drive.seed_folder("q"))
    store = AgoraStore(store_dir, raw_storage=FakeRawStorage(), git=git,
                       temp_dir=tmp_path / "tmp")
    plan = plan_erase([EraseTarget(kind="annex_key", key=_annex_name(b"payload"))],
                      admin=admin, store=store)
    assert unrelated_id not in plan.delete_file_ids, "無關檔案不得進抹除清單"
    groups = delete_groups_for(plan, admin=admin)
    for g in groups:
        assert unrelated_id not in g.file_ids


def test_erase_cli_requires_management_credentials(tmp_path: Path, capsys, monkeypatch):
    """抹除 CLI 只接受管理憑證：worker 的 conf 會被拒（沒有管理身分）。

    管理 CLI 只從 `--config` 推導憑證路徑（`AISTORAGE_RCLONE_CONF`，由部署時
    指向管理 conf）；這裡刻意把 worker 的 conf 指進去——裡面的憑證沒有真本
    資料夾權限，`_build_deps` 之後的 Drive 存取會失敗。斷言 CLI 不會
    假裝成功（非 0 或明確的錯誤輸出）。
    """
    import json as _json

    from aistorage.admin.__main__ import main

    worker_conf = tmp_path / "rclone-worker.conf"
    worker_conf.write_text("[gdrive]\n")  # worker 的憑證（沒有管理員的內容）
    cfg = tmp_path / "committer.e2e.json"
    cfg.write_text(_json.dumps({
        "format": "aistorage.committer/v1",
        "repo": "agora-e2e",
        "repo_uuid": UUID,
        "repo_url": "annex::" + UUID,
        "prefix_folder_id": "not-a-real-folder-id",
        "quarantine_folder_id": "q",
        "identity_registry_path": str(tmp_path / "identity.json"),
        "pin_repo_url": "file://" + str(tmp_path / "pin"),
    }), encoding="utf-8")
    (tmp_path / "identity.json").write_text(
        _json.dumps({"format": "aistorage.identity/v1", "profiles": {}}),
        encoding="utf-8")
    monkeypatch.setenv("AISTORAGE_RCLONE_CONF", str(worker_conf))
    monkeypatch.setenv("AISTORAGE_PIN_KEY", str(tmp_path / "pin.key"))
    monkeypatch.setenv("AISTORAGE_PIN_KNOWN_HOSTS", str(tmp_path / "known_hosts"))

    # `--confirm` 不存在時應停在 dry-run；worker 的 conf 進不了管理路徑，
    # 必須明確報錯（not_wired／admin_error），不能靜默地當成做完了。
    rc = main(["erase", "--config", str(cfg), "--session", "opencode:ses_1",
               "--canary", "CANARY", "--why", "test"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc != 0, f"worker 憑證不能執行抹除，必須非 0（實際 {rc}）"
    assert ('"error"' in combined), f"必須是明確的錯誤 JSON：{combined[-400:]}"
    assert '"deleted"' not in combined, "任何情況下都不得回報刪除數"
    assert "not_wired" in combined or "admin_error" in combined
    assert "Traceback" not in combined, "錯誤要收斂成 JSON，不是 traceback"



# ===========================================================================
# (3) Foundry 讀取：每筆結果附快照時間與新鮮度
# ===========================================================================


def _foundry_reader(drive: FakeDrive, tmp_path: Path, *, clock: FixedClock,
                    artifacts: list[ArtifactRow], published_at: str = T1) -> FoundryReader:
    index_path = tmp_path / "foundry_index.sqlite"
    build_foundry_index(
        index_path, artifacts=artifacts,
        meta=FoundryIndexMeta(generation=1, built_at=T1,
                              foundry_main_sha="main_sha"))
    index_file = drive.create("root", "index-g1.sqlite", index_path)
    manifest = {
        "format": "aistorage.readview/v1",
        "element": "foundry",
        "generation": 1,
        "published_at": published_at,
        "agora_main_sha": "main_sha",
        "converter_versions": {},
        "rebuild_epoch": 0,
        "index": {
            "id": index_file.id,
            "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            "size": index_path.stat().st_size,
        },
        "files": [index_file.id],
        "retired": [],
    }
    manifest_file = drive.create("root", "readview-manifest.json",
                                 (json.dumps(manifest) + "\n").encode("utf-8"))
    cfg = ReaderConfig(manifest_file_id=manifest_file.id,
                       sa_key_path=tmp_path / "sa.json",
                       cache_dir=tmp_path / "cache")
    return FoundryReader(ReadViewClient(drive, cfg, clock=clock),
                         drive=drive, clock=clock)


def _artifact(*, created_at: str, name: str = "report.pdf",
              artifact_id: str = "artifact:01ABCDEF2345GHJKLMNPQRS",
              kind: str = "contained", link: str | None = None,
              object_file_id: str | None = None, content: bytes = b"payload") -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact_id, kind=kind, name=name,
        content_type="application/pdf" if kind == "contained" else "text/markdown",
        producer="profile:mac-worker", produced_by_session_id="opencode:s1",
        created_at=created_at, updated_at=created_at,
        size=len(content) if kind == "contained" else None,
        sha256=hashlib.sha256(content).hexdigest() if kind == "contained" else None,
        annex_key=_annex_name(content) if kind == "contained" else None,
        object_file_id=object_file_id, link=link)


def test_foundry_results_attach_a_snapshot_time(tmp_path: Path):
    """`find`／`get` 的每一筆結果都要附 `freshness`（有 `snapshot_at`）。

    這條只驗「有附」；「附的是哪個時間」由下一條驗（spec 要求與 Agora 相同
    的新鮮度規則：這一類不是單一 Session 的快照，用世代的 `published_at`）。
    """
    drive = FakeDrive()
    drive.seed_folder("root")
    content = b"payload"
    obj = drive.create("root", "obj.bin", content)
    art = _artifact(created_at=T0, object_file_id=obj.id, content=content)
    reader = _foundry_reader(drive, tmp_path, clock=FixedClock(T2), artifacts=[art])

    found = reader.find()
    assert found.value, "find 必須回傳至少一筆"
    assert found.freshness.snapshot_at, "清單整體必須附快照時間"
    for f in found.value:
        assert f.freshness is not None, "每一筆都要有 freshness"
        assert f.freshness.snapshot_at, "每一筆都要附快照時間"
        assert f.freshness.generation == 1

    got = reader.get(art.artifact_id)
    assert got.freshness.snapshot_at, "get 也要附快照時間"
    assert got.value.data == content


def test_foundry_freshness_uses_the_generation_published_at(tmp_path: Path):
    """每筆結果的快照時間是**世代的 published_at**，與產出多舊無關。

    情境：世代在 T1（09:00）發佈，產出本身是 T0（08:00）的舊東西，讀者時鐘
    是 T2（10:00）。以 max_lag=90 分鐘讀取時：
    - 契約：落後 60 分鐘 → 符合、不附警告；舊產出不該讓整份目錄被判成過期。
    - F-M1 的現況：用 created_at（08:00）→ 落後 120 分鐘 → 誤判為未達新鮮度。
    """
    drive = FakeDrive()
    drive.seed_folder("root")
    old = _artifact(created_at=T0)
    reader = _foundry_reader(drive, tmp_path, clock=FixedClock(T2), artifacts=[old])

    result = reader.find(max_lag=timedelta(minutes=90))
    assert all(f.freshness.snapshot_at == T1 for f in result.value), (
        "快照時間必須是世代的 published_at（T1），不是產出的 created_at（T0）")
    assert result.freshness.snapshot_at == T1
    assert result.value[0].freshness.satisfied is True, (
        "以 published_at 算只落後 60 分鐘（<= 90），舊產出不該讓它過期")
    assert result.value[0].freshness.warning is None

    # 收緊 max_lag 到 30 分鐘：落後 60 分鐘 → 未達新鮮度並附警告
    strict = reader.find(max_lag=timedelta(minutes=30))
    assert strict.value[0].freshness.satisfied is False
    assert strict.value[0].freshness.warning, "未達新鮮度必須附警告"
    assert strict.value[0].freshness.snapshot_at == T1, "警告時仍要附正確的快照時間"


def test_foundry_get_attaches_snapshot_time_and_freshness(tmp_path: Path):
    """`get` 取回本體時也要附快照時間與新鮮度（link 與 contained 都是）。"""
    drive = FakeDrive()
    drive.seed_folder("root")
    content = b"payload-get"
    obj = drive.create("root", "obj2.bin", content)
    contained = _artifact(
        created_at=T1, artifact_id="artifact:01ABCDEF2345GHJKLMNPQRV",
        name="a.bin", content=content, object_file_id=obj.id)
    link = _artifact(
        created_at=T1, artifact_id="artifact:01ABCDEF2345GHJKLMNPQRW",
        name="b.md", kind="link", link="https://example.invalid/b.md")
    reader = _foundry_reader(drive, tmp_path, clock=FixedClock(T1),
                             artifacts=[contained, link])

    got = reader.get(contained.artifact_id)
    assert got.freshness.snapshot_at, "contained 的 get 要附快照時間"
    assert got.freshness.satisfied is None, "沒指定 max_lag 時 satisfied 是 None"
    assert got.value.data == content

    got_link = reader.get(link.artifact_id)
    assert got_link.freshness.snapshot_at, "link 的 get 也要附快照時間"
    assert got_link.value.origin["link"] == "https://example.invalid/b.md"

