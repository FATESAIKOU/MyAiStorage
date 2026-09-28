"""讀取視圖（read view）格式：manifest、FileRef、ReadingRef 與常數。

提交流程與讀取介面共用（純資料＋純函式，不碰網路、不寫入）。
依據：docs/impl/group4-modules.md 第 0、2 節；design D5；
schemas/readview-manifest.schema.json。

形狀：讀取視圖資料夾是平的，只有三種檔案——manifest（固定 id，原地更新）、
index（每一個世代一份）、reading（每一個「Session × 快照」一份，內容定址、
永不更新）。manifest 的固定 id 是信任錨點：只有提交流程能 update 它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from jsonschema import Draft202012Validator

from aistorage.errors import MismatchError
from aistorage.schema import FieldError, _create_format_checker, _locate_schema_file

# 讀取視圖格式標識
READVIEW_FORMAT = "aistorage.readview/v1"

# 讀取視圖所屬的儲存要素
ELEMENT_AGORA = "agora"

# manifest 的檔名（僅供資訊用途；讀取一律以固定 id 定位）
MANIFEST_FILE_NAME = "readview-manifest.json"

# manifest 的下載上限（4 MiB）：manifest 只放 id 清單，不放內容
MANIFEST_MAX_BYTES = 4 << 20

_SCHEMA_PATH = _locate_schema_file("readview-manifest.schema.json")
with open(_SCHEMA_PATH, encoding="utf-8") as _f:
    _MANIFEST_SCHEMA = json.load(_f)

_MANIFEST_VALIDATOR = Draft202012Validator(
    _MANIFEST_SCHEMA, format_checker=_create_format_checker()
)


@dataclass(frozen=True)
class FileRef:
    """讀取視圖檔案的引用（以 id 定位，以 sha256 驗證內容）。"""

    id: str
    sha256: str
    size: int

    @classmethod
    def from_drive(cls, f: Any) -> FileRef:
        """自 DriveFile（或任何帶 id/sha256/size 的物件）建立 FileRef。

        不 import drive.model，讓 readview 保持無相依（讀者端只需標準函式庫）。
        """
        return cls(id=f.id, sha256=(f.sha256 or "").lower(), size=int(f.size or 0))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class ReadingRef:
    """一份閱讀版的引用（Session × 快照 → 檔案）。"""

    session_id: str
    snapshot_sha256: str
    file_id: str
    sha256: str
    size: int
    is_latest: bool

    @property
    def key(self) -> tuple[str, str]:
        """(session_id, snapshot_sha256 小寫)——prev_readings 的索引鍵。"""
        return (self.session_id, self.snapshot_sha256.lower())

    @property
    def file_ref(self) -> FileRef:
        return FileRef(id=self.file_id, sha256=self.sha256, size=self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "snapshot_sha256": self.snapshot_sha256,
            "file_id": self.file_id,
            "sha256": self.sha256,
            "size": self.size,
            "is_latest": self.is_latest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReadingRef:
        return cls(
            session_id=data["session_id"],
            snapshot_sha256=data["snapshot_sha256"],
            file_id=data["file_id"],
            sha256=data["sha256"],
            size=int(data["size"]),
            is_latest=bool(data["is_latest"]),
        )


@dataclass(frozen=True)
class RawRef:
    """一份原始紀錄本體的引用（Session × 快照 → 檔案）。

    `sha256` 就是該快照的 `snapshot_sha256`（內容定址），所以讀者下載後可以
    同時驗「檔案沒被動過」與「這份確實是那個快照」；`agora checkout` 的起點包
    要靠這個保證「原始紀錄原封不動」（ADR 0010 的 KV cache 要求）。
    """

    session_id: str
    snapshot_sha256: str
    file_id: str
    size: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.session_id, self.snapshot_sha256.lower())

    @property
    def file_ref(self) -> FileRef:
        return FileRef(id=self.file_id, sha256=self.snapshot_sha256.lower(), size=self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "snapshot_sha256": self.snapshot_sha256,
            "file_id": self.file_id,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawRef:
        return cls(
            session_id=data["session_id"],
            snapshot_sha256=data["snapshot_sha256"],
            file_id=data["file_id"],
            size=int(data["size"]),
        )


@dataclass(frozen=True)
class Manifest:
    """讀取視圖 manifest（信任錨點，固定 id，原地更新）。"""

    format: str
    element: str
    generation: int
    published_at: str
    agora_main_sha: str
    converter_versions: dict[str, str]
    index: FileRef | None
    files: tuple[str, ...] = ()
    retired: tuple[tuple[str, int], ...] = ()
    rebuild_epoch: int = 0
    pending: tuple[ReadingRef, ...] = ()
    pending_raws: tuple[RawRef, ...] = ()

    @property
    def is_initial(self) -> bool:
        """是否為管理者建立的初始空 manifest（尚未發佈過任何世代）。"""
        return self.generation == 0 and self.index is None

    def to_dict(self) -> dict[str, Any]:
        """序列化為符合 schemas/readview-manifest.schema.json 的字典。"""
        return {
            "format": self.format,
            "element": self.element,
            "generation": self.generation,
            "published_at": self.published_at,
            "agora_main_sha": self.agora_main_sha,
            "converter_versions": dict(self.converter_versions),
            "rebuild_epoch": self.rebuild_epoch,
            "index": self.index.to_dict() if self.index is not None else None,
            "files": list(self.files),
            "pending": [r.to_dict() for r in self.pending],
            "pending_raws": [r.to_dict() for r in self.pending_raws],
            "retired": [[fid, gen] for fid, gen in self.retired],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        """自已通過 schema 驗證的字典還原 Manifest。"""
        index_raw = data.get("index")
        index = (
            FileRef(
                id=index_raw["id"],
                sha256=index_raw["sha256"],
                size=int(index_raw["size"]),
            )
            if isinstance(index_raw, dict)
            else None
        )
        return cls(
            format=data["format"],
            element=data["element"],
            generation=int(data["generation"]),
            published_at=data["published_at"],
            agora_main_sha=data["agora_main_sha"],
            converter_versions=dict(data["converter_versions"]),
            index=index,
            files=tuple(data["files"]),
            retired=tuple((str(pair[0]), int(pair[1])) for pair in data["retired"]),
            rebuild_epoch=int(data.get("rebuild_epoch", 0)),
            pending=tuple(ReadingRef.from_dict(r) for r in data.get("pending", [])),
            pending_raws=tuple(RawRef.from_dict(r) for r in data.get("pending_raws", [])),
        )


def initial_manifest(
    *,
    element: str = ELEMENT_AGORA,
    agora_main_sha: str = "",
    published_at: str,
) -> Manifest:
    """建立管理者初始化用的空 manifest（generation=0、index 為 null）。

    管理者在 Mac 上以提交流程的身分建立一次，id 寫進 config/committer.json
    與讀者設定（PM 決定 10）。之後每一輪由 publisher 以 next_manifest 切換世代。
    """
    return Manifest(
        format=READVIEW_FORMAT,
        element=element,
        generation=0,
        published_at=published_at,
        agora_main_sha=agora_main_sha or "unborn",
        converter_versions={},
        index=None,
        files=(),
        retired=(),
        rebuild_epoch=0,
        pending=(),
        pending_raws=(),
    )


def next_manifest(
    prev: Manifest,
    *,
    index: FileRef | None,
    files: tuple[str, ...],
    reading_refs: tuple[ReadingRef, ...],
    retire_now: tuple[str, ...] = (),
    delete_now: tuple[str, ...] = (),
    published_at: str,
    agora_main_sha: str,
    converter_versions: dict[str, str],
    rebuild_epoch: int | None = None,
    raw_refs: tuple[RawRef, ...] = (),
) -> Manifest:
    """由舊 manifest 推出新世代的 manifest（純函式）。

    - generation 為 prev + 1（單調遞增）。
    - retired ＝（prev.retired 減去 delete_now）∪（retire_now 配上新世代），
      所以被退役的檔案要再等一個世代才永久刪除。
    - files 為本世代引用的全部 file id（index ＋全部 reading ＋全部原始紀錄本體）。
    - pending 為已上傳但尚未被本世代引用的 reading（完整重建分批上傳的中間輪次）；
      pending_raws 是對應的原始紀錄本體。兩者都不在 files 內，但列入 trusted_ids，
      避免下一輪被清掃隔離。
    """
    removed = set(delete_now)
    keep_retired = tuple(
        (fid, gen) for fid, gen in prev.retired if fid not in removed
    )
    new_retired = keep_retired + tuple((fid, prev.generation + 1) for fid in retire_now)
    epoch = prev.rebuild_epoch if rebuild_epoch is None else rebuild_epoch
    return Manifest(
        format=READVIEW_FORMAT,
        element=prev.element,
        generation=prev.generation + 1,
        published_at=published_at,
        agora_main_sha=agora_main_sha,
        converter_versions=dict(converter_versions),
        index=index,
        files=tuple(files),
        retired=new_retired,
        rebuild_epoch=epoch,
        pending=reading_refs,
        pending_raws=tuple(raw_refs),
    )


def parse_manifest(data: bytes) -> Manifest:
    """解析 manifest 位元組；不符 schema → MismatchError。

    讀者與提交流程都走這裡：manifest 損毀是不可信任的狀態，必須明確報錯，
    不可退化成「當作沒有 manifest」。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise MismatchError("manifest 必須是位元組")
    try:
        obj = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise MismatchError(f"manifest JSON 解析失敗: {e}") from None

    errors: list[FieldError] = []
    for err in _MANIFEST_VALIDATOR.iter_errors(obj):
        path_str = ".".join(str(p) for p in err.path)
        field = path_str or (str(err.path[-1]) if err.path else "")
        errors.append(FieldError(field=str(field), message=err.message))
    if errors:
        detail = "; ".join(f"{e.field}: {e.message}" for e in errors[:5])
        raise MismatchError(f"manifest 不符合 schemas/readview-manifest.schema.json: {detail}")

    return Manifest.from_dict(obj)


def serialize_manifest(m: Manifest) -> bytes:
    """序列化 manifest（sort_keys、indent=2、結尾換行；輸出確定性）。"""
    return (json.dumps(m.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def trusted_ids(m: Manifest, manifest_file_id: str) -> frozenset[str]:
    """讀取視圖的可信 file id 集合（供第 4 步清掃）。

    ＝ manifest 自身 ＋ 本世代引用的檔案 ＋ pending ＋ retired。
    不在集合內者一律隔離（ADR 0008：可信內容的唯一來源是釘選值／manifest）。
    """
    ids = {manifest_file_id}
    ids.update(m.files)
    ids.update(r.file_id for r in m.pending)
    ids.update(r.file_id for r in m.pending_raws)
    ids.update(fid for fid, _ in m.retired)
    return frozenset(ids)
