"""讀取介面：讀取視圖取檔、驗證與快取（tasks 4.3）。

只讀：只使用 DriveClient 的讀取方法（get、download、download_bytes、
find_by_name 不在此用）。SA 本身也沒有寫入能力（1.5）。

manifest 的解析與驗證走 readview/model.py（4.1）——讀取視圖的格式只有一份
規則（schemas/readview-manifest.schema.json），這裡回傳的仍是字典，
介面不變。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from aistorage.drive.model import DriveClient
from aistorage.clock import Clock
from aistorage.errors import AiStorageError, MismatchError, NotFound, ReadError
from aistorage.reading import validate_reading
from aistorage.reader.config import ReaderConfig
from aistorage.readview.model import READVIEW_FORMAT as MANIFEST_FORMAT
from aistorage.readview.model import parse_manifest
from aistorage.search.query import RawRef, ReadingRef

MANIFEST_MAX_BYTES = 4 << 20
INDEX_MAX_BYTES = 256 << 20
READING_MAX_BYTES = 128 << 20
#: 原始紀錄本體的上限。與 inbox 對原始紀錄的 100 MiB 上限一致（DEFAULT_MAX_RAW_SIZE），
#: 讀取端不因為自己是大檔就放寬。
RAW_MAX_BYTES = 128 << 20


class AccessDenied(AiStorageError):
    """沒有 Agora 讀取權（manifest 回 403 或 404；後者可能是 Drive 的權限遮蔽）。"""


class StaleManifest(AiStorageError):
    """讀到的 manifest 世代比本地快取還小（疑似舊世代回放）。"""


def _is_access_denied(error: Exception) -> bool:
    if isinstance(error, NotFound):
        return True
    if isinstance(error, ReadError) and not isinstance(error, NotFound):
        msg = str(error)
        return "HTTP 403" in msg or "HTTP 401" in msg
    return False


def _parse_manifest(data: bytes) -> dict[str, Any]:
    """以 readview/model.py（4.1）驗證 manifest，回傳同樣的字典形狀。

    讀取介面本來就只認 schema 驗證過的 manifest；這裡走同一份規則
    （schemas/readview-manifest.schema.json），避免兩邊各驗一套。

    **generation 0（`index=None`）是合法的初始狀態**，不是錯誤：管理者用
    `initial_manifest` 建立的空 manifest 就是這樣（`Manifest.is_initial`）。
    它代表「還沒有任何東西被發佈過」，所以讀取端視為**空的**讀取視圖：
    catalog 回空、find 回零筆、讀指定 Session 回 KeyError（不存在）。

    這一點是 9.1 e2e 才發現的：原本這裡丟 MismatchError，導致
    `aistorage_split` 在「等待可見」的迴圈裡第一輪就爆掉——
    而那時候提交流程根本還沒跑、讀取視圖理應是空的。
    """
    manifest = parse_manifest(data)
    out = manifest.to_dict()
    if out.get("index") is None:
        # 初始世代：視為空讀取視圖（保留 is_initial 供上層判斷）
        out = {**out, "index": {}, "is_initial": True}
    return out


def _build_empty_index(
    dest: Path, *, generation: int, agora_main_sha: str, built_at: str
) -> None:
    """刻一份空的讀取視圖索引（初始世代用；schema 與出版端共用同一份）。"""
    from aistorage.search.index import IndexMeta, build_index, ensure_sqlite_version

    ensure_sqlite_version()
    build_index(
        dest,
        entries=[], links=[], handoffs=[], rejections=[],
        meta=IndexMeta(generation=generation, built_at=built_at,
                       agora_main_sha=agora_main_sha),
    )


class ReadViewClient:
    """讀取視圖用戶端（SA 讀者身分）。"""

    def __init__(self, drive: DriveClient, cfg: ReaderConfig, *, clock: Clock) -> None:
        self._drive = drive
        self._cfg = cfg
        self._clock = clock
        self._dir = cfg.cache_dir / cfg.manifest_file_id
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._dir, 0o700)
            os.chmod(cfg.cache_dir, 0o700)
        except OSError:
            pass

    def manifest(self) -> dict[str, Any]:
        """每次呼叫都重新下載（很小）；世代倒退 → StaleManifest。"""
        try:
            data = self._drive.download_bytes(
                self._cfg.manifest_file_id, max_bytes=MANIFEST_MAX_BYTES)
        except Exception as e:
            if _is_access_denied(e):
                raise AccessDenied(
                    f"讀取 manifest 被拒（無 Agora 讀取權）: {e}") from None
            raise
        manifest = _parse_manifest(data)
        cached = self._cached_generations()
        if cached and manifest["generation"] < max(cached):
            raise StaleManifest(
                f"manifest 世代倒退：遠端 {manifest['generation']} < 快取 {max(cached)}")
        return manifest

    def _cached_generations(self) -> list[int]:
        gens: list[int] = []
        for child in self._dir.glob("index-g*.sqlite"):
            try:
                gens.append(int(child.name[len("index-g"):-len(".sqlite")]))
            except ValueError:
                continue
        return gens

    def _verify_file(self, path: Path, *, sha256: str, size: int) -> None:
        digest = hashlib.sha256()
        total = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
                total += len(chunk)
        if total != size:
            raise MismatchError(f"檔案大小不符: {path.name}（預期 {size}，實際 {total}）")
        if digest.hexdigest().lower() != sha256.lower():
            raise MismatchError(f"檔案雜湊不符: {path.name}")

    def index(self) -> sqlite3.Connection:
        """依 generation 快取的唯讀連線；第二次呼叫不重新下載。"""
        return sqlite3.connect(f"file:{self.index_path()}?mode=ro", uri=True)

    def index_path(self) -> Path:
        """目前世代的索引在本機快取的路徑（必要時先下載並驗證）。

        4.5 重建驗證要拿整個索引檔做逐表比對（dump_tables 需要路徑），所以
        這個 accessor 是公開的；回傳的是唯讀快取檔，不得就地修改。
        """
        manifest = self.manifest()
        generation = manifest["generation"]
        ref = manifest["index"]
        dest = self._dir / f"index-g{generation}.sqlite"
        if not dest.is_file():
            tmp = self._dir / f"index-g{generation}.sqlite.tmp"
            if ref:
                self._drive.download(ref["id"], tmp, max_bytes=INDEX_MAX_BYTES)
                self._verify_file(tmp, sha256=ref["sha256"], size=int(ref["size"]))
            else:
                # 初始世代（index 為 null）：本機刻一份**空的**索引，
                # 讓「還沒有任何東西被發佈過」可以用同一條查詢路徑回答。
                # 用 search.index.build_index 產生，schema 不會與出版端漂移。
                _build_empty_index(
                    tmp,
                    generation=generation,
                    agora_main_sha=manifest.get("agora_main_sha") or "unborn",
                    built_at=manifest.get("published_at") or "1970-01-01T00:00:00Z",
                )
            os.replace(tmp, dest)
        return dest

    def reading(self, ref: ReadingRef) -> dict:
        """依 sha256 快取的閱讀版；下載後驗證 sha256、size，並跑 validate_reading。"""
        dest = self._dir / f"reading-{ref.sha256}.json"
        if not dest.is_file():
            tmp = self._dir / f"reading-{ref.sha256}.json.tmp"
            self._drive.download(ref.file_id, tmp, max_bytes=READING_MAX_BYTES)
            self._verify_file(tmp, sha256=ref.sha256, size=int(ref.size))
            os.replace(tmp, dest)
        try:
            data = json.loads(dest.read_text(encoding="utf-8"))
        except ValueError as e:
            raise MismatchError(f"reading 檔不是合法 JSON: {ref.file_id}: {e}") from None
        errors = validate_reading(data)
        if errors:
            detail = "; ".join(f"{e.field}: {e.message}" for e in errors[:5])
            raise MismatchError(f"reading 檔未通過格式驗證: {ref.file_id}: {detail}")
        return data

    def raw(self, ref: RawRef) -> bytes:
        """取某個快照的**原始紀錄本體**（`agora checkout` 的起點包要用）。

        依 `snapshot_sha256` 快取（內容定址，所以不同 Session 的同名快照也安全）。
        下載後驗 sha256 與 size：`snapshot_sha256` 本身就是內容雜湊，所以這一步
        同時保證「檔案沒被動過」與「這份確實是那個快照」——`agora checkout` 靠它
        承諾「原始紀錄原封不動」（ADR 0010 的 KV cache 要求）。
        """
        sha = ref.snapshot_sha256.lower()
        dest = self._dir / f"raw-{sha}"
        if not dest.is_file():
            tmp = self._dir / f"raw-{sha}.tmp"
            self._drive.download(ref.file_id, tmp, max_bytes=RAW_MAX_BYTES)
            self._verify_file(tmp, sha256=sha, size=int(ref.size))
            os.replace(tmp, dest)
        return dest.read_bytes()
