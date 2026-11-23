"""The small shared pieces with no dependency of their own (design §3.1).

`iso_utc` is the one timestamp shape the events and the state files use; it
sits here, not in `pmo.runtime`, so the storage layer (`pmo.ledger`) does not
have to import the command environment (`Ctx`, `Deps`, the identity gate) just
to write a time (arch review #361 低-7).
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["iso_utc", "parse_ts", "real_now", "received_at_key"]

#: The moment the ranks without one carry, so two keys of the same rank never
#: compare a datetime against a string (or against nothing).
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def received_at_key(value: object) -> tuple:
    """A `received_at`'s sort key: no time first, then by instant, unreadable last.

    The back end writes `…Z`, but `+00:00`, `+09:00` and fractional seconds are
    the same kind of value; a readable time is therefore its moment, not its
    text, so mixed spellings order correctly and equal instants written
    differently compare equal (the caller breaks the tie). A value without a
    zone is UTC, as `parse_ts` reads it (#522).

    The moment is never converted to UTC: `0001-01-01T00:00:00+01:00` is an hour
    before `0001-01-01T00:00:00Z`, and `astimezone` would overflow on it.

    The result is `(rank, moment)`, plus the original text for an unreadable
    value (`(2, _EPOCH, text)`, so those keep a stable order among themselves).
    The moment is the fixed `_EPOCH` wherever there is none, so a key never
    compares a datetime with a string, and callers can append their own fields
    (`(received_at_key(x), item_id)`).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return (0, _EPOCH)
    moment = parse_ts(value)
    if moment is None:
        return (2, _EPOCH, value if isinstance(value, str) else str(value))
    return (1, moment)
