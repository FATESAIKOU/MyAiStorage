"""Unit tests for ModelBroker interface and FakeModelBroker."""

from __future__ import annotations

from sidecar.model_broker import FakeModelBroker


def test_fake_model_broker_grant_and_release():
    broker = FakeModelBroker(default_access={"OPENAI_API_KEY": "test-key"})

    lease = broker.request_lease(
        worker="worker-1",
        role="coder",
        coding_agent="opencode",
        model="claude-3-7-sonnet",
    )

    assert lease.granted is True
    assert lease.lease_id.startswith("lease_")
    assert lease.worker == "worker-1"
    assert lease.role == "coder"
    assert lease.coding_agent == "opencode"
    assert lease.model == "claude-3-7-sonnet"
    assert lease.access == {"OPENAI_API_KEY": "test-key"}
    assert lease.expires_at is not None

    assert lease.lease_id in broker.active_leases
    assert len(broker.requests) == 1

    # Release
    broker.release(lease.lease_id)
    assert lease.lease_id not in broker.active_leases
    assert lease.lease_id in broker.released_leases


def test_fake_model_broker_rejection():
    broker = FakeModelBroker(default_granted=False, default_reason="quota exhausted")

    lease = broker.request_lease(
        worker="worker-2",
        role="reviewer",
        coding_agent="claude",
        model="gpt-4o",
    )

    assert lease.granted is False
    assert lease.reason == "quota exhausted"
    assert lease.access == {}
    assert lease.lease_id not in broker.active_leases


def test_fake_model_broker_per_rule_override():
    broker = FakeModelBroker(default_granted=True)
    broker.set_rule("intern", "gemini-ultra", granted=False, reason="interns cannot use ultra")

    lease_ok = broker.request_lease("worker-1", "engineer", "opencode", "gemini-ultra")
    assert lease_ok.granted is True

    lease_denied = broker.request_lease("worker-1", "intern", "opencode", "gemini-ultra")
    assert lease_denied.granted is False
    assert lease_denied.reason == "interns cannot use ultra"
