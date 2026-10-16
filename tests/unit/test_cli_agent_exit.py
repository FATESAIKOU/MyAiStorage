"""`agora continue`: an agent that dies must not look like a clean write-back (#76).

The bug: `_wait_agent` only `proc.wait()`ed and never read `returncode`, so the
exit code came from the write-back alone - a dead agent with nothing new looked
like exit 0. The contract (same shape as #54's `agora-locked`): the write-back
still happens, but a positive exit (>0, except the 130/143 Ctrl-C/SIGTERM
convention and a SIGTERM agora itself forwarded) prints a fixed stderr line
`agora-agent-failed <code>`; the exit is the agent's code when the write-back
succeeded, otherwise the write-back's own failure code (3).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import sys
from pathlib import Path

from agora import cli, store
from agora.agents.base import Exported, Launch

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


class ExitAgent:
    """In-memory sessions; the launch is `["true"]` so tests can swap its exit."""

    name = "opencode"

    def __init__(self):
        self.sessions: dict[str, list[str]] = {"ses_a": ["把 CSV 轉成 Markdown 表格", "好的，三個步驟"]}
        self.launched: list[Launch] = []
        self.speak = True

    def _exported(self, sid):
        msgs = self.sessions[sid]
        return Exported(session_id=sid, raw=json.dumps({"id": sid, "m": msgs}, ensure_ascii=False).encode(),
                        dir="/tmp", title=msgs[0][:20], created_at="2026-10-01T00:00:00Z",
                        agent_version="9.9", message_count=len(msgs))

    def export(self, sid):
        return self._exported(sid)

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def native(self, turns):
        return json.dumps({"id": "synth", "m": ["\n".join(lines) for _role, lines in turns]},
                          ensure_ascii=False).encode()

    def append_native(self, raw, turns):
        msgs = list(json.loads(raw)["m"])
        if len(msgs) % 2:
            msgs.append(cli.NO_REPLY)
        msgs += ["\n".join(lines) for _role, lines in turns]
        return json.dumps({"id": "appended", "m": msgs}, ensure_ascii=False).encode()

    def summarize(self, prompt, workdir):
        return json.dumps({"purpose": "合併要約", "decisions": [], "progress": "進行中",
                           "open_questions": []}, ensure_ascii=False), "fake-model"

    def start_native(self, raw, workdir):
        new = f"ses_n{len(self.sessions)}"
        self.sessions[new] = list(json.loads(raw)["m"])
        launch = Launch(argv=["true"], cwd=str(workdir), agent_session_id=new, before_count=len(self.sessions[new]))
        self.launched.append(launch)
        return launch

    def collect(self, launch):
        if self.speak:
            self.sessions[launch.agent_session_id].append("接著做完了")
        exported = self._exported(launch.agent_session_id)
        return exported if exported.message_count > launch.before_count else None


def _env_fixture(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    agent = ExitAgent()
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    return agent


try:
    import pytest

    @pytest.fixture
    def env(tmp_path, monkeypatch):
        return _env_fixture(tmp_path, monkeypatch)
except ImportError:  # pragma: no cover
    pass


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def _die_with(monkeypatch, argv):
    """Run `argv` instead of the fake's `["true"]`, keeping everything else."""
    real = cli.subprocess.Popen

    def spy(call_argv, **kwargs):
        if call_argv == ["true"]:
            call_argv = argv
        return real(call_argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", spy)


def _fail_with(monkeypatch, code: int):
    _die_with(monkeypatch, [sys.executable, "-c", f"import sys; sys.exit({code})"])


def test_agent_fails_with_no_new_content_reports_its_code(env, capsys, monkeypatch):
    """#76: the agent dies (exit 3) before saying anything - the old code said 0."""
    env.speak = False
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 3)
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0")
    assert code == 3, err
    assert "agora-agent-failed 3" in err
    assert "沒有新內容" in err


def test_agent_fails_but_new_content_is_still_written_back(env, capsys, monkeypatch):
    """#76: what the agent said before dying is not lost, and the exit is still its."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 3)
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0")
    assert code == 3, err
    assert "agora-agent-failed 3" in err
    assert out == parent
    code, shown, _ = run(capsys, "show", "session", parent)
    assert code == 0
    assert "接著做完了" in shown


def test_agent_exit_zero_keeps_old_behavior(env, capsys, monkeypatch):
    """#76: a clean agent changes nothing - exit 0 and no fixed line."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                         "--dir", "/tmp", "--sync-period", "0")
    assert code == 0, err
    assert "agora-agent-failed" not in err
    assert out == parent


def test_ctrl_c_convention_codes_are_not_failures(env, capsys, monkeypatch):
    """#76: 130 (Ctrl-C) and 143 (SIGTERM) are a goodbye, not a crash."""
    for conventional in (130, 143):
        _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                           "--agent", "opencode")
        _fail_with(monkeypatch, conventional)
        code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                           "--dir", "/tmp", "--sync-period", "0")
        assert code == 0, (conventional, err)
        assert "agora-agent-failed" not in err, conventional


def test_sigterm_death_is_not_a_failure(env, capsys, monkeypatch):
    """#76: killed by a signal (-15) stays 0 - the headless gentle stop depends on it."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _die_with(monkeypatch, ["sh", "-c", "kill -TERM $$"])
    code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")
    assert code == 0, err
    assert "agora-agent-failed" not in err


def test_agent_failure_plus_upload_failure_keeps_exit_3_with_signal(
        env, capsys, monkeypatch):
    """#76: the write-back's own failure (3) wins over the agent's code, but the line stays."""
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a",
                       "--agent", "opencode")
    _fail_with(monkeypatch, 5)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    code, _, err = run(capsys, "continue", "session", parent, "--agent", "opencode",
                       "--dir", "/tmp", "--sync-period", "0")
    assert code == cli.EXIT_IN_OUTBOX == 3, err
    assert "agora-agent-failed 5" in err
