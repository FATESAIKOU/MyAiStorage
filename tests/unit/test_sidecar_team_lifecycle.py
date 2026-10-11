"""Unit tests for team lifecycle (Task 3.4, Design D4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sidecar.config import SidecarConfig
from sidecar.herdr import FakeHerdrClient
from sidecar.host import AgentHost
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
                "sync_period": 600,
                "cwd": "/workspace/team",
            }
        },
    )

    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)

    rec = host.start_team("worker-team")

    assert rec.status == "running"
    assert rec.identity == "worker-team"
    assert rec.agora_session == "agora:01JTEAMHOME"
    assert rec.agent_kind == "opencode"

    # Verify agora continue command generated
    pane_run_call = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert pane_run_call[1][1] == (
        "agora continue session agora:01JTEAMHOME --agent opencode --fork --sync-period 600"
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
    rec = host.start_team("worker-team")

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
