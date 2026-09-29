"""AiStorage 核對、預檢與 push 後驗證模組。

依據規格：
- docs/impl/group3-modules.md 第 3.5 節
- design.md D2（第 5 步 clone 驗證、第 9 步預檢、第 10 步 push 後重放驗證）
- review-1.2-1.6 H3（push 後以 ls-remote 驗證）
- review-1.4f3 M1（push 後驗證：active 清單比對、created_time、removed 包含關係、重放比對）
- review-cdb4a34 M3（同名檔：任一檔 checksum＋size 相符就算存在）
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import tempfile
import time
from typing import TYPE_CHECKING, Iterable
from datetime import datetime

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
    - 遠端主 manifest 必須恰好一個且內容雜湊等於 state.manifest_sha256。
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
    if len(m_files) != 1:
        raise MismatchError(f"遠端主 manifest 數量異常: 找到 {len(m_files)} 個 (預期恰好 1 個)")

    mf = m_files[0]
    if mf.sha256 is None:
        raise MismatchError(f"Drive 尚未提供 checksum (sha256 is None): {mf.name}")

    if mf.sha256 != state.manifest_sha256:
        raise MismatchError(
            f"遠端主 manifest 雜湊 ({mf.sha256}) 與釘選值 ({state.manifest_sha256}) 不符"
        )

    if expected_annex_keys is not None:
        verify_annex_coverage(state.annex_keys, expected_annex_keys)


def verify_pin_keys_on_drive(
    drive: DriveClient,
    prefix_folder_id: str,
    state: PinState,
    *,
    repo_listing: RepoListing | None = None,
) -> None:
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

    `repo_listing` 必須是**第 4 步 sweep 之後**重新列舉的前綴（M3）：sweep 之前
    的 listing 還含著被隔離掉的同名注入檔，用它比對會把「真的那份物件明明還在」
    判成不存在。
    """
    if not state.annex_keys:
        return
    if repo_listing is not None:
        candidates = tuple(repo_listing.files)
    else:
        candidates = tuple(drive.list_children(prefix_folder_id))
    missing: list[str] = []
    unreadable: list[str] = []
    for key in sorted(state.annex_keys):
        if not _is_plausible_annex_key(key):
            unreadable.append(f"{key}（形狀不合法）")
            continue
        by_name = tuple(f for f in candidates if f.name == key)
        if not by_name:
            missing.append(key)
            continue
        if find_annex_file(by_name, key) is None:
            unreadable.append(key)
    if missing or unreadable:
        raise MismatchError(
            f"釘選值記載的 annex 物件在 Drive 上不存在或內容不符"
            f"（不在 {prefix_folder_id}: {len(missing)} 個、不符: {len(unreadable)} 個）："
            f"{(missing + unreadable)[:3]}；真本與釘選值已不一致，"
            "需要管理者確認後重建釘選值（init-pin），不要自己好"
        )


def verify_new_keys_on_drive(
    drive: DriveClient,
    prefix_folder_id: str,
    new_keys: frozenset[str] | set[str],
    *,
    attempts: int = 3,
    retry_delay_s: float = 2.0,
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
    重新列舉、隔一會兒再看。真的沒上傳成功的話，重試只會多花幾秒，最後照樣
    中止——方向仍然是 fail-closed。
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
    raise MismatchError(
        f"這一輪新寫的 annex 物件沒有真的在 Drive 上（{len(problems)} 個，"
        f"已重新列舉 {attempts} 次）：{problems[:3]}")


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

    f = files[0]
    if f.sha256 is None:
        raise MismatchError(f"Drive 尚未提供 checksum (sha256 is None): {f.name}")

    if f.sha256 != state.manifest_sha256:
        raise MismatchError(
            f"預檢失敗：遠端 manifest 雜湊 ({f.sha256}) 與釘選值 ({state.manifest_sha256}) 不符"
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

    # 2. 重新尋找主 manifest
    manifest_name = f"GITMANIFEST--{state.repo_uuid}"
    m_files = drive.find_by_name(listing_before.prefix_folder_id, manifest_name)
    if len(m_files) != 1:
        raise MismatchError(f"push 後遠端主 manifest 數量異常 ({len(m_files)} != 1)")

    m_data = drive.download_bytes(m_files[0].id, max_bytes=1024 * 1024)
    new_manifest_sha = hashlib.sha256(m_data).hexdigest().lower()
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
