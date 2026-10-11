"""Unit tests for agent host and herdr client wrapper."""

from __future__ import annotations

import signal

import pytest

from sidecar.herdr import (
    FakeHerdrClient,
    HerdrError,
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
        sync_period=5,
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
    assert cmd == "agora continue session agora:01JHOMESESSION --agent opencode --fork --sync-period 5"

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
    pgid = fake_herdr.process_infos[rec.herdr_pane_id]["foreground_process_group_id"]

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


def test_interrupt_and_prompt_behavior(tmp_path):
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    # 1. Opencode: sends esc twice and waits for indicator disappearance
    host.start_agent("team-oc", "agora:01JOC", "opencode", "oc-worker")
    fake_herdr.agent_outputs["oc-worker"] = "Writing code...\nesc interrupt"

    real_send_keys = fake_herdr.agent_send_keys
    esc_counts: dict[str, int] = {"oc-worker": 0, "claude-worker": 0}

    def spy_send_keys(target, *keys):
        res = real_send_keys(target, *keys)
        if "esc" in keys:
            esc_counts[target] = esc_counts.get(target, 0) + 1
            if target == "oc-worker" and esc_counts[target] >= 2:
                fake_herdr.agent_outputs[target] = "Ready for prompt > "
            elif target == "claude-worker" and esc_counts[target] >= 1:
                fake_herdr.agent_outputs[target] = "Interrupted · What should Claude do instead?"
        return res

    fake_herdr.agent_send_keys = spy_send_keys

    host.interrupt_and_prompt("oc-worker", "urgent input for oc")
    assert esc_counts["oc-worker"] == 2
    assert ("oc-worker", "urgent input for oc") in fake_herdr.prompts
    # Verify ctrl+c was never sent
    assert not any("ctrl+c" in k[1] for k in fake_herdr.sent_keys if k[0] == "oc-worker")

    # 2. Claude: sends esc once and waits for indicator disappearance
    host.start_agent("team-cl", "agora:01JCL", "claude", "claude-worker")
    fake_herdr.agent_outputs["claude-worker"] = "Generating reply...\nesc to interrupt"

    host.interrupt_and_prompt("claude-worker", "urgent input for claude")
    assert esc_counts["claude-worker"] == 1
    assert ("claude-worker", "urgent input for claude") in fake_herdr.prompts
    assert not any("ctrl+c" in k[1] for k in fake_herdr.sent_keys if k[0] == "claude-worker")

    # 3. Idle agent: does not send esc at all
    host.start_agent("team-idle", "agora:01JIDLE", "opencode", "idle-worker")
    fake_herdr.agent_outputs["idle-worker"] = "Ready > "
    fake_herdr.agents["idle-worker"]["agent_status"] = "idle"

    host.interrupt_and_prompt("idle-worker", "normal message")
    assert esc_counts.get("idle-worker", 0) == 0
    assert ("idle-worker", "normal message") in fake_herdr.prompts


def test_termination_waits_full_timeout_stage_without_clamping(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    sleep_calls: list[float] = []

    def mock_sleeper(duration: float) -> None:
        sleep_calls.append(duration)

    host = AgentHost(fake_herdr, reg, sleeper=mock_sleeper)
    host.start_agent("team-term", "agora:01JTERM", "opencode", "victim-agent")

    # Keep alive through stage 1 and stage 2 to verify both sleeps
    res = host.terminate_agent("victim-agent", timeout_stage=5.0, check_alive_func=lambda _: True)
    assert res is True
    # Must wait full 5.0 seconds per stage, not clamped to 0.05
    assert sleep_calls == [5.0, 5.0]

    # Test custom timeout_stage=3.5
    sleep_calls.clear()
    host.start_agent("team-term2", "agora:01JTERM2", "opencode", "victim-agent-2")
    res2 = host.terminate_agent("victim-agent-2", timeout_stage=3.5, check_alive_func=lambda _: True)
    assert res2 is True
    assert sleep_calls == [3.5, 3.5]


def test_is_working_only_checks_tail_lines_ignoring_historical_dialogue(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    # 1. Opencode with historical text containing "esc interrupt" in line 2
    host.start_agent("team-oc", "agora:01JOC", "opencode", "oc-worker")
    fake_herdr.agents["oc-worker"]["agent_status"] = "idle"

    historical_screen = (
        "User: Explain what 'esc interrupt' means in this tool.\n"
        "Assistant: In opencode, 'esc interrupt' is a keybinding indicator.\n"
        "User: Got it, thank you.\n"
        "Assistant: You're welcome!\n"
        "Ready for user prompt > \n"
    )
    fake_herdr.agent_outputs["oc-worker"] = historical_screen

    # Historical dialogue must not cause False Positive
    assert host.is_working("oc-worker") is False
    assert host.get_agent_status("oc-worker") == "idle"

    # When "esc interrupt" is actually in the bottom status line (tail)
    active_screen = historical_screen + "Thinking and writing response...\nesc interrupt · 5.2k\n"
    fake_herdr.agent_outputs["oc-worker"] = active_screen
    assert host.is_working("oc-worker") is True
    assert host.get_agent_status("oc-worker") == "working"

    # 2. Claude with historical text containing "esc to interrupt"
    host.start_agent("team-cl", "agora:01JCL", "claude", "cl-worker")
    fake_herdr.agents["cl-worker"]["agent_status"] = "idle"
    cl_historical = (
        "User: How to interrupt Claude?\n"
        "Assistant: Press esc to interrupt.\n"
        "User: Thanks for the explanation.\n"
        "Assistant: Glad to help!\n"
        "Claude is waiting for prompt > \n"
    )
    fake_herdr.agent_outputs["cl-worker"] = cl_historical
    assert host.is_working("cl-worker") is False
    assert host.get_agent_status("cl-worker") == "idle"

    cl_active = cl_historical + "Generating reply...\nesc to interrupt\n"
    fake_herdr.agent_outputs["cl-worker"] = cl_active
    assert host.is_working("cl-worker") is True
    assert host.get_agent_status("cl-worker") == "working"


def test_interrupt_reports_failure_on_exception_or_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    # Use fast clock so timeout expires immediately
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)
    host.start_agent("team-int", "agora:01JINT", "opencode", "broken-worker")
    fake_herdr.agent_outputs["broken-worker"] = "Writing code...\nesc interrupt"

    # Simulate agent that is working, but during interrupt wait loop read fails
    read_calls = {"count": 0}

    def flakey_read(target, **kwargs):
        read_calls["count"] += 1
        if read_calls["count"] == 1:
            return "Writing code...\nesc interrupt"
        raise HerdrError(["agent", "read"], 1, "Pane communication error")

    fake_herdr.agent_read = flakey_read

    # interrupt must return False when read fails during wait loop (not return True)
    res = host.interrupt("broken-worker", timeout=0.01)
    assert res is False

    # Also test send_prompt when indicator never clears (timeouts)
    read_calls["count"] = 0
    fake_herdr.agent_read = lambda target, **kw: "Writing code...\nesc interrupt"
    with pytest.raises(RuntimeError, match="Failed to interrupt agent 'broken-worker'"):
        host.send_prompt("broken-worker", "failed delivery message", interrupt_if_working=True, timeout=0.01)

    assert ("broken-worker", "failed delivery message") not in fake_herdr.prompts


def test_recover_agents_verifies_foreground_process_group(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec_done = host.start_agent("team-done", "agora:01JDONE", "opencode", "agent-done")
    rec_alive = host.start_agent("team-alive", "agora:01JALIVE", "claude", "agent-alive")

    # In herdr agent_list, both names are present
    assert "agent-done" in fake_herdr.agents
    assert "agent-alive" in fake_herdr.agents

    # But agent-done pane has no foreground process (pgid=0 or None)
    fake_herdr.process_infos[rec_done.herdr_pane_id]["foreground_process_group_id"] = 0
    fake_herdr.process_infos[rec_alive.herdr_pane_id]["foreground_process_group_id"] = 5555

    recovered = host.recover_agents()
    # Only agent-alive is recovered
    assert len(recovered) == 1
    assert recovered[0].agent_id == rec_alive.agent_id

    # agent-done is marked exited due to lack of foreground process
    done_record = reg.get(rec_done.agent_id)
    assert done_record.status == "exited"
    assert done_record.exit_reason == "no foreground process in pane on restart"


def test_start_agent_default_sync_period_is_5_minutes(tmp_path, monkeypatch):
    """H1/D10: default sync_period is 5 (minutes), passed through verbatim."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="default-sync",
    )

    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][1] == (
        "agora continue session agora:01JHOME --agent opencode --fork --sync-period 5"
    )


def test_start_agent_env_none_does_not_inherit_process_env(tmp_path, monkeypatch):
    """M2/D6: env=None gives an empty extra environment, never the sidecar
    process environment (which may hold A2A_TOKEN)."""
    monkeypatch.setenv("A2A_TOKEN", "super-secret-token")
    monkeypatch.setenv("SOME_OTHER_SECRET", "shh")

    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    host.start_agent(
        identity="team-worker",
        agora_session="agora:01JENV",
        agent_kind="opencode",
        agent_name="no-env-worker",
    )

    tab_create_call = next(c for c in fake_herdr.calls if c[0] == "tab_create")
    passed_env = tab_create_call[1][2]
    assert passed_env == {}
    assert "A2A_TOKEN" not in passed_env


def test_start_agent_waits_for_prompt_before_harness(tmp_path, monkeypatch):
    """M3/D4: harness goes in only after pane wait-output sees the prompt."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="waited-worker",
        harness_content="## Harness\n",
    )

    assert rec.status == "running"
    call_names = [c[0] for c in fake_herdr.calls]
    assert call_names.index("pane_wait_output") < call_names.index("agent_prompt")
    wait_call = next(c for c in fake_herdr.calls if c[0] == "pane_wait_output")
    assert wait_call[1][0] == rec.herdr_pane_id


def test_start_agent_prompt_wait_timeout_registers_and_raises(tmp_path, monkeypatch):
    """M3: when the prompt never appears, fail loudly and leave evidence in
    the registry instead of sending the harness into the void."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    def boom_wait(*args, **kwargs):
        raise HerdrError(["pane", "wait-output"], 1, "timeout waiting for output")

    monkeypatch.setattr(fake_herdr, "pane_wait_output", boom_wait)

    with pytest.raises(RuntimeError, match="showed no output"):
        host.start_agent(
            identity="team-worker",
            agora_session="agora:01JHOME",
            agent_kind="opencode",
            agent_name="slow-worker",
            harness_content="## Harness\n",
        )

    # Harness was never sent, but the attempt is recorded.
    assert ("slow-worker", "## Harness\n") not in fake_herdr.prompts
    rec = reg.get_by_name("slow-worker")
    assert rec is not None
    assert rec.status == "starting"
    assert "readiness wait failed" in (rec.exit_reason or "")


def test_recover_agents_herdr_down_leaves_registry_untouched(tmp_path, monkeypatch, caplog):
    """M4: when herdr is unreachable, recover changes nothing in the registry."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent("team-1", "agora:01J1", "opencode", "agent-one")

    def boom_list():
        raise HerdrError(["agent", "list"], 1, "herdr server unreachable")

    monkeypatch.setattr(fake_herdr, "agent_list", boom_list)

    with caplog.at_level("WARNING", logger="sidecar.host"):
        recovered = host.recover_agents()

    assert recovered == []
    assert reg.get(rec.agent_id).status == "running"
    assert "leaving registry untouched" in caplog.text


def test_recover_agents_process_info_failure_keeps_agent_status(tmp_path, monkeypatch, caplog):
    """M4: a single pane_process_info failure keeps that agent's status for
    the next recover pass instead of marking it exited."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec_flaky = host.start_agent("team-1", "agora:01J1", "opencode", "agent-flaky")
    rec_ok = host.start_agent("team-2", "agora:01J2", "claude", "agent-ok")

    real_info = fake_herdr.pane_process_info

    def flaky_info(pane_id):
        if pane_id == rec_flaky.herdr_pane_id:
            raise HerdrError(["pane", "process-info"], 1, "transient failure")
        return real_info(pane_id)

    monkeypatch.setattr(fake_herdr, "pane_process_info", flaky_info)

    with caplog.at_level("WARNING", logger="sidecar.host"):
        recovered = host.recover_agents()

    assert [r.agent_id for r in recovered] == [rec_ok.agent_id]
    assert reg.get(rec_flaky.agent_id).status == "running"
    assert "leaving status unchanged" in caplog.text


def test_wait_for_ready_requires_matching_kind_and_idle(tmp_path, monkeypatch):
    """3.12/D10: readiness is herdr recognizing the expected kind plus idle."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)

    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="ready-worker",
    )
    assert rec.status == "running"
    get_calls = [c for c in fake_herdr.calls if c[0] == "agent_get"]
    assert any(c[1][0] == "ready-worker" for c in get_calls)


def test_wait_for_ready_rejects_wrong_kind(tmp_path, monkeypatch):
    """A pane detected as another agent kind never counts as ready."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)

    fake_herdr.agent_kinds["picky-worker"] = "claude"
    fake_herdr.agents["picky-worker"] = {
        "name": "picky-worker",
        "pane_id": "w1:p1",
        "agent_status": "idle",
        "cwd": "/tmp",
    }
    with pytest.raises(RuntimeError, match="not ready in time.*'claude'.*'idle'"):
        host.wait_for_ready(
            "picky-worker", "w1:p1", "opencode",
            timeout_ms=50, poll_interval=0.01,
        )


def test_wait_for_ready_waits_for_idle_transition(tmp_path, monkeypatch):
    """An agent that settles from working to idle becomes ready."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)

    fake_herdr.agent_kinds["settling-worker"] = "opencode"
    fake_herdr.agents["settling-worker"] = {
        "name": "settling-worker",
        "pane_id": "w1:p9",
        "agent_status": "working",
        "cwd": "/tmp",
    }
    reads = {"count": 0}
    real_get = fake_herdr.agent_get

    def flipping_get(target):
        reads["count"] += 1
        if reads["count"] >= 3:
            fake_herdr.agents["settling-worker"]["agent_status"] = "idle"
        return real_get(target)

    monkeypatch.setattr(fake_herdr, "agent_get", flipping_get)
    host.wait_for_ready(
        "settling-worker", "w1:p9", "opencode",
        timeout_ms=5000, poll_interval=0.01,
    )
    assert reads["count"] >= 3


def test_wait_for_ready_timeout_when_get_keeps_failing(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)

    def boom_get(target):
        raise HerdrError(["agent", "get"], 1, "gone")

    monkeypatch.setattr(fake_herdr, "agent_get", boom_get)
    with pytest.raises(RuntimeError, match="not ready in time"):
        host.wait_for_ready(
            "ghost-worker", "w1:p1", "opencode",
            timeout_ms=50, poll_interval=0.01,
        )


def test_poll_fork_session_finds_late_announcement(tmp_path, monkeypatch):
    """3.12/D10: the fork line arrives seconds after the first output; the
    capture polls until it appears (two-stage fake output)."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)

    stages = [
        "[agora] 工作目錄 /workspace/team\n",
        "[agora] 工作目錄 /workspace/team\n",
        "[agora] fork 已存成 agora:01JLATE123\nopencode TUI ready > ",
    ]
    calls = {"count": 0}

    def staged_read(target, **kwargs):
        calls["count"] += 1
        idx = min(calls["count"] - 1, len(stages) - 1)
        return stages[idx]

    monkeypatch.setattr(fake_herdr, "agent_read", staged_read)

    found = host.poll_fork_session("late-worker", timeout_ms=5000, poll_interval=0.01)
    assert found == "agora:01JLATE123"
    assert calls["count"] >= 3


def test_poll_fork_session_timeout_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, sleeper=lambda _: None)

    fake_herdr.agent_outputs["quiet-worker"] = "[agora] 工作目錄 /x\n"
    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="quiet-worker",
        fork=True,
        fork_timeout_ms=50,
        fork_poll_interval=0.01,
    )
    assert rec.status == "running"
    assert rec.extra is None
    assert host.export_agent_session("quiet-worker") == "agora:01JHOME"


def test_export_returns_forked_session_not_home(tmp_path, monkeypatch):
    """L3: with --fork the dialogue lives in the forked Session; export
    returns it, not the team's home Session."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    # agora continue --fork announces the new session id on startup.
    fake_herdr.agent_outputs["fork-worker"] = (
        "[agora] fork 已存成 agora:01JFORK123\nopencode TUI ready > "
    )
    rec = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="fork-worker",
        fork=True,
    )

    assert host.export_agent_session(rec.agent_name) == "agora:01JFORK123"
    # Lookup by id works too.
    assert host.export_agent_session(rec.agent_id) == "agora:01JFORK123"

    # No fork announcement on the pane: home is the fallback.
    rec2 = host.start_agent(
        identity="team-worker",
        agora_session="agora:01JHOME",
        agent_kind="opencode",
        agent_name="plain-worker",
        fork=True,
    )
    assert host.export_agent_session(rec2.agent_name) == "agora:01JHOME"
    assert host.export_agent_session("no-such-agent") is None


