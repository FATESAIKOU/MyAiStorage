"""Unit tests for the resident sidecar daemon loop (D10, D-run, 3.12)."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from angareion.backend import BackendError, FakeBackend, RateLimitError
from angareion.messages import Message
from sidecar.cli import build_parser, main
from sidecar.config import SidecarConfig
from sidecar.herdr import FakeHerdrClient
from sidecar.host import AgentHost
from sidecar.queue import MessageQueue
from sidecar.receiver import Receiver
from sidecar.registry import Registry
from sidecar.relay import FakeRelaySender, RelayServer
from sidecar.run import MAX_BACKOFF_SECONDS, run_loop


@pytest.fixture(autouse=True)
def clean_herdr_context(monkeypatch):
    for var in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def short_sock_path():
    import tempfile, shutil
    td = tempfile.mkdtemp(prefix="scrun_", dir="/tmp")
    p = Path(td) / "s.sock"
    yield p
    if p.exists():
        p.unlink(missing_ok=True)
    shutil.rmtree(td, ignore_errors=True)


def _make_stack(tmp_path):
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
    return backend, cfg, fake_herdr, reg, queue, host, receiver


def test_run_loop_delivers_and_acks_in_one_cycle(tmp_path, short_sock_path):
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)

    rec = host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")
    fake_herdr.agent_outputs["worker-1"] = "Ready > "

    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=9,
        content="Urgent via run loop",
    )
    backend.post_message("general", msg)

    server = RelayServer(
        socket_path=short_sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=fake_herdr,
    )

    assert run_loop(receiver=receiver, registry=reg, relay_server=server, max_cycles=1) == 0

    assert ("worker-1", "Urgent via run loop") in fake_herdr.prompts
    assert queue.count("worker-team") == 0
    all_msgs = backend.list_messages().messages
    assert any(m.ack == msg.id for m in all_msgs)
    assert not short_sock_path.exists()


def test_run_loop_recovers_and_refreshes_heartbeat(tmp_path, short_sock_path):
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)

    rec = host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")
    before = reg.get(rec.agent_id).updated_at

    server = RelayServer(
        socket_path=short_sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=fake_herdr,
    )

    assert run_loop(
        receiver=receiver,
        registry=reg,
        relay_server=server,
        recover_interval=0,
        max_cycles=1,
    ) == 0

    after = reg.get(rec.agent_id).updated_at
    assert after >= before
    assert reg.get(rec.agent_id).status == "running"


def test_run_loop_survives_backend_errors(tmp_path, short_sock_path, caplog):
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)
    backend.next_error = BackendError("GitHub 500 flakiness")

    sleeps: list[float] = []
    server = RelayServer(
        socket_path=short_sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=fake_herdr,
    )

    with caplog.at_level("WARNING", logger="sidecar.run"):
        rc = run_loop(
            receiver=receiver,
            registry=reg,
            relay_server=server,
            max_cycles=2,
            sleeper=sleeps.append,
        )

    assert rc == 0
    assert "retrying next cycle" in caplog.text


def test_run_loop_backs_off_on_rate_limit(tmp_path, short_sock_path, caplog):
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)
    backend.next_error = RateLimitError("secondary rate limit", retry_after=7)

    sleeps: list[float] = []
    server = RelayServer(
        socket_path=short_sock_path,
        registry=reg,
        sender=FakeRelaySender(),
        herdr=fake_herdr,
    )

    with caplog.at_level("WARNING", logger="sidecar.run"):
        rc = run_loop(
            receiver=receiver,
            registry=reg,
            relay_server=server,
            max_cycles=1,
            sleeper=sleeps.append,
        )

    assert rc == 0
    assert sleeps == [7.0]
    assert "backing off" in caplog.text


def test_cli_run_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["run", "--once"])
    assert args.subcommand == "run"
    assert args.once is True
    assert args.poll_interval == 30.0
    assert args.recover_interval == 300.0
    assert args.channel == "general"


def test_cli_run_once_with_fake_backend(tmp_path, monkeypatch, short_sock_path):
    """`sidecar run --once` wires poll + relay + recover with fakes and exits 0."""
    monkeypatch.setenv("A2A_BACKEND", "fake")
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "state_dir": str(tmp_path / "state"),
        "channel_repo": "owner-org/channel-repo",
        "identities": ["worker-team"],
        "teams": {},
    }))

    code = main([
        "--config", str(cfg_file),
        "run", "--once",
        "--sock", str(short_sock_path),
    ])
    assert code == 0


def test_cli_run_refuses_without_channel_repo(tmp_path, monkeypatch, capsys, short_sock_path):
    monkeypatch.setenv("A2A_BACKEND", "fake")
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"state_dir": str(tmp_path / "state")}))

    code = main(["--config", str(cfg_file), "run", "--once", "--sock", str(short_sock_path)])
    assert code == 1
    _, err = capsys.readouterr()
    assert "channel_repo" in err


def _make_team_stack(tmp_path, **team_overrides):
    """Stack whose worker-team has a home session (orchestration tests)."""
    backend = FakeBackend()
    backend.create_channel("general")
    state_dir = tmp_path / "state"
    team = {"agent": "opencode", "home_session": "agora:01JTEAMHOME"}
    team.update(team_overrides)
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["worker-team"],
        teams={"worker-team": team},
    )
    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)
    return backend, cfg, fake_herdr, reg, queue, host, receiver


def _post_team_mail(backend, urgency=3, content="Routine work item"):
    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "worker-team"},
        channel="general",
        urgency=urgency,
        content=content,
    )
    backend.post_message("general", msg)
    return msg


def test_run_starts_team_for_queued_mail_without_agent(tmp_path, short_sock_path):
    """3.12/D10 orchestration: mail for an idle identity starts one team,
    whose hand-off drains the queue (trigger id recorded in headers)."""
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_team_stack(tmp_path)
    msg = _post_team_mail(backend)

    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    assert run_loop(
        receiver=receiver, registry=reg, relay_server=server,
        max_cycles=1, team_fork_timeout_ms=0,
    ) == 0

    tab_calls = [c for c in fake_herdr.calls if c[0] == "tab_create"]
    assert len(tab_calls) == 1
    pane_run = next(c for c in fake_herdr.calls if c[0] == "pane_run")
    assert f"--header sidecar.message={msg.id}" in pane_run[1][1]
    assert (fake_herdr.prompts[-1][1]) == "Routine work item"
    assert queue.count("worker-team") == 0
    assert any(m.ack == msg.id for m in backend.list_messages().messages)


def test_run_starts_at_most_one_agent_per_identity(tmp_path, short_sock_path, monkeypatch):
    """A second cycle with the agent running starts nothing new, even when
    fresh mail arrives mid-run and delivery keeps failing (queue stays)."""
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_team_stack(tmp_path)
    _post_team_mail(backend)

    def boom_send_prompt(agent_name, text, **kwargs):
        raise RuntimeError("pane gone")

    monkeypatch.setattr(host, "send_prompt", boom_send_prompt)

    real_poll = receiver.poll_once
    polls = {"count": 0}

    def poll_then_new_mail():
        out = real_poll()
        polls["count"] += 1
        if polls["count"] == 1:
            _post_team_mail(backend, content="arrived later")
        return out

    receiver.poll_once = poll_then_new_mail

    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    assert run_loop(
        receiver=receiver, registry=reg, relay_server=server,
        max_cycles=2, team_fork_timeout_ms=0, poll_interval=0,
    ) == 0

    assert len([c for c in fake_herdr.calls if c[0] == "tab_create"]) == 1
    assert len(reg.list_agents(status="running")) == 1
    assert queue.count("worker-team") == 2


def test_run_survives_unstartable_team(tmp_path, short_sock_path, caplog):
    """An identity with queued mail but no home session is logged, and the
    daemon keeps running instead of dying."""
    backend = FakeBackend()
    backend.create_channel("general")
    state_dir = tmp_path / "state"
    cfg = SidecarConfig(
        config_path=tmp_path / "config.json",
        state_dir=state_dir,
        identities=["homeless-team"],
        teams={"homeless-team": {"agent": "opencode"}},
    )
    fake_herdr = FakeHerdrClient()
    reg = Registry(state_dir)
    queue = MessageQueue(state_dir)
    host = AgentHost(fake_herdr, reg, config=cfg)
    receiver = Receiver(backend, cfg, reg, queue, host)

    msg = Message(
        from_={"repo": "pmo", "role": "PM"},
        to={"repo": "worker", "role": "homeless-team"},
        channel="general",
        urgency=3,
        content="Nobody can start me",
    )
    backend.post_message("general", msg)

    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    with caplog.at_level("WARNING", logger="sidecar.run"):
        rc = run_loop(
            receiver=receiver, registry=reg, relay_server=server,
            max_cycles=2, team_fork_timeout_ms=0, poll_interval=0,
        )
    assert rc == 0
    assert "Could not start team 'homeless-team'" in caplog.text
    assert queue.count("homeless-team") == 1


def test_run_skips_identity_with_starting_record(tmp_path, short_sock_path):
    """A previous failed start (record stuck at starting) blocks respawn:
    at most one live attempt per identity, no pane leak."""
    from sidecar.registry import AgentRecord, now_iso

    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_team_stack(tmp_path)
    _post_team_mail(backend)
    reg.register(AgentRecord(
        agent_id="ag_stuck", identity="worker-team", agora_session="agora:01JX",
        agent_kind="opencode", herdr_tab_id="w1:t9", herdr_pane_id="w1:p9",
        agent_name="stuck-worker", started_at=now_iso(), status="starting",
        log_path=str(tmp_path / "log"), updated_at=now_iso(),
        exit_reason="readiness wait failed: boom",
    ))

    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    assert run_loop(
        receiver=receiver, registry=reg, relay_server=server,
        max_cycles=1, team_fork_timeout_ms=0,
    ) == 0
    assert not [c for c in fake_herdr.calls if c[0] == "tab_create"]


def test_run_sigterm_stops_cleanly_and_unlinks_socket(tmp_path, short_sock_path):
    """3.12: SIGTERM requests a clean stop; the bound socket file is removed."""
    import time as _time

    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)
    old_handler = signal.getsignal(signal.SIGTERM)
    killed = {"count": 0}

    def killer_sleep(seconds):
        if killed["count"] == 0:
            killed["count"] += 1
            os.kill(os.getpid(), signal.SIGTERM)
        _time.sleep(0.01)

    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    try:
        rc = run_loop(
            receiver=receiver, registry=reg, relay_server=server,
            max_cycles=None, sleeper=killer_sleep,
        )
    finally:
        signal.signal(signal.SIGTERM, old_handler)
    assert rc == 0
    assert not short_sock_path.exists()
    assert signal.getsignal(signal.SIGTERM) == old_handler


def test_run_heartbeat_failure_does_not_kill_daemon(tmp_path, short_sock_path, caplog, monkeypatch):
    """3.12: a failing heartbeat write is logged; the loop survives."""
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)
    host.start_agent("worker-team", "agora:01JTEST", "opencode", "worker-1")

    def boom_touch(agent_id):
        raise OSError("disk read-only")

    monkeypatch.setattr(reg, "touch", boom_touch)
    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    with caplog.at_level("WARNING", logger="sidecar.run"):
        rc = run_loop(
            receiver=receiver, registry=reg, relay_server=server,
            recover_interval=0, max_cycles=1,
        )
    assert rc == 0
    assert "Heartbeat write failed" in caplog.text


def test_run_backoff_capped_at_300_seconds(tmp_path, short_sock_path):
    """3.12: a huge retry_after stalls one cycle at most 300s, not longer."""
    backend, cfg, fake_herdr, reg, queue, host, receiver = _make_stack(tmp_path)
    backend.next_error = RateLimitError("secondary rate limit", retry_after=3600)

    sleeps: list[float] = []
    server = RelayServer(
        socket_path=short_sock_path, registry=reg,
        sender=FakeRelaySender(), herdr=fake_herdr,
    )
    rc = run_loop(
        receiver=receiver, registry=reg, relay_server=server,
        max_cycles=1, sleeper=sleeps.append,
    )
    assert rc == 0
    assert sleeps == [MAX_BACKOFF_SECONDS]
    assert MAX_BACKOFF_SECONDS == 300.0


def test_cli_run_refuses_without_channel_repo(tmp_path, monkeypatch, capsys, short_sock_path):
    monkeypatch.setenv("A2A_BACKEND", "fake")
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"state_dir": str(tmp_path / "state")}))

    code = main(["--config", str(cfg_file), "run", "--once", "--sock", str(short_sock_path)])
    assert code == 1
    _, err = capsys.readouterr()
    assert "channel_repo" in err
