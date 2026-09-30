"""第 3 組 8.3 整合案例 5：暫時性錯誤（503）。

在真的 HttpDriveClient 外面包一層注入 503 的代理，驗證：
- 提交流程在 Drive 失敗時**中止**（不是靜默成功，也不是重試到天荒地老）；
- 中止前**沒有任何移動**：Drive 上的真本（manifest／bundle）、隔離資料夾、
  收件匣都保持原樣，釘選值也沒有被推進。
"""

from __future__ import annotations

import urllib.error

import pytest

from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli, run
from aistorage.converters import get_converter
from aistorage.drive.model import DriveFile
from aistorage.errors import ReadError
from aistorage.identity import load_registry
from aistorage.integrity.pin import GitPinStore

from ._harness import (
    _put_session,
    _raw_for,
    annex_git_factory,
    build_annex_repo,
    make_signer,
    write_registry,
)

pytestmark = pytest.mark.integration

S1 = "ses_opencode_basic_501"
S2 = "ses_opencode_basic_502"
S3 = "ses_opencode_basic_503"


class _FailOnce503:
    """包住真實的 Drive 用戶端：在指定操作上丟一次 503，之後恢復正常。"""

    def __init__(
        self, inner, *, fail_on: str = "list_children", only_folder: str | None = None
    ) -> None:
        self._inner = inner
        self.fail_on = fail_on
        self.only_folder = only_folder
        self.failed = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _maybe_fail(self, op: str, folder_id: str | None = None) -> None:
        if self.failed or op != self.fail_on:
            return
        if self.only_folder is not None and folder_id != self.only_folder:
            return
        self.failed = True
        raise ReadError("Drive 請求失敗 (HTTP 503): 注入的暫時性錯誤")

    def list_children(self, folder_id: str) -> list[DriveFile]:
        self._maybe_fail("list_children", folder_id)
        return self._inner.list_children(folder_id)

    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]:
        self._maybe_fail("find_by_name", parent_id)
        return self._inner.find_by_name(parent_id, name)

    def create(self, parent_id: str, name: str, content, *, mime_type: str = "application/octet-stream"):
        self._maybe_fail("create", parent_id)
        return self._inner.create(parent_id, name, content, mime_type=mime_type)

    def update_content(self, file_id: str, content):
        self._maybe_fail("update_content")
        return self._inner.update_content(file_id, content)

    def move(self, file_id: str, *, from_parent: str, to_parent: str):
        self._maybe_fail("move", from_parent)
        return self._inner.move(file_id, from_parent=from_parent, to_parent=to_parent)

    def delete_permanently(self, file_id: str) -> None:
        self._maybe_fail("delete_permanently")
        self._inner.delete_permanently(file_id)

    def get(self, file_id: str) -> DriveFile:
        self._maybe_fail("get")
        return self._inner.get(file_id)

    def download(self, file_id: str, dest, *, max_bytes: int) -> int:
        self._maybe_fail("download")
        return self._inner.download(file_id, dest, max_bytes=max_bytes)

    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes:
        self._maybe_fail("download_bytes")
        return self._inner.download_bytes(file_id, max_bytes=max_bytes)


def _files_under(drive, folder_id: str) -> set[str]:
    """資料夾底下（遞迴）所有「檔案」的名稱；隔離會依日期分層。"""
    names: set[str] = set()
    stack = [folder_id]
    while stack:
        for child in drive.list_children(stack.pop()):
            if child.is_folder:
                stack.append(child.id)
            else:
                names.add(child.name)
    return names


