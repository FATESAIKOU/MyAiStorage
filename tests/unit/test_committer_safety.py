"""review-g5-6 的 H4（維護旗標）與 M8（git-annex／filter-repo 執行目錄防呆）。

H4：管理操作（抹除、回滾）進行中時，提交流程整輪不做任何事（不清掃、不 push、
不發佈、不刪收件匣）；旗標內容損毀 → fail-closed 中止。
M8：會改寫 repo 的 git 指令（git-annex、filter-repo）不得在專案 repo 裡執行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aistorage.annex.git import SubprocessAnnexGit
from aistorage.committer.run import Deps, run
from aistorage.drive.fake import FakeDrive
from aistorage.integrity.pin import MemoryPinStore
from aistorage.safety import UnsafeWorkdirError, assert_safe_workdir, is_within

from test_committer_readview_wiring import _env
from test_committer_smoke import _seed_valid_inbox_session


# ---------------------------------------------------------------------
# annex_keys_in 的 key 解析（整合測試發現的真實缺陷）
# ---------------------------------------------------------------------


def test_annex_keys_in_parses_one_key_per_line(tmp_path: Path, monkeypatch) -> None:
    """`git annex find --format` 不會自動換行，必須自己帶 `\n`。

    沒有換行時所有 key 會被串成一行，pin 只記到 1 個垃圾字串，下一輪 sweep 就會
    把真的 annex 物件（閱讀版、meta.json…）全部隔離——這是整合測試在真 Drive 上
    才發現的。這裡用假的 _run 模擬 git-annex 的真實輸出來釘住行為。
    """
    from aistorage.annex.git import SubprocessAnnexGit

    keys = [
        "SHA256E-s240--aaa.json",
        "SHA256E-s320--bbb.json",
        "SHA256E-s307200--ccc.bin",
    ]
    captured: list[list[str]] = []

    class _Fake(SubprocessAnnexGit):
        def _run(self, cmd, *, is_write=False, timeout=60.0):  # type: ignore[override]
            captured.append(list(cmd))
            # git-annex 的 --format 不補換行，除非格式裡自己帶
            fmt = [c for c in cmd if c.startswith("--format=")]
            assert fmt and fmt[0] == "--format=${key}\n", fmt
            return "".join(k + "\n" for k in keys)

    got = _Fake(tmp_path).annex_keys_in("uuid-1")

    assert got == frozenset(keys)
    assert captured[0][:2] == ["git", "annex"]


def test_annex_keys_in_uses_in_filter_and_no_all_flag(tmp_path: Path, monkeypatch) -> None:
    """git-annex 10.x 的 find 沒有 --all（原本的寫法必定 rc=1）。"""
    from aistorage.annex.git import SubprocessAnnexGit

    seen: list[list[str]] = []

    class _Fake(SubprocessAnnexGit):
        def _run(self, cmd, *, is_write=False, timeout=60.0):  # type: ignore[override]
            seen.append(list(cmd))
            return ""

    _Fake(tmp_path).annex_keys_in("uuid-2")

    assert "--in=uuid-2" in seen[0]
    assert "--all" not in seen[0]


# ---------------------------------------------------------------------
# H4：維護旗標
# ---------------------------------------------------------------------


def _with_maintenance(tmp_path: Path, *, raw: str | None) -> tuple:
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _env(tmp_path)
    texts = {}
    if raw is not None:
        texts[maintenance_relpath(cfg.repo)] = raw
    deps.pins = MemoryPinStore(
        initial_state=deps.pins._states["agora"],  # type: ignore[attr-defined]
        texts=texts,
    )
    return cfg, deps, extra


FLAG = '{"reason": "抹除 6.1", "at": "2026-09-27T09:00:00Z", "by": "user"}'


def test_maintenance_flag_stops_the_run_before_anything_moves(tmp_path: Path) -> None:
    cfg, deps, extra = _with_maintenance(tmp_path, raw=FLAG)
    drive: FakeDrive = deps.drive  # type: ignore[assignment]

    before_prefix = {f.name: f.id for f in drive.list_children(cfg.prefix_folder_id)}
    before_inbox = {f.name for f in drive.list_children(extra["inbox_folder_id"])}

    report = run(cfg, deps, dry_run=False)

    assert report.maintenance == "active"
    assert report.aborted_at is None
    # 整輪結束，什麼都沒動
    assert {f.name: f.id for f in drive.list_children(cfg.prefix_folder_id)} == before_prefix
    assert {f.name for f in drive.list_children(extra["inbox_folder_id"])} == before_inbox
    assert drive.list_children(cfg.quarantine_folder_id) == []
    assert report.readview_publish is None
    assert report.maintenance_reason == "抹除 6.1"


def test_corrupt_maintenance_flag_fails_closed(tmp_path: Path) -> None:
    """旗標內容損毀 → 中止（不猜、不當成沒旗標）。"""
    cfg, deps, extra = _with_maintenance(tmp_path, raw="{ not json")
    drive: FakeDrive = deps.drive  # type: ignore[assignment]
    before_inbox = {f.name for f in drive.list_children(extra["inbox_folder_id"])}

    report = run(cfg, deps, dry_run=False)

    assert report.ok is False
    assert report.aborted_at == "maintenance"
    assert report.code == "flag_corrupt"
    assert {f.name for f in drive.list_children(extra["inbox_folder_id"])} == before_inbox


def test_no_maintenance_flag_runs_normally(tmp_path: Path) -> None:
    cfg, deps, extra = _with_maintenance(tmp_path, raw=None)
    report = run(cfg, deps, dry_run=False)
    assert report.maintenance is None
    assert report.ok is True, report.aborted_at
    assert report.counts["accepted"] == 1


def test_pin_store_read_text_is_optional_for_old_fakes(tmp_path: Path) -> None:
    """沒有 read_text 的 PinStore（例如舊 fake）不等於維護中，照常跑。"""

    class _OldPins(MemoryPinStore):
        read_text = None  # type: ignore[assignment]

    cfg, deps, extra = _env(tmp_path)
    state = deps.pins._states["agora"]  # type: ignore[attr-defined]
    deps.pins = _OldPins(initial_state=state)

    report = run(cfg, deps, dry_run=False)
    assert report.maintenance is None
    assert report.ok is True


# ---------------------------------------------------------------------
# M8：執行目錄防呆
# ---------------------------------------------------------------------


def test_workdir_inside_project_repo_is_rejected(tmp_path: Path) -> None:
    from aistorage.safety import project_repo_toplevel

    project = project_repo_toplevel()
    assert project is not None, "應該找得到專案 repo"

    with pytest.raises(UnsafeWorkdirError, match="專案 repo"):
        assert_safe_workdir(project / "somewhere", purpose="測試")


def test_workdir_in_temp_is_allowed(tmp_path: Path) -> None:
    got = assert_safe_workdir(tmp_path / "ok", purpose="測試", require_temp=True)
    assert got == (tmp_path / "ok").resolve()


def test_require_temp_rejects_non_temp(tmp_path: Path) -> None:
    import tempfile

    outside = Path.home() / ".somewhere-not-temp"
    with pytest.raises(UnsafeWorkdirError, match="不在暫存區"):
        assert_safe_workdir(outside, purpose="測試", require_temp=True)
    assert Path(tempfile.gettempdir())  # 至少 tmpdir 存在


def test_require_annex_remote_rejects_plain_repo(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "plain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)

    with pytest.raises(UnsafeWorkdirError, match="annex::"):
        assert_safe_workdir(repo, purpose="測試", require_annex_remote=True)

    # 設成 annex:: 遠端就放行
    subprocess.run(
        ["git", "remote", "add", "origin", "annex::deadbeef?encryption=none"],
        cwd=repo, check=True,
    )
    assert assert_safe_workdir(repo, purpose="測試", require_annex_remote=True) == repo.resolve()


def test_subprocess_annex_git_refuses_project_repo() -> None:
    from aistorage.safety import project_repo_toplevel

    project = project_repo_toplevel()
    assert project is not None
    with pytest.raises(UnsafeWorkdirError):
        SubprocessAnnexGit(project / "sub")


def test_allowed_workdir_env_is_the_only_escape_hatch(tmp_path: Path, monkeypatch) -> None:
    from aistorage.safety import project_repo_toplevel

    project = project_repo_toplevel()
    assert project is not None
    target = project / "tools" / "admin-clone"
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(target))
    assert assert_safe_workdir(target, purpose="測試") == target.resolve()


def test_is_within_handles_equal_paths() -> None:
    assert is_within(Path("/a/b"), Path("/a/b"))
    assert is_within(Path("/a/b/c"), Path("/a/b"))
    assert not is_within(Path("/a/bc"), Path("/a/b"))
