"""The agora command over a fake rclone and a fake agent (test-plan U-CLI)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h
from agora.agents.base import Exported, Launch, format_reading

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


class FakeAgent:
    """Stores sessions in memory; launching 'talks' by appending a message."""

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

    def reading(self, raw):
        msgs = json.loads(raw)["m"]
        return format_reading([("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)])

    def start_native(self, raw, workdir):
        new = f"ses_n{len(self.sessions)}"
        self.sessions[new] = list(json.loads(raw)["m"])
        launch = Launch(argv=["true"], cwd=str(workdir), agent_session_id=new, before_count=len(self.sessions[new]))
        self.launched.append(launch)
        return launch

    def start_injected(self, reading_file, workdir):
        new = f"ses_i{len(self.sessions)}"
        self.sessions[new] = [f"read {reading_file}"]
        launch = Launch(argv=["true"], cwd=str(workdir), agent_session_id=new, before_count=1)
        self.launched.append(launch)
        return launch

    def collect(self, launch):
        if self.speak:
            self.sessions[launch.agent_session_id].append("接著做完了")
        exported = self._exported(launch.agent_session_id)
        return exported if exported.message_count > launch.before_count else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    agent = FakeAgent()
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    monkeypatch.setattr(cli, "EXIT_CODE", 0)
    return agent


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def test_import_then_search(env, capsys):
    code, out, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a",
                       "--header", "title=CSV 規劃", "--header", "先做讀取")
    assert code == 0 and out.startswith("agora:")
    code, found, _ = run(capsys, "search", "session", "表格", "--no-sync")
    assert found.split()[0] == out
    hdr, _ = h.split_document((store.Paths.from_env().mirror / out.split(":")[1] / "session.md").read_text())
    assert hdr["title"] == "CSV 規劃" and hdr["note"] == "先做讀取"
    assert hdr["source"]["session_id"] == "ses_a" and hdr["relation"] == "import"


def test_reimport_unchanged_keeps_id(env, capsys):
    _, first, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    _, second, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    assert first == second


def test_reimport_changed_without_children_updates_same_id(env, capsys):
    _, first, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    env.sessions["ses_a"].append("新的一則")
    _, second, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    assert first == second


def test_reimport_after_continue_branches(env, capsys):  # S7
    _, first, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    run(capsys, "continue-session", first, "--agent", "opencode", "--dir", "/tmp")
    env.sessions["ses_a"].append("來源端又改了")
    _, second, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    assert second != first
    hdr = store.Index(store.Paths.from_env()).header(second.split(":")[1])
    assert hdr["parents"][0]["id"] == first


def test_continue_native_creates_child(env, capsys):
    _, parent, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    code, child, _ = run(capsys, "continue-session", parent, "--agent", "opencode", "--dir", "/tmp",
                         "--header", "tags=next")
    assert code == 0 and child.startswith("agora:") and child != parent
    hdr = store.Index(store.Paths.from_env()).header(child.split(":")[1])
    assert hdr["relation"] == "continue" and hdr["parents"][0]["id"] == parent
    assert hdr["tags"] == ["next"]
    assert env.launched[-1].agent_session_id.startswith("ses_n")   # same agent → native
    assert not list(store.Paths.from_env().pending.glob("*.json"))


def test_continue_with_nothing_new_saves_nothing(env, capsys):
    _, parent, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    env.speak = False
    code, out, err = run(capsys, "continue-session", parent, "--agent", "opencode", "--dir", "/tmp")
    assert code == 0 and out == "" and "沒有新內容" in err


def test_merge_then_continue_uses_injection(env, capsys):  # L3
    _, a, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_b")
    code, merged, _ = run(capsys, "merge-session", f"{a},{b}")
    assert code == 0
    paths = store.Paths.from_env()
    hdr = store.Index(paths).header(merged.split(":")[1])
    assert hdr["relation"] == "merge" and [p["id"] for p in hdr["parents"]] == [a, b]
    assert "raw" not in hdr
    _, child, _ = run(capsys, "continue-session", merged, "--agent", "opencode", "--dir", "/tmp")
    assert env.launched[-1].agent_session_id.startswith("ses_i")   # merge → injection


def test_upload_failure_exits_3_and_stays_searchable(env, capsys, monkeypatch):  # N13
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    code, out, err = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    assert code == cli.EXIT_IN_OUTBOX and "outbox" in err
    monkeypatch.setattr(cli, "EXIT_CODE", 0)
    _, found, _ = run(capsys, "search", "session", "CSV")
    assert found.split()[0] == out


def test_pending_from_dead_agora_is_finished_later(env, capsys):  # S3, N2
    _, parent, _ = run(capsys, "import", "--format", "opencode", "--session-id", "ses_a")
    paths = store.Paths.from_env()
    env.sessions["ses_x"] = ["a", "b"]
    record = {"agora_id": "agora:01K6DEADBEEF000000000000AA", "agent": "opencode",
              "agent_session_id": "ses_x", "dir": "/tmp", "before_count": 2,
              "parent": {"id": parent, "raw_md5": None}, "title": "t"}
    cli._write_pending(paths, record)          # nobody holds the lock: that agora died
    _, _, err = run(capsys, "sync")
    assert "補存" in err
    assert store.Index(paths).header("01K6DEADBEEF000000000000AA")["relation"] == "continue"


def test_pending_held_by_live_agora_is_left_alone(env, capsys):  # N2
    import fcntl
    paths = store.Paths.from_env()
    record = {"agora_id": "agora:01K6LIVE0000000000000000AA", "agent": "opencode",
              "agent_session_id": "ses_a", "dir": "/tmp", "before_count": 0,
              "parent": {"id": "agora:x", "raw_md5": None}}
    path = cli._write_pending(paths, record)
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        run(capsys, "sync")
        assert path.exists()


def test_search_rejects_unknown_filter(env, capsys):
    code, _, err = run(capsys, "search", "session", "x", "--header", "foo=bar", "--no-sync")
    assert code == 2 and "agent" in err
