"""AiStorage 收件匣掃描模組。

依據規格：
- docs/impl/group3-modules.md 第 4.1 節
- design D2、review-1.8 L4（只算形狀符合的項目數，0 就結束）
- review-g3d M1（同名重複檔案多候選保留與逐一嘗試）、M4（拆分 items 與 junk）
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import re
from typing import Any

from aistorage.drive.model import DriveClient, DriveFile
from aistorage.identity import Registry

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_INBOX_PART_PATTERN = re.compile(
    r"^(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})\.(?P<ext>sidecar\.json|sig|raw)$"
)


@dataclass(frozen=True)
class InboxItem:
    """收件匣項目分組結構。"""

    item_key: str
    inbox_folder_id: str
    sidecar: DriveFile | None = None
    sig: DriveFile | None = None
    raw: DriveFile | None = None
    extras: tuple[DriveFile, ...] = ()
    sidecars: tuple[DriveFile, ...] = ()
    sigs: tuple[DriveFile, ...] = ()
    raws: tuple[DriveFile, ...] = ()

    def __post_init__(self) -> None:
        if self.sidecar is not None and not self.sidecars:
            object.__setattr__(self, "sidecars", (self.sidecar,))
        elif self.sidecars and self.sidecar is None:
            object.__setattr__(self, "sidecar", self.sidecars[0])

        if self.sig is not None and not self.sigs:
            object.__setattr__(self, "sigs", (self.sig,))
        elif self.sigs and self.sig is None:
            object.__setattr__(self, "sig", self.sigs[0])

        if self.raw is not None and not self.raws:
            object.__setattr__(self, "raws", (self.raw,))
        elif self.raws and self.raw is None:
            object.__setattr__(self, "raw", self.raws[0])


@dataclass(frozen=True)
class InboxScan:
    """收件匣掃描結果（M4：拆分合法 ULID 項目與垃圾檔案）。"""

    items: tuple[InboxItem, ...]
    junk: tuple[DriveFile, ...]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[InboxItem]:
        return iter(self.items)

    def __getitem__(self, idx: int) -> InboxItem:
        return self.items[idx]


def is_actionable(item: InboxItem) -> bool:
    """判定收件匣項目是否形狀符合（具備 sidecar 與 sig）。"""
    return (item.sidecar is not None or bool(item.sidecars)) and (
        item.sig is not None or bool(item.sigs)
    )


def count_shaped(scan_or_items: InboxScan | Sequence[InboxItem]) -> int:
    """計算具備處理形狀（有 sig 與 sidecar）之項目總數。

    第 2 步使用：若 count_shaped(items) == 0 則提交流程提前結束（review-1.8 L4）。
    M4: 只看 items，不計入 junk。
    """
    items = (
        scan_or_items.items
        if isinstance(scan_or_items, InboxScan)
        else scan_or_items
    )
    return sum(1 for item in items if is_actionable(item))


def scan_inboxes(drive: DriveClient, registry: Registry) -> InboxScan:
    """掃描登錄檔所有收件匣資料夾，將檔案依 <ULID>.(raw|sidecar.json|sig) 分組。

    - 排除 trashed=True 之檔案（DriveClient API 預設已排除）。
    - 依據 item_key (<ULID>) 匯總 sidecar、sig、raw。
    - M1: 同一個 part 有多個同名檔時，全部保留於 sidecars、sigs、raws 候選清單中。
    - M4: 不符合格式之檔案（非有效 ULID 或在收件匣中的資料夾）歸入 junk，不進 evaluate。
    """
    inbox_map = registry.inbox_folders()
    items: list[InboxItem] = []
    junk: list[DriveFile] = []

    for folder_id in inbox_map:
        children = drive.list_children(folder_id)

        grouped: dict[str, dict[str, Any]] = {}

        for f in children:
            if f.is_folder:
                # 收件匣中出現資料夾視為 junk (M4)
                junk.append(f)
                continue

            m = _INBOX_PART_PATTERN.match(f.name)
            if m:
                ulid = m.group("ulid")
                ext = m.group("ext")

                if ulid not in grouped:
                    grouped[ulid] = {
                        "sidecars": [],
                        "sigs": [],
                        "raws": [],
                        "extras": [],
                    }

                bucket = grouped[ulid]
                if ext == "sidecar.json":
                    bucket["sidecars"].append(f)
                elif ext == "sig":
                    bucket["sigs"].append(f)
                elif ext == "raw":
                    bucket["raws"].append(f)
            else:
                # 檢查是否帶有 ULID 前綴但副檔名不符
                parts = f.name.split(".", 1)
                if len(parts) == 2 and _ULID_PATTERN.match(parts[0]):
                    ulid = parts[0]
                    if ulid not in grouped:
                        grouped[ulid] = {
                            "sidecars": [],
                            "sigs": [],
                            "raws": [],
                            "extras": [],
                        }
                    grouped[ulid]["extras"].append(f)
                else:
                    junk.append(f)

        for ulid, b in grouped.items():
            scs = tuple(b["sidecars"])
            sigs = tuple(b["sigs"])
            raws = tuple(b["raws"])
            extras = tuple(b["extras"])
            items.append(
                InboxItem(
                    item_key=ulid,
                    inbox_folder_id=folder_id,
                    sidecar=scs[0] if scs else None,
                    sig=sigs[0] if sigs else None,
                    raw=raws[0] if raws else None,
                    extras=extras,
                    sidecars=scs,
                    sigs=sigs,
                    raws=raws,
                )
            )

    return InboxScan(items=tuple(items), junk=tuple(junk))
