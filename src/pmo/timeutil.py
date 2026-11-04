"""The small shared pieces with no dependency of their own (design §3.1).

`iso_utc` is the one timestamp shape the events and the state files use; it
sits here, not in `pmo.runtime`, so the storage layer (`pmo.ledger`) does not
have to import the command environment (`Ctx`, `Deps`, the identity gate) just
to write a time (arch review #361 低-7).
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["iso_utc", "parse_ts", "real_now"]


def iso_utc(dt: datetime) -> str:
    """The one timestamp shape the events and the state files use."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: object) -> datetime | None:
    """One state timestamp as an aware datetime; unreadable ones are `None`.

    The files hold `iso_utc`'s shape (`…Z`), but MyPMO also writes them, so a
    plain `+00:00` is read the same way; a value without a zone is taken as
    UTC. An unreadable value is not damage (the schemas only type it as a
    string): the step that needs the moment just does not act on it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def real_now() -> datetime:
    return datetime.now(timezone.utc)
