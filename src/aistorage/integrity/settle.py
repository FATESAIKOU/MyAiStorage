"""AiStorage 待定釘選值結算模組。

依據規格：
- docs/impl/group3-modules.md 第 3.3 節
- design.md D2（第 3 步結算、唯讀不移動、重放驗證、.bak 復原）
- ADR 0008（信任錨點防線）
- review-1.4f3 M2（不存在「信任任何能重放的 manifest」模式，僅 state 與 pending 可信）
- review-903d7e2 M2（主 manifest 候選與重查指紋都用「這一輪 push 之前就存在」這個
  住民偽造不了的條件篩掉；重放出同樣 refs 的多份候選用 createdTime 決勝）
- review-5d4dd52 H1（候選**只認** `pending.expected_manifest_sha256`：提交流程
  自己知道剛寫出去的位元組，住民就無法靠搶「建立最早」把自己的變體轉正成真本；
  沒有這個欄位時多份內容不同的候選一律 fail-closed，不用 createdTime 猜）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import hashlib
from pathlib import Path
import shutil
import tempfile
from typing import Any

from aistorage.annex.manifest import BundleName, Manifest, parse_bundle_name, parse_manifest
from aistorage.annex.replay import replay_refs
from aistorage.clock import Clock, parse_rfc3339
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import MismatchError, ReadError, TooLarge
from aistorage.integrity.pin import PinPending, PinState

#: `pending.written_at` 之後多久還算「這一輪 push 的產物」。
#:
#: review-903d7e2 M2：pending 寫在 push **之前**，而 manifest 是 push **期間**由
#: rclone 寫出來的（實測 rclone 1.75.1 與 1.69.3：file id 每輪都會變，是刪掉
#: 重建），所以真正那份 manifest 的 createdTime **晚於** `written_at`——不能拿
#: 「早於 written_at」篩候選，那會把真本篩掉。改用反過來的方向：晚於
#: `written_at + 這個容差` 的同名檔不算候選、也不進重查指紋。push 實測 22～29 秒、
#: Drive 列表落後實測可達 30 秒，給十分鐘綽綽有餘。
#:
#: 這同時是住民攻擊的下限：要放一份「重放出同樣 refs」的同名 manifest 來
#: 拖住 settle（review-903d7e2 M2 的攻擊 (a)），他必須在 push 之後十分鐘內上傳，
#: 而他得先讀到剛 push 出去的 manifest 才有內容可複製（Drive 列表落後）。
#:
#: 更重要的是「一次就永久」：被篩掉的檔案從此不進指紋，所以他**不斷新增同名檔也
#: 操縱不了重查上限**（舊規則是指紋每次都變，必定用完上限、每一輪都中止在第 3 步，
#: sweep 永遠跑不到）。
PUSH_GRACE = timedelta(minutes=10)


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


def _relist(drive: DriveClient, prefix_folder_id: str) -> RepoListing:
    """重新列舉前綴（只讀；用於「決定當下」的證據）。"""
    children = drive.list_children(prefix_folder_id)
    return RepoListing(
        prefix_folder_id=prefix_folder_id,
        files=tuple(c for c in children if not c.is_folder),
        subfolders=tuple(c for c in children if c.is_folder),
    )


def _relevant_names(state: PinState) -> frozenset[str]:
    """settle 的結論真正依賴的檔名（其餘檔案變動與結論無關）。

    review-1926cd3-142fd04 M2：`_fingerprint` 原本涵蓋前綴裡**所有**檔案，
    於是住民只要在重查期間持續放檔案，每一次重新列舉都「有變動」，重查必定
    用完上限，最後照樣丟掉 pending——把一個上限變成住民可以操縱的開關。

    settle 判斷用得到的只有：主 manifest、`.bak`、以及正式值引用的 bundle
    （active 與 removed）。pending 記的是 refs 與 annex key，沒有 bundle 名單，
    所以「pending 引用的 bundle」在這個模型裡就是正式值的那一份。
    住民的垃圾檔、未知檔名、未被引用的 annex 物件都不影響結論。
    """
    return frozenset(
        {f"GITMANIFEST--{state.repo_uuid}", f"GITMANIFEST--{state.repo_uuid}.bak"}
        | set(state.active_bundles)
        | set(state.removed_bundles)
    )


def _fingerprint(
    listing: RepoListing,
    names: frozenset[str] = frozenset(),
    *,
    exclude_created_after: datetime | None = None,
) -> tuple[tuple[str, str | None, int | None, str], ...]:
    """前綴的「指紋」：檔名、雜湊、大小、id。任一項不同就代表期間有變動。

    `names` 給定時只看這些檔名（M2：住民的垃圾檔不應該影響 settle 的結論）。

    `exclude_created_after` 給定時，**建立時間晚於它**的檔案完全不進指紋
    （review-903d7e2 M2）：那一輪 push 之後才出現的同名 manifest／`.bak` 不是
    「遠端狀態改變了」，而是有人放了東西進來。把它算進指紋等於給住民一個
    「每次重查都一定有變動」的開關——他只要在重查那幾十秒裡加檔名就能讓上限
    必定用完、每一輪都中止在第 3 步。
    """
    def _in_fingerprint(f: DriveFile) -> bool:
        if names and f.name not in names:
            return False
        if exclude_created_after is not None:
            try:
                if f.created_at > exclude_created_after:
                    return False
            except (ValueError, TypeError):
                pass  # 讀不出建立時間：寧可算進來（多一次重查），不要漏掉真變動
        return True

    kept = [f for f in listing.files if _in_fingerprint(f)]
    if not kept and exclude_created_after is not None:
        # 篩完一個都不剩＝連真本都被算成「push 之後」——那一定是推算出錯或這一輪
        # push 慢到離譜。不要因為篩選而看不到任何相關檔案（會把「遠端有變動」判成
        # 「沒變動」），寧可不做這次篩選。
        kept = [f for f in listing.files if not names or f.name in names]
    return tuple(sorted((f.name, f.sha256, f.size, f.id) for f in kept))


def push_cutoff(pending: PinPending | None) -> datetime | None:
    """這一輪 push 的產物最晚可以建立到什麼時候（`written_at + PUSH_GRACE`）。

    讀不出 `pending.written_at` 就沒有這條篩選（回 None＝全部都算候選），寧可多
    比較幾份，也不要因為時間欄位壞掉而看不到真本。
    """
    if pending is None:
        return None
    try:
        return parse_rfc3339(pending.written_at) + PUSH_GRACE
    except (ValueError, TypeError):
        return None


def _is_push_output(f: DriveFile, cutoff: datetime | None) -> bool:
    """這份檔案算不算「這一輪 push 已經寫出來的東西」。

    cutoff 為 None（沒有 pending／時間讀不出來）時全部算——篩選是為了排除住民在
    push 之後放的東西，不能反過來把真本篩掉。
    """
    if cutoff is None:
        return True
    try:
        return f.created_at <= cutoff
    except (ValueError, TypeError):
        return True


def _only_push_output(files: list[DriveFile], cutoff: datetime | None) -> list[DriveFile]:
    """只留下「這一輪 push 寫出來的」那些；**一個都沒有時原樣返回**。

    為什麼要這個讓步：`written_at + PUSH_GRACE` 是一個「不早不晚」的推算，正常
    情況下真正那份 manifest 一定落在裡面（push 實測 22～29 秒）。但如果一輪 push
    真的超過十分鐘（卡住），連真本都會被篩掉——那時寧可**不要篩**、讓下面的比對
    照舊以 createdTime 決議或 fail-closed 中止，也不要因為篩選把真本判成不存在。
    """
    inside = [f for f in files if _is_push_output(f, cutoff)]
    return inside or files


#: DROPPED／BAK_RECOVERY 這兩個結論會**丟掉 pending**，所以它們在丟東西之前
#: 最多重新列舉這麼多次。重查是為了讓「丟掉 pending」建立在決定當下的證據上，
#: 而不是進場時的快照；用完仍在變動就中止（保留 pending），不是照丟。
_MAX_RECHECK = 2


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

    impl1：DROPPED 與 BAK_RECOVERY 會丟掉 pending，所以它們必須建立在**決定當下**
    的證據上。步驟 2 要下載並重放 bundle，耗時數十秒；這段時間裡別的輪次可能
    已經把 push 完成（impl1 現場：pending 被丟掉、遠端卻已經往前，之後每一輪都
    `No git repository found in this remote`）。PROMOTED 不受影響——它要求「看得到」
    新 manifest，而 Drive 的列表只會落後、不會超前。所以只有這兩個結論在丟東西
    之前重新列舉一次，前綴有變動就重做整段判斷（有上限）。

    review-1926cd3-142fd04 M2：兩件事要一起改，否則上限形同虛設。

    1. **用完上限時中止，不丟 pending**。原本第三次不論清單是否還在變動都照樣
       回傳 DROPPED／BAK_RECOVERY，等於把「用完上限」變成「照舊丟掉 pending」，
       行為回到修改之前。現在改成 fail-closed：中止這一輪（pending 保留），
       方向和「主 manifest 缺失」那條路一致。
    2. **指紋只算與結論有關的檔案**（`_relevant_names`）。原本指紋涵蓋前綴裡
       所有檔案，住民只要持續放垃圾檔就能讓每一次重查都「有變動」、必定用完
       上限。指紋縮小到主 manifest／`.bak`／pending 與正式值引用的 bundle 之後，
       垃圾檔不再影響結論，也就無法再操縱這個上限。
    3. **push 之後才出現的同名 manifest／`.bak` 連指紋都不算**
       （`_fingerprint(exclude_created_after=…)`，見 `push_cutoff`）。第 2 項還漏
       了一個洞：主 manifest 的**檔名本身**就在相關檔名裡，住民只要在重查那幾十
       秒裡新增同名檔，每一次重查都必定「有變動」→ 必定用完上限 → 每一輪都中止
       在第 3 步、sweep 永遠跑不到。

    review-903d7e2 M2：候選也用同一條規則篩，而且「refs 相符但內容相異」的多份
    候選不再中止——用 createdTime 決勝（見 `_settle_once`）。

    review-5d4dd52 H1：`createdTime` 決勝被拿掉了（它等於讓住民自己決定真身）。
    現在候選只認 `pending.expected_manifest_sha256`；沒有這個欄位時多份內容不同
    的候選一律 fail-closed 中止（見 `_pick_pending_candidate`）。
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
    relevant = _relevant_names(state)
    cutoff = push_cutoff(pending)

    def _fp(ls: RepoListing) -> tuple[tuple[str, str | None, int | None, str], ...]:
        return _fingerprint(ls, relevant, exclude_created_after=cutoff)

    # 進場就用自己的 listing：呼叫端那份是第 3 步開始時的快照。
    listing = _relist(drive, listing.prefix_folder_id)

    for attempt in range(_MAX_RECHECK + 1):
        outcome, new_state, discard = _settle_once(
            state, pending, listing, drive,
            main_name=main_name, bak_name=bak_name, workdir=workdir, clock=clock,
            may_recheck=attempt < _MAX_RECHECK, cutoff=cutoff,
        )
        if outcome is None:
            # 「判定用的畫面裡沒有主 manifest」有可能是 Drive 的列表落後：先重查再說
            if attempt == _MAX_RECHECK:
                raise MismatchError(
                    "主 manifest 缺失且 .bak 無法恢復至正式釘選值；"
                    "真本已不完整，需要管理者用 init-pin 重建釘選值"
                )
            fresh = _relist(drive, listing.prefix_folder_id)
            if _fp(fresh) != _fp(listing):
                listing = fresh
                continue
            raise MismatchError(
                "主 manifest 缺失且 .bak 無法恢復至正式釘選值；"
                "真本已不完整，需要管理者用 init-pin 重建釘選值"
            )
        if not discard:
            return outcome, new_state
        # 這個結論會丟掉 pending → 確認前綴在這段時間裡沒有變動
        fresh = _relist(drive, listing.prefix_folder_id)
        if _fp(fresh) == _fp(listing):
            return outcome, new_state
        if attempt == _MAX_RECHECK:
            # M2：重查用完上限、前綴仍在變動 → 中止這一輪並**保留 pending**。
            # 丟掉 pending 就再也沒有人負責把遠端往前推（impl1 的終局）。
            raise MismatchError(
                f"前綴在重查期間持續變動（已重查 {_MAX_RECHECK} 次），"
                f"無法在決定當下確認遠端狀態；中止這一輪並保留待定釘選值——"
                f"它仍然是「這一輪 push 之後遠端會變成什麼」的唯一說明。"
                f"若確實需要管理者介入，請確認前綴不再變動後再由 settle 結算，"
                f"或用 init-pin 以觀測到的遠端狀態重建釘選值"
            )
        listing = fresh

    raise MismatchError("內部錯誤：settle 的重查迴圈沒有回傳結果")


def _pick_pending_candidate(
    matches: list[dict[str, Any]], pending: PinPending
) -> dict[str, Any]:
    """多份候選都重放得出 `pending.refs` 時，哪一份才是這一輪 push 的產物？

    **H1（review-5d4dd52）：只認 `pending.expected_manifest_sha256`。**

    提交流程 push 之後從本機 `.git/annex/git-remote-annex/<uuid>/manifest` 讀到
    自己剛寫出去的位元組（實測與遠端那份 sha256 完全相同），把它記進 pending。
    舊規則是「refs 相同就成立，多份用 `createdTime` 最早者勝」，而那個「最早」
    是住民**自己就能製造**的：只要早於 rclone 寫出 manifest 就能贏。贏了之後
    住民的變體被轉正成正式值（釘選值的 `manifest_sha256` 就是它）、真正那份被
    當成「內容不同的同名 manifest」隔離，住民再刪掉自己那份，前綴就沒有能用的
    manifest、`git clone` 失敗，而且不會自己好。

    所以改成：候選**只接受** sha 等於 `expected_manifest_sha256` 的檔案。

    - 有這個欄位 → 命中它就採用（其他內容的同名檔不是這一輪的產物，之後由
      sweep 收拾）。
    - 沒有這個欄位（push 與補寫 pending 之間中斷）**而且**候選內容不只一種
      → **fail-closed 中止**：保留 pending 與遠端原狀。「停擺」是已經接受的
      殘餘風險（ADR 0008 殘餘風險 (1)），「被接管」不是，所以寧可停擺。
    - 沒有這個欄位、候選內容只有一種 → 那就是它（沒有可選的余地）。
    """
    shas = {c["sha256"] for c in matches}
    if len(shas) == 1:
        return matches[0]

    expected = pending.expected_manifest_sha256
    if expected:
        pinned = [c for c in matches if c["sha256"].lower() == expected.lower()]
        if len(pinned) == 1:
            return pinned[0]
        if len(pinned) > 1:
            # 內容雜湊相同的多份（rclone 同一輪 push 內因 Drive 列表落後會留下
            # 兩份位元組相同的主 manifest，實測 1.75.1／1.69.3）：對 settle 的
            # 結論沒有分別，挑建立最早的一份下載。
            from aistorage.integrity.verify import _created_rank

            return min(pinned, key=lambda c: _created_rank(c["file"]))
        raise MismatchError(
            f"候選之中沒有一份的內容雜湊等於 pending 記載的 expected_manifest_sha256"
            f"（{expected}）；讀到的候選內容雜湊 {sorted(shas)}——"
            f"遠端沒有這一輪 push 寫出去的位元組，不猜，保留 pending 與遠端原狀，中止這一輪"
        )

    raise MismatchError(
        f"存在多個內容相異但重放 refs 均相符待定之 manifest 候選: {sorted(shas)}；"
        f"而 pending 沒有 expected_manifest_sha256（這一轮在 push 與補寫之間中斷了，"
        f"或 pin repo 裡是舊格式的 pending）——沒有任何證據能分辨哪一份才是這一輪 "
        f"push 的產物，而用 createdTime 決勝等於讓住民自己決定真身。不猜，"
        f"保留 pending 與遠端原狀，中止這一輪"
    )


def _settle_once(
    state: PinState,
    pending: PinPending,
    listing: RepoListing,
    drive: DriveClient,
    *,
    main_name: str,
    bak_name: str,
    workdir: Path,
    clock: Clock,
    may_recheck: bool,
    cutoff: datetime | None = None,
) -> tuple[SettleOutcome | None, PinState, bool]:
    """`settle` 的一輪判斷。

    回傳 `(outcome, state, discard)`：
    - `outcome` 為 None 代表「判定用的畫面可能落後了，請重新列舉再判一次」；
    - `discard` 代表這個結論會**丟掉 pending**（DROPPED／BAK_RECOVERY）。

    `cutoff` = `push_cutoff(pending)`：建立時間晚於它的同名 manifest／`.bak`
    **不算候選**（review-903d7e2 M2）。這些是這一輪 push 之後才被放進來的東西，
    不是「這一輪 push 的產物」；它們也不進指紋，所以無法用來操縱重查上限。
    """
    main_files = [f for f in listing.files if f.name == main_name]

    # 若無主 manifest，檢驗是否符合 Rule C: .bak 復原
    if not main_files:
        bak_files = _only_push_output(
            [f for f in listing.files if f.name == bak_name], cutoff
        )
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
                        return SettleOutcome.BAK_RECOVERY, state, True
                except (MismatchError, TooLarge):
                    continue
        if may_recheck:
            # 「沒有主 manifest」有可能是 Drive 的列表落後：重新列舉再判一次
            return None, state, False
        raise MismatchError(
            "主 manifest 缺失且 .bak 無法恢復至正式釘選值；"
            "真本已不完整，需要管理者用 init-pin 重建釘選值"
        )

    # 存在主 manifest 候選檔案（篩掉這一輪 push 之後才出現的那些）
    candidate_results: list[dict[str, Any]] = []

    for mf in _only_push_output(main_files, cutoff):
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
                "file": mf,
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
        matched = _pick_pending_candidate(pending_matches, pending)
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
        return SettleOutcome.PROMOTED, new_state, False

    # 2. 檢查是否符合 DROPPED（符合 state.refs 且內容雜湊相符）
    state_matches = [
        c for c in candidate_results
        if c["refs"] == state.refs and c["sha256"] == state.manifest_sha256
    ]
    if state_matches:
        return SettleOutcome.DROPPED, state, True

    # 3. 兩者皆不符合 -> 中止（不丟 pending：遠端已經領先釘選值，丟掉 pending
    #    就再也沒有人負責把它推進）
    seen = {c["sha256"] for c in candidate_results}
    raise MismatchError(
        f"遠端 manifest 狀態與待定或正式釘選值均不符，無法自動結算"
        f"（讀到的 manifest 內容雜湊: {sorted(seen) or '（沒有讀到任何候選）'}，"
        f"待定 refs: {pending.refs}，正式 refs: {state.refs}）；"
        f"pending 保留不動，遠端也沒有被動過——需要管理者確認後用 init-pin 重建釘選值"
    )
