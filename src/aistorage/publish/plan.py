"""發佈計畫（純函式）：由真本狀態＋舊 manifest 算出要建立、沿用、退役、刪除的檔案。

依據：docs/impl/group4-modules.md 第 4.1 節（PM 決定 1、2、5）。

規則摘要：
- 「要發佈的快照集合」＝每個 Session 的最新快照 ∪ 所有交接單與接續 Link 釘住的
  快照（D10：從被釘住的快照讀）。
- 舊世代已經有同一個 (session_id, snapshot_sha256) 的 reading → 沿用舊的 file id，
  不重新上傳（D5：只重寫有變動的檔案）。
- converter_versions 改變、或呼叫端要求 → 完整重建（force_full）。
- 退役的檔案要再等一個世代才永久刪除，確保讀者拿到任何一份 manifest 時，
  其引用的檔案都還在。

本模組只讀真本工作樹與呼叫端給的舊 index 資訊，不碰網路、不寫任何東西。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from aistorage.agora.store import AgoraStore
from aistorage.errors import MismatchError
from aistorage.readview.model import FileRef, Manifest

# 單輪最多上傳幾份 reading（Drive 約每秒 2 個檔案；分批避免一次灌太多）
DEFAULT_MAX_NEW_READINGS = 500

# prev_readings 的索引鍵：(session_id, snapshot_sha256 小寫)
ReadingKey = tuple[str, str]


@dataclass(frozen=True)
class SnapshotTarget:
    """一個「Session × 快照」＝一份閱讀版。"""

    session_id: str
    snapshot_sha256: str
    snapshot_at: str
    is_latest: bool
    pinned: bool

    @property
    def key(self) -> ReadingKey:
        return (self.session_id, self.snapshot_sha256.lower())


@dataclass(frozen=True)
class ReadingToPublish:
    """要發佈的一份閱讀版。"""

    session_id: str
    snapshot_sha256: str
    is_latest: bool
    existing: FileRef | None  # 舊世代已有 → 沿用，不上傳

    @property
    def key(self) -> ReadingKey:
        return (self.session_id, self.snapshot_sha256.lower())

    @classmethod
    def of(cls, t: SnapshotTarget, existing: FileRef | None) -> ReadingToPublish:
        return cls(
            session_id=t.session_id,
            snapshot_sha256=t.snapshot_sha256,
            is_latest=t.is_latest,
            existing=existing,
        )


@dataclass(frozen=True)
class PublishPlan:
    """一輪發佈的計畫。"""

    targets: tuple[SnapshotTarget, ...] = ()
    readings_new: tuple[ReadingToPublish, ...] = ()    # 需要轉換並上傳的
    readings_keep: tuple[ReadingToPublish, ...] = ()   # 沿用舊的 file id
    retire_now: tuple[str, ...] = ()                   # 舊世代有、新世代沒有 → 列入 retired
    delete_now: tuple[str, ...] = ()                   # retired 裡「退役世代 < 新世代 − 1」→ 永久刪除
    full_rebuild: bool = False
    switch_index: bool = True                          # False＝分批上傳的中間輪次，不切換 manifest 的 index
    batch_remaining: int = 0                           # 還有多少份 reading 留到下一輪
    generation: int = 0                                # 本輪將發佈的世代號（prev + 1）

    @property
    def skipped(self) -> bool:
        """沒有任何 reading 要上傳、也沒有檔案要退役或刪除。

        索引仍可能需要重建（例：只有拒收原因改變），所以是否真的跳過發佈由
        publisher 依 agora_main_sha 與拒收原因判斷。
        """
        return (
            not self.full_rebuild
            and not self.readings_new
            and not self.retire_now
            and not self.delete_now
        )

    @property
    def kept_file_ids(self) -> frozenset[str]:
        """沿用中的 reading file id（新世代仍然引用，故不退役）。"""
        return frozenset(r.existing.id for r in self.readings_keep if r.existing is not None)


def _read_json(path: Path) -> Any:
    """讀一個真本 JSON 檔；不存在回傳 None，損毀拋 MismatchError（不靜默吞掉）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise MismatchError(f"真本檔案損毀: {path}: {e}") from e


