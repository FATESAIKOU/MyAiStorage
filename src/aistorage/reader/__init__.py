"""讀取介面函式庫（tasks 4.3、4.4）。

唯一讀取手段：條件篩選、全文搜尋與讀取都經由這裡。讀取只讀、不觸發任何
同步或提交流程（ADR 0007）；「觸發提交流程」屬於寫入端，不放在這裡。
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, TypeVar

from aistorage.clock import Clock
from aistorage.errors import MismatchError
from aistorage.reading import messages_before
from aistorage.reader.client import AccessDenied, ReadViewClient, StaleManifest
from aistorage.reader.freshness import evaluate_freshness
from aistorage.search.query import (
    HandoffRow,
    Hit,
    LinkRow,
    MessageMatch,
    Query,
    ReadingRef,
    RejectionRow,
    SessionRow,
    get_handoff,
    get_handoffs,
    get_links,
    get_reading_ref,
    get_rejection,
    get_session_row,
    search,
)

__all__ = [
    "AccessDenied",
    "StaleManifest",
    "Freshness",
    "Result",
    "FoundSession",
    "SessionView",
    "SnapshotView",
    "ContinuationView",
    "CatalogEntry",
    "AgoraReader",
]

T = TypeVar("T")


@dataclass(frozen=True)
class Freshness:
    snapshot_at: str | None  # Session＝snapshot_at；交接單與 Link＝該世代 published_at
    generation: int
    published_at: str
    satisfied: bool | None  # 沒有指定 max_lag 時是 None
    warning: str | None
    stopped_ok: bool


@dataclass(frozen=True)
class Result(Generic[T]):
    value: T
    freshness: Freshness


@dataclass(frozen=True)
class FoundSession:
    """find 的一筆結果：Hit＋它自己的 Freshness（spec：每筆都附快照時間）。

    清單整體的 Result.freshness 以最舊的一筆為準。
    """

    hit: Hit
    freshness: Freshness


@dataclass(frozen=True)
class SnapshotView:
    session_id: str
    snapshot_sha256: str
    snapshot_at: str
    committed_at: str
    via: str


@dataclass(frozen=True)
class SessionView:
    session: SessionRow
    links_out: tuple[LinkRow, ...]
    links_in: tuple[LinkRow, ...]
    handoffs_targeting: tuple[HandoffRow, ...]  # 以它為目標的
    handoffs_by_holder: tuple[HandoffRow, ...]  # 同持有者發起的
    snapshots: tuple[SnapshotView, ...]


@dataclass(frozen=True)
class ContinuationView:
    handoff: HandoffRow
    messages: tuple[dict, ...]  # 被釘住快照裡接續點之前的訊息
    sibling_links: tuple[LinkRow, ...]  # 指向同一目標的其他接續 Link


@dataclass(frozen=True)
class CatalogEntry:
    session_id: str
    raw_sha256: str
    snapshot_at: str
    # 同步器要知道 Agora 那一邊是不是已停止，才能走「停止後恢復只觸發一次」。
    # 9.1 e2e 實測：這欄原本不存在，同步器拿不到 status，恢復邏輯在真實
    # 環境永遠走不到（讀取端 index 的 sessions.status 一直都有）。
    status: str | None = None


class AgoraReader:
    """Agora 讀取介面（唯讀）。"""

    def __init__(self, client: ReadViewClient, *, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    def _fresh(self, manifest: dict, *, snapshot_at: str | None,
               status: str | None = None, stopped_at: str | None = None,
               max_lag: timedelta | None) -> Freshness:
        return Freshness(**evaluate_freshness(
            snapshot_at=snapshot_at, status=status, stopped_at=stopped_at,
            generation=manifest["generation"], published_at=manifest["published_at"],
            now=self._clock.now(), max_lag=max_lag))

    def _worst(self, manifest: dict, at_list: list[str | None],
               max_lag: timedelta | None) -> Freshness:
        known = [a for a in at_list if a]
        if not known:
            return self._fresh(manifest, snapshot_at=None, max_lag=max_lag)
        return self._fresh(manifest, snapshot_at=min(known), max_lag=max_lag)

    def manifest(self) -> dict:
        """目前讀取視圖的 manifest（世代、published_at、index 定位）。

        寫入端（同步器、skill）需要它判斷「我上傳之後有沒有新的世代發佈」
        （PM 決定 3 的補傳條件）。純讀取，不觸發任何寫入。
        """
        return dict(self._client.manifest())

    def find_sessions(self, q: Query, *, max_lag: timedelta | None = None
                      ) -> Result[list[FoundSession]]:
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            hits, _ = search(db, q)
        finally:
            db.close()
        found = [
            FoundSession(
                hit=h,
                freshness=self._fresh(
                    manifest, snapshot_at=h.session.snapshot_at,
                    status=h.session.status, stopped_at=h.session.stopped_at,
                    max_lag=max_lag),
            )
            for h in hits
        ]
        freshness = self._worst(
            manifest, [h.session.snapshot_at for h in hits], max_lag)
        return Result(value=found, freshness=freshness)

    def get_session(self, session_id: str, *, max_lag: timedelta | None = None
                    ) -> Result[SessionView]:
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            row = get_session_row(db, session_id)
            if row is None:
                raise KeyError(f"讀取視圖沒有這個 Session: {session_id}")
            links_out, links_in = get_links(db, session_id)
            targeting = [h for h in get_handoffs(db, target_session_id=session_id)]
            by_holder = [h for h in get_handoffs(db)
                         if h.producer == row.producer and h.target_session_id != session_id]
            snaps = [SnapshotView(session_id=r[0], snapshot_sha256=r[1],
                                  snapshot_at=r[2], committed_at=r[3], via=r[4])
                     for r in db.execute(
                         "SELECT * FROM snapshots WHERE session_id = ?"
                         " ORDER BY snapshot_at, snapshot_sha256", (session_id,))]
            view = SessionView(
                session=row,
                links_out=tuple(links_out), links_in=tuple(links_in),
                handoffs_targeting=tuple(targeting),
                handoffs_by_holder=tuple(by_holder),
                snapshots=tuple(snaps),
            )
        finally:
            db.close()
        return Result(value=view, freshness=self._fresh(
            manifest, snapshot_at=row.snapshot_at, status=row.status,
            stopped_at=row.stopped_at, max_lag=max_lag))

    def get_reading(self, session_id: str, *, snapshot_sha256: str | None = None,
                    include_reverted: bool = False,
                    include_reasoning: bool = False,
                    max_lag: timedelta | None = None) -> Result[dict]:
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            ref = get_reading_ref(db, session_id, snapshot_sha256)
            if ref is None:
                raise KeyError(f"讀取視圖沒有這份閱讀版: {session_id}"
                               + (f"@{snapshot_sha256}" if snapshot_sha256 else ""))
            row = get_session_row(db, session_id)
        finally:
            db.close()
        reading = self._client.reading(ref)
        view = copy.deepcopy(reading)
        kept = []
        for msg in view.get("messages", []):
            if not include_reverted and msg.get("reverted", False):
                continue
            if not include_reasoning:
                msg["parts"] = [p for p in msg.get("parts", [])
                                if p.get("type") != "reasoning"]
            kept.append(msg)
        view["messages"] = kept
        snap_at = row.snapshot_at if row else None
        return Result(value=view, freshness=self._fresh(
            manifest, snapshot_at=snap_at,
            status=row.status if row else None,
            stopped_at=row.stopped_at if row else None, max_lag=max_lag))

    def get_continuation(self, handoff_id: str) -> Result[ContinuationView]:
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            handoff = get_handoff(db, handoff_id)
            if handoff is None:
                raise KeyError(f"讀取視圖沒有這張交接單: {handoff_id}")
            ref = get_reading_ref(db, handoff.target_session_id,
                                  handoff.snapshot_sha256)
            if ref is None:
                raise MismatchError(
                    f"被釘住的快照沒有發佈閱讀版: {handoff.target_session_id}")
            _, links_in = get_links(db, handoff.target_session_id)
            siblings = tuple(l for l in links_in
                             if l.kind == "continuation" and l.handoff_id != handoff_id)
        finally:
            db.close()
        reading = self._client.reading(ref)
        # snapshot_sha256 必須相符（messages_before 內部檢查），讀的是被釘住的快照。
        messages = messages_before(reading, handoff.message_id,
                                   snapshot_sha256=handoff.snapshot_sha256)
        return Result(
            value=ContinuationView(handoff=handoff, messages=tuple(messages),
                                   sibling_links=siblings),
            freshness=self._fresh(manifest, snapshot_at=manifest["published_at"],
                                  max_lag=None),
        )

    def list_open_handoffs(self, *, case_id: str | None = None
                           ) -> Result[list[HandoffRow]]:
        """列出待認領的交接單：只看主 Session 寫的（PM 決定 9）。

        依 handoffs.author_session_id 找到作者 Session，parent 非空（子 Session）
        或作者不明的一律排除。index 若還沒有 author_session_id 欄位
        （4.1 impl3 補上之前），raise MismatchError 而不是默默不篩。
        """
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            columns = [r[1] for r in db.execute("PRAGMA table_info(handoffs)")]
            if "author_session_id" not in columns:
                raise MismatchError(
                    "index 的 handoffs 表缺少 author_session_id 欄位，"
                    "無法篩選主 Session 寫的交接單")
            rows = get_handoffs(db, open_only=True)
            kept: list[HandoffRow] = []
            for h in rows:
                if case_id is not None and h.case_id != case_id:
                    continue
                author = get_session_row(db, h.author_session_id or "")
                if author is None or author.parent_id is not None:
                    continue
                kept.append(h)
        finally:
            db.close()
        return Result(value=kept, freshness=self._fresh(
            manifest, snapshot_at=manifest["published_at"], max_lag=None))

    def get_rejection(self, item_key: str) -> Result[RejectionRow | None]:
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            row = get_rejection(db, item_key)
        finally:
            db.close()
        return Result(value=row, freshness=self._fresh(
            manifest, snapshot_at=manifest["published_at"], max_lag=None))

    def catalog(self, session_ids: list[str]) -> Result[dict[str, CatalogEntry]]:
        manifest = self._client.manifest()
        db = self._client.index()
        try:
            out: dict[str, CatalogEntry] = {}
            for sid in session_ids:
                row = get_session_row(db, sid)
                if row is not None:
                    out[sid] = CatalogEntry(session_id=sid, raw_sha256=row.raw_sha256,
                                            snapshot_at=row.snapshot_at)
        finally:
            db.close()
        freshness = self._worst(
            manifest, [e.snapshot_at for e in out.values()], None)
        return Result(value=out, freshness=freshness)

    def wait_for_snapshot(self, session_id: str, raw_sha256: str, *,
                          timeout: timedelta,
                          poll: timedelta = timedelta(seconds=15)) -> Result[bool]:
        """輪詢 catalog 直到快照可見或逾時；只讀、只觀察，不觸發任何事。"""
        deadline = self._clock.now() + timeout
        step = max(poll.total_seconds(), 0.05)
        last_at: str | None = None
        while True:
            found = self.catalog([session_id]).value.get(session_id)
            if found is not None and found.raw_sha256.lower() == raw_sha256.lower():
                last_at = found.snapshot_at
                manifest = self._client.manifest()
                return Result(value=True, freshness=self._fresh(
                    manifest, snapshot_at=last_at, max_lag=None))
            if self._clock.now() >= deadline:
                manifest = self._client.manifest()
                return Result(value=False, freshness=self._fresh(
                    manifest, snapshot_at=None, max_lag=None))
            time.sleep(step)
