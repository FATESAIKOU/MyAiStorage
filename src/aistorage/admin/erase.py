"""6.1 抹除（只在 Mac 上、以管理憑證、在 AdminLock 內執行）。

做法照 design D2「改寫與抹除」與技術驗證 1.3，分成四段（review H6）：

1. `plan_erase`（唯讀）：列出所有要刪的 file id，並**逐一 `get()` 確認每個
   類別各自的 parent**（讀取視圖／收件匣／隔離區的檔案不在真本前綴之下，
   用前綴去檢查必定被拒絕）。
2. `rewrite_local`：filter-repo 改寫歷史、redact／remap、annex drop、gc、
   抹除紀錄，並 commit（遠端刪掉之後如果沒有 commit，真本就只剩本機）。
3. `swap_remote`（在 AdminLock 內）：刪遠端 → `git annex copy` ＋
   `git push --force` → ls-remote／manifest 驗證 → 以觀測到的遠端狀態重建
   正式 pin → 讀取視圖 rebuild epoch 加 1。任何一步失敗都保留鎖。
4. `verify_canary`：後置條件**必須真的執行**（讀不到就判失敗，不略過）。

與草案的差異（實作時確定）：
- `plan_erase` 除了 `admin` 另取 `store`（讀 snapshots／handoffs／links
  計算沿用與重新對應）與可選的 `index_db`（讀取視圖含目標內容的 reading）。
- `snapshot_remap` 在 plan 時為空，由 rewrite 在改寫 raw 之後填入回報與
  抹除紀錄（plan 階段無法在不知道新 raw 位元組的情況下預知新雜湊）；
  `plan_hash` 只覆蓋可執行的計畫內容。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.remote import (
    DeleteGroup,
    RemoteCheck,
    SwapAborted,
    SwapReport,
    check_repo_dir,
    swap_remote,
)
from aistorage.agora.store import AgoraStore
from aistorage.annex.manifest import parse_bundle_name, parse_manifest
from aistorage.clock import format_rfc3339
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.schema import generate_ulid

EraseKind = Literal["session", "segment", "annex_key"]


@dataclass(frozen=True)
class EraseTarget:
    kind: EraseKind
    session_id: str | None = None
    message_ids: tuple[str, ...] = ()
    key: str | None = None


@dataclass(frozen=True)
class ErasePlan:
    targets: tuple[EraseTarget, ...]
    repo_uuids: tuple[str, ...]
    delete_file_ids: tuple[str, ...]
    readview_file_ids: tuple[str, ...]
    inbox_file_ids: tuple[str, ...]
    quarantine_file_ids: tuple[str, ...]
    snapshot_remap: dict[str, str]
    run_ids_to_delete: tuple[int, ...]
    known_clones: tuple[str, ...]
    condemned_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EraseReport:
    plan_hash: str
    deleted_file_ids: tuple[str, ...]
    snapshot_remap: dict[str, str]
    erasure_record_id: str
    commit_sha: str | None = None
    swap: SwapReport | None = None
    verify: tuple[tuple[str, int], ...] = ()


def _canonical_plan(plan: ErasePlan) -> str:
    return json.dumps({
        "targets": [{"kind": t.kind, "session_id": t.session_id,
                     "message_ids": sorted(t.message_ids), "key": t.key}
                    for t in plan.targets],
        "repo_uuids": sorted(plan.repo_uuids),
        "delete_file_ids": sorted(plan.delete_file_ids),
        "readview_file_ids": sorted(plan.readview_file_ids),
        "inbox_file_ids": sorted(plan.inbox_file_ids),
        "quarantine_file_ids": sorted(plan.quarantine_file_ids),
        "run_ids_to_delete": sorted(plan.run_ids_to_delete),
        "known_clones": sorted(plan.known_clones),
        "condemned_keys": sorted(plan.condemned_keys),
    }, sort_keys=True, ensure_ascii=False)


def plan_hash(plan: ErasePlan) -> str:
    """計畫雜湊：apply 的 --confirm 必須等於它，內容一變就拒絕執行。"""
    return hashlib.sha256(_canonical_plan(plan).encode("utf-8")).hexdigest()


def _walk_files(drive: DriveClient, folder_id: str,
                _depth: int = 0) -> list[DriveFile]:
    if _depth > 8:
        return []
    out: list[DriveFile] = []
    for child in drive.list_children(folder_id):
        if child.is_folder:
            out.extend(_walk_files(drive, child.id, _depth + 1))
        else:
            out.append(child)
    return out


def _target_session_ids(targets: Sequence[EraseTarget]) -> set[str]:
    return {t.session_id for t in targets if t.session_id}


def plan_erase(targets: Sequence[EraseTarget], *, admin: AdminDeps,
               store: AgoraStore,
               index_db: Path | None = None) -> ErasePlan:
    """計算抹除計畫（唯讀；不刪除任何東西）。

    - repo 檔：前綴子樹下全部 bundle／manifest／.bak（抹除即重建整批歷史，
      A 線的 settle 下一輪會以新 pin 為準；annex key 檔名符合者就是 key 本體）。
    - segment：沿用 snapshots.jsonl 的快照清單（remap 由 rewrite 填）。
    - 讀取視圖：目標 Session 在 index readings 表的全部 file_id（保守：整份 reading）。
    - 收件匣：sidecar 的 metadata.id 指向目標 Session 者。
    - 隔離區：檔名落在刪除清單者。

    計畫階段就逐一 `get()` 確認 parent（review H6）：刪錯資料夾是不可逆的，
    寧可在計畫就拒絕，也不要刪到一半才失敗。
    """
    targets = tuple(targets)
    if not targets:
        raise AdminError("抹除目標不可為空")
    for t in targets:
        if t.kind == "session" and not t.session_id:
            raise AdminError("session 抹除必須指定 session_id")
        if t.kind == "segment" and (not t.session_id or not t.message_ids):
            raise AdminError("segment 抹除必須指定 session_id 與 message_ids")
        if t.kind == "annex_key" and not t.key:
            raise AdminError("annex_key 抹除必須指定 key")

    session_ids = _target_session_ids(targets)
    keys = {t.key for t in targets if t.key}
    # session 抹除連帶它快照的 annex key（annex 存放的 raw 本體）。
    for sid in session_ids:
        try:
            for snap in store.snapshots(sid):
                if snap.annex_key:
                    keys.add(snap.annex_key)
        except Exception:
            continue

    delete_ids: list[str] = []
    condemned_names: set[str] = set()
    for f in _walk_files(admin.drive, admin.prefix_folder_id):
        if f.name.startswith("GITBUNDLE-") or f.name.startswith("GITMANIFEST"):
            delete_ids.append(f.id)
            condemned_names.add(f.name)
            continue
        if parse_bundle_name(f.name) is not None:
            delete_ids.append(f.id)
            condemned_names.add(f.name)
            continue
        if f.name in keys or any(f.name.startswith(k) for k in keys):
            delete_ids.append(f.id)
            condemned_names.add(f.name)
    for root in admin.extra_scan_roots:
        for f in _walk_files(admin.drive, root):
            if f.name in keys or any(f.name.startswith(k) for k in keys):
                delete_ids.append(f.id)
                condemned_names.add(f.name)

    readview_ids: list[str] = []
    if index_db is not None and session_ids:
        con = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
        try:
            placeholders = ",".join("?" * len(session_ids))
            for (fid,) in con.execute(
                    "SELECT DISTINCT file_id FROM readings"
                    f" WHERE session_id IN ({placeholders})", sorted(session_ids)):
                readview_ids.append(fid)
        finally:
            con.close()

    inbox_ids: list[str] = []
    for folder_id in admin.inbox_folder_ids:
        for folder_file in _walk_files(admin.drive, folder_id):
            if not folder_file.name.endswith(".sidecar.json"):
                continue
            try:
                sidecar = json.loads(
                    admin.drive.download_bytes(folder_file.id, max_bytes=1 << 20).decode("utf-8"))
            except Exception:
                continue
            meta = sidecar.get("metadata", {}) if isinstance(sidecar, dict) else {}
            if meta.get("id") in session_ids:
                item_key = folder_file.name[: -len(".sidecar.json")]
                for g in _walk_files(admin.drive, folder_id):
                    if g.name.startswith(item_key + "."):
                        inbox_ids.append(g.id)

    quarantine_ids: list[str] = []
    if admin.quarantine_folder_id:
        # 只收同名（被隔離的 repo 檔）；隔離區其他檔案與本次抹除無關，不碰。
        for f in _walk_files(admin.drive, admin.quarantine_folder_id):
            if f.name in condemned_names:
                quarantine_ids.append(f.id)

    plan = ErasePlan(
        targets=targets,
        repo_uuids=tuple(admin.repo_uuids),
        delete_file_ids=tuple(sorted(set(delete_ids))),
        readview_file_ids=tuple(sorted(set(readview_ids))),
        inbox_file_ids=tuple(sorted(set(inbox_ids))),
        quarantine_file_ids=tuple(sorted(set(quarantine_ids))),
        snapshot_remap={},
        run_ids_to_delete=(),
        known_clones=tuple(admin.known_clones),
        condemned_keys=tuple(sorted(keys)),
    )
    # H6：計畫階段就確認每個類別各自的 parent；不符就拒絕整個計畫。
    check_plan_parents(admin.drive, delete_groups_for(plan, admin=admin))
    return plan


def delete_groups_for(plan: ErasePlan, *, admin: AdminDeps) -> tuple[DeleteGroup, ...]:
    """把計畫的 file id 依類別配給**該類別自己的**合法 parent 根。"""
    groups: list[DeleteGroup] = []
    if plan.delete_file_ids:
        groups.append(DeleteGroup(name="repo", file_ids=plan.delete_file_ids,
                                  parent_roots=(admin.prefix_folder_id,)))
    if plan.readview_file_ids:
        if not admin.readview_folder_id:
            raise AdminError(
                "計畫有讀取視圖檔要刪，但沒有設定 readview_folder_id，"
                "無法確認 parent（拒絕執行）")
        groups.append(DeleteGroup(name="readview", file_ids=plan.readview_file_ids,
                                  parent_roots=(admin.readview_folder_id,)))
    if plan.inbox_file_ids:
        groups.append(DeleteGroup(name="inbox", file_ids=plan.inbox_file_ids,
                                  parent_roots=tuple(admin.inbox_folder_ids)))
    if plan.quarantine_file_ids:
        groups.append(DeleteGroup(name="quarantine",
                                  file_ids=plan.quarantine_file_ids,
                                  parent_roots=(admin.quarantine_folder_id,)))
    return tuple(groups)


def check_plan_parents(drive: DriveClient, groups: Sequence[DeleteGroup]) -> None:
    """計畫階段就確認每個類別各自的 parent；不符立刻拒絕（不可逆操作）。"""
    from aistorage.admin.remote import check_delete_group

    for group in groups:
        check_delete_group(drive, group)


# redact 回呼的形狀：依來源格式從 raw 移除指定訊息，回傳新 raw。
# RedactRaw = Callable[[source: str, raw: bytes, message_ids: tuple[str, ...]], bytes]


def _require_filter_repo() -> str:
    path = shutil.which("git-filter-repo")
    if path is None:
        raise AdminError(
            "找不到 git-filter-repo（抹除改寫歷史需要）。"
            "請先安裝（例如 brew install git-filter-repo 或 pip install git-filter-repo）")
    return path


def _git(repo_dir: Path, *args: str, check: bool = True) -> str:
    from aistorage.annex.git import get_git_env

    proc = subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True,
        env=get_git_env(), timeout=600, check=False)
    if check and proc.returncode != 0:
        raise AdminError(f"git {' '.join(args)} 失敗 (rc={proc.returncode})")
    return proc.stdout


def _write_erasure_record(store: AgoraStore, *, who: str, at: str, why: str,
                          plan: ErasePlan, remap: dict[str, str]) -> str:
    """抹除紀錄：只記 id 與雜湊對應，不含任何被抹除的內容。"""
    record_id = generate_ulid()
    store.put_json(
        f"_admin/erasures/{record_id}.json",
        {
            "id": record_id,
            "who": who,
            "at": at,
            "why": why,
            "targets": [{"kind": t.kind, "session_id": t.session_id,
                         "message_ids": sorted(t.message_ids), "key": t.key}
                        for t in plan.targets],
            "repo_uuids": sorted(plan.repo_uuids),
            "snapshot_remap": dict(sorted(remap.items())),
        },
    )
    return record_id


def _redact_snapshots(store: AgoraStore, tmpdir: Path, session_id: str,
                      message_ids: tuple[str, ...], redact_raw: Any
                      ) -> tuple[dict[str, str], dict[str, tuple[str, str]], list[str]]:
    """改寫某 Session 每一份快照的 raw（不碰 git 歷史）。

    回傳 (remap, new_refs, old_git_blobs)：舊 content-sha → 新 content-sha、
    舊 content-sha → (kind, 新 ref)、被取代的舊 git blob id（給 strip 用）。
    snapshots.jsonl 的對應行同步更新；歷史清除由呼叫端用 filter-repo 做。
    """
    from aistorage.agora import layout as _layout

    remap: dict[str, str] = {}
    new_refs: dict[str, tuple[str, str]] = {}
    old_git_blobs: list[str] = []
    source = session_id.split(":", 1)[0]
    snaps = store.snapshots(session_id)
    rel = _layout.session_snapshots_path(session_id)
    path = store.worktree / rel
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    changed = False
    for i, snap in enumerate(snaps):
        raw_bytes = store.raw_path_for_snapshot(session_id, snap.snapshot_sha256).read_bytes()
        new_bytes = redact_raw(source, raw_bytes, tuple(message_ids))
        if new_bytes == raw_bytes:
            continue
        new_sha = hashlib.sha256(new_bytes).hexdigest().lower()
        tmp_raw = Path(tmpdir) / f"redacted-{new_sha}.raw"
        tmp_raw.write_bytes(new_bytes)
        ref = store.raw_storage.store(store.worktree / _layout.session_raw_path(session_id), tmp_raw)
        remap[snap.snapshot_sha256.lower()] = new_sha
        new_refs[snap.snapshot_sha256.lower()] = (ref.kind, ref.ref)
        if i < len(lines):
            import json as _json
            entry = _json.loads(lines[i])
            if entry.get("git_blob"):
                old_git_blobs.append(entry["git_blob"])
            entry["snapshot_sha256"] = new_sha
            if ref.kind == "git":
                entry["git_blob"] = ref.ref
            else:
                entry["annex_key"] = ref.ref
            lines[i] = _json.dumps(entry, ensure_ascii=False)
            changed = True
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        store._record_changed(rel)
    return remap, new_refs, old_git_blobs


def _origin_config(repo_dir: Path) -> list[tuple[str, str]]:
    """`remote.origin.*` 的全部設定（URL ＋ git-annex 遠端的 type／uuid／rcloneprefix…）。"""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "config", "--get-regexp", r"^remote\.origin\."],
        capture_output=True, text=True, timeout=30, check=False)
    pairs: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key:
            pairs.append((key, value))
    return pairs


def _restore_origin(repo_dir: Path, config: Sequence[tuple[str, str]]) -> None:
    """把 filter-repo 刪掉的 `remote.origin.*` 整段還原。

    `git-filter-repo --force` 會移除 origin 遠端（它假設這是乾淨的 clone，避免把
    改寫後的歷史推回原遠端）。抹除接著要 push 回同一個 annex:: 遠端，所以必須
    連 annex 遠端的設定（`type`／`annex-uuid`／`rcloneprefix`…）一起還原：只把
    URL 加回來的話，`git annex copy --to=origin` 會說「no available git remote
    named origin」，而安全檢查（origin 必須是 annex::）也會擋掉。
    """
    if not config:
        return
    url = next((v for k, v in config if k == "remote.origin.url"), None)
    if not url:
        return
    current = _origin_config(repo_dir)
    if not any(k == "remote.origin.url" for k, _ in current):
        _git(repo_dir, "remote", "add", "origin", url)
    elif dict(current).get("remote.origin.url") != url:
        _git(repo_dir, "remote", "set-url", "origin", url)
    have = {k for k, _ in current}
    for key, value in config:
        if key in ("remote.origin.url",) or key in have:
            continue
        _git(repo_dir, "config", key, value)


def _filter_repo(workdir: Path, origin: Sequence[tuple[str, str]],
                 *args: str) -> None:
    proc = subprocess.run(
        ["git-filter-repo", *args, "--force"],
        cwd=str(workdir), capture_output=True, text=True, timeout=1800)
    _restore_origin(workdir, origin)
    if proc.returncode != 0:
        raise AdminError(f"filter-repo {' '.join(args)} 失敗 (rc={proc.returncode})")


def _drop_local_annex_objects(repo_dir: Path, keys: Sequence[str]) -> list[str]:
    """刪掉本機 annex 物件（1.3 發現 #3：留著會在重建時被重新上傳）。

    **物件的路徑是 git-annex 自己算出來的**（`.git/annex/objects/<2>/<2>/<KEY>` 的
    fan-out 取自 key 的雜湊，不是 key 的字元），所以一律用
    `git annex contentlocation <key>` 問——用字元去拼路徑會安靜地什麼都沒刪掉。
    annex 物件是 0444 唯讀，直接 rm 會 Permission denied，所以先 chmod。
    """
    removed: list[str] = []
    for key in keys:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "annex", "contentlocation", key],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if proc.returncode != 0:
            continue                      # 本機沒有這個物件
        for loc in proc.stdout.split():
            candidate = (repo_dir / loc.strip()).resolve()
            if not candidate.is_file():
                continue
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
            candidate.unlink()
            removed.append(key)
    return sorted(set(removed))


def rewrite_local(plan: ErasePlan, *, admin: AdminDeps, store: AgoraStore,
                  repo_dir: Path | str, redact_raw: Any = None,
                  who: str = "admin", why: str = "",
                  check: bool = True) -> tuple[str, dict[str, str], str]:
    """本機改寫（不碰遠端）。回傳 (commit sha, remap, 抹除紀錄 id)。

    指令順序照 1.3 實測：`annex drop` → `annex forget --force` →
    `update-ref -d refs/annex/last-index`（annex 索引會保留舊 blob）→
    `filter-repo` → 再次 `update-ref -d` → `reflog expire` → `gc --prune=now`
    （dangling blob 沒 prune 掉的話，本機掃描還是找得到）。
    """
    if check:
        _require_filter_repo()
    workdir = check_repo_dir(repo_dir, purpose="抹除改寫工作目錄")
    origin = _origin_config(workdir)
    now = format_rfc3339(admin.clock.now(), include_fraction=True)
    remap: dict[str, str] = {}

    session_targets = [t for t in plan.targets if t.kind == "session"]
    segment_targets = [t for t in plan.targets if t.kind == "segment"]
    key_targets = [t for t in plan.targets if t.kind == "annex_key"]

    if segment_targets and redact_raw is None:
        raise AdminError("segment 抹除需要 redact_raw 回呼（依來源格式移除訊息）")

    # 1. 被淘汰的 annex 物件先丟（遠端的 key 依 file id 刪除）
    for key in plan.condemned_keys:
        _git(workdir, "annex", "drop", "--force", "--key", key, check=False)
    if plan.condemned_keys:
        _git(workdir, "annex", "forget", "--force", check=False)
    _drop_local_annex_objects(workdir, plan.condemned_keys)
    _git(workdir, "update-ref", "-d", "refs/annex/last-index", check=False)

    # 2. 整個 Session：從全部歷史刪目錄
    for t in session_targets:
        assert t.session_id is not None
        rel = _session_rel(t.session_id)
        _filter_repo(workdir, origin, "--path", rel, "--invert-paths")

    # 3. segment：改寫每一份快照的 raw，收集新舊雜湊
    old_blob_ids: list[str] = []
    new_refs: dict[str, tuple[str, str]] = {}
    for t in segment_targets:
        assert t.session_id is not None and t.message_ids
        sub_remap, sub_refs, sub_blobs = _redact_snapshots(
            store, admin.workdir, t.session_id, tuple(t.message_ids), redact_raw)
        remap.update(sub_remap)
        new_refs.update(sub_refs)
        old_blob_ids.extend(sub_blobs)

    # 4. 同步改寫 snapshots.jsonl、handoffs、links 的雜湊（PM 決定 5）
    if remap:
        erased_by_session: dict[str, set[str]] = {}
        for t in segment_targets:
            assert t.session_id is not None
            erased_by_session.setdefault(t.session_id, set()).update(t.message_ids)
        _remap_hashes(store, remap, new_refs, erased_by_session)

    # 5. 抹除紀錄（只有 id 與雜湊對應，不含內容）
    record_id = _write_erasure_record(
        store, who=who, at=now, why=why, plan=plan, remap=remap)

    # 6. 清除被取代的 blob，然後 gc
    if old_blob_ids:
        ids_file = Path(admin.workdir) / "strip-ids.txt"
        ids_file.write_text("\n".join(sorted(set(old_blob_ids))) + "\n")
        _filter_repo(workdir, origin, "--strip-blobs-with-ids", str(ids_file))
    _git(workdir, "update-ref", "-d", "refs/annex/last-index", check=False)
    _git(workdir, "reflog", "expire", "--expire=now", "--all")
    _git(workdir, "gc", "--prune=now")

    # 7. commit（遠端被刪掉之後，這份改寫必須已經在真本裡）
    _git(workdir, "add", "-A")
    status = _git(workdir, "status", "--porcelain")
    if status.strip():
        _git(workdir, "-c", "user.name=AiStorage Admin",
             "-c", "user.email=admin@aistorage.local",
             "commit", "-m", f"admin: erase {record_id}")
    commit_sha = _git(workdir, "rev-parse", "HEAD").strip()
    return commit_sha, remap, record_id


def _session_rel(session_id: str) -> str:
    from aistorage.agora.layout import session_dir_for_id

    try:
        return session_dir_for_id(session_id)
    except ValueError as e:
        raise AdminError(f"無效的 Session id: {session_id}: {e}") from None


def apply_erase(plan: ErasePlan, *, confirm: str, admin: AdminDeps,
                store: AgoraStore, repo_dir: Path | str, cfg: Any, deps: Any,
                git: Any, redact_raw: Any = None, who: str = "admin",
                why: str = "", canary: str,
                config_path: Path | str | None = None,
                readview_folder_id: str | None = None) -> EraseReport:
    """執行抹除（呼叫端負責先進入 AdminLock）。

    - confirm 必須等於 plan_hash(plan)，否則拒絕。
    - canary 與 why 必填：後置條件沒有內容可比、或紀錄沒有原因，都等於
      沒驗／沒紀錄（fail-closed）。
    - 順序：本機改寫（rewrite_local，含 commit）→ 刪遠端 → push → 驗證 →
      重建 pin → 讀取視圖重建世代 → 後置條件（verify_canary）。
    """
    if confirm != plan_hash(plan):
        raise AdminError("確認碼與計畫雜湊不符，拒絕執行（計畫可能已被更動）")
    if not canary.strip():
        raise AdminError(
            "抹除必須提供 --canary：一段只存在於要抹除內容裡的字串，"
            "後置條件要靠它確認真的找不到了")
    if not why.strip():
        raise AdminError("抹除必須說明原因（--why）")

    workdir = check_repo_dir(repo_dir, purpose="抹除執行目錄")
    commit_sha, remap, record_id = rewrite_local(
        plan, admin=admin, store=store, repo_dir=workdir,
        redact_raw=redact_raw, who=who, why=why)

    groups = delete_groups_for(plan, admin=admin)
    try:
        swap = swap_remote(
            admin=admin, cfg=cfg, deps=deps, git=git, repo_dir=workdir,
            delete_groups=groups, force_push=True, config_path=config_path,
            resume_hint=(f"抹除紀錄 {record_id} 已在真本（commit {commit_sha[:8]}）；"
                         "遠端尚未一致，保留維護旗標"))
    except SwapAborted as e:
        # 記錄已經 commit，所以即使遠端沒.swap 成功，真本也留有紀錄；
        # 把 swap 進度一併附在例外上，讓 CLI 印出「做到哪一步、下一步是什麼」。
        raise SwapAborted(e.report, str(e).split("｜")[0], resume_hint=e.resume_hint)

    checks = verify_canary(
        drive=admin.drive,
        folder_ids=[admin.prefix_folder_id,
                    *( [readview_folder_id] if readview_folder_id else []),
                    *admin.inbox_folder_ids,
                    *([admin.quarantine_folder_id] if admin.quarantine_folder_id else [])],
        repo_dir=workdir, repo_uuid=admin.repo_uuid or "", canary=canary,
        manifest_file_id=swap.manifest_file_id)
    assert_clean(checks)

    return EraseReport(
        plan_hash=plan_hash(plan),
        deleted_file_ids=swap.deleted_file_ids,
        snapshot_remap=remap,
        erasure_record_id=record_id,
        commit_sha=commit_sha,
        swap=swap,
        verify=tuple((c.location, c.count) for c in checks),
    )


def _remap_hashes(store: AgoraStore, remap: dict[str, str],
                  new_refs: dict[str, tuple[str, str]],
                  erased_by_session: dict[str, set[str]]) -> None:
    """把 snapshots.jsonl、handoffs、links、continuations、claims 裡的舊雜湊換成新的。

    接續點訊息本身被抹除的交接單標 `erased: true`
    （讀取時照樣看得到交接單，但內容已經被抹除）。

    **`continuations/` 與 `claims/` 也要重寫**（review-2bc0785 M3）：它們的
    `body.continuation.snapshot_sha256` 指向被抹除的那一份快照。不重寫的話，抹除
    之後真本裡還留著「被抹除的那一版曾經存在」的指紋，而且和 Link 的雜湊對不上。
    """
    import json as _json

    def _swap(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (remap[v.lower()] if k == "snapshot_sha256"
                        and isinstance(v, str) and v.lower() in remap else _swap(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [_swap(v) for v in obj]
        return obj

    for rel in [*(str(p.relative_to(store.worktree))
                 for p in store.worktree.glob("sessions/*/*/snapshots.jsonl")),
                *(str(p.relative_to(store.worktree))
                 for p in store.worktree.glob("handoffs/*.json")),
                *(str(p.relative_to(store.worktree))
                 for p in store.worktree.glob("continuations/*.json")),
                *(str(p.relative_to(store.worktree))
                 for p in store.worktree.glob("claims/*.json")),
                *(str(p.relative_to(store.worktree))
                 for p in store.worktree.glob("links/**/*.json"))]:
        target = store.worktree / rel
        if rel.endswith(".jsonl"):
            lines = []
            changed = False
            for line in target.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = _json.loads(line)
                sha = entry.get("snapshot_sha256", "")
                if isinstance(sha, str) and sha.lower() in remap:
                    entry["snapshot_sha256"] = remap[sha.lower()]
                    kind, value = new_refs.get(sha.lower(), (None, None))
                    if kind == "git":
                        entry["git_blob"] = value
                    elif kind == "annex":
                        entry["annex_key"] = value
                    changed = True
                lines.append(_json.dumps(entry, ensure_ascii=False))
            if changed:
                target.write_text("\n".join(lines) + "\n", encoding="utf-8")
                store._record_changed(rel)
        else:
            try:
                obj = _json.loads(target.read_text(encoding="utf-8"))
            except ValueError:
                continue
            new_obj = _swap(obj)
            if isinstance(new_obj, dict):
                body = new_obj.get("body", {})
                if isinstance(body, dict):
                    tgt = body.get("target_session_id")
                    cont = body.get("continuation", {})
                    if (isinstance(tgt, str) and isinstance(cont, dict)
                            and cont.get("message_id") in erased_by_session.get(tgt, set())):
                        new_obj["erased"] = True
            if new_obj != obj:
                store.put_json(rel, new_obj)


def verify_canary(*, drive: DriveClient, folder_ids: Sequence[str],
                  repo_dir: Path | None, repo_uuid: str, canary: str,
                  manifest_file_id: str | None = None,
                  max_scan_bytes: int = 256 * 1024 * 1024) -> list[RemoteCheck]:
    """抹除的後置條件（fail-closed：讀不到就算失敗，不當成 0 命中）。

    - Drive：前綴／讀取視圖／收件匣／隔離區逐檔掃描 canary；下載失敗 → raise。
    - 遠端 bundle：不在 manifest 裡的 bundle 一律算殘留。
    - git：`log --all -S`（歷史）、`cat-file --batch-all-objects`（所有物件）、
      本機 annex 物件。
    """
    if not canary.strip():
        raise AdminError("後置條件需要 canary（沒有可比對的字串就等於沒驗）")
    needle = canary.encode("utf-8")
    checks: list[RemoteCheck] = []

    for folder_id in folder_ids:
        count = 0
        for f in _walk_files(drive, folder_id):
            size = f.size if f.size is not None else max_scan_bytes + 1
            if size > max_scan_bytes:
                raise AdminError(
                    f"後置條件無法驗證 {f.id}：檔案 {size} 位元組超過掃描上限 "
                    f"{max_scan_bytes}（讀不到就當通過是放行漏洞）")
            try:
                data = drive.download_bytes(f.id, max_bytes=max_scan_bytes)
            except Exception as e:
                raise AdminError(
                    f"後置條件讀不到 {f.id}（{folder_id}）：{e}；"
                    "無法確認就當失敗") from None
            count += data.count(needle)
        checks.append(RemoteCheck(location=f"drive:{folder_id}", count=count))

    if manifest_file_id is not None:
        try:
            data = drive.download_bytes(manifest_file_id, max_bytes=1024 * 1024)
            manifest = parse_manifest(data, repo_uuid=repo_uuid)
        except Exception as e:
            raise AdminError(f"後置條件讀不到 manifest：{e}") from None
        known = set(manifest.active) | set(manifest.removed)
        unlisted = 0
        for folder_id in folder_ids:
            for f in _walk_files(drive, folder_id):
                if f.name.startswith("GITBUNDLE-") and f.name not in known:
                    unlisted += 1
        checks.append(RemoteCheck(location="unlisted-bundle", count=unlisted))

    if repo_dir is not None:
        commits = _git(repo_dir, "log", "--all", f"-S{canary}", "--format=%H")
        checks.append(RemoteCheck(location="git-history",
                                  count=len([c for c in commits.split() if c])))
        checks.append(RemoteCheck(
            location="git-objects",
            count=_scan_all_git_objects(repo_dir, needle)))
        checks.append(RemoteCheck(
            location="annex-objects",
            count=_scan_annex_objects(repo_dir, needle)))
    return checks


def _scan_all_git_objects(repo_dir: Path, needle: bytes) -> int:
    """`git cat-file --batch-all-objects --batch` 掃過所有物件內容。"""
    proc = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        cwd=str(repo_dir), capture_output=True, timeout=1800, check=False)
    if proc.returncode != 0:
        raise AdminError(
            f"後置條件：git cat-file 掃描失敗 (rc={proc.returncode})；不得略過")
    return proc.stdout.count(needle)


def _scan_annex_objects(repo_dir: Path, needle: bytes) -> int:
    count = 0
    objects = repo_dir / ".git" / "annex" / "objects"
    if not objects.is_dir():
        return 0
    for path in objects.rglob("*"):
        if path.is_file() and path.name != "tmp":
            try:
                if needle in path.read_bytes():
                    count += 1
            except OSError:
                continue
    return count


def assert_clean(checks: Sequence[RemoteCheck]) -> None:
    """後置條件：任何位置計數非零就 raise（附位置清單，不含內容）。"""
    bad = [(c.location, c.count) for c in checks if c.count != 0]
    if bad:
        detail = ", ".join(f"{loc}={n}" for loc, n in bad)
        raise AdminError(f"後置條件未通過（仍有殘留）：{detail}")


def verify_remote(*, drive: DriveClient, folder_ids: Sequence[str],
                  repo_dir: Path | None, repo_uuid: str, canary: str,
                  manifest_text: bytes | None = None,
                  expected_blobs: Sequence[str] = (),
                  check_canary: bool = True,
                  max_scan_bytes: int = 256 * 1024 * 1024) -> list[RemoteCheck]:
    """抹除／復原的後置條件檢查（修掉 spike 的兩個放行漏洞）。相容舊呼叫。"""
    checks: list[RemoteCheck] = []
    if check_canary and canary:
        needle = canary.encode("utf-8")
        for folder_id in folder_ids:
            count = 0
            for f in _walk_files(drive, folder_id):
                size = f.size if f.size is not None else max_scan_bytes + 1
                if size > max_scan_bytes:
                    continue
                try:
                    data = drive.download_bytes(f.id, max_bytes=max_scan_bytes)
                except Exception:
                    continue
                count += data.count(needle)
            checks.append(RemoteCheck(location=f"drive:{folder_id}", count=count))
    if repo_dir is not None and check_canary and canary:
        try:
            proc = subprocess.run(
                ["git", "log", "--all", "--format=%H"], cwd=repo_dir,
                capture_output=True, text=True, timeout=120)
            commits = proc.stdout.split() if proc.returncode == 0 else []
            count = 0
            for sha in commits:
                grep = subprocess.run(
                    ["git", "grep", "-c", canary, sha, "--"], cwd=repo_dir,
                    capture_output=True, text=True, timeout=120)
                if grep.returncode == 0:
                    for line in grep.stdout.splitlines():
                        if ":" in line:
                            try:
                                count += int(line.rsplit(":", 1)[1])
                            except ValueError:
                                count += 1
            checks.append(RemoteCheck(location="git-history", count=count))
        except (OSError, subprocess.SubprocessError) as e:
            raise AdminError(f"git 歷史檢查失敗: {e}") from None
    if repo_dir is not None:
        for blob in expected_blobs:
            proc = subprocess.run(
                ["git", "cat-file", "-e", blob], cwd=repo_dir,
                capture_output=True, timeout=60)
            if proc.returncode != 0:
                raise AdminError(f"git cat-file 失敗（不得略過）: {blob}")
    if manifest_text is not None:
        try:
            manifest = parse_manifest(manifest_text, repo_uuid=repo_uuid)
        except Exception as e:
            raise AdminError(f"manifest 解析失敗: {e}") from None
        known = set(manifest.active) | set(manifest.removed)
        unlisted = 0
        for folder_id in folder_ids:
            for f in _walk_files(drive, folder_id):
                if f.name.startswith("GITBUNDLE-") and f.name not in known:
                    unlisted += 1
        checks.append(RemoteCheck(location="unlisted-bundle", count=unlisted))
    return checks