def iter_session_ids(store: AgoraStore) -> tuple[str, ...]:
    """列出真本中所有 Session 的 id（依 meta.json 的 id 為準，依 id 排序）。"""
    root = store.worktree / "sessions"
    if not root.is_dir():
        return ()
    found: list[str] = []
    for source_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for sess_dir in sorted(p for p in source_dir.iterdir() if p.is_dir()):
            meta = _read_json(sess_dir / "meta.json")
            if isinstance(meta, dict) and isinstance(meta.get("id"), str):
                found.append(meta["id"])
    return tuple(sorted(found))


def load_session_meta(store: AgoraStore, session_id: str) -> dict[str, Any]:
    """讀取 Session 的真本 metadata 字典；不存在拋 MismatchError。"""
    meta = store.get_record(session_id)
    if not isinstance(meta, dict):
        raise MismatchError(f"真本缺少 Session metadata: {session_id}")
    return meta


def collect_pinned_snapshots(store: AgoraStore) -> dict[str, set[str]]:
    """被交接單或接續 Link 釘住的快照：{session_id: {snapshot_sha256, ...}}。

    交接單（handoffs/*.json）與接續 Link（links/continuation/<from>/*.json）
    都記錄接續點所在的快照；兩邊都讀，容忍其中一邊缺漏。
    """
    pinned: dict[str, set[str]] = {}
    wt = store.worktree

    handoffs_dir = wt / "handoffs"
    if handoffs_dir.is_dir():
        for p in sorted(handoffs_dir.glob("*.json")):
            data = _read_json(p)
            if not isinstance(data, dict):
                continue
            body = data.get("body")
            if not isinstance(body, dict):
                continue
            target = body.get("target_session_id")
            cont = body.get("continuation")
            if isinstance(target, str) and isinstance(cont, dict):
                snap = cont.get("snapshot_sha256")
                if isinstance(snap, str) and snap:
                    pinned.setdefault(target, set()).add(snap.lower())

    links_dir = wt / "links" / "continuation"
    if links_dir.is_dir():
        for p in sorted(links_dir.glob("*/*.json")):
            data = _read_json(p)
            if not isinstance(data, dict):
                continue
            target = data.get("to")
            cont = data.get("continuation")
            if isinstance(target, str) and isinstance(cont, dict):
                snap = cont.get("snapshot_sha256")
                if isinstance(snap, str) and snap:
                    pinned.setdefault(target, set()).add(snap.lower())

    return pinned


def collect_snapshot_targets(store: AgoraStore) -> tuple[SnapshotTarget, ...]:
    """算出要發佈的快照集合（每個 Session 的最新 ＋ 所有被釘住的快照）。

    釘住的快照若不在該 Session 的快照歷史裡 → MismatchError（真本狀態不一致，
    必須中止，不能默默不發）。
    """
    pinned = collect_pinned_snapshots(store)
    targets: list[SnapshotTarget] = []

    for session_id in iter_session_ids(store):
        meta = load_session_meta(store, session_id)
        snaps = store.snapshots(session_id)
        history: dict[str, str] = {
            s.snapshot_sha256.lower(): s.snapshot_at for s in snaps
        }
        latest_sha = (meta.get("raw_sha256") or "").lower()
        if not latest_sha:
            raise MismatchError(f"真本 Session metadata 缺少 raw_sha256: {session_id}")
        latest_at = history.get(latest_sha, meta.get("snapshot_at") or "")

        targets.append(
            SnapshotTarget(
                session_id=session_id,
                snapshot_sha256=latest_sha,
                snapshot_at=latest_at,
                is_latest=True,
                pinned=latest_sha in pinned.get(session_id, set()),
            )
        )

        for snap_sha in sorted(pinned.get(session_id, set())):
            if snap_sha == latest_sha:
                continue
            if snap_sha not in history:
                raise MismatchError(
                    f"交接單／接續 Link 釘住的快照不在 Session 歷史中: "
                    f"{session_id} @ {snap_sha}"
                )
            targets.append(
                SnapshotTarget(
                    session_id=session_id,
                    snapshot_sha256=snap_sha,
                    snapshot_at=history[snap_sha],
                    is_latest=False,
                    pinned=True,
                )
            )

    targets.sort(key=lambda t: (t.session_id, t.snapshot_sha256))
    return tuple(targets)


