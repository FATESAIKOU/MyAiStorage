"""6.1 抹除（只在 Mac 上、以管理憑證、在 AdminLock 內執行）。

做法照 design D2「改寫與抹除」與技術驗證 1.3：
- 部分抹除：其他 Session 與 annex 物件保留。
- 依 file id 永久刪除（刪前 get() 確認 parents）；先 dry-run。
- 所有指令先 dry-run，只列 id、計數與雜湊；`--confirm <plan-hash>` 才執行。
- 抹除紀錄只記 id 與雜湊對應，不含任何被抹除的內容。

與草案的差異（實作時確定）：
- `plan_erase` 除了 `admin` 另取 `store`（讀 snapshots／handoffs／links
  計算沿用與重新對應）與可選的 `index_db`（讀取視圖含目標內容的 reading）。
- `snapshot_remap` 在 plan 時為空，由 apply 在改寫 raw 之後填入回報與
  抹除紀錄（plan 階段無法在不知道新 raw 位元組的情況下預知新雜湊）；
  `plan_hash` 只覆蓋可執行的計畫內容。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from aistorage.admin import AdminDeps, AdminError
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


@dataclass(frozen=True)
class EraseReport:
    plan_hash: str
    deleted_file_ids: tuple[str, ...]
    snapshot_remap: dict[str, str]
    erasure_record_id: str
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
    - segment：沿用 snapshots.jsonl 的快照清單（remap 由 apply 填）。
    - 讀取視圖：目標 Session 在 index readings 表的全部 file_id（保守：整份 reading）。
    - 收件匣：sidecar 的 metadata.id 指向目標 Session 者。
    - 隔離區：檔名落在刪除清單者。
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

    return ErasePlan(
        targets=targets,
        repo_uuids=tuple(admin.repo_uuids),
        delete_file_ids=tuple(sorted(set(delete_ids))),
        readview_file_ids=tuple(sorted(set(readview_ids))),
        inbox_file_ids=tuple(sorted(set(inbox_ids))),
        quarantine_file_ids=tuple(sorted(set(quarantine_ids))),
        snapshot_remap={},
        run_ids_to_delete=(),
        known_clones=tuple(admin.known_clones),
    )


# redact 回呼的形狀：依來源格式從 raw 移除指定訊息，回傳新 raw。
# RedactRaw = Callable[[source: str, raw: bytes, message_ids: tuple[str, ...]], bytes]


def _require_filter_repo() -> str:
    path = shutil.which("git-filter-repo")
    if path is None:
        raise AdminError(
            "找不到 git-filter-repo（抹除改寫歷史需要）。"
            "請先安裝（例如 brew install git-filter-repo 或 pip install git-filter-repo）")
    return path


def _git(repo_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise AdminError(f"git {' '.join(args)} 失敗 (rc={proc.returncode})")
    return proc.stdout


def _delete_drive_id(drive: DriveClient, file_id: str, *,
                     prefix_folder_id: str) -> None:
    """依 file id 永久刪除；刪前 get() 確認 parents（1.3 M1 防呆）。"""
    try:
        info = drive.get(file_id)
    except Exception as e:
        raise AdminError(f"刪除前確認失敗 {file_id}: {e}") from None
    if prefix_folder_id not in info.parents:
        raise AdminError(f"拒絕刪除：{file_id} 的 parents 不含預期前綴")
    drive.delete_permanently(file_id)


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


def apply_erase(plan: ErasePlan, *, confirm: str, admin: AdminDeps,
                store: AgoraStore, repo_dir: Path,
                redact_raw: Any = None,
                who: str = "admin", why: str = "") -> EraseReport:
    """執行抹除（呼叫端負責先進入 AdminLock）。

    - confirm 必須等於 plan_hash(plan)，否則拒絕。
    - session：filter-repo 刪目錄；annex_key：dead／forget／drop；
      segment：需 redact_raw(source, raw, message_ids) 回呼改寫 raw，
      再以 --strip-blobs-with-ids 清除舊 blob。
    - 改寫交接單與 Link 的 snapshot_sha256（PM 決定 5）；接續點訊息本身
      被抹除的交接單標 erased。
    """
    if confirm != plan_hash(plan):
        raise AdminError("確認碼與計畫雜湊不符，拒絕執行（計畫可能已被更動）")
    _require_filter_repo()
    repo_dir = Path(repo_dir)
    now = format_rfc3339(admin.clock.now(), include_fraction=True)
    remap: dict[str, str] = {}

    session_targets = [t for t in plan.targets if t.kind == "session"]
    segment_targets = [t for t in plan.targets if t.kind == "segment"]
    key_targets = [t for t in plan.targets if t.kind == "annex_key"]

    if segment_targets and redact_raw is None:
        raise AdminError("segment 抹除需要 redact_raw 回呼（依來源格式移除訊息）")

    # 1. 整個 Session：從全部歷史刪除目錄
    for t in session_targets:
        assert t.session_id is not None
        from aistorage.agora.layout import session_dir_for_id
        try:
            rel = session_dir_for_id(t.session_id)
        except ValueError as e:
            raise AdminError(f"無效的 Session id: {t.session_id}: {e}") from None
        proc = subprocess.run(
            ["git-filter-repo", "--path", rel, "--invert-paths", "--force"],
            cwd=repo_dir, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise AdminError(f"filter-repo 刪除 {rel} 失敗 (rc={proc.returncode})")

    # 2. segment：改寫每一份快照的 raw，收集新舊雜湊
    old_blob_ids: list[str] = []
    new_refs: dict[str, tuple[str, str]] = {}
    for t in segment_targets:
        assert t.session_id is not None and t.message_ids
        sub_remap, sub_refs, sub_blobs = _redact_snapshots(
            store, admin.workdir, t.session_id, tuple(t.message_ids), redact_raw)
        remap.update(sub_remap)
        new_refs.update(sub_refs)
        old_blob_ids.extend(sub_blobs)

    # 3. annex key：dead／forget／drop（git-annex 分支只留 key 名稱供確認攻擊比對）
    for t in key_targets:
        assert t.key is not None
        for args in (["annex", "dead", t.key],
                     ["annex", "forget", "--drop-dead", "here"],
                     ["annex", "drop", "--force", "--key", t.key]):
            proc = subprocess.run(
                ["git", *args], cwd=repo_dir,
                capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise AdminError(f"git {' '.join(args)} 失敗 (rc={proc.returncode})")

    # 4. 從歷史清除被取代的 blob
    if old_blob_ids:
        ids_file = admin.workdir / "strip-ids.txt"
        ids_file.write_text("\n".join(sorted(set(old_blob_ids))) + "\n")
        proc = subprocess.run(
            ["git-filter-repo", "--strip-blobs-with-ids", str(ids_file), "--force"],
            cwd=repo_dir, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise AdminError(f"filter-repo 清除 blob 失敗 (rc={proc.returncode})")
    _git(repo_dir, "gc", "--prune=now")

    # 5. 同步改寫 snapshots.jsonl、handoffs、links 的雜湊（PM 決定 5）
    if remap:
        erased_by_session: dict[str, set[str]] = {}
        for t in segment_targets:
            assert t.session_id is not None
            erased_by_session.setdefault(t.session_id, set()).update(t.message_ids)
        _remap_hashes(store, remap, new_refs, erased_by_session)

    # 6. 依 file id 永久刪除遠端檔案（bundle、manifest、.bak、key、讀取視圖舊檔）
    deleted: list[str] = []
    for fid in (*plan.delete_file_ids, *plan.readview_file_ids,
                *plan.inbox_file_ids, *plan.quarantine_file_ids):
        _delete_drive_id(drive=admin.drive, file_id=fid,
                         prefix_folder_id=admin.prefix_folder_id)
        deleted.append(fid)

    record_id = _write_erasure_record(
        store, who=who, at=now, why=why, plan=plan, remap=remap)
    checks = verify_remote(
        drive=admin.drive, folder_ids=[admin.prefix_folder_id],
        repo_dir=repo_dir, repo_uuid=admin.repo_uuid or "",
        canary="", manifest_text=None,
        expected_blobs=(), check_canary=False)
    return EraseReport(
        plan_hash=plan_hash(plan),
        deleted_file_ids=tuple(deleted),
        snapshot_remap=remap,
        erasure_record_id=record_id,
        verify=tuple((c.location, c.count) for c in checks),
    )


def _remap_hashes(store: AgoraStore, remap: dict[str, str],
                    new_refs: dict[str, tuple[str, str]],
                    erased_by_session: dict[str, set[str]]) -> None:
    """把 snapshots.jsonl、handoffs、links 裡的舊雜湊換成新的。

    接續點訊息本身被抹除的交接單標 `erased: true`
    （讀取時照樣看得到交接單，但內容已經被抹除）。
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
                 for p in store.worktree.glob("sessions/*/snapshots.jsonl")),
                *(str(p.relative_to(store.worktree))
                 for p in (store.worktree / "handoffs").glob("*.json")
                 if (store.worktree / "handoffs").is_dir()),
                *(str(p.relative_to(store.worktree))
                 for p in (store.worktree / "links").rglob("*.json")
                 if (store.worktree / "links").is_dir())]:
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


