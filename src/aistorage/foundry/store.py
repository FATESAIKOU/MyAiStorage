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
from aistorage.annex.git import AnnexGit
from aistorage.errors import MismatchError
from aistorage.foundry import layout


class FoundryStore:
    """Foundry 真本工作樹存取介面。"""

    def __init__(
        self,
        worktree: Path | str,
        *,
        git: AnnexGit | None = None,
        temp_dir: Path | str | None = None,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        self.git = git
        if temp_dir is not None:
            self._temp_dir = Path(temp_dir).resolve()
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="aistorage_foundry_temp_"))
        self._changed_paths: list[str] = []

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

        回傳產生之 annex key（例如 SHA256E-s<size>--<sha256><ext>）。
        """
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"找不到產出本體來源檔案: {src}")

        raw_bytes = src.read_bytes()
        size = len(raw_bytes)
        sha256 = hashlib.sha256(raw_bytes).hexdigest().lower()
        ext = src.suffix or Path(filename).suffix
        if not ext:
            ext = ".bin"
        annex_key = f"SHA256E-s{size}--{sha256}{ext}"

        relpath = layout.object_path(ulid, filename)
        dest_p = self.worktree / relpath
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_p)

        if self.git is not None:
            self.git.add([relpath])
            if isinstance(self.git, FakeAnnexGit):
                self.git.local_keys = frozenset(set(self.git.local_keys) | {annex_key})

        self._changed_paths.append(relpath)
        return annex_key
