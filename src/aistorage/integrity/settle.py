"""AiStorage 待定釘選值結算模組。

依據規格：
- docs/impl/group3-modules.md 第 3.3 節
- design.md D2（第 3 步結算、唯讀不移動、重放驗證、.bak 復原）
- ADR 0008（信任錨點防線）
- review-1.4f3 M2（不存在「信任任何能重放的 manifest」模式，僅 state 與 pending 可信）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import shutil
import tempfile
from typing import Any

from aistorage.annex.manifest import BundleName, Manifest, parse_bundle_name, parse_manifest
from aistorage.annex.replay import replay_refs
from aistorage.clock import Clock
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import MismatchError, ReadError, TooLarge
from aistorage.integrity.pin import PinPending, PinState


@dataclass(frozen=True)
class RepoListing:
    """單次列舉前綴資料夾之結果（供第 3 步結算與第 4 步清掃共用）。"""

    prefix_folder_id: str
    files: tuple[DriveFile, ...]
    subfolders: tuple[DriveFile, ...]


class SettleOutcome(Enum):
    """待定結算結果狀態。"""

    NO_PENDING = "no_pending"          # 無待定釘選值，直接跳過
    PROMOTED = "promoted"              # 遠端已成功落實待定狀態 -> 轉正
    DROPPED = "dropped"                # 遠端仍為正式狀態（push 未發生或已還原）-> 丟棄待定
    BAK_RECOVERY = "bak_recovery"      # 主 manifest 缺失但 .bak 等於正式釘選值 -> 丟棄待定，照常往下


def check_manifest_continuity(state: PinState, parsed_manifest: Manifest) -> None:
    """驗證 manifest 連續性（M3、M5）。

    1. removed ⊇ state.removed_bundles（已移除不可遺失）
    2. 新增的 removed 必須來自原本的 active（removed - state.removed_bundles ⊆ state.active_bundles）
    3. 舊的 active 不可憑空消失（state.active_bundles ⊆ new.active ∪ new.removed）
    """
    existing_active = set(state.active_bundles)
    new_active = set(parsed_manifest.active)
    new_removed = set(parsed_manifest.removed)

    # 1. removed 包含關係
    if not parsed_manifest.removed.issuperset(state.removed_bundles):
        missing_removed = state.removed_bundles - parsed_manifest.removed
        raise MismatchError(f"新 manifest 遺失了既有已移除 bundle 紀錄: {missing_removed}")

    # 2. 新移除者必須來自原本 active
    newly_removed = parsed_manifest.removed - state.removed_bundles
    if not newly_removed.issubset(existing_active):
        invalid_removed = newly_removed - existing_active
        raise MismatchError(f"新移除之 bundle 以前並非 active bundle: {invalid_removed}")

    # 3. 舊 active 不能憑空消失 (M5)
    disappeared = existing_active - (new_active | new_removed)
    if disappeared:
        raise MismatchError(f"舊 active bundle 憑空消失（既非 active 亦未列入 removed）: {disappeared}")


def _download_and_replay(
    active_bundle_names: tuple[str, ...],
    repo_uuid: str,
    available_files: tuple[DriveFile, ...],
    drive: DriveClient,
    workdir: Path,
) -> dict[str, str]:
    """依序下載 active bundle 並於獨立環境中重放計算 refs。"""
    temp_bundle_dir = Path(tempfile.mkdtemp(prefix="aistorage_settle_bundles_", dir=str(workdir)))
    downloaded_paths: list[tuple[BundleName, Path]] = []

    try:
        for idx, b_name in enumerate(active_bundle_names):
            b_info = parse_bundle_name(b_name)
            if not b_info or b_info.repo_uuid != repo_uuid:
                raise MismatchError(f"active bundle 檔名不合法或 repo_uuid 不符: {b_name}")

            # 在 listing 中尋找檔名相符且 sha256 相符之檔案
            matched = [
                f for f in available_files
                if f.name == b_name and f.sha256 == b_info.sha256
            ]
            if not matched:
                raise MismatchError(f"找不到符合 active 宣告與雜湊之 bundle: {b_name}")

            target_file = matched[0]
            dest = temp_bundle_dir / f"{idx:04d}_{b_name}"
            # 單一 bundle 下載上限 256 MiB
            drive.download(target_file.id, dest, max_bytes=256 * 1024 * 1024)
            downloaded_paths.append((b_info, dest))

        replayed = replay_refs(
            downloaded_paths,
            workdir=workdir,
            repo_uuid=repo_uuid,
        )
        return replayed
    finally:
        shutil.rmtree(temp_bundle_dir, ignore_errors=True)


def settle(
    state: PinState,
    pending: PinPending | None,
    listing: RepoListing,
    drive: DriveClient,
    *,
    workdir: Path,
    clock: Clock,
) -> tuple[SettleOutcome, PinState]:
    """結算待定釘選值（提交流程第 3 步）。

    全程唯讀，不做任何移動。
    1. 候選 manifest：名稱符合 GITMANIFEST--<uuid> 的檔案（可能有多個同名），逐一 download_bytes（上限 1 MiB）。
    2. 對每個候選：parse_manifest -> active 的每個 bundle 下載並 replay_refs。
    3. 判定：
       - 有候選 refs == pending.refs 且通過連續性檢查，無第二個內容相異但亦相符之候選 -> PROMOTED（產生新 PinState）。
       - 有候選 refs == state.refs 且內容雜湊 == state.manifest_sha256 -> DROPPED。
       - 主 manifest 不在、.bak 內容雜湊 == state.manifest_sha256 且重放 refs == state.refs -> BAK_RECOVERY。
       - 其他情形 -> 拋出 MismatchError（中止）。
    4. 任何 ReadError 原樣往上拋（中止，不猜）。
    """
    if pending is None:
        return SettleOutcome.NO_PENDING, state

    # M3: 檢查 pending 基準是否與當前正式值一致
    if pending.base_manifest_sha256 != state.manifest_sha256:
        raise MismatchError(
            f"待定基準雜湊 ({pending.base_manifest_sha256}) 與正式釘選值雜湊 ({state.manifest_sha256}) 不符"
        )

    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    main_name = f"GITMANIFEST--{state.repo_uuid}"
    bak_name = f"GITMANIFEST--{state.repo_uuid}.bak"

    main_files = [f for f in listing.files if f.name == main_name]

    # 若無主 manifest，檢驗是否符合 Rule C: .bak 復原
    if not main_files:
        bak_files = [f for f in listing.files if f.name == bak_name]
        for bf in bak_files:
            b_data = drive.download_bytes(bf.id, max_bytes=1024 * 1024)
            b_hash = hashlib.sha256(b_data).hexdigest().lower()
            if b_hash == state.manifest_sha256:
                try:
                    parsed_bak = parse_manifest(b_data, repo_uuid=state.repo_uuid)
                    replayed = _download_and_replay(
                        parsed_bak.active, state.repo_uuid, listing.files, drive, workdir
                    )
                    if replayed == state.refs:
                        return SettleOutcome.BAK_RECOVERY, state
                except (MismatchError, TooLarge):
                    continue
        raise MismatchError("主 manifest 缺失且 .bak 無法恢復至正式釘選值")

    # 存在主 manifest 候選檔案
    candidate_results: list[dict[str, Any]] = []

    for mf in main_files:
        m_bytes = drive.download_bytes(mf.id, max_bytes=1024 * 1024)
        m_sha = hashlib.sha256(m_bytes).hexdigest().lower()

        try:
            parsed = parse_manifest(m_bytes, repo_uuid=state.repo_uuid)
            replayed = _download_and_replay(
                parsed.active, state.repo_uuid, listing.files, drive, workdir
            )
            candidate_results.append({
                "sha256": m_sha,
                "manifest": parsed,
                "refs": replayed,
            })
        except (MismatchError, TooLarge):
            # 無效或無法重放之候選
            continue

    # 1. 檢查是否符合 PROMOTED（符合 pending.refs 且通過連續性檢查）
    pending_matches = []
    for c in candidate_results:
        if c["refs"] == pending.refs:
            check_manifest_continuity(state, c["manifest"])
            pending_matches.append(c)

    if pending_matches:
        distinct_shas = {c["sha256"] for c in pending_matches}
        if len(distinct_shas) > 1:
            raise MismatchError(
                f"存在多個內容相異但重放 refs 均相符待定之 manifest 候選: {distinct_shas}"
            )
        matched = pending_matches[0]
        new_state = PinState(
            repo=state.repo,
            repo_uuid=state.repo_uuid,
            refs=pending.refs,
            manifest_sha256=matched["sha256"],
            prev_manifest_sha256=state.manifest_sha256,
            active_bundles=matched["manifest"].active,
            removed_bundles=matched["manifest"].removed,
            annex_keys=pending.annex_keys,
            promoted_at=clock.now_utc(),
            run_id=pending.run_id,
        )
        return SettleOutcome.PROMOTED, new_state

    # 2. 檢查是否符合 DROPPED（符合 state.refs 且內容雜湊相符）
    state_matches = [
        c for c in candidate_results
        if c["refs"] == state.refs and c["sha256"] == state.manifest_sha256
    ]
    if state_matches:
        return SettleOutcome.DROPPED, state

    # 3. 兩者皆不符合 -> 中止
    raise MismatchError("遠端 manifest 狀態與待定或正式釘選值均不符，無法自動結算")
