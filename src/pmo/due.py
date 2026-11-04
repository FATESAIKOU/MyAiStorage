"""The moments a round would next act on, shared by tick and `pmo wake show`.

`pmo tick` ends with `[idle] [next-due=…]` (⑤), and `pmo wake show` prints the
same "earliest due" so the PMO can decide the wake interval (design §3.6's
「預設建議頻率」). If the two answered differently the PMO would not know which
one to believe, so both call `earliest_due` here (裁定第 2 節). The watchdog is
part of it: the reason a 10-minute interval is suggested is the watchdog, not
only the hand-offs.

This module is **pure**: it depends on the data shapes of `teamstate`／
`tickstate` and on `timeutil`, never on `tick`. `wake` imports it, so keeping
`tick` out keeps `pmo wake show` a read of local files (no round, no HTTP).
The watchdog's own step in `tick` uses `watchdog_deadline` for the same
reason: one spelling of "when is this team's watchdog next due".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterable

from .timeutil import parse_ts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .tickstate import TickState

__all__ = [
    "WATCHDOG_SECONDS",
    "earliest_due",
    "should_be_running",
    "watchdog_deadline",
]

#: The watchdog's interval (design §3.6: 看門狗 10 分鐘): the first poke 10
#: minutes after the dispatch, the escalation 10 minutes after that poke.
WATCHDOG_SECONDS = 10 * 60.0


def should_be_running(st: dict) -> bool:
    """The design's「應該在跑」: not closed, and a dispatch is recorded.

    `ack_at` may or may not be set (ruling 10: a team PM that ends after its
    「收到」 and before the work is done still needs a person). Moved here from
    `tick` so the watchdog and the due calculation read the state the same way.
    """
    return st.get("closed") is not True and bool(str(st.get("ticket") or "").strip())


def _base(st: dict) -> datetime | None:
    """The watchdog's clock start: `received_at`, falling back to `dispatched_at`."""
    return parse_ts(st.get("received_at") or st.get("dispatched_at"))


def watchdog_deadline(st: dict, now: datetime) -> datetime | None:
    """The next watchdog moment for one team state, or `None` (design §3.6).

    The logic is `tick.step_watchdog`'s, moved out whole: `ack_at` or a team
    that should not be running ends it; a state with neither `received_at` nor
    `dispatched_at` is not enough to act on. Before the first poke the moment
    is `base + 10m`; between the poke and the escalation it is
    `ack_poke_at + 10m`; after the escalation there is none left.

    The moment is the state's own clock, so it may already be in the past: that
    is the round's overdue action, and `pmo wake show` prints it as such
    instead of hiding it. `now` is the caller's clock (both callers pass the
    one they act on); the arithmetic here does not need it.
    """
    if not should_be_running(st) or st.get("ack_at"):
        return None
    base = _base(st)
    if base is None:
        return None
    poke_at = parse_ts(st.get("ack_poke_at"))
    if poke_at is None:
        return base + timedelta(seconds=WATCHDOG_SECONDS)
    if st.get("ack_escalated_at") is not None:
        return None
    return poke_at + timedelta(seconds=WATCHDOG_SECONDS)


def earliest_due(
    tick_state: "TickState",
    team_states: Iterable[dict],
    now: datetime,
) -> datetime | None:
    """The earliest moment `tick` would next act on, or `None` when quiet.

    Three sources, the same ones `[idle]` and `wake show` report: every given
    team's watchdog (`watchdog_deadline`), `tick.json`'s `handoffs` and its
    `worker_poll` entries (`next_due_at`). Unreadable timestamps are skipped
    the way tick skips them: the schema only types them as strings, so a value
    that cannot be read is not acted on rather than treated as damage.
    """
    moments: list[datetime] = []
    for st in team_states:
        moment = watchdog_deadline(st, now)
        if moment is not None:
            moments.append(moment)
    for entry in tick_state.handoffs.values():
        moment = parse_ts(entry.next_due_at)
        if moment is not None:
            moments.append(moment)
    for poll in tick_state.worker_poll.values():
        moment = parse_ts(poll.next_due_at)
        if moment is not None:
            moments.append(moment)
    return min(moments) if moments else None
