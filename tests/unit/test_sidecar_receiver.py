"""Unit tests for Angareion receiver loop, queueing, delivery, and ACK (Task 3.3, Design D3, D8)."""

from __future__ import annotations

import pytest

from angareion.backend import FakeBackend
from angareion.messages import Message
from sidecar.config import SidecarConfig
from sidecar.herdr import FakeHerdrClient
from sidecar.host import AgentHost
from sidecar.queue import MessageQueue
from sidecar.receiver import Receiver
from sidecar.registry import Registry
from sidecar.relay import AngareionRelaySender


@pytest.fixture(autouse=True)
def clean_herdr_context(monkeypatch):
    for var in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID"):
        monkeypatch.delenv(var, raising=False)


def test_receiver_high_urgency_interrupts_and_acks(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    # Start an agent that is working
    rec = host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")
    fake_herdr.agent_outputs["worker-1"] = "Working on task...\nesc interrupt"

    # Hook send_keys to clear screen indicator after 2 esc
    real_send_keys = fake_herdr.agent_send_keys

    def spy_send_keys(target, *keys):
        res = real_send_keys(target, *keys)
        fake_herdr.agent_outputs[target] = "Ready for prompt > "
        return res

    fake_herdr.agent_send_keys = spy_send_keys

    receiver = Receiver(backend, cfg, reg, queue, host)

    # Post urgency 9 message
    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=9,
        content="Emergency interrupt instruction",
    )
    backend.post_message("general", msg)

    # Poll once
    processed = receiver.poll_once()
    assert processed == 1

    # Delivered: prompt was submitted, esc was sent, queue is empty
    assert ("worker-1", "Emergency interrupt instruction") in fake_herdr.prompts
    assert queue.count("worker-team") == 0

    # Ack was posted to backend
    all_msgs = backend.list_messages().messages
    acks = [m for m in all_msgs if m.ack == msg.id]
    assert len(acks) == 1
    assert acks[0].ack == msg.id
    assert acks[0].from_ == {"repo": "sidecar", "role": "worker-team"}


def test_receiver_low_urgency_queues_and_hand_off_delivers(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)

    # No agent running: post urgency 3 message
    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=3,
        content="Routine work item",
    )
    backend.post_message("general", msg)

    processed = receiver.poll_once()
    assert processed == 1

    # Agent was not running: message remains in persistent queue, NOT yet acked
    assert queue.count("worker-team") == 1
    all_msgs = backend.list_messages().messages
    assert not any(m.ack == msg.id for m in all_msgs)

    # Start agent and perform hand-off delivery
    rec = host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")
    delivered = receiver.deliver_hand_off("worker-team", "worker-1")
    assert delivered == 1
    assert queue.count("worker-team") == 0

    # Now it is delivered into agent input and ACK is posted
    assert ("worker-1", "Routine work item") in fake_herdr.prompts
    all_msgs_after = backend.list_messages().messages
    acks = [m for m in all_msgs_after if m.ack == msg.id]
    assert len(acks) == 1


def test_receiver_deduplication_via_seen_json(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)

    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=5,
        content="Do not duplicate me",
    )
    backend.post_message("general", msg)

    assert receiver.poll_once() == 1
    assert queue.count("worker-team") == 1

    # Reset last_etag to force re-reading the same list of messages
    receiver.last_etag = None
    # SeenStore must prevent duplicate enqueue
    assert receiver.poll_once() == 0
    assert queue.count("worker-team") == 1


def test_receiver_restart_recovery_persists_queue(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver1 = Receiver(backend, cfg, reg, queue, host)

    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=4,
        content="Survives restart",
    )
    backend.post_message("general", msg)
    assert receiver1.poll_once() == 1
    assert queue.count("worker-team") == 1

    # Simulate sidecar crash and restart: create fresh Receiver with new instances pointing to state_dir
    reg2 = Registry(state_dir)
    queue2 = MessageQueue(state_dir)
    host2 = AgentHost(fake_herdr, reg2, config=cfg)
    receiver2 = Receiver(backend, cfg, reg2, queue2, host2)

    assert queue2.count("worker-team") == 1
    rec = host2.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")
    delivered = receiver2.deliver_hand_off("worker-team", "worker-1")
    assert delivered == 1
    assert queue2.count("worker-team") == 0


def test_angareion_relay_sender_integration():
    backend = FakeBackend()
    backend.create_channel("general")

    team_addresses = {
        "team-worker": {"repo": "worker", "role": "worker-team", "name": "opencode-1"},
    }
    sender = AngareionRelaySender(
        backend, default_channel="general", team_addresses=team_addresses
    )
    res = sender.send(
        from_identity="team-worker",
        to_identity="pmo",
        content="Report via Angareion backend",
        urgency=6,
    )

    assert res["from"] == "team-worker"
    assert res["to"] == "pmo"
    assert res["content"] == "Report via Angareion backend"

    all_msgs = backend.list_messages().messages
    assert len(all_msgs) == 1
    assert all_msgs[0].content == "Report via Angareion backend"
    # L8/D10: from is the team's full configured address, not repo sidecar.
    assert all_msgs[0].from_ == {"repo": "worker", "role": "worker-team", "name": "opencode-1"}


