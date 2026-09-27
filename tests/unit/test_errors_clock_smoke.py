"""Smoke tests for errors and clock modules."""

import pytest
from aistorage.clock import Clock, FixedClock, SystemClock
from aistorage.errors import (
    AbortRun,
    MismatchError,
    NotFound,
    NotFoundError,
    ReadError,
    TooLarge,
    TooLargeError,
    WriteError,
)


def test_errors_smoke():
    abort = AbortRun(step="intake.scan", code="SCAN_EMPTY", message="No items")
    assert abort.step == "intake.scan"
    assert abort.code == "SCAN_EMPTY"
    assert "intake.scan" in str(abort)

    assert issubclass(NotFound, ReadError)
    assert issubclass(NotFoundError, ReadError)
    assert issubclass(TooLarge, Exception)
    assert issubclass(TooLargeError, Exception)
    assert issubclass(WriteError, Exception)
    assert issubclass(MismatchError, Exception)


def test_clock_smoke():
    fixed = FixedClock("2026-09-27T12:00:00Z")
    assert isinstance(fixed, Clock)
    assert fixed.now_utc() == "2026-09-27T12:00:00Z"
    fixed.set_time("2026-09-28T00:00:00Z")
    assert fixed.now_utc() == "2026-09-28T00:00:00Z"

    sys_clock = SystemClock()
    assert isinstance(sys_clock, Clock)
    now = sys_clock.now_utc()
    assert now.endswith("Z")
    assert "T" in now
