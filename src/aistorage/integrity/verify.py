"""AiStorage 核對、預檢與 push 後驗證模組。

依據規格：
- docs/impl/group3-modules.md 第 3.5 節
- design.md D2（第 5 步 clone 驗證、第 9 步預檢、第 10 步 push 後重放驗證）
- review-1.2-1.6 H3（push 後以 ls-remote 驗證）
- review-1.4f3 M1（push 後驗證：active 清單比對、created_time、removed 包含關係、重放比對）
- review-cdb4a34 M3（同名檔：任一檔 checksum＋size 相符就算存在）
- review-final M1 b（被釘選的 key 不見時，先到隔離區找 sha256 與 size 都相符的
  檔自動搬回來——內容定址，所以搬回來的一定是對的位元組；見
  `restore_pinned_keys_from_quarantine`）
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import tempfile
import time
from typing import TYPE_CHECKING, Iterable, Sequence
from datetime import datetime, timezone

from aistorage.annex.git import AnnexGit
from aistorage.annex.manifest import parse_bundle_name, parse_manifest
from aistorage.annex.replay import replay_refs
from aistorage.clock import parse_rfc3339
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import MismatchError
from aistorage.integrity.pin import PinState
from aistorage.integrity.settle import RepoListing, check_manifest_continuity


def verify_annex_coverage(
    annex_keys: frozenset[str],
    required_keys: frozenset[str] | set[str] | list[str],
) -> None:
    """驗證 integrity 的 annex key 集合是否涵蓋所有必要的 annex 物件（如原始紀錄、產出本體）。

    若缺少任一必要 key，拋出 MismatchError。

    review-g7-e2e A-M1：檢查本身是對的，問題在「必要 key 從哪裡來」。在
    `AnnexRawStorage` 改用 `git annex lookupkey` 之後（review A-H2），這裡的
    `required_keys` 必須是 git-annex 實際產生的 key。所以這裡多加一道把關：
    形狀不合法（例如自己推算時補上的 `.json` 副檔名，而檔案本身沒有副檔名）
    直接 raise——寧可在覆蓋檢查就爆掉，不要等到 settle 誤判成「遠端少了物件」。
    """
    for key in sorted(set(required_keys)):
        if not _is_plausible_annex_key(key):
            raise MismatchError(
                f"必要的 annex key 形狀不合法（不是 git-annex 產生的 key）: {key!r}；"
                "key 必須來自 `git annex lookupkey`，不能自己用 "
                "SHA256E-s<size>--<sha><ext> 推算"
            )
    missing = set(required_keys) - set(annex_keys)
    if missing:
        raise MismatchError(
            f"integrity annex_keys 集合缺少必要之物件: {sorted(missing)}"
        )


def _is_plausible_annex_key(key: str) -> bool:
    """只接受 `SHA256E-s<size>--<sha256>[.<ext>]`。

    review 的 L 項：WORM key 的形狀其實是 `WORM-s<size>-m<mtime>--<name>`，**沒有
    雜湊**，而且 WORM 檔不可驗證內容。這個 repo 只會產生 SHA256E（原始紀錄、
    產出本體），所以 WORM 一律判成不合法——寧可在覆蓋檢查就爆掉，也不要讓
    一個無法驗證的 key 混進釘選值。
    """
    import re

    return bool(re.match(r"^SHA256E-s\d+--[0-9a-f]{64}(\.[^/\s]*)?$", key))


def _key_size_and_sha(key: str) -> tuple[int, str] | None:
    """從 annex key 解析出 `(size, sha256)`；形狀不合法回 None。"""
    size_text, _, sha_text = key.partition("--")
    try:
        key_size = int(size_text.split("-s", 1)[1])
    except (IndexError, ValueError):
        return None
    return key_size, sha_text.split(".", 1)[0].lower()


def find_annex_file(files: Iterable[DriveFile], key: str) -> DriveFile | None:
    """在這批檔案裡找「**任一**」checksum 與 size 都和 key 相符的檔案。

    M3（review-cdb4a34）：Drive 允許同一個資料夾裡有多個**同名**檔（1.4i 實測），
    所以「同名就是它」不成立。舊的寫法用 `{name: file}` 建字典，後者覆蓋前者，
    於是住民只要在前綴放一個與釘選 key 同名的垃圾檔，就可能正好被選到 → 判定
    物件不存在 → 整輪中止，而且每一輪重放一次就是零成本的 DoS。

    正確的判斷是「**有任何一個**同名檔的 checksum 與 size 相符就算存在」：
    真的那份物件一定符合，而假的永遠不符合。
    """
    parsed = _key_size_and_sha(key)
    if parsed is None:
        return None
    key_size, key_sha = parsed
    for f in files:
        if f.name != key or f.is_folder:
            continue
        if f.sha256 is not None and f.sha256.lower() == key_sha and f.size == key_size:
            return f
    return None


def unique_manifest_sha(files: Sequence[DriveFile]) -> str | None:
    """這批同名主 manifest 的「唯一內容雜湊」；只有一種內容時回它，否則回 None。

    為什麼不是「恰好一個檔」（review-903d7e2 實測）：rclone 每輪 push 都會重寫
    manifest，**同一輪 push 內**因為 Drive 的列表落後也可能寫成兩份——實測
    1.75.1（committer workflow 釘的版本）與 1.69.3（Mac 上跑 e2e 的版本）都是
    每輪 push 後前綴裡有兩份同名、位元組相同的主 manifest，而且：

    - `git push` 一樣成功（不會出現 Duplicate object 之类的錯誤）；
    - `git clone` 一樣成功；
    - 下一輪 push 會由 rclone 自己把多餘的那份清掉。

    所以「檔案數 ≠ 1」不是健康的判斷依據，「**內容種類 ≠ 1**」才是：只要所有同名
    manifest 的內容都等於正式值，遠端狀態就是對的（`plan_sweep` 也不會搬它們，
    見 sweep 的 H1）。同名但內容相異仍然是注入物，照樣中止。
    """
    shas = {f.sha256 for f in files if not f.is_folder}
    if len(shas) != 1:
        return None
    only = next(iter(shas))
    return only.lower() if only else None


def _manifest_with_sha(files: Sequence[DriveFile], sha: str) -> DriveFile:
    """從同名 manifest 清單裡挑出內容雜湊等於 `sha` 的那一個（`unique_manifest_sha`
    已經確認過只有一種內容，這裡只是挑一個下載）。"""
    for f in files:
        if not f.is_folder and f.sha256 is not None and f.sha256.lower() == sha:
            return f
    raise MismatchError(f"找不到內容雜湊等於 {sha} 的 manifest")


def manifest_files_with_sha(files: Sequence[DriveFile], sha: str) -> list[DriveFile]:
    """同名主 manifest 裡，Drive checksum 等於 `sha` 的那些檔。"""
    want = sha.lower()
    return [
        f for f in files
        if not f.is_folder and f.sha256 is not None and f.sha256.lower() == want
    ]


def pinned_push_manifest(
    files: Sequence[DriveFile],
    expected_sha256: str | None,
    *,
    context: str,
) -> DriveFile:
    """H1：這一輪 push 的產物 = 內容雜湊等於 `expected_sha256` 的那份同名 manifest。

    **只認這一個內容。** 住民可以在同一輪 push 期間放一份「能解析、而且重放得出
    同樣 refs、但位元組不同（多一個換行之類）」的主 manifest 變體；舊規則要求
    「所有同名檔只有一種內容」，於是這份變體每一輪都讓 `verify_after_push` 失敗，
    把 promote 拖到下一輪（而且 pending 延後一輪才結算，收件匣的項目也跟著多等
    一輪）。現在改成「遠端必須有這一份，其他內容不算這一輪的產物」——變體再也
    拖不住 promote，也沒有任何一份變體會被轉正。

    `expected_sha256` 缺席（push 與補寫 pending 之間中斷）時退回
    `unique_manifest_sha`：只有一種內容就接受，有多種就中止（fail-closed，不用
    `createdTime` 猜）。
    """
    if expected_sha256:
        matched = manifest_files_with_sha(files, expected_sha256)
        if not matched:
            other = sorted({str(f.sha256) for f in files if not f.is_folder})
            raise MismatchError(
                f"{context}：遠端沒有任何一份主 manifest 的內容雜湊等於 pending 記載的"
                f" expected_manifest_sha256 ({expected_sha256})"
                f"（找到 {len(files)} 個同名檔，內容雜湊 {other or '（沒有 checksum）'}）"
                f"——這一輪 push 寫出去的位元組不在遠端，中止，不 promote"
            )
        # 內容雜湊相同的多份（rclone 同一輪 push 內因 Drive 列表落後會留下兩份）
        # 對驗證沒有分別，挑建立最早的那一份下載即可。
        return min(matched, key=_created_rank)

    sha = unique_manifest_sha(files)
    if sha is None:
        raise MismatchError(
            f"{context}：遠端主 manifest 內容不一致或缺少 checksum"
            f"（{len(files)} 個同名檔，內容雜湊 "
            f"{sorted({f.sha256 for f in files})}）；pending 沒有記"
            f" expected_manifest_sha256（這一轮在 push 與補寫之間中斷了？），"
            f"無法分辨哪一份才是這一輪 push 的產物——不猜，中止"
        )
    return _manifest_with_sha(files, sha)


def _created_rank(f: DriveFile) -> tuple[datetime, str]:
    """`created_at`（讀不出來排最後）＋ file id，確保結果穩定。"""
    try:
        return (f.created_at, f.id)
    except (ValueError, TypeError):
        return (datetime.max.replace(tzinfo=timezone.utc), f.id)


def verify_clone(
    git: AnnexGit,
    state: PinState,
    *,
    drive: DriveClient,
    prefix_folder_id: str,
    expected_annex_keys: frozenset[str] | set[str] | None = None,
) -> None:
    """提交流程第 5 步：驗證 clone 成果。

    - git.ls_remote() 之 ref 集合與值必須完全等於 state.refs。
    - 遠端主 manifest 必須**只有一種內容**且等於 state.manifest_sha256——不是
      「恰好一個檔」：rclone 同一輪 push 內可能因 Drive 列表落後留下兩份位元組
      相同的（實測 1.75.1 與 1.69.3），而 push 與 clone 在那種狀態下都正常。
    - 若 Drive 未提供 checksum，拋出 MismatchError 註明 Drive 尚未提供 checksum。
    - 若提供 expected_annex_keys，驗證 state.annex_keys 涵蓋所有預期之 annex 物件。
    - 否則拋出 MismatchError。
    """
    remote_refs = git.ls_remote()
    if remote_refs != state.refs:
        # impl1：這是「遠端已經往前、釘選值還在後面」——最常見的原因是某一輪
        # push 成功但第 11 步驗證沒過（那一輪沒有 promote，也沒有任何東西被
        # 改動）。這種狀態要嘛由下一輪的 settle 從 pending 結算回來，要嘛等人
        # 用 init-pin 重建釘選值；**不會**自己好。把怎麼辦寫進訊息。
        raise MismatchError(
            f"clone 後 ls-remote ({remote_refs}) 與正式釘選值 refs ({state.refs}) 不符："
            f"遠端已經領先釘選值。這一輪不動任何東西（不清掃、不 push）。"
            f"若某一輪 push 成功但驗證未完成，下一輪會從 pending 結算；"
            f"若已經沒有 pending，需要管理者確認後用 init-pin 重建釘選值"
        )

    manifest_name = f"GITMANIFEST--{state.repo_uuid}"
    m_files = drive.find_by_name(prefix_folder_id, manifest_name)
    if not m_files:
        raise MismatchError(f"遠端主 manifest 不存在: {manifest_name}")
    # 「恰好一種內容」而不是「恰好一個檔」：rclone 每輪 push 都重寫 manifest，
    # 同一輪 push 內也可能因 Drive 列表落後留下兩份位元組相同的（實測）。
    remote_sha = unique_manifest_sha(m_files)
    if remote_sha is None:
        raise MismatchError(
            f"遠端主 manifest 內容不一致或 Drive 尚未提供 checksum: 找到 {len(m_files)} 個"
            f"（內容雜湊 {sorted(str(x) for x in {f.sha256 for f in m_files})}，"
            f"預期所有同名檔的內容都等於 {state.manifest_sha256}）"
        )
    if remote_sha != state.manifest_sha256:
        raise MismatchError(
            f"遠端主 manifest 雜湊 ({remote_sha}) 與釘選值 ({state.manifest_sha256}) 不符"
        )

    if expected_annex_keys is not None:
        verify_annex_coverage(state.annex_keys, expected_annex_keys)


def _pinned_keys_not_on_drive(
    candidates: Sequence[DriveFile], annex_keys: Iterable[str],
) -> tuple[list[str], list[str]]:
    """把 key 分成「前綴裡一份都沒有」與「同名但內容不符」兩組。

    兩組都是「被釘選的 key 在 Drive 上沒有相符的檔案」，處置相同（自癒或中止），
    但報告要分得開：前者多半是檔案被搬走或被刪了，後者是前綴裡躺著一份同名
    垃圾檔（`find_annex_file` 以「任一檔相符就算存在」判定，見 M3）。
    """
    missing: list[str] = []
    unreadable: list[str] = []
    for key in sorted(set(annex_keys)):
        if not _is_plausible_annex_key(key):
            unreadable.append(f"{key}（形狀不合法）")
            continue
        by_name = tuple(f for f in candidates if f.name == key)
        if not by_name:
            missing.append(key)
            continue
        if find_annex_file(by_name, key) is None:
            unreadable.append(key)
    return missing, unreadable


@dataclass(frozen=True)
class RestoredFromQuarantine:
    """自癒：從隔離區搬回一個被釘選的 annex key 的紀錄。

    只記 key、隔離區裡那一檔的 file id 與它原本的父資料夾（D2 log 規則：不記
    內容）。`dry_run=True` 表示「記下來但沒有真的搬」。
    """

    key: str
    quarantine_file_id: str
    from_parent: str
    dry_run: bool = False


#: 隔離區的遞迴深度上限（`quarantine/<日期>/` 只有一層，但別讓它變成無上限的掃描）。
QUARANTINE_SCAN_DEPTH = 4


def restore_pinned_keys_from_quarantine(
    drive: DriveClient,
    quarantine_folder_id: str,
    prefix_folder_id: str,
    keys: Iterable[str],
    *,
    dry_run: bool = False,
) -> tuple[RestoredFromQuarantine, ...]:
    """隔離區裡有 sha256 與 size 都相符的檔，就把它搬回前綴（M1 自癒）。

    為什麼可以自動搬（review-final M1 b）：

    - **內容定址**：annex key 的形狀是 `SHA256E-s<size>--<sha256>`，所以「檔名相符
      ＋ Drive 的 `sha256Checksum` 相符 ＋ `size` 相符」三項都對上，就代表這一份的
      位元組**就是**釘選值記載的那個物件。搬回來的不可能是別人的東西。
    - **隔離是搬走不是刪除**（`apply_sweep` 用 addParents/removeParents，file id
      還在），而且隔離區保留 7 天（`quarantine_retention_days`），足以涵蓋住民為了
      讓提交流程停擺而反覆重來的時間。
    - 這是 recovery runbook「模式 0（從隔離區搬回真本）」的**自動版**：那一類
      事故（impl1 把真正的新世代 raw 當注入物搬走）從此會自己好，不必人工寫腳本。
      範圍仍然很窄——**只有釘選值記載的 key**，而且只從隔離區搬，所以永遠不會把
      注入物放回真本。

    找不到就什麼都不做（回空 tuple），由呼叫端照原樣中止。`dry_run` 只記錄會搬
    哪幾個，不真的搬（第 12 步的隔離區清理也會尊重 dry-run，所以這裡必須一致）。
    """
    wanted = {k for k in keys if _is_plausible_annex_key(k)}
    if not wanted or not quarantine_folder_id:
        return ()

    found: dict[str, DriveFile] = {}
    pending_dirs: list[tuple[str, int]] = [(quarantine_folder_id, 0)]
    while pending_dirs:
        folder_id, depth = pending_dirs.pop()
        try:
            children = drive.list_children(folder_id)
        except Exception:
            # 讀不到隔離區不是「有東西可搬」，也不是「確定沒有」——交給呼叫端照原樣
            # 中止並把情況寫進訊息。
            continue
        for child in children:
            if child.is_folder:
                if depth < QUARANTINE_SCAN_DEPTH:
                    pending_dirs.append((child.id, depth + 1))
                continue
            if child.name in wanted and child.name not in found and (
                # key 的形狀就是檔名，所以拿檔名當 key 問同一個規則：
                # sha256 與 size 都要和 key 內嵌的值相符。
                find_annex_file((child,), child.name) is not None
            ):
                found[child.name] = child

    restored: list[RestoredFromQuarantine] = []
    for key in sorted(found):
        f = found[key]
        from_parent = f.parents[0] if f.parents else ""
        if not from_parent:
            continue
        if not dry_run:
            drive.move(f.id, from_parent=from_parent, to_parent=prefix_folder_id)
        restored.append(
            RestoredFromQuarantine(
                key=key, quarantine_file_id=f.id, from_parent=from_parent,
                dry_run=dry_run,
            )
        )
    return tuple(restored)


def verify_pin_keys_on_drive(
    drive: DriveClient,
    prefix_folder_id: str,
    state: PinState,
    *,
    repo_listing: RepoListing | None = None,
    quarantine_folder_id: str | None = None,
    dry_run: bool = False,
) -> tuple[RestoredFromQuarantine, ...]:
    """第 5 步（e2e 修正）：釘選值記載的 annex 物件，**Drive 上**真的在嗎？

    為什麼不能只信 `git annex find --in=<uuid>`（location log）：

    - location log 是**提交流程自己寫的**（`git annex copy` 寫入、再隨 git-annex
      分支 push 出去），它宣稱「某個 key 在遠端」不等於遠端真的有該檔案；
    - 它的內容也會隨 consolidate（`annex.max-git-bundles`）而變動。e2e 實測
      （impl3／9.1）：**第一次提交成功之後每一輪**都在這裡
      `MismatchError: clone 後遠端 annex key 集合缺少釘選值記載之物件
      ['SHA256E-s19984--…']` 中止，而且不會自己好——只能人工重建釘選值。
      釘選值與 Drive 其實是對的，錯的是拿自己寫的帳本去對帳。

    所以這一檢查改成**問 Drive**：前綴底下必須有 `name == key` 的檔案，
    而且 Drive 的 `sha256Checksum` 與 `size` 要和 key 內嵌的一致（與
    清掃第 4 步判定 annex 物件用的是同一套規則）。少一個就是真的不見了 →
    中止（fail-closed，寧可不要靜靜地把真本當成完整的）。

    **M1（review-final）b：少一個時先到隔離區自癒，再照常繼續。**
    內容定址，所以從隔離區搬回來的一定是對的位元組（見
    `restore_pinned_keys_from_quarantine`）。找得到就搬回前綴、重查一次，然後
    **照常繼續這一輪**（回傳搬了哪幾個，寫進執行報告與健康檢查）；隔離區也沒有
    才中止——而且中止訊息要說明「隔離區也沒有」，因為隔離區 7 天後會被 purge，
    那之後就真的沒有了。沒給 `quarantine_folder_id` 時行為與原本完全相同。

    `repo_listing` 必須是**第 4 步 sweep 之後**重新列舉的前綴（M3）：sweep 之前
    的 listing 還含著被隔離掉的同名注入檔，用它比對會把「真的那份物件明明還在」
    判成不存在。（自癒搬回之後不看這份 listing：它是在搬回之前列的。）
    """
    if not state.annex_keys:
        return ()
    if repo_listing is not None:
        candidates: Sequence[DriveFile] = tuple(repo_listing.files)
    else:
        candidates = tuple(drive.list_children(prefix_folder_id))
    missing, unreadable = _pinned_keys_not_on_drive(candidates, state.annex_keys)

    restored: tuple[RestoredFromQuarantine, ...] = ()
    if missing or unreadable:
        restored = restore_pinned_keys_from_quarantine(
            drive,
            quarantine_folder_id or "",
            prefix_folder_id,
            missing + [k for k in unreadable if _is_plausible_annex_key(k)],
            dry_run=dry_run,
        )
        if restored and not dry_run:
            # 搬回來的檔案不在原本那份 listing 裡（它是在隔離區被列到的），
            # 所以要重新列舉一次前綴再判。
            missing, unreadable = _pinned_keys_not_on_drive(
                tuple(drive.list_children(prefix_folder_id)), state.annex_keys
            )

    if missing or unreadable:
        healed = [r.key for r in restored]
        if restored and dry_run:
            detail = (
                f"（dry-run：隔離區裡有 {len(healed)} 個相符的檔"
                f"（{healed[:3]}），但 dry-run 不搬任何東西，所以這一輪照原樣中止。）"
            )
        elif healed:
            detail = (
                f"已從隔離區搬回 {len(healed)} 個（{healed[:3]}），但這些仍然不符："
            )
        elif quarantine_folder_id:
            detail = (
                "隔離區裡也沒有 sha256 與 size 都相符的檔——注意隔離區 7 天後會被"
                " purge，屆時就真的沒有了（模式 0 要趁現在做）："
            )
        else:
            detail = ""
        raise MismatchError(
            f"釘選值記載的 annex 物件在 Drive 上不存在或內容不符"
            f"（不在 {prefix_folder_id}: {len(missing)} 個、不符: {len(unreadable)} 個）："
            f"{(missing + unreadable)[:3]}{detail}"
            f"真本與釘選值已不一致，需要管理者確認後重建釘選值（init-pin），不要自己好"
        )
    return restored


def verify_new_keys_on_drive(
    drive: DriveClient,
    prefix_folder_id: str,
    new_keys: frozenset[str] | set[str],
    *,
    attempts: int = 4,
    retry_delay_s: float = 10.0,
) -> int:
    """M1（review-25a48a9）：這一輪新寫的 annex 物件，**Drive 上**真的在嗎？

    為什麼需要：`annex_keys_in()` 讀的是 clone 下來的 git-annex location log，
    而那份 location log 是提交流程自己寫的。Drive 上的物件如果實際上不在
    （被誤隔離後又被 purge、Drive 端遺失……），location log 仍然會宣稱它在，
    覆蓋率檢查就會通過。所以這裡直接問 Drive：
    - 前綴底下有沒有 `name == key` 的檔案；
    - `sha256Checksum` 等於 key 內嵌的雜湊；
    - `size` 等於 key 內嵌的大小。

    缺任何一項都 raise `MismatchError`（中止這一輪，不 promote）。

    **必須在 push 之後呼叫**：第 3 步的 listing 是 push 之前的，拿它來比對會把
    「這一輪剛推上去的物件」全部判成不存在。

    impl1：Drive 的列表會**落後**寫入。剛 push 完就去列舉，新上傳的物件常常還
    沒出現（impl1 現場：上傳後 30 秒仍列不到，於是這裡誤判「物件不在」而中止，
    釘選值不轉正，留下一個沒人負責的 pending）。所以判定為缺物件時**重試**：
    重新列舉、隔一會兒再看。真的沒上傳成功的話，重試只會多花時間，最後照樣
    中止——方向仍然是 fail-closed。

    review-1926cd3-142fd04 L：原本 3 次 × 2 秒 = 6 秒，遠小於實測的 30 秒延遲，
    等於每次都會誤判而中止（雖然後續有 pending 與 settle 接手，方向是對的）。
    預設值改成 **4 次、每次等 10 秒（檢查落在第 0／10／30／60 秒，總計等待
    60 秒）**，涵蓋觀察到的 30 秒列表延遲並留一點餘裕。真的壞掉時這一輪就多花
    60 秒才中止，比起誤判留下一個沒人負責的 pending划算得多。
    """
    if not new_keys:
        return 0
    keys = sorted(new_keys)
    for attempt in range(max(1, attempts)):
        candidates = tuple(drive.list_children(prefix_folder_id))
        problems: list[str] = []
        for key in keys:
            if not _is_plausible_annex_key(key):
                problems.append(f"{key}（形狀不合法）")
                continue
            by_name = tuple(f for f in candidates if f.name == key)
            if not by_name:
                problems.append(f"{key}（Drive 上沒有同名檔案）")
                continue
            # M3：同名多檔時，只要**任一**檔 checksum 與 size 都相符就算存在。
            if find_annex_file(by_name, key) is None:
                problems.append(f"{key}（同名檔的 checksum／size 都不符 key 內嵌值）")
        if not problems:
            return len(keys)
        if attempt + 1 < max(1, attempts):
            time.sleep(retry_delay_s * (attempt + 1))
    waited = retry_delay_s * sum(range(1, max(1, attempts)))
    raise MismatchError(
        f"這一輪新寫的 annex 物件沒有真的在 Drive 上（{len(problems)} 個，"
        f"已重新列舉 {attempts} 次、等待約 {waited:.0f} 秒；Drive 的列表落後"
        f"實測可達 30 秒）：{problems[:3]}")


def precheck(
    drive: DriveClient,
    prefix_folder_id: str,
    manifest_name: str,
    state: PinState,
) -> None:
    """提交流程第 9 步：push 前預檢。

    - 只查名稱符合 manifest_name 的檔案。
    - 必須**只有一種內容**，且等於 state.manifest_sha256。
      （不是「恰好一個檔」：rclone 每輪 push 都重寫 manifest，同一輪 push 內也可能
      因 Drive 列表落後留下兩份位元組相同的——實測 1.75.1 與 1.69.3 都如此。）
    - 任何不符拋出 MismatchError（中止流程）。
    """
    files = drive.find_by_name(prefix_folder_id, manifest_name)
    if not files:
        raise MismatchError(f"預檢失敗：遠端 manifest 不存在 ({manifest_name})")

    sha = unique_manifest_sha(files)
    if sha is None:
        raise MismatchError(
            f"預檢失敗：遠端 manifest 內容不一致或 Drive 尚未提供 checksum "
            f"({len(files)} 個同名檔，內容雜湊 "
            f"{sorted(str(x) for x in {f.sha256 for f in files})}，"
            f"預期全部等於 {state.manifest_sha256})"
        )

    if sha != state.manifest_sha256:
        raise MismatchError(
            f"預檢失敗：遠端 manifest 雜湊 ({sha}) 與釘選值 ({state.manifest_sha256}) 不符"
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
    push_started_at: str | datetime,
    *,
    workdir: Path,
    expected_annex_keys: frozenset[str] | set[str] | None = None,
    pushed_annex_keys: frozenset[str] | set[str] | None = None,
    expected_manifest_sha256: str | None = None,
) -> PushVerification:
    """提交流程第 10 步：push 後遠端狀態驗證。

    規則（review-1.2-1.6 H3、review-1.4f3 M1、review-g3c M5）：
    1. ls_remote == local_refs（全部 ref 相等）。
    2. 重新列舉，主 manifest 恰好一個，解析出 active 與 removed。
    3. 連續性檢查（check_manifest_continuity）：
       - removed ⊇ state.removed_bundles。
       - newly_removed ⊆ state.active_bundles。
       - state.active_bundles ⊆ new_active ∪ new_removed（舊 active 不得憑空消失）。
    4. active 裡不在 state.active_bundles 的新增 bundle：
       - 其 created_at >= push_started_at。
       - listing_before 裡沒有同名檔。
       - 篩選比對名稱與雜湊相符之檔案。
    5. 下載 active bundle（篩選符合雜湊者），連同既有 active 依序重放，驗證 refs == local_refs。
    6. 若提供 expected_annex_keys，驗證「這一輪 push 出去的 annex key 集合」涵蓋所有
       預期之 annex 物件。比較對象是 `pushed_annex_keys`（即 pending 記的那一份），
       不是 `state.annex_keys`——正式釘選值在第 12 步 promote 之前本來就落後這一輪
       剛寫進去的 key，拿它當比較對象會讓「有新增物件」的所有輪次都失敗。
    - 任何一條不符拋出 MismatchError（待定釘選值保留，留待下一輪 settle 結算）。

    **H1（review-5d4dd52）：第 2 條只認 `expected_manifest_sha256`。**

    提交流程 push 之後從本機 `.git/annex/git-remote-annex/<uuid>/manifest` 讀到
    自己剛寫出去的位元組（實測與遠端那份 sha256 相同），把它記進 pending。
    舊規則要求「所有同名主 manifest 只有一種內容」，於是住民在 push 期間放一份
    位元組不同的變體就能讓這一輪**永遠不 promote**（pending 延一輪、收件匣項目
    多等一輪，而且他可以一再重來）。現在改成「遠端必須有 pending 記載的那一份；
    其他內容不是這一輪的產物，不讓驗證失敗」——變體既拖不住 promote，也不可能
    被轉正成正式值。
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    if expected_annex_keys is not None:
        # 比較對象：優先用這一輪 push 出去的 key 集合（pending 那份），沒有才退回
        # 正式釘選值（此時只驗「既有 key 沒被弄丟」）。
        verify_annex_coverage(
            pushed_annex_keys if pushed_annex_keys is not None else state.annex_keys,
            expected_annex_keys,
        )

    if isinstance(push_started_at, str):
        push_dt = parse_rfc3339(push_started_at)
    else:
        push_dt = push_started_at

    # 1. ls-remote 嚴格比對
    remote_refs = git.ls_remote()
    if remote_refs != local_refs:
        raise MismatchError(
            f"push 後 ls-remote ({remote_refs}) 與本地 refs ({local_refs}) 不符"
        )

    # 2. 重新尋找主 manifest。H1：只認 pending 記載的那一份位元組；其他內容
    #    （住民在 push 期間放的變體）不是這一輪的產物，不讓驗證失敗。
    manifest_name = f"GITMANIFEST--{state.repo_uuid}"
    m_files = drive.find_by_name(listing_before.prefix_folder_id, manifest_name)
    pushed_file = pinned_push_manifest(
        m_files, expected_manifest_sha256, context="push 後主 manifest 驗證"
    )
    pushed_sha = pushed_file.sha256.lower() if pushed_file.sha256 else None

    m_data = drive.download_bytes(pushed_file.id, max_bytes=1024 * 1024)
    new_manifest_sha = hashlib.sha256(m_data).hexdigest().lower()
    if pushed_sha is not None and new_manifest_sha != pushed_sha:
        raise MismatchError(
            f"push 後主 manifest 的 Drive checksum ({pushed_sha}) 與實際內容雜湊 "
            f"({new_manifest_sha}) 不符"
        )
    parsed_manifest = parse_manifest(m_data, repo_uuid=state.repo_uuid)

    # 3. 連續性檢查 (M3, M5)
    check_manifest_continuity(state, parsed_manifest)

    # 4. 檢查新增之 active bundle
    existing_active = set(state.active_bundles)
    listing_before_names = {f.name for f in listing_before.files}

    new_bundles = [b for b in parsed_manifest.active if b not in existing_active]
    for b_name in new_bundles:
        if b_name in listing_before_names:
            raise MismatchError(f"新增之 active bundle '{b_name}' 已存在於 push 前的 listing 中")

        b_info = parse_bundle_name(b_name)
        if not b_info or b_info.repo_uuid != state.repo_uuid:
            raise MismatchError(f"新增之 active bundle 檔名不合法: {b_name}")

        b_files = drive.find_by_name(listing_before.prefix_folder_id, b_name)
        matched = [
            f for f in b_files
            if f.sha256 == b_info.sha256 and f.size == b_info.size
        ]
        if not matched:
            raise MismatchError(f"找不到符合新增 active bundle 宣告與雜湊之檔案: {b_name}")

        bf = matched[0]
        if bf.created_at < push_dt:
            raise MismatchError(
                f"新增之 bundle '{b_name}' created_at ({bf.created_time}) 早於 push 開始時間 ({push_started_at})"
            )

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
                matched = [
                    f for f in found
                    if f.sha256 == b_info.sha256 and f.size == b_info.size
                ]
                if not matched:
                    raise MismatchError(f"遠端找不到符合雜湊之 active bundle: {b_name}")
                drive.download(matched[0].id, dest, max_bytes=256 * 1024 * 1024)
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
        shutil.rmtree(temp_dir, ignore_errors=True)
