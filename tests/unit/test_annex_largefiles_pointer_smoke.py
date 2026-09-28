"""review-b1039a8 的 M2／M3／L（largefiles 單一來源、指標解析、annex key 形狀）。

M2：`annex.largefiles` 的**單一來源**是 `annex.git.DEFAULT_LARGEFILES`，
     `clone_for_commit` 在 clone 時就設好；`AnnexRawStorage` 不再覆寫（只在
     還沒設定時補上預設），所以結果不再取決於建構順序。
M3：annex 指標的 key **直接從指標內容解析**；解析不出來就 raise，
     不再退回 `git annex get --all`（那會把所有 Session 的歷史 raw 全部下載）。
L：  `verify_annex_coverage` 的 key 形狀檢查只接受 SHA256E（WORM 沒有雜湊、不可驗證）。

git／git-annex 一律在 tmp_path 暫存目錄執行。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from aistorage.agora.store import AgoraStore, AnnexRawStorage
from aistorage.annex.git import DEFAULT_LARGEFILES, SubprocessAnnexGit
from aistorage.errors import MismatchError, ReadError
from aistorage.integrity.verify import (
    _is_plausible_annex_key,
    verify_annex_coverage,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {proc.stderr.strip()[-200:]}")
    return proc.stdout


def _git_soft(repo: Path, *args: str) -> tuple[int, str]:
    """不檢查 rc 的 git（`lookupkey` 在「不是 annex 物件」時本來就回 1）。"""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=120, check=False)
    return proc.returncode, proc.stdout.strip()


def _annex_repo(path: Path, *, largefiles: str | None = None) -> tuple[Path, str]:
    """真的 git-annex repo；largefiles 有給就設（模擬 Foundry 傳自己的規則）。"""
    from aistorage.annex.git import get_git_env

    env = get_git_env()
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main", "-q")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@t.invalid")
    (path / "README.md").write_text("x\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "seed")
    subprocess.run(["git", "-C", str(path), "annex", "init", "aistorage-it"],
                   env=env, check=True, capture_output=True)
    if largefiles is not None:
        _git(path, "config", "annex.largefiles", largefiles)
    return path, DEFAULT_LARGEFILES


# ------------------------------------------------------------------ M2


def test_clone_for_commit_sets_the_final_rule(tmp_path: Path) -> None:
    """M2：clone 就把最終規則設好（不再是 include=*.json）。"""
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    seed, _ = _annex_repo(tmp_path / "seed")
    uuid = [l.split(":", 1)[1].strip() for l in _git(seed, "annex", "info", "--fast").splitlines()
            if l.startswith("uuid:")][0] if False else None
    # 用 initremote 建一個可 push 的本機 annex 遠端，再以 clone_for_commit 取得 clone
    from aistorage.annex.git import get_git_env

    env = get_git_env()
    subprocess.run(["git", "-C", str(seed), "annex", "initremote", "origin",
                    "type=directory", "encryption=none", f"directory={remote_dir}",
                    "--with-url"], env=env, check=True, capture_output=True)
    uuid = [l.split(":", 1)[1].strip()
            for l in _git(seed, "annex", "info", "origin", "--fast").splitlines()
            if l.startswith("uuid:")][0]
    _git(seed, "annex", "copy", "--to", "origin")
    _git(seed, "push", "origin", "main", "git-annex")
    url = f"annex::{uuid}?type=directory&encryption=none&directory={remote_dir}"

    clone = SubprocessAnnexGit.clone_for_commit(url, tmp_path / "clone")
    rule = _git(clone.workdir, "config", "--get", "annex.largefiles").strip()
    assert rule == DEFAULT_LARGEFILES
    assert rule != "include=*.json"
    assert clone.workdir == (tmp_path / "clone").resolve()


def test_annex_raw_storage_does_not_override_an_existing_rule(tmp_path: Path) -> None:
    """M2：已經有規則（Foundry 的 `include=objects/*/*`）時不覆寫。"""
    repo, _ = _annex_repo(tmp_path / "foundry", largefiles="include=objects/*/*")
    AnnexRawStorage(repo)          # 預設是 Agora 的規則，但不該覆寫
    assert _git(repo, "config", "--get", "annex.largefiles").strip() == "include=objects/*/*"


def test_annex_raw_storage_fills_in_a_missing_rule(tmp_path: Path) -> None:
    """M2：完全沒設定時才補上預設（自己 git init 出來的 repo）。"""
    repo, _ = _annex_repo(tmp_path / "plain")
    subprocess.run(["git", "-C", str(repo), "config", "--unset-all", "annex.largefiles"],
                   check=False, capture_output=True)
    storage = AnnexRawStorage(repo)
    assert _git(repo, "config", "--get", "annex.largefiles").strip() == DEFAULT_LARGEFILES
    assert storage.largefiles == DEFAULT_LARGEFILES


def test_meta_json_stays_a_git_blob_with_the_default_rule(tmp_path: Path) -> None:
    """M2 的實際效果：`meta.json` 不得被 annex 收走（否則每一則控制記錄
    都變成 Drive 上的一個獨立物件，bundle 與 API 呼叫次數都會膨脹）。"""
    # 規則由 clone_for_commit（或 AnnexRawStorage 補上）設定，這裡直接給最終值
    repo, _ = _annex_repo(tmp_path / "agora", largefiles=DEFAULT_LARGEFILES)
    meta = repo / "sessions" / "opencode" / "s1" / "meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"id": "opencode:s1"}))
    _git(repo, "add", ".")
    rc, out = _git_soft(repo, "annex", "lookupkey", "sessions/opencode/s1/meta.json")
    assert (rc, out) != (0, ""), "meta.json 不該有 annex key"
    # 對照組：同樣規則下 raw 確實被 annex 收走
    raw = repo / "sessions" / "opencode" / "s1" / "raw"
    raw.write_bytes(b"RAW")
    _git(repo, "add", ".")
    rc_raw, out_raw = _git_soft(repo, "annex", "lookupkey", "sessions/opencode/s1/raw")
    assert rc_raw == 0 and out_raw.startswith("SHA256E-s3--")


# ------------------------------------------------------------------ M3


def test_annex_key_is_parsed_from_the_pointer_content(tmp_path: Path) -> None:
    repo, _ = _annex_repo(tmp_path / "agora")
    store = AgoraStore(repo, temp_dir=tmp_path / "tmp")
    key = "SHA256E-s2--" + "b" * 64 + ".json"
    p = repo / "sessions" / "opencode" / "s1" / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(f"/annex/objects/{key}\n".encode())
    assert store._annex_key_from_pointer(p) == key
    # `.git/annex/objects/` 前綴也認得
    p.write_bytes(f".git/annex/objects/{key}\n".encode())
    assert store._annex_key_from_pointer(p) == key
    # 不是指標 → None
    p.write_text('{"id": "x"}')
    assert store._annex_key_from_pointer(p) is None


def test_unparsable_pointer_raises_instead_of_get_all(tmp_path: Path) -> None:
    """M3：解析不出 key → MismatchError，**不**退回 `git annex get --all`。"""
    repo, _ = _annex_repo(tmp_path / "agora")
    store = AgoraStore(repo, git=SubprocessAnnexGit(repo), temp_dir=tmp_path / "tmp")
    p = repo / "sessions" / "opencode" / "s1" / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"/annex/objects/\n")          # 有指標前綴但沒有 key

    seen: list[list[str]] = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append(list(cmd))
        return real_run(cmd, *a, **kw)

    import aistorage.agora.store as store_mod

    store_mod.subprocess.run = _spy  # type: ignore[assignment]
    try:
        with pytest.raises(MismatchError, match="不再退回 get --all"):
            store._materialize_annexed("sessions/opencode/s1/meta.json")
    finally:
        store_mod.subprocess.run = real_run  # type: ignore[assignment]

    assert not any("--all" in c for c in seen), seen


def test_get_failure_raises_read_error(tmp_path: Path) -> None:
    repo, _ = _annex_repo(tmp_path / "agora")
    store = AgoraStore(repo, temp_dir=tmp_path / "tmp")
    p = repo / "sessions" / "opencode" / "s1" / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(f"/annex/objects/SHA256E-s2--{'c' * 64}.json\n".encode())

    class _FailingGit:
        def __getattr__(self, name):  # noqa: ANN001
            def _noop(*a, **k):  # noqa: ANN001
                return None
            return _noop

    store.git = _FailingGit()  # type: ignore[assignment]
    with pytest.raises(ReadError, match="取回 annex 物件失敗"):
        store._materialize_annexed("sessions/opencode/s1/meta.json")


def test_read_json_file_still_materialises_a_real_pointer(tmp_path: Path) -> None:
    """相容層保留：被 annex 收走的 JSON，pointer → `get --key` → 讀得到內容。

    （M2 的遷移期：已經進 annex 的 JSON 還在線上，這層要能撐住，
    直到它們被遷移成一般檔案。）
    """
    import os
    import stat

    from aistorage.annex.git import get_git_env

    repo, _ = _annex_repo(tmp_path / "agora", largefiles="anything")
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    env = get_git_env()
    subprocess.run(["git", "-C", str(repo), "annex", "initremote", "origin",
                    "type=directory", "encryption=none", f"directory={remote_dir}",
                    "--with-url"], env=env, check=True, capture_output=True)

    p = repo / "sessions" / "opencode" / "s1" / "meta.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": "opencode:s1"}))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add meta")
    key = _git(repo, "annex", "lookupkey", "sessions/opencode/s1/meta.json").strip()
    assert key.startswith("SHA256E-")
    _git(repo, "annex", "copy", "--to", "origin")

    # 把本機物件刪掉，模擬「clone 之後還沒抓回來」（路徑問 git-annex：
    # fan-out 是 key 雜湊推導的，不是 key 的字元）
    obj = repo / _git(repo, "annex", "contentlocation", key).strip()
    os.chmod(obj.parent, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)  # 父層也是唯讀
    os.chmod(obj, stat.S_IRUSR | stat.S_IWUSR)
    obj.unlink()
    assert not obj.exists()
    # 工作樹換成指標（這就是全新 clone 的樣子：index 是指標、內容還沒抓回來）
    p.write_bytes(f"/annex/objects/{key}\n".encode())
    assert p.read_bytes().startswith(b"/annex/objects/")

    store = AgoraStore(repo, git=SubprocessAnnexGit(repo), temp_dir=tmp_path / "tmp")
    assert store._annex_key_from_pointer(p) == key
    assert store.read_json_file("sessions/opencode/s1/meta.json") == {"id": "opencode:s1"}


# ------------------------------------------------------------------- L


def test_plausible_annex_key_only_accepts_sha256e() -> None:
    assert _is_plausible_annex_key("SHA256E-s10--" + "e" * 64)
    assert _is_plausible_annex_key("SHA256E-s10--" + "e" * 64 + ".pdf")
    # WORM 沒有雜湊、無法驗證內容 → 一律不合法
    assert not _is_plausible_annex_key("WORM-s10-m1700000000--report.pdf")
    assert not _is_plausible_annex_key("WORM-s10--" + "e" * 64)
    assert not _is_plausible_annex_key("SHA1-s10--" + "e" * 40)


def test_verify_annex_coverage_rejects_worm_key() -> None:
    with pytest.raises(Exception, match="形狀不合法"):
        verify_annex_coverage(frozenset(), {"WORM-s10-m1700000000--report.pdf"})
    verify_annex_coverage(frozenset({"SHA256E-s1--" + "f" * 64}),
                          {"SHA256E-s1--" + "f" * 64})
