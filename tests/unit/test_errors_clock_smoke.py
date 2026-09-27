"""Smoke tests for errors and clock modules."""

from datetime import datetime, timezone
import pytest

from aistorage.clock import (
    Clock,
    FixedClock,
    SystemClock,
    format_rfc3339,
    parse_rfc3339,
)
from aistorage.errors import (
    AbortRun,
    MismatchError,
    NotFound,
    ReadError,
    TooLarge,
    WriteError,
)


def test_errors_smoke():
    abort = AbortRun(step="intake.scan", code="SCAN_EMPTY", message="No items")
    assert abort.step == "intake.scan"
    assert abort.code == "SCAN_EMPTY"
    assert "intake.scan" in str(abort)

    assert issubclass(NotFound, ReadError)
    assert not issubclass(TooLarge, ReadError)
    assert issubclass(TooLarge, Exception)
    assert issubclass(WriteError, Exception)
    assert issubclass(MismatchError, Exception)


def test_clock_smoke():
    fixed = FixedClock("2026-09-27T12:00:00Z")
    assert isinstance(fixed, Clock)
    assert fixed.now_utc() == "2026-09-27T12:00:00Z"
    assert fixed.now() == datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)

    # advance
    fixed.advance(3600)
    assert fixed.now_utc() == "2026-09-27T13:00:00Z"

    fixed.set_time("2026-09-28T00:00:00Z")
    assert fixed.now_utc() == "2026-09-28T00:00:00Z"

    sys_clock = SystemClock()
    assert isinstance(sys_clock, Clock)
    now = sys_clock.now_utc()
    assert now.endswith("Z")
    assert "T" in now
    assert isinstance(sys_clock.now(), datetime)


def test_rfc3339_parsing_and_formatting():
    # Z format
    dt = parse_rfc3339("2026-09-27T08:00:00Z")
    assert dt == datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)

    # Milliseconds
    dt_ms = parse_rfc3339("2026-09-27T08:00:00.500Z")
    assert dt_ms.microsecond == 500000

    # Nanoseconds (rclone format, truncated to 6 digits)
    dt_ns = parse_rfc3339("2026-09-27T08:00:00.123456789Z")
    assert dt_ns.microsecond == 123456

    # Offset
    dt_offset = parse_rfc3339("2026-09-27T09:00:00+01:00")
    assert dt_offset == datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)

    # Formatting
    formatted = format_rfc3339(dt)
    assert formatted == "2026-09-27T08:00:00Z"
    formatted_frac = format_rfc3339(dt_ms, include_fraction=True)
    assert formatted_frac == "2026-09-27T08:00:00.500000Z"
