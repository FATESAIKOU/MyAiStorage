"""讀取視圖重建與驗證（tasks 4.5）。

依據 docs/impl/group4-modules.md 第 7 節（PM 決定 5）。

兩件事：

1. `rebuild_local`：從真本**全部**重新產生閱讀版與索引（完全不碰 Drive）。
   這是「讀取視圖是衍生物、可以重建」的實作：閱讀版與索引壞了或版本落後，
   都能從原始紀錄重來。轉換與索引組裝**重用 publisher 的同一段程式**
   （`publish.publisher.convert_reading`／`index_entries`／`collect_links`／
   `collect_handoffs`），所以「重建結果」與「增量發佈結果」用同一條路徑產生。
2. `compare_with_published`：以讀取端身分（唯讀）比對讀取視圖與重建結果，
   輸出差異的**計數與 id**，沒有任何內容。

CLI（完全唯讀，Mac 或 worker 任何地方都能跑）：

    python -m aistorage.committer.rebuild --verify --repo <真本 repo 或 annex url>
    python -m aistorage.committer.rebuild --repo <repo>      # 只重建，不比對

「完整重新發佈」不是這裡的事：它需要提交流程的寫入身分，只能在 Actions 上跑，
由 `config/committer.json` 的 `readview_rebuild_epoch` 遞增觸發
（publisher 會看到 manifest.rebuild_epoch < 設定值而走 force_full）。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Sequence

from aistorage.agora.store import AgoraStore, GitRawStorage
from aistorage.annex.git import SubprocessAnnexGit, get_git_env
from aistorage.clock import SystemClock
from aistorage.errors import ReadError
from aistorage.publish.plan import (
    ReadingKey,
    SnapshotTarget,
    collect_snapshot_targets,
)
from aistorage.publish.publisher import (
    collect_handoffs,
    collect_links,
    convert_reading,
    index_entries,
    serialize_reading,
)
from aistorage.readview.model import FileRef
from aistorage.readview.naming import reading_name
from aistorage.search.index import IndexMeta, dump_tables
from aistorage.search.query import RawRef, ReadingRef

# 索引比對時要略過的表：generation／built_at 必然不同（本地重建 vs 已發佈世代）
META_TABLE = "meta"

#: 索引比對時要略過 file_id 的表（本地重建沒有 Drive id）：readings 與 raws 的
#: 第三欄都是 file_id。
TABLES_WITH_FILE_ID = frozenset({"readings", "raws"})
READINGS_FILE_ID_COLUMN = 2  # readings: session_id, snapshot_sha256, file_id, sha256, size, is_latest


@dataclass(frozen=True)
class RebuildEntry:
    """本地重建出來的一份閱讀版。"""

    session_id: str
    snapshot_sha256: str
    is_latest: bool
    sha256: str
    size: int
    path: Path

    @property
    def key(self) -> ReadingKey:
        return (self.session_id, self.snapshot_sha256.lower())


@dataclass(frozen=True)
class RebuildResult:
    """一次本地重建的結果。"""

    out_dir: Path
    index_path: Path
    index_sha256: str
    index_tables: dict[str, list[tuple]]
    entries: tuple[RebuildEntry, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()  # (session_id, snapshot_sha256) 轉換失敗

    @property
    def sessions(self) -> int:
        return len({e.session_id for e in self.entries})

    @property
    def readings(self) -> int:
        return len(self.entries)

    def by_key(self) -> dict[ReadingKey, RebuildEntry]:
        return {e.key: e for e in self.entries}


@dataclass(frozen=True)
class RebuildDiff:
    """讀取視圖與重建結果的差異（只有計數與 id，沒有內容）。"""

    published_generation: int
    published_at: str
    agora_main_sha: str
    compared_readings: int
    missing_in_published: tuple[tuple[str, str], ...] = ()
    missing_locally: tuple[tuple[str, str], ...] = ()
    mismatched: tuple[tuple[str, str], ...] = ()
    body_failures: tuple[tuple[str, str, str], ...] = ()
    raw_failures: tuple[tuple[str, str, str], ...] = ()
    index_tables_differing: tuple[str, ...] = ()
    rebuild_failures: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not (
            self.missing_in_published
            or self.missing_locally
            or self.mismatched
            or self.body_failures
            or self.raw_failures
            or self.index_tables_differing
        )

    def counts(self) -> dict[str, int]:
        return {
            "compared_readings": self.compared_readings,
            "missing_in_published": len(self.missing_in_published),
            "missing_locally": len(self.missing_locally),
            "mismatched": len(self.mismatched),
            "body_failures": len(self.body_failures),
            "raw_failures": len(self.raw_failures),
            "index_tables_differing": len(self.index_tables_differing),
            "rebuild_failures": len(self.rebuild_failures),
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def rebuild_local(
    store: AgoraStore,
    converters: dict[str, Any],
    out_dir: Path,
    *,
    generation: int = 0,
    agora_main_sha: str = "",
    built_at: str | None = None,
    index_builder: Any | None = None,
) -> RebuildResult:
    """從真本全部重新產生閱讀版與索引（不碰 Drive、不寫任何真本）。

    要發佈的快照集合與 publisher 完全相同（每個 Session 的最新 ＋ 被交接單
    與接續 Link 釘住的快照）。轉換失敗的快照記在 failures，不產生檔案。

    索引的 file_id 在本地重建時是空的（還沒上傳 Drive），所以與已發佈索引
    比對時要略過 file_id（見 compare_with_published）。
    """
    out = Path(out_dir)
    readings_dir = out / "readings"
    readings_dir.mkdir(parents=True, exist_ok=True)

    targets: tuple[SnapshotTarget, ...] = collect_snapshot_targets(store)
    entries: list[RebuildEntry] = []
    failures: list[tuple[str, str]] = []
    bodies: dict[ReadingKey, dict] = {}
    refs: dict[ReadingKey, FileRef] = {}
    #: 原始紀錄本體的定位。file_id 在本地重建時是空的（還沒上傳 Drive），
    #: sha256 就是 snapshot_sha256（內容定址），所以索引的 raws 表仍能比對。
    raw_refs: dict[ReadingKey, FileRef] = {}

    for t in targets:
        raw_ref = FileRef(
            id="",
            sha256=t.snapshot_sha256.lower(),
            size=store.raw_path_for_snapshot(
                t.session_id, t.snapshot_sha256).stat().st_size,
        )
        raw_refs[t.key] = raw_ref
        body = convert_reading(store, t.session_id, t.snapshot_sha256, converters, bodies)
        if body is None:
            failures.append((t.session_id, t.snapshot_sha256))
            continue
        payload = serialize_reading(body)
        path = readings_dir / reading_name(t.session_id, t.snapshot_sha256)
        path.write_bytes(payload)
        ref = FileRef(id="", sha256=_sha256_file(path), size=len(payload))
        refs[t.key] = ref
        entries.append(
            RebuildEntry(
                session_id=t.session_id,
                snapshot_sha256=t.snapshot_sha256,
                is_latest=t.is_latest,
                sha256=ref.sha256,
                size=ref.size,
                path=path,
            )
        )

    latest_by_session = {t.session_id: t for t in targets if t.is_latest}
    index_path = out / "index.sqlite"
    if index_builder is None:
        from aistorage.search.index import build_index

        index_builder = build_index
    index_builder(
        index_path,
        entries=index_entries(store, latest_by_session, bodies, refs, raw_refs),
        links=collect_links(store),
        handoffs=collect_handoffs(store),
        rejections=(),
        meta=IndexMeta(
            generation=generation,
            built_at=built_at or SystemClock().now_utc(),
            agora_main_sha=agora_main_sha,
            converter_versions={},
        ),
    )

    return RebuildResult(
        out_dir=out,
        index_path=index_path,
        index_sha256=_sha256_file(index_path),
        index_tables=dump_tables(index_path),
        entries=tuple(entries),
        failures=tuple(failures),
    )


def _project_readings(rows: Sequence[tuple]) -> list[tuple]:
    """readings／raws 表比對用的投影：去掉 file_id（本地重建沒有 Drive id）。"""
    return [tuple(r[:READINGS_FILE_ID_COLUMN] + r[READINGS_FILE_ID_COLUMN + 1:]) for r in rows]


def _index_differs(local: dict[str, list[tuple]], published: dict[str, list[tuple]]) -> list[str]:
    """逐表比對（各表已依主鍵排序；G：不比對檔案雜湊）。"""
    differing: list[str] = []
    for table in sorted(set(local) | set(published)):
        if table == META_TABLE:
            continue  # generation／built_at 必然不同，不算差異
        lhs, rhs = local.get(table, []), published.get(table, [])
        if table in TABLES_WITH_FILE_ID:
            lhs, rhs = _project_readings(lhs), _project_readings(rhs)
        if lhs != rhs:
            differing.append(table)
    return differing


def compare_with_published(
    result: RebuildResult,
    client: Any,
    *,
    verify_bodies: bool = True,
) -> RebuildDiff:
    """以讀取端身分比對讀取視圖與本地重建結果（唯讀）。

    比對三件事：
    1. readings：依 (session_id, snapshot_sha256) 對照 sha256／size／is_latest。
    2. 索引：dump_tables 逐表比對（略過 meta 與 readings.file_id）。
    3. reading 檔本體：verify_bodies=True 時逐一下載並驗證 sha256 與格式。

    回傳的差異只有計數與 id，沒有任何內容。
    """
    manifest = client.manifest()
    published_tables = dump_tables(client.index_path())

    local = result.by_key()
    published: dict[ReadingKey, tuple] = {}
    is_latest: dict[ReadingKey, bool] = {}
    for row in published_tables.get("readings", []):
        sid, snap, _fid, sha, size, latest = row
        published[(str(sid), str(snap).lower())] = (str(sha).lower(), int(size))
        is_latest[(str(sid), str(snap).lower())] = bool(latest)

    missing_in_published = sorted(set(local) - set(published))
    missing_locally = sorted(set(published) - set(local))
    mismatched = sorted(
        key
        for key in set(local) & set(published)
        if (local[key].sha256, local[key].size) != published[key]
    )

    body_failures: list[tuple[str, str, str]] = []
    raw_failures: list[tuple[str, str, str]] = []
    if verify_bodies:
        for row in published_tables.get("readings", []):
            sid, snap, fid, sha, size, latest = row
            key = (str(sid), str(snap).lower())
            ref = ReadingRef(
                session_id=key[0],
                snapshot_sha256=key[1],
                file_id=str(fid),
                sha256=str(sha).lower(),
                size=int(size),
                is_latest=bool(latest),
            )
            try:
                client.reading(ref)
            except Exception as e:  # 記代碼，不記內容
                body_failures.append((key[0], key[1], type(e).__name__))
        # 原始紀錄本體也要驗：`agora checkout` 靠它保證「原封不動」，壞掉的
        # 快照在索引裡看不出來，只有實際下載並比對 sha256 才抓得到。
        for row in published_tables.get("raws", []):
            sid, snap, fid, sha, size = row
            ref = RawRef(
                session_id=str(sid),
                snapshot_sha256=str(snap).lower(),
                file_id=str(fid),
                sha256=str(sha).lower(),
                size=int(size),
            )
            try:
                client.raw(ref)
            except Exception as e:  # 記代碼，不記內容
                raw_failures.append((str(sid), str(snap).lower(), type(e).__name__))

    return RebuildDiff(
        published_generation=int(manifest.get("generation", 0)),
        published_at=str(manifest.get("published_at", "")),
        agora_main_sha=str(manifest.get("agora_main_sha", "")),
        compared_readings=len(local),
        missing_in_published=tuple(missing_in_published),
        missing_locally=tuple(missing_locally),
        mismatched=tuple(mismatched),
        body_failures=tuple(body_failures),
        raw_failures=tuple(raw_failures),
        index_tables_differing=tuple(_index_differs(result.index_tables, published_tables)),
        rebuild_failures=result.failures,
    )


def format_diff(diff: RebuildDiff) -> str:
    """把差異輸出成人看的報告：只有計數與 id（session_id＋快照雜湊前 12 碼）。"""
    def _ids(items: Sequence[tuple[str, str]]) -> str:
        return ", ".join(f"{sid}@{sha[:12]}" for sid, sha in items) or "-"

    lines = [
        f"REBUILD_VERIFY generation={diff.published_generation} "
        f"agora_main_sha={diff.agora_main_sha[:12]} "
        f"published_at={diff.published_at}",
        f"  readings_compared={diff.compared_readings}",
        f"  missing_in_published={len(diff.missing_in_published)}: "
        f"{_ids(diff.missing_in_published)}",
        f"  missing_locally={len(diff.missing_locally)}: {_ids(diff.missing_locally)}",
        f"  mismatched={len(diff.mismatched)}: {_ids(diff.mismatched)}",
        f"  body_failures={len(diff.body_failures)}: "
        f"{', '.join(f'{sid}@{sha[:12]}:{code}' for sid, sha, code in diff.body_failures) or '-'}",
        f"  raw_failures={len(diff.raw_failures)}: "
        f"{', '.join(f'{sid}@{sha[:12]}:{code}' for sid, sha, code in diff.raw_failures) or '-'}",
        f"  index_tables_differing={len(diff.index_tables_differing)}: "
        f"{', '.join(diff.index_tables_differing) or '-'}",
        f"  rebuild_failures={len(diff.rebuild_failures)}: {_ids(diff.rebuild_failures)}",
        f"  RESULT={'OK' if diff.ok else 'MISMATCH'}",
    ]
    return "\n".join(lines)


def clone_true_copy(repo: str, dest: Path, *, timeout: float = 600.0) -> Path:
    """以讀取端身分把真本 clone 到本機（唯讀；不 push、不改遠端）。

    D5：管理與復原作業可以用讀取端身分 clone。這裡只做 `git clone -b main`
    與 `git annex init`（raw 在 annex 物件庫裡，要能取出才重建得起來）。
    憑證由呼叫端以路徑或環境變數提供，**不**接受命令列上的 token。
    """
    dest_path = Path(dest).resolve()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    env = get_git_env()
    proc = subprocess.run(
        ["git", "clone", "-b", "main", repo, str(dest_path)],
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise ReadError(f"git clone 失敗 (rc={proc.returncode})")

    annex = subprocess.run(
        ["git", "-C", str(dest_path), "annex", "init"],
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=120.0,
        check=False,
    )
    if annex.returncode != 0:
        raise ReadError(
            "git annex init 失敗：重建需要從 annex 物件庫取出原始紀錄"
        )
    return dest_path


def open_store(clone_dir: Path, *, temp_dir: Path | None = None) -> AgoraStore:
    """以 clone 好的真本建立 AgoraStore（raw 由 git 物件庫取出）。"""
    return AgoraStore(
        worktree=clone_dir,
        raw_storage=GitRawStorage(clone_dir),
        git=SubprocessAnnexGit(clone_dir),
        temp_dir=temp_dir,
    )


def _build_reader_client(args: argparse.Namespace):
    """建立讀取端用戶端（SA 讀取身分；金鑰只以路徑引用）。"""
    from aistorage.drive.http import HttpDriveClient
    from aistorage.drive.sa_auth import ServiceAccountToken
    from aistorage.reader.client import ReadViewClient
    from aistorage.reader.config import ReaderConfig

    cfg = ReaderConfig.load(args.reader_config)
    if args.manifest_file_id:
        cfg = ReaderConfig(
            manifest_file_id=args.manifest_file_id,
            sa_key_path=cfg.sa_key_path,
            cache_dir=cfg.cache_dir,
        )
    token = ServiceAccountToken(cfg.sa_key_path)
    drive = HttpDriveClient(token)
    return ReadViewClient(drive, cfg, clock=SystemClock())


def main(argv: list[str] | None = None) -> int:
    """`rebuild-readview` 的進入點（唯讀）。"""
    parser = argparse.ArgumentParser(
        prog="python -m aistorage.committer.rebuild",
        description="從真本重建讀取視圖並（選用）與已發佈的讀取視圖比對（唯讀）",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("AISTORAGE_REBUILD_REPO"),
        help="真本 repo（路徑或 annex url）；也可用環境變數 AISTORAGE_REBUILD_REPO",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="與已發佈的讀取視圖比對（需要讀取端身分與 manifest id）",
    )
    parser.add_argument("--reader-config", default=None, help="讀者設定檔路徑")
    parser.add_argument("--manifest-file-id", default=None, help="覆寫讀者設定裡的 manifest id")
    parser.add_argument("--out", default=None, help="重建產物的輸出目錄（預設暫存目錄）")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出報告")
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("需要 --repo（或環境變數 AISTORAGE_REBUILD_REPO）指向真本")

    from aistorage.converters import CONVERTERS

    converters = dict(CONVERTERS)
    with tempfile.TemporaryDirectory(prefix="aistorage_rebuild_") as td:
        base = Path(td)
        clone_dir = clone_true_copy(args.repo, base / "repo")
        store = open_store(clone_dir, temp_dir=base / "store_tmp")
        out_dir = Path(args.out) if args.out else base / "out"
        result = rebuild_local(store, converters, out_dir)

        summary: dict[str, Any] = {
            "sessions": result.sessions,
            "readings": result.readings,
            "rebuild_failures": len(result.failures),
            "index_sha256": result.index_sha256,
        }
        if not args.verify:
            if args.json:
                print(json.dumps({"rebuild": summary}, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    "REBUILD_LOCAL "
                    f"sessions={result.sessions} readings={result.readings} "
                    f"rebuild_failures={len(result.failures)} "
                    f"index_sha256={result.index_sha256[:12]}"
                )
            return 0

        client = _build_reader_client(args)
        diff = compare_with_published(result, client)
        if args.json:
            print(
                json.dumps(
                    {
                        "rebuild": summary,
                        "diff": {
                            "generation": diff.published_generation,
                            "agora_main_sha": diff.agora_main_sha,
                            "counts": diff.counts(),
                            "missing_in_published": diff.missing_in_published,
                            "missing_locally": diff.missing_locally,
                            "mismatched": diff.mismatched,
                            "body_failures": diff.body_failures,
                            "raw_failures": diff.raw_failures,
                            "index_tables_differing": diff.index_tables_differing,
                            "ok": diff.ok,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(format_diff(diff))
        return 0 if diff.ok else 1


if __name__ == "__main__":
    sys.exit(main())
