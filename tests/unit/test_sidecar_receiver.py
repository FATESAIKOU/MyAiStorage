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

    sender = AngareionRelaySender(backend, default_channel="general")
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
    assert all_msgs[0].from_ == {"repo": "sidecar", "role": "team-worker"}
