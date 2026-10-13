"""v2 write path against the real opencode (change agora-opencode-v2, 2.8).

Isolated XDG+HOME, managed-service port moved off 49374 first, fake upstream
for the model (no real model, no credentials, no network beyond 127.0.0.1):

    native() -> real `session import` -> `run -s` one round -> collect

The reading version keeps the earlier keyword, and at least once the test pins
`message_count` vs `idle` (M8): idle rows are markers, not new content.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agora.agents import base, opencode as oc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import opencode_noninteractive as agent  # noqa: E402
from fake_upstream import start as start_upstream, write_config  # noqa: E402

pytestmark = pytest.mark.integration

REAL = agent.real_opencode()

if REAL is None:
    pytest.skip("opencode is not installed", allow_module_level=True)
if not agent.session_export_supported():
    pytest.skip("needs opencode 2.x (`session export`)", allow_module_level=True)


def _default_port_listening() -> str:
    proc = subprocess.run(["lsof", "-nP", "-iTCP:49374", "-sTCP:LISTEN"],
                          capture_output=True, text=True)
    return proc.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_v2_native_import_run_collect(tmp_path, monkeypatch):
    before = _default_port_listening()
    # Isolation: four XDG + HOME point under tmp_path (never the real store);
    # pytest-outside AGORA_* point there too (conftest only isolates inside pytest).
    home = tmp_path / "home"
    data = tmp_path / "data"
    config = tmp_path / "config"
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    for d in (home, data, config, state, cache):
        d.mkdir(parents=True, exist_ok=True)
    for var, val in (("HOME", str(home)), ("XDG_DATA_HOME", str(data)),
                     ("XDG_CONFIG_HOME", str(config)), ("XDG_STATE_HOME", str(state)),
                     ("XDG_CACHE_HOME", str(cache)),
                     ("AGORA_CONFIG", str(tmp_path / "agora-config")),
                     ("AGORA_CACHE_DIR", str(tmp_path / "agora-cache")),
                     ("AGORA_STATE_DIR", str(tmp_path / "agora-state")),
                     ("AGORA_RCLONE", "fake"),
                     ("RCLONE_CONFIG", str(tmp_path / "agora-config" / "rclone.conf"))):
        monkeypatch.setenv(var, val)
    for var in ("OPENCODE_API_KEY", "OPENCODE_PERMISSION"):
        monkeypatch.delenv(var, raising=False)
    # Port isolation first (H2): the default 49374 is machine-shared, XDG is not enough.
    free_service = _free_port()
    subprocess.run([REAL, "service", "set", "port", str(free_service)],
                   check=True, capture_output=True, text=True)
    # Fake upstream for the model (M6): local, no credentials, deterministic text.
    upstream = start_upstream(mode="text", log=str(tmp_path / "upstream.log"))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path))
    try:
        write_config(config / "opencode" / "opencode.json", upstream.url)
        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
        subprocess.run(["git", "-c", "user.email=agora@example.invalid",
                        "-c", "user.name=agora", "commit", "-q", "--allow-empty",
                        "-m", "init"], cwd=str(proj), check=True)

        recorded: list[str] = []
        try:
            turns = [("user", ["ZZNATIVE 前文關鍵字"]),
                     ("assistant", ["ZZNATIVE 回答"])]
            built = oc.ADAPTER.native(turns)
            launch = oc.ADAPTER.start_native(built, proj)
            recorded.append(launch.agent_session_id)
            assert launch.before_count == 2  # two user/assistant rows, no idle yet

            proc = subprocess.run(
                [REAL, "run", "--standalone", "-m", "fakeup/fake-model",
                 "-s", launch.agent_session_id, "--format", "json", "第二輪"],
                cwd=str(proj), env={**os.environ, "PWD": str(proj)},
                capture_output=True, text=True, timeout=120)
            assert proc.returncode == 0, proc.stderr[-500:]

            collected = oc.ADAPTER.collect(launch)
            assert collected is not None, "the agent said something new"
            body = base.reading(oc.ADAPTER, collected.raw)
            assert "ZZNATIVE 前文關鍵字" in body  # earlier history survived
            # M8: the fake's run appends user+assistant+idle; only the first two
            # are conversation, so the count grows by exactly 2 while an idle row
            # is really there.
            assert collected.message_count == launch.before_count + 2
            landed = json.loads(collected.raw)
            assert any(m.get("type") == "idle" for m in landed["messages"])
            assert collected.message_count == sum(
                1 for m in landed["messages"] if m.get("type") in ("user", "assistant"))
        finally:
            for sid in sorted(set(recorded)):
                subprocess.run([REAL, "session", "delete", sid, "--standalone"],
                               cwd=str(proj), env={**os.environ, "PWD": str(proj)},
                               capture_output=True)
    finally:
        upstream.stop()
    after = _default_port_listening()
    # H2: the shared managed service was never touched - only looked at.
    assert before.splitlines()[:1] == after.splitlines()[:1] or (not before and not after)