def test_terminate_agent_sigkill_verifies_process_gone(tmp_path, monkeypatch):
    """L4: after SIGKILL the process must really be gone; a survivor is not
    recorded as terminated and the call reports failure."""
    import signal as _signal

    monkeypatch.delenv("HERDR_PANE_ID", raising=False)

    class UnkillableHerdr(FakeHerdrClient):
        def pane_close(self, pane_id: str):
            self.calls.append(("pane_close", (pane_id,), {}))
            self.closed_panes.add(pane_id)
            # Pane UI goes away but the process survives (D-state, etc.).

    fake_herdr = UnkillableHerdr()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)

    signals: list[int] = []
    host = AgentHost(fake_herdr, reg, kill_pg_func=lambda pgid, sig: signals.append(sig))

    rec = host.start_agent("team-worker", "agora:01J1", "opencode", "survivor")

    res = host.terminate_agent("survivor", timeout_stage=0.01, check_alive_func=lambda _: True)
    assert res is False
    assert _signal.SIGKILL in signals
    # Honestly still running, not terminated.
    assert reg.get(rec.agent_id).status == "running"


def test_terminate_agent_sigkill_success_when_process_gone(tmp_path, monkeypatch):
    """L4: the normal path (process really gone) still reports success."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    fake_herdr = FakeHerdrClient()
    state_dir = tmp_path / "state"
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg)

    rec = host.start_agent("team-worker", "agora:01J1", "opencode", "mortal")
    res = host.terminate_agent("mortal", timeout_stage=0.01, check_alive_func=lambda _: True)
    assert res is True
    assert reg.get(rec.agent_id).status == "terminated"

