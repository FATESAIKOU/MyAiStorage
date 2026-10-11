"""Job scheduling: `at` (one-shot) and `every` (polling, optional `until`).

Jobs live only in memory (design D2) and their action is always one of the
four external actions (design D1).  Each job remembers its last run and the
last error (code + message, never a secret) so GET /jobs can show why a
scheduled action did not happen (Q7).  The auto-reopen job that D4 creates
when every subscription is cooling down is the same kind of job: visible in
GET /jobs and deletable through DELETE /jobs/{id}.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .protocols import ACTIONS, BadRequestError, iso

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")


def parse_time(value: Any) -> float:
    """Epoch seconds from an epoch number or an ISO 8601 string."""
    if isinstance(value, bool) or value is None:
        raise BadRequestError("time must be epoch seconds or an ISO 8601 string")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise BadRequestError(f"unparseable time {value!r}; use epoch seconds or ISO 8601")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise BadRequestError("time must be epoch seconds or an ISO 8601 string")


def parse_duration(value: Any) -> float:
    """Seconds from a number or a short string like "30s", "5m", "2h", "1d"."""
    if isinstance(value, bool) or value is None:
        raise BadRequestError("every must be seconds or a duration like \"5m\"")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value.strip())
        if match:
            amount, unit = float(match.group(1)), match.group(2)
            return amount * {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]
    raise BadRequestError(f"unparseable duration {value!r}; use seconds or e.g. \"5m\"")


@dataclass
class Job:
    id: str
    action: str
    args: dict[str, Any]
    at: float | None = None
    every: float | None = None
    until: float | None = None
    created_at: float = 0.0
    next_run_at: float = 0.0
    auto: bool = False  # created by the quota machinery, not by a caller
    last_run: float | None = None
    last_error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "args": self.args,
            "at": iso(self.at),
            "every": self.every,
            "until": iso(self.until),
            "next_run_at": iso(self.next_run_at),
            "auto": self.auto,
            "last_run": iso(self.last_run),
            "last_error": self.last_error,
        }


class JobRegistry:
    """The in-memory job list; the core drives it with tick(now)."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0

    def add(
        self,
        action: str,
        args: dict[str, Any],
        *,
        at: float | None = None,
        every: float | None = None,
        until: float | None = None,
        auto: bool = False,
        now: float,
    ) -> Job:
        if action not in ACTIONS:
            raise BadRequestError(f"action must be one of {', '.join(ACTIONS)}")
        if not isinstance(args, dict):
            raise BadRequestError("args must be a JSON object")
        if (at is None) == (every is None):
            raise BadRequestError("give exactly one of at / every")
        if every is not None and every <= 0:
            raise BadRequestError("every must be positive")
        if until is not None and every is None:
            raise BadRequestError("until only makes sense with every")
        self._counter += 1
        job = Job(
            id=f"job-{self._counter}",
            action=action,
            args=dict(args),
            at=at,
            every=every,
            until=until,
            created_at=now,
            next_run_at=at if at is not None else now + every,
            auto=auto,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def due(self, now: float) -> list[Job]:
        return [job for job in self._jobs.values() if job.next_run_at <= now]

    def finish(self, job: Job, now: float) -> None:
        """After a run: drop one-shots, reschedule `every` jobs under `until`."""
        if job.every is None:
            self._jobs.pop(job.id, None)
            return
        nxt = job.next_run_at + job.every
        if job.until is not None and nxt > job.until:
            self._jobs.pop(job.id, None)
            return
        if self._jobs.get(job.id) is job:
            job.next_run_at = nxt
