"""AiStorage git-annex bundle 重放與 ref 集合計算模組。

依據規格：
- docs/impl/group3-modules.md 第 3.1 節
- review-g3a.md H2（每次在完全獨立乾淨的暫存 repo 重放）、M4（本機雜湊核對、cat-file commit 完整性驗證、normalize_refs）
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Union

from aistorage.annex.git import get_git_env
from aistorage.annex.manifest import (
    BundleName,
    normalize_bundle_heads,
    parse_bundle_name,
)
from aistorage.errors import MismatchError


def replay_refs(
    bundle_paths_in_order: list[Union[Path, tuple[BundleName, Path]]],
    *,
    workdir: Path,
    repo_uuid: str,
) -> dict[str, str]:
    """在乾淨獨立的暫存 repo 中依序解開 git bundles 並計算最終之 ref 映射表。

    安全性與完整性保證（H2, M4, N1 & N7）：
    - 每次呼叫一律建立全新的暫存 bare repo，結束後立即銷毀，嚴防跨次重放殘留物件。
    - 重放前逐一分塊計算本機 bundle 檔案之 SHA-256，與檔名宣告之雜湊核對。
    - 嚴格比對 bundle 檔名中的 repo_uuid 與必填之 repo_uuid。
    - 以最後一個 bundle 宣告之 heads 為準，並透過 git cat-file 驗證所有 commit 物件均真實存在於重放後的物件庫中。
    - 透過 normalize_bundle_heads 進行嚴格的 namespace 與 ref 正規化。
    - 所有 git 子程序執行均套用隔離環境變數與逾時機制。

    Args:
        bundle_paths_in_order: 依 GITMANIFEST 順序排列之 bundle 清單（Path 或 (BundleName, Path)）
        workdir: 用於建立隔離暫存目錄的父目錄
        repo_uuid: 預期的 git-annex remote UUID（必填）

    Returns:
        clean_ref -> sha 字典（已剝除 namespace 前綴，排除 HEAD 與 peeled ref）

    Raises:
        MismatchError: bundle 不存在、雜湊不符、unbundle 失敗、commit 缺失或 ref 格式不符
    """
    if not bundle_paths_in_order:
        raise MismatchError("bundle 清單不得為空")

    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    git_env = get_git_env()

    # 1. M4, N1 & N7: 本機分塊 SHA-256 雜湊與 repo_uuid 預檢
    normalized_bundles: list[tuple[BundleName, Path]] = []
    for item in bundle_paths_in_order:
        if isinstance(item, tuple):
            b_name, b_path = item
        else:
            b_path = Path(item).resolve()
            parsed = parse_bundle_name(b_path.name)
            if parsed is None:
                raise MismatchError(f"無法解析 bundle 檔名格式: '{b_path.name}'")
            b_name = parsed

        p = Path(b_path).resolve()
        if not p.is_file():
            raise MismatchError(f"Bundle 檔案不存在: {p}")

        if b_name.repo_uuid != repo_uuid:
            raise MismatchError(
                f"Bundle '{b_name.name}' 之 repo_uuid ({b_name.repo_uuid}) 不符預期 ({repo_uuid})"
            )

        # N7: 分塊計算本機 SHA-256（避免整檔讀入記憶體）
        hasher = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        actual_sha = hasher.hexdigest().lower()
        if actual_sha != b_name.sha256.lower():
            raise MismatchError(
                f"Bundle '{b_name.name}' 本機 SHA-256 雜湊不符: 宣告為 {b_name.sha256}，實際為 {actual_sha}"
            )

        normalized_bundles.append((b_name, p))

    # 2. H2 & N7: 建立全新、隔離的暫存 bare repo，絕不重用舊目錄
    temp_repo_dir = tempfile.mkdtemp(dir=workdir, prefix="replay_isolated_")
    try:
        try:
            proc = subprocess.run(
                ["git", "-C", temp_repo_dir, "init", "--bare"],
                capture_output=True,
                text=True,
                env=git_env,
                timeout=60.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise MismatchError("初始化暫存 git repo 逾時") from None

        if proc.returncode != 0:
            raise MismatchError(f"初始化暫存 git repo 失敗: {proc.stderr}")

        # 3. 依序 unbundle
        for b_name, b_path in normalized_bundles:
            try:
                proc = subprocess.run(
                    ["git", "-C", temp_repo_dir, "bundle", "unbundle", str(b_path)],
                    capture_output=True,
                    text=True,
                    env=git_env,
                    timeout=60.0,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise MismatchError(f"解開 bundle 逾時 ({b_name.name})") from None

            if proc.returncode != 0:
                raise MismatchError(f"解開 bundle 失敗 ({b_name.name}): {proc.stderr}")

        # 4. 以最後一個 bundle 宣告的集合為準
        last_b_name, last_b_path = normalized_bundles[-1]
        try:
            proc = subprocess.run(
                ["git", "bundle", "list-heads", str(last_b_path)],
                capture_output=True,
                text=True,
                env=git_env,
                timeout=60.0,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise MismatchError(f"讀取 bundle list-heads 逾時 ({last_b_name.name})") from None

        if proc.returncode != 0:
            raise MismatchError(
                f"讀取 bundle list-heads 失敗 ({last_b_name.name}): {proc.stderr}"
            )

        raw_heads: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                sha, ref_name = parts[0], parts[1]
                raw_heads[ref_name] = sha

        # 5. M4: 驗證 list-heads 宣告之 commit 物件真實存在於物件庫中
        for ref_name, sha in raw_heads.items():
            if ref_name == "HEAD" or ref_name.endswith("^{}"):
                continue
            try:
                check_proc = subprocess.run(
                    ["git", "-C", temp_repo_dir, "cat-file", "-e", f"{sha}^{{commit}}"],
                    capture_output=True,
                    env=git_env,
                    timeout=60.0,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise MismatchError(f"驗證 commit 物件存在性逾時: {sha}") from None

            if check_proc.returncode != 0:
                raise MismatchError(
                    f"Bundle '{last_b_name.name}' 宣告之 ref '{ref_name}' 目標 commit '{sha}' 不存在於重放後物件庫中"
                )

        # 6. M4 & N1: 正規化 bundle ref 集合
        return normalize_bundle_heads(raw_heads, repo_uuid=repo_uuid)

    finally:
        # H2: 銷毀暫存 repo 目錄
        shutil.rmtree(temp_repo_dir, ignore_errors=True)
