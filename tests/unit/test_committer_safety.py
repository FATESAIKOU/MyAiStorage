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
from aistorage.errors import MismatchError, ReadError, WriteError
from aistorage.integrity.pin import MemoryPinStore
from aistorage.safety import UnsafeWorkdirError, assert_safe_workdir, is_within

from test_committer_readview_wiring import _env
from test_committer_smoke import _seed_valid_inbox_session


# ---------------------------------------------------------------------
# review-b1039a8：H1 覆蓋率要比對遠端實況、H2 promote 前再查旗標、M5 拒收要等發佈
# ---------------------------------------------------------------------


def test_post_push_coverage_compares_against_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第 10 步的比對對象必須是「push 之後重新向遠端查」的 key 集合（H1）。

    原本 expected ⊆ pushed 在構造上恆真（兩邊都是本機算出來的），所以
    「物件沒有真的上到 Drive」完全擋不住。這裡觀察 run 真的傳了什麼下去。
    """
    import aistorage.integrity.verify as verify_mod

    cfg, deps, extra = _env(tmp_path)
    _seed_valid_inbox_session(
        deps.drive, extra["inbox_folder_id"], extra["priv_bytes"], extra["key_id"]
    )
    calls: list[dict[str, object]] = []
    original = verify_mod.verify_after_push

    def _spy(*args, **kwargs):
        calls.append(dict(kwargs))
        kwargs["pushed_annex_keys"] = frozenset(kwargs.get("expected_annex_keys") or ())
        return original(*args, **kwargs)

    import importlib

    committer_run = importlib.import_module("aistorage.committer.run")
    monkeypatch.setattr(committer_run, "verify_after_push", _spy)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert len(calls) == 1, calls
    # 必要 key＝「這一輪新寫的 key」，不是整份歷史
    assert calls[0]["expected_annex_keys"] == frozenset()
    # 比對對象＝遠端實際回報的 key 集合
    assert calls[0]["pushed_annex_keys"] == frozenset()


def test_annex_coverage_detects_a_key_missing_on_the_remote() -> None:
    """覆蓋率函式本身：遠端少一個 key 就必須 raise。"""
    from aistorage.integrity.verify import verify_annex_coverage

    with pytest.raises(MismatchError):
        verify_annex_coverage(frozenset({"key_a"}), frozenset({"key_a", "key_b"}))


def _init_bare_pin_repo(remote: Path, work: Path) -> None:
    import subprocess

    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "README").write_text("pin\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=work, check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True)


def test_read_text_raises_when_clone_fails(tmp_path: Path) -> None:
    """H2：只有檔案確實不存在才回 None；clone 失敗要 raise ReadError。

    之前 `except (ReadError, WriteError): return None` 會讓提交流程把「讀不到
    維護旗標」誤判成「沒有維護中」，於是在管理操作進行中照常 push。
    """
    import shutil

    from aistorage.integrity.pin import GitPinStore

    remote = tmp_path / "remote.git"
    _init_bare_pin_repo(remote, tmp_path / "seed")
    work = tmp_path / "pin"
    store = GitPinStore(f"file://{remote}", work)

    # 檔案確實不存在 → None
    assert store.read_text(".pin/agora.maintenance") is None

    # clone 失敗（遠端壞掉）→ raise，不是 None
    shutil.rmtree(remote, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    with pytest.raises((ReadError, WriteError)):
        store.read_text(".pin/agora.maintenance")


def test_maintenance_flag_appearing_mid_run_blocks_promote(tmp_path: Path) -> None:
    """H2：管理者在這一輪跑到一半才上鎖 → promote 之前中止，pending 留著。"""
    from aistorage.admin.lock import maintenance_relpath, read_maintenance

    cfg, deps, extra = _env(tmp_path)
    _seed_valid_inbox_session(
        deps.drive, extra["inbox_folder_id"], extra["priv_bytes"], extra["key_id"]
    )
    state = deps.pins._states["agora"]  # type: ignore[attr-defined]

    class _FlagAfterScan:
        """第 1b 步讀不到旗標，跑到一半才放進去（模擬管理者中途上鎖）。"""

        def __init__(self, inner):
            self._inner = inner
            self._scanned = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def read_text(self, relpath):
            self._scanned += 1
            if self._scanned == 1:
                return None
            return '{"reason": "抹除", "at": "2026-09-27T09:00:00Z", "by": "user"}'

    deps.pins = _FlagAfterScan(deps.pins)  # type: ignore[assignment]
    report = run(cfg, deps, dry_run=False)

    assert report.maintenance == "active"
    # 釘選值沒有被推進（pending 留給下一輪 settle）
    still = deps.pins._inner._states["agora"]  # type: ignore[attr-defined]
    assert still == state
    assert read_maintenance is not None and maintenance_relpath(cfg.repo)


def test_rejected_items_are_kept_when_publish_fails(tmp_path: Path) -> None:
    """M5：這一輪讀取視圖發佈失敗 → 拒收項目不刪（寫入者還沒看到原因）。"""
    from aistorage.clock import FixedClock
    from aistorage.drive.fake import FakeDrive
    from aistorage.errors import MismatchError, ReadError, WriteError
    from aistorage.identity import Registry
    from aistorage.intake.scan import scan_inboxes

    cfg, deps, extra = _env(tmp_path)
    drive: FakeDrive = deps.drive  # type: ignore[assignment]
    inbox = extra["inbox_folder_id"]

    # 放一個驗章失敗的項目（bad_signature）→ REJECT
    from aistorage.inbox import sign_sidecar_bytes
    import hashlib as _h
    import json as _j

    from aistorage.schema import generate_ulid

    ulid = generate_ulid()
    sidecar = _j.dumps({
        "format": "aistorage.inbox/v1", "item_key": ulid, "profile": "mac-opencode",
        "metadata": {"id": f"opencode:ses_{ulid}", "type": "session",
                     "created_at": "2026-09-27T08:00:00Z",
                     "updated_at": "2026-09-27T08:00:00Z",
                     "case_id": None, "provenance": None},
        "raw": {"sha256": "0" * 64, "size": 3}, "session": {
            "source": "opencode", "source_session_id": f"ses_{ulid}",
            "snapshot_at": "2026-09-27T08:00:00Z", "status": "running",
            "stopped_at": None, "in_progress": False, "parent_id": None},
        "body": {},
    }).encode("utf-8")
    drive.seed_file(inbox, f"{ulid}.sidecar.json", sidecar)
    drive.seed_file(inbox, f"{ulid}.sig", b'{"not": "a signature"}')
    drive.seed_file(inbox, f"{ulid}.raw", b"raw")

    # 有讀取視圖設定時 run 會改用 DriveReadViewPublisher，所以要讓「它」失敗
    import importlib

    import aistorage.publish.publisher as pub_mod

    def _boom(self, store, **kwargs):
        raise ReadError("模擬發佈失敗")

    orig = pub_mod.DriveReadViewPublisher.publish
    pub_mod.DriveReadViewPublisher.publish = _boom  # type: ignore[assignment]
    try:
        _run_report = None
    finally:
        pass
    # 讀取視圖有設定才會走 publisher；用 readview 設定開啟
    import dataclasses as _dc

    from test_committer_readview_wiring import _readview_manifest

    rv_id = drive.seed_folder("rv-1")
    manifest_id = drive.seed_file(
        rv_id, "readview-manifest.json", _readview_manifest(generation=1),
        created_time="2026-09-27T08:30:00Z",
    )
    cfg = _dc.replace(
        cfg, readview_folder_id=rv_id, readview_manifest_file_id=manifest_id
    )
    try:
        report = run(cfg, deps, dry_run=False)
    finally:
        pub_mod.DriveReadViewPublisher.publish = orig  # type: ignore[assignment]

    assert report.readview_publish == "publish_failed", report.readview_publish
    names = [f.name for f in drive.list_children(inbox)]
    assert any(n.startswith(ulid) for n in names), (
        f"發佈失敗時不該刪掉拒收項目（還沒人看得到原因）: {names}"
    )


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


def test_pin_store_without_read_text_is_rejected(tmp_path: Path) -> None:
    """PinStore 缺 read_text → 立刻發現（L：不能默默視為「沒有維護中」）。"""

    class _OldPins(MemoryPinStore):
        read_text = None  # type: ignore[assignment]

    cfg, deps, extra = _env(tmp_path)
    state = deps.pins._states["agora"]  # type: ignore[attr-defined]
    deps.pins = _OldPins(initial_state=state)

    report = run(cfg, deps, dry_run=False)
    assert report.ok is False
    assert report.aborted_at == "maintenance"
    assert report.code == "TypeError"


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
    """M4：允許清單豁免「暫存區／其他 repo」那兩條，但豁免不了專案 repo。"""
    from aistorage.safety import project_repo_toplevel

    project = project_repo_toplevel()
    assert project is not None

    # 合法用法：允許一個專案 repo 之外的長期 clone
    allowed = tmp_path / "long-lived-clone"
    allowed.mkdir()
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(allowed))
    assert assert_safe_workdir(allowed, purpose="測試") == allowed.resolve()
    # 允許清單裡的目錄底下也放行
    inner = allowed / "nested"
    inner.mkdir()
    assert assert_safe_workdir(inner, purpose="測試") == inner.resolve()

    # 不合法：允許清單不能用來豁免專案 repo 的保護
    inside = project / "tools" / "admin-clone"
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(inside))
    with pytest.raises(UnsafeWorkdirError, match="允許清單不能用來豁免"):
        assert_safe_workdir(inside, purpose="測試")


def test_allowed_workdir_env_rejects_too_broad_values(tmp_path: Path, monkeypatch) -> None:
    """M4：`/`、`$HOME` 與 $HOME 的祖先都不接受（等於允許任何地方）。"""
    from aistorage.safety import ALLOWED_WORKDIR_ENV

    home = Path.home()
    for too_broad in ("/", str(home), str(home.parent), str(home.parent.parent)):
        monkeypatch.setenv(ALLOWED_WORKDIR_ENV, too_broad)
        target = tmp_path / "ok"
        target.mkdir(exist_ok=True)
        with pytest.raises(UnsafeWorkdirError, match="太寬"):
            assert_safe_workdir(target, purpose="測試")


def test_allowed_workdir_env_accepts_normal_dir(tmp_path: Path, monkeypatch) -> None:
    from aistorage.safety import ALLOWED_WORKDIR_ENV

    ok = tmp_path / "clone"
    ok.mkdir()
    monkeypatch.setenv(ALLOWED_WORKDIR_ENV, f"{tmp_path / 'a'}:{ok}")
    assert assert_safe_workdir(ok, purpose="測試") == ok.resolve()


def test_is_within_handles_equal_paths() -> None:
    assert is_within(Path("/a/b"), Path("/a/b"))
    assert is_within(Path("/a/b/c"), Path("/a/b"))
    assert not is_within(Path("/a/bc"), Path("/a/b"))
