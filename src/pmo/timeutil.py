"""The small shared pieces with no dependency of their own (design §3.1).

`iso_utc` is the one timestamp shape the events and the state files use; it
sits here, not in `pmo.runtime`, so the storage layer (`pmo.ledger`) does not
have to import the command environment (`Ctx`, `Deps`, the identity gate) just
to write a time (arch review #361 低-7).
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["iso_utc", "real_now"]


def iso_utc(dt: datetime) -> str:
    """The one timestamp shape the events and the state files use."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def real_now() -> datetime:
    return datetime.now(timezone.utc)
