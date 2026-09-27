"""讀取介面：讀取視圖取檔、驗證與快取（tasks 4.3）。

只讀：只使用 DriveClient 的讀取方法（get、download、download_bytes、
find_by_name 不在此用）。SA 本身也沒有寫入能力（1.5）。

manifest 解析目前是最小實作（格式、世代、index 定位）；完整
readview/model.py（4.1）落實後以它為準，介面不變。
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
from aistorage.search.query import ReadingRef

MANIFEST_FORMAT = "aistorage.readview/v1"
MANIFEST_MAX_BYTES = 4 << 20
INDEX_MAX_BYTES = 256 << 20
READING_MAX_BYTES = 128 << 20


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
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise MismatchError(f"manifest 不是合法 JSON: {e}") from None
    if not isinstance(manifest, dict):
        raise MismatchError("manifest 最外層必須是物件")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise MismatchError(f"manifest 格式不符: {manifest.get('format')!r}")
    generation = manifest.get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise MismatchError(f"manifest generation 非法: {generation!r}")
    index = manifest.get("index")
    if not isinstance(index, dict):
        raise MismatchError("manifest 缺少 index 定位")
    for key in ("id", "sha256", "size"):
        if index.get(key) is None:
            raise MismatchError(f"manifest index 缺少 {key}")
    if not isinstance(manifest.get("published_at"), str):
        raise MismatchError("manifest 缺少 published_at")
    return manifest


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
        manifest = self.manifest()
        generation = manifest["generation"]
        ref = manifest["index"]
        dest = self._dir / f"index-g{generation}.sqlite"
        if not dest.is_file():
            tmp = self._dir / f"index-g{generation}.sqlite.tmp"
            self._drive.download(ref["id"], tmp, max_bytes=INDEX_MAX_BYTES)
            self._verify_file(tmp, sha256=ref["sha256"], size=int(ref["size"]))
            os.replace(tmp, dest)
        return sqlite3.connect(f"file:{dest}?mode=ro", uri=True)

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
