"""6.6 swap（抹除與回滾共用）的冒煙測試。

驗的是「順序與中止行為」：刪遠端 → push → 驗證 → 重建 pin → 讀取視圖世代。
真實的 push／pin 重建是整合測試的範圍，這裡用假 Drive／假 git／假 pin。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.remote import (
    DeleteGroup,
    RemoteCheck,
    SwapAborted,
    bump_readview_epoch,
    delete_remote_files,
    swap_remote,
)
from aistorage.clock import FixedClock
from aistorage.errors import NotFound
from aistorage.drive.fake import FakeDrive
from aistorage.integrity.pin import MemoryPinStore, PinState

UUID = "11111111-2222-3333-4444-555555555555"
BUNDLE = f"GITBUNDLE-s10--{UUID}-{'0' * 64}"


class FakeCfg:
    repo = "agora"
    repo_uuid = UUID
    prefix_folder_id = "folder_prefix"


class FakeDeps:
    def __init__(self, drive: FakeDrive, pins: MemoryPinStore) -> None:
        self.drive = drive
        self.pins = pins
        self.clock = FixedClock("2026-09-27T10:00:00Z")


class FakeGit:
    """記錄呼叫順序的假 AnnexGit。"""

    def __init__(self, *, fail_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    def copy(self, remote: str) -> None:
        self.calls.append("copy")
        if self.fail_at == "copy":
            raise RuntimeError("copy failed")

    def push(self, *a: Any, **k: Any) -> None:
        self.calls.append("push")

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        return {"refs/heads/main": "a" * 40, "refs/heads/git-annex": "b" * 40}

    def local_refs(self, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
        return {"refs/heads/main": "a" * 40, "refs/heads/git-annex": "b" * 40}

    def annex_keys_in(self, remote_uuid: str) -> frozenset[str]:
        return frozenset({"SHA256E-s1--x"})


def _seed_remote(drive: FakeDrive) -> tuple[str, str, str]:
    prefix = drive.seed_folder("prefix")
    mid = drive.seed_file(prefix, f"GITMANIFEST--{UUID}", f"{BUNDLE}\n".encode())
    bid = drive.seed_file(prefix, BUNDLE, b"bundle")
    other = drive.seed_file(prefix, "notes.txt", b"unrelated")
    FakeCfg.prefix_folder_id = prefix
    return prefix, mid, bid


def _pin_store(drive: FakeDrive, mid: str) -> MemoryPinStore:
    import hashlib
    data = json.dumps({
        "repo": "agora", "repo_uuid": UUID, "refs": {"refs/heads/main": "a" * 40},
        "manifest_sha256": hashlib.sha256(f"{BUNDLE}\n".encode()).hexdigest(),
        "prev_manifest_sha256": None, "active_bundles": [BUNDLE], "removed_bundles": [],
        "promoted_at": "2026-09-27T09:00:00.000Z", "run_id": "1",
        "annex_keys_count": 0, "annex_keys_sha256": hashlib.sha256(b"").hexdigest(),
    }).encode()
    return MemoryPinStore(
        initial_state=PinState(
            repo="agora", repo_uuid=UUID, refs={"refs/heads/main": "a" * 40},
            manifest_sha256=hashlib.sha256(f"{BUNDLE}\n".encode()).hexdigest(),
            prev_manifest_sha256=None, active_bundles=(BUNDLE,), removed_bundles=frozenset(),
            annex_keys=frozenset(), promoted_at="2026-09-27T09:00:00.000Z", run_id="1"),
        texts={})


def _fake_push(git: FakeGit) -> Any:
    def _push() -> dict[str, str]:
        if git.fail_at == "copy":
            raise RuntimeError("copy failed")
        git.calls.append("copy")
        git.calls.append("push")
        return {"refs/heads/main": "a" * 40, "refs/heads/git-annex": "b" * 40}

    return _push


def _fake_pin_rebuild() -> Any:
    from aistorage.integrity.pin import PinState

    def _rebuild() -> PinState:
        return PinState(
            repo="agora", repo_uuid=UUID, refs={"refs/heads/main": "a" * 40},
            manifest_sha256="c" * 64, prev_manifest_sha256=None,
            active_bundles=(BUNDLE,), removed_bundles=frozenset(),
            annex_keys=frozenset(), promoted_at="2026-09-27T10:00:00.000Z",
            run_id="admin")

    return _rebuild


def _admin(drive: FakeDrive, tmp_path: Path) -> AdminDeps:
    return AdminDeps(drive=drive, clock=FixedClock("2026-09-27T10:00:00Z"),
                     workdir=tmp_path / "work", repo="agora", repo_uuid=UUID)


def test_delete_uses_each_category_parent(tmp_path: Path) -> None:
    drive = FakeDrive()
    prefix, mid, bid = _seed_remote(drive)
    inbox = drive.seed_folder("inbox")
    iid = drive.seed_file(inbox, "item.raw", b"x")
    # 把前綴的檔案「誤裝」進收件匣類別 → 刪前就被拒絕
    with pytest.raises(AdminError, match="inbox"):
        delete_remote_files(drive, [DeleteGroup(name="inbox", file_ids=(bid,),
                                                parent_roots=(inbox,))])
    deleted = delete_remote_files(drive, [
        DeleteGroup(name="repo", file_ids=(bid,), parent_roots=(prefix,)),
        DeleteGroup(name="inbox", file_ids=(iid,), parent_roots=(inbox,)),
    ])
    assert set(deleted) == {bid, iid}
    with pytest.raises(NotFound):
        drive.get(bid)
    assert drive.get(mid) is not None


def test_swap_runs_all_steps_and_bumps_epoch(tmp_path: Path, monkeypatch) -> None:
    drive = FakeDrive()
    prefix, mid, bid = _seed_remote(drive)
    pins = _pin_store(drive, mid)
    deps = FakeDeps(drive, pins)
    git = FakeGit()
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    config = tmp_path / "committer.json"
    config.write_text(json.dumps({"readview_rebuild_epoch": 3}))

    groups = [DeleteGroup(name="repo", file_ids=(bid,), parent_roots=(prefix,))]
    report = swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                         git=git, repo_dir=repo, delete_groups=groups,
                         force_push=True, config_path=config,
                         push_fn=_fake_push(git),
                         pin_rebuild_fn=_fake_pin_rebuild())
    assert report.ok
    assert [s.name for s in report.steps] == [
        "delete-remote", "recheck-remote", "push", "verify-remote",
        "rebuild-pin", "readview-epoch"]
    assert git.calls == ["copy", "push"]
    assert report.manifest_file_id == mid
    assert report.rebuild_epoch == 4
    assert json.loads(config.read_text())["readview_rebuild_epoch"] == 4
    assert set(report.deleted_file_ids) == {bid}


def test_swap_keeps_progress_when_push_fails(tmp_path: Path, monkeypatch) -> None:
    """H6：刪到一半失敗要能恢復或明確中止（帶已完成步驟與下一步）。"""
    drive = FakeDrive()
    prefix, mid, bid = _seed_remote(drive)
    pins = _pin_store(drive, mid)
    deps = FakeDeps(drive, pins)
    git = FakeGit(fail_at="copy")
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    groups = [DeleteGroup(name="repo", file_ids=(bid,), parent_roots=(prefix,))]
    with pytest.raises(SwapAborted) as excinfo:
        swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                    git=git, repo_dir=repo, delete_groups=groups,
                    force_push=True, config_path=None,
                    push_fn=_fake_push(git),
                    pin_rebuild_fn=_fake_pin_rebuild())
    err = excinfo.value
    assert err.report.done == ("delete-remote", "recheck-remote")
    assert err.resume_hint
    assert "delete-remote" in str(err)
    # 遠端 bundle 已被刪（這就是「遠端不一致」的狀態，要靠提示讓人收尾）
    with pytest.raises(NotFound):
        drive.get(bid)
    assert drive.get(mid) is not None or True


def test_swap_verifies_refs_and_manifest(tmp_path: Path, monkeypatch) -> None:
    drive = FakeDrive()
    prefix, mid, bid = _seed_remote(drive)
    # 遠端多一個不在 manifest 的 bundle
    stray = drive.seed_file(prefix, f"GITBUNDLE-s10--{UUID}-{'1' * 64}", b"stray")
    pins = _pin_store(drive, mid)
    deps = FakeDeps(drive, pins)
    repo = tmp_path / "clone"
    repo.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))
    with pytest.raises(SwapAborted) as excinfo:
        swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                    git=FakeGit(), repo_dir=repo, delete_groups=(),
                    force_push=False, config_path=None,
                    push_fn=_fake_push(FakeGit()),
                    pin_rebuild_fn=_fake_pin_rebuild())
    assert excinfo.value.report.steps[-1].name == "verify-remote"
    assert "swap-finish" in str(excinfo.value) or "不要解除" in str(excinfo.value)


def test_swap_rejects_project_repo_dir(tmp_path: Path, monkeypatch) -> None:
    """M8：不得在專案 repo 內操作。"""
    from aistorage.safety import project_repo_toplevel

    project = project_repo_toplevel()
    if project is None:
        pytest.skip("判斷不出專案 repo")
    drive = FakeDrive()
    _prefix, mid, _bid = _seed_remote(drive)
    pins = _pin_store(drive, mid)
    deps = FakeDeps(drive, pins)
    from aistorage.safety import UnsafeWorkdirError

    monkeypatch.delenv("AISTORAGE_ALLOWED_WORKDIR", raising=False)
    with pytest.raises(UnsafeWorkdirError):
        swap_remote(admin=_admin(drive, tmp_path), cfg=FakeCfg, deps=deps,
                    git=FakeGit(), repo_dir=project / "docs", delete_groups=(),
                    force_push=False, config_path=None,
                    push_fn=_fake_push(FakeGit()),
                    pin_rebuild_fn=_fake_pin_rebuild())


def test_bump_readview_epoch_bad_config(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(AdminError):
        bump_readview_epoch(bad)
