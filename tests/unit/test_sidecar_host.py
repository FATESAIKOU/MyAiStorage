"""Unit tests for agent host and herdr client wrapper."""

from __future__ import annotations

import signal

import pytest

from sidecar.herdr import (
    FakeHerdrClient,
    check_nesting_env,
    sanitize_herdr_env,
)
from sidecar.host import AgentHost
from sidecar.registry import Registry


@pytest.fixture(autouse=True)
def clean_herdr_context(monkeypatch):
    for var in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID"):
        monkeypatch.delenv(var, raising=False)


def test_anti_nesting_detection_and_sanitization(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_ENV", "worker")
    monkeypatch.setenv("OTHER_VAR", "value")

    assert set(check_nesting_env()) == {"HERDR_PANE_ID", "HERDR_ENV"}

    sanitized = sanitize_herdr_env()
    assert "HERDR_PANE_ID" not in sanitized
    assert "HERDR_ENV" not in sanitized
    assert sanitized["OTHER_VAR"] == "value"

    fake_herdr = FakeHerdrClient()
    reg = Registry(Path := pytest.importorskip("pathlib").Path("/tmp"))

    # When nested, AgentHost initialization must raise RuntimeError
    with pytest.raises(RuntimeError, match="Detected HERDR environment variables"):
        AgentHost(fake_herdr, reg, reject_nested=True)

    # When reject_nested=False, it permits initialization
    host = AgentHost(fake_herdr, reg, reject_nested=False)
    assert host is not None


def test_start_agent_launches_agora_continue_and_registers(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("HERDR_TAB_ID", raising=False)

    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOMESESSION",
        agent_kind="opencode",
        agent_name="impl1",
        fork=True,
        sync_period=300,
        cwd="/workspace/repo",
        harness_content="## Harness Instructions\nDo work.",
    )

    assert rec.status == "running"
    assert rec.agent_name == "impl1"
    assert rec.agora_session == "agora:01JHOMESESSION"
    assert rec.agent_kind == "opencode"
    assert rec.herdr_pane_id == "w1:p1"
    assert rec.herdr_tab_id == "w1:t1"

    # Check herdr calls
    call_names = [c[0] for c in fake_herdr.calls]
    assert "tab_create" in call_names
    assert "pane_run" in call_names
    assert "agent_rename" in call_names
    assert "agent_prompt" in call_names

    # Check agora continue command
    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][0] == "w1:p1"
    cmd = pane_run_call[1][1]
    assert cmd == "agora continue session agora:01JHOMESESSION --agent opencode --fork --sync-period 300"

    # Check harness was prompted
    prompt_call = next(c for c in fake_herdr.calls if c[0] == "agent_prompt")
    assert prompt_call[1][0] == "impl1"
    assert prompt_call[1][1] == "## Harness Instructions\nDo work."

    # Registry has it persisted
    persisted = reg.get(rec.agent_id)
    assert persisted is not None
    assert persisted.agent_name == "impl1"


def test_get_agent_status_opencode_esc_interrupt(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="tester",
    )

    # In fake herdr, agent_status is default "idle"
    fake_herdr.agents["tester"]["agent_status"] = "idle"
    # But screen contains "esc interrupt"
    fake_herdr.agent_outputs["tester"] = (
        "Thinking about problem...\n"
        "esc interrupt · 24.2K (2%)\n"
    )

    # opencode status must be working based on terminal screen
    status = host.get_agent_status("tester")
    assert status == "working"

    # When "esc interrupt" disappears from screen
    fake_herdr.agent_outputs["tester"] = "Ready for user prompt > "
    status_idle = host.get_agent_status("tester")
    assert status_idle == "idle"

    # When agent is absent
    fake_herdr.agents.pop("tester")
    assert host.get_agent_status("tester") == "exited"


