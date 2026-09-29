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
- review-1926cd3-142fd04 H1／M1（見 plan_sweep 與 resolve_manifest_evidence）：
  已有 KEEP 候選時其餘同名 manifest 一律隔離（不讀內容）；內容證據必須綁在
  pending 上（重放 refs == pending.refs）；HOLD 只給有背書的檔案並設年齡上限
"""

from __future__ import annotations

from collections.abc import Mapping
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
    HOLD = "hold"                               # 有背書（pending／待轉正的 manifest）且未逾齡：不動
    NEED_ADMIN = "need_admin"                   # 沒有任何背書也沒有「是注入物」的證據：不動，但要人處理


@dataclass(frozen=True)
class SweepPolicy:
    """第 4 步的外部輸入：HOLD 的背書來源、年齡上限與時鐘。

    為什麼要有這個（review-1926cd3-142fd04 H1／M1）：HOLD 是「**有人替它背書**
    所以先不要動」，不是「看起來合法就先不要動」。沒有背書的檔案會變成住民永久
    存放任意內容的地方，所以：

    - `pending`：待定釘選值（push **之前**就寫下來，唯一可信的「即將發生」紀錄）。
      manifest 的 HOLD 必須靠它（重放 refs == pending.refs），annex 物件的 HOLD
      必須靠 `pending.annex_keys`。
    - `hold_max_age_days`：HOLD 的年齡上限（`quarantine_retention_days`）。
      超齡還在 HOLD 代表釘選值這一週都沒追上，繼續等沒有意義 → 隔離並回報。
    - `now`：計算年齡用的時鐘（測試可注入）。
    """

    pending: PinPending | None = None
    hold_max_age_days: int | None = None
    now: datetime | None = None


#: HOLD 年齡上限的預設值。等同設定檔的 `quarantine_retention_days`
#: （`DEFAULT_QUARANTINE_DAYS`）；`SweepPolicy(hold_max_age_days=None)` 代表
#: **不設上限**（純函式呼叫端自己判斷要不要追年齡，健康檢查就是這樣用的：
#: 它要看到全部背書不了的檔案，而不是只看到還沒逾齡的那些）。
DEFAULT_HOLD_MAX_AGE_DAYS = 7


def file_age_days(f: DriveFile, now: datetime | None) -> float | None:
    """檔案在 Drive 上的年齡（天）。`created_time` 讀不出來回 None（視為未逾齡）。

    用 Drive 的 `created_time`（寫入者控制不了），不是 `modified_time`——
    住民可以反覆改寫檔案讓 modified_time 一直更新，那樣年齡上限就形同虛設。
    """
    if now is None:
        return None
    try:
        created = f.created_at
    except (ValueError, TypeError):
        return None
    return (now - created).total_seconds() / 86400.0


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
    backed_bundles: Mapping[str, tuple[Disposition, str]] | None = None,
    policy: SweepPolicy | None = None,
) -> list[SweepDecision]:
    """計算清掃計畫（純函式，不碰網路與 Drive）。

    規則（design D2、review-1.4f3 H2/H3、review-1.4f5 H1、review-g3c L、impl1、
    review-1926cd3-142fd04 H1／M1）：

    - 非 .bak 的 GITMANIFEST：**已經有一份 KEEP 候選**（內容雜湊 == 正式值）時，
      其餘同名主 manifest 一律 QUARANTINE，**不讀內容**。只有完全沒有 KEEP 候選
      時才需要讀內容判斷（NEED_MANIFEST_CHECK，見 `resolve_manifest_evidence`）。
    - .bak：同上——已經有一份 KEEP 候選時其餘同名 .bak 一律 QUARANTINE。
    - GITBUNDLE：名稱 ∈ active 而且 sha256 == 名稱內嵌雜湊且大小相符 -> KEEP（重複者僅留一個）；
                名稱 ∈ removed -> GC；
                **有背書**（被候選 manifest 列為 active）-> HOLD／NEED_ADMIN（未逾齡）；
                其餘 -> QUARANTINE（自洽但無背書也不再留著）。
    - annex 物件（SHA256E-s<N>--<sha>…）：key ∈ state.annex_keys 而且 sha256 相符且大小相符 -> KEEP（重複者僅留一個）；
      key ∈ pending.annex_keys 且自洽 -> HOLD（未逾齡）；
      **其餘一律 QUARANTINE**（自洽不等於被背書）。
    - **但「自洽不等於被背書就隔離」這條只在釘選值與前綴一致時適用**：前綴裡
      沒有任何一份內容等於正式值的主 manifest（`pin_in_sync` 為 false）時，
      釘選值對這個前綴根本沒有任何權威——「不在釘選值裡」不能當成注入物的
      證據，那會把「遠端領先釘選值、pending 又不見了」這個狀態裡**真正的新世代
      物件**搬走（impl1 的終局：raw 物件被隔離 → `init-pin` 斷言它們在 Drive 上
      → 只能照 recovery runbook 模式 0 搬回來）。這種狀態下不背書也不隔離的檔案
      一律 NEED_ADMIN：不搬移，列入健康檢查等人處理；釘選值一旦追上
      （`init-pin` 或下一輪 settle），下一輪它們就會被正常隔離。
    - sha256 或 size 缺失 -> NEED_CONTENT_CHECK。
    - 子資料夾 -> QUARANTINE（整個子樹）。
    - 其他名稱 -> QUARANTINE。
    - **檔名宣告與內容不符**（bundle 的 sha256／大小、annex key 的 sha256／大小
      對不上）一律 QUARANTINE，不受上面那個閘門影響：git-annex 的 key／檔名本來
      就是從內容算出來的，對不上就是假的，跟釘選值有沒有跟上無關。

    **H1：已有 KEEP 候選時，其餘同名 manifest 必然是注入物。**

    會引爆事故的正常情況是「前綴裡**只有一份**主 manifest，而它的內容領先釘選值」
    （某一輪 push 成功、第 11 步驗證沒過，rclone 原地更新同一個檔案）。正常流程
    不會產生第二份同名主 manifest——所以當前綴裡已經躺著一份內容等於正式值的
    KEEP 候選時，任何**其他**同名主 manifest 只可能是住民放進去的，直接隔離即可，
    不需要讀它的內容。反過來，舊規則在這裡會讀內容、看到「解析得出來、引用的
    bundle 都在」，於是 HOLD——而 HOLD 讓 `verify_clone` 的「主 manifest 恰好一份」
    每一輪都失敗，提交流程永久停擺（review-1926cd3 H1）。

    **M1：HOLD 只給有背書的檔案，而且有年齡上限。**

    舊規則是「內容與檔名自稱值相符就 HOLD」，等於任何位元組只要檔名取成自己的
    雜湊就能永久留在真本前綴裡。現在：

    - annex 物件的背書是 `pending.annex_keys`（push 之前就寫下來）；
    - bundle 的背書是「pending 對應的那份 manifest 列為 active」
      （由 `resolve_manifest_evidence` 判定後經 `backed_bundles` 帶進來）；
    - manifest 的背書是「重放出來的 refs == pending.refs」；
    - 沒有 pending、也沒有任何東西背書 → 不是 HOLD，是 **NEED_ADMIN**：
      不搬移（沒有「是注入物」的證據），但要列入健康檢查等人處理。
    - HOLD 超過 `hold_max_age_days`（`quarantine_retention_days`）→ QUARANTINE
      並在報告裡回報（`held_expired_files`），不再無限期等。

    `manifest_verdicts`（file id → (處置, 理由)）由 `resolve_manifest_evidence`
    帶進來重算，`backed_bundles` 由它一併帶入，維持「整份重判」而不是逐項補丁。
    """
    decisions: list[SweepDecision] = []
    default_from_parent = prefix_folder_id or listing.prefix_folder_id
    verdicts = manifest_verdicts or {}
    backed = backed_bundles or {}
    pol = policy or SweepPolicy()
    max_age = pol.hold_max_age_days
    now = pol.now
    pending = pol.pending

    def _aged_out(f: DriveFile) -> bool:
        """HOLD 是否已逾齡（age 上限 = quarantine_retention_days）。"""
        if max_age is None or now is None:
            return False
        age = file_age_days(f, now)
        return age is not None and age > max_age

    def _hold(f: DriveFile, reason: str) -> SweepDecision:
        """HOLD 的單一出口：逾齡（超過 quarantine_retention_days）就降級成隔離。

        M1：HOLD 是「有人替它背書、釘選值這一輪就會追上」的**暫時**狀態。
        放了七天還在 HOLD，代表釘選值這一週都沒追上（多半是 pending 被丟掉、
        或管理操作卡住），繼續等下去只是讓住民多一個永久存放區——所以逾齡就
        隔離，並且在報告裡回報（`held_expired_files`），讓它變成可見的問題。

        **NEED_ADMIN 不走這裡**：它代表「沒有任何可信來源能解釋這個檔案」，
        而那個檔案很可能**就是**真本唯一的一份（impl1 現場：pending 被丟掉之後
        剩下的那份 manifest）。逾齡就隔離等於消滅真本。它改由健康檢查警示 +
        檔名回報來逼人處理（`need_admin_files`），隔離留給「有證據是注入物」的
        情況。
        """
        if _aged_out(f):
            return SweepDecision(
                file=f,
                disposition=Disposition.QUARANTINE,
                reason=(
                    f"{reason}；但它作為 HOLD 已經超過 {max_age} 天仍然沒有釘選值／"
                    "待定背書（釘選值這一週都沒追上），不再等待：隔離並回報"
                ),
                from_parent=default_from_parent,
            )
        return SweepDecision(
            file=f, disposition=Disposition.HOLD, reason=reason,
            from_parent=default_from_parent,
        )

    def _need_admin(f: DriveFile, reason: str) -> SweepDecision:
        """沒有任何背書、也沒有「是注入物」的證據：不動，但列入健康檢查。"""
        return SweepDecision(
            file=f, disposition=Disposition.NEED_ADMIN, reason=reason,
            from_parent=default_from_parent,
        )

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

    # H1：先掃一次清單，找出「已經有 KEEP 候選」的兩種名字。Drive 更新是原地
    # 更新，正常流程不會有第二份同名檔；有 KEEP 候選就代表其餘同名檔是注入物。
    has_main_keep_candidate = any(
        f.name == main_name and f.sha256 is not None
        and f.sha256.lower() == state.manifest_sha256.lower()
        for f in listing.files
    )
    has_bak_keep_candidate = any(
        f.name == bak_name and f.sha256 is not None
        and f.sha256.lower() in allowed_bak_hashes
        for f in listing.files
    )
    # 前綴裡有沒有「釘選值背書得住」的那一份主 manifest。沒有的話釘選值對這個
    # 前綴沒有任何權威，「不在釘選值裡」就不能當成注入物的證據（見 docstring）。
    pin_in_sync = has_main_keep_candidate

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
            elif has_main_keep_candidate:
                # H1：前綴裡已經有一份內容等於正式值的 manifest（Drive 原地更新，
                # 不會產生第二份同名檔），這份同名檔必然是注入物。不讀內容。
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason=(
                            "已有內容等於正式值的主 manifest，另一份同名主 manifest "
                            "必然是注入物（rclone 原地更新，不會有第二份同名檔）"
                        ),
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
                if disposition is Disposition.HOLD:
                    decisions.append(_hold(f, reason))
                elif disposition is Disposition.NEED_ADMIN:
                    decisions.append(_need_admin(f, reason))
                else:
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
            elif has_bak_keep_candidate:
                # H1：同樣的邏輯套用在 .bak 上。
                decisions.append(
                    SweepDecision(
                        file=f,
                        disposition=Disposition.QUARANTINE,
                        reason=(
                            "已有內容符合釘選允許集合的 .bak，另一份同名 .bak "
                            "必然是注入物（原地更新，不會有第二份同名檔）"
                        ),
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
                if disposition is Disposition.HOLD:
                    decisions.append(_hold(f, reason))
                elif disposition is Disposition.NEED_ADMIN:
                    decisions.append(_need_admin(f, reason))
                else:
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
                # M1：不在釘選值的 active/removed 裡，證據只有「檔名自稱的
                # sha256/size 與實際內容對得上」。這**不再是**留下的理由——
                # 任何位元組都做得到自洽。留下的唯一背書是「某份候選 manifest
                # 把它列為 active」，由 `resolve_manifest_evidence` 判定後經
                # `backed_bundles` 帶進來。
                self_consistent = (
                    f.size == b_info.size and f_sha == b_info.sha256.lower()
                )
                if not self_consistent:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason="bundle 雜湊或大小與檔名宣告不符（檔名宣告與內容不符）",
                            from_parent=default_from_parent,
                        )
                    )
                    continue

                backed_verdict = backed.get(f.name)
                if backed_verdict is None and not pin_in_sync:
                    decisions.append(
                        _need_admin(
                            f,
                            "內容與檔名宣告相符，但前綴裡沒有任何一份主 manifest "
                            "的內容等於正式釘選值——釘選值對這個前綴沒有權威，"
                            "「不在釘選值裡」不足以指認它是注入物："
                            "不搬移（避免消滅真本），列入健康檢查等人處理",
                        )
                    )
                elif backed_verdict is None:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason=(
                                "內容與檔名宣告相符，但不在釘選值的 active/removed 內，"
                                "也沒有任何候選 manifest 把它列為 active："
                                "自洽不等於被背書，隔離"
                            ),
                            from_parent=default_from_parent,
                        )
                    )
                elif backed_verdict[0] is Disposition.NEED_ADMIN:
                    decisions.append(
                        _need_admin(
                            f,
                            f"內容與檔名宣告相符，且{backed_verdict[1]}："
                            "不搬移（避免消滅真本），但要人處理",
                        )
                    )
                else:
                    decisions.append(
                        _hold(
                            f,
                            f"內容與檔名宣告相符，且{backed_verdict[1]}：不搬移",
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
            elif f.size == key_size and f_sha == key_sha:
                # M1：自洽但不在釘選值裡。唯一可信的背書是 pending.annex_keys
                # ——它在 push **之前**就寫進 pin repo，記的是「這輪會寫上去什麼」。
                if pending is not None and f.name in pending.annex_keys:
                    decisions.append(
                        _hold(
                            f,
                            "內容與 key 內嵌的 sha256／大小相符，且 pending.annex_keys "
                            "記著這個 key（push 之前就寫定，尚待釘選值轉正）",
                        )
                    )
                elif not pin_in_sync:
                    decisions.append(
                        _need_admin(
                            f,
                            "內容與 key 內嵌的 sha256／大小相符，但前綴裡沒有任何"
                            "一份主 manifest 的內容等於正式釘選值——釘選值對這個前綴"
                            "沒有權威，無法分辨它是新世代的 raw 物件還是注入物："
                            "不搬移（搬走等於消滅真本），列入健康檢查等人處理",
                        )
                    )
                else:
                    decisions.append(
                        SweepDecision(
                            file=f,
                            disposition=Disposition.QUARANTINE,
                            reason=(
                                "annex 物件自洽（sha256／大小與 key 內嵌值相符），"
                                "但不在釘選值的 key 集合內，也不在 pending.annex_keys："
                                "自洽不等於被背書，隔離"
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
    backed_bundles: Mapping[str, tuple[Disposition, str]] | None = None,
    policy: SweepPolicy | None = None,
) -> list[SweepDecision]:
    """針對缺少 sha256Checksum 或 size 的項目下載計算雜湊並整份重新判定（M8, L）。

    - 快取 key 使用複合值 (file_id, size, md5 或 modified_time) 防止原地改寫偽造。
    - 大檔下載可指定 workdir。
    - 將解析後的檔案更新回完整 listing，重新整份執行 plan_sweep 以保持全局狀態一致性。
    - `manifest_verdicts`／`backed_bundles`／`policy` 會一併帶進重判（impl1：
      manifest 內容驗證的結果不能在這一步被清掉；review-1926cd3 M1：pending
      背書與 HOLD 年齡上限也不能在這一步失效）。
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
        backed_bundles=backed_bundles,
        policy=policy,
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
    workdir: Path | None = None,
    policy: SweepPolicy | None = None,
) -> list[SweepDecision]:
    """讀 manifest 內容，判定「是注入物」還是「釘選值還沒追上的新世代」。

    只處理 `plan_sweep` 標成 NEED_MANIFEST_CHECK 的檔（內容雜湊既不是正式值、
    不是已退役的上一版，而且前綴裡**沒有** KEEP 候選——有 KEEP 候選時 `plan_sweep`
    就已經直接隔離了，不會走到這裡，見 H1）。

    判斷分兩層證據：

    1. **是不是這個 repo 的 manifest**：內容能 `parse_manifest(repo_uuid=...)`，
       而且它列出的每個 active bundle 都在前綴裡、Drive 的 sha256 與檔名內嵌雜湊
       相符。任何一條不成立 → 有「是注入物」的證據 → QUARANTINE。
    2. **有沒有背書**（impl1 review M1）：
       - 有 pending：把候選 manifest 的 active bundle 實際下載並重放，
         **refs == pending.refs** 才是真的 → HOLD（pending 是在 push 之前寫下的，
         它記的就是「這一輪 push 之後遠端會變成什麼」）。
         重放出來不等於 pending.refs → 沒有任何可信來源能解釋這份 manifest
         → **NEED_ADMIN**（不搬移，但列入健康檢查等人處理；不是無限期 HOLD）。
       - 沒有 pending：沒有任何可信來源能解釋它 → NEED_ADMIN。

    **為什麼 refs 不符時不直接隔離**：settle 已經用同一套重放檢查過 pending，
    走到這裡代表 pending 要嘛不存在、要嘛已經被 settle 處理過。這時把一份
    「解析得出來、bundle 都在」的 manifest 搬走，正是 impl1 弄壞真本的那一步
    （`No git repository found in this remote`）。但它也不能無限期留下——所以
    是 NEED_ADMIN：有名字、有計數、健康檢查會報出來。

    判定完之後整份重判（和 resolve_content_checks 同一個理由：逐項補丁會讓
    全域狀態不一致），並把「哪一份 manifest 是背書來源」交給 `plan_sweep`，
    讓它把該 manifest 列為 active 的 bundle 一併判成 HOLD／NEED_ADMIN。

    讀不到（ReadError）原樣往上拋：寧可中止，也不要在讀不到證據時搬走真本。
    """
    if not any(d.disposition == Disposition.NEED_MANIFEST_CHECK for d in decisions):
        return decisions

    available = listing.files if listing is not None else ()
    verdicts: dict[str, tuple[Disposition, str]] = {}
    #: bundle 名稱 -> 背書它的 manifest 檔名（只有真的那一份會進來）
    backed_bundles: dict[str, tuple[Disposition, str]] = {}
    pol = policy or SweepPolicy()
    pending = pol.pending
    verified: dict[str, tuple[Any, ...]] = {}  # file id -> 解析結果（待重放）

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

        verified[f.id] = (f, parsed)

    # 第二層：有 pending 才算得出「這一輪 push 之後遠端會變成什麼」
    if verified:
        if pending is None:
            for fid, (f, _parsed) in verified.items():
                verdicts[fid] = (
                    Disposition.NEED_ADMIN,
                    "內容是這個 repo 的 manifest，且它引用的 bundle 都在前綴裡，"
                    "但沒有 pending 能證明它是哪一輪 push 的產物："
                    "無法證明是注入物所以不搬移，也無法證明該留到釘選值追上——"
                    "需要管理者用 init-pin 重建釘選值（健康檢查會報出這個檔名）",
                )
        else:
            matched_ids: list[str] = []
            for fid, (f, parsed) in verified.items():
                replay = _replay_candidate_refs(
                    parsed.active, repo_uuid, available, drive, workdir
                )
                if replay.refs is None:
                    if replay.injection_evidence:
                        verdicts[fid] = (
                            Disposition.QUARANTINE,
                            "manifest 引用的 bundle 無法重放，它描述的遠端狀態不成立"
                            f"（{replay.detail}）",
                        )
                    else:
                        verdicts[fid] = (
                            Disposition.NEED_ADMIN,
                            f"無法驗證這份 manifest（{replay.detail}）："
                            "證據不足以指認它是注入物，不搬移，等管理者處理",
                        )
                    continue
                if replay.refs == pending.refs:
                    matched_ids.append(fid)
                else:
                    verdicts[fid] = (
                        Disposition.NEED_ADMIN,
                        "manifest 可以解析、引用的 bundle 也都在，但重放出來的 refs "
                        "既不等於待定釘選值記載的 refs、也不等於正式釘選值："
                        "沒有可信來源能解釋這份 manifest，不搬移（避免消滅真本），"
                        "改由管理者用 init-pin 重建釘選值",
                    )
            if len(matched_ids) > 1:
                # 多份候選都重放得出 pending.refs：無法分辨哪份才是（review M2）
                for fid in matched_ids:
                    verdicts[fid] = (
                        Disposition.NEED_ADMIN,
                        "不只一份候選 manifest 重放出待定的 refs，無法分辨哪一份"
                        "才是這一輪的產物：不搬移，等管理者處理",
                    )
                matched_ids = []
            for fid in matched_ids:
                f, parsed = verified[fid]
                verdicts[fid] = (
                    Disposition.HOLD,
                    "內容是這個 repo 的 manifest、引用的 bundle 都在，"
                    "而且重放出來的 refs 等于 pending.refs（pending 在 push 之前"
                    "就寫定，這就是它該有的樣子）：等釘選值轉正",
                )

    # 背書鏈：一份 manifest 通過內容檢查、又沒有被隔離（判定是 HOLD 或
    # NEED_ADMIN），它列為 active 的 bundle 就跟著同一個處置。這是 bundle
    # 唯一的背書來源——自洽（檔名內嵌的 sha256／大小與內容相符）任何位元組
    # 都做得到，所以不構成背書。
    for fid, (f, parsed) in verified.items():
        disp, _reason = verdicts.get(fid, (None, ""))
        if disp not in (Disposition.HOLD, Disposition.NEED_ADMIN):
            continue
        for b_name in parsed.active:
            backed_bundles.setdefault(
                b_name,
                (
                    disp,
                    f"被「{f.name}」列為 active，而那份 manifest 的判定是 "
                    f"{disp.value}（沒有任何其他來源能背書這個 bundle）",
                ),
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
        backed_bundles=backed_bundles,
        policy=pol,
    )


