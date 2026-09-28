"""git-annex／filter-repo 執行目錄的防呆（M8）。

為什麼需要：`git annex init`、`git annex copy`、`git-filter-repo --force` 這類指令
**會改寫它執行目錄所在的 repo**。萬一把專案 repo（MyAiStorage 原始碼的工作目錄）
當成真本或 pin repo 傳進去，就會在專案裡建立 git-annex 分支、加上 annex filter，
甚至改寫專案本身的歷史。這是不可逆的，所以做成硬性檢查：不符合就 raise。

規則（`assert_safe_workdir`）：
1. 工作目錄不得位於**目前的專案 repo** 之內（用 `git rev-parse --show-toplevel` 比較）。
2. 選用 `require_annex_remote=True` 時，該目錄必須是一個 git repo，且
   `remote.origin.url` 以 `annex::` 開頭（也就是確實指向真本，不是本機路徑）。
3. 選用 `require_temp=True` 時，該目錄必須在暫存區底下
   （`TMPDIR` 或傳入的 `temp_roots`）。

`allowed_workdir_env()` 是唯一的逃生門，而且它不是預設開啟的：管理操作在正式環境
需要指到一個長期存在的 clone 時，必須明確設 `AISTORAGE_ALLOWED_WORKDIR`，
值是該目錄（多個以 `:` 分隔）。提交流程自己用的是 `tempfile.mkdtemp`，
所以永遠不需要這個逃生門。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

ALLOWED_WORKDIR_ENV = "AISTORAGE_ALLOWED_WORKDIR"


class UnsafeWorkdirError(RuntimeError):
    """工作目錄不安全（可能會改寫專案 repo）。"""


def project_repo_toplevel() -> Path | None:
    """目前這個 Python 原始碼所在 repo 的根目錄；判斷不出來回傳 None。

    以套件檔案位置往上找，而不是以 cwd 判斷：cwd 可能是任何地方（CI 的 workspace、
    測試的暫存目錄），但套件原始碼一定在專案 repo 裡。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return None


def git_toplevel(path: Path) -> Path | None:
    """`git -C <path> rev-parse --show-toplevel`；不是 git repo 或失敗回傳 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if out else None


def is_within(path: Path, parent: Path) -> bool:
    """`path` 是否在 `parent` 之內（含自己）。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def allowed_workdirs() -> list[Path]:
    """`AISTORAGE_ALLOWED_WORKDIR` 明確允許的工作目錄（多個以 `:` 分隔）。"""
    raw = os.environ.get(ALLOWED_WORKDIR_ENV, "").strip()
    if not raw:
        return []
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def assert_safe_workdir(
    workdir: Path | str,
    *,
    purpose: str,
    require_annex_remote: bool = False,
    require_temp: bool = False,
    temp_roots: tuple[Path, ...] = (),
) -> Path:
    """確認 `workdir` 可以拿來跑會改寫 repo 的 git 指令；不安全就 raise。

    Args:
        workdir: 準備在裡面跑 git-annex／filter-repo 的目錄。
        purpose: 用途說明（只出現在例外訊息裡，方便回報是哪裡呼叫的）。
        require_annex_remote: 要求該目錄是 git repo 且 origin 是 `annex::` 遠端。
        require_temp: 要求該目錄位於暫存區底下。
        temp_roots: 額外視為暫存區的根目錄（預設用 TMPDIR／tempfile.gettempdir()）。

    Returns:
        正規化（resolve）之後的工作目錄。

    Raises:
        UnsafeWorkdirError: 任何一條規則不符。
    """
    import tempfile

    target = Path(workdir).expanduser().resolve()

    for allowed in allowed_workdirs():
        if is_within(target, allowed):
            return target

    project = project_repo_toplevel()
    if project is not None and is_within(target, project):
        raise UnsafeWorkdirError(
            f"{purpose}：工作目錄 {target} 位於專案 repo（{project}）之內，"
            "絕對不可以（會在專案裡建立 git-annex 分支或改寫專案歷史）。"
            f"請改用暫存目錄；若確實需要，設定 {ALLOWED_WORKDIR_ENV} 明確指定。"
        )

    if require_annex_remote:
        toplevel = git_toplevel(target)
        if toplevel is None:
            raise UnsafeWorkdirError(f"{purpose}：{target} 不是 git repo")
        proc = subprocess.run(
            ["git", "-C", str(toplevel), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        url = proc.stdout.strip()
        if not url.startswith("annex::"):
            raise UnsafeWorkdirError(
                f"{purpose}：{toplevel} 的 remote.origin.url 不是 annex:: 遠端"
                f"（{url or '未設定'}），拒絕操作。"
            )

    if require_temp:
        roots = list(temp_roots) or [Path(tempfile.gettempdir()).resolve()]
        if not any(is_within(target, root) for root in roots):
            raise UnsafeWorkdirError(
                f"{purpose}：工作目錄 {target} 不在暫存區"
                f"（{[str(r) for r in roots]}）之內，拒絕操作。"
            )

    return target
