"""AiStorage git-annex bundle 重放與 ref 集合計算模組。

依據規格：docs/impl/group3-modules.md 第 3.1 節
- replay_refs: 在指定工作目錄中依序 unbundle，任何一個解不開拋出 MismatchError
- ref 以最後一個 bundle 宣告的集合為準（review-1.4f3 L2）
- 去掉 refs/namespaces/git-remote-annex/<uuid>/ 前綴
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from aistorage.errors import MismatchError

_NAMESPACE_PREFIX_PATTERN = re.compile(
    r"^refs/namespaces/git-remote-annex/[^/]+/(.*)$"
)


def replay_refs(bundle_paths_in_order: list[Path], *, workdir: Path) -> dict[str, str]:
    """在 workdir 中依序解開 git bundles 並計算最終之 ref 映射表。

    Args:
        bundle_paths_in_order: 依 GITMANIFEST 順序排列之 bundle 本機路徑清單
        workdir: 暫存用 git 工作目錄

    Returns:
        ref_name -> sha 字串字典（已剝除 namespace 前綴）

    Raises:
        MismatchError: bundle 不存在、unbundle 失敗或 list-heads 失敗
    """
    if not bundle_paths_in_order:
        raise MismatchError("bundle 清單不得為空")

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # 確保 workdir 為合法之 git repo
    git_dir = workdir if (workdir / "HEAD").is_file() else (workdir / ".git")
    if not git_dir.exists():
        proc = subprocess.run(
            ["git", "-C", str(workdir), "init", "--bare"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise MismatchError(f"初始化 git repo 失敗: {proc.stderr}")

    for b_path in bundle_paths_in_order:
        p = Path(b_path).resolve()
        if not p.is_file():
            raise MismatchError(f"Bundle 檔案不存在: {p}")

        proc = subprocess.run(
            ["git", "-C", str(workdir), "bundle", "unbundle", str(p)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise MismatchError(f"解開 bundle 失敗 ({p.name}): {proc.stderr}")

    # 以最後一個 bundle 宣告的集合為準
    last_bundle = Path(bundle_paths_in_order[-1]).resolve()
    proc = subprocess.run(
        ["git", "bundle", "list-heads", str(last_bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MismatchError(
            f"讀取 bundle list-heads 失敗 ({last_bundle.name}): {proc.stderr}"
        )

    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        sha, ref_name = parts[0], parts[1]
        m = _NAMESPACE_PREFIX_PATTERN.match(ref_name)
        clean_ref = m.group(1) if m else ref_name
        refs[clean_ref] = sha

    return refs
