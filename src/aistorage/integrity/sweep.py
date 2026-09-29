"""AiStorage 上層同名檢查與清掃模組。

依據規格：
- docs/impl/group3-modules.md 第 3.4 節
- design.md D2（第 4 步清掃、純函式計算計畫、只看 metadata、讀不到就中止不移動）
- ADR 0008（以偵測與隔離保證真本）
- review-1.4f3 H1/H2/H3（讀不到不移動、manifest 區分角色、annex key 來自釘選值）
- review-1.4f5 H1（active vs removed bundle 解析與處置）
- review-g3c H3（按日期分子資料夾 quarantine/<YYYY-MM-DD>/ 隔離）
- review-g3c H4（提供 run_settle_and_sweep 組合函式供測試與提交流程使用）
- review-g3c M7（SweepDecision 攜帶 from_parent，隔離搬移使用各自的 parent）
- review-g3c M8（resolve_content_checks 快取使用複合 key，解析後全量重跑 plan_sweep）
- review-g3c M9（提供 plan_readview_sweep 介面 stub）
- review-g3c L（annex 物件比對 s<N> 與 f.size；大檔下載可指定 workdir）
- impl1：GITMANIFEST 與 bundle 在沒有「它是注入物」的證據之前一律不隔離
  （HOLD／NEED_MANIFEST_CHECK），理由見 plan_sweep 的說明
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import re
import tempfile
from typing import Any

from aistorage.annex.manifest import parse_bundle_name, parse_manifest
from aistorage.clock import Clock, FixedClock
from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient, DriveFile
from aistorage.errors import AbortRun, MismatchError, ReadError, TooLarge
from aistorage.integrity.pin import PinPending, PinState, PinStore
from aistorage.integrity.settle import RepoListing, SettleOutcome, settle

_ANNEX_KEY_PATTERN = re.compile(r"^SHA256E-s(\d+)--([0-9a-f]{64})(\..*)?$")

#: manifest 檔案的大小上限：git-remote-annex 的 manifest 只是 bundle 名字清單，
#: 遠超過這個大小就不可能是 manifest（也就是說，它是注入物）。
MANIFEST_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PrefixLevel:
    """前綴路徑層級定義（用於逐層檢查同名資料夾）。"""

    parent_id: str
    name: str
    expected_id: str


class Disposition(Enum):
    """清掃處置決策。"""

    KEEP = "keep"                               # 保留
    QUARANTINE = "quarantine"                   # 移動至隔離資料夾
    GC = "gc"                                   # 已移除 bundle，保留至第 11 步 GC 回收
    NEED_CONTENT_CHECK = "need_content_check"   # 缺少 sha256 或 size，需下載計算雜湊後判定
    NEED_MANIFEST_CHECK = "need_manifest_check" # manifest 雜湊不在釘選值內，需讀內容才能判斷
    HOLD = "hold"                               # 已排除是注入物，但也不是釘選值記載的那份：不動


@dataclass(frozen=True)
class SweepDecision:
    """單一檔案或資料夾之清掃判定決策。"""

    file: DriveFile
    disposition: Disposition
    reason: str
    from_parent: str = ""

    @property
    def id(self) -> str:
        """相容性屬性，直接存取目標檔案或資料夾 ID。"""
        return self.file.id


def check_parents(levels: list[PrefixLevel], drive: DriveClient) -> list[SweepDecision]:
    """檢查前綴路徑各層級之同名資料夾。

    - expected_id 不在清單中 -> 拋出 MismatchError（中止）。
    - 讀取失敗 -> 拋出 ReadError（中止）。
    - 回傳應隔離之多餘同名資料夾決策清單（M7: 附帶 from_parent=level.parent_id）。
    """
    to_quarantine: list[SweepDecision] = []
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
                to_quarantine.append(
                    SweepDecision(
                        file=c,
                        disposition=Disposition.QUARANTINE,
                        reason=f"上層同名多餘資料夾 (name={c.name})",
                        from_parent=level.parent_id,
                    )
                )
    return to_quarantine


def plan_sweep(
    listing: RepoListing,
    state: PinState,
    *,
    repo_uuid: str,
    prefix_folder_id: str | None = None,
    manifest_verdicts: dict[str, tuple[Disposition, str]] | None = None,
) -> list[SweepDecision]:
    """計算清掃計畫（純函式，不碰網路與 Drive）。

    規則（design D2、review-1.4f3 H2/H3、review-1.4f5 H1、review-g3c L、impl1）：

    - 非 .bak 的 GITMANIFEST：只 KEEP 一個（sha256 == state.manifest_sha256）；其他 QUARANTINE。
    - .bak：sha256 ∈ {state.manifest_sha256, state.prev_manifest_sha256} 的保留一個；其他 QUARANTINE。
    - GITBUNDLE：名稱 ∈ active 而且 sha256 == 名稱內嵌雜湊且大小相符 -> KEEP（重複者僅留一個）；
                名稱 ∈ removed -> GC；
                其他 -> QUARANTINE。
    - annex 物件（SHA256E-s<N>--<sha>…）：key ∈ state.annex_keys 而且 sha256 相符且大小相符 -> KEEP（重複者僅留一個）；
      **內容與 key 自稱值相符但不在釘選值裡 -> HOLD**；sha256／大小與 key 內嵌值不符 -> QUARANTINE。
    - sha256 或 size 缺失 -> NEED_CONTENT_CHECK。
    - 子資料夾 -> QUARANTINE（整個子樹）。
    - 其他名稱 -> QUARANTINE。

    **impl1：GITMANIFEST 與 bundle 沒有「是注入物」的證據之前一律不隔離。**

    原規則是「內容雜湊不在釘選值裡 → QUARANTINE」。但釘選值落後遠端是完全正常
    的中間狀態：某一輪 push 成功了、第 11 步的驗證卻沒過（Drive 列表比 push 慢、
    連線中斷），釘選值就停在上一版。下一輪的清掃若照舊把那份 manifest 與它
    引用的新 bundle 當注入物搬走，`git clone` 就再也找不到 manifest
    （`git-remote-annex: No git repository found in this remote`），而且沒有任何
    一輪能自己回來——這是真本被提交流程自己消滅，不是外來攻擊。

    所以改成「先證明是注入物，隔離才允許發生」：

    - manifest 內容雜湊正是**已退役的上一版**（state.prev_manifest_sha256）：
      證據充分（就是那份該退位的內容）→ QUARANTINE。
    - manifest 內容雜湊既不是正式值也不是上一版：**無法從釘選值判斷**，標成
      NEED_MANIFEST_CHECK，交給 `resolve_manifest_evidence` 讀內容——能解析成
      這個 repo 的 manifest、而且它列的每個 active bundle 都在前綴裡（名稱與
      雜湊都對得上），就是真的 → HOLD；解析不了或引用了不存在的 bundle，才算
      注入物 → QUARANTINE。
    - bundle 自己宣告的 sha256/size 與實際內容對不上：證據充分（檔名在騙人）
      → QUARANTINE。對得上但不在釘選值的 active/removed 內：可能屬於尚未轉正
      的新世代 → HOLD。

    `manifest_verdicts`（file id → (處置, 理由)）由 `resolve_manifest_evidence`
    帶進來重算，維持「整份重判」而不是逐項補丁。
    """
    decisions: list[SweepDecision] = []
    default_from_parent = prefix_folder_id or listing.prefix_folder_id
    verdicts = manifest_verdicts or {}

    # 1. 子資料夾一律隔離（repo layout 是平的）
    for subfolder in listing.subfolders:
        decisions.append(
            SweepDecision(
                file=subfolder,
                disposition=Disposition.QUARANTINE,
                reason="repo 資料夾下不允許存在子資料夾",
                from_parent=default_from_parent,
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
                    from_parent=default_from_parent,
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
                            from_parent=default_from_parent,
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="重複之正式 manifest",
                            from_parent=default_from_parent,
                        )
                    )
            elif state.prev_manifest_sha256 and f_sha == state.prev_manifest_sha256.lower():
                # 證據充分：內容雜湊正是釘選值記載「已經退位」的上一版，而真本
                # 該有的是正式值。這份一定不是遠端現在在用的 manifest。
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason=f"上一版 manifest 冒充 (內容雜湊 {f_sha} 正是已退役的上一版)",
                        from_parent=default_from_parent,
                    )
                )
            else:
                disposition, reason = verdicts.get(
                    f.id,
                    (
                        Disposition.NEED_MANIFEST_CHECK,
                        f"內容雜湊 ({f_sha}) 不在釘選值內，無法證明是注入物，需讀內容驗證",
                    ),
                )
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=disposition,
                        reason=reason,
                        from_parent=default_from_parent,
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
                            from_parent=default_from_parent,
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="重複之備份 manifest",
                            from_parent=default_from_parent,
                        )
                    )
            else:
                disposition, reason = verdicts.get(
                    f.id,
                    (
                        Disposition.NEED_MANIFEST_CHECK,
                        f".bak 內容雜湊 ({f_sha}) 不在允許之釘選集合內，"
                        "無法證明是注入物，需讀內容驗證",
                    ),
                )
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=disposition,
                        reason=reason,
                        from_parent=default_from_parent,
                    )
                )
            continue

        # (C) GITBUNDLE
        b_info = parse_bundle_name(f.name)
        if b_info and b_info.repo_uuid == repo_uuid:
            if f.name in state.active_bundles:
                if f.size is None:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.NEED_CONTENT_CHECK,
                            reason="active bundle 缺少 size 資訊，需下載確認",
                            from_parent=default_from_parent,
                        )
                    )
                    continue

                size_matches = (f.size == b_info.size)
                sha_matches = (f_sha == b_info.sha256.lower())
                if sha_matches and size_matches:
                    if f.name not in kept_bundles:
                        kept_bundles.add(f.name)
                        decisions.append(
                            SweepDecision(
                                file=f,
                                disposition=Disposition.KEEP,
                                reason="符合 active bundle 宣告與釘選",
                                from_parent=default_from_parent,
                            )
                        )
                    else:
                        decisions.append(
                            SweepDecision(
                                file=f,
                                disposition=Disposition.QUARANTINE,
                                reason="重複之 active bundle",
                                from_parent=default_from_parent,
                            )
                        )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="bundle 雜湊或大小與檔名宣告不符",
                            from_parent=default_from_parent,
                        )
                    )
            elif f.name in state.removed_bundles:
                size_matches = (f.size is None or f.size == b_info.size)
                sha_matches = (f_sha == b_info.sha256.lower())
                if sha_matches and size_matches:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.GC,
                            reason="屬於已移除 (removed) 清單，留待 GC 回收",
                            from_parent=default_from_parent,
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="removed bundle 雜湊或大小與檔名宣告不符",
                            from_parent=default_from_parent,
                        )
                    )
            else:
                # impl1：不在釘選值的 active/removed 裡不等於它是注入物——釘選值
                # 落後遠端時，這正是「剛 push 上去、還沒轉正」的那一份。
                # 檔名自稱的 sha256/size 與實際內容對不上才是證據（檔名在騙人）。
                self_consistent = (
                    f.size == b_info.size and f_sha == b_info.sha256.lower()
                )
                if self_consistent:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.HOLD,
                            reason=(
                                "內容與檔名宣告相符，但不在釘選值的 active/removed 內"
                                "（可能是尚未轉正的新世代，或上一輪 push 後驗證未完成的產物）："
                                "不搬移，等釘選值追上或管理者處理"
                            ),
                            from_parent=default_from_parent,
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="bundle 雜湊或大小與檔名宣告不符（檔名宣告與內容不符）",
                            from_parent=default_from_parent,
                        )
                    )
            continue

        # (D) annex 物件（SHA256E-s<N>--<sha>…）
        # 依 2.x 設定固定使用 SHA256E backend
        annex_match = _ANNEX_KEY_PATTERN.match(f.name)
        if annex_match:
            key_size = int(annex_match.group(1))
            key_sha = annex_match.group(2).lower()
            if f.name in state.annex_keys and f_sha == key_sha:
                if f.size is None:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.NEED_CONTENT_CHECK,
                            reason="annex 物件缺少 size 資訊，需下載確認",
                            from_parent=default_from_parent,
                        )
                    )
                    continue

                if f.size == key_size:
                    if f.name not in kept_annex_keys:
                        kept_annex_keys.add(f.name)
                        decisions.append(
                            SweepDecision(
                                file=f,
                                disposition=Disposition.KEEP,
                                reason="符合釘選之 annex key、大小與雜湊",
                                from_parent=default_from_parent,
                            )
                        )
                    else:
                        decisions.append(
                            SweepDecision(
                                file=f,
                                disposition=Disposition.QUARANTINE,
                                reason="重複之 annex 物件",
                                from_parent=default_from_parent,
                            )
                        )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason=f"annex 物件大小 ({f.size}) 與 key 宣告 ({key_size}) 不符",
                            from_parent=default_from_parent,
                        )
                    )
            else:
                # impl1：與 manifest／bundle 同一條規則。key 自稱的 sha256 與大小
                # 與實際內容相符，卻不在釘選值的 key 集合裡 —— 這正是「這一輪剛
                # push 上去、釘選值還沒轉正」的那一個（impl1 現場被隔離的就是
                # 這種）。沒有雜湊／大小不符的證據之前不搬。
                if f.size == key_size and f_sha == key_sha:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.HOLD,
                            reason=(
                                "內容與 key 內嵌的 sha256／大小相符，但不在釘選值的 "
                                "key 集合內（可能是尚未轉正的新世代）：不搬移，"
                                "等釘選值追上或管理者處理"
                            ),
                            from_parent=default_from_parent,
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason=(
                                f"annex 物件的 sha256／大小與 key 內嵌值不符"
                                f"（Drive: {f_sha}／{f.size}，key: {key_sha}／{key_size}）"
                            ),
                            from_parent=default_from_parent,
                        )
                    )
            continue

        # (E) 其他不認得的檔案名稱一律隔離
        decisions.append(
            SweepDecision(
                file=f,
                disposition=Disposition.QUARANTINE,
                reason=f"未知或不合法的檔案名稱: '{f.name}'",
                from_parent=default_from_parent,
            )
        )

    return decisions


def resolve_content_checks(
    decisions: list[SweepDecision],
    drive: DriveClient,
    cache: dict[Any, Any],
    state: PinState,
    *,
    repo_uuid: str,
    listing: RepoListing | None = None,
    prefix_folder_id: str | None = None,
    workdir: Path | None = None,
    manifest_verdicts: dict[str, tuple[Disposition, str]] | None = None,
) -> list[SweepDecision]:
    """針對缺少 sha256Checksum 或 size 的項目下載計算雜湊並整份重新判定（M8, L）。

    - 快取 key 使用複合值 (file_id, size, md5 或 modified_time) 防止原地改寫偽造。
    - 大檔下載可指定 workdir。
    - 將解析後的檔案更新回完整 listing，重新整份執行 plan_sweep 以保持全局狀態一致性。
    - `manifest_verdicts` 會一併帶進重判（impl1：manifest 內容驗證的結果不能
      在這一步被清掉）。
    """
    if not any(d.disposition == Disposition.NEED_CONTENT_CHECK for d in decisions):
        return decisions

    updated_files_by_id: dict[str, DriveFile] = {}

    for d in decisions:
        if d.disposition != Disposition.NEED_CONTENT_CHECK:
            continue

        f = d.file
        fid = f.id
        cache_key = (fid, f.size, f.md5 or f.modified_time)

        if cache_key in cache:
            cached_val = cache[cache_key]
            if isinstance(cached_val, tuple):
                calc_sha, calc_size = cached_val
            else:
                calc_sha = cached_val
                calc_size = f.size if f.size is not None else 0
        else:
            try:
                data = drive.download_bytes(fid, max_bytes=32 * 1024 * 1024)
                calc_sha = hashlib.sha256(data).hexdigest().lower()
                calc_size = len(data)
            except TooLarge:
                with tempfile.NamedTemporaryFile(dir=str(workdir) if workdir else None) as tf:
                    p = Path(tf.name)
                    drive.download(fid, p, max_bytes=1024 * 1024 * 1024)
                    hasher = hashlib.sha256()
                    calc_size = 0
                    with open(p, "rb") as fp:
                        while chunk := fp.read(65536):
                            hasher.update(chunk)
                            calc_size += len(chunk)
                    calc_sha = hasher.hexdigest().lower()

            cache[cache_key] = (calc_sha, calc_size)

        # 構造帶有完整 sha256 與 size 的 DriveFile
        updated_file = DriveFile(
            id=f.id,
            name=f.name,
            mime_type=f.mime_type,
            parents=f.parents,
            size=f.size if f.size is not None else calc_size,
            sha256=calc_sha,
            md5=f.md5,
            created_time=f.created_time,
            modified_time=f.modified_time,
            trashed=f.trashed,
        )
        updated_files_by_id[fid] = updated_file

    # 替換回完整 listing 並重新判定 (M8)
    if listing is None:
        all_files = [
            updated_files_by_id.get(d.file.id, d.file)
            for d in decisions
            if not d.file.is_folder
        ]
        all_subfolders = [d.file for d in decisions if d.file.is_folder]
        full_listing = RepoListing(
            prefix_folder_id=prefix_folder_id or (decisions[0].from_parent if decisions else ""),
            files=tuple(all_files),
            subfolders=tuple(all_subfolders),
        )
    else:
        new_files = tuple(
            updated_files_by_id.get(f.id, f) for f in listing.files
        )
        full_listing = RepoListing(
            prefix_folder_id=listing.prefix_folder_id,
            files=new_files,
            subfolders=listing.subfolders,
        )

    return plan_sweep(
        full_listing,
        state,
        repo_uuid=repo_uuid,
        prefix_folder_id=prefix_folder_id or full_listing.prefix_folder_id,
        manifest_verdicts=manifest_verdicts,
    )


def resolve_manifest_evidence(
    decisions: list[SweepDecision],
    drive: DriveClient,
    cache: dict[Any, Any],
    state: PinState,
    *,
    repo_uuid: str,
    listing: RepoListing | None = None,
    prefix_folder_id: str | None = None,
) -> list[SweepDecision]:
    """讀 manifest 內容，判定「是注入物」還是「釘選值還沒追上的新世代」（impl1）。

    只處理 `plan_sweep` 標成 NEED_MANIFEST_CHECK 的檔（內容雜湊既不是正式值也
    不是已退役的上一版）。判斷需要兩件事，兩件都成立才算真的：

    1. 內容能 `parse_manifest(repo_uuid=...)`——不是這個 repo 的 manifest 就是注入物；
    2. 它列出的每個 active bundle 都在前綴裡，而且 Drive  上的 sha256 與檔名
       內嵌雜湊相符——引用了不存在的 bundle 就是注入物。

    兩件都成立 → HOLD（不搬移，等釘選值追上或管理者 `init-pin`）。任何一件不成立
    → QUARANTINE。判定完之後整份重判（和 resolve_content_checks 同一個理由：
    逐項補丁會讓全域狀態不一致）。

    讀不到（ReadError）原樣往上拋：寧可中止，也不要在讀不到證據時搬走真本。
    """
    if not any(d.disposition == Disposition.NEED_MANIFEST_CHECK for d in decisions):
        return decisions

    available = listing.files if listing is not None else ()
    verdicts: dict[str, tuple[Disposition, str]] = {}

    for d in decisions:
        if d.disposition != Disposition.NEED_MANIFEST_CHECK:
            continue
        f = d.file
        cache_key = ("manifest-bytes", f.id, f.size, f.md5 or f.modified_time)
        data = cache.get(cache_key)
        if data is None:
            try:
                data = drive.download_bytes(f.id, max_bytes=MANIFEST_MAX_BYTES)
            except TooLarge as e:
                # manifest 只是一份 bundle 名字清單；大到離譜就不可能是 manifest。
                verdicts[f.id] = (
                    Disposition.QUARANTINE,
                    f"檔名是 manifest 但內容超過 {MANIFEST_MAX_BYTES} bytes，不可能是 manifest",
                )
                continue
            cache[cache_key] = data

        try:
            parsed = parse_manifest(data, repo_uuid=repo_uuid)
        except MismatchError as e:
            verdicts[f.id] = (
                Disposition.QUARANTINE,
                f"檔名是 manifest 但內容解析失敗（不是這個 repo 的 manifest）: {e}",
            )
            continue

        missing: list[str] = []
        for b_name in parsed.active:
            b_info = parse_bundle_name(b_name)
            if b_info is None:
                missing.append(b_name)
                continue
            if not any(
                x.name == b_name and x.sha256 is not None
                and x.sha256.lower() == b_info.sha256
                for x in available
            ):
                missing.append(b_name)
        if missing:
            verdicts[f.id] = (
                Disposition.QUARANTINE,
                f"manifest 引用的 bundle 在前綴裡找不到（{len(missing)} 個）：{missing[:2]}",
            )
            continue

        verdicts[f.id] = (
            Disposition.HOLD,
            "內容是這個 repo 的 manifest，且它引用的 bundle 都在前綴裡："
            "無法證明是注入物（多半是釘選值尚未轉正的新世代），不搬移",
        )

    if listing is None:
        all_files = [
            d.file for d in decisions if not d.file.is_folder
        ]
        all_subfolders = [d.file for d in decisions if d.file.is_folder]
        full_listing = RepoListing(
            prefix_folder_id=prefix_folder_id
            or (decisions[0].from_parent if decisions else ""),
            files=tuple(all_files),
            subfolders=tuple(all_subfolders),
        )
    else:
        full_listing = listing

    return plan_sweep(
        full_listing,
        state,
        repo_uuid=repo_uuid,
        prefix_folder_id=prefix_folder_id or full_listing.prefix_folder_id,
        manifest_verdicts=verdicts,
    )


def apply_sweep(
    decisions: list[SweepDecision],
    drive: DriveClient,
    *,
    quarantine_folder_id: str,
    clock: Clock | None = None,
    prefix_folder_id: str | None = None,
    dry_run: bool = False,
) -> int:
    """執行清掃決策（只執行 QUARANTINE 之搬移）。

    規則（review-g3c H3, M7、impl1）：
    - 移至日期子資料夾 quarantine/<YYYY-MM-DD>/。
    - 逐項使用各自的 from_parent 搬移。
    - 任何寫入失敗立即拋出 AbortRun（終止這一輪）；回傳移動的數量。
    - **只有 QUARANTINE 會被搬**。HOLD（已排除是注入物、但釘選值還沒追上）
      與 NEED_* 都是不動的——它們留在前綴裡是刻意的，不是漏處理。
    """
    quarantine_items = [d for d in decisions if d.disposition == Disposition.QUARANTINE]
    if dry_run:
        return len(quarantine_items)

    if not quarantine_items:
        return 0

    # 1. 建立或取得日期子資料夾 (H3)
    folder_name = (
        clock.now().strftime("%Y-%m-%d")
        if clock
        else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    matching_folders = [
        c for c in drive.list_children(quarantine_folder_id)
        if c.is_folder and c.name == folder_name
    ]
    if len(matching_folders) > 1:
        raise AbortRun(
            "sweep",
            "multiple_quarantine_folders",
            f"隔離資料夾中存在多個同名的日期子資料夾: '{folder_name}'",
        )
    elif len(matching_folders) == 1:
        target_folder_id = matching_folders[0].id
    else:
        created = drive.create(
            quarantine_folder_id,
            folder_name,
            b"",
            mime_type=GOOGLE_FOLDER_MIME,
        )
        target_folder_id = created.id if isinstance(created, DriveFile) else str(created)

    # 2. 逐一搬移
    moved_count = 0
    for d in quarantine_items:
        from_parent = d.from_parent or prefix_folder_id
        if not from_parent and d.file.parents:
            from_parent = d.file.parents[0]

        try:
            drive.move(
                d.file.id,
                from_parent=from_parent,
                to_parent=target_folder_id,
            )
            moved_count += 1
        except Exception as e:
            raise AbortRun(
                "sweep",
                "move_failed",
                f"隔離檔案移動失敗 ({d.file.name}, id={d.file.id}): {e}",
            ) from e

    return moved_count


def plan_readview_sweep(
    listing: RepoListing,
    trusted_file_ids: set[str] | frozenset[str],
    *,
    readview_folder_id: str,
) -> list[SweepDecision]:
    """計算讀取視圖資料夾之清掃計畫（第 4 組介面 stub，review-g3c M9）。

    可信集合為讀取視圖 manifest 列出的 file id。
    凡不在 trusted_file_ids 者一律 QUARANTINE。
    """
    decisions: list[SweepDecision] = []
    for subfolder in listing.subfolders:
        decisions.append(
            SweepDecision(
                file=subfolder,
                disposition=Disposition.QUARANTINE,
                reason="讀取視圖資料夾下不允許存在未授權子資料夾",
                from_parent=readview_folder_id,
            )
        )
    for f in listing.files:
        if f.id in trusted_file_ids:
            decisions.append(
                SweepDecision(
                    file=f,
                    disposition=Disposition.KEEP,
                    reason="屬於讀取視圖可信檔案清單",
                    from_parent=readview_folder_id,
                )
            )
        else:
            decisions.append(
                SweepDecision(
                    file=f,
                    disposition=Disposition.QUARANTINE,
                    reason=f"檔案 (id={f.id}, name={f.name}) 不在讀取視圖可信清單中",
                    from_parent=readview_folder_id,
                )
            )
    return decisions


@dataclass(frozen=True)
class SettleAndSweepResult:
    """第 3〜4 步結合執行結果結構（H4）。"""

    settle_outcome: SettleOutcome
    state: PinState
    listing: RepoListing
    decisions: tuple[SweepDecision, ...]
    moved_count: int


def run_settle_and_sweep(
    state: PinState,
    pending: PinPending | None,
    *,
    drive: DriveClient,
    pins: PinStore,
    levels: list[PrefixLevel] | None = None,
    prefix_folder_id: str,
    quarantine_folder_id: str,
    repo_uuid: str,
    workdir: Path,
    clock: Clock | None = None,
    cache: dict[Any, Any] | None = None,
    dry_run: bool = False,
    readview_listing: RepoListing | None = None,
) -> SettleAndSweepResult:
    """第 3〜4 步組合函式（供提交流程與性質測試共用進入點，review-g3c H4）。

    順序：
    1. 列舉前綴資料夾（單次列舉供 settle 與 sweep 共用）
    2. 第 3 步：settle（結算待定釘選值，唯讀不移動 Drive 檔案）
    3. 第 4 步：check_parents（檢查上層同名資料夾）
    4. 第 4 步：plan_sweep（計算前綴資料夾檔案處置計畫）
    5. 第 4 步：resolve_content_checks（下載並重新計算缺少雜湊者）
    6. 第 4 步：resolve_manifest_evidence（讀 manifest 內容，分注入物與新世代）
    7. 第 4 步：apply_sweep（執行 QUARANTINE 之搬移）

    保證：
    - 任何步驟中發生 ReadError，立即終止，絕對不執行任何 Drive 寫入（WRITE_OPS）。
    """
    if cache is None:
        cache = {}

    # 1. 一次性列舉前綴資料夾
    children = drive.list_children(prefix_folder_id)
    files = tuple(c for c in children if not c.is_folder)
    subfolders = tuple(c for c in children if c.is_folder)
    listing = RepoListing(
        prefix_folder_id=prefix_folder_id,
        files=files,
        subfolders=subfolders,
    )

    # 2. 結算待定
    clock_obj = clock or FixedClock()
    settle_outcome, current_state = settle(
        state,
        pending,
        listing,
        drive,
        workdir=workdir,
        clock=clock_obj,
    )
    if not dry_run:
        if settle_outcome == SettleOutcome.PROMOTED:
            pins.promote(current_state)
        elif settle_outcome in (SettleOutcome.DROPPED, SettleOutcome.BAK_RECOVERY):
            pins.drop_pending(current_state.repo)

    # 3. 檢查上層同名資料夾 (M7)
    parent_decisions: list[SweepDecision] = []
    if levels:
        parent_decisions = check_parents(levels, drive)

    # 4. 計算清掃計畫
    sweep_decisions = plan_sweep(
        listing,
        current_state,
        repo_uuid=repo_uuid,
        prefix_folder_id=prefix_folder_id,
    )

    # 5. 解析內容檢查 (M8)
    sweep_decisions = resolve_content_checks(
        sweep_decisions,
        drive,
        cache,
        current_state,
        repo_uuid=repo_uuid,
        listing=listing,
        prefix_folder_id=prefix_folder_id,
        workdir=workdir,
    )

    # 5.4 manifest 內容驗證（impl1）：內容雜湊不在釘選值內的 manifest，要讀內容
    # 才能分辨「注入物」與「釘選值尚未轉正的新世代」。只有前者會被搬走。
    sweep_decisions = resolve_manifest_evidence(
        sweep_decisions,
        drive,
        cache,
        current_state,
        repo_uuid=repo_uuid,
        listing=listing,
        prefix_folder_id=prefix_folder_id,
    )

    # 5.5 讀取視圖清掃計畫（若提供 readview_listing）
    readview_decisions: list[SweepDecision] = []
    if readview_listing is not None:
        readview_decisions = plan_readview_sweep(readview_listing, current_state)

    # 6. 合併所有決策並套用清掃 (H3, M7)
    all_decisions = parent_decisions + sweep_decisions + readview_decisions
    moved_count = apply_sweep(
        all_decisions,
        drive,
        quarantine_folder_id=quarantine_folder_id,
        clock=clock,
        prefix_folder_id=prefix_folder_id,
        dry_run=dry_run,
    )

    return SettleAndSweepResult(
        settle_outcome=settle_outcome,
        state=current_state,
        listing=listing,
        decisions=tuple(all_decisions),
        moved_count=moved_count,
    )
