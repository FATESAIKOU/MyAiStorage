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
    authenticate_peer,
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


def _register_running_agent(reg, state_dir, identity="team-worker", pids=None):
    rec = AgentRecord(
        agent_id=f"ag_{identity}",
        identity=identity,
        agora_session="agora:01JRELAY",
        agent_kind="opencode",
        herdr_tab_id="w1:t1",
        herdr_pane_id="w1:p1",
        agent_name=f"{identity}-worker",
        started_at="2026-10-11T12:00:00Z",
        status="running",
        log_path=str(state_dir / "log"),
        updated_at="2026-10-11T12:00:00Z",
        extra={"pids": pids} if pids is not None else None,
    )
    reg.register(rec)
    return rec


def test_authenticate_peer_rejects_foreground_pgid_1(tmp_path):
    """L1: when the pane's foreground pgid is 1, no process is admitted,
    not even one whose ancestor chain (always ending at 1) intersects."""
    import os as _os

    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    _register_running_agent(reg, state_dir)

    fake_herdr = FakeHerdrClient()
    fake_herdr.process_infos["w1:p1"] = {
        "foreground_process_group_id": 1,
        "shell_pid": 1,
        "foreground_processes": [{"pid": 1}],
        "pane_id": "w1:p1",
    }

    # An unrelated local process must not authenticate through pid 1.
    assert authenticate_peer(_os.getpid(), reg, herdr=fake_herdr) is None
    assert authenticate_peer(1, reg, herdr=fake_herdr) is None


def test_relay_rejects_out_of_range_urgency(tmp_path, short_sock_path):
    """L2/D10: urgency outside 0-9 is an explicit error, not a silent drop."""
    import socket as _socket

    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    _register_running_agent(reg, state_dir, pids=[os.getpid()])

    server = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=FakeHerdrClient(),
    )
    server.start()

    def raw_request(payload: dict) -> dict:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            s.connect(str(sock_path))
            s.sendall(json.dumps(payload).encode("utf-8"))
            raw = s.recv(65536)
            assert raw, "server must answer, never stay silent"
            return json.loads(raw.decode("utf-8"))
        finally:
            s.close()

    try:
        for bad in (10, -1, 99):
            res = raw_request({"content": "out of range", "urgency": bad})
            assert res["ok"] is False
            assert "urgency" in res["error"]

        ok = raw_request({"content": "in range", "urgency": 9})
        assert ok["ok"] is True
    finally:
        server.stop()


def test_send_relay_message_rejects_out_of_range_urgency_client_side(short_sock_path):
    """L2: the client refuses out-of-range urgency without touching the socket."""
    with pytest.raises(ValueError, match="urgency must be an integer between 0 and 9"):
        send_relay_message(content="x", urgency=10, socket_path=short_sock_path)
    with pytest.raises(ValueError, match="urgency must be an integer between 0 and 9"):
        send_relay_message(content="x", urgency=-1, socket_path=short_sock_path)


def test_say_urgency_help_says_0_to_9(capsys):
    """L2/D10: CLI documents the Angareion 0-9 range."""
    from sidecar.cli import build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["say", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--urgency" in help_text
    assert "0-9" in help_text
    assert "0-10" not in help_text


def test_relay_server_refuses_live_socket_takeover(tmp_path, short_sock_path):
    """L5: starting a second server on a live socket raises instead of
    stealing it; the first server keeps serving."""
    import os as _os

    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    _register_running_agent(reg, state_dir, pids=[_os.getpid()])

    sender_a = FakeRelaySender()
    server_a = RelayServer(
        socket_path=sock_path, registry=reg, sender=sender_a, herdr=FakeHerdrClient()
    )
    server_a.start()

    server_b = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=FakeHerdrClient(),
    )
    try:
        with pytest.raises(RuntimeError, match="already served by a live server"):
            server_b.start()

        res = send_relay_message(content="still A", to="pmo", socket_path=sock_path)
        assert res["content"] == "still A"
        assert len(sender_a.sent) == 1
    finally:
        server_a.stop()
        server_b.stop()


def test_relay_server_replaces_stale_socket_file(tmp_path, short_sock_path):
    """L5: a stale (non-served) file at the socket path is replaced."""
    sock_path = short_sock_path
    sock_path.write_text("leftover", encoding="utf-8")

    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    server = RelayServer(
        socket_path=sock_path, registry=reg, sender=FakeRelaySender()
    )
    try:
        server.start()
        assert sock_path.exists()
    finally:
        server.stop()


def test_relay_socket_and_dir_permissions(tmp_path, monkeypatch, short_sock_path):
    """L6/3.12: socket is 0600 always; the directory is tightened to 0700
    only when it is the default state dir, never a custom path."""
    import shutil as _shutil
    import stat as _stat
    import tempfile as _tempfile

    # Custom directory: permissions left alone.
    short_sock_path.parent.chmod(0o755)
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    server = RelayServer(
        socket_path=short_sock_path, registry=reg, sender=FakeRelaySender()
    )
    try:
        server.start()
        assert _stat.S_IMODE(short_sock_path.stat().st_mode) == 0o600
        assert _stat.S_IMODE(short_sock_path.parent.stat().st_mode) == 0o755
    finally:
        server.stop()

    # Default state dir: tightened to 0700.
    home = Path(_tempfile.mkdtemp(prefix="sch_", dir="/tmp"))
    monkeypatch.setenv("HOME", str(home))
    try:
        default_sock = home / ".local" / "state" / "sidecar" / "t.sock"
        default_sock.parent.mkdir(parents=True, exist_ok=True)
        default_sock.parent.chmod(0o755)
        server2 = RelayServer(
            socket_path=default_sock, registry=reg, sender=FakeRelaySender()
        )
        try:
            server2.start()
            assert _stat.S_IMODE(default_sock.stat().st_mode) == 0o600
            assert _stat.S_IMODE(default_sock.parent.stat().st_mode) == 0o700
        finally:
            server2.stop()
    finally:
        _shutil.rmtree(home, ignore_errors=True)


def test_relay_invalid_json_gets_error_response(tmp_path, short_sock_path):
    """L7: malformed payloads get an explicit error, never silence."""
    import socket as _socket

    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    _register_running_agent(reg, state_dir, pids=[os.getpid()])

    server = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=FakeHerdrClient(),
    )
    server.start()
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            s.connect(str(sock_path))
            s.sendall(b"this is not json{{{")
            raw = s.recv(65536)
            assert raw, "server must answer, never stay silent"
            res = json.loads(raw.decode("utf-8"))
            assert res["ok"] is False
        finally:
            s.close()
    finally:
        server.stop()


def test_relay_sender_failure_gets_error_response(tmp_path, short_sock_path):
    """L7/3.12: when the relay sender blows up, the caller gets an explicit
    error response, never an empty (silent) one."""
    sock_path = short_sock_path
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    _register_running_agent(reg, state_dir, pids=[os.getpid()])

    class BoomSender(FakeRelaySender):
        def send(self, from_identity, to_identity, content, urgency=5, extra=None):
            raise RuntimeError("angareion is away")

    server = RelayServer(
        socket_path=sock_path,
        registry=reg,
        sender=BoomSender(),
        herdr=FakeHerdrClient(),
    )
    server.start()
    try:
        with pytest.raises(RuntimeError, match="angareion is away"):
            send_relay_message(
                content="Report that cannot go out",
                to="pmo",
                socket_path=sock_path,
            )
    finally:
        server.stop()
