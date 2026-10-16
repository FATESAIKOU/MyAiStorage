"""AGORA_OPENCODE_CMD / AGORA_CLAUDE_CMD: the agent executable interface (#77).

`base.agent_cmd()` is how every call that runs an agent CLI picks the executable:
`AGORA_<NAME>_CMD` (an executable path or a name found on PATH) overrides the
plain `opencode`/`claude` name. MyLinuxPool's sidecar relies on it
(`AGORA_CLAUDE_CMD=claude-real`), so these tests pin both halves - the env value
is really the one started, and unset means the plain name - even though the
docstring used to call the override a test-only thing.

Fake agents only (tests/fakes). Each test that starts something also puts a
decoy of the same plain name first on PATH: a regression that ignores the
environment fails here instead of reaching the user's real agent CLI.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agora import cli
from agora.agents import base
from agora.agents import claude as C
from agora.agents import opencode as OC

FAKES = Path(__file__).resolve().parent.parent / "fakes"
FAKE_RCLONE = FAKES / "fake_rclone.py"
FAKE_CLAUDE = FAKES / "fake_claude.py"
FAKE_OPENCODE = FAKES / "fake_opencode.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
OC_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
CL_SID = "11111111-2222-4333-8444-555555555555"


def _wrapper(path: Path, target: Path) -> Path:
    path.write_text(f"#!/bin/sh\nexec {sys.executable} {target} \"$@\"\n")
    path.chmod(0o755)
    return path


def _path_decoy(tmp_path: Path, monkeypatch, name: str) -> Path:
    """A `claude`/`opencode` first on PATH that a passing test never runs."""
    bin_dir = tmp_path / "decoy-bin"
    bin_dir.mkdir(exist_ok=True)
    marker = tmp_path / "decoy.log"
    decoy = bin_dir / name
    decoy.write_text(f'#!/bin/sh\necho "$0 $@" >> "{marker}"\nexit 9\n')
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return marker


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


@pytest.fixture
def claude_lab(tmp_path, monkeypatch):
    """The real claude adapter over tests/fakes/fake_claude.py, fixture session laid out."""
    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(_wrapper(tmp_path / "rclone", FAKE_RCLONE)))
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    work = tmp_path / "proj"
    work.mkdir()
    proj = home / ".claude" / "projects" / C.encode_project_dir(work)
    proj.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    return {"home": home, "work": work, "fake_home": fake_home}


def test_agent_cmd_reads_the_env_and_falls_back_to_the_name(monkeypatch):
    monkeypatch.delenv("AGORA_CLAUDE_CMD", raising=False)
    monkeypatch.delenv("AGORA_OPENCODE_CMD", raising=False)
    assert base.agent_cmd("claude") == "claude"
    assert base.agent_cmd("opencode") == "opencode"
    monkeypatch.setenv("AGORA_CLAUDE_CMD", "/some/where/claude-real")
    monkeypatch.setenv("AGORA_OPENCODE_CMD", "opencode-v2")
    assert base.agent_cmd("claude") == "/some/where/claude-real"
    assert base.agent_cmd("opencode") == "opencode-v2"


def test_claude_defaults_to_the_plain_name(tmp_path, monkeypatch):
    """Unset, argv starts with plain `claude` (no env, no spawn)."""
    monkeypatch.delenv("AGORA_CLAUDE_CMD", raising=False)
    work = tmp_path / "proj"
    work.mkdir()
    raw = C.ADAPTER.native([("user", ["ZZ 一句"]), ("assistant", ["好"])])
    interactive = C.ADAPTER.start_native(raw, work)
    headless = C.ADAPTER.start_native(
        C.ADAPTER.native([("user", ["ZZ 再一句"]), ("assistant", ["好"])]),
        work, headless=True)
    assert interactive.argv[:1] == ["claude"]
    assert headless.argv[:1] == ["claude"]


def test_continue_starts_the_executable_the_env_names(claude_lab, tmp_path, monkeypatch,
                                                      capsys):
    """`agora continue` really starts the AGORA_CLAUDE_CMD executable as argv[0]."""
    wrapper = _wrapper(tmp_path / "claude-from-env", FAKE_CLAUDE)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(wrapper))
    decoy = _path_decoy(tmp_path, monkeypatch, "claude")

    launched: list[list[str]] = []
    real_popen = subprocess.Popen

    def spy(argv, **kwargs):
        launched.append(list(argv))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", spy)

    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    code, out, err = run(capsys, "continue", "session", a, "--agent", "claude",
                         "--dir", str(claude_lab["work"]), "--sync-period", "0")
    assert code == 0, err
    assert out == a
    # the one agent child `continue` starts is the env-named executable, up front
    agent_calls = [call for call in launched if call[0] == str(wrapper)]
    assert [call[1] for call in agent_calls] == ["--resume"]
    logged = (claude_lab["fake_home"] / "claude-args.log").read_text()
    assert json.loads(logged.splitlines()[-1])[:1] == ["--resume"]
    assert not decoy.exists()


def test_import_export_start_the_executable_the_env_names(tmp_path, monkeypatch):
    """opencode's export and import calls both start the env-named executable."""
    fh = tmp_path / "fake-home"
    (fh / "opencode-sessions").mkdir(parents=True)
    shutil.copyfile(OC_FIX / "oc-v2-basic.json", fh / "opencode-sessions" / "default.json")
    monkeypatch.setenv("FAKE_HOME", str(fh))
    wrapper = _wrapper(tmp_path / "opencode-from-env", FAKE_OPENCODE)
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(wrapper))
    decoy = _path_decoy(tmp_path, monkeypatch, "opencode")

    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        if isinstance(argv, list):
            calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(OC.subprocess, "run", spy)

    work = tmp_path / "proj"
    work.mkdir()
    exported = OC.ADAPTER.export("default")                # `session export`
    launch = OC.ADAPTER.start_native(exported.raw, work)   # export + `session import`
    assert calls and calls[0][:3] == [str(wrapper), "session", "export"]
    assert ["session", "import"] in [call[1:3] for call in calls]
    assert all(call[0] == str(wrapper) for call in calls)
    assert launch.argv[0] == str(wrapper)
    assert not decoy.exists()
