"""AiStorage 回收與隔離清理模組。

依據規格：
- docs/impl/group3-modules.md 第 3.6 節
- design.md D2（第 11 步回收、每輪上限 200、隔離保留 7 天）
- review-1.3c M1（永久刪除前逐一 get 確認 parents 與名稱集合之防呆機制）
- review-1.8 H2（bundle 回收機制使 repo 規模有上限）
- review-g3c H3（按日期分子資料夾 quarantine/<YYYY-MM-DD>/，purge 依子資料夾名稱或 created_at 判定 7 天）
- review-g3c M6（GC 與 purge 採盡力而為，捕獲 ReadError/WriteError/NotFound 不中止整輪）
- review-g3c L（候選檔案依名稱排序以維持確定性）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aistorage.clock import parse_rfc3339
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import MismatchError, NotFound, ReadError, WriteError
from aistorage.integrity.pin import PinState
from aistorage.integrity.settle import RepoListing

MAX_GC_PER_RUN = 200
DEFAULT_QUARANTINE_DAYS = 7


def collect_removed_bundles(listing: RepoListing, state: PinState) -> list[DriveFile]:
    """自列舉清單中挑選出已被 consolidate 取代之 bundle 檔案清單。"""
    return [f for f in listing.files if f.name in state.removed_bundles]


def gc_removed(
    files: list[DriveFile],
    drive: DriveClient,
    *,
    prefix_folder_id: str,
    state: PinState,
    max_delete: int = MAX_GC_PER_RUN,
    dry_run: bool = False,
) -> int:
    """永久刪除已移除 (consolidate) 之 bundle 檔案（提交流程第 11 步）。

    防呆安全保證（review-1.3c M1 & review-1.8 H2）：
    - 排序維持確定性（L）。
    - 永久刪除前，逐一呼叫 drive.get(id) 再次確認其 parents 包含 prefix_folder_id，且名稱屬於 state.removed_bundles。
    - 若防呆檢查不符（MismatchError）嚴格拋出中斷（M6）。
    - 盡力而為（M6）：讀寫失敗（ReadError, WriteError, NotFound）捕捉並略過，不中止整輪。
    - 單輪刪除數量上限預設為 200 個，避免批次超時，未刪除者留待後續輪次回收。
    """
    candidates = sorted(files, key=lambda x: x.name)[:max_delete]
    if dry_run:
        return len(candidates)

    deleted_count = 0
    for f in candidates:
        try:
            df = drive.get(f.id)
        except (ReadError, WriteError, NotFound):
            continue

        # 防呆檢查：不符者必須嚴格拋出 MismatchError
        if prefix_folder_id not in df.parents:
            raise MismatchError(
                f"回收防呆檢查失敗：檔案 {df.id} ({df.name}) 之 parents {df.parents} 不包含前綴資料夾 {prefix_folder_id}"
            )
        if df.name not in state.removed_bundles:
            raise MismatchError(
                f"回收防呆檢查失敗：檔案 {df.id} ({df.name}) 不在釘選值之已移除清單中"
            )

        try:
            drive.delete_permanently(df.id)
            deleted_count += 1
        except (ReadError, WriteError, NotFound):
            continue

    return deleted_count


def purge_quarantine(
    drive: DriveClient,
    quarantine_folder_id: str,
    *,
    older_than_days: int = DEFAULT_QUARANTINE_DAYS,
    now: str | datetime,
    max_delete: int = MAX_GC_PER_RUN,
    dry_run: bool = False,
) -> int:
    """清理隔離資料夾中超過保留期限（預設 7 天）之子資料夾（或個別檔案）。

    - 依據子資料夾名稱（YYYY-MM-DD）或 created_at 比對超過指定天數者執行永久刪除（H3）。
    - 盡力而為（M6）：捕捉 ReadError、WriteError、NotFound。
    - 單輪上限 max_delete（預設 200）。
    """
    if isinstance(now, str):
        now_dt = parse_rfc3339(now)
    else:
        now_dt = now

    cutoff_dt = now_dt - timedelta(days=older_than_days)

    try:
        children = drive.list_children(quarantine_folder_id)
    except (ReadError, WriteError, NotFound):
        return 0

    expired_items: list[DriveFile] = []
    for item in children:
        is_expired = False
        if item.is_folder:
            try:
                folder_date = datetime.strptime(item.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if folder_date < cutoff_dt:
                    is_expired = True
            except ValueError:
                if item.created_at < cutoff_dt:
                    is_expired = True
        else:
            if item.created_at < cutoff_dt:
                is_expired = True

        if is_expired:
            expired_items.append(item)

    candidates = sorted(expired_items, key=lambda x: x.name)[:max_delete]
    if dry_run:
        return len(candidates)

    deleted_count = 0
    for item in candidates:
        try:
            drive.delete_permanently(item.id)
            deleted_count += 1
        except (ReadError, WriteError, NotFound):
            continue

    return deleted_count
