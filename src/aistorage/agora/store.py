"""Agora 真本資料存取與寫入管理模組。

依據規格：
- docs/impl/group3-modules.md 第 6.2 節
- review-g3b.md H3（快照 commit 可達性保證、hash-object 嚴格失敗、取出與存入雜湊比對）
- review-g3b.md M6（暫存目錄置於工作樹外部）、M7（RawRef、SnapshotEntry 欄位）、M8（meta 驗證）
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Literal, Protocol, runtime_checkable

from aistorage.agora import layout
from aistorage.annex.git import AnnexGit, get_git_env
from aistorage.errors import MismatchError, WriteError
from aistorage.schema import validate_record_metadata


@dataclass(frozen=True)
class RawRef:
    """原始紀錄之底層儲存引用標識。"""

    kind: Literal["git", "annex"]
    ref: str


@dataclass(frozen=True)
class SnapshotEntry:
    """快照歷史項目（對應 sessions/<source>/<id>/snapshots.jsonl 中的每一行）。"""

    snapshot_sha256: str
    snapshot_at: str
    raw_size: int
    item_key: str
    committed_at: str
    git_blob: str | None = None
    annex_key: str | None = None
    via: Literal["sync", "rewrite", "import"] = "sync"
    rewrite_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_at": self.snapshot_at,
            "raw_size": self.raw_size,
            "item_key": self.item_key,
            "committed_at": self.committed_at,
            "git_blob": self.git_blob,
            "annex_key": self.annex_key,
            "via": self.via,
        }
        if self.rewrite_id is not None:
            d["rewrite_id"] = self.rewrite_id
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
            via=data.get("via", "sync"),
            rewrite_id=data.get("rewrite_id"),
        )


@dataclass
class SessionRecord:
    """Session 真本 metadata（對應 sessions/<source>/<id>/meta.json）。"""

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
            in_progress=data.get("in_progress", False),
            archived_at=data.get("archived_at"),
            title=data.get("title"),
            extra=extra,
        )


@runtime_checkable
class RawStorage(Protocol):
    """原始紀錄 (raw) 儲存協定。"""

    def store(self, worktree_path: Path, src: Path) -> RawRef:
        """將 src 內容寫入指定之工作樹路徑，並回傳儲存引用標識 (RawRef)。"""
        ...

    def retrieve(self, ref: str, dest: Path) -> None:
        """依據引用標識取出原始紀錄寫入 dest。"""
        ...


class FakeRawStorage(RawStorage):
    """記憶體與本機檔案之測試用 Raw 儲存實作。"""

    def __init__(self, use_annex_key: bool = False) -> None:
        self.use_annex_key = use_annex_key
        self._blobs: dict[str, bytes] = {}

    def store(self, worktree_path: Path, src: Path) -> RawRef:
        data = Path(src).read_bytes()
        dest_p = Path(worktree_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        dest_p.write_bytes(data)

        sha = hashlib.sha256(data).hexdigest().lower()
        if self.use_annex_key:
            ref = f"SHA256E-s{len(data)}--{sha}"
            self._blobs[ref] = data
            return RawRef(kind="annex", ref=ref)
        else:
            ref = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
            self._blobs[ref] = data
            return RawRef(kind="git", ref=ref)

    def retrieve(self, ref: str, dest: Path) -> None:
        if ref not in self._blobs:
            raise KeyError(f"找不到 raw 物件: {ref}")
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        dest_p.write_bytes(self._blobs[ref])


class GitRawStorage(RawStorage):
    """使用 Git 物件庫之 Raw 儲存實作。"""

    def __init__(self, git_workdir: Path | str) -> None:
        self.git_workdir = Path(git_workdir).resolve()

    def store(self, worktree_path: Path, src: Path) -> RawRef:
        data = Path(src).read_bytes()
        dest_p = Path(worktree_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        dest_p.write_bytes(data)

        # H3-b & R12: 透過 git hash-object -w 寫入物件庫；隔離環境並設定逾時
        proc = subprocess.run(
            ["git", "-C", str(self.git_workdir), "hash-object", "-w", str(Path(src).resolve())],
            capture_output=True,
            text=True,
            check=False,
            env=get_git_env(),
            timeout=60.0,
        )
        if proc.returncode != 0:
            raise WriteError(f"git hash-object -w 失敗 (rc={proc.returncode}): {proc.stderr}")

        sha = proc.stdout.strip()
        return RawRef(kind="git", ref=sha)

    def retrieve(self, ref: str, dest: Path) -> None:
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        # R12: 隔離環境並設定逾時
        proc = subprocess.run(
            ["git", "-C", str(self.git_workdir), "cat-file", "-p", ref],
            capture_output=True,
            check=False,
            env=get_git_env(),
            timeout=60.0,
        )
        if proc.returncode != 0:
            stderr_msg = (
                proc.stderr.decode("utf-8", errors="replace")
                if isinstance(proc.stderr, bytes)
                else str(proc.stderr)
            )
            raise KeyError(f"無法自 git 物件庫取出 raw ({ref}): {stderr_msg}")
        dest_p.write_bytes(proc.stdout)


class AgoraStore:
    """Agora 真本資料存取與寫入管理。"""

    def __init__(
        self,
        worktree: Path | str,
        raw_storage: RawStorage,
        *,
        git: AnnexGit | None = None,
        temp_dir: Path | str | None = None,
    ) -> None:
        # R10: 當 raw_storage 為 GitRawStorage 時，必須提供 git 執行個體以維護 commit 快照可達性
        if isinstance(raw_storage, GitRawStorage) and git is None:
            raise ValueError("raw_storage 為 GitRawStorage 時，必須提供 git (AnnexGit) 執行個體以維護快照 commit 歷史")
        self.worktree = Path(worktree).resolve()
        self.raw_storage = raw_storage
        self.git = git
        # M6 & R12: 暫存目錄獨立於工作樹外部，且預設建立獨立臨時目錄避免本機衝突
        if temp_dir is not None:
            self._temp_dir = Path(temp_dir).resolve()
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="aistorage_store_"))
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

        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise MismatchError(f"真本項目 JSON 損毀: {relpath}") from None

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
        try:
            with open(p, encoding="utf-8") as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if line:
                        entries.append(SnapshotEntry.from_dict(json.loads(line)))
        except json.JSONDecodeError as e:
            raise MismatchError(f"快照歷史 snapshots.jsonl JSON 格式損毀: {relpath}") from None
        return entries

    def raw_path_for_snapshot(self, session_id: str, snapshot_sha256: str) -> Path:
        """取出特定快照版本的 raw 原始本體並存至暫存檔，回傳其 Path。

        H3-c: 取出後一律計算 SHA-256 並比對 snapshot_sha256，不符拋出 MismatchError。
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

        # M6: 暫存目錄在工作樹外部
        dest_path = self._temp_dir / f"{snapshot_sha256}.raw"

        # 若檔案已存在且雜湊吻合則直接回傳
        if dest_path.is_file():
            existing_sha = hashlib.sha256(dest_path.read_bytes()).hexdigest().lower()
            if existing_sha == snapshot_sha256.lower():
                return dest_path

        self.raw_storage.retrieve(ref, dest_path)

        # H3-c: 取出後雜湊驗證
        retrieved_bytes = dest_path.read_bytes()
        actual_sha = hashlib.sha256(retrieved_bytes).hexdigest().lower()
        if actual_sha != snapshot_sha256.lower():
            dest_path.unlink(missing_ok=True)
            raise MismatchError(
                f"自 RawStorage 取出之 raw 雜湊 ({actual_sha}) 與預期之 snapshot_sha256 ({snapshot_sha256}) 不符"
            )

        return dest_path

    def put_session(
        self,
        rec: SessionRecord,
        raw_src: Path,
        *,
        via: str = "sync",
        rewrite_id: str | None = None,
    ) -> None:
        """寫入或更新 Session 項目（存入 raw、追加 snapshots.jsonl、寫入 meta.json）。

        H3-a & PM 決定 3: 若配置了 git，寫入每份快照後立即 commit checkpoint，保證物件在 git 歷史中可達。
        H3-c: 驗證 raw_src 的 SHA-256 與大小必須完全吻合 rec。
        M8: 寫入前執行 validate_record_metadata 檢查。

        Args:
            rec: Session 真本 metadata。
            raw_src: 原始紀錄本體檔案路徑。
            via: 快照來源（"sync"、"rewrite"、"import"），記入 snapshots.jsonl。
            rewrite_id: 經由改寫提案寫入時，該 rewrite 項目的 id（via="rewrite" 時必填）。
        """
        raw_p = Path(raw_src)
        if not raw_p.is_file():
            raise FileNotFoundError(f"找不到原始紀錄檔案: {raw_p}")

        if via not in ("sync", "rewrite", "import", "rollback"):
            raise ValueError(f"無效之快照來源 via: {repr(via)}")
        if via == "rewrite" and not rewrite_id:
            raise ValueError("via='rewrite' 時必須提供 rewrite_id")

        # H3-c: 嚴格驗證 raw_src 雜湊與大小
        raw_bytes = raw_p.read_bytes()
        actual_raw_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
        actual_raw_size = len(raw_bytes)

        if actual_raw_sha != rec.raw_sha256.lower():
            raise MismatchError(
                f"raw_src 之 SHA-256 ({actual_raw_sha}) 與 SessionRecord.raw_sha256 ({rec.raw_sha256}) 不符"
            )
        if actual_raw_size != rec.raw_size:
            raise MismatchError(
                f"raw_src 之大小 ({actual_raw_size}) 與 SessionRecord.raw_size ({rec.raw_size}) 不符"
            )

        # M8: 驗證真本 metadata 符合規範
        meta_dict = rec.to_dict()
        val_errors = validate_record_metadata(meta_dict)
        if val_errors:
            err_msg = "; ".join(f"{e.field}: {e.message}" for e in val_errors)
            raise ValueError(f"SessionRecord 未通過 record metadata 驗證: {err_msg}")

        session_id = rec.id
        raw_relpath = layout.session_raw_path(session_id)
        raw_worktree_path = self.worktree / raw_relpath

        # 1. 存入 raw
        raw_ref = self.raw_storage.store(raw_worktree_path, raw_p)
        self._record_changed(raw_relpath)

        # 2. 追加快照歷史（M7: 避免重複追加同一快照）
        existing_snaps = self.snapshots(session_id)
        if not (existing_snaps and existing_snaps[-1].snapshot_sha256 == rec.raw_sha256):
            snap_entry = SnapshotEntry(
                snapshot_sha256=rec.raw_sha256,
                snapshot_at=rec.snapshot_at,
                raw_size=rec.raw_size,
                item_key=rec.last_item_key,
                committed_at=rec.committed_at,
                git_blob=raw_ref.ref if raw_ref.kind == "git" else None,
                annex_key=raw_ref.ref if raw_ref.kind == "annex" else None,
                via=via,  # type: ignore[arg-type]
                rewrite_id=rewrite_id,
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
        self.put_json(meta_relpath, meta_dict)

        # H3-a / PM 決定 3: 若配置了 git，對每一份收進的快照各 commit 一次
        if self.git is not None:
            self.commit_checkpoint(f"snapshot: {rec.last_item_key}")

    def commit_checkpoint(self, message: str) -> str | None:
        """提交當前變更為一個 commit，確保每份快照在 git 歷史中均有 commit 引用（H3-a）。"""
        if self.git is not None and self._changed_paths:
            self.git.add(self._changed_paths)
            sha = self.git.commit(message)
            self._changed_paths.clear()
            return sha
        return None

    def put_json(self, relpath: str, obj: dict[str, Any]) -> None:
        """以固定格式寫入 JSON 檔案（sort_keys=True, indent=2, 結尾換行）。"""
        target = self.worktree / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        target.write_text(content, encoding="utf-8")
        self._record_changed(relpath)

    def append_line(self, relpath: str, line: str) -> None:
        """追加單行字串至指定檔案（若檔案不存在則建立），並登記至變更清單。"""
        target = self.worktree / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if not line.endswith("\n"):
            line = line + "\n"
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)
        self._record_changed(relpath)

    def changed_paths(self) -> list[str]:
        """取得此次所有新增或修改的相對路徑清單。"""
        return list(self._changed_paths)

    def _record_changed(self, relpath: str) -> None:
        clean = relpath.lstrip("/")
        if clean not in self._changed_paths:
            self._changed_paths.append(clean)
