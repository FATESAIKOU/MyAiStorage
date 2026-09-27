"""6.4 復原（Drive 上的 repo 被刪除時，從 clone 重建）。

三種模式（見 docs/runbooks/recovery.md；完整演練只在整合測試做）：
- from-clone：用任一個 clone 推到新的前綴（或原本的前綴）；重建 pin；
  更新 config；讀取視圖完整重建。
- bak-only：主 manifest 不在、.bak 在（1.2m 的做法：以 .bak 為準，
  下一輪提交流程的 BAK_RECOVERY 會接手，管理者只需確認）。
- both-missing：主 manifest 與 .bak 都不在，只能 from-clone。

全部都在 AdminLock 之內執行（呼叫端負責）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aistorage.admin import AdminError
from aistorage.drive.model import DriveClient

RecoverMode = Literal["from-clone", "bak-only", "both-missing"]


@dataclass(frozen=True)
class RecoverPlan:
    mode: RecoverMode
    steps: tuple[str, ...]
    detail: dict[str, Any] = field(default_factory=dict)


def detect_remote_state(drive: DriveClient, prefix_folder_id: str,
                        repo_uuid: str) -> tuple[bool, bool]:
    """回傳 (主 manifest 在不在, .bak 在不在)。只查名稱符合的檔。"""
    manifest_name = f"GITMANIFEST--{repo_uuid}"
    names = [f.name for f in drive.list_children(prefix_folder_id)
             if not f.is_folder]
    return manifest_name in names, f"{manifest_name}.bak" in names


def plan_recover(*, manifest_present: bool, bak_present: bool,
                 new_prefix: str | None = None) -> RecoverPlan:
    """依遠端狀態選擇復原模式（純函式）。"""
    if manifest_present:
        return RecoverPlan(
            mode="from-clone",
            steps=("確認主 manifest 仍在：不需要復原，先跑健康檢查找真正的原因",),
            detail={"reason": "manifest-present"},
        )
    if bak_present:
        return RecoverPlan(
            mode="bak-only",
            steps=(
                "確認 .bak 的內容雜湊等於正式 pin 的 manifest（人工比對）",
                "下一輪提交流程走 BAK_RECOVERY（丟棄待定、照常往下），不需重建",
                "若待定與 .bak 皆不可信，改走 from-clone",
            ),
            detail={},
        )
    if new_prefix is None:
        raise AdminError("主 manifest 與 .bak 都不在，只能 from-clone：請指定新前綴")
    return RecoverPlan(
        mode="from-clone",
        steps=(
            "用任一個 clone 推到新前綴（git push＋上傳 bundle／manifest／annex 物件）",
            "以觀測到的遠端狀態重建正式 pin（init-pin --confirm，只在 Mac）",
            "更新 config/committer.json（前綴 id、repo_uuid 若有變化）",
            "讀取視圖完整重建（readview_rebuild_epoch 加 1，等全部上傳完才切換 manifest）",
            "跑一輪完整提交流程（含清掃）確認正常，把步驟與耗時記進 runbook",
        ),
        detail={"new_prefix": new_prefix},
    )


def plan_recover_from_drive(drive: DriveClient, prefix_folder_id: str,
                            repo_uuid: str, *,
                            new_prefix: str | None = None) -> RecoverPlan:
    """先偵測遠端狀態再選模式（列舉失敗就中止，不猜）。"""
    manifest_present, bak_present = detect_remote_state(
        drive, prefix_folder_id, repo_uuid)
    return plan_recover(manifest_present=manifest_present, bak_present=bak_present,
                        new_prefix=new_prefix)