def test_angareion_relay_sender_refuses_missing_channel():
    """L8: the relay never creates channels; a missing one is an error."""
    from angareion.backend import ChannelNotFoundError

    backend = FakeBackend()
    sender = AngareionRelaySender(
        backend,
        default_channel="no-such-channel",
        team_addresses={"team-worker": {"repo": "worker", "role": "worker-team"}},
    )

    with pytest.raises(ChannelNotFoundError, match="does not exist"):
        sender.send(
            from_identity="team-worker",
            to_identity="pmo",
            content="Nowhere to go",
            urgency=5,
        )

    # Nothing was auto-created.
    assert backend.find_channel("no-such-channel") is None
    assert backend.list_messages().messages == []


def test_angareion_relay_sender_requires_full_team_address():
    """L8: without a configured full address the relay refuses to report."""
    backend = FakeBackend()
    backend.create_channel("general")
    sender = AngareionRelaySender(backend, default_channel="general")

    with pytest.raises(ValueError, match="repo and role are required"):
        sender.send(
            from_identity="unconfigured-team",
            to_identity="pmo",
            content="Who am I",
            urgency=5,
        )


def test_receiver_enqueue_failure_does_not_mark_seen(tmp_path, monkeypatch):
    """M1: crashing between queue write and seen-mark must not lose mail.
    The message is neither queued nor seen, so the next poll redelivers it."""
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)

    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=4,
        content="Must survive the crash window",
    )
    backend.post_message("general", msg)

    def boom_enqueue(identity, message):
        raise OSError("disk full in the crash window")

    monkeypatch.setattr(queue, "enqueue", boom_enqueue)

    with pytest.raises(OSError, match="disk full"):
        receiver.poll_once()

    assert queue.count("worker-team") == 0
    assert not receiver.seen.has(msg.id)

    # Restart with a healthy queue on the same state dir: redelivery works.
    reg2 = Registry(state_dir)
    queue2 = MessageQueue(state_dir)
    host2 = AgentHost(fake_herdr, reg2, config=cfg)
    receiver2 = Receiver(backend, cfg, reg2, queue2, host2)

    assert receiver2.poll_once() == 1
    assert queue2.count("worker-team") == 1


def test_receiver_last_since_cursor_filters_old_messages(tmp_path):
    """last_since advances past enqueued messages so later polls only see
    what is new (backend-side since filtering)."""
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)

    msg1 = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=2,
        content="first",
        at="2026-10-11T10:00:00+00:00",
    )
    backend.post_message("general", msg1)
    assert receiver.poll_once() == 1
    assert receiver.last_since == "2026-10-11T10:00:00+00:00"

    msg2 = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=2,
        content="second",
        at="2026-10-11T11:00:00+00:00",
    )
    backend.post_message("general", msg2)
    assert receiver.poll_once() == 1
    assert receiver.last_since == "2026-10-11T11:00:00+00:00"
    assert queue.count("worker-team") == 2


def test_receiver_delivery_failure_stays_queued_and_logs(tmp_path, monkeypatch, caplog):
    """A failed prompt delivery keeps the message queued and logs a warning."""
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    rec = host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")

    def boom_prompt(agent_name, text, **kwargs):
        raise RuntimeError("pane gone")

    monkeypatch.setattr(host, "send_prompt", boom_prompt)
    receiver = Receiver(backend, cfg, reg, queue, host)

    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=3,
        content="Undeliverable for now",
    )
    backend.post_message("general", msg)

    with caplog.at_level("WARNING", logger="sidecar.receiver"):
        assert receiver.poll_once() == 1

    assert queue.count("worker-team") == 1
    assert "delivery failed" in caplog.text
    all_msgs = backend.list_messages().messages
    assert not any(m.ack == msg.id for m in all_msgs)


def test_deliver_hand_off_defers_when_agent_working(tmp_path, caplog):
    """3.12: hand-off requires an idle agent; a working one keeps its queue."""
    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg, sleeper=lambda _: None)
    receiver = Receiver(backend, cfg, reg, queue, host)

    rec = host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")
    fake_herdr.agent_kinds["worker-1"] = "opencode"
    fake_herdr.agents["worker-1"]["agent_status"] = "working"
    queue.enqueue("worker-team", {"id": "m1", "content": "wait your turn",
                                  "channel": "general",
                                  "from": {"repo": "pmo", "role": "PM"}})

    with caplog.at_level("WARNING", logger="sidecar.receiver"):
        delivered = receiver.deliver_hand_off(
            "worker-team", "worker-1",
            wait_timeout_ms=50, ready_poll_interval=0.01,
        )

    assert delivered == 0
    assert queue.count("worker-team") == 1
    assert "not ready" in caplog.text
