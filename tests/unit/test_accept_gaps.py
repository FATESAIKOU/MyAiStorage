"""第 9 組追溯表的兩個優先缺口（驗收測試；只依介面與文件）。

對應 `docs/impl/spec-traceability.md` 的「建議優先補的三個」中的前兩個
（第三個是 Foundry 讀取的新鮮度，已隨 git-annex 版 Foundry 移除，ADR 0009）：

1. **永久保存**：GC 只回收 bundle，annex 物件（Agora 的原始紀錄）永遠不被刪。
   驗證方式：把 annex 物件混進清掃的列舉清單，斷言 `plan_sweep` 只給
   KEEP／NEED_CONTENT_CHECK（永不 QUARANTINE／GC），而 `gc_removed`
   只刪 `removed_bundles` 裡的 bundle、不碰 annex 物件。

2. **worker 的 Drive 憑證無法執行抹除或刪除真本**（`specs/common/identity`）：
   管理路徑（抹除的計畫與執行）只接受管理身分；worker 的憑證會被擋下
   （Drive 對真本檔案回 403／404）。用 FakeDrive 注入 403／404 與
   `WriteError(status_code=403)` 模擬。

規則：FakeDrive 與自編測試資料；不碰真 Drive／GitHub／MyBrain；祕密只以
路徑引用。
"""

from __future__ import annotations

import hashlib
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
from aistorage.integrity.gc import collect_removed_bundles, gc_removed
from aistorage.integrity.pin import PinState
from aistorage.integrity.settle import RepoListing
from aistorage.integrity.sweep import Disposition, plan_sweep

T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"
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
# (1) 永久保存：GC 只回收 bundle；annex 物件永遠不被刪
# ===========================================================================


def test_gc_only_reclaims_bundles_and_never_touches_annex_objects(tmp_path: Path):
    """提交流程的 GC：annex 物件（Agora 的原始紀錄）不得被回收。

    - `plan_sweep` 對 annex 物件的處置只會是 KEEP（或 NEED_CONTENT_CHECK），
      永遠不會是 GC 或 QUARANTINE。
    - `gc_removed` 只刪 `removed_bundles` 裡的 GITBUNDLE；annex 物件即使
      被誤放進候選清單，也必須在防呆檢查被擋下（MismatchError），不能刪掉。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")

    raw_content = b"agora raw snapshot payload"
    reading_content = b"agora reading json payload"
    raw_key = _annex_name(raw_content)
    reading_key = _annex_name(reading_content, ".json")
    raw_file_id = drive.seed_file(prefix, raw_key, raw_content)
    reading_file_id = drive.seed_file(prefix, reading_key, reading_content)

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
        annex_keys=frozenset({raw_key, reading_key}),
    )
    listing = _listing(drive, prefix)

    # 1. 清掃計畫：annex 物件一律 KEEP，bundle 依 active／removed 分流
    decisions = {d.file.name: d.disposition for d in plan_sweep(
        listing, state, repo_uuid=UUID)}
    assert decisions[raw_key] == Disposition.KEEP, "Agora 的 annex 原始紀錄必須保留"
    assert decisions[reading_key] == Disposition.KEEP, "被 annex 收走的閱讀版也必須保留"
    assert decisions[active_bundle] == Disposition.KEEP
    assert decisions[removed_bundle] == Disposition.GC, "只有 removed bundle 才能回收"
    assert Disposition.GC not in (decisions[raw_key], decisions[reading_key]), (
        "annex 物件絕不能被標成 GC（永久保存）")
    assert Disposition.QUARANTINE not in (decisions[raw_key], decisions[reading_key]), (
        "annex 物件不在 removed 清單裡，不該被隔離")

    # 2. GC：只刪 removed bundle；兩個 annex 物件原封不動
    to_gc = collect_removed_bundles(listing, state)
    assert [f.name for f in to_gc] == [removed_bundle]
    deleted = gc_removed(to_gc, drive, prefix_folder_id=prefix, state=state)
    assert deleted == 1
    with pytest.raises(NotFound):
        drive.get(removed_file_id)
    for fid in (raw_file_id, reading_file_id, active_file_id):
        assert drive.get(fid).id == fid, "annex 物件與 active bundle 都必須還在"

    # 3. 誤放 annex 物件進 GC 候選：防呆檢查必須擋下，不能刪
    with pytest.raises(Exception) as excinfo:
        gc_removed([drive.get(raw_file_id)], drive,
                   prefix_folder_id=prefix, state=state)
    assert "不在釘選值之已移除清單中" in str(excinfo.value)
    assert drive.get(raw_file_id).id == raw_file_id, "防呆擋下之後 annex 物件必須還在"
    assert drive.get(reading_file_id).id == reading_file_id


def test_gc_ignores_files_under_subfolders_of_the_prefix(tmp_path: Path):
    """前綴底下的子目錄不是 GITBUNDLE：GC 不會去碰裡面的東西。

    提交流程第 12 步只回收 `removed_bundles` 裡的 GITBUNDLE，Drive 上其他結構
    （例如日後某個實體放在同一個前綴底下的子樹）不在它的回收範圍。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    sub = drive.seed_folder("nested", parent=prefix)
    payload = b"contained report bytes"
    object_file_id = drive.seed_file(sub, "report.pdf", payload)
    other_file_id = drive.seed_file(prefix, "notes.json", b'{"a":1}')

    state = _pin_state(active=(), removed=frozenset())
    listing = _listing(drive, prefix)
    # 沒有 removed bundle 時回收清單是空的，子樹與其他檔案都不會被回收
    assert collect_removed_bundles(listing, state) == []
    assert gc_removed([], drive, prefix_folder_id=prefix, state=state) == 0
    assert drive.get(object_file_id).id == object_file_id
    assert drive.get(other_file_id).id == other_file_id
    # 即使把子目錄裡的檔案誤放進候選，防呆也會擋下（parent 不符或不在 removed 清單）
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
