"""AiStorage 核對、預檢與 push 後驗證模組。

依據規格：
- docs/impl/group3-modules.md 第 3.5 節
- design.md D2（第 5 步 clone 驗證、第 9 步預檢、第 10 步 push 後重放驗證）
- review-1.2-1.6 H3（push 後以 ls-remote 驗證）
- review-1.4f3 M1（push 後驗證：active 清單比對、created_time、removed 包含關係、重放比對）
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import tempfile

from aistorage.annex.git import AnnexGit
from aistorage.annex.manifest import parse_bundle_name, parse_manifest
from aistorage.annex.replay import replay_refs
from aistorage.drive.model import DriveClient
from aistorage.errors import MismatchError
from aistorage.integrity.pin import PinState
from aistorage.integrity.settle import RepoListing


def verify_clone(
    git: AnnexGit,
    state: PinState,
    *,
    drive: DriveClient | None = None,
    prefix_folder_id: str | None = None,
) -> None:
    """提交流程第 5 步：驗證 clone 成果。

    - git.ls_remote() 之 ref 集合與值必須完全等於 state.refs。
    - 若提供 drive 與 prefix_folder_id，遠端主 manifest 必須恰好一個且內容雜湊等於 state.manifest_sha256。
    - 否則拋出 MismatchError。
    """
    remote_refs = git.ls_remote()
    if remote_refs != state.refs:
        raise MismatchError(
            f"clone 後 ls-remote ({remote_refs}) 與正式釘選值 refs ({state.refs}) 不符"
        )

    if drive is not None and prefix_folder_id is not None:
        manifest_name = f"GITMANIFEST--{state.repo_uuid}"
        m_files = drive.find_by_name(prefix_folder_id, manifest_name)
        if len(m_files) != 1:
            raise MismatchError(f"遠端主 manifest 數量異常: 找到 {len(m_files)} 個 (預期恰好 1 個)")
        if m_files[0].sha256 != state.manifest_sha256:
            raise MismatchError(
                f"遠端主 manifest 雜湊 ({m_files[0].sha256}) 與釘選值 ({state.manifest_sha256}) 不符"
            )


def precheck(
    drive: DriveClient,
    prefix_folder_id: str,
    manifest_name: str,
    state: PinState,
) -> None:
    """提交流程第 9 步：push 前預檢。

    - 只查名稱符合 manifest_name 的檔案。
    - 必須恰好一個，且 sha256Checksum 等於 state.manifest_sha256。
    - 任何不符拋出 MismatchError（中止流程）。
    """
    files = drive.find_by_name(prefix_folder_id, manifest_name)
    if len(files) != 1:
        raise MismatchError(f"預檢失敗：遠端 manifest 數量異常 ({len(files)} != 1)")
    if files[0].sha256 != state.manifest_sha256:
        raise MismatchError(
            f"預檢失敗：遠端 manifest 雜湊 ({files[0].sha256}) 與釘選值 ({state.manifest_sha256}) 不符"
        )


@dataclass(frozen=True)
class PushVerification:
    """push 後驗證通過之成果結構。"""

    new_manifest_sha256: str
    active: tuple[str, ...]
    removed: frozenset[str]


def verify_after_push(
    git: AnnexGit,
    drive: DriveClient,
    listing_before: RepoListing,
    state: PinState,
    local_refs: dict[str, str],
    push_started_at: str,
    *,
    workdir: Path,
) -> PushVerification:
    """提交流程第 10 步：push 後遠端狀態驗證。

    規則（review-1.2-1.6 H3、review-1.4f3 M1）：
    1. ls_remote == local_refs（全部 ref 相等）。
    2. 重新列舉，主 manifest 恰好一個，解析出 active 與 removed。
    3. active 裡不在 state.active_bundles 的新增 bundle：
       - 其 created_time >= push_started_at。
       - listing_before 裡沒有同名檔。
    4. removed 包含關係：
       - removed 必須為 state.removed_bundles 之超集（removed ⊇ state.removed_bundles）。
       - 新移除者必須來自原本的 active（removed − state.removed_bundles ⊆ state.active_bundles）。
    5. 下載新增 bundle，連同既有 active 依序重放，驗證 refs == local_refs。
    - 任何一條不符拋出 MismatchError（待定釘選值保留，留待下一輪 settle 結算）。
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. ls-remote 嚴格比對
    remote_refs = git.ls_remote()
    if remote_refs != local_refs:
        raise MismatchError(
            f"push 後 ls-remote ({remote_refs}) 與本地 refs ({local_refs}) 不符"
        )

    # 2. 重新尋找主 manifest
    manifest_name = f"GITMANIFEST--{state.repo_uuid}"
    m_files = drive.find_by_name(listing_before.prefix_folder_id, manifest_name)
    if len(m_files) != 1:
        raise MismatchError(f"push 後遠端主 manifest 數量異常 ({len(m_files)} != 1)")

    m_data = drive.download_bytes(m_files[0].id, max_bytes=1024 * 1024)
    new_manifest_sha = hashlib.sha256(m_data).hexdigest().lower()
    parsed_manifest = parse_manifest(m_data, repo_uuid=state.repo_uuid)

    # 3. 檢查新增之 active bundle
    existing_active = set(state.active_bundles)
    listing_before_names = {f.name for f in listing_before.files}

    new_bundles = [b for b in parsed_manifest.active if b not in existing_active]
    for b_name in new_bundles:
        if b_name in listing_before_names:
            raise MismatchError(f"新增之 active bundle '{b_name}' 已存在於 push 前的 listing 中")

        b_files = drive.find_by_name(listing_before.prefix_folder_id, b_name)
        if not b_files:
            raise MismatchError(f"找不到新增之 active bundle 檔案: {b_name}")

        bf = b_files[0]
        if bf.created_time < push_started_at:
            raise MismatchError(
                f"新增之 bundle '{b_name}' created_time ({bf.created_time}) 早於 push 開始時間 ({push_started_at})"
            )

    # 4. removed 包含關係檢查
    if not parsed_manifest.removed.issuperset(state.removed_bundles):
        missing_removed = state.removed_bundles - parsed_manifest.removed
        raise MismatchError(f"新 manifest 遺失了既有已移除 bundle 紀錄: {missing_removed}")

    newly_removed = parsed_manifest.removed - state.removed_bundles
    if not newly_removed.issubset(existing_active):
        invalid_removed = newly_removed - existing_active
        raise MismatchError(f"新移除之 bundle 以前並非 active bundle: {invalid_removed}")

    # 5. 下載 active bundle 並依序重放
    temp_dir = Path(tempfile.mkdtemp(prefix="aistorage_verify_bundles_", dir=str(workdir)))
    try:
        downloaded_bundles: list[Path] = []
        for idx, b_name in enumerate(parsed_manifest.active):
            b_info = parse_bundle_name(b_name)
            if not b_info or b_info.repo_uuid != state.repo_uuid:
                raise MismatchError(f"bundle 檔名不合法: {b_name}")

            dest = temp_dir / b_name
            if not dest.is_file():
                found = drive.find_by_name(listing_before.prefix_folder_id, b_name)
                if not found:
                    raise MismatchError(f"遠端找不到 active bundle: {b_name}")
                drive.download(found[0].id, dest, max_bytes=256 * 1024 * 1024)
            downloaded_bundles.append(dest)

        replayed = replay_refs(
            downloaded_bundles,
            workdir=workdir,
            repo_uuid=state.repo_uuid,
        )
        if replayed != local_refs:
            raise MismatchError(
                f"push 後 bundle 重放 refs ({replayed}) 與 local_refs ({local_refs}) 不符"
            )

        return PushVerification(
            new_manifest_sha256=new_manifest_sha,
            active=parsed_manifest.active,
            removed=parsed_manifest.removed,
        )
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
