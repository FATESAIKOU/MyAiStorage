"""Unit tests for team lifecycle (Task 3.4, Design D4, D10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from angareion.backend import FakeBackend
from angareion.messages import Message
from sidecar.config import SidecarConfig
from sidecar.herdr import FakeHerdrClient
from sidecar.host import AgentHost
from sidecar.queue import MessageQueue
from sidecar.receiver import Receiver
from sidecar.registry import Registry


@pytest.fixture(autouse=True)
def clean_herdr_context(monkeypatch):
    for var in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID"):
        monkeypatch.delenv(var, raising=False)


def test_start_team_reads_config_and_loads_harness(tmp_path):
    harness_file = tmp_path / "harness.md"
    harness_file.write_text("## Team Harness\nFollow instructions verbatim.\n", encoding="utf-8")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={
            "worker-team": {
                "agent": "opencode",
                "home_session": "agora:01JTEAMHOME",
                "harness_path": str(harness_file),
                "sync_period": 10,
                "cwd": "/workspace/team",
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    rec = host.start_team("worker-team", fork_timeout_ms=0)

    assert rec.status == "running"
    assert rec.identity == "worker-team"
    assert rec.agora_session == "agora:01JTEAMHOME"
    assert rec.agent_kind == "opencode"

    # Verify agora continue command generated (sync_period is minutes, D10;
    # fork sessions carry the team identity header, D11/3.9)
    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][1] == (
        "agora continue session agora:01JTEAMHOME --agent opencode --fork --sync-period 10"
        " --header sidecar.identity=worker-team"
    )

    # Verify harness was passed verbatim as first input
    prompt_call = next(c for c in fake_herdr.calls if c[0] == "agent_prompt")
    assert prompt_call[1][0] == rec.agent_name
    assert prompt_call[1][1] == "## Team Harness\nFollow instructions verbatim.\n"

    # Export returns agora session
    exported = host.export_agent_session(rec.agent_name)
    assert exported == "agora:01JTEAMHOME"


def test_terminate_team_escalates_and_marks_terminated(tmp_path):
    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={
            "worker-team": {
                "agent": "claude",
                "home_session": "agora:01JCLAUDEHOME",
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)

    signals: list[int] = []

    def mock_kill_pg(pgid: int, sig: int):
        signals.append(sig)

    host = AgentHost(fake_herdr, reg, config=cfg, kill_pg_func=mock_kill_pg)
    rec = host.start_team("worker-team", fork_timeout_ms=0)

    # Terminate team
    res = host.terminate_team("worker-team", timeout_stage=0.01)
    assert res is True
    assert reg.get(rec.agent_id).status == "terminated"

    # Terminating again when no active agent returns False
    assert host.terminate_team("worker-team") is False


def test_start_team_missing_home_session_raises(tmp_path):
    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["no-home-team"],
        teams={"no-home-team": {"agent": "opencode"}},
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    with pytest.raises(ValueError, match="No home session configured"):
        host.start_team("no-home-team")


def test_start_team_default_sync_period_is_5_minutes(tmp_path):
    """D10: sync_period is minutes, default 5, passed through verbatim."""
    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={
            "worker-team": {
                "agent": "opencode",
                "home_session": "agora:01JTEAMHOME",
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    host.start_team("worker-team", fork_timeout_ms=0)

    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][1] == (
        "agora continue session agora:01JTEAMHOME --agent opencode --fork --sync-period 5"
        " --header sidecar.identity=worker-team"
    )


def test_start_team_waits_for_prompt_then_harness_then_hand_off(tmp_path):
    """H2/D10: start_team waits for the prompt, sends harness first, then
    deliver_hand_off flushes queued low-urgency messages."""
    harness_file = tmp_path / "harness.md"
    harness_file.write_text("## Team Harness\n", encoding="utf-8")

    backend = FakeBackend()
    backend.create_channel("general")

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={
            "worker-team": {
                "agent": "opencode",
                "home_session": "agora:01JTEAMHOME",
                "harness_path": str(harness_file),
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)

    # A low-urgency message arrives while no agent runs: stays queued.
    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=3,
        content="Routine work item",
    )
    backend.post_message("general", msg)
    assert receiver.poll_once() == 1
    assert queue.count("worker-team") == 1

    rec = host.start_team("worker-team", receiver=receiver, fork_timeout_ms=0)

    # Prompt wait happened before the harness went in.
    call_names = [c[0] for c in fake_herdr.calls]
    first_wait = call_names.index("pane_wait_output")
    first_prompt = call_names.index("agent_prompt")
    assert first_wait < first_prompt

    # Harness first, hand-off second; queue drained and acked.
    assert fake_herdr.prompts[0] == (rec.agent_name, "## Team Harness\n")
    assert fake_herdr.prompts[1] == (rec.agent_name, "Routine work item")
    assert queue.count("worker-team") == 0
    all_msgs = backend.list_messages().messages
    assert any(m.ack == msg.id for m in all_msgs)


def test_start_team_fork_carries_identity_and_message_headers(tmp_path):
    """3.9/D11: fork sessions carry sidecar.identity and sidecar.message."""
    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={
            "worker-team": {
                "agent": "opencode",
                "home_session": "agora:01JTEAMHOME",
                "identity": {"repo": "worker", "role": "worker-team", "name": "opencode-1"},
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    host.start_team("worker-team", trigger_message_id="msg_trigger_1", fork_timeout_ms=0)

    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][1] == (
        "agora continue session agora:01JTEAMHOME --agent opencode --fork --sync-period 5"
        " --header sidecar.identity=worker-team --header sidecar.message=msg_trigger_1"
    )

    # Without a triggering message only the identity header is carried.
    fake_herdr.calls.clear()
    host.start_team("worker-team", fork_timeout_ms=0)
    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][1] == (
        "agora continue session agora:01JTEAMHOME --agent opencode --fork --sync-period 5"
        " --header sidecar.identity=worker-team"
    )


def test_start_team_quotes_wire_values_in_shell_command(tmp_path):
    """batch3: wire values (message ids) are shell-quoted into one word."""
    import shlex as _shlex

    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={
            "worker-team": {
                "agent": "opencode",
                "home_session": "agora:01JTEAMHOME",
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    evil = "msg x; rm -rf /tmp/pwn $(evil)"
    host.start_team("worker-team", trigger_message_id=evil, fork_timeout_ms=0)

    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    cmd = pane_run_call[1][1]
    assert _shlex.split(cmd) == [
        "agora", "continue", "session", "agora:01JTEAMHOME",
        "--agent", "opencode", "--fork", "--sync-period", "5",
        "--header", "sidecar.identity=worker-team",
        "--header", f"sidecar.message={evil}",
    ]
