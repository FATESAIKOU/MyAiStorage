"""讀取視圖初始化（管理者，一次性；部署手冊步驟 5）。

用**提交流程的身分**（committer 的 rclone conf）在讀取視圖資料夾建立
generation = 0 的空 manifest，並把資料夾分享給讀取用的 service account。
manifest 的 **file id** 是讀取端唯一的定位依據（D5／PM 決定 10），所以：

- 建立之後 id 固定，之後每一輪由 publisher 以 `update_content` 原地更新；
- 印出這個 id（不是秘密，`docs/resources.md` 與 `reader.json` 都要填）；
- **已存在就拒絕覆寫**：manifest 是可信內容的根，覆寫它等於改掉讀取端的
  信任錨點。要重來只能走 `docs/runbooks/recovery.md`。

分享的單位是**資料夾**而不是 manifest 檔：讀取端要依 id 讀 manifest、index
與各份 reading，只分享檔案本身不夠（見 `1.5` 的做法）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aistorage.admin import AdminError
from aistorage.clock import Clock, format_rfc3339
from aistorage.drive.model import DriveClient
from aistorage.readview.model import initial_manifest, parse_manifest, serialize_manifest

#: 讀取視圖 manifest 的檔名（id 才是重點，名字只是給人看；1.4i 用的是 manifest.json）
MANIFEST_NAME = "manifest.json"

#: 讀取視圖 manifest 的大小上限（-generation 0 只有幾百 bytes）
_MAX_MANIFEST_BYTES = 1 << 20


@dataclass(frozen=True)
class InitReadviewPlan:
    folder_id: str
    manifest_name: str
    existing_manifest_id: str | None
    existing_manifest_name: str | None
    sa_email: str | None
    folder_children: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return self.existing_manifest_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "manifest_name": self.manifest_name,
            "existing_manifest_id": self.existing_manifest_id,
            "existing_manifest_name": self.existing_manifest_name,
            "sa_email": self.sa_email,
            "will_share_folder": self.sa_email is not None,
            "folder_children": list(self.folder_children),
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class InitReadviewResult:
    manifest_file_id: str
    generation: int
    folder_id: str
    shared_with: str | None
    permission_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_file_id": self.manifest_file_id,
            "generation": self.generation,
            "folder_id": self.folder_id,
            "shared_with": self.shared_with,
            "permission_id": self.permission_id,
        }


def _find_existing_manifest(
    drive: DriveClient, folder_id: str
) -> tuple[str | None, str | None]:
    """資料夾裡既有的讀取視圖 manifest（依名稱，或依「能不能解析」）。

    只看檔名不夠：1.4i 的經驗是 Drive 允許同名檔（會變成兩份），所以也要看內容
    能不能被 `parse_manifest` 解析。
    """
    by_name: tuple[str | None, str | None] = (None, None)
    for child in drive.list_children(folder_id):
        if child.is_folder:
            continue
        if child.name == MANIFEST_NAME:
            by_name = (child.id, child.name)
        try:
            data = drive.download_bytes(child.id, max_bytes=_MAX_MANIFEST_BYTES)
        except Exception:
            continue
        try:
            parse_manifest(data)
        except Exception:
            continue
        return child.id, child.name
    return by_name


def plan_init_readview(drive: DriveClient, folder_id: str, *,
                       sa_email: str | None = None) -> InitReadviewPlan:
    """唯讀計畫：資料夾存在嗎、裡面是不是已經有 manifest、要不要分享。"""
    try:
        meta = drive.get(folder_id)
    except Exception as e:
        raise AdminError(f"讀不到讀取視圖資料夾 {folder_id}: {e}") from None
    if not meta.is_folder:
        raise AdminError(f"{folder_id} 不是資料夾（{meta.name}）")
    if sa_email is not None and "@" not in sa_email:
        raise AdminError(f"--sa-email 看起來不是 email: {sa_email!r}")
    existing_id, existing_name = _find_existing_manifest(drive, folder_id)
    children = tuple(
        f"{c.name}" for c in drive.list_children(folder_id) if not c.is_folder)
    return InitReadviewPlan(
        folder_id=folder_id,
        manifest_name=MANIFEST_NAME,
        existing_manifest_id=existing_id,
        existing_manifest_name=existing_name,
        sa_email=sa_email,
        folder_children=children,
    )


def init_readview(drive: DriveClient, folder_id: str, *,
                   sa_email: str | None = None, confirm: bool = False,
                   clock: Clock | None = None,
                   element: str = "agora") -> InitReadviewPlan | InitReadviewResult:
    """建立 generation = 0 的空 manifest（必要時一併分享資料夾給 SA）。

    `confirm=False` 時只回傳計畫，不寫入任何東西。`element` 決定 manifest 屬於哪個
    要素——期 1 只有 Agora 有讀取視圖（ADR 0009：Foundry 的產出登錄不再經過這裡）。
    """
    if element != "agora":
        raise AdminError(
            f"未知的 element {element!r}：期 1 只有 Agora 有讀取視圖（ADR 0009）")
    plan = plan_init_readview(drive, folder_id, sa_email=sa_email)
    if not confirm:
        return plan
    if plan.existing_manifest_id is not None:
        raise AdminError(
            f"讀取視圖資料夾 {folder_id} 裡已經有 manifest"
            f"（{plan.existing_manifest_name}，id={plan.existing_manifest_id}）："
            "拒絕覆寫。manifest 是讀取端的信任錨點；要重來請走 "
            "docs/runbooks/recovery.md，或先手動刪掉既有 manifest 再執行。")

    from datetime import timezone

    now = clock.now() if clock is not None else datetime.now(timezone.utc)
    body = serialize_manifest(
        initial_manifest(element=element,
                         published_at=format_rfc3339(now, include_fraction=True)))
    created = drive.create(folder_id, MANIFEST_NAME, body,
                           mime_type="application/json")
    permission_id = None
    if sa_email is not None:
        permission_id = drive.share(folder_id, email=sa_email, role="reader") or None
    return InitReadviewResult(
        manifest_file_id=created.id,
        generation=0,
        folder_id=folder_id,
        shared_with=sa_email,
        permission_id=permission_id,
    )
