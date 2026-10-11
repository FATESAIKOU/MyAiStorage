"""Unit tests for the resident sidecar daemon loop (D10, D-run)."""

from __future__ import annotations

import json
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
from sidecar.run import run_loop


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
