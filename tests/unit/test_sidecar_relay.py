"""Unit tests for report relay server and sidecar say command (Task 3.5, Design D6)."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from sidecar.cli import main
from sidecar.herdr import FakeHerdrClient
from sidecar.host import AgentHost
from sidecar.registry import AgentRecord, Registry
from sidecar.relay import (
    FakeRelaySender,
    RelayServer,
    get_peer_pid,
    send_relay_message,
)


@pytest.fixture(autouse=True)
def clean_herdr_context(monkeypatch):
    for var in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def short_sock_path():
    import tempfile, shutil
    td = tempfile.mkdtemp(prefix="sc_", dir="/tmp")
    p = Path(td) / "s.sock"
    yield p
    if p.exists():
        p.unlink(missing_ok=True)
    shutil.rmtree(td, ignore_errors=True)


def test_get_peer_pid_on_socketpair():
    s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid = get_peer_pid(s1)
        # Should return the current process PID on Linux (SO_PEERCRED) or macOS (LOCAL_PEERPID)
        assert pid == os.getpid()
    finally:
        s1.close()
        s2.close()


def test_relay_server_authenticates_peer_and_dispatches_with_team_identity(tmp_path, short_sock_path):
    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    # Register an agent whose process includes the current process PID
    my_pid = os.getpid()
    rec = AgentRecord(
        agent_id="ag_relay_1",
        identity="team-worker",
        agora_session="agora:01JRELAY",
        agent_kind="opencode",
        herdr_tab_id="w1:t1",
        herdr_pane_id="w1:p1",
        agent_name="relay-worker",
        started_at="2026-10-11T12:00:00Z",
        status="running",
        log_path=str(state_dir / "log"),
        updated_at="2026-10-11T12:00:00Z",
        extra={"pids": [my_pid]},
    )
    reg.register(rec)

    fake_herdr = FakeHerdrClient()
    sender = FakeRelaySender()

    server = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=sender,
        herdr=fake_herdr,
    )
    server.start()

    try:
        # Call relay from the authenticated process
        res = send_relay_message(
            content="Task completed successfully",
            to="pmo",
            urgency=7,
            socket_path=sock_path,
        )

        assert res["content"] == "Task completed successfully"
        assert res["to"] == "pmo"
        assert res["urgency"] == 7
        assert res["from"] == "team-worker"

        assert len(sender.sent) == 1
        assert sender.sent[0]["from"] == "team-worker"
        assert sender.sent[0]["content"] == "Task completed successfully"
    finally:
        server.stop()


def test_relay_server_rejects_unregistered_peer(tmp_path, short_sock_path):
    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    # Register agent with a different non-matching PID
    rec = AgentRecord(
        agent_id="ag_relay_other",
        identity="team-other",
        agora_session="agora:01JOTHER",
        agent_kind="opencode",
        herdr_tab_id="w1:t1",
        herdr_pane_id="w1:p1",
        agent_name="other-worker",
        started_at="2026-10-11T12:00:00Z",
        status="running",
        log_path=str(state_dir / "log"),
        updated_at="2026-10-11T12:00:00Z",
        extra={"pids": [99999999]},
    )
    reg.register(rec)

    fake_herdr = FakeHerdrClient()
    sender = FakeRelaySender()

    server = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=sender,
        herdr=fake_herdr,
        peer_checker=lambda pid, agent: False,  # Explicitly reject
    )
    server.start()

    try:
        with pytest.raises(RuntimeError, match="Unauthorized"):
            send_relay_message(
                content="Unauthorized attempt",
                to="pmo",
                socket_path=sock_path,
            )
        assert len(sender.sent) == 0
    finally:
        server.stop()


def test_cli_sidecar_say_command(tmp_path, capsys, short_sock_path):
    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    rec = AgentRecord(
        agent_id="ag_relay_say",
        identity="team-say",
        agora_session="agora:01JSAY",
        agent_kind="claude",
        herdr_tab_id="w1:t1",
        herdr_pane_id="w1:p1",
        agent_name="say-worker",
        started_at="2026-10-11T12:00:00Z",
        status="running",
        log_path=str(state_dir / "log"),
        updated_at="2026-10-11T12:00:00Z",
        extra={"pids": [os.getpid()]},
    )
    reg.register(rec)

    sender = FakeRelaySender()
    server = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=sender,
    )
    server.start()

    try:
        # Test text output
        code = main(["say", "PR opened at #100", "--to", "pmo", "--sock", str(sock_path)])
        assert code == 0
        out, _ = capsys.readouterr()
        assert "Sent message to pmo" in out
        assert "team-say" in out

        # Test --json output
        code = main(["say", "Follow up message", "--to", "arch", "--sock", str(sock_path), "--json"])
        assert code == 0
        out_json, _ = capsys.readouterr()
        data = json.loads(out_json)
        assert data["ok"] is True
        assert data["result"]["from"] == "team-say"
        assert data["result"]["to"] == "arch"
    finally:
        server.stop()


def test_agent_environment_isolation_no_a2a_token(tmp_path, monkeypatch):
    """Verify that agent started by host does not receive A2A tokens in environment (D6)."""
    monkeypatch.setenv("A2A_TOKEN", "super-secret-token")
    monkeypatch.setenv("A2A_TOKEN_FILE", "/secret/token")

    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JENV",
        agent_kind="opencode",
        agent_name="isolated-worker",
        env={"CUSTOM_KEY": "val"},
    )

    # Check tab_create env passed to herdr
    tab_create_call = next(c for c in fake_herdr.calls if c[0] == "tab_create")
    passed_env = tab_create_call[1][2]
    assert passed_env is not None
    assert "CUSTOM_KEY" in passed_env
    # The agent environment passed to herdr MUST NOT contain A2A tokens
    assert "A2A_TOKEN" not in passed_env
    assert "A2A_TOKEN_FILE" not in passed_env
