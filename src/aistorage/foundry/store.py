"""Foundry 工作樹儲存與操作介面（FoundryStore）。

依據規格：
- docs/impl/group5-7-modules.md 第 6.2 節 (7.2)
- 佈局：
  catalog/<ULID>.json               # 產出目錄（每件一筆）：metadata＋body＋{object_key?, size?, sha256?}
  objects/<ULID>/<安全化的檔名>     # contained：以 git annex add 存成 annex 物件
  _committer/…                      # 清冊、拒收
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from aistorage.annex.fake import FakeAnnexGit
from aistorage.annex.git import AnnexGit, get_git_env
from aistorage.errors import MismatchError, WriteError
from aistorage.foundry import layout
import subprocess

#: Foundry 的 annex.largefiles 規則（review F-H2）。
#: 收容產出（PDF、圖片等）要進 annex，catalog 的 JSON 留在 git。
#: 實測（git-annex 10.20260901）：
#:   include=objects/*/* → objects/<ULID>/<檔名> 有 key、catalog/*.json 沒有
#:   anything             → 連 catalog 也進 annex（bundle 會變大，不理想）
LARGEFILES_OBJECTS = "include=objects/*/*"


class FoundryStore:
    """Foundry 真本工作樹存取介面。"""

    def __init__(
        self,
        worktree: Path | str,
        *,
        git: AnnexGit | None = None,
        temp_dir: Path | str | None = None,
        largefiles: str | None = None,
        configure_annex: bool = True,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        self.git = git
        # F-H2：largefiles 必須涵蓋 objects/**，否則收容產出会變成 git blob
        # （100 MB 的產出直接進 bundle，正是 annex 要避免的）
        self.largefiles = largefiles if largefiles is not None else LARGEFILES_OBJECTS
        if temp_dir is not None:
            self._temp_dir = Path(temp_dir).resolve()
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="aistorage_foundry_temp_"))
        self._changed_paths: list[str] = []
        self._keys: set[str] = set()
        # `configure_annex=False`：給單元測試用假 AnnexGit 的情境（沒有真的 git
        # repo 就不可能設定 annex.largefiles；提交流程在測試時會注入
        # raw_storage_factory，那時也代表 git 是假的）。
        if configure_annex:
            self._ensure_largefiles_config()

    def _ensure_largefiles_config(self) -> None:
        """設定 annex.largefiles（F-H2）。

        對「已在 index 裡的檔案」無效，所以必須在建構時就設好（早於任何
        put_contained_object）；失敗要 raise，因為後面 lookupkey 會全部查不到。
        """
        if not (self.worktree / ".git").exists():
            return
        proc = subprocess.run(
            ["git", "-C", str(self.worktree), "config", "annex.largefiles", self.largefiles],
            capture_output=True, check=False, env=get_git_env(), timeout=30.0,
        )
        if proc.returncode != 0:
            raise WriteError(
                f"設定 annex.largefiles 失敗 (rc={proc.returncode}): {self.largefiles}")

    def keys(self) -> set[str]:
        """本工作樹已知的 annex key（由 put_contained_object 實際向 git-annex 取得）。"""
        return set(self._keys)

    def _lookup_key(self, relpath: str, dest_p: Path) -> str | None:
        """F-H2：key 一律用 `git annex lookupkey`（相對於 repo 根），不自己算。"""
        if self.git is not None:
            getter = getattr(self.git, "lookupkey", None)
            if callable(getter):
                return getter(relpath)
        proc = subprocess.run(
            ["git", "-C", str(self.worktree), "annex", "lookupkey", relpath],
            capture_output=True, text=True, check=False, env=get_git_env(), timeout=60.0,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def put_json(self, relpath: str, obj: Any) -> str:
        """寫入 JSON 檔案並加入 git 暫存。"""
        p = self.worktree / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        p.write_text(content, encoding="utf-8")
        if self.git is not None:
            self.git.add([relpath])
        self._changed_paths.append(relpath)
        return relpath

    def put_catalog(self, ulid: str, data: dict[str, Any]) -> str:
        """寫入產出目錄紀錄 catalog/<ULID>.json。"""
        relpath = layout.catalog_path(ulid)
        return self.put_json(relpath, data)

    def annex_keys(self) -> frozenset[str]:
        """真本中所有收容產出的 annex key 集合（與 AgoraStore.annex_keys 同介面）。"""
        return frozenset(self._keys)

    def changed_paths(self) -> list[str]:
        """取得此次所有新增或修改的相對路徑（與 AgoraStore 同介面）。"""
        return list(self._changed_paths)

    def get_catalog(self, ulid: str) -> dict[str, Any] | None:
        """讀取特定產出紀錄 catalog/<ULID>.json。"""
        try:
            relpath = layout.catalog_path(ulid)
        except ValueError:
            return None

        p = self.worktree / relpath
        if not p.is_file():
            return None

        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise MismatchError(f"Foundry catalog JSON 損毀: {relpath}") from e

    def list_catalog(self) -> list[dict[str, Any]]:
        """列舉真本內所有產出項目（依 ULID 排序）。"""
        catalog_dir = self.worktree / "catalog"
        if not catalog_dir.is_dir():
            return []

        entries: list[dict[str, Any]] = []
        for p in sorted(catalog_dir.glob("*.json")):
            if p.is_file():
                try:
                    with open(p, encoding="utf-8") as f:
                        entries.append(json.load(f))
                except json.JSONDecodeError as e:
                    raise MismatchError(f"Foundry catalog JSON 損毀: {p.name}") from e
        return entries

    def put_contained_object(self, ulid: str, filename: str, src_path: Path) -> str:
        """將實體檔案寫入 objects/<ULID>/<安全檔名> 並加入 git-annex 物件庫。

        回傳**實際由 git-annex 產生**的 key（F-H2：自己算的 key 幾乎一定不對——
        副檔名取自工作樹檔名、沒有副檔名就沒有副檔名、還受 annex.maxextensionlength
        影響）。檔案沒有進 annex 就 raise，不讓它留在 git blob 裡冒充。
        """
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"找不到產出本體來源檔案: {src}")

        relpath = layout.object_path(ulid, filename)
        dest_p = self.worktree / relpath
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_p)

        if self.git is not None:
            self.git.add([relpath])
        elif (self.worktree / ".git").exists():
            proc = subprocess.run(
                ["git", "-C", str(self.worktree), "add", str(dest_p)],
                capture_output=True, check=False, env=get_git_env(), timeout=60.0)
            if proc.returncode != 0:
                raise WriteError(f"git add 失敗 (rc={proc.returncode}): {relpath}")

        annex_key = self._lookup_key(relpath, dest_p)
        if not annex_key:
            raise WriteError(
                f"收容產出沒有進 git-annex（annex.largefiles={self.largefiles!r} "
                f"涵蓋不到 {relpath}）：拒絕把它當成 annex 物件。"
                "內容會留在 git blob 裡，bundle 會滾雪球。")
        self._keys.add(annex_key)
        if isinstance(self.git, FakeAnnexGit):
            self.git.local_keys = frozenset(set(self.git.local_keys) | {annex_key})

        self._changed_paths.append(relpath)
        return annex_key
