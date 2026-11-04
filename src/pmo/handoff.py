"""交接出去的群請求：tick 的插槽 A／B 與 `pmo inbox handoff-close`（#358 T5-3）。

設計 §3.4、§3.2.1、Q-9 (a)：群請求的帳本、步驟機與重試 sweep 留在 MyPMO，
tick 只做兩件事——

- **插槽 A（`record`）**：這一輪 `angareion read` 看到幾個群請求 id，就記進
  `tick.json` 的 `handoffs`（`handed_at`＝這一輪的 now、`next_due_at`＝now＋
  10 分鐘、`last_reminded_at` 先空著）。已經有的 id **不覆蓋** `handed_at`：
  補讀（`--since`）讓同一則再出現時，提醒的節奏不重來。
- **插槽 B（`sweep`）**：`next_due_at` ≤ now 的才印一行
  `[group-sweep-due] ‹id…›`，印完把 `last_reminded_at`／`next_due_at` 從
  **這一輪**往後推 10 分鐘（設計 §3.4：「不補跑錯過的次數」）；所以很久沒被
  叫醒也只印一次，不會一次補好幾輪。

兩個插槽都只把改動放進 `run.writes`（`TickState → TickState`），由 `commit`
在 `tick.json.lock` 內重讀再套（design §3.4；`tick` 不打 HTTP 也不在這裡）。

`close` 是 `pmo inbox handoff-close ‹id›` 的本體：在 `tick.json.lock` 內重讀、
刪掉那一筆、寫回；之後 tick 就不再為它印 `[group-sweep-due]`。

模組不 import `tick`（`tick` import 這裡；兩個插槽拿到的 `run` 只用
`run.snapshot`／`run.now`／`run.events`／`run.writes`／`run.handoffs_seen`）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from . import _lock, tickstate
from .timeutil import iso_utc, parse_ts

if TYPE_CHECKING:  # pragma: no cover - 只給型別檢查；執行時不 import tick
    from .tick import TickRun

__all__ = [
    "HANDOFF_LOCK_TIMEOUT_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "close",
    "record",
    "sweep",
]

#: 交接提醒的間隔（design §3.4／Q-9 (a)：交接與 worker 比對都是 10 分鐘，從
#: 這一輪往後推、不補跑）。
SWEEP_INTERVAL_SECONDS = 10 * 60.0

#: `handoff-close` 等 `tick.json.lock` 的上限（design §3.4：最多 10 秒）。
HANDOFF_LOCK_TIMEOUT_SECONDS = 10.0


# --------------------------------------------------------------------------- #
# 插槽 A：記錄這一輪看到的群請求 id
# --------------------------------------------------------------------------- #


def record(run: "TickRun") -> None:
    """把這一輪的 `run.handoffs_seen` 記進 `handoffs`（插槽 A；不覆蓋舊的）。

    同一輪重複的 id 只記一次（`dict.fromkeys` 保序）；空字串不是 id，略過。
    沒有新 id 就不排 write，`commit` 連讀改寫都不必做。
    """
    ids = [key for key in dict.fromkeys(run.handoffs_seen) if key]
    if not ids:
        return
    run.writes.append(partial(_recorded, ids=tuple(ids), now=run.now))


def _recorded(
    state: tickstate.TickState, *, ids: tuple[str, ...], now: datetime
) -> tickstate.TickState:
    """One write: add the ids that are not in `handoffs` yet."""
    handoffs = dict(state.handoffs)
    changed = False
    for key in ids:
        if key in handoffs:
            # 已交接過的 id：`handed_at`／`next_due_at` 都不動（design §3.4）。
            continue
        handoffs[key] = tickstate.Handoff(
            handed_at=iso_utc(now),
            next_due_at=iso_utc(now + timedelta(seconds=SWEEP_INTERVAL_SECONDS)),
            last_reminded_at=None,
        )
        changed = True
    return replace(state, handoffs=handoffs) if changed else state


# --------------------------------------------------------------------------- #
# 插槽 B：到期的 `[group-sweep-due]`
# --------------------------------------------------------------------------- #


def due_ids(state: tickstate.TickState, now: datetime) -> list[str]:
    """`next_due_at` 已到的 id，排序後回傳（同一行輸出的順序固定）。"""
    due: list[str] = []
    for key, entry in state.handoffs.items():
        moment = parse_ts(entry.next_due_at)
        if moment is not None and moment <= now:
            due.append(key)
    return sorted(due)


def sweep(run: "TickRun") -> None:
    """到期的交接提醒：印一行、把那一筆往後推（插槽 B；設計 §3.2.1）。"""
    due = due_ids(run.snapshot, run.now)
    if not due:
        return
    run.events.append(f"[group-sweep-due] {' '.join(due)}")
    run.writes.append(partial(_reminded, ids=tuple(due), now=run.now))


def _reminded(
    state: tickstate.TickState, *, ids: tuple[str, ...], now: datetime
) -> tickstate.TickState:
    """One write: push the reminded ids forward from **this** round.

    An id another tick already pushed (or `handoff-close` deleted) is left
    alone; the worst case is one repeated line, the same one the design
    accepts for the worker poll (design §3.4).
    """
    handoffs = dict(state.handoffs)
    changed = False
    for key in ids:
        entry = handoffs.get(key)
        if entry is None:
            continue
        moment = parse_ts(entry.next_due_at)
        if moment is None or moment > now:
            continue
        handoffs[key] = replace(
            entry,
            last_reminded_at=iso_utc(now),
            next_due_at=iso_utc(now + timedelta(seconds=SWEEP_INTERVAL_SECONDS)),
        )
        changed = True
    return replace(state, handoffs=handoffs) if changed else state


# --------------------------------------------------------------------------- #
# `pmo inbox handoff-close ‹id›`
# --------------------------------------------------------------------------- #


def close(
    state_dir: Path,
    handoff_id: str,
    *,
    timeout: float | None = None,
) -> bool:
    """Delete one entry from `handoffs`; `False` when the id is not there.

    The read-modify-write is inside `tick.json.lock` (design §3.4), re-reading
    the file first, so a tick that records another id meanwhile is not
    overwritten. Waiting longer than `timeout` raises `_lock.LockTimeout`; a
    damaged file raises `SchemaError`; both are the CLI's exit 2. The default
    is `HANDOFF_LOCK_TIMEOUT_SECONDS` (read at call time, so a test can lower
    it).
    """
    wait = HANDOFF_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    path = tickstate.tick_path(Path(state_dir))
    with _lock.path_lock(tickstate.lock_path(path), timeout=wait):
        state = tickstate.load_or_empty(path)
        if handoff_id not in state.handoffs:
            return False
        handoffs = dict(state.handoffs)
        del handoffs[handoff_id]
        tickstate.save(path, replace(state, handoffs=handoffs))
        return True