def test_recover_agents_against_herdr_agent_list(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    # Start two agents
    rec1 = host.start_agent("team-1", "agora:01J1", "opencode", "agent-one")
    rec2 = host.start_agent("team-2", "agora:01J2", "claude", "agent-two")

    # Simulate agent-two exiting before restart
    fake_herdr.agents.pop("agent-two")

    # Recreate host / simulate restart
    recovered = host.recover_agents()
    assert len(recovered) == 1
    assert recovered[0].agent_id == rec1.agent_id

    # agent-two is marked exited in registry
    assert reg.get(rec2.agent_id).status == "exited"
    assert "not found in herdr agent list" in reg.get(rec2.agent_id).exit_reason


def test_three_stage_escalation_termination(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    signals_sent: list[tuple[int, int]] = []

    def mock_kill_pg(pgid: int, sig: int):
        signals_sent.append((pgid, sig))

    host = AgentHost(fake_herdr, reg, kill_pg_func=mock_kill_pg)
    rec = host.start_agent("team-worker", "agora:01J1", "opencode", "victim")

    # Case 1: Agent survives ctrl+c, survives SIGTERM, killed by SIGKILL
    alive_states = {"calls": 0}

    def check_alive(agent_name: str) -> bool:
        alive_states["calls"] += 1
        # Remains alive for stage 1 and stage 2 checks
        return True

    res = host.terminate_agent("victim", timeout_stage=0.01, check_alive_func=check_alive)
    assert res is True

    # Check key sent: ctrl+c
    assert any(k[1] == ("ctrl+c",) for k in fake_herdr.sent_keys)

    # Check signals sent: SIGTERM followed by SIGKILL
    pgid = fake_herdr.process_infos[rec.herdr_pane_id]["foreground_process_group_id"]
    assert (pgid, signal.SIGTERM) in signals_sent
    assert (pgid, signal.SIGKILL) in signals_sent

    # Pane closed
    assert rec.herdr_pane_id in fake_herdr.closed_panes

    # Registry status updated
    assert reg.get(rec.agent_id).status == "terminated"


def test_real_herdr_client_command_dispatch_and_errors(monkeypatch):
    import subprocess
    from sidecar.herdr import HerdrError, RealHerdrClient

    captured_cmds: list[list[str]] = []

    def mock_run(cmd, capture_output=True, text=True, env=None):
        captured_cmds.append(cmd)
        if "fail" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr='{"error": {"message": "failed deliberately"}}'
            )
        if "agent" in cmd and "read" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="screen output", stderr="")
        if "agent" in cmd and "list" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout='{"result": {"agents": [{"name": "live1"}]}}', stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout='{"result": {"ok": true}}', stderr="")

    monkeypatch.setattr(subprocess, "run", mock_run)

    client = RealHerdrClient(executable="herdr-bin", session="worker-sess")

    client.tab_create(cwd="/tmp", label="my-tab", env={"K": "V"}, focus=True)
    assert captured_cmds[-1] == [
        "herdr-bin", "--session", "worker-sess", "tab", "create",
        "--cwd", "/tmp", "--label", "my-tab", "--env", "K=V", "--focus",
    ]

    client.pane_run("w1:p1", "echo hello")
    assert captured_cmds[-1] == ["herdr-bin", "--session", "worker-sess", "pane", "run", "w1:p1", "echo hello"]

    client.agent_rename("w1:p1", "my-agent")
    assert captured_cmds[-1] == ["herdr-bin", "--session", "worker-sess", "agent", "rename", "w1:p1", "my-agent"]

    with pytest.raises(ValueError, match="does not match pattern"):
        client.agent_rename("w1:p1", "123-invalid")

    client.agent_prompt("my-agent", "hello", wait=True, until=["idle", "blocked"], timeout_ms=5000)
    assert captured_cmds[-1] == [
        "herdr-bin", "--session", "worker-sess", "agent", "prompt", "my-agent", "hello",
        "--wait", "--until", "idle", "--until", "blocked", "--timeout", "5000",
    ]

    text = client.agent_read("my-agent", lines=50)
    assert text == "screen output"

    agents = client.agent_list()
    assert agents == [{"name": "live1"}]

    with pytest.raises(HerdrError, match="failed deliberately"):
        client._run(["fail"])


def test_start_agent_with_worktree(tmp_path):
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JWORKTREE",
        agent_kind="claude",
        agent_name="claudie",
        worktree=True,
    )

    assert rec.status == "running"
    call_names = [c[0] for c in fake_herdr.calls]
    assert "worktree_create" in call_names
