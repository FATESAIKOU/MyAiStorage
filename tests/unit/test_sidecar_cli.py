"""The console script: config, --version, `sidecar run` (change sidecar-v0, D7/D8)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import socket
import threading
import time
import urllib.request

import pytest

from sidecar import cli
from sidecar.config import Config, load_config
from sidecar.protocols import BadRequestError, ConfigError
from sidecar.wiring import build_core


# --- --version (design D8) ----------------------------------------------------


def test_version_prints_the_installed_git_sha(monkeypatch):
    class FakeDist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"vcs_info": {"commit_id": "abc1234"}})

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: FakeDist())
    assert cli.version_string() == "sidecar abc1234"


def test_version_falls_back_to_the_package_version(monkeypatch):
    def not_installed(name):
        raise Exception("not installed")

    monkeypatch.setattr("importlib.metadata.distribution", not_installed)
    out = cli.version_string()
    assert out.startswith("sidecar ")
    assert "install sha unknown" in out


def test_main_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "sidecar " in capsys.readouterr().out


def test_main_without_a_command_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: sidecar" in capsys.readouterr().err


# --- config (design D7) -------------------------------------------------------


def test_load_config_defaults_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDECAR_CONFIG", str(tmp_path / "missing.json"))
    config = load_config()
    assert config.port == 8471  # D7's default, read from the file when present
    assert config.gateway_connect_file.name == "gateway-connect.json"
    assert config.sync_period == 5.0
    assert config.resume_prompt == "額度恢復了，請接著剛才的工作"
    assert config.retry_interval == 300.0
    assert config.poke_queue_timeout == 30.0
    assert config.close_timeout == 30.0
    assert config.quota_reasons == ("free_tier_limit",)
    assert config.subscription_source is None
    assert config.agents == {}


def test_load_config_reads_the_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "port": 9000,
                "subscription_source": {"kind": "llmgateway"},
                "gateway_connect_file": "/x/gateway-connect.json",
                "agents": {"claude-code": {"command": "/x/claude", "extra_args": ["--foo"]}},
                "sync_period": 2,
                "resume_prompt": "",
                "retry_interval": 60,
                "poke_queue_timeout": 5,
                "close_timeout": 10,
                "quota_reasons": ["free_tier_limit", "another_limit"],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.path == path
    assert config.port == 9000
    assert config.subscription_source == {"kind": "llmgateway"}
    assert config.gateway_connect_file == _p.Path("/x/gateway-connect.json")
    assert config.agents["claude-code"].command == "/x/claude"
    assert config.agents["claude-code"].extra_args == ["--foo"]
    assert config.sync_period == 2.0
    assert config.resume_prompt == ""
    assert config.retry_interval == 60.0
    assert config.poke_queue_timeout == 5.0
    assert config.close_timeout == 10.0
    assert config.quota_reasons == ("free_tier_limit", "another_limit")


def test_load_config_rejects_bad_files(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": "8471"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text(json.dumps({"retry_interval": -1}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# --- wiring -------------------------------------------------------------------


def test_build_core_wires_the_file_source_and_hides_the_agent_seams(tmp_path):
    subs_path = tmp_path / "subs.json"
    subs_path.write_text(
        json.dumps(
            [
                {
                    "schema": 1,
                    "id": "claude-personal",
                    "agents": ["claude-code"],
                    "priority": 10,
                    "key": "file-secret",
                    "cooldown": {},
                    "available": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    core = build_core(Config(subscription_source={"kind": "file", "path": str(subs_path)}))
    # The drivers/session store of tasks 3.1-3.3 are not part of this change yet,
    # so the daemon runs but cannot open an agent.
    with pytest.raises(BadRequestError):
        core.open("t1", "claude-code", str(workdir), {"mode": "new"})


def test_build_core_wires_llmgateway_without_the_extra_installed(tmp_path):
    config = Config(subscription_source={"kind": "llmgateway"},
                    gateway_connect_file=tmp_path / "connect.json")
    core = build_core(config)
    # The source exists; without the lgw extra every operation is Unreachable,
    # which open turns into a 503 (covered by the sources tests).
    assert core is not None


def test_main_run_with_llmgateway_still_serves_status(tmp_path):
    port = _free_port()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "port": port,
                "subscription_source": {"kind": "llmgateway"},
                "gateway_connect_file": str(tmp_path / "connect.json"),
            }
        ),
        encoding="utf-8",
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=cli.run,
        args=(load_config(path),),
        kwargs={"stop_event": stop},
        daemon=True,
    )
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_for_agents(base)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


# --- `sidecar run` ------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_agents(base: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(base + "/agents", timeout=1) as response:
                assert response.status == 200
                assert json.loads(response.read()) == {"agents": []}
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_run_serves_agents_and_stops_cleanly():
    port = _free_port()
    stop = threading.Event()
    thread = threading.Thread(
        target=cli.run, args=(Config(port=port),), kwargs={"stop_event": stop}, daemon=True
    )
    thread.start()
    try:
        _wait_for_agents(f"http://127.0.0.1:{port}")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