@dataclass(frozen=True)
class ReplayResult:
    """候選 manifest 的重放結果。

    - `refs is not None`：重放成功，這就是它描述的遠端狀態。
    - `refs is None` + `injection_evidence=True`：**有**「是注入物」的證據
      （引用的 bundle 抓不到／名稱不合法／repo 不符）→ 可以隔離。
    - `refs is None` + `injection_evidence=False`：證據不足（例如 bundle 太大
      下載不下來）→ 不搬移，列入健康檢查等人處理。
    """

    refs: dict[str, str] | None
    injection_evidence: bool
    detail: str = ""


def _replay_candidate_refs(
    active_bundle_names: tuple[str, ...],
    repo_uuid: str,
    available_files: tuple[DriveFile, ...],
    drive: DriveClient,
    workdir: Path | None,
) -> ReplayResult:
    """把候選 manifest 的 active bundle 實際重放出 refs。

    與 settle 用**同一套** `_download_and_replay`：證據必須是同一種，否則
    「settle 說不對、sweep 說有背書」會出現分歧。

    `MismatchError`（bundle 名稱／repo 不符，或前綴裡沒有雜湊相符的那一份）
    是「這份 manifest 描述的遠端狀態不成立」的**證據**；`TooLarge` 只是讀不下來，
    不是證據。`ReadError` 原樣往外拋——讀不到就中止，一個檔都不動。
    """
    from aistorage.integrity.settle import _download_and_replay

    with tempfile.TemporaryDirectory(
        prefix="aistorage_sweep_replay_",
        dir=str(workdir) if workdir and Path(workdir).is_dir() else None,
    ) as td:
        try:
            refs = _download_and_replay(
                active_bundle_names, repo_uuid, available_files, drive, Path(td)
            )
        except MismatchError as e:
            return ReplayResult(None, injection_evidence=True, detail=str(e))
        except TooLarge as e:
            return ReplayResult(
                None, injection_evidence=False,
                detail=f"bundle 超過 256 MiB 下載上限，無法重放驗證: {e}",
            )
        return ReplayResult(refs, injection_evidence=True)


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
    hold_max_age_days: int | None = DEFAULT_HOLD_MAX_AGE_DAYS,
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

    **pending 傳的是「settle 之前」的那一份**：settle 結算完成之後 pending
    可能已經被丟掉或轉正，而第 4 步的背書判斷必須以「這一輪開跑時 pending 還在」
    為準（review-1926cd3 M1）。所以在呼叫 sweep 的三個函式裡都用同一個
    `SweepPolicy`，不要各自重建。

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

    # 第 4 步的背書用「**還在 pin repo 裡**」的那一份 pending：settle 的每一種
    # 結論都會消耗掉 pending（PROMOTED／DROPPED／BAK_RECOVERY 都會刪掉它），
    # 而 DROPPED 的意思是「那一輪的 push 沒有落實」——拿那份被丟掉的紀錄背書
    # 會把孤兒物件誤認為該留的。
    policy = SweepPolicy(
        pending=pending if (pending is None or dry_run) else None,
        hold_max_age_days=hold_max_age_days,
        now=clock_obj.now(),
    )

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
        policy=policy,
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
        policy=policy,
    )

    # 5.4 manifest 內容驗證（impl1 + review-1926cd3 H1／M1）：前綴裡沒有 KEEP
    # 候選時，內容雜湊不在釘選值內的 manifest 要讀內容才能分辨「注入物」與
    # 「釘選值尚未轉正的新世代」，而且留下來的證據必須綁在 pending 上。
    sweep_decisions = resolve_manifest_evidence(
        sweep_decisions,
        drive,
        cache,
        current_state,
        repo_uuid=repo_uuid,
        listing=listing,
        prefix_folder_id=prefix_folder_id,
        workdir=workdir,
        policy=policy,
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
