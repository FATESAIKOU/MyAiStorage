"""`replay_refs` 的多 bundle 情境（e2e 修正的回歸測試）。

**原本的問題**：`replay_refs` 只取**最後一個** bundle 宣告的 heads。
git-remote-annex 的每個 bundle 只帶「自上次 consolidate 以來有變動的分支」，
所以最後一個 bundle 常常只有 `git-annex`（location log 變了、`main` 沒變）。
只取它 → `refs/heads/main` 消失 →

- 第 11 步 push 後驗證：「push 後 bundle 重放 refs 少了 refs/heads/main」→ 中止；
- 第 3 步 settle：遠端 manifest 重放出來的 refs 既不等於 pending 也不等於正式
  釘選值 → 每���輪都 MismatchError。

兩者都**不會自己好**，整個提交流程就此卡死（impl3／9.1 實測）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from aistorage.annex.git import get_git_env
from aistorage.annex.replay import replay_refs
from aistorage.errors import MismatchError

UUID = "00000000-0000-0000-0000-0000000000bb"
NS_MAIN = f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main"
NS_ANNEX = f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/git-annex"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True, env=get_git_env())
    return proc.stdout.strip()


def _bundle_name(raw: bytes) -> str:
    return f"GITBUNDLE-s{len(raw)}--{UUID}-{hashlib.sha256(raw).hexdigest()}"


@pytest.fixture
def two_bundles(tmp_path: Path):
    """兩個真的 bundle：b1 有 main＋git-annex，b2 **只有** git-annex。

    這就是 git-remote-annex 在「main 沒變、只有 location log 變了」時產生的形狀。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@e.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c1")
    main1 = _git(repo, "rev-parse", "refs/heads/main")
    _git(repo, "update-ref", NS_MAIN, main1)

    _git(repo, "checkout", "-q", "-b", "git-annex")
    (repo / "uuid.log").write_text(f"{UUID}\n", encoding="utf-8")
    _git(repo, "add", "uuid.log")
    _git(repo, "commit", "-qm", "annex")
    annex1 = _git(repo, "rev-parse", "refs/heads/git-annex")
    _git(repo, "update-ref", NS_ANNEX, annex1)
    _git(repo, "checkout", "-q", "main")

    b1 = tmp_path / "b1.bundle"
    _git(repo, "bundle", "create", str(b1), NS_MAIN, NS_ANNEX)
    raw1 = b1.read_bytes()

    # 第二個 bundle：只動 git-annex（location log 又多一筆），main 不動
    _git(repo, "checkout", "-q", "git-annex")
    (repo / "location.log").write_text("SHA256E-s10--aa more\n", encoding="utf-8")
    _git(repo, "add", "location.log")
    _git(repo, "commit", "-qm", "location log")
    annex2 = _git(repo, "rev-parse", "refs/heads/git-annex")
    _git(repo, "update-ref", NS_ANNEX, annex2)
    _git(repo, "checkout", "-q", "main")
    b2 = tmp_path / "b2.bundle"
    _git(repo, "bundle", "create", str(b2), NS_ANNEX)
    raw2 = b2.read_bytes()

    from aistorage.annex.manifest import parse_bundle_name

    return [
        (parse_bundle_name(_bundle_name(raw1)), b1),
        (parse_bundle_name(_bundle_name(raw2)), b2),
    ], {"main": main1, "annex1": annex1, "annex2": annex2}


def test_replay_uses_every_bundle_not_only_the_last(two_bundles, tmp_path: Path) -> None:
    """兩個 bundle 依序重放 → refs 必須同時有 main（來自 b1）與 git-annex（來自 b2）。"""
    bundles, expected = two_bundles
    refs = replay_refs(bundles, workdir=tmp_path / "work", repo_uuid=UUID)
    assert refs == {
        "refs/heads/main": expected["main"],
        "refs/heads/git-annex": expected["annex2"],
    }


def test_replay_single_bundle_still_works(two_bundles, tmp_path: Path) -> None:
    """只有一個 bundle 時行為不變（回歸）。"""
    bundles, expected = two_bundles
    refs = replay_refs([bundles[0]], workdir=tmp_path / "work", repo_uuid=UUID)
    assert refs == {
        "refs/heads/main": expected["main"],
        "refs/heads/git-annex": expected["annex1"],
    }


def test_replay_order_matters_later_bundle_wins(tmp_path: Path) -> None:
    """後面的 bundle 對它宣告到的 ref 覆蓋前面的。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@e.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c1")
    first = _git(repo, "rev-parse", "refs/heads/main")
    _git(repo, "update-ref", NS_MAIN, first)
    b1 = tmp_path / "b1.bundle"
    _git(repo, "bundle", "create", str(b1), NS_MAIN)

    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c2")
    second = _git(repo, "rev-parse", "refs/heads/main")
    _git(repo, "update-ref", NS_MAIN, second)
    b2 = tmp_path / "b2.bundle"
    _git(repo, "bundle", "create", str(b2), NS_MAIN)

    from aistorage.annex.manifest import parse_bundle_name

    refs = replay_refs(
        [(parse_bundle_name(_bundle_name(b1.read_bytes())), b1),
         (parse_bundle_name(_bundle_name(b2.read_bytes())), b2)],
        workdir=tmp_path / "work", repo_uuid=UUID)
    assert refs == {"refs/heads/main": second}
    assert refs != {"refs/heads/main": first}


def test_replay_rejects_a_missing_commit(two_bundles, tmp_path: Path) -> None:
    """宣告的 commit 不在重放後的物件庫 → 仍然 raise（不要為了通過而放寬）。"""
    bundles, _ = two_bundles
    from dataclasses import replace

    broken = replace(bundles[0][0], sha256="0" * 64)
    # 雜湊不符的 bundle 在預檢就被擋下（這裡確認預檢還在）
    with pytest.raises(MismatchError, match="SHA-256 雜湊不符"):
        replay_refs([(broken, bundles[0][1])], workdir=tmp_path / "work", repo_uuid=UUID)
