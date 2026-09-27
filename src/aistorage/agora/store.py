"""Agora 真本資料庫操作模組 (AgoraStore)。

依據規格：docs/impl/group3-modules.md 第 6 節
- SessionRecord / SnapshotEntry 資料模型
- RawStorage 協定與 FakeRawStorage / GitRawStorage 實作
- AgoraStore: 在本機 clone 的真本工作樹上讀寫 Session、快照歷史與各型態項目
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Protocol, runtime_checkable

from aistorage.agora import layout


@dataclass
class SessionRecord:
    """真本 Session 記錄（對應 meta.json）。"""

    id: str
    producer: str
    created_at: str
    updated_at: str
    status: str
    snapshot_at: str
    raw_sha256: str
    raw_size: int
    committed_at: str
    last_item_key: str
    type: str = "session"
    case_id: str | None = None
    provenance: str | None = None
    role: str | None = None
    role_version: str | None = None
    stopped_at: str | None = None
    parent_id: str | None = None
    in_progress: bool = False
    archived_at: str | None = None
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化為符合 meta.json 規範之字典。"""
        res: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "producer": self.producer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "case_id": self.case_id,
            "provenance": self.provenance,
            "role": self.role,
            "role_version": self.role_version,
            "status": self.status,
            "stopped_at": self.stopped_at,
            "snapshot_at": self.snapshot_at,
            "raw_sha256": self.raw_sha256,
            "raw_size": self.raw_size,
            "parent_id": self.parent_id,
            "in_progress": self.in_progress,
            "archived_at": self.archived_at,
            "committed_at": self.committed_at,
            "last_item_key": self.last_item_key,
            "title": self.title,
        }
        if self.extra:
            for k, v in self.extra.items():
                if k not in res:
                    res[k] = v
        return res

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord:
        """從字典反序列化為 SessionRecord。"""
        known_keys = {
            "id", "type", "producer", "created_at", "updated_at",
            "case_id", "provenance", "role", "role_version",
            "status", "stopped_at", "snapshot_at", "raw_sha256",
            "raw_size", "parent_id", "in_progress", "archived_at",
            "committed_at", "last_item_key", "title",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            id=data["id"],
            type=data.get("type", "session"),
            producer=data["producer"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            status=data["status"],
            snapshot_at=data["snapshot_at"],
            raw_sha256=data["raw_sha256"],
            raw_size=int(data["raw_size"]),
            committed_at=data["committed_at"],
            last_item_key=data["last_item_key"],
            case_id=data.get("case_id"),
            provenance=data.get("provenance"),
            role=data.get("role"),
            role_version=data.get("role_version"),
            stopped_at=data.get("stopped_at"),
            parent_id=data.get("parent_id"),
            in_progress=bool(data.get("in_progress", False)),
            archived_at=data.get("archived_at"),
            title=data.get("title"),
            extra=extra,
        )


@dataclass(frozen=True)
class SnapshotEntry:
    """snapshots.jsonl 中的單筆快照歷史項目。"""

    snapshot_sha256: str
    snapshot_at: str
    raw_size: int
    item_key: str
    committed_at: str
    git_blob: str | None = None
    annex_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_at": self.snapshot_at,
            "raw_size": self.raw_size,
            "item_key": self.item_key,
            "committed_at": self.committed_at,
        }
        if self.git_blob is not None:
            d["git_blob"] = self.git_blob
        if self.annex_key is not None:
            d["annex_key"] = self.annex_key
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnapshotEntry:
        return cls(
            snapshot_sha256=data["snapshot_sha256"],
            snapshot_at=data["snapshot_at"],
            raw_size=int(data["raw_size"]),
            item_key=data["item_key"],
            committed_at=data["committed_at"],
            git_blob=data.get("git_blob"),
            annex_key=data.get("annex_key"),
        )


@runtime_checkable
class RawStorage(Protocol):
    """原始紀錄 (raw) 儲存協定。"""

    def store(self, worktree_path: Path, src: Path) -> str:
        """將 src 內容寫入指定之工作樹路徑，並回傳引用標識 (git_blob 或 annex_key)。"""
        ...

    def retrieve(self, ref: str, dest: Path) -> None:
        """依據引用標識取出原始紀錄寫入 dest。"""
        ...