def normalize_prev_readings(
    prev_readings: Mapping[ReadingKey, FileRef] | Iterable[tuple[ReadingKey, FileRef]] | None,
) -> dict[ReadingKey, FileRef]:
    """把舊世代 index 的 readings 表正規化成以小寫 snapshot_sha256 為鍵的字典。"""
    if prev_readings is None:
        return {}
    items = prev_readings.items() if isinstance(prev_readings, Mapping) else prev_readings
    out: dict[ReadingKey, FileRef] = {}
    for key, ref in items:
        session_id, snap_sha = key
        out[(session_id, snap_sha.lower())] = ref
    return out


def plan_publish(
    store: AgoraStore,
    prev: Manifest | None,
    prev_readings: Mapping[ReadingKey, FileRef] | None,
    converter_versions: dict[str, str],
    *,
    force_full: bool = False,
    max_new_readings: int = DEFAULT_MAX_NEW_READINGS,
) -> PublishPlan:
    """由真本狀態＋舊 manifest 算出這一輪的發佈計畫（純函式，不寫任何東西）。

    Args:
        store: AgoraStore（只讀其工作樹）。
        prev: 舊世代的 manifest；None 或 generation=0 的初始 manifest 代表尚未發佈過。
        prev_readings: 舊世代 index 的 readings 表 {(session_id, snapshot_sha256): FileRef}。
        converter_versions: 本輪各來源應用的轉換器版本。
        force_full: 呼叫端要求完整重建（設定遞增 rebuild_epoch、4.5 驗證模式）。
        max_new_readings: 單輪最多上傳幾份 reading，超過則分批，中間輪次不切換 index。

    Returns:
        PublishPlan。
    """
    prev_gen = prev.generation if prev is not None else 0
    versions = dict(converter_versions)
    version_changed = prev is not None and dict(prev.converter_versions) != versions
    full_rebuild = bool(force_full) or version_changed

    existing_map = normalize_prev_readings(prev_readings)
    targets = collect_snapshot_targets(store)

    new_all: list[ReadingToPublish] = []
    keep: list[ReadingToPublish] = []
    for t in targets:
        existing = existing_map.get(t.key)
        if existing is not None and not full_rebuild:
            keep.append(ReadingToPublish.of(t, existing))
        else:
            new_all.append(ReadingToPublish.of(t, None))

    retire_now: tuple[str, ...] = ()
    delete_now: tuple[str, ...] = ()
    switch_index = True
    batch_remaining = 0
    readings_new = tuple(new_all)

    if new_all and max_new_readings > 0 and len(new_all) > max_new_readings:
        # 分批：這一輪只上傳前 N 份。manifest 必須一次指向一整組完整的檔案，
        # 所以中間輪次不切換 index，已上傳的份數記在 pending 讓下一輪沿用。
        readings_new = tuple(new_all[:max_new_readings])
        batch_remaining = len(new_all) - max_new_readings
        switch_index = False
    elif prev is not None:
        kept_ids = {r.existing.id for r in keep if r.existing is not None}
        # 舊世代引用的檔案裡，不在沿用集合者一律退役（舊 index 一定會退役：
        # 它的 id 要等新的 index 上傳完才知道）
        retire_now = tuple(fid for fid in prev.files if fid not in kept_ids)
        next_gen = prev_gen + 1
        delete_now = tuple(fid for fid, gen in prev.retired if gen < next_gen - 1)

    return PublishPlan(
        targets=targets,
        readings_new=readings_new,
        readings_keep=tuple(keep),
        retire_now=retire_now,
        delete_now=delete_now,
        full_rebuild=full_rebuild,
        switch_index=switch_index,
        batch_remaining=batch_remaining,
        generation=prev_gen + 1,
    )
