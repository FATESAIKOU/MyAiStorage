"""The real opencode 2.x summarize path (change agora-opencode-v2, task 4.4).

`agora merge` writes its summaries with `opencode run --standalone`; the session
that run creates has to be deleted again, and the model it answered with has to be
read off the session (design 5.3). This test does exactly that against the
installed opencode 2.0.26, under the change's isolation recipe:

- `HOME` and all four XDG directories point inside this test's `tmp_path`, and the
  inherited `OPENCODE_*` variables are dropped; nothing of the user's own store is
  in reach (`opencode debug paths` was used to confirm the mapping);
- the managed service's port is moved to a free one inside that isolated
  `XDG_CONFIG_HOME` with `opencode service set port` - measured to only write
  `service.json` and contact nothing - so even a call that forgot `--standalone`
  could not reach the machine-wide service on the default port;
- the model is `tests/fakes/fake_upstream.py` on 127.0.0.1: no network beyond
  loopback, no credentials, and a fixed sentence the test asserts on (review M6);
- the machine-wide default port is only looked at, before and after, with `lsof`
  (never connected to, never stopped); if `lsof` is absent the check reports itself
  as unavailable rather than failing.

Run:
    uv run pytest -q -m integration tests/integration/test_opencode_v2_summarize.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import fake_upstream  # noqa: E402  (needs the path above)
import opencode_noninteractive as agent  # noqa: E402

from agora.agents import opencode as oc

pytestmark = pytest.mark.integration

REAL_OPENCODE = agent.real_opencode()
DEFAULT_PORT = 49374


def _shared_port() -> str | None:
    """What listens on the machine-wide default port, as `lsof` sees it.

    A look, never a knock: no connection is opened and the service is not asked
    anything (review H2). None when `lsof` is not on this machine at all.
    """
    try:
        proc = subprocess.run(["lsof", "-nP", f"-iTCP:{DEFAULT_PORT}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip()


def _free_port() -> int:
    """A port nothing is listening on right now, for the isolated service."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A real opencode that can only see this test's directories and model."""
    if REAL_OPENCODE is None:
        pytest.skip("這台機器沒有 opencode（AGORA_REAL_OPENCODE 或 PATH）")
    for name in [k for k in os.environ if k.startswith("OPENCODE_")]:
        monkeypatch.delenv(name)
    xdg = {var: tmp_path / var[4:-5].lower().replace("_", "-") for var in (
        "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var, path in xdg.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(path))
    # the version probe runs under the isolated env too (even --help stays inside)
    if not agent.session_export_supported():
        pytest.skip("這台機器沒有 opencode 2.x（session export）")
    # the recipe's "pytest 外" variables, now that this runs inside pytest (L3)
    monkeypatch.setenv("AGORA_RCLONE", str(tmp_path / "rclone-not-needed"))
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setenv("AGORA_OPENCODE_CMD", REAL_OPENCODE)
    monkeypatch.setenv("AGORA_OPENCODE_MODEL",
                       f"{fake_upstream.PROVIDER}/{fake_upstream.MODEL}")

    config = xdg["XDG_CONFIG_HOME"] / "opencode" / "opencode.json"
    upstream = fake_upstream.start(mode="text", log=tmp_path / "upstream-calls.log")
    fake_upstream.write_config(config, upstream.url)
    service_port = _free_port()
    set_port = subprocess.run([REAL_OPENCODE, "service", "set", "port", str(service_port)],
                              env={**os.environ}, capture_output=True, text=True, timeout=120)
    assert set_port.returncode == 0, set_port.stderr
    assert json.loads((config.parent / "service.json").read_text())["port"] == service_port

    workdir = tmp_path / "summarize"          # a project of its own, never the repo
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    try:
        yield upstream, workdir
    finally:
        upstream.stop()


def test_a_real_summarize_run_reads_the_model_and_deletes_its_session(isolated,
                                                                       monkeypatch):
    """4.4: the real opencode answers through the fake upstream, the model is read
    off the v2 session the run made, and that session is really gone afterwards."""
    upstream, workdir = isolated
    deleted: list[str] = []
    real_drop = oc.ADAPTER._drop_summary_session
    monkeypatch.setattr(oc.ADAPTER, "_drop_summary_session",
                        lambda session_id, wd: (deleted.append(session_id),
                                               real_drop(session_id, wd))[1])

    before = _shared_port()
    text, model = oc.ADAPTER.summarize("ZZ 用一句話寫個要約。", workdir)
    after = _shared_port()

    assert text == fake_upstream.DEFAULT_TEXT
    assert model == fake_upstream.MODEL       # read off the real v2 assistant row
    assert upstream.calls(), "opencode 沒有問到 fake upstream"
    assert deleted and deleted[0].startswith("ses_"), "沒有刪到要約用的 session"
    assert after == before, f"預設埠 {DEFAULT_PORT} 的服務被動到了：{before!r} -> {after!r}"
    # nothing of the run is left: no session in the isolated store, no record
    listed = subprocess.run([REAL_OPENCODE, "session", "list", "--standalone",
                             "--format", "json"], cwd=str(workdir),
                            capture_output=True, text=True, timeout=120)
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout or "[]") == []
    assert list(workdir.glob("pending-*")) == []
    assert list(workdir.glob("material-*")) == []
