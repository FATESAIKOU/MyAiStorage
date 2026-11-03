"""Unit tests for the four pmo ports and their fakes (`#110` T-1, design §3.1, §3.3).

The point of the fakes is that a consumer written against the Protocols runs
unchanged against them, so the tests below are written the way T-2..T-5 will be:
a small function that only knows the port. Nothing here reaches a real
Angareion, aicomm, sidecar, GitHub or HOME.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from pmo.fakes import FakeAicomm, FakeAngareion, FakeGithub, FakeSidecar
from pmo.ports import (
    AgentRecord,
    AicommPort,
    AngareionPort,
    GitHubPort,
    IssueData,
    JobRecord,
    Message,
    PortError,
    SidecarError,
    SidecarPort,
    seconds_of,
)


def _message(message_id: str = "01JMSG", body: str = "hello") -> Message:
    return Message(
        message_id=message_id,
        channel="team-myaistorage-m1",
        sender="myaistorage-pm-m1",
        to="mypmo-pm-m1",
        received_at="2026-11-03T01:00:00Z",
        body=body,
        urgency=5,
        in_reply_to=None,
    )


# --- the fakes stand in for the four ports ---------------------------------


@pytest.mark.parametrize(
    ["fake", "port"],
    [
        (FakeAngareion(), AngareionPort),
        (FakeAicomm(), AicommPort),
        (FakeSidecar(), SidecarPort),
        (FakeGithub(), GitHubPort),
    ],
    ids=["angareion", "aicomm", "sidecar", "github"],
)
def test_each_fake_has_the_port_it_stands_in_for(fake, port):
    assert isinstance(fake, port)


def test_a_consumer_of_the_angareion_port_runs_against_the_fake():
    def drain(port: AngareionPort) -> list[str]:
        return [message.message_id for message in port.read()]

    fake = FakeAngareion([_message("01JA"), _message("01JB")])
    assert drain(fake) == ["01JA", "01JB"]
    assert drain(fake) == []  # read drains the queue, the way a cursor would


def test_a_consumer_of_the_aicomm_port_runs_against_the_fake():
    def reply(port: AicommPort, body: str) -> str:
        return port.send(channel="team-myaistorage-m1", to="myaistorage-pm-m1", body=body)

    fake = FakeAicomm()
    assert reply(fake, "收到 myaistorage#352").startswith("fake-msg-")
    assert fake.sent[0].body == "收到 myaistorage#352"


def test_a_consumer_of_the_sidecar_port_runs_against_the_fake():
    def poke(port: SidecarPort, agent_id: str) -> int:
        return port.poke(agent_id, text="停下來，等指示", interrupt=True)

    fake = FakeSidecar()
    fake.add_agent("myaistorage-impl-m1-1")
    assert poke(fake, "myaistorage-impl-m1-1") == 200  # the port answers with the status
    assert fake.pokes == [("myaistorage-impl-m1-1", "停下來，等指示", True)]


def test_a_consumer_of_the_github_port_runs_against_the_fake():
    def title(port: GitHubPort) -> str:
        return port.issue_view("FATESAIKOU/MyAiStorage", 352).title

    fake = FakeGithub()
    fake.add_issue("FATESAIKOU/MyAiStorage", 352, "pmo 骨架", "body")
    assert title(fake) == "pmo 骨架"


# --- FakeAngareion ---------------------------------------------------------


def test_the_angareion_fake_records_what_was_sent():
    fake = FakeAngareion()
    message_id = fake.send(
        channel="team-myaistorage-m1",
        to="myaistorage-pm-m1",
        body="收到",
        in_reply_to="01JWORK",
        urgency=8,
        ref="myaistorage#352",
    )
    sent = fake.sent[0]
    assert sent.message_id == message_id
    assert (sent.channel, sent.to, sent.body) == ("team-myaistorage-m1", "myaistorage-pm-m1", "收到")
    assert (sent.in_reply_to, sent.urgency, sent.ref) == ("01JWORK", 8, "myaistorage#352")


def test_the_angareion_fake_logs_calls_without_draining_the_queue():
    # 假物件照真 CLI 的合約過濾（#355 高-1）：channel 對不上就撈不到。
    fake = FakeAngareion([_message()], logged=[_message("01JLOG")])
    assert [
        message.message_id
        for message in fake.log(channel="team-myaistorage-m1", since="2026-11-03T00:00:00Z")
    ] == ["01JLOG"]
    assert fake.log_calls == [
        {
            "channel": "team-myaistorage-m1",
            "sender": None,
            "to": None,
            "thread": None,
            "grep": None,
            "since": "2026-11-03T00:00:00Z",
        }
    ]
    assert len(fake.pending) == 1  # log is not a read


def test_the_angareion_fake_records_read_calls():
    fake = FakeAngareion()
    fake.read()
    fake.read(since="2026-11-03T00:00:00Z")
    assert fake.read_calls == [None, "2026-11-03T00:00:00Z"]


# --- FakeAicomm -----------------------------------------------------------


def test_the_aicomm_fake_records_ticket_urgency_and_no_wake():
    fake = FakeAicomm()
    fake.send(channel="team-myaistorage-m1", to="myaistorage-pm-m1", body="收到", ticket="T-1",
              urgency=5, no_wake=True)
    sent = fake.sent[0]
    assert (sent.ticket, sent.urgency, sent.no_wake) == ("T-1", 5, True)


def test_the_aicomm_fake_can_fail_every_send():
    fake = FakeAicomm()
    fake.always_fail = PortError("aicomm timed out")
    with pytest.raises(PortError, match="timed out"):
        fake.send(channel="c", to="a", body="b")
    assert fake.sent == []


# --- FakeSidecar ----------------------------------------------------------


def test_the_sidecar_fake_serves_agents():
    fake = FakeSidecar()
    fake.add_agent("myaistorage-pm-m1", state="running", session="ses-1")
    fake.add_agent("myaistorage-impl-m1-1", state="exited", session=None,
                   exit={"reason": "no candidate is left after exclude"})
    assert [agent.agent_id for agent in fake.list_agents()] == ["myaistorage-pm-m1", "myaistorage-impl-m1-1"]
    assert fake.get_agent("myaistorage-impl-m1-1").exit_reason() == "no candidate is left after exclude"
    assert fake.get_agent("myaistorage-pm-m1").exit_reason() is None


def test_the_sidecar_fake_refuses_an_unknown_agent():
    fake = FakeSidecar()
    with pytest.raises(SidecarError) as caught:
        fake.get_agent("nobody")
    assert caught.value.status == 404 and caught.value.code == "agent_not_found"
    with pytest.raises(SidecarError):
        fake.poke("nobody", text="x")
    with pytest.raises(SidecarError):
        fake.close_agent("nobody")


def test_the_sidecar_fake_closes_an_agent_and_forgets_it():
    fake = FakeSidecar()
    fake.add_agent("myaistorage-pm-m1")
    fake.close_agent("myaistorage-pm-m1")
    assert fake.closed == ["myaistorage-pm-m1"]
    with pytest.raises(SidecarError):
        fake.get_agent("myaistorage-pm-m1")


def test_the_sidecar_fake_opens_an_agent():
    fake = FakeSidecar()
    record = fake.open_agent(agent_id="myaistorage-impl-m1-1", workdir="/tmp/w", provider="opencode",
                             model="glm", input="read BRIEF.md")
    assert record.state == "starting"
    assert fake.get_agent("myaistorage-impl-m1-1") == record


def test_the_sidecar_fake_records_session_and_fresh():
    """Architect 中-4: `pmo open` passes `--session`／`--fresh` through the port."""
    fake = FakeSidecar()
    fake.open_agent(agent_id="a1", workdir="/tmp/w", provider="opencode", model="glm", input="i",
                    session="agora:ses-1", fresh=True)
    assert fake.opened_sessions == [("a1", "agora:ses-1", True)]
    record = fake.get_agent("a1")
    assert record.session == "agora:ses-1"


def test_the_sidecar_fake_defaults_to_no_session_and_not_fresh():
    fake = FakeSidecar()
    fake.open_agent(agent_id="a1", workdir="/tmp/w", provider="opencode", model="glm", input="i")
    assert fake.opened_sessions == [("a1", None, False)]


def test_the_sidecar_fake_reports_job_every_in_seconds():
    """`GET /jobs` answers seconds (`600.0`), whatever shape `POST /jobs` took (M5-1)."""
    fake = FakeSidecar()
    job = fake.create_job(every="10m", agent_id="mypmo-pm-m1", text="定時叫醒：請跑 pmo tick")
    assert job.every == 600.0
    assert fake.list_jobs()[0].every == 600.0
    assert job.args == {"id": "mypmo-pm-m1", "text": "定時叫醒：請跑 pmo tick", "interrupt": False}


def test_the_sidecar_fake_deletes_a_job_and_says_whether_it_existed():
    fake = FakeSidecar()
    fake.add_job("job-1", every="10m", text="x", agent_id="mypmo-pm-m1")
    assert fake.delete_job("job-1") is True
    assert fake.delete_job("job-1") is False
    assert fake.deleted_jobs == ["job-1", "job-1"]


@pytest.mark.parametrize(
    "method",
    ["list_agents", "get_agent", "open_agent", "poke", "close_agent", "list_jobs", "create_job", "delete_job"],
)
def test_every_sidecar_call_can_be_made_to_fail(method):
    fake = FakeSidecar()
    fake.add_agent("myaistorage-pm-m1")
    fake.errors[method] = SidecarError(status=409, code="agent_not_running")
    calls = {
        "list_agents": lambda: fake.list_agents(),
        "get_agent": lambda: fake.get_agent("myaistorage-pm-m1"),
        "open_agent": lambda: fake.open_agent(
            agent_id="a", workdir="/tmp/w", provider="p", model="m", input="i"
        ),
        "poke": lambda: fake.poke("myaistorage-pm-m1", text="x"),
        "close_agent": lambda: fake.close_agent("myaistorage-pm-m1"),
        "list_jobs": lambda: fake.list_jobs(),
        "create_job": lambda: fake.create_job(every="10m", agent_id="a", text="x"),
        "delete_job": lambda: fake.delete_job("job-1"),
    }
    with pytest.raises(SidecarError) as caught:
        calls[method]()
    assert caught.value.code == "agent_not_running"


def test_the_sidecar_fake_reports_a_job_error_and_a_last_run():
    fake = FakeSidecar()
    job = fake.add_job("job-1", every="10m", text="x", agent_id="mypmo-pm-m1",
                       last_error="agent_awaiting_answer", last_run="2026-11-03T01:00:00Z")
    assert job.last_error == "agent_awaiting_answer"
    assert job.last_run == "2026-11-03T01:00:00Z"


# --- FakeGithub -----------------------------------------------------------


def test_the_github_fake_refuses_an_issue_it_does_not_have():
    fake = FakeGithub()
    with pytest.raises(PortError):
        fake.issue_view("FATESAIKOU/MyAiStorage", 999)
    assert fake.calls == [("FATESAIKOU/MyAiStorage", 999)]


def test_the_github_fake_returns_title_and_body():
    fake = FakeGithub({"o/r#1": IssueData(title="t", body="b")})
    assert fake.issue_view("o/r", 1) == IssueData(title="t", body="b")


# --- the records ---------------------------------------------------------


@pytest.mark.parametrize(
    ["value", "seconds"],
    [
        ("10m", 600.0),
        ("2h", 7200.0),
        ("30s", 30.0),
        ("45", 45.0),
        (600.0, 600.0),
        (600, 600.0),
        (" 10m ", 600.0),
    ],
)
def test_seconds_of_takes_both_shapes(value, seconds):
    assert seconds_of(value) == seconds


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, -600.0, "10x", "", "m", "1.5m", [], {}, float("nan"), float("inf"), float("-inf")],
)
def test_seconds_of_refuses_what_is_not_a_duration(value):
    with pytest.raises(SidecarError):
        seconds_of(value)


def test_a_sidecar_error_carries_the_status_and_the_code():
    error = SidecarError(status=409, code="agent_not_running")
    assert (error.status, error.code) == (409, "agent_not_running")
    assert str(error) == "agent_not_running"
    assert str(SidecarError(status=500)) == "sidecar error (status 500)"


def test_an_agent_record_writes_only_its_known_fields():
    """Architect 低-2: `extra` stays on the object; `to_dict` is what gets printed."""
    record = AgentRecord(agent_id="a1", state="running", session="ses-1", extra={"workdir": "/tmp/w"})
    assert record.to_dict() == {"id": "a1", "state": "running", "session": "ses-1"}
    assert record.extra == {"workdir": "/tmp/w"}


def test_a_job_record_writes_only_its_known_fields():
    job = JobRecord(job_id="job-1", action="poke", args={"id": "a"}, every=600.0, extra={"owner": "?"})
    assert job.to_dict() == {
        "id": "job-1",
        "action": "poke",
        "args": {"id": "a"},
        "every": 600.0,
        "last_run": None,
        "last_error": None,
    }
    assert job.extra == {"owner": "?"}
