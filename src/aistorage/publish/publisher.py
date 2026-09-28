"""讀取視圖發佈器（提交流程第 12 步；只在 committer 端使用）。

依據：docs/impl/group4-modules.md 第 0、4.2、4.3 節與 PM 決定 1、2、5。

順序（每一步失敗都 raise；第 12 步失敗**不影響**真本，下一輪會重新發佈）：
1. get(manifest_file_id) ＋ download_bytes → parse_manifest（管理者建立的
   generation=0 空 manifest 視為 prev=None）。
2. 下載舊的 index（依 manifest 的 FileRef，驗證 sha256）→ 取得 prev_readings、
   舊的 meta 與舊的 rejections（供冪等判斷）。
3. plan_publish。
4. 對 readings_new 逐一轉換（converter）→ create → 記下 FileRef。轉換失敗的快照
   不發佈，index 記失敗代碼（只記代碼不記內容）。
5. build_index 到 workdir → create（分批的中間輪次不建立、不切換 index）。
6. 組出新的 manifest（generation = prev + 1、retired 加上 retire_now、移除
   delete_now）→ **update_content(manifest_file_id, …)**（原地更新，id 不變）。
7. delete_permanently(delete_now)：刪除前先 get() 確認 parents 是讀取視圖資料夾；
   刪除失敗盡力而為，記入 PublishReport（該檔案會離開 manifest 的可信集合，下一輪
   第 4 步的清掃會把它隔離）。

冪等：agora_main_sha 與 run_rejections 都沒變、也沒要求完整重建時不發佈，
回傳 status="skipped"。

**限流**：Drive 約每秒 2 個檔案。完整重建（或變動量太大）時分批上傳，每輪最多
max_new_readings 份；manifest 必須一次指向一整組完整的檔案，所以中間輪次
**不切換 index**，已上傳的份數記在 manifest.pending 讓下一輪沿用（PM 決定 5）。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Sequence

from aistorage.agora.store import AgoraStore
from aistorage.clock import Clock, format_rfc3339
from aistorage.drive.model import DriveClient
from aistorage.errors import MismatchError, NotFound, ReadError
from aistorage.publish.plan import (
    DEFAULT_MAX_NEW_READINGS,
    ReadingKey,
    SnapshotTarget,
    iter_session_ids,
    load_session_meta,
    plan_publish,
)
from aistorage.publish.rejections import RejectionRow
from aistorage.reading import validate_reading
from aistorage.readview.model import (
    ELEMENT_AGORA,
    MANIFEST_MAX_BYTES,
    FileRef,
    Manifest,
    ReadingRef,
    initial_manifest,
    next_manifest,
    parse_manifest,
    serialize_manifest,
)
from aistorage.readview.naming import index_name, reading_name
from aistorage.search.index import (
    FORMAT as SEARCH_INDEX_FORMAT,
    HandoffRow,
    IndexEntry,
    IndexMeta,
    LinkRow,
)

# 索引下載上限（50 MiB 為門檻；超過視為異常）
INDEX_MAX_BYTES = 64 << 20

IndexBuilder = Callable[..., Any]


@dataclass(frozen=True)
class PublishReport:
    """一次發佈的結果（只記 id、計數與旗標，不記內容；D2 log 規則）。"""

    status: str                      # "skipped" | "published" | "planned"（dry_run）
    manifest_file_id: str
    generation: int
    agora_main_sha: str
    #: H4（review-25a48a9）：這個世代（或 skipped 時既有的世代）**包含**哪些
    #: 拒收項目的 item_key。第 14 步刪除收件匣裡的拒收項目，靠的是「這一筆已經
    #: 進入過某個已發佈的世代」，不是「這一輪有沒有發佈」——被拒收的項目通常在
    #: 24 小時後那一輪不會產生新內容，publisher 會回 skipped，舊的判斷會讓它
    #: 永遠刪不掉（收件匣因此永遠不是空的，每一輪都浪費 Actions 分鐘）。
    published_item_keys: tuple[str, ...] = ()
    readings_created: int = 0
    readings_kept: int = 0
    readings_failed: tuple[tuple[str, str], ...] = ()  # (session_id, snapshot_sha256)
    index_file_id: str | None = None
    retired: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    delete_failures: tuple[str, ...] = ()
    rejections: int = 0
    file_count: int = 0                # 本世代 manifest 引用的檔案數
    index_file_count: int = 0          # IndexStats.file_count（D5 的 5,000 門檻用）
    index_bytes: int = 0
    over_threshold: bool = False
    batch_remaining: int = 0
    full_rebuild: bool = False
    dry_run: bool = False

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def published(self) -> bool:
        return self.status == "published"


def load_manifest(drive: DriveClient, manifest_file_id: str) -> Manifest:
    """依固定 id 讀取並解析 manifest；讀不到或損毀 → MismatchError（fail-closed）。

    讀取視圖的可信集合只能來自 manifest（ADR 0008），所以這裡絕不吞掉錯誤。
    """
    try:
        drive.get(manifest_file_id)
    except NotFound as e:
        raise MismatchError(f"讀取視圖 manifest 不存在: {manifest_file_id}") from e
    data = drive.download_bytes(manifest_file_id, max_bytes=MANIFEST_MAX_BYTES)
    return parse_manifest(data)


def load_manifest_or_none(drive: DriveClient, manifest_file_id: str | None) -> Manifest | None:
    """寬鬆版：manifest 尚未建立（沒有 id、404、未解析）→ None。

    給第 4 步的清掃用：管理者還沒初始化 manifest 時跳過讀取視圖的清掃並在
    RunReport 標記，而不是誤把整個資料夾隔離。
    """
    if not manifest_file_id:
        return None
    try:
        m = load_manifest(drive, manifest_file_id)
    except (MismatchError, ReadError, NotFound):
        return None
    return None if m.is_initial else m


def serialize_reading(reading: dict) -> bytes:
    """閱讀版的位元組表示（確定性；內容定址的依據）。"""
    return (
        json.dumps(reading, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def convert_reading(
    store: AgoraStore,
    session_id: str,
    snapshot_sha256: str,
    converters: dict[str, Any],
    cache: dict[ReadingKey, dict] | None = None,
) -> dict | None:
    """把某個快照的原始紀錄轉成閱讀版；失敗回傳 None（只記代碼不記內容）。

    真本層級的失敗（取不出 raw、雜湊不符、metadata 損毀）往上拋；只有
    「轉換器轉不出來或轉出不合規範」才算轉換失敗。cache 可在同一輪內重用
    轉換結果（publisher 與 4.5 重建共用這一段，才能保證兩邊產物一致）。
    """
    key = (session_id, snapshot_sha256.lower())
    if cache is not None and key in cache:
        return cache[key]

    source = session_id.split(":", 1)[0] if ":" in session_id else ""
    conv = converters.get(source)
    if conv is None:
        return None

    meta = load_session_meta(store, session_id)
    raw_path = store.raw_path_for_snapshot(session_id, snapshot_sha256)
    try:
        reading = conv.convert(
            raw_path, session_id=session_id, parent_id=meta.get("parent_id")
        )
    except Exception:
        return None
    if not isinstance(reading, dict) or validate_reading(reading):
        return None
    if cache is not None:
        cache[key] = reading
    return reading


def index_entries(
    store: AgoraStore,
    latest_by_session: dict[str, SnapshotTarget],
    bodies: dict[ReadingKey, dict],
    refs: dict[ReadingKey, FileRef],
) -> list[IndexEntry]:
    """組出索引的 sessions 輸入（每個 Session 一筆 IndexEntry）。

    metadata 欄位名對應 group4 第 3 節 A 的 sessions 表；reading／reading_ref
    為最新快照（全文只放最新快照）。快照歷史帶上已發佈者的 file_id／sha256／
    size，索引據此產生 readings 表（被接續點釘住的舊快照也在裡面），並帶著
    該快照的 **annex key**（`agora checkout` 依它去取原始紀錄本體）。
    """
    latest_keys = {t.key for t in latest_by_session.values()}
    entries: list[IndexEntry] = []
    for session_id in iter_session_ids(store):
        smeta = load_session_meta(store, session_id)
        snaps: list[dict[str, Any]] = []
        for s in store.snapshots(session_id):
            d = s.to_dict()          # 已經含 annex_key（`agora checkout` 要用）
            ref = refs.get((session_id, s.snapshot_sha256.lower()))
            if ref is not None:
                d["file_id"] = ref.id
                d["sha256"] = ref.sha256
                d["size"] = ref.size
            snaps.append(d)

        key = next((k for k in latest_keys if k[0] == session_id), ("", ""))
        ref = refs.get(key)
        reading_ref = (
            {
                "snapshot_sha256": key[1],
                "file_id": ref.id,
                "sha256": ref.sha256,
                "size": ref.size,
            }
            if ref is not None
            else None
        )
        entries.append(
            IndexEntry(
                metadata={
                    "session_id": session_id,
                    "source": session_id.split(":", 1)[0] if ":" in session_id else "",
                    "title": smeta.get("title"),
                    "producer": smeta.get("producer"),
                    "case_id": smeta.get("case_id"),
                    "status": smeta.get("status"),
                    "stopped_at": smeta.get("stopped_at"),
                    "in_progress": bool(smeta.get("in_progress", False)),
                    "created_at": smeta.get("created_at"),
                    "updated_at": smeta.get("updated_at"),
                    "snapshot_at": smeta.get("snapshot_at"),
                    "raw_sha256": smeta.get("raw_sha256"),
                    "raw_size": smeta.get("raw_size"),
                    "parent_id": smeta.get("parent_id"),
                    "reading_status": smeta.get("reading_status", "ok"),
                    "reading_error_code": smeta.get("reading_error_code"),
                    "committed_at": smeta.get("committed_at"),
                },
                snapshots=snaps,
                reading=bodies.get(key),
                reading_ref=reading_ref,
            )
        )
    return entries


def collect_links(store: AgoraStore) -> list[LinkRow]:
    """真本的接續與參考 Link（讀取端依 session_id 反查兩個方向）。"""
    rows: list[LinkRow] = []
    base = store.worktree / "links"

    cont_dir = base / "continuation"
    if cont_dir.is_dir():
        for p in sorted(cont_dir.glob("*/*.json")):
            data = _load_record(p)
            if not isinstance(data, dict):
                continue
            cont = data.get("continuation") or {}
            rows.append(
                LinkRow(
                    kind="continuation",
                    from_session_id=data.get("from"),
                    to_session_id=data.get("to"),
                    handoff_id=data.get("handoff_id"),
                    claim_id=data.get("claim_id"),
                    snapshot_sha256=cont.get("snapshot_sha256"),
                    message_id=cont.get("message_id"),
                )
            )

    ref_dir = base / "reference"
    if ref_dir.is_dir():
        for p in sorted(ref_dir.glob("*/*.json")):
            data = _load_record(p)
            if not isinstance(data, dict):
                continue
            rows.append(
                LinkRow(
                    kind="reference",
                    from_session_id=data.get("from"),
                    to_session_id=data.get("to"),
                    reference_id=data.get("reference_id"),
                    read_snapshot_at=data.get("read_snapshot_at"),
                )
            )

    rows.sort(
        key=lambda r: (
            r.kind,
            r.from_session_id or "",
            r.to_session_id or "",
            r.handoff_id or "",
            r.reference_id or "",
        )
    )
    return rows


def collect_handoffs(store: AgoraStore) -> list[HandoffRow]:
    """真本的交接單（含寫入者提供的交接內容原樣）。

    作者 Session（author_session_id）＝寫這張交接單的那個 Session，**由提交流程
    填成 target_session_id**（PM 決定）：apply_handoff 的持有者檢查已經保證寫這張
    交接單的就是被接續 Session 的持有者（D10：接續由被接續 Session 的持有者發起），
    所以「目標 Session」即「作者」。寫入端若在 body 或 metadata 明確提供了
    `author_session_id`，以它為準（為將來預留；期 1 的同步器不提供）。
    連 target 都缺（真本不完整）時才是 NULL，由讀取端排除。
    """
    rows: list[HandoffRow] = []
    d = store.worktree / "handoffs"
    if not d.is_dir():
        return rows
    for p in sorted(d.glob("*.json")):
        data = _load_record(p)
        if not isinstance(data, dict):
            continue
        body = data.get("body") or {}
        cont = body.get("continuation") or {}
        claimed = data.get("claimed_by") or {}
        target = body.get("target_session_id")
        author = body.get("author_session_id")
        if not _is_session_id(author):
            author = data.get("author_session_id")
        if not _is_session_id(author):
            author = target
        rows.append(
            HandoffRow(
                handoff_id=data.get("id"),
                target_session_id=target,
                snapshot_sha256=cont.get("snapshot_sha256"),
                message_id=cont.get("message_id"),
                producer=data.get("producer"),
                created_at=data.get("created_at"),
                updated_at=data.get("updated_at"),
                case_id=data.get("case_id"),
                body_json=json.dumps(body, sort_keys=True, ensure_ascii=False),
                claimed_by_claim_id=claimed.get("claim_id"),
                claimed_by_session_id=claimed.get("session_id"),
                claimed_at=claimed.get("at"),
                author_session_id=author if _is_session_id(author) else None,
            )
        )
    rows.sort(key=lambda r: r.handoff_id or "")
    return rows


class DriveReadViewPublisher:
    """實作 committer.publish.ReadViewPublisher 的 Drive 發佈器。"""

    def __init__(
        self,
        drive: DriveClient,
        *,
        folder_id: str,
        manifest_file_id: str,
        converters: dict[str, Any],
        clock: Clock,
        workdir: Path,
        rebuild_epoch: int = 0,
        max_new_readings: int = DEFAULT_MAX_NEW_READINGS,
        index_builder: IndexBuilder | None = None,
    ) -> None:
        self._drive = drive
        self._folder_id = folder_id
        self._manifest_file_id = manifest_file_id
        self._converters = dict(converters)
        self._clock = clock
        self._workdir = Path(workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._rebuild_epoch = int(rebuild_epoch)
        self._max_new_readings = int(max_new_readings)
        if index_builder is None:
            from aistorage.search import index as search_index

            index_builder = search_index.build_index
        self._index_builder = index_builder

    # ------------------------------------------------------------------
    # 對外
    # ------------------------------------------------------------------

    def publish(
        self,
        store: AgoraStore,
        *,
        agora_main_sha: str,
        run_rejections: Sequence[RejectionRow] = (),
        force_full: bool = False,
        dry_run: bool = False,
    ) -> PublishReport:
        """發佈讀取視圖（提交流程第 12 步）。任何一步失敗都往上拋。"""
        rejections = list(run_rejections)
        versions = self._converter_versions()

        # ---- 1. 舊 manifest ----
        raw_manifest = load_manifest(self._drive, self._manifest_file_id)
        prev = None if raw_manifest.is_initial else raw_manifest

        # ---- 2. 舊 index（prev_readings、舊 meta、舊 rejections）----
        prev_readings, prev_meta, prev_rejections = self._load_prev_index(prev)

        # ---- 冪等：真本沒動、拒收原因沒動、也不要求重建 → 不發佈 ----
        epoch_bumped = prev is not None and self._rebuild_epoch > prev.rebuild_epoch
        if (
            not dry_run
            and not force_full
            and not epoch_bumped
            and prev is not None
            and prev.agora_main_sha == agora_main_sha
            and dict(prev.converter_versions) == versions
            and prev_meta.get("format") == SEARCH_INDEX_FORMAT
            and self._rejections_fingerprint(prev_rejections)
            == rejections_fingerprint(rejections)
        ):
            return PublishReport(
                status="skipped",
                manifest_file_id=self._manifest_file_id,
                generation=prev.generation,
                agora_main_sha=agora_main_sha,
                index_file_id=prev.index.id if prev.index else None,
                rejections=len(rejections),
                file_count=len(prev.files),
                # H4：skipped 代表「這一輪的拒收與既有世代完全相同」——
                # 冪等判斷就是比對這一點，所以它們**已經**在已發佈的世代裡。
                published_item_keys=tuple(
                    sorted({row.item_key for row in prev_rejections})
                ),
            )

        # ---- 3. 計畫 ----
        plan = plan_publish(
            store,
            prev,
            prev_readings,
            versions,
            force_full=force_full or epoch_bumped,
            max_new_readings=self._max_new_readings,
        )

        # ---- 4. 轉換並上傳 readings_new ----
        refs: dict[ReadingKey, FileRef] = {
            r.key: r.existing for r in plan.readings_keep if r.existing is not None
        }
        bodies: dict[ReadingKey, dict] = {}
        failed: list[tuple[str, str]] = []
        created_count = 0
        for r in plan.readings_new:
            body = convert_reading(store, r.session_id, r.snapshot_sha256,
                                    self._converters, bodies)
            if body is None:
                failed.append((r.session_id, r.snapshot_sha256))
                continue
            payload = serialize_reading(body)
            if dry_run:
                ref = FileRef(id="", sha256=_sha256_hex(payload), size=len(payload))
            else:
                created = self._drive.create(
                    self._folder_id,
                    reading_name(r.session_id, r.snapshot_sha256),
                    payload,
                    mime_type="application/json",
                )
                ref = FileRef.from_drive(created)
            refs[r.key] = ref
            created_count += 1

        # 沿用的最新快照也要有本體，索引才放得進全文（但 Drive 上不重傳）
        for r in plan.readings_keep:
            if r.is_latest:
                convert_reading(store, r.session_id, r.snapshot_sha256,
                                self._converters, bodies)

        # ---- 5. 建立索引（分批的中間輪次不建立、不切換）----
        generation = plan.generation
        reading_refs = [
            ReadingRef(
                session_id=t.session_id,
                snapshot_sha256=t.snapshot_sha256,
                file_id=ref.id,
                sha256=ref.sha256,
                size=ref.size,
                is_latest=t.is_latest,
            )
            for t in plan.targets
            if (ref := refs.get(t.key)) is not None
        ]
        latest_by_session = {t.session_id: t for t in plan.targets if t.is_latest}

        index_ref: FileRef | None = None
        index_bytes = 0
        over_threshold = False
        index_file_count = 0
        if plan.switch_index:
            index_path = self._workdir / f"index-g{generation}.sqlite"
            stats = self._index_builder(
                index_path,
                entries=index_entries(store, latest_by_session, bodies, refs),
                links=collect_links(store),
                handoffs=collect_handoffs(store),
                rejections=list(rejections),
                meta=IndexMeta(
                    generation=generation,
                    built_at=self._now(),
                    agora_main_sha=agora_main_sha,
                    converter_versions=versions,
                ),
            )
            index_bytes = index_path.stat().st_size if index_path.is_file() else 0
            over_threshold = bool(getattr(stats, "over_threshold", False))
            index_file_count = int(getattr(stats, "file_count", 0))
            if not dry_run:
                created_index = self._drive.create(
                    self._folder_id,
                    index_name(generation, _sha256_file(index_path)),
                    index_path,
                    mime_type="application/vnd.sqlite3",
                )
                index_ref = FileRef.from_drive(created_index)

        # ---- 6. 組出新的 manifest，原地更新 ----
        published_at = self._now()
        base = (
            initial_manifest(
                element=ELEMENT_AGORA,
                agora_main_sha=agora_main_sha,
                published_at=published_at,
            )
            if prev is None
            else prev
        )

        if plan.switch_index:
            files = tuple(
                dict.fromkeys(
                    ([index_ref.id] if index_ref is not None else [])
                    + [r.file_id for r in reading_refs]
                )
            )
            pending: tuple[ReadingRef, ...] = ()
            retire_now = plan.retire_now
            delete_now = plan.delete_now
        else:
            # 分批的中間輪次：不切換 index，舊世代的檔案全部保留，本輪上傳的份數
            # 記在 pending 讓下一輪沿用（否則會被下一輪的清掃隔離）。
            uploaded = {r.key for r in plan.readings_new}
            pending = tuple(
                r for r in reading_refs if (r.session_id, r.snapshot_sha256) in uploaded
            )
            files = tuple(
                dict.fromkeys(list(base.files) + [r.file_id for r in pending])
            )
            retire_now = ()
            delete_now = ()

        new_manifest = next_manifest(
            base,
            index=(index_ref if plan.switch_index else base.index),
            files=files,
            reading_refs=pending,
            retire_now=retire_now,
            delete_now=delete_now,
            published_at=published_at,
            agora_main_sha=agora_main_sha,
            converter_versions=versions,
            rebuild_epoch=self._rebuild_epoch,
        )
        if not dry_run:
            self._drive.update_content(
                self._manifest_file_id, serialize_manifest(new_manifest)
            )

        # ---- 7. 永久刪除退役滿一個世代的檔案 ----
        deleted: list[str] = []
        delete_failures: list[str] = []
        for fid in delete_now:
            try:
                f = self._drive.get(fid)
                if self._folder_id not in f.parents:
                    raise MismatchError(
                        f"拒絕刪除：檔案 {fid} ({f.name}) 不在讀取視圖資料夾內"
                    )
                if not dry_run:
                    self._drive.delete_permanently(fid)
                deleted.append(fid)
            except Exception as e:  # 盡力而為，記入報告（不讓整輪中止）
                delete_failures.append(f"{fid}:{type(e).__name__}")

        return PublishReport(
            status="planned" if dry_run else "published",
            manifest_file_id=self._manifest_file_id,
            generation=new_manifest.generation,
            agora_main_sha=agora_main_sha,
            # H4：這個世代包含了這一輪的全部拒收（驗章前的也在裡面——
            # `collect_rejections` 會把它們一起收）。
            published_item_keys=tuple(sorted({row.item_key for row in rejections})),
            readings_created=created_count,
            readings_kept=len(plan.readings_keep),
            readings_failed=tuple(sorted(set(failed))),
            index_file_id=index_ref.id if index_ref is not None else None,
            retired=tuple(retire_now),
            deleted=tuple(deleted),
            delete_failures=tuple(delete_failures),
            rejections=len(rejections),
            file_count=len(new_manifest.files),
            index_file_count=index_file_count,
            index_bytes=index_bytes,
            over_threshold=over_threshold,
            batch_remaining=plan.batch_remaining,
            full_rebuild=plan.full_rebuild,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # 內部
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return format_rfc3339(self._clock.now(), include_fraction=True)

    def _converter_versions(self) -> dict[str, str]:
        """各來源應用的轉換器版本；轉換器可用 `version` 屬性自報版本。"""
        return {
            source: str(getattr(conv, "version", "1"))
            for source, conv in sorted(self._converters.items())
        }

    def _load_prev_index(
        self, prev: Manifest | None
    ) -> tuple[
        dict[ReadingKey, FileRef],
        dict[str, str],
        list[tuple[str, str, str, int, str]],
    ]:
        """下載舊 index，取出 prev_readings、meta 與 rejections（唯讀）。

        索引雜湊與 manifest 記錄不符 → MismatchError（信任錨點被動過，不可繼續）。
        缺表（例如某個 Session 轉換失敗）視為沒有可沿用的項目。
        """
        readings: dict[ReadingKey, FileRef] = {}
        meta: dict[str, str] = {}
        rejections: list[tuple[str, str, str, int, str]] = []
        if prev is None:
            return readings, meta, rejections

        # manifest.pending（上一輪分批上傳、尚未被任何 index 引用的份數）也可沿用
        for r in prev.pending:
            readings.setdefault(r.key, r.file_ref)

        if prev.index is None:
            return readings, meta, rejections

        path = self._workdir / "prev-index.sqlite"
        self._drive.download(prev.index.id, path, max_bytes=INDEX_MAX_BYTES)
        actual = _sha256_file(path)
        if actual != prev.index.sha256.lower():
            raise MismatchError(
                f"舊 index 雜湊不符（manifest 記錄 {prev.index.sha256[:12]}，"
                f"實際 {actual[:12]}）"
            )

        readings: dict[ReadingKey, FileRef] = {}
        meta: dict[str, str] = {}
        rejections: list[tuple[str, str, str, int, str]] = []
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            if "meta" in tables:
                for k, v in con.execute("SELECT key, value FROM meta"):
                    meta[str(k)] = str(v)
            if "readings" in tables:
                for sid, snap, fid, sha, size in con.execute(
                    "SELECT session_id, snapshot_sha256, file_id, sha256, size FROM readings"
                ):
                    readings[(str(sid), str(snap).lower())] = FileRef(
                        id=str(fid), sha256=str(sha).lower(), size=int(size)
                    )
            if "rejections" in tables:
                for row in con.execute(
                    "SELECT item_key, code, at, authenticated, item_id FROM rejections"
                ):
                    rejections.append(
                        (str(row[0]), str(row[1]), str(row[2]), int(row[3] or 0), row[4] or "")
                    )
        finally:
            con.close()

        return readings, meta, rejections

    @staticmethod
    def _rejections_fingerprint(
        rows: Sequence[tuple[str, str, str, int, str]]
    ) -> str:
        return rejections_fingerprint(
            [
                RejectionRow(
                    item_key=a,
                    code=b,
                    at=c,
                    item_id=(e or None),
                    authenticated=bool(d),
                )
                for a, b, c, d, e in rows
            ]
        )


def rejections_fingerprint(rows: Sequence[RejectionRow]) -> str:
    """拒收原因集合的指紋（只用代碼與時間，不含任何內容）。"""
    payload = json.dumps(
        sorted(
            (r.item_key, r.code, r.at, r.item_id or "", bool(r.authenticated))
            for r in rows
        ),
        separators=(",", ":"),
    )
    return _sha256_hex(payload.encode("utf-8"))


def _load_record(path: Path) -> Any:
    """讀真本 JSON；損毀 → MismatchError（不靜默略過，否則索引會與真本不一致）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MismatchError(f"真本檔案損毀: {path}: {e}") from e


def _is_session_id(value: Any) -> bool:
    """是否為 <source>:<source_session_id> 形式的 Session id。"""
    if not isinstance(value, str) or ":" not in value:
        return False
    source, _, rest = value.partition(":")
    return bool(source) and bool(rest) and ":" not in rest
