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
import re
import shutil
import subprocess
import tempfile
from typing import Any, Literal, Protocol, runtime_checkable

from aistorage.agora import layout
from aistorage.annex.fake import FakeAnnexGit
from aistorage.annex.git import DEFAULT_LARGEFILES, AnnexGit, get_git_env
from aistorage.errors import MismatchError, ReadError, WriteError
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
    via: Literal["sync", "rewrite", "import", "rollback"] = "sync"
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


#: 原始紀錄的 annex.largefiles 規則。**單一來源是 `annex.git.DEFAULT_LARGEFILES`**
#: （`clone_for_commit` 在 clone 時就設好）；這裡只是同名別名 + 給舊呼叫端用。
#: 原始紀錄路徑是 `sessions/<source>/<id>/raw`（沒有副檔名），所以不能用
#: `include=*.json`；實測 `include=sessions/*/*/raw` 會讓 raw 進 annex、
#: `meta.json`／`snapshots.jsonl` 仍留在 git（只有小檔進 bundle）。
LARGEFILES_RAW = DEFAULT_LARGEFILES


class AnnexRawStorage(RawStorage):
    """使用 Git-Annex 物件庫存放原始紀錄之實作。

    依據 tasks 2.6 決策，以及 review-g7-e2e A-H1／A-H2／A-H3：

    - **A-H1（檔名與 largefiles 對不上）**：原始紀錄的路徑是
      `sessions/<source>/<id>/raw`（`agora/layout.py`，**沒有副檔名**），
      原本的 `annex.largefiles=include=*.json` 涵蓋不到它，raw 會被存成
      一般的 git blob（2.6 量到的 push 26 秒／bundle 0.4 MB 效果根本沒發生）。
      改成**路徑規則** `include=sessions/*/*/raw`（實測：raw 進 annex、
      `meta.json` 仍留在 git）。注意 largefiles 設定對**已在 index 裡**的檔案
      不生效，所以規則必須在 `store()` 之前設好（建構時就設）。
    - **A-H2（key 自己算錯）**：key 一律用 `git annex lookupkey <path>` 取得，
      這是唯一來源。對沒有副檔名的檔案，git-annex 產生的是
      `SHA256E-s<size>--<sha>`（**沒有副檔名**），自己補 `.json` 一定對不上。
      查不到（檔案沒進 annex）就 raise，不讓內容留在 git blob 裡冒充 annex。
    - **A-H3（全新 clone 取不回舊快照）**：`retrieve` 在本機找不到時，會用
      `git annex get --key=<k> --from <remote>` 從遠端 special remote 取回；
      取不到就 raise `ReadError`（不再 `except Exception: pass` 吞掉）。
    - 取出時嚴格比對 key 宣告的 SHA-256 與大小，不符拋出 MismatchError。
    """

    def __init__(
        self,
        git_workdir: Path | str,
        *,
        git: AnnexGit | None = None,
        largefiles: str | None = None,
        remote: str = "origin",
    ) -> None:
        self.git_workdir = Path(git_workdir).resolve()
        self.git = git
        # A-H1：預設是路徑規則，不是 include=*.json（那個涵蓋不到 `raw`）
        self.largefiles = largefiles if largefiles is not None else LARGEFILES_RAW
        self.remote = remote
        self._blobs: dict[str, bytes] = {}
        self._keys: set[str] = set()

        self._ensure_largefiles_config()

    def _ensure_largefiles_config(self) -> None:
        """只在**還沒設定**時補上 `annex.largefiles`（M2）。

        M2：`clone_for_commit` 已經把最終規則設好了，這裡再覆寫會讓結果取決於
        建構順序（管理腳本直接開的 clone、FakeAnnexGit 分支都
        沒有建構 `AnnexRawStorage`，規則就不對）。所以：

        - 已經有設定 → 什麼都不做（尊重呼叫端傳進來的規則）；
        - 沒有設定（自己 `git init` 出來的 repo）→ 補上預設規則，並在失敗時 raise
          （A-H1：設定沒生效時後面的 lookupkey 會全部查不到，錯誤會被誤判成
          「沒有進 annex」）。
        """
        git_dir = self.git_workdir / ".git"
        if not git_dir.exists():
            return

        current = subprocess.run(
            ["git", "-C", str(self.git_workdir), "config", "--get", "annex.largefiles"],
            capture_output=True, text=True, check=False, env=get_git_env(),
            timeout=30.0,
        )
        if current.returncode == 0 and current.stdout.strip():
            return
        proc = subprocess.run(
            ["git", "-C", str(self.git_workdir), "config", "annex.largefiles",
             self.largefiles],
            capture_output=True, check=False, env=get_git_env(), timeout=30.0,
        )
        if proc.returncode != 0:
            raise WriteError(
                f"設定 annex.largefiles 失敗 (rc={proc.returncode}): {self.largefiles}")

    def _lookup_key(self, path: Path) -> str | None:
        """annex key 的唯一來源：git annex lookupkey（SubprocessAnnexGit 或直接呼叫）。

        路徑要用**相對於 repo 根**的形式：`git annex lookupkey` 對絕對路徑
        找不到檔案（實測會靜靜回傳空字串），那樣會被誤判成「沒有進 annex」。
        """
        target = Path(path)
        try:
            rel: str | Path = target.resolve().relative_to(self.git_workdir)
        except ValueError:
            rel = target
        if self.git is not None:
            getter = getattr(self.git, "lookupkey", None)
            if callable(getter):
                return getter(rel)
        proc = subprocess.run(
            ["git", "-C", str(self.git_workdir), "annex", "lookupkey", str(rel)],
            capture_output=True, text=True, check=False, env=get_git_env(), timeout=60.0,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _assert_key_is_this_content(self, key: str, data: bytes, dest_p: Path) -> None:
        """確認 annex 物件 `key` 的內容就是剛寫進去的 `data`。

        key 的名稱是內容雜湊，所以兩者必須一致。只有真的 git-annex 環境才檢查
        （單元測試用的假 git 沒有真的物件庫）。
        """
        if self.git is None or isinstance(self.git, FakeAnnexGit):
            return
        if not (self.git_workdir / ".git").exists():
            return
        local = self._local_object_bytes(key)
        if local is None:
            raise WriteError(
                f"剛寫入的 {dest_p} 對應的 annex 物件 {key} 不在本機物件庫裡："
                "沒有真的入 annex，拒絕把它記成快照的 key。")
        if local != data:
            raise MismatchError(
                f"annex 物件 {key} 的內容與剛寫入的 raw 不符"
                f"（物件 {len(local)} bytes／寫入 {len(data)} bytes）："
                "這個 key 不能代表這一版，拒絕記下。"
                "（多半是 index 裡這一條還被當成已入 annex，git-annex 跳過了它）")

    def store(self, worktree_path: Path, src: Path) -> RawRef:
        """將 src 原始紀錄存為 git-annex 物件並回傳 RawRef（key 由 git-annex 決定）。"""
        src_p = Path(src).resolve()
        if not src_p.is_file():
            raise FileNotFoundError(f"找不到原始紀錄來源檔案: {src_p}")

        data = src_p.read_bytes()

        dest_p = Path(worktree_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        try:
            rel: str | Path = dest_p.relative_to(self.git_workdir)
        except ValueError:
            rel = dest_p
        # H1（review-cdb4a34，資料遺失）：**必須先把這條路徑從 index 拿掉**。
        # git-annex 的 largefiles clean filter 看到 index 裡這一條已經是指向
        # annex 物件的 symlink，就會判定「已經入過 annex」而整條跳過——於是
        # 同一個 Session 換新版本時：lookupkey 回上一版的 key、新內容從來沒有
        # 變成 annex 物件、樹狀也還指著舊物件。key 的名稱是舊內容的雜湊，
        # sweep／key 覆蓋率／隔離區全都抓不到，讀者只會讀到舊版本。
        if (self.git_workdir / ".git").exists():
            subprocess.run(
                ["git", "-C", str(self.git_workdir), "update-index",
                 "--force-remove", "--", str(rel)],
                capture_output=True, check=False, env=get_git_env(), timeout=60.0,
            )
        # dest 可能是 git-annex 的**符號連結**（core.symlinks=true 的環境）。
        # `shutil.copy2` 會沿著連結寫進去，而 git-annex 的物件是 hardlink——
        # 於是「上一個快照的 key」指向的物件被**就地改寫**成新內容：舊快照的
        # raw 再也讀不回來，連 key 的雜湊都不再對應內容。
        # git 追蹤的是路徑不是 inode，所以先移除目標路徑再寫新的。
        if dest_p.is_symlink() or dest_p.exists():
            dest_p.unlink()
        shutil.copy2(src_p, dest_p)

        if self.git is not None:
            self.git.add([str(rel)])
        elif (self.git_workdir / ".git").exists():
            proc = subprocess.run(
                ["git", "-C", str(self.git_workdir), "add", str(rel)],
                capture_output=True, check=False, env=get_git_env(), timeout=60.0,
            )
            if proc.returncode != 0:
                raise WriteError(f"git add 失敗 (rc={proc.returncode}): {dest_p}")

        # A-H2：key 必須問 git-annex；查不到就是「沒有進 annex」，要立刻 raise
        annex_key = self._lookup_key(dest_p)
        if not annex_key:
            raise WriteError(
                f"原始紀錄沒有進 git-annex（annex.largefiles={self.largefiles!r} "
                f"涵蓋不到 {dest_p}）：拒絕把它當成 annex 物件。"
                "內容會留在 git blob 裡，2.6 的量測效果不會發生。"
            )
        # H1（review-cdb4a34）：key 的名稱就是**內容雜湊**，所以拿到 key 之後
        # 一定要確認它指的就是剛剛寫進去的內容。少了這一步，上面那個
        # 「index 記成已入 annex → filter 跳過」的情況只會靜靜地記下上一版的
        # key，而這一版的內容從來沒有變成物件：資料遺失，而且 key 的雜湊與
        # 內容不一致，sweep 與 key 覆蓋率都看不出來。
        self._assert_key_is_this_content(annex_key, data, dest_p)

        self._blobs[annex_key] = data
        self._keys.add(annex_key)

        if isinstance(self.git, FakeAnnexGit):
            self.git.local_keys = frozenset(set(self.git.local_keys) | {annex_key})

        return RawRef(kind="annex", ref=annex_key)

    def retrieve(self, ref: str, dest: Path) -> None:
        """依據 annex key 取出原始紀錄寫入 dest，並驗證其 SHA-256 雜湊與大小。"""
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)

        data = self._read_raw_bytes(ref)
        self._verify_annex_key(ref, data)
        dest_p.write_bytes(data)

    def _local_object_bytes(self, ref: str) -> bytes | None:
        """本機（clone 內）已有的物件內容；沒有就回傳 None。"""
        if (self.git_workdir / ".git").exists():
            proc = subprocess.run(
                ["git", "-C", str(self.git_workdir), "annex", "contentlocation", ref],
                capture_output=True, text=True, check=False, env=get_git_env(),
                timeout=30.0,
            )
            if proc.returncode == 0:
                loc = proc.stdout.strip()
                if loc:
                    p = (self.git_workdir / loc).resolve()
                    if p.is_file():
                        return p.read_bytes()

        annex_objs = self.git_workdir / ".git" / "annex" / "objects"
        if annex_objs.is_dir():
            for base in (ref, f"{ref}.tmp"):
                matches = list(annex_objs.glob(f"**/{base}"))
                for m in matches:
                    if m.is_file():
                        return m.read_bytes()
        return None

    def _read_raw_bytes(self, ref: str) -> bytes:
        if ref in self._blobs:
            return self._blobs[ref]

        data = self._local_object_bytes(ref)
        if data is not None:
            return data

        # A-H3：本機沒有就從遠端 special remote 取回（全新 clone 的正常路徑）
        if self.git is not None and hasattr(self.git, "get_key"):
            self.git.get_key(ref, from_remote=self.remote)
            data = self._local_object_bytes(ref)
            if data is not None:
                self._blobs[ref] = data
                return data

        parsed = self.parse_annex_key(ref)
        if parsed is not None:
            _, exp_size, exp_sha = parsed
            # 工作樹上的 raw（路徑由 layout 決定，沒有副檔名）
            from aistorage.agora import layout as _layout

            for source in ("opencode", "claude-code"):
                for cand in self.git_workdir.glob(f"sessions/{source}/*/raw*"):
                    if cand.is_file() and cand.stat().st_size == exp_size:
                        b = cand.read_bytes()
                        if hashlib.sha256(b).hexdigest().lower() == exp_sha:
                            return b

        raise ReadError(
            f"無法取得 annex 物件 {ref}：本機沒有、遠端 special remote 也取不到"
            f"（remote={self.remote}）。提交流程每一輪都是全新 clone，"
            "取不到就代表遠端沒有這個物件或 special remote 設定有問題。")

    @staticmethod
    def parse_annex_key(ref: str) -> tuple[str, int, str] | None:
        """解析 annex key，回傳 (key, size, sha256) 或 None。"""
        m = re.match(r"^SHA256(?:E)?-s(\d+)--([0-9a-f]{64})(\..*)?$", ref)
        if not m:
            return None
        return ref, int(m.group(1)), m.group(2).lower()

    def _verify_annex_key(self, ref: str, data: bytes) -> None:
        parsed = self.parse_annex_key(ref)
        if parsed is None:
            return
        _, expected_size, expected_sha = parsed
        actual_size = len(data)
        actual_sha = hashlib.sha256(data).hexdigest().lower()

        if actual_size != expected_size:
            raise MismatchError(
                f"取出之 annex 物件大小 ({actual_size}) 與 key 宣告 ({expected_size}) 不符: {ref}"
            )
        if actual_sha != expected_sha:
            raise MismatchError(
                f"取出之 annex 物件 SHA-256 ({actual_sha}) 與 key 宣告 ({expected_sha}) 不符: {ref}"
            )

    def keys(self) -> frozenset[str]:
        """回傳目前已存入之 annex key 集合。"""
        return frozenset(self._keys)


class AgoraStore:
    """Agora 真本資料存取與寫入管理。"""

    def __init__(
        self,
        worktree: Path | str,
        raw_storage: RawStorage | None = None,
        *,
        git: AnnexGit | None = None,
        temp_dir: Path | str | None = None,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        if raw_storage is None:
            raw_storage = AnnexRawStorage(self.worktree, git=git)
        elif isinstance(raw_storage, GitRawStorage) and git is None:
            raise ValueError("raw_storage 為 GitRawStorage 時，必須提供 git (AnnexGit) 執行個體以維護快照 commit 歷史")
        self.raw_storage = raw_storage
        self.git = git
        # M6 & R12: 暫存目錄獨立於工作樹外部，且預設建立獨立臨時目錄避免本機衝突
        if temp_dir is not None:
            self._temp_dir = Path(temp_dir).resolve()
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="aistorage_store_"))
        self._changed_paths: list[str] = []

    #: git-annex 沒 symlink 支援時，checkout 出來的檔案會是「指標文字」
    #: （內容形如 `/annex/objects/SHA256E-…`），不是真正的內容。
    _ANNEX_POINTER_PREFIXES = (b"/annex/objects/", b".git/annex/objects/")

    def _is_annex_pointer(self, p: Path) -> bool:
        """判斷工作樹裡的檔案其實是 git-annex 的指標（而非內容）。"""
        try:
            head = p.read_bytes()[:64]
        except OSError:
            return False
        return any(head.startswith(prefix) for prefix in self._ANNEX_POINTER_PREFIXES)

    def _annex_key_from_pointer(self, p: Path) -> str | None:
        """**直接從指標檔的內容**解析 annex key（M3）。

        git-annex 沒 symlink 支援時，checkout 出來的指標內容形如
        `/annex/objects/SHA256E-s…`（或 `.git/annex/objects/…`），key 就在文字裡，
        不需要 `git annex lookupkey`（那個指令對 locked/symlink 模式不一定準）。
        解析不出來回傳 None（呼叫端要 fail-closed，不要退回下載全部物件）。
        """
        try:
            head = p.read_bytes()[:512]
        except OSError:
            return None
        text = head.decode("utf-8", "replace").strip()
        for prefix in ("/annex/objects/", ".git/annex/objects/"):
            if text.startswith(prefix):
                key = text[len(prefix):].splitlines()[0].strip() if text[len(prefix):] else ""
                return key or None
        return None

    def _materialize_annexed(self, relpath: str) -> bool:
        """把 annex 指標換成真實內容（`git annex get --key=…`）。成功回傳 True。

        為什麼需要：工作樹是 git-annex clone 時，被 annex 收走的 `.json` 記錄
        （`read_json_file` 的相容層）會 checkout 成指標文字而不是內容，而且 clone
        之後物件也還沒抓回來。整合測試在真 Drive 上遇到：第二輪讀 `meta.json`
        拿到指標文字 →「JSON 損毀」→ 整輪中止。

        M3：**key 直接從指標內容解析**；解析不出來就 raise MismatchError。
        原本這裡會退回 `git annex get --all`——那會把整個 repo 的所有 annex 物件
        （= 所有 Session 的全部歷史 raw，2.6 讓遠端大約 4.5 倍）一次下載，
        足以耗掉 runner 的 20 分鐘與磁碟，而且那一輪的耗時會讓 D9 的分鐘數估算失準。
        """
        if self.git is None:
            raise MismatchError(
                f"工作樹的檔案是 annex 指標，但沒有 git 執行個體可取回: {relpath}")
        p = self.worktree / relpath
        key = self._annex_key_from_pointer(p)
        if not key:
            raise MismatchError(
                f"annex 指標解析不出 key（不再退回 get --all，見 review M3）: {relpath}")
        proc = subprocess.run(
            ["git", "-C", str(self.worktree), "annex", "get", f"--key={key}", "--from", "origin"],
            capture_output=True,
            check=False,
            env=get_git_env(),
            timeout=300,
        )
        if proc.returncode != 0:
            # 遠端沒有這個 key（或取回失敗）：fail-closed，不要靜靜續行
            raise ReadError(
                f"從遠端取回 annex 物件失敗 (rc={proc.returncode}): {key}（{relpath}）")
        return True

    def read_json_file(self, relpath: str) -> dict[str, Any]:
        """讀工作樹裡的 JSON 記錄；必要時先讓 git-annex 取回真正的內容。

        M3：指標的處理只有一條路徑——解析不出 key 就 raise（不退回 `get --all`）；
        取回失敗也 raise。成功取回後仍讀不到 JSON 才算「JSON 損毀」。
        """
        p = self.worktree / relpath
        if self._is_annex_pointer(p):
            self._materialize_annexed(relpath)
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MismatchError(f"真本項目 JSON 損毀: {relpath}") from None
        except OSError as e:
            raise MismatchError(f"真本項目無法讀取: {relpath} ({type(e).__name__})") from None

    def get_record(self, item_id: str) -> dict[str, Any] | None:
        """依據 item_id 取得真本項目的 JSON 字典。若項目不存在回傳 None。"""
        try:
            relpath = layout.record_path_for_id(item_id)
        except ValueError:
            return None

        p = self.worktree / relpath
        if not p.is_file() and not p.is_symlink():
            return None
        return self.read_json_file(relpath)

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

    def annex_keys(self) -> frozenset[str]:
        """取得真本中所有 Session 快照與 RawStorage 所涵蓋之 annex key 集合。"""
        keys: set[str] = set()
        if isinstance(self.raw_storage, AnnexRawStorage):
            keys.update(self.raw_storage.keys())
        sessions_dir = self.worktree / "sessions"
        if sessions_dir.is_dir():
            for p in sessions_dir.glob("*/*/snapshots.jsonl"):
                if p.is_file():
                    try:
                        with open(p, encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    entry = json.loads(line)
                                    ak = entry.get("annex_key")
                                    if ak:
                                        keys.add(ak)
                    except Exception:
                        pass
        return frozenset(keys)

    def _record_changed(self, relpath: str) -> None:
        clean = relpath.lstrip("/")
        if clean not in self._changed_paths:
            self._changed_paths.append(clean)

