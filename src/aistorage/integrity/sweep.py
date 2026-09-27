"""AiStorage 上層同名檢查與清掃模組。

依據規格：
- docs/impl/group3-modules.md 第 3.4 節
- design.md D2（第 4 步清掃、純函式計算計畫、只看 metadata、讀不到就中止不移動）
- ADR 0008（以偵測與隔離保證真本）
- review-1.4f3 H1/H2/H3（讀不到不移動、manifest 區分角色、annex key 來自釘選值）
- review-1.4f5 H1（active vs removed bundle 解析與處置）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import re
import tempfile

from aistorage.annex.manifest import parse_bundle_name
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import AbortRun, MismatchError, ReadError, TooLarge
from aistorage.integrity.pin import PinState
from aistorage.integrity.settle import RepoListing

_ANNEX_KEY_PATTERN = re.compile(r"^SHA256E-s(\d+)--([0-9a-f]{64})(\..*)?$")


@dataclass(frozen=True)
class PrefixLevel:
    """前綴路徑層級定義（用於逐層檢查同名資料夾）。"""

    parent_id: str
    name: str
    expected_id: str


def check_parents(levels: list[PrefixLevel], drive: DriveClient) -> list[DriveFile]:
    """檢查前綴路徑各層級之同名資料夾。

    - expected_id 不在清單中 -> 拋出 MismatchError（中止）。
    - 讀取失敗 -> 拋出 ReadError（中止）。
    - 回傳應隔離之多餘同名資料夾清單。
    """
    to_quarantine: list[DriveFile] = []
    for level in levels:
        children = drive.list_children(level.parent_id)
        matching = [c for c in children if c.is_folder and c.name == level.name]
        has_expected = any(c.id == level.expected_id for c in matching)
        if not has_expected:
            raise MismatchError(
                f"層級 '{level.name}' (parent={level.parent_id}) 找不到預期 ID 之資料夾 ({level.expected_id})"
            )
        for c in matching:
            if c.id != level.expected_id:
                to_quarantine.append(c)
    return to_quarantine


class Disposition(Enum):
    """清掃處置決策。"""

    KEEP = "keep"                               # 保留
    QUARANTINE = "quarantine"                   # 移動至隔離資料夾
    GC = "gc"                                   # 已移除 bundle，保留至第 11 步 GC 回收
    NEED_CONTENT_CHECK = "need_content_check"   # 缺少 sha256，需下載計算雜湊後判定


@dataclass(frozen=True)
class SweepDecision:
    """單一檔案或資料夾之清掃判定決策。"""

    file: DriveFile
    disposition: Disposition
    reason: str


def plan_sweep(
    listing: RepoListing,
    state: PinState,
    *,
    repo_uuid: str,
) -> list[SweepDecision]:
    """計算清掃計畫（純函式，不碰網路與 Drive）。

    規則（design D2、review-1.4f3 H2/H3、review-1.4f5 H1）：
    - 非 .bak 的 GITMANIFEST：只 KEEP 一個（sha256 == state.manifest_sha256）；其他 QUARANTINE。
    - .bak：sha256 ∈ {state.manifest_sha256, state.prev_manifest_sha256} 的保留一個；其他 QUARANTINE。
    - GITBUNDLE：名稱 ∈ active 而且 sha256 == 名稱內嵌雜湊且大小相符 -> KEEP（重複者僅留一個）；
                名稱 ∈ removed -> GC；
                其他 -> QUARANTINE。
    - annex 物件（SHA256E-s<N>--<sha>…）：key ∈ state.annex_keys 而且 sha256 相符 -> KEEP（重複者僅留一個）；其他 QUARANTINE。
    - sha256 是 None 的檔 -> NEED_CONTENT_CHECK。
    - 子資料夾 -> QUARANTINE（整個子樹）。
    - 其他名稱 -> QUARANTINE。
    """
    decisions: list[SweepDecision] = []

    # 1. 子資料夾一律隔離（repo layout 是平的）
    for subfolder in listing.subfolders:
        decisions.append(
            SweepDecision(
                file=subfolder,
                disposition=Disposition.QUARANTINE,
                reason="repo 資料夾下不允許存在子資料夾",
            )
        )

    # 追蹤已保留之唯一實體
    kept_main_manifest = False
    kept_bak_manifest = False
    kept_bundles: set[str] = set()
    kept_annex_keys: set[str] = set()

    main_name = f"GITMANIFEST--{repo_uuid}"
    bak_name = f"GITMANIFEST--{repo_uuid}.bak"

    allowed_bak_hashes = {state.manifest_sha256.lower()}
    if state.prev_manifest_sha256:
        allowed_bak_hashes.add(state.prev_manifest_sha256.lower())

    for f in listing.files:
        # sha256 缺失時標記 NEED_CONTENT_CHECK
        if f.sha256 is None:
            decisions.append(
                SweepDecision(
                    file=f,
                    disposition=Disposition.NEED_CONTENT_CHECK,
                    reason="缺少 sha256 雜湊，需下載驗證",
                )
            )
            continue

        f_sha = f.sha256.lower()

        # (A) 主 GITMANIFEST
        if f.name == main_name:
            if f_sha == state.manifest_sha256.lower():
                if not kept_main_manifest:
                    kept_main_manifest = True
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.KEEP,
                            reason="符合當前正式 manifest 內容雜湊",
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="重複之正式 manifest",
                        )
                    )
            else:
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason=f"主 manifest 雜湊 ({f_sha}) 與釘選值 ({state.manifest_sha256}) 不符",
                    )
                )
            continue

        # (B) 備份 GITMANIFEST.bak
        if f.name == bak_name:
            if f_sha in allowed_bak_hashes:
                if not kept_bak_manifest:
                    kept_bak_manifest = True
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.KEEP,
                            reason="符合備份 manifest 允許之內容雜湊",
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="重複之備份 manifest",
                        )
                    )
            else:
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason=f".bak manifest 雜湊 ({f_sha}) 不在允許之釘選集合內",
                    )
                )
            continue

        # (C) GITBUNDLE
        b_info = parse_bundle_name(f.name)
        if b_info and b_info.repo_uuid == repo_uuid:
            if f.name in state.active_bundles:
                # 驗證雜湊與大小
                size_matches = (f.size is None or f.size == b_info.size)
                sha_matches = (f_sha == b_info.sha256.lower())
                if sha_matches and size_matches:
                    if f.name not in kept_bundles:
                        kept_bundles.add(f.name)
                        decisions.append(
                            SweepDecision(
                                file=f,
                                disposition=Disposition.KEEP,
                                reason="符合 active bundle 宣告與釘選",
                            )
                        )
                    else:
                        decisions.append(
                            SweepDecision(
                                file=f,
                                disposition=Disposition.QUARANTINE,
                                reason="重複之 active bundle",
                            )
                        )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="bundle 雜湊或大小與檔名宣告不符",
                        )
                    )
            elif f.name in state.removed_bundles:
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.GC,
                        reason="屬於已移除 (removed) 清單，留待 GC 回收",
                    )
                )
            else:
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason="非 active 亦非 removed 清單中之 bundle",
                    )
                )
            continue

        # (D) annex 物件（SHA256E-s<N>--<sha>…）
        annex_match = _ANNEX_KEY_PATTERN.match(f.name)
        if annex_match:
            key_sha = annex_match.group(2).lower()
            if f.name in state.annex_keys and f_sha == key_sha:
                if f.name not in kept_annex_keys:
                    kept_annex_keys.add(f.name)
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.KEEP,
                            reason="符合釘選之 annex key 與雜湊",
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="重複之 annex 物件",
                        )
                    )
            else:
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason="annex 物件不在釘選 key 集合或雜湊不符",
                    )
                )
            continue

        # (E) 其他不認得的檔案名稱一律隔離
        decisions.append(
            SweepDecision(
                file=f,
                disposition=Disposition.QUARANTINE,
                reason=f"未知或不合法的檔案名稱: '{f.name}'",
            )
        )

    return decisions


def resolve_content_checks(
    decisions: list[SweepDecision],
    drive: DriveClient,
    cache: dict[str, str],
    state: PinState,
    *,
    repo_uuid: str,
) -> list[SweepDecision]:
    """針對缺少 sha256Checksum 的項目下載計算雜湊並更新判定。"""
    resolved_decisions: list[SweepDecision] = []

    for d in decisions:
        if d.disposition != Disposition.NEED_CONTENT_CHECK:
            resolved_decisions.append(d)
            continue

        fid = d.file.id
        if fid in cache:
            calc_sha = cache[fid]
        else:
            try:
                data = drive.download_bytes(fid, max_bytes=32 * 1024 * 1024)
                calc_sha = hashlib.sha256(data).hexdigest().lower()
            except TooLarge:
                with tempfile.NamedTemporaryFile() as tf:
                    p = Path(tf.name)
                    drive.download(fid, p, max_bytes=1024 * 1024 * 1024)
                    hasher = hashlib.sha256()
                    with open(p, "rb") as f:
                        while chunk := f.read(65536):
                            hasher.update(chunk)
                    calc_sha = hasher.hexdigest().lower()
            cache[fid] = calc_sha

        # 構造帶有 sha256 的 DriveFile 重新判定
        updated_file = DriveFile(
            id=d.file.id,
            name=d.file.name,
            mime_type=d.file.mime_type,
            parents=d.file.parents,
            size=d.file.size,
            sha256=calc_sha,
            md5=d.file.md5,
            created_time=d.file.created_time,
            modified_time=d.file.modified_time,
            trashed=d.file.trashed,
        )
        single_listing = RepoListing(
            prefix_folder_id=d.file.parents[0] if d.file.parents else "",
            files=(updated_file,),
            subfolders=(),
        )
        sub_decisions = plan_sweep(single_listing, state, repo_uuid=repo_uuid)
        resolved_decisions.append(sub_decisions[0])

    return resolved_decisions


def apply_sweep(
    decisions: list[SweepDecision],
    drive: DriveClient,
    *,
    prefix_folder_id: str,
    quarantine_folder_id: str,
    dry_run: bool = False,
) -> int:
    """執行清掃決策（只執行 QUARANTINE 之搬移）。

    任何寫入失敗立即拋出 AbortRun（終止這一輪）；回傳移動的數量。
    """
    quarantine_items = [d for d in decisions if d.disposition == Disposition.QUARANTINE]
    if dry_run:
        return len(quarantine_items)

    moved_count = 0
    for d in quarantine_items:
        try:
            drive.move(
                d.file.id,
                from_parent=prefix_folder_id,
                to_parent=quarantine_folder_id,
            )
            moved_count += 1
        except Exception as e:
            raise AbortRun(
                "sweep",
                "move_failed",
                f"隔離檔案移動失敗 ({d.file.name}, id={d.file.id}): {e}",
            ) from e

    return moved_count
