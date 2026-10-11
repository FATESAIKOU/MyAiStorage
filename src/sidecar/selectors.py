"""The default Selector: available first, then priority, then id (design D3)."""

from __future__ import annotations

from typing import Sequence

from .protocols import Subscription


class PrioritySelector:
    """Pick the available subscription that supports the agent kind, is not
    excluded, and has the smallest priority number; equal priorities sort by
    id so the result is deterministic.

    The input is the whole `list` (including unusable rows); the selector does
    the filtering.  Replacing this class later must not change sidecar itself:
    the core only calls `choose`.
    """

    def choose(
        self, agent: str, subscriptions: Sequence[Subscription], exclude: Sequence[str] = ()
    ) -> str | None:
        excluded = set(exclude)
        usable = [
            s
            for s in subscriptions
            if s.available and agent in s.agents and s.id not in excluded
        ]
        if not usable:
            return None
        return min(usable, key=lambda s: (s.priority, s.id)).id