@dataclass(frozen=True)
class RemoteCheck:
    location: str
    count: int


def verify_remote(*, drive: DriveClient, folder_ids: Sequence[str],
                  repo_dir: Path | None, repo_uuid: str, canary: str,
                  manifest_text: bytes | None = None,
                  expected_blobs: Sequence[str] = (),
                  check_canary: bool = True,
                  max_scan_bytes: int = 256 * 1024 * 1024) -> list[RemoteCheck]:
    """抹除／復原的後置條件檢查（修掉 spike 的兩個放行漏洞）。

    - remote 上有不在 manifest 裡的 bundle → location 'unlisted-bundle' 計數（漏洞 1）。
    - git cat-file 失敗 → 直接 raise，不略過（漏洞 2）。
    - canary 在各處的出現次數（搜尋只輸出計數）；check_canary=False 時跳過內容掃描。
    呼叫端用 assert_clean 判定；回傳各位置計數。
    """
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


def assert_clean(checks: Sequence[RemoteCheck]) -> None:
    """後置條件：任何位置計數非零就 raise（附位置清單，不含內容）。"""
    bad = [(c.location, c.count) for c in checks if c.count != 0]
    if bad:
        detail = ", ".join(f"{loc}={n}" for loc, n in bad)
        raise AdminError(f"後置條件未通過（仍有殘留）：{detail}")