class FakeRawStorage(RawStorage):
    """記憶體與本機檔案之測試用 Raw 儲存實作。"""

    def __init__(self, use_annex_key: bool = False) -> None:
        self.use_annex_key = use_annex_key
        self._blobs: dict[str, bytes] = {}

    def store(self, worktree_path: Path, src: Path) -> str:
        data = Path(src).read_bytes()
        dest_p = Path(worktree_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        dest_p.write_bytes(data)

        sha = hashlib.sha256(data).hexdigest().lower()
        if self.use_annex_key:
            ref = f"SHA256E-s{len(data)}--{sha}"
        else:
            ref = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()

        self._blobs[ref] = data
        return ref

    def retrieve(self, ref: str, dest: Path) -> None:
        if ref not in self._blobs:
            raise KeyError(f"找不到 raw 物件: {ref}")
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        dest_p.write_bytes(self._blobs[ref])


class GitRawStorage(RawStorage):
    """使用 Git 物件庫之 Raw 儲存實作。"""

    def __init__(self, git_workdir: Path | str) -> None:
        self.git_workdir = Path(git_workdir)

    def store(self, worktree_path: Path, src: Path) -> str:
        data = Path(src).read_bytes()
        dest_p = Path(worktree_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        dest_p.write_bytes(data)

        # 透過 git hash-object -w 寫入物件庫
        proc = subprocess.run(
            ["git", "-C", str(self.git_workdir), "hash-object", "-w", str(src.resolve())],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            # 若無 git 倉庫環境，退回手動計算 SHA-1
            sha = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
            return sha
        return proc.stdout.strip()

    def retrieve(self, ref: str, dest: Path) -> None:
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(self.git_workdir), "cat-file", "-p", ref],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise KeyError(f"無法自 git 物件庫取出 raw ({ref}): {proc.stderr.decode('utf-8', errors='ignore')}")
        dest_p.write_bytes(proc.stdout)


class AgoraStore:
    """Agora 真本資料存取與寫入管理。"""

    def __init__(self, worktree: Path | str, raw_storage: RawStorage) -> None:
        self.worktree = Path(worktree).resolve()
        self.raw_storage = raw_storage
        self._changed_paths: list[str] = []

    def get_record(self, item_id: str) -> dict[str, Any] | None:
        """依據 item_id 取得真本項目的 JSON 字典。若項目不存在回傳 None。"""
        try:
            relpath = layout.record_path_for_id(item_id)
        except ValueError:
            return None

        p = self.worktree / relpath
        if not p.is_file():
            return None

        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def get_session(self, session_id: str) -> SessionRecord | None:
        """取得指定 Session 的 SessionRecord。若不存在回傳 None。"""
        data = self.get_record(session_id)
        if data is None:
            return None
        return SessionRecord.from_dict(data)

    def snapshots(self, session_id: str) -> list[SnapshotEntry]:
        """取得指定 Session 的所有快照歷史項目清單（依寫入順序排序）。"""
        relpath = layout.session_snapshots_path(session_id)
        p = self.worktree / relpath
        if not p.is_file():
            return []

        entries: list[SnapshotEntry] = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(SnapshotEntry.from_dict(json.loads(line)))
        return entries

    def raw_path_for_snapshot(self, session_id: str, snapshot_sha256: str) -> Path:
        """取出特定快照版本的 raw 原始本體並存至暫存檔，回傳其 Path。

        Raises:
            KeyError: 找不到對應的快照
        """
        all_snaps = self.snapshots(session_id)
        target: SnapshotEntry | None = None
        for s in all_snaps:
            if s.snapshot_sha256 == snapshot_sha256:
                target = s
                break

        if target is None:
            raise KeyError(
                f"在 Session '{session_id}' 中找不到雜湊為 '{snapshot_sha256}' 的快照"
            )

        ref = target.git_blob or target.annex_key
        if not ref:
            raise KeyError(
                f"快照 '{snapshot_sha256}' 未記錄 git_blob 或 annex_key"
            )

        dest_dir = self.worktree / "_committer" / "tmp" / "snapshots"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{snapshot_sha256}.raw"

        # 若檔案已存在且雜湊吻合則直接回傳
        if dest_path.is_file():
            existing_sha = hashlib.sha256(dest_path.read_bytes()).hexdigest().lower()
            if existing_sha == snapshot_sha256:
                return dest_path

        self.raw_storage.retrieve(ref, dest_path)
        return dest_path

    def put_session(self, rec: SessionRecord, raw_src: Path) -> None:
        """寫入或更新 Session 項目（存入 raw、追加 snapshots.jsonl、寫入 meta.json）。"""
        session_id = rec.id
        raw_relpath = layout.session_raw_path(session_id)
        raw_worktree_path = self.worktree / raw_relpath

        # 1. 存入 raw
        ref = self.raw_storage.store(raw_worktree_path, Path(raw_src))
        self._record_changed(raw_relpath)

        # 2. 追加快照歷史
        is_annex = ref.startswith("SHA256")
        snap_entry = SnapshotEntry(
            snapshot_sha256=rec.raw_sha256,
            snapshot_at=rec.snapshot_at,
            raw_size=rec.raw_size,
            item_key=rec.last_item_key,
            committed_at=rec.committed_at,
            git_blob=None if is_annex else ref,
            annex_key=ref if is_annex else None,
        )

        snapshots_relpath = layout.session_snapshots_path(session_id)
        snapshots_path = self.worktree / snapshots_relpath
        snapshots_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(snap_entry.to_dict(), ensure_ascii=False) + "\n"
        with open(snapshots_path, "a", encoding="utf-8") as f:
            f.write(line)
        self._record_changed(snapshots_relpath)

        # 3. 寫入 meta.json
        meta_relpath = layout.session_meta_path(session_id)
        self.put_json(meta_relpath, rec.to_dict())

    def put_json(self, relpath: str, obj: dict[str, Any]) -> None:
        """以固定格式寫入 JSON 檔案（sort_keys=True, indent=2, 結尾換行）。"""
        target = self.worktree / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        target.write_text(content, encoding="utf-8")
        self._record_changed(relpath)

    def changed_paths(self) -> list[str]:
        """取得此次所有新增或修改的相對路徑清單。"""
        return list(self._changed_paths)

    def _record_changed(self, relpath: str) -> None:
        clean = relpath.lstrip("/")
        if clean not in self._changed_paths:
            self._changed_paths.append(clean)
