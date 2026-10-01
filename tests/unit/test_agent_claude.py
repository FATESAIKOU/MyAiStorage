"""Unit tests for the Claude Code adapter (test-plan: U-RV-02, U-IMP-04,
U-IMP-05, U-CON-04/05, encode rule, collect-None).

Fixtures are hand-written and self-made (test-plan 0.3); the fake claude
covers --version / --resume. AGORA_CLAUDE_HOME comes from tests/conftest.py
(isolated per test), so the real ~/.claude is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agora.agents import claude as C
from agora.agents.base import AgentError

FIX = Path(__file__).parent.parent / "fixtures" / "claude"
FAKE = Path(__file__).parent.parent / "fakes" / "fake_claude.py"
SID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture()
def claude_env(tmp_path, monkeypatch):
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    proj = home / ".claude" / "projects" / "-tmp-my-proj-v2"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "cl-basic.jsonl", proj / f"{SID}.jsonl")
    aux_src = FIX / "cl-aux" / SID
    if aux_src.is_dir():
        shutil.copytree(aux_src, proj / SID)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(fake_home))
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(FAKE))
    st = os.stat(FAKE)
    os.chmod(FAKE, st.st_mode | stat.S_IEXEC)
    return {"home": home, "proj": proj, "fake_home": fake_home}


def read_main(exported) -> tuple[list[str], dict]:
    doc = json.loads(exported.raw.decode("utf-8"))
    return doc["main"], doc["aux"]


# --- encode rule -----------------------------------------------------------

@pytest.mark.parametrize(("path", "want"), [
    ("/tmp/my-proj.v2", "-tmp-my-proj-v2"),
    ("/private/tmp/agora-spike-impl2/proj", "-private-tmp-agora-spike-impl2-proj"),
    ("/a.b/c.d.e/f", "-a-b-c-d-e-f"),
])
def test_encode_project_dir(path, want):
    assert C.encode_project_dir(path) == want


# --- export ----------------------------------------------------------------

def test_export_basic(claude_env):
    e = C.ADAPTER.export(SID)
    assert e.session_id == SID
    assert e.dir == "/tmp/my-proj.v2"  # from jsonl cwd, not the dir name (N9)
    assert e.title == "CSV 轉 Markdown 的規劃"  # summary line wins
    assert e.created_at == "2026-10-02T01:00:00.000Z"  # first line timestamp
    assert e.agent_version == "9.9.9"
    assert e.message_count == 5  # 2 user + 3 assistant
    main, aux = read_main(e)
    assert len(main) == 11
    assert sorted(aux) == ["subagents/agent-0123456789abcdef.jsonl",
                           "subagents/agent-0123456789abcdef.meta.json"]
    assert all(json.loads(line)["sessionId"] == SID for line in main)


def test_export_title_falls_back_to_first_user_text(claude_env):
    proj = claude_env["proj"]
    lines = (proj / f"{SID}.jsonl").read_text().splitlines()
    lines = [line for line in lines if json.loads(line).get("type") != "summary"]
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    e = C.ADAPTER.export(SID)
    assert e.title == "把 CSV 轉成 Markdown 表格，先列三個步驟"
    long_title = "字" * 100
    lines[0] = json.dumps({**json.loads(lines[0]),
                           "message": {"role": "user", "content": long_title}})
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    assert C.ADAPTER.export(SID).title == "字" * 60


def test_export_partial_last_line_dropped(claude_env, capsys):
    proj = claude_env["proj"]
    shutil.copy(FIX / "cl-partial.jsonl", proj / f"{SID}.jsonl")
    e = C.ADAPTER.export(SID)
    main, _aux = read_main(e)
    assert len(main) == 11  # the half-written 12th line is gone (S10)
    assert e.message_count == 5
    assert "最後一行" in capsys.readouterr().err


def test_export_missing_raises(claude_env):
    with pytest.raises(AgentError):
        C.ADAPTER.export("00000000-0000-4000-8000-000000000000")


def test_export_broken_cli_gives_no_version(claude_env, monkeypatch):
    monkeypatch.setenv("AGORA_CLAUDE_CMD", "/nonexistent/zz-claude")
    assert C.ADAPTER.export(SID).agent_version is None


# --- reading (U-RV-02) -------------------------------------------------------

def test_reading_basic(claude_env):
    body = C.ADAPTER.reading(C.ADAPTER.export(SID).raw)
    assert "把 CSV 轉成 Markdown 表格" in body
    assert "三個步驟" in body
    assert "[tool] Bash" in body
    assert "ZZTOOLOUT" not in body  # tool results are skipped
    assert "ZZTHINK" not in body  # thinking is skipped
    assert "[skip" not in body  # summary/meta/bookkeeping are silent


def test_reading_truncates_long_tool_input(claude_env):
    body = C.ADAPTER.reading(C.ADAPTER.export(SID).raw)
    long_line = next(line for line in body.splitlines()
                     if line.startswith("[tool] Bash") and "xxx" in line)
    assert long_line.endswith("…")
    assert len(long_line.split(" ", 2)[2]) <= 201


def test_reading_unknown_type_is_skipped_explicitly():
    raw = json.dumps({
        "format": C.FORMAT,
        "main": ['{"type": "user", "sessionId": "s", "message": "hi"}',
                 '{"type": "zzwidget", "sessionId": "s"}'],
        "aux": {},
    }).encode()
    assert "[skip zzwidget]" in C.ADAPTER.reading(raw)


# --- start_native / collect (U-CON-04, U-CON-05) ------------------------------

def test_start_native_rewrites_ids_and_cwd(claude_env, tmp_path):
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    raw = C.ADAPTER.export(SID).raw
    launch = C.ADAPTER.start_native(raw, workdir)
    assert launch.argv[0] == str(FAKE)
    assert launch.argv[1] == "--resume"
    new_id = launch.agent_session_id
    assert new_id and new_id != SID
    uuid.UUID(new_id)
    assert launch.cwd == str(workdir)
    assert launch.before_count == 5
    new_file = (claude_env["home"] / ".claude" / "projects"
                / C.encode_project_dir(workdir) / f"{new_id}.jsonl")
    assert new_file.is_file()
    lines = new_file.read_text().splitlines()
    assert len(lines) == 11
    assert all(json.loads(line)["sessionId"] == new_id for line in lines)
    assert {json.loads(line).get("cwd") for line in lines} == {str(workdir)}
    aux_file = (claude_env["home"] / ".claude" / "projects"
                / C.encode_project_dir(workdir) / new_id
                / "subagents" / "agent-0123456789abcdef.jsonl")
    assert aux_file.is_file()
    assert all(json.loads(line)["sessionId"] == new_id
               for line in aux_file.read_text().splitlines())


def test_collect_no_new_content_returns_none(claude_env, tmp_path):
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    launch = C.ADAPTER.start_native(C.ADAPTER.export(SID).raw, workdir)
    assert C.ADAPTER.collect(launch) is None


def test_collect_after_fake_resume(claude_env, tmp_path):
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    launch = C.ADAPTER.start_native(C.ADAPTER.export(SID).raw, workdir)
    env = {**os.environ, "FAKE_AGENT_MODE": "append"}
    proc = subprocess.run([str(FAKE), "--resume", launch.agent_session_id,
                           "-p", "ZZSAY 再補一句"],
                          cwd=str(workdir), env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    got = C.ADAPTER.collect(launch)
    assert got is not None
    assert got.session_id == launch.agent_session_id
    assert got.message_count == 7
    main, _aux = read_main(got)
    assert len(main) == 13
    assert "ZZSAY" in got.raw.decode("utf-8")


def test_collect_missing_session_raises(claude_env, tmp_path):
    from agora.agents.base import Launch
    with pytest.raises(AgentError):
        C.ADAPTER.collect(Launch(argv=[], cwd=str(tmp_path),
                                 agent_session_id=str(uuid.uuid4()), before_count=0))


# --- start_injected ------------------------------------------------------------

def test_start_injected(claude_env, tmp_path):
    reading = tmp_path / "reading.md"
    reading.write_text("# from agora:xxx\n\n測試\n", encoding="utf-8")
    workdir = tmp_path / "proj"
    workdir.mkdir()
    launch = C.ADAPTER.start_injected(reading, workdir)
    assert launch.argv[0] == str(FAKE)
    assert launch.argv[1] == "--session-id"
    uuid.UUID(launch.argv[2])
    assert launch.argv[2] == launch.agent_session_id
    assert launch.argv[3].startswith(f"@{reading.resolve()} ")
    assert launch.cwd == str(workdir)
    assert launch.before_count == 0


# --- adapter surface -------------------------------------------------------------

def test_adapter_surface():
    assert C.ADAPTER.name == "claude"
    assert hasattr(C.ADAPTER, "export")
    assert hasattr(C.ADAPTER, "reading")
    assert hasattr(C.ADAPTER, "start_native")
    assert hasattr(C.ADAPTER, "start_injected")
    assert hasattr(C.ADAPTER, "collect")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
