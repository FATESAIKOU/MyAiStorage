"""Unit tests for sidecar check/version (3.7), herdr session (3.8), and
agora fork listing (3.9). All backends are fakes: no network, no real herdr."""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from angareion.backend import AuthenticationError, BackendError, FakeBackend, RateLimitError
from sidecar.agora import FakeAgoraClient
from sidecar.check import run_checks
from sidecar.cli import _report_check, build_parser, cmd_status, main
from sidecar.config import SidecarConfig
from sidecar.herdr import FakeHerdrClient, HerdrError, RealHerdrClient
from sidecar.queue import MessageQueue
from sidecar.registry import AgentRecord, Registry


@pytest.fixture(autouse=True)
def clean_herdr_context(monkeypatch):
    for var in ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID"):
        monkeypatch.delenv(var, raising=False)


def _write_token(path, content="test-token-xyz", mode=0o600):
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _healthy_config(tmp_path, token_mode=0o600, **overrides):
    if "a2a_token_path" not in overrides:
        overrides["a2a_token_path"] = _write_token(tmp_path / "token", mode=token_mode)
    kwargs = {
        "config_path": tmp_path / "config.json",
        "state_dir": tmp_path / "state",
        "identities": ["worker-team"],
        "channel_repo": "owner-org/channel-repo",
        "herdr_session": "worker",
    }
    kwargs.update(overrides)
    return SidecarConfig(**kwargs)


def _ok_backend_factory(backend):
    def factory(channel_repo, token):
        assert channel_repo == "owner-org/channel-repo"
        assert token == "test-token-xyz"
        return backend
    return factory


def _fake_backend_with_channels(*channels):
    backend = FakeBackend()
    for channel in channels:
        backend.create_channel(channel)
    return backend


# --- 3.7 --version ---


def test_version_prints_installed_sha(monkeypatch, capsys):
    from sidecar import cli as cli_module

    class FakeDist:
        version = "0.1.0"

        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"vcs_info": {"commit_id": "deadbeef" * 5}})

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: FakeDist())
    assert cli_module.sidecar_version_string() == f"sidecar {'deadbeef' * 5}"

    with pytest.raises(SystemExit) as exc:
        cli_module.build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert ("deadbeef" * 5) in out


def test_version_falls_back_to_package_version(monkeypatch):
    from sidecar import cli as cli_module

    class FakeDist:
        version = "0.1.0"

        def read_text(self, name):
            return None

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: FakeDist())
    assert cli_module.sidecar_version_string() == "sidecar 0.1.0 (install sha unknown)"

    def boom(name):
        from importlib.metadata import PackageNotFoundError

        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.distribution", boom)
    assert cli_module.sidecar_version_string() == "sidecar 0.1.0 (install sha unknown)"


# --- 3.7 check: exit 0 ---


def test_check_all_ok_returns_0(tmp_path):
    config = _healthy_config(tmp_path)
    backend = _fake_backend_with_channels("general")
    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(backend))
    assert exit_code == 0
    assert [i.name for i in items] == ["config", "token", "herdr", "channel"]
    assert all(i.status == "ok" for i in items)


def test_check_json_report_shape_and_text(tmp_path, capsys):
    config = _healthy_config(tmp_path)
    backend = _fake_backend_with_channels("general")
    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(backend))
    assert _report_check([i.to_dict() for i in items], exit_code, True) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["exit"] == 0
    assert [c["name"] for c in parsed["checks"]] == ["config", "token", "herdr", "channel"]

    assert _report_check([i.to_dict() for i in items], exit_code, False) == 0
    text = capsys.readouterr().out
    assert "ok config" in text
    assert "exit 0" in text


# --- 3.7 check: exit 1 (definitely unhealthy) ---


