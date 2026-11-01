from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from _round_seam import in_process_start_child
from agora import cli, store
from agora import header as h
from agora.agents.base import AgentError, Exported, Launch

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


class FileBackedForkAgent:
    name = "opencode"

    def __init__(self, raw_dir: Path):
        self.raw_dir = raw_dir
        self.sessions: dict[str, list[str]] = {"ses_a": ["把 CSV 轉成 Markdown 表格", "好的，三個步驟"]}
        self.launched: list[Launch] = []
        self.speak = False
        self.appended: list = []

    def _exported(self, sid: str) -> Exported:
        msgs = self.sessions[sid]
        raw_bytes = json.dumps({"id": sid, "m": msgs}, ensure_ascii=False).encode("utf-8")
        raw_file = self.raw_dir / f"{sid}-raw.json"
        raw_file.write_bytes(raw_bytes)
        raw_md5 = hashlib.md5(raw_bytes).hexdigest()
        turns = [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]
        return Exported(
            session_id=sid,
            raw_file=raw_file,
            raw_md5=raw_md5,
            raw_size=len(raw_bytes),
            dir="/tmp",
            title=msgs[0][:20],
            created_at="2026-10-01T00:00:00Z",
            agent_version="9.9",
            message_count=len(msgs),
            turns=turns,
        )

    def export(self, sid: str) -> Exported:
        if sid not in self.sessions:
            raise AgentError(f"沒有這個 session：{sid}")
        return self._exported(sid)

    def turns(self, raw: bytes):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def native(self, turns):
        return json.dumps({"id": "synth", "m": ["\n".join(lines) for _role, lines in turns]},
                          ensure_ascii=False).encode("utf-8")

    def append_native(self, raw: bytes, turns):
        self.appended.append((raw, turns))
        msgs = list(json.loads(raw)["m"])
        if len(msgs) % 2:
            msgs.append(cli.NO_REPLY)
        msgs += ["\n".join(lines) for _role, lines in turns]
        return json.dumps({"id": "appended", "m": msgs}, ensure_ascii=False).encode("utf-8")

    def summarize(self, prompt, workdir):
        return json.dumps({"purpose": "合併要約", "decisions": [], "progress": "進行中",
                           "open_questions": []}, ensure_ascii=False), "fake-model"

    def start_native(self, raw: bytes, workdir):
        new = f"ses_n{len(self.sessions)}"
        self.sessions[new] = list(json.loads(raw)["m"])
        launch = Launch(argv=["true"], cwd=str(workdir), agent_session_id=new, before_count=len(self.sessions[new]))
        self.launched.append(launch)
        return launch

    def collect(self, launch: Launch):
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
    raw_dir = tmp_path / "agent_raw"
    raw_dir.mkdir()
    agent = FileBackedForkAgent(raw_dir)
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    return agent


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def paths():
    return store.Paths.from_env()


def header_of(agora_id):
    return store.Index(paths()).header(agora_id.split(":")[1])


def _import(capsys, *extra):
    return run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode", *extra)


def _fork(capsys, agora_id, *extra):
    return run(capsys, "continue", "session", agora_id, "--agent", "opencode", "--fork", "--dir", "/tmp", *extra)


def test_fork_does_not_read_exported_raw_and_matches_content(env, capsys, monkeypatch):
    code, a, err = _import(capsys, "--header", "title=表格排版", "--header", "description=先做讀取")
    assert code == 0, err

    accesses: list[int] = []
    real_getter = Exported.raw.fget

    def tracked_raw(self):
        accesses.append(1)
        return real_getter(self)

    monkeypatch.setattr(Exported, "raw", property(tracked_raw))

    code, b, err = _fork(capsys, a)
    assert code == 0, err
    assert b.startswith("agora:") and b != a
    assert not accesses, f"Exported.raw was accessed {len(accesses)} time(s) during fork"

    b_ulid = b.split(":")[1]
    hdr = header_of(b)
    fork_sid = env.launched[-1].agent_session_id
    expected_raw = (env.raw_dir / f"{fork_sid}-raw.json").read_bytes()
    expected_md5 = hashlib.md5(expected_raw).hexdigest()

    assert h.agora_of(hdr)["raw"]["md5"] == expected_md5
    saved_raw_path = paths().mirror / b_ulid / store.raw_name(h.agora_of(hdr)["raw"])
    assert saved_raw_path.read_bytes() == expected_raw
    assert store.fetch_raw(paths(), store.Drive(paths()), b_ulid, hdr) == expected_raw
    assert hashlib.md5(saved_raw_path.read_bytes()).hexdigest() == expected_md5


def test_load_for_launch_fork_does_not_read_exported_raw(env, capsys, monkeypatch):
    code, a, err = _import(capsys, "--header", "title=表格排版", "--header", "description=先做讀取")
    assert code == 0, err

    parent_hdr = header_of(a)
    state = {
        "agent": "opencode",
        "source_id": a,
        "parent": parent_hdr,
        "fork": True,
        "updates": {},
        "add_tags": [],
        "workdir": "/tmp",
        "headless": False,
        "headless_port": None,
        "harness_file": None,
        "agent_arg": [],
    }

    accesses: list[int] = []
    real_getter = Exported.raw.fget

    def tracked_raw(self):
        accesses.append(1)
        return real_getter(self)

    monkeypatch.setattr(Exported, "raw", property(tracked_raw))

    result = cli._load_for_launch(paths(), state)
    assert not accesses, f"Exported.raw was accessed {len(accesses)} time(s) in _load_for_launch"

    fork_sid = result["launch"]["agent_session_id"]
    expected_raw = (env.raw_dir / f"{fork_sid}-raw.json").read_bytes()
    expected_md5 = hashlib.md5(expected_raw).hexdigest()

    assert result["fork_md5"] == expected_md5
    assert result["saved_fork"] is not None
    fork_id = result["fork_id"]
    fork_ulid = fork_id.split(":")[1]
    saved_hdr = store.Index(paths()).header(fork_ulid)
    assert h.agora_of(saved_hdr)["raw"]["md5"] == expected_md5
    saved_raw = paths().mirror / fork_ulid / store.raw_name(h.agora_of(saved_hdr)["raw"])
    assert saved_raw.read_bytes() == expected_raw