def test_transient_503_aborts_run_without_moving_anything(
    it_settings, real_drive, sandbox, tmp_path
):
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    class _Folder:
        id = inbox_id

    inbox = _Folder()
    annex = build_annex_repo(
        prefix=prefix_name,
        workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=20,
        annex_object_sizes=(300,),
    )
    git_factory = annex_git_factory(annex)
    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    pin_repo = it_settings["pin_repo_url"]
    pins = GitPinStore(
        pin_repo, tmp_path / "pin",
        key_path=it_settings["pin_key"], known_hosts_path=it_settings["known_hosts"],
    )
    cfg = CommitterConfig(
        repo=sandbox.pin_repo_name(),
        repo_uuid=annex.uuid,
        repo_url=annex.url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=pin_repo,
        max_git_bundles=20,
    )

    def _deps(drive):
        return Deps(
            drive=drive,
            pins=pins,
            git_factory=git_factory,
            registry=load_registry(signer.registry_path, allow_example=False),
            converters={"opencode": get_converter("opencode")},
            publisher=NullPublisher(),
            clock=SystemClock(),
        )

    sandbox.register_pin_store(pins)
    init_pin_cli(cfg, _deps(real_drive), confirm=True)
    state_before, pending_before = pins.load(cfg.repo)
    assert pending_before is None

    manifest_name = f"GITMANIFEST--{annex.uuid}"
    prefix_before = {f.name: f.id for f in real_drive.list_children(prefix_id)}
    quarantine_before = {f.name for f in real_drive.list_children(quarantine_id)}
    assert manifest_name in prefix_before

    # ── (a) 掃描收件匣時就 503：中止，而且什麼都沒動 ────────────────
    _put_session(real_drive, inbox_id, signer, _raw_for(S1), S1, "2026-09-27T13:00:00Z")
    flaky = _FailOnce503(real_drive, fail_on="list_children", only_folder=inbox_id)
    report = run(cfg, _deps(flaky), dry_run=False)
    assert flaky.failed, "測試沒有真的注入到 503"
    assert report.ok is False, "Drive 失敗時這一輪必須中止"

    prefix_after = {f.name: f.id for f in real_drive.list_children(prefix_id)}
    assert prefix_after == prefix_before, "503 中止後真本不該有變動"
    assert {f.name for f in real_drive.list_children(quarantine_id)} == quarantine_before, (
        "503 中止後不該有東西被隔離"
    )
    assert [f.name for f in real_drive.list_children(inbox_id)], "503 中止後收件匣不該被清空"
    state_after, pending_after = pins.load(cfg.repo)
    assert pending_after is None, "503 中止後不該留下 pending"
    assert state_after.refs == state_before.refs, "503 中止後釘選值不該被推進"
    assert state_after.manifest_sha256 == state_before.manifest_sha256

    # ── (b) 服務恢復：這一輪正常完成 ───────────────────────────────
    report_ok = run(cfg, _deps(real_drive), dry_run=False)
    assert report_ok.ok is True, f"恢復後中止於 {report_ok.aborted_at}:{report_ok.code}"
    assert report_ok.counts["accepted"] == 1, report_ok.counts

    # ── (c) 有東西要搬的時候 503：隔離必須完全沒發生 ────────────────
    # （前綴裡塞一個假的 bundle，讓這輪的 sweep 真的想把它搬到隔離資料夾）
    fake_bundle = f"GITBUNDLE-{annex.uuid[:8]}-{'b' * 64}"
    real_drive.create(prefix_id, fake_bundle, b"not a real bundle")
    # 注意：sweep 會先建好日期子資料夾再搬檔，所以「中止後隔離資料夾裡多了一個空資料夾」
    # 是預期行為；真正要驗的是沒有任何**檔案**被搬走。
    assert _files_under(real_drive, quarantine_id) == set()

    _put_session(real_drive, inbox_id, signer, _raw_for(S2), S2, "2026-09-27T13:10:00Z")
    flaky_move = _FailOnce503(real_drive, fail_on="move")
    report_move = run(cfg, _deps(flaky_move), dry_run=False)
    assert flaky_move.failed, "測試沒有真的在搬移時注入 503"
    assert report_move.ok is False, "搬移失敗時這一輪必須中止"
    assert _files_under(real_drive, quarantine_id) == set(), (
        "搬移失敗時不該有檔案進到隔離資料夾"
    )
    assert fake_bundle in {f.name for f in real_drive.list_children(prefix_id)}, (
        "搬移失敗時東西必須留在原處（不能搬一半）"
    )
    assert [f.name for f in real_drive.list_children(inbox_id)], "中止後收件匣不該被清空"

    # ── (d) 再恢復一次：正常完成，假 bundle 這次才被隔離 ────────────
    _put_session(real_drive, inbox_id, signer, _raw_for(S3), S3, "2026-09-27T13:20:00Z")
    report3 = run(cfg, _deps(real_drive), dry_run=False)
    assert report3.ok is True, f"恢復後中止於 {report3.aborted_at}:{report3.code}"
    assert report3.counts["quarantined_files"] >= 1, report3.counts
    assert real_drive.list_children(inbox_id) == [], "恢復後收件匣應該清空"
    state_final, _ = pins.load(cfg.repo)
    assert state_final.refs["refs/heads/main"] != state_before.refs["refs/heads/main"]