def test_check_token_missing_is_exit_1(tmp_path):
    config = _healthy_config(tmp_path, a2a_token_path=tmp_path / "no-such-token")
    items, exit_code = run_checks(
        config, FakeHerdrClient(), _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 1
    token_item = next(i for i in items if i.name == "token")
    assert token_item.status == "fail"


def test_check_token_bad_permissions_is_exit_1(tmp_path):
    config = _healthy_config(tmp_path, token_mode=0o644)
    items, exit_code = run_checks(
        config, FakeHerdrClient(), _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 1
    assert next(i for i in items if i.name == "token").status == "fail"


def test_check_token_0400_is_accepted(tmp_path):
    """D11: read-only mounts (0400) are owner-only, like 0600."""
    config = _healthy_config(tmp_path, token_mode=0o400)
    items, exit_code = run_checks(
        config, FakeHerdrClient(), _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 0
    assert next(i for i in items if i.name == "token").status == "ok"


def test_check_missing_channel_repo_is_exit_1(tmp_path):
    config = _healthy_config(tmp_path, channel_repo=None)
    items, exit_code = run_checks(
        config, FakeHerdrClient(), _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 1
    assert next(i for i in items if i.name == "config").status == "fail"


def test_check_herdr_incompatible_is_exit_1(tmp_path):
    config = _healthy_config(tmp_path)
    herdr = FakeHerdrClient()
    herdr.server_status = {"status": "running", "running": True, "version": "0.1.0", "compatible": False}
    items, exit_code = run_checks(
        config, herdr, _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 1
    assert next(i for i in items if i.name == "herdr").status == "fail"


def test_check_channel_401_is_exit_1(tmp_path):
    config = _healthy_config(tmp_path)

    class DeniedBackend(FakeBackend):
        def list_channels(self):
            raise AuthenticationError("Invalid token (HTTP 401)")

    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(DeniedBackend()))
    assert exit_code == 1
    assert next(i for i in items if i.name == "channel").status == "fail"


def test_check_channel_403_is_exit_1(tmp_path):
    config = _healthy_config(tmp_path)

    class LimitedBackend(FakeBackend):
        def list_channels(self):
            raise RateLimitError("Secondary rate limit (HTTP 403)", retry_after=60)

    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(LimitedBackend()))
    assert exit_code == 1
    assert next(i for i in items if i.name == "channel").status == "fail"


def test_check_missing_config_file_is_exit_1(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "no-such.json"), "check", "--json"])
    assert code == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ok"] is False
    assert parsed["exit"] == 1
    assert parsed["checks"][0]["name"] == "config"
    assert parsed["checks"][0]["status"] == "fail"


# --- 3.7 check: exit 2 (unknown) ---


def test_check_herdr_unreachable_is_exit_2(tmp_path):
    config = _healthy_config(tmp_path)
    herdr = FakeHerdrClient()
    herdr.next_server_error = HerdrError(["herdr", "status"], 1, "no server at socket")
    items, exit_code = run_checks(
        config, herdr, _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 2
    assert next(i for i in items if i.name == "herdr").status == "unknown"


def test_check_channel_500_is_exit_2(tmp_path):
    config = _healthy_config(tmp_path)

    class FlakyBackend(FakeBackend):
        def list_channels(self):
            raise BackendError("Failed to list channels: HTTP 500")

    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(FlakyBackend()))
    assert exit_code == 2
    assert next(i for i in items if i.name == "channel").status == "unknown"


def test_check_channel_connection_error_is_exit_2(tmp_path):
    config = _healthy_config(tmp_path)

    class OfflineBackend(FakeBackend):
        def list_channels(self):
            raise BackendError("Network error connecting to GitHub: refused")

    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(OfflineBackend()))
    assert exit_code == 2
    assert next(i for i in items if i.name == "channel").status == "unknown"


def test_check_report_contains_no_secrets(tmp_path, capsys):
    config = _healthy_config(tmp_path)

    class HostileBackend(FakeBackend):
        def list_channels(self):
            raise BackendError("boom test-token-xyz leaked?")

    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(HostileBackend()))
    _report_check([i.to_dict() for i in items], exit_code, True)
    out = capsys.readouterr().out
    assert "test-token-xyz" not in out


# --- 3.8 herdr session ---


def test_config_loads_herdr_session(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"herdr_session": "worker"}))
    from sidecar.config import load_config

    assert load_config(cfg_file).herdr_session == "worker"


def test_real_herdr_client_sends_global_session_option(monkeypatch):
    captured: list[list[str]] = []

    def mock_run(cmd, capture_output=True, text=True, env=None):
        captured.append(cmd)
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout='{"status": "running", "running": true, "compatible": true}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RealHerdrClient(session="worker")
    status = client.status_server()
    assert captured[-1] == ["herdr", "--session", "worker", "status", "server", "--json"]
    assert status["running"] is True


def test_fake_herdr_status_server_default_and_error():
    herdr = FakeHerdrClient()
    assert herdr.status_server()["running"] is True
    assert ("status_server", (), {}) in herdr.calls

    herdr.next_server_error = HerdrError(["herdr", "status"], 1, "gone")
    with pytest.raises(HerdrError):
        herdr.status_server()


# --- 3.9 agora forks in status ---


def _status_args(config_path, as_json=True):
    return argparse.Namespace(config=str(config_path), json=as_json)


