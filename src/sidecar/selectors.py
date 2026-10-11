"""The default Selector: available first, then priority (design D3)."""

from __future__ import annotations

from typing import Sequence

from .protocols import Subscription


class PrioritySelector:
    """Pick the available subscription that supports the agent kind and has
    the smallest priority number; ties go to the order the source returned.

    Replacing this class with an injected function later must not change
    sidecar itself (Q8 補充): the core only calls `choose`.
    """

    def choose(self, agent: str, subscriptions: Sequence[Subscription]) -> str | None:
        usable = [s for s in subscriptions if s.available and agent in s.agents]
        if not usable:
            return None
        return min(usable, key=lambda s: s.priority).id
