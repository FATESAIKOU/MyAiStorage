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
    """A port call failed for an external reason.

    `returncode` is the child process's exit status when the port ran one
    (the Angareion adapter fills it): the caller maps a usage error (1) and a
    back-end failure (2) to different exit codes (design §3.2). A failure that
    never reached a process (missing command, timeout, unreadable output)
    leaves it `None`. Adding the field keeps every existing `PortError(...)`
    call working.
    """

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class AicommTimeout(PortError):
    """The `aicomm send` child timed out: the message may or may not be written.

    aicomm writes first and wakes second, so a timeout is the one case where
    the caller must say「送出結果不明，不要直接重送」instead of「送不出去」
    (design §3.2 `pmo reply`; `SentButNotRecorded` is the other direction of
    the same problem). The adapter raises it on `TimeoutExpired` only.
    """


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


class SidecarShapeError(SidecarError):
    """The sidecar answered, but not in a shape this port reads (#356 低-1).

    Kept apart from「連不上」: an unreachable sidecar has no `status` and its
    detail is the connection failure, while this one came from a 2xx answer
    whose body is not the expected shape. `pmo check` says the two differently,
    so a sidecar that is up but talking an unreadable shape is never reported
    as not answering.

    `status` is the HTTP status the answer came with when the caller knows it
    (`list_agents` tags it); a shape checked on a payload the caller already
    owns (a `POST` answer) leaves it `None`.

    `record` says which shape was wrong: `False` for the outer envelope (the
    answer is not the `{"agents": [...]}` this port reads), `True` for one
    record inside it (an element that is not an object, or one without
    `id`／`state`). `pmo check` prints the two differently: only the outer one
    is a version mismatch about the wrapper (#356 修正三 低-2).
    """

    def __init__(
        self,
        status: int | None = None,
        code: str | None = None,
        detail: str = "",
        *,
        record: bool = False,
    ) -> None:
        super().__init__(status=status, code=code, detail=detail)
        self.record = record

    def at_status(self, status: int) -> "SidecarShapeError":
        """Tag the 2xx status the answer came with; the caller made the call."""
        if self.status is None:
            self.status = status
        return self


class JobCreatedUnreadable(SidecarError):
    """`POST /jobs` answered 2xx, but the body is not the job object (#358 T5-4).

    The sidecar creates the job **before** it writes the answer
    (`core.add_job(...)` then `return 201, job.to_dict()`), so a 2xx whose body
    this port cannot read still means the job probably exists. `pmo wake set`
    therefore does not treat it as a failed create: it looks the new job up
    once with `GET /jobs` (裁定第 1 節的退路) and finishes the normal flow.
    `status` is that 2xx code. When the lookup cannot find the job either, the
    same error escapes `set_interval` and the CLI prints the「請跑 pmo wake
    show 確認」line as exit 2.
    """


def sidecar_address(address: str) -> str:
    """One sidecar address in the one form the code compares and looks up (#371).

    A trailing `/` names the same machine (`http://h:8471/` is `http://h:8471`),
    so it is stripped once here and nowhere else: the port lookup, `status`'s
    per-address job cache and the「same machine as `pmo_sidecar`」comparison all
    see the same string, and a shared sidecar is asked once.
    """
    return str(address).rstrip("/")


def sidecar_code(error: "SidecarError") -> str:
    """One known word for a sidecar failure (D-12: never the message text).

    Used by `pmo.ops` and `pmo.dispatch` so the same refusal reads the same way
    everywhere (`agent_not_running`, `HTTP 500`, 「連不上」).
    """
    if error.code:
        return error.code
    if error.status is not None:
        return f"HTTP {error.status}"
    return "連不上"


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
    """One agent seat, as `GET /agents[/<id>]` reports it.

    `attach` is the sidecar's `attach.local`: the whole command line for an
    interactive team PM, verbatim (sidecar.md §2.1: 「呼叫端 MUST 原樣執行，
    不得解析或拼接」). It is `None` for the `-p` and opencode agents (the
    sidecar's `attach` is `null` there), and the `tmux` key beside it is not
    read (it is a transitional alias the sidecar will remove; #356 裁定).

    `input_unconfirmed_at` is the sidecar's「卡在 `starting`」marker: it stays
    `None` until the agent's first input is confirmed.

    `provider` is the provider the sidecar says the seat is actually using
    (§2.1); `[no-candidate]` prefers it over the team settings' default (#358
    T5-2, ruling 5).
    """

    agent_id: str
    state: str
    exit: dict[str, object] | None = None  # e.g. {"reason": "no candidate..."}
    session: str | None = None
    provider: str | None = None
    attach: str | None = None
    input_unconfirmed_at: str | None = None
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
        if self.provider is not None:
            known["provider"] = self.provider
        if self.attach is not None:
            known["attach"] = self.attach
        if self.input_unconfirmed_at is not None:
            known["input_unconfirmed_at"] = self.input_unconfirmed_at
        return known


@dataclass(frozen=True)
class JobRecord:
    """One job, as `GET /jobs` reports it (`every` already in seconds).

    `every` is `None` for a one-off job: the sidecar's auto-reopen job is an
    `at` job (sidecar.md: `every` is `null`, `at` names the one time it fires),
    and those are exactly the ones `pmo close` deletes (#370 高-2). `at` keeps
    that time as the sidecar sent it; it is `None` for an `every` job.
    """

    job_id: str
    action: str
    args: dict[str, object]
    every: float | None
    last_run: object = None
    last_error: str | None = None
    at: str | None = None
    extra: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        known: dict[str, object] = {
            "id": self.job_id,
            "action": self.action,
            "args": dict(self.args),
            "every": self.every,
            "last_run": self.last_run,
            "last_error": self.last_error,
        }
        if self.at is not None:
            known["at"] = self.at
        return known


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
    """The sidecar seam: agents and poke jobs over HTTP.

    #370 起 `poke` 回傳狀態碼（`int`），純放寬：不讀回傳值的呼叫端不受影響。
    """

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
        agent: str,
        workdir: str,
        input: str,
        provider: str | None = None,
        model: str | None = None,
        tui: bool = False,
        session: str | None = None,
        fresh: bool = False,
        agent_args: tuple[str, ...] | None = None,
    ) -> AgentRecord:
        """`POST /agents`; refusal or an unreachable sidecar raises `SidecarError`.

        `agent` is required by the sidecar and has no default there (`core.open`
        refuses an empty one with 400), so it is required here too (#356 裁定).
        `workdir` and `input` are the other two always-sent fields; everything
        else rides in the body only when it applies: `tui` for an interactive
        team PM, `session` to continue a recorded conversation
        (`pmo open --session`), `fresh` for a new one (`--fresh`, tui only),
        `provider`／`model` when the flags or the team name them (a `provider`
        alone is legal; both absent lets the sidecar pick), and `agent_args` as
        the argv tail (`agent_args_mode: "append"`). `subscription` is never
        sent (design §3.2, T-4b).
        """
        ...

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> int:
        """`POST /agents/<id>/poke`; returns the HTTP status it answered with.

        Unknown id raises `SidecarError` (`agent_not_running`／
        `agent_not_found`); the status is what `pmo poke` prints, so a 204 stays
        a 204 rather than being reported as a 200.
        """
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