def test_status_lists_own_agora_forks(tmp_path, capsys):
    state_dir = tmp_path / "state"
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "state_dir": str(state_dir),
        "identities": ["worker-team"],
        "teams": {},
    }))

    agora = FakeAgoraClient()
    agora.add_session("agora:01JFORK1", identity="worker-team", message="msg-1", title="Do thing")
    agora.add_session("agora:01JFORK2", identity="worker-team", message="msg-2", active=True)
    agora.add_session("agora:01JOTHER", identity="other-team", message="msg-9")
    agora.add_session("agora:01JHOME", identity="worker-team", fork=False)

    assert cmd_status(_status_args(cfg_file), agora_client=agora) == 0
    parsed = json.loads(capsys.readouterr().out)
    ids = [f["id"] for f in parsed["agora_forks"]]
    assert ids == ["agora:01JFORK1", "agora:01JFORK2"]
    assert parsed["agora_forks"][0]["message"] == "msg-1"
    assert parsed["agora_forks"][1]["active"] is True
    assert parsed["agora_forks_error"] is None
    # The lookup used the --filter shape (D11).
    assert agora.searches == [[f"sidecar.identity=worker-team"]]

    assert cmd_status(_status_args(cfg_file, as_json=False), agora_client=agora) == 0
    text = capsys.readouterr().out
    assert "Agora forks:" in text
    assert "agora:01JFORK1" in text
    assert "[active] agora:01JFORK2" in text
    assert "agora:01JOTHER" not in text


def test_status_survives_agora_failure(tmp_path, capsys):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "state_dir": str(tmp_path / "state"),
        "identities": ["worker-team"],
    }))

    class BrokenAgora:
        def search_sessions(self, filters):
            raise RuntimeError("Drive is away")

    assert cmd_status(_status_args(cfg_file), agora_client=BrokenAgora()) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["agora_forks"] == []
    assert "Drive is away" in parsed["agora_forks_error"]


def test_fake_agora_client_filter_matching():
    agora = FakeAgoraClient()
    agora.add_session("agora:01JA", identity="team-a", message="m1")
    agora.add_session("agora:01JB", identity="team-b", message="m2")
    rows = agora.search_sessions(["sidecar.identity=team-a"])
    assert [r["id"] for r in rows] == ["agora:01JA"]
    assert rows[0]["message"] == "m1"


def test_check_herdr_non_dict_status_is_unknown(tmp_path):
    """batch3: an unparseable herdr status is unknown, not a failure."""
    config = _healthy_config(tmp_path)

    class WeirdHerdr(FakeHerdrClient):
        def status_server(self):
            return "running-ish"

    items, exit_code = run_checks(
        config, WeirdHerdr(), _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 2
    assert next(i for i in items if i.name == "herdr").status == "unknown"


def test_check_channel_404_is_exit_1(tmp_path):
    """batch3: a definitive client error (404) fails the check."""
    config = _healthy_config(tmp_path)

    class GoneBackend(FakeBackend):
        def list_channels(self):
            raise BackendError("Failed to list channels: HTTP 404")

    items, exit_code = run_checks(config, FakeHerdrClient(), _ok_backend_factory(GoneBackend()))
    assert exit_code == 1
    assert next(i for i in items if i.name == "channel").status == "fail"


def test_check_backend_factory_error_is_unknown(tmp_path):
    """batch3: when the backend itself cannot be built, the channel is unknown."""
    config = _healthy_config(tmp_path)

    def boom_factory(channel_repo, token):
        raise RuntimeError("transport misconfigured")

    items, exit_code = run_checks(config, FakeHerdrClient(), boom_factory)
    assert exit_code == 2
    assert next(i for i in items if i.name == "channel").status == "unknown"


def test_check_fail_beats_unknown_precedence(tmp_path):
    """batch3: a definite failure wins over unknowns (documented in --help)."""
    config = _healthy_config(tmp_path, token_mode=0o644)
    herdr = FakeHerdrClient()
    herdr.next_server_error = HerdrError(["herdr", "status"], 1, "no server at socket")
    items, exit_code = run_checks(
        config, herdr, _ok_backend_factory(_fake_backend_with_channels("general"))
    )
    assert exit_code == 1
    assert next(i for i in items if i.name == "token").status == "fail"
    assert next(i for i in items if i.name == "herdr").status == "unknown"


def test_check_help_documents_precedence_and_version_fallback(capsys):
    """batch3: --help states the exit-code precedence and sha fallback."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["check", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "1 when any check definitely fails" in text
    assert "2 when nothing failed but something" in text

    with pytest.raises(SystemExit) as exc2:
        build_parser().parse_args(["--help"])
    assert exc2.value.code == 0
    top = " ".join(capsys.readouterr().out.split())
    assert "or the package version when the sha is unknown" in top


def test_real_herdr_client_tab_create_carries_session(monkeypatch):
    """batch3: every herdr call carries the global --session option."""
    captured: list[list[str]] = []

    def mock_run(cmd, capture_output=True, text=True, env=None):
        captured.append(cmd)
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout='{"result": {"tab": {"tab_id": "w1:t1"}, "root_pane": {"pane_id": "w1:p1"}}}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = RealHerdrClient(session="worker")
    client.tab_create(cwd="/tmp", env={"K": "V"})
    assert captured[-1] == [
        "herdr", "--session", "worker", "tab", "create",
        "--cwd", "/tmp", "--env", "K=V", "--no-focus",
    ]
