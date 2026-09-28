"""Foundry 讀取視圖發佈器（tasks 7.4、review-25a48a9 H5）。

與 Agora 共用**同一套** manifest 機制（`readview/model.py`：固定 id 原地更新、
單調遞增世代、退役檔保留一個世代再刪），`element="foundry"`。index 檔名用
`.sqlite`（內容一致，不再用 `.json` 裝 SQLite）。

給 `committer/run.py` 呼叫（impl1 接線，本檔不改 run.py）。接線方式
（把 `_publish_foundry` 的整個函式換成以下三行；`target` 已有讀取視圖欄位）：

```python
from aistorage.publish.foundry import FoundryReadViewPublisher
pub = FoundryReadViewPublisher(
    deps.drive, folder_id=target.readview_folder_id,
    manifest_file_id=target.readview_manifest_file_id,
    prefix_folder_id=target.prefix_folder_id,
    clock=deps.clock, workdir=work_temp)
pub_report, issues = pub.publish(
    foundry_store, foundry_main_sha=local_refs.get("refs/heads/main", ""),
    run_rejections=collect_rejections(foundry_store, decisions),
    allowed_keys=state.annex_keys, dry_run=dry_run)
```

- `pub_report.status`：`published`／`skipped`（冪等）／`planned`（dry-run）。
- `issues`：對不上而沒發佈的 `(artifact_id, code)`（F-H3；呼叫端把數量記進
  RunReport，例如 `foundry.unpublished=len(issues)`）。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Sequence

from aistorage.clock import Clock, format_rfc3339
from aistorage.drive.model import DriveClient
from aistorage.errors import MismatchError
from aistorage.foundry.index import (
    ArtifactIssue,
    ArtifactRow,
    FoundryIndexMeta,
    build_foundry_index,
    resolve_object_file_ids,
)
from aistorage.publish.publisher import PublishReport, rejections_fingerprint
from aistorage.publish.rejections import RejectionRow
from aistorage.readview.model import (
    FileRef,
    initial_manifest,
    next_manifest,
)
from aistorage.readview.naming import index_name
from aistorage.search.index import FORMAT as SEARCH_INDEX_FORMAT  # noqa: F401  (index 格式一致)

#: Foundry 讀取視圖的要素標識（與 Agora 共用同一套 manifest schema）。
ELEMENT_FOUNDRY = "foundry"

#: index 下載上限（與 Agora 發佈器相同）。
INDEX_MAX_BYTES = 64 << 20


def rows_from_catalog(catalogs: Iterable[dict[str, Any]]) -> list[ArtifactRow]:
    """由 Foundry 真本的 catalog 項目組出索引列。

    欄位取 catalog 的**最上層**（H5）：`apply_artifact` 把 annex_key／sha256／
    size 寫在最上層，body 裡沒有。contained 的列缺任一欄位就 raise
    MismatchError（資料不一致），不要默默略過。
    """
    rows: list[ArtifactRow] = []
    for cat in catalogs:
        if not isinstance(cat, dict):
            raise MismatchError(f"產出目錄項目不是物件: {type(cat).__name__}")
        meta = cat.get("metadata") if isinstance(cat.get("metadata"), dict) else {}
        body = cat.get("body") if isinstance(cat.get("body"), dict) else {}
        artifact_id = str(cat.get("artifact_id") or meta.get("id") or "")
        kind = str(cat.get("kind") or body.get("kind") or "")
        if not artifact_id or not kind:
            raise MismatchError(
                f"產出目錄項目缺少 artifact_id／kind: {artifact_id!r}")
        if kind == "contained":
            missing = [k for k in ("annex_key", "sha256", "size")
                       if cat.get(k) is None]
            if missing:
                raise MismatchError(
                    f"收容產出 {artifact_id} 的 catalog 缺少欄位: {missing} "
                    "(apply 必須寫在最上層，不略過)")
        rows.append(ArtifactRow(
            artifact_id=artifact_id,
            kind=kind,
            name=str(cat.get("name") or body.get("name") or ""),
            producer=str(cat.get("producer") or meta.get("producer") or ""),
            produced_by_session_id=str(
                cat.get("produced_by_session_id")
                or body.get("produced_by_session_id") or ""),
            created_at=str(meta.get("created_at") or ""),
            updated_at=str(meta.get("updated_at") or ""),
            content_type=cat.get("content_type", body.get("content_type")),
            case_id=cat.get("case_id", meta.get("case_id")),
            size=cat.get("size", body.get("size")),
            sha256=cat.get("sha256", body.get("sha256")),
            annex_key=cat.get("annex_key"),
            repo=cat.get("repo", body.get("repo")),
            path=cat.get("path", body.get("path")),
            link=cat.get("link", body.get("link")),
        ))
    return rows


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _artifacts_fp(rows: Sequence[ArtifactRow]) -> str:
    payload = sorted(
        (r.artifact_id, r.kind, r.producer, r.produced_by_session_id,
         r.annex_key or "", r.sha256 or "", r.object_file_id or "")
        for r in rows)
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FoundryPublishResult:
    """一次 Foundry 發佈的結果：Agora 同形的報告＋未發佈筆數。"""

    report: PublishReport
    unpublished: tuple[tuple[str, str], ...] = ()


class FoundryReadViewPublisher:
    """Foundry 讀取視圖發佈器（用法與 Agora 的 DriveReadViewPublisher 對齊）。"""

    def __init__(
        self,
        drive: DriveClient,
        *,
        folder_id: str,
        manifest_file_id: str,
        prefix_folder_id: str,
        clock: Clock,
        workdir: Path | str,
    ) -> None:
        self._drive = drive
        self._folder_id = folder_id
        self._manifest_file_id = manifest_file_id
        self._prefix_folder_id = prefix_folder_id
        self._clock = clock
        self._workdir = Path(workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        store: Any,
        *,
        foundry_main_sha: str,
        run_rejections: Sequence[RejectionRow] = (),
        allowed_keys: Iterable[str] | None = None,
        force_full: bool = False,
        dry_run: bool = False,
    ) -> FoundryPublishResult:
        """發佈 Foundry 讀取視圖（任何一步失敗都往上拋，真本不受影響）。

        Args:
            store: FoundryStore（`list_catalog()` 讀產出目錄）。
            foundry_main_sha: 真本 main 的 sha（冪等比對用）。
            run_rejections: 這一輪的拒收（冪等比對用）。
            allowed_keys: pin 的 annex key 集合（F-H3 的 key_not_in_pin 檢查；
                不傳就不檢查 pin，只比對 Drive）。
            force_full: 強制發佈（忽略冪等）。
            dry_run: 只計畫，不寫入。
        """
        from aistorage.publish.publisher import load_manifest
        from aistorage.readview.model import serialize_manifest

        rejections = list(run_rejections)
        raw_manifest = load_manifest(self._drive, self._manifest_file_id)
        if raw_manifest.element != ELEMENT_FOUNDRY:
            raise MismatchError(
                f"讀取視圖 manifest 的 element 是 {raw_manifest.element!r}，"
                "不是 'foundry'（Agora 與 Foundry 的 manifest 不能混用）")
        prev = None if raw_manifest.is_initial else raw_manifest

        rows = rows_from_catalog(store.list_catalog())
        # link 型（原處產出）沒有 annex 物件，直接發佈；只有 contained 才需要
        # 解析 object_file_id（舊的 `_publish_foundry` 把 link 也送進 resolve，
        # 讓它們全部變成 `missing_annex_key` 而永遠發佈不出去）。
        contained = [r for r in rows if r.kind == "contained"]
        linked = [r for r in rows if r.kind != "contained"]
        resolved, issues = resolve_object_file_ids(
            contained, drive=self._drive,
            prefix_folder_id=self._prefix_folder_id,
            allowed_keys=allowed_keys,
        )
        publishable = resolved + linked
        unpublished = tuple((i.artifact_id, i.code) for i in issues)

        # 冪等：真本沒動、拒收沒動、可發佈集合沒動 → 不發佈（與 Agora 相同）。
        if (not dry_run and not force_full and prev is not None
                and prev.agora_main_sha == foundry_main_sha
                and self._prev_rejections_fp(prev)
                == rejections_fingerprint(rejections)
                and self._prev_artifacts_fp(prev) == _artifacts_fp(publishable)):
            return FoundryPublishResult(
                report=PublishReport(
                    status="skipped",
                    manifest_file_id=self._manifest_file_id,
                    generation=prev.generation,
                    agora_main_sha=foundry_main_sha,
                    index_file_id=prev.index.id if prev.index else None,
                    rejections=len(rejections),
                    file_count=len(prev.files),
                ),
                unpublished=unpublished,
            )

        generation = (prev.generation + 1) if prev is not None else 1
        published_at = format_rfc3339(self._clock.now(), include_fraction=True)
        index_path = self._workdir / f"foundry-index-g{generation}.sqlite"
        build_foundry_index(
            index_path,
            artifacts=publishable,
            rejections=rejections,
            meta=FoundryIndexMeta(
                generation=generation,
                built_at=published_at,
                foundry_main_sha=foundry_main_sha,
            ),
        )
        index_ref: FileRef | None = None
        if not dry_run:
            created = self._drive.create(
                self._folder_id,
                index_name(generation, _sha256_file(index_path)),
                index_path,
                mime_type="application/vnd.sqlite3",
            )
            index_ref = FileRef.from_drive(created)

        base = prev if prev is not None else initial_manifest(
            element=ELEMENT_FOUNDRY, agora_main_sha=foundry_main_sha,
            published_at=published_at)
        # 退役：上一世代的 index 留一個世代再刪（與 Agora 相同）。
        retire_now = tuple(
            r for r in ([prev.index.id] if prev is not None
                        and prev.index is not None else []))
        delete_now = tuple(fid for fid, _gen in base.retired)
        new_manifest = next_manifest(
            base,
            index=index_ref if not dry_run else base.index,
            files=((index_ref.id,) if index_ref is not None else base.files),
            reading_refs=(),
            retire_now=retire_now,
            delete_now=delete_now,
            published_at=published_at,
            agora_main_sha=foundry_main_sha,
            converter_versions={},
        )
        if not dry_run:
            self._drive.update_content(
                self._manifest_file_id, serialize_manifest(new_manifest))

        deleted: list[str] = []
        delete_failures: list[str] = []
        for fid in delete_now:
            try:
                f = self._drive.get(fid)
                if self._folder_id not in f.parents:
                    raise MismatchError(
                        f"拒絕刪除：檔案 {fid} 不在讀取視圖資料夾內")
                if not dry_run:
                    self._drive.delete_permanently(fid)
                deleted.append(fid)
            except Exception as e:  # 盡力而為，記入報告
                delete_failures.append(f"{fid}:{type(e).__name__}")

        return FoundryPublishResult(
            report=PublishReport(
                status="planned" if dry_run else "published",
                manifest_file_id=self._manifest_file_id,
                generation=new_manifest.generation,
                agora_main_sha=foundry_main_sha,
                index_file_id=index_ref.id if index_ref is not None else None,
                retired=tuple(retire_now),
                deleted=tuple(deleted),
                delete_failures=tuple(delete_failures),
                rejections=len(rejections),
                file_count=len(new_manifest.files),
                dry_run=dry_run,
            ),
            unpublished=unpublished,
        )

    def _prev_rejections_fp(self, prev: Any) -> str | None:
        if prev is None or prev.index is None:
            return None
        try:
            path = self._download_prev_index(prev)
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                if "rejections" not in tables:
                    return None
                rows = [
                    RejectionRow(item_key=str(r[0]), code=str(r[1]), at=str(r[2]),
                                 item_id=r[4] or None,
                                 authenticated=bool(r[3] or 0))
                    for r in con.execute(
                        "SELECT item_key, code, at, authenticated, item_id FROM rejections")
                ]
            finally:
                con.close()
            return rejections_fingerprint(rows)
        except Exception:
            return None

    def _prev_artifacts_fp(self, prev: Any) -> str | None:
        if prev is None or prev.index is None:
            return None
        try:
            path = self._download_prev_index(prev)
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                if "artifacts" not in tables:
                    return None
                rows = sorted(
                    (str(r[0]), str(r[1]), str(r[4]), str(r[6] or ""),
                     str(r[11] or ""), str(r[10] or ""), str(r[15] or ""))
                    for r in con.execute(
                        "SELECT artifact_id, kind, content_type, name, producer, "
                        "case_id, produced_by_session_id, created_at, updated_at, "
                        "size, sha256, annex_key, repo, path, link, object_file_id "
                        "FROM artifacts")
                )
            finally:
                con.close()
            return hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        except Exception:
            return None

    def _download_prev_index(self, prev: Any) -> Path:
        path = self._workdir / "foundry-prev-index.sqlite"
        self._drive.download(prev.index.id, path, max_bytes=INDEX_MAX_BYTES)
        return path
