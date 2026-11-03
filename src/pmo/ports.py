"""The four ports: the only seams the `pmo` command touches the outside through.

The command talks to Angareion, aicomm, the sidecar and GitHub and to nothing
else (design §3.1; the core never touches the outside). Each port is a small
Protocol; tests replace every port with the same-named fake in `pmo.fakes`
(the way the current PMO tool uses its `Deps` seam).

The interface shapes are the design's (§3.3): Angareion sends with
`--channel --to --in-reply-to --urgency --ref --body -` and answers with the
message id, reads with `--json [--since]` (including `received_at`) and logs
without moving the cursor; aicomm sends with `--channel --to [--ticket]
[--urgency] --body -` (write plus wake); the sidecar serves `/agents`,
`/agents/<id>`, `POST /agents`, `/poke`, `/close`, and `/jobs`; GitHub is
`gh issue view <n> --repo <owner/repo> --json title,body`, read-only.
Records keep only those fields. Anything else the backend sends stays in
`extra` on the record and is never printed: `to_dict` writes the known fields
only (D-12: only known fields reach the printer).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class PortError(RuntimeError):
    """A port call failed for an external reason."""


class SidecarError(PortError):
    """The sidecar refused, is not reachable, or the thing is not there.

    `status` is the HTTP status when there was one; `code` is the sidecar's
    machine error code when the body named one (e.g. `workdir_not_found`,
    `agent_not_running`). The detail text is only ever built from known fields.
    """

    def __init__(self, status: int | None = None, code: str | None = None, detail: str = "") -> None:
        super().__init__(detail or code or f"sidecar error (status {status if status is not None else '?'})")
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Message:
    """One Angareion message, as the core sees it (design §1.1).

    `mentions` is the "提及" part of the叫人 definition (design §1.1: 寄給或
    提及 PMO); Angareion's `read --json` carries it and the叫人 check needs
    it, so it rides on the record. The fake messages carry the same field.
    """

    message_id: str
    channel: str
    sender: str
    to: str | None
    received_at: str
    body: str
    urgency: int | None = None
    in_reply_to: str | None = None
    mentions: tuple[str, ...] | None = None


_SECONDS_RE = re.compile(r"([0-9]+)([smh]?)")


def seconds_of(value: object) -> float:
    """`every`, whatever shape it arrived in, as seconds.

    `POST /jobs` sends a string (`10m`) but `GET /jobs` returns seconds
    (`600.0`); comparisons translate both to seconds and accept both shapes
    (design §3.6 step 2, M5-1). Booleans and other shapes are not an `every`.
    """
    if isinstance(value, bool) or value is None:
        raise SidecarError(detail=f"every must be a number of seconds or '‹n›s|m|h', got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds):
            raise SidecarError(detail=f"every must be a finite number of seconds, got {value!r}")
        if seconds < 0:
            raise SidecarError(detail=f"every must not be negative ({value!r})")
        return seconds
    if isinstance(value, str):
        match = _SECONDS_RE.fullmatch(value.strip())
        if not match:
            raise SidecarError(detail=f"every must be '‹n›s', '‹n›m' or '‹n›h', got {value!r}")
        count, unit = match.groups()
        multiplier = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
        return int(count) * multiplier
    raise SidecarError(detail=f"every must be a number of seconds or '‹n›s|m|h', got {value!r}")


@dataclass(frozen=True)
class AgentRecord:
    """One agent seat, as `GET /agents[/<id>]` reports it."""

    agent_id: str
    state: str
    exit: dict[str, object] | None = None  # e.g. {"reason": "no candidate..."}
    session: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def exit_reason(self) -> str | None:
        """The `exit.reason` when there is one."""
        if not isinstance(self.exit, dict):
            return None
        reason = self.exit.get("reason")
        return reason if isinstance(reason, str) else None

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {"id": self.agent_id, "state": self.state}
        if self.exit is not None:
            known["exit"] = self.exit
        if self.session is not None:
            known["session"] = self.session
        return known


@dataclass(frozen=True)
class JobRecord:
    """One poke job, as `GET /jobs` reports it (`every` already in seconds)."""

    job_id: str
    action: str
    args: dict[str, object]
    every: float
    last_run: object = None
    last_error: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.job_id,
            "action": self.action,
            "args": dict(self.args),
            "every": self.every,
            "last_run": self.last_run,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class IssueData:
    """One GitHub issue, as `gh issue view --json title,body` returns it."""

    title: str
    body: str

    def to_dict(self) -> dict[str, object]:
        return {"title": self.title, "body": self.body}


@runtime_checkable
class AngareionPort(Protocol):
    """The Angareion seam: send, read (the PMO identity), log (for rebuild)."""

    def send(
        self,
        *,
        channel: str,
        to: str,
        body: str,
        in_reply_to: str | None = None,
        urgency: int | None = None,
        ref: str | None = None,
    ) -> str:
        """Send one message; returns the message id."""
        ...

    def read(self, *, since: str | None = None) -> list[Message]:
        """Read everything unread since the cursor (PMO's only way to read)."""
        ...

    def log(
        self,
        *,
        channel: str | None = None,
        sender: str | None = None,
        to: str | None = None,
        thread: str | None = None,
        grep: str | None = None,
        since: str | None = None,
    ) -> list[Message]:
        """List past messages without moving the cursor (rebuild; design §1.2)."""
        ...


@runtime_checkable
class AicommPort(Protocol):
    """The aicomm seam: write into a channel and wake someone up."""

    def send(
        self,
        *,
        channel: str,
        to: str,
        body: str,
        ticket: str | None = None,
        urgency: int | None = None,
        no_wake: bool = False,
    ) -> str:
        """Send one message through aicomm (write + wake); returns the message id."""
        ...


@runtime_checkable
class SidecarPort(Protocol):
    """The sidecar seam: agents and poke jobs over HTTP."""

    def list_agents(self) -> list[AgentRecord]:
        """`GET /agents` (this machine's sidecar)."""
        ...

    def get_agent(self, agent_id: str) -> AgentRecord:
        """`GET /agents/<id>`; unknown id raises `SidecarError`."""
        ...

    def open_agent(
        self,
        *,
        agent_id: str,
        workdir: str,
        provider: str,
        model: str,
        input: str,
        session: str | None = None,
        fresh: bool = False,
    ) -> AgentRecord:
        """`POST /agents`; refusal or an unreachable sidecar raises `SidecarError`.

        `session` continues a recorded conversation (`pmo open --session`), and
        `fresh` asks for a new one (`--fresh`); both ride in the `POST` body
        (design §3.2, T-4b).
        """
        ...

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> None:
        """`POST /agents/<id>/poke`; unknown id raises `SidecarError`."""
        ...

    def close_agent(self, agent_id: str) -> None:
        """`POST /agents/<id>/close`; unknown id raises `SidecarError`."""
        ...

    def list_jobs(self) -> list[JobRecord]:
        """`GET /jobs` — this sidecar's scheduled jobs (`every` in seconds)."""
        ...

    def create_job(self, *, every: str, agent_id: str, text: str, interrupt: bool = False) -> JobRecord:
        """`POST /jobs` (a `poke` job; the design's "create before delete")."""
        ...

    def delete_job(self, job_id: str) -> bool:
        """`DELETE /jobs/<id>`; reports whether such a job existed."""
        ...


@runtime_checkable
class GitHubPort(Protocol):
    """The GitHub seam, read-only: what `gh issue view` reports."""

    def issue_view(self, repo: str, number: int) -> IssueData:
        """The issue's title and body; an unknown number raises a lookup error."""
        ...
