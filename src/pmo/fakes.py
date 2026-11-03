"""Fakes for the four ports.

Same names and shapes as the real seam will have, so a consumer written
against the Protocols runs unchanged against these (`#110` T-1: the fakes are
deliverables, tests use them instead of any real backend, real HOME or real
sidecar).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ports import (
    AgentRecord,
    IssueData,
    JobRecord,
    Message,
    PortError,
    SidecarError,
    seconds_of,
)

#: Prefix of the message ids the fakes hand out (harmless-looking, fake).
FAKE_ID_PREFIX = "fake-msg-"


@dataclass(frozen=True)
class SentMessage:
    """What a fake recorded about one sent message."""

    message_id: str
    channel: str
    to: str
    body: str
    in_reply_to: str | None = None
    urgency: int | None = None
    ref: str | None = None
    ticket: str | None = None
    no_wake: bool = False


class FakeAngareion:
    """A channel with a queue: `read` drains it, `send` appends to `sent`.

    `errors` maps a method name to the exception that calling that method
    should raise, the way the real backend refuses or is rate-limited (design
    §2.3 的 angareion 分支；the same injection point FakeSidecar has).
    """

    def __init__(self, messages: list[Message] | None = None, logged: list[Message] | None = None) -> None:
        self.pending: list[Message] = list(messages or [])
        self.logged: list[Message] = list(logged or [])
        self.sent: list[SentMessage] = []
        self.read_calls: list[str | None] = []
        self.log_calls: list[dict[str, str | None]] = []
        self.errors: dict[str, PortError] = {}
        self._next = 0

    def _maybe_fail(self, method: str) -> None:
        error = self.errors.get(method)
        if error is not None:
            raise error

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
        self._maybe_fail("send")
        self._next += 1
        message_id = f"{FAKE_ID_PREFIX}{self._next:03d}"
        self.sent.append(
            SentMessage(
                message_id=message_id,
                channel=channel,
                to=to,
                body=body,
                in_reply_to=in_reply_to,
                urgency=urgency,
                ref=ref,
            )
        )
        return message_id

    def read(self, *, since: str | None = None) -> list[Message]:
        self.read_calls.append(since)
        self._maybe_fail("read")
        delivering, self.pending = self.pending, []
        return delivering

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
        """List `logged` the way `angareion log` filters (channel／target／since…).

        The real `log` narrows its answer by every flag; a fake that returned
        everything would hide a caller that forgot `--channel` (#355 高-1).
        `thread` matches `in_reply_to`, `since` is an inclusive lower bound.
        """
        self.log_calls.append(
            {"channel": channel, "sender": sender, "to": to, "thread": thread, "grep": grep, "since": since}
        )
        self._maybe_fail("log")
        found: list[Message] = []
        for message in self.logged:
            if channel is not None and message.channel != channel:
                continue
            if sender is not None and message.sender != sender:
                continue
            if to is not None and message.to != to:
                continue
            if thread is not None and message.in_reply_to != thread:
                continue
            if grep is not None and grep.lower() not in message.body.lower():
                continue
            if since is not None and (message.received_at or "") < since:
                continue
            found.append(message)
        return found


class FakeAicomm:
    """Records every aicomm send; delivers nothing."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self._next = 0
        self.always_fail: PortError | None = None

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
        if self.always_fail is not None:
            raise self.always_fail
        self._next += 1
        message_id = f"{FAKE_ID_PREFIX}acomm-{self._next:03d}"
        self.sent.append(
            SentMessage(
                message_id=message_id,
                channel=channel,
                to=to,
                body=body,
                urgency=urgency,
                ticket=ticket,
                no_wake=no_wake,
            )
        )
        return message_id


class FakeSidecar:
    """An in-memory sidecar: agents and jobs, with injection points for fail.

    `errors` maps a method name to the `SidecarError` (or any exception) that
    calling that method should raise — the way the real HTTP calls refuse or
    time out (design §2.3 的 sidecar 分支).
    """

    def __init__(self, addresses: str = "http://sidecar.local") -> None:
        self.address = addresses
        self.agents: dict[str, AgentRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.pokes: list[tuple[str, str, bool]] = []
        #: (agent id, session, fresh) for every `open_agent` call (design §3.2).
        self.opened_sessions: list[tuple[str, str | None, bool]] = []
        #: The full request every `open_agent` call was given, as the real
        #: sidecar would read it (tests can check the shape, #356 裁定 §2).
        self.opened_payloads: list[dict[str, object]] = []
        self.closed: list[str] = []
        self.deleted_jobs: list[str] = []
        self.errors: dict[str, PortError] = {}
        self._agent_next = 0
        self._job_next = 0

    def _maybe_fail(self, method: str) -> None:
        error = self.errors.get(method)
        if error is not None:
            raise error

    def add_agent(self, agent_id: str, state: str = "running", session: str | None = "s1",
                  exit: dict[str, object] | None = None,
                  attach: str | None = None,
                  input_unconfirmed_at: str | None = None) -> AgentRecord:
        record = AgentRecord(
            agent_id=agent_id,
            state=state,
            exit=exit,
            session=session,
            attach=attach,
            input_unconfirmed_at=input_unconfirmed_at,
        )
        self.agents[agent_id] = record
        return record

    def list_agents(self) -> list[AgentRecord]:
        self._maybe_fail("list_agents")
        return list(self.agents.values())

    def get_agent(self, agent_id: str) -> AgentRecord:
        self._maybe_fail("get_agent")
        try:
            return self.agents[agent_id]
        except KeyError:
            raise SidecarError(status=404, code="agent_not_found", detail=f"no agent {agent_id!r}") from None

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
        self._maybe_fail("open_agent")
        self._agent_next += 1
        self.opened_sessions.append((agent_id, session, fresh))
        self.opened_payloads.append(
            {
                "id": agent_id,
                "agent": agent,
                "workdir": workdir,
                "input": input,
                "provider": provider,
                "model": model,
                "tui": tui,
                "session": session,
                "fresh": fresh,
                "agent_args": agent_args,
            }
        )
        record = AgentRecord(
            agent_id=agent_id,
            state="starting",
            session=session,
            extra={
                "workdir": workdir,
                "provider": provider,
                "model": model,
                "input": input,
                "fresh": fresh,
            },
        )
        self.agents[agent_id] = record
        return record
        record = AgentRecord(
            agent_id=agent_id,
            state="starting",
            session=session,
            extra={
                "workdir": workdir,
                "provider": provider,
                "model": model,
                "input": input,
                "fresh": fresh,
            },
        )
        self.agents[agent_id] = record
        return record

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> int:
        self._maybe_fail("poke")
        if agent_id not in self.agents:
            raise SidecarError(status=404, code="agent_not_running", detail=f"no agent {agent_id!r}")
        self.pokes.append((agent_id, text, interrupt))
        return 200

    def close_agent(self, agent_id: str) -> None:
        self._maybe_fail("close_agent")
        if agent_id not in self.agents:
            raise SidecarError(status=404, code="agent_not_found", detail=f"no agent {agent_id!r}")
        self.closed.append(agent_id)
        self.agents.pop(agent_id, None)

    def add_job(self, job_id: str, *, every: object = None, text: str = "", agent_id: str = "",
                last_error: str | None = None, interrupt: bool = False,
                action: str = "poke", last_run: object = None, at: str | None = None,
                auto: bool = False) -> JobRecord:
        """Add one job; `every=None` makes it a one-off `at` job (#370 高-2)."""
        record = JobRecord(
            job_id=job_id,
            action=action,
            args={"id": agent_id, "text": text, "interrupt": interrupt},
            every=None if every is None else seconds_of(every),
            last_run=last_run,
            last_error=last_error,
            at=at,
            extra={"auto": True} if auto else {},
        )
        self.jobs[job_id] = record
        return record

    def list_jobs(self) -> list[JobRecord]:
        self._maybe_fail("list_jobs")
        return list(self.jobs.values())

    def create_job(self, *, every: str, agent_id: str, text: str, interrupt: bool = False) -> JobRecord:
        self._maybe_fail("create_job")
        self._job_next += 1
        record = self.add_job(
            f"job-{self._job_next}", every=every, text=text, agent_id=agent_id, interrupt=interrupt
        )
        return record

    def delete_job(self, job_id: str) -> bool:
        self._maybe_fail("delete_job")
        self.deleted_jobs.append(job_id)
        existed = job_id in self.jobs
        self.jobs.pop(job_id, None)
        return existed


class FakeSidecars:
    """A `Deps.sidecar_for` over several fake sidecars.

    The real port is looked up by address (#356): a team's `sidecar` and the
    PMO's `pmo_sidecar` may name different machines. `default` answers every
    address the mapping does not name, which is the shape most tests want (one
    fake for everything); `addresses` records what was looked up so a test can
    tell a routed call from a wrong one.
    """

    def __init__(
        self,
        by_address: dict[str, FakeSidecar] | None = None,
        *,
        default: FakeSidecar | None = None,
    ) -> None:
        self.by_address = dict(by_address or {})
        self.default = default
        self.addresses: list[str] = []

    def __call__(self, address: str) -> FakeSidecar:
        self.addresses.append(address)
        if address in self.by_address:
            return self.by_address[address]
        if self.default is not None:
            return self.default
        raise AssertionError(f"no fake sidecar for {address!r}")


class FakeGithub:
    """A fixed issue table: `issue_view` looks up `"<owner/repo>#<n>"` keys."""

    def __init__(self, issues: dict[str, IssueData] | None = None) -> None:
        self.issues: dict[str, IssueData] = dict(issues or {})
        self.calls: list[tuple[str, int]] = []

    def add_issue(self, repo: str, number: int, title: str, body: str) -> None:
        self.issues[f"{repo}#{number}"] = IssueData(title=title, body=body)

    def issue_view(self, repo: str, number: int) -> IssueData:
        self.calls.append((repo, number))
        try:
            return self.issues[f"{repo}#{number}"]
        except KeyError:
            raise PortError(f"the issue {repo}#{number} is not readable here") from None
