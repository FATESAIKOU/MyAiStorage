"""AiStorage 收件匣掃描模組。

依據規格：
- docs/impl/group3-modules.md 第 4.1 節
- design D2、review-1.8 L4（只算形狀符合的項目數，0 就結束）
"""

from __future__ import annotations

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
    sidecar: DriveFile | None
    sig: DriveFile | None
    raw: DriveFile | None
    extras: tuple[DriveFile, ...] = ()


def is_actionable(item: InboxItem) -> bool:
    """判定收件匣項目是否形狀符合（具備 sidecar 與 sig）。"""
    return item.sidecar is not None and item.sig is not None


def count_shaped(items: list[InboxItem]) -> int:
    """計算具備處理形狀（有 sig 與 sidecar）之項目總數。

    第 2 步使用：若 count_shaped(items) == 0 則提交流程提前結束（review-1.8 L4）。
    """
    return sum(1 for item in items if is_actionable(item))


def scan_inboxes(drive: DriveClient, registry: Registry) -> list[InboxItem]:
    """掃描登錄檔所有收件匣資料夾，將檔案依 <ULID>.(raw|sidecar.json|sig) 分組。

    - 排除 trashed=True 之檔案（DriveClient API 預設已排除）。
    - 依據 item_key (<ULID>) 匯總 sidecar、sig、raw。
    - 若有同名重複檔案，第一個保留，其餘歸入 extras。
    - 任何不符合格式之檔案（非有效 ULID 或無效副檔名）歸入該資料夾或該 ULID 之 extras。
    """
    inbox_map = registry.inbox_folders()
    items: list[InboxItem] = []

    for folder_id in inbox_map:
        children = drive.list_children(folder_id)

        # 暫存分組：ulid -> {"sidecar": None, "sig": None, "raw": None, "extras": []}
        grouped: dict[str, dict[str, Any]] = {}
        unmatched_files: list[DriveFile] = []

        for f in children:
            if f.is_folder:
                # 收件匣中出現資料夾視為 extra
                unmatched_files.append(f)
                continue

            m = _INBOX_PART_PATTERN.match(f.name)
            if m:
                ulid = m.group("ulid")
                ext = m.group("ext")

                if ulid not in grouped:
                    grouped[ulid] = {
                        "sidecar": None,
                        "sig": None,
                        "raw": None,
                        "extras": [],
                    }

                bucket = grouped[ulid]
                if ext == "sidecar.json":
                    if bucket["sidecar"] is None:
                        bucket["sidecar"] = f
                    else:
                        bucket["extras"].append(f)
                elif ext == "sig":
                    if bucket["sig"] is None:
                        bucket["sig"] = f
                    else:
                        bucket["extras"].append(f)
                elif ext == "raw":
                    if bucket["raw"] is None:
                        bucket["raw"] = f
                    else:
                        bucket["extras"].append(f)
            else:
                # 檢查是否帶有 ULID 前綴但副檔名不符
                parts = f.name.split(".", 1)
                if len(parts) == 2 and _ULID_PATTERN.match(parts[0]):
                    ulid = parts[0]
                    if ulid not in grouped:
                        grouped[ulid] = {
                            "sidecar": None,
                            "sig": None,
                            "raw": None,
                            "extras": [],
                        }
                    grouped[ulid]["extras"].append(f)
                else:
                    unmatched_files.append(f)

        for ulid, b in grouped.items():
            items.append(
                InboxItem(
                    item_key=ulid,
                    inbox_folder_id=folder_id,
                    sidecar=b["sidecar"],
                    sig=b["sig"],
                    raw=b["raw"],
                    extras=tuple(b["extras"]),
                )
            )

        for unf in unmatched_files:
            items.append(
                InboxItem(
                    item_key=unf.name,
                    inbox_folder_id=folder_id,
                    sidecar=None,
                    sig=None,
                    raw=None,
                    extras=(unf,),
                )
            )

    return items
