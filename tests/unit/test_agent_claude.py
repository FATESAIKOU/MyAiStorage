"""Unit tests for the Claude Code adapter (test-plan: U-RV-02, U-IMP-04,
U-IMP-05, U-CON-04/05, encode rule, collect-None; review CL1-CL14).

Fixtures are hand-written and self-made (test-plan 0.3); fake_claude covers
--version / --resume, launched through a sh wrapper so the invocation does
not depend on the user's python3 shim (CL3). AGORA_CLAUDE_HOME comes from
tests/conftest.py (isolated per test) and CLAUDE_CONFIG_DIR is always
removed, so the real ~/.claude is never touched (CL4).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agora.agents import claude as C
from agora.agents.base import AgentError, Launch

FIX = Path(__file__).parent.parent / "fixtures" / "claude"
FAKE = Path(__file__).parent.parent / "fakes" / "fake_claude.py"
SID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def no_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


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
    wrapper = tmp_path / "claude"  # CL3: sh wrapper, no shebang roulette
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_HOME", str(fake_home))
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(wrapper))
    return {"home": home, "proj": proj, "fake_home": fake_home,
            "wrapper": wrapper}


def read_main(exported) -> tuple[list[str], dict]:
    doc = json.loads(exported.raw.decode("utf-8"))
    return doc["main"], doc["aux"]


# --- encode rule (CL1) -------------------------------------------------------

@pytest.mark.parametrize(("path", "want"), [
    ("/tmp/my-proj.v2", "-tmp-my-proj-v2"),
    ("/private/tmp/agora-spike-impl2/proj", "-private-tmp-agora-spike-impl2-proj"),
    ("/a.b/c.d.e/f", "-a-b-c-d-e-f"),
    ("/tmp/a_b 專案.v2", "-tmp-a-b----v2"),  # _, space, CJK, . each -> one -
])
def test_encode_project_dir(path, want):
    assert C.encode_project_dir(path) == want


# --- home priority (CL4) -----------------------------------------------------

def test_home_priority_agora_first(tmp_path, monkeypatch):
    assert C.projects_dir().name == "projects"
    assert C.projects_dir().parent == Path(os.environ["AGORA_CLAUDE_HOME"]) / ".claude"


def test_home_priority_config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("AGORA_CLAUDE_HOME")
    cfg = tmp_path / "cfg"
    proj = cfg / "projects" / "-tmp-my-proj-v2"
    proj.mkdir(parents=True)
    shutil.copy(FIX / "cl-basic.jsonl", proj / f"{SID}.jsonl")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AGORA_CLAUDE_CMD", "/nonexistent/zz-claude")
    e = C.ADAPTER.export(SID)
    assert e.session_id == SID
    assert e.dir == "/tmp/my-proj.v2"


# --- export ------------------------------------------------------------------

def test_export_model_is_the_last_assistant_one(claude_env):  # design v4 source.model
    assert C.ADAPTER.export(SID).model == "zz-model"
    proj = claude_env["proj"]
    lines = [json.loads(line) for line in (proj / f"{SID}.jsonl").read_text().splitlines()]
    assistants = [o for o in lines if o.get("type") == "assistant"]
    last = assistants[-1]
    last["message"]["model"] = "zz-model-newer"
    (proj / f"{SID}.jsonl").write_text(
        "\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n")
    assert C.ADAPTER.export(SID).model == "zz-model-newer"


def test_export_model_none_when_no_model_field(claude_env):
    proj = claude_env["proj"]
    lines = []
    for line in (proj / f"{SID}.jsonl").read_text().splitlines():
        o = json.loads(line)
        if o.get("type") == "assistant":
            o["message"].pop("model", None)
        lines.append(json.dumps(o, ensure_ascii=False))
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    assert C.ADAPTER.export(SID).model is None


def test_export_basic(claude_env):
    e = C.ADAPTER.export(SID)
    assert e.session_id == SID
    assert e.dir == "/tmp/my-proj.v2"  # from jsonl cwd, not the dir name (N9)
    assert e.title == "CSV 轉 Markdown 的規劃"  # summary line wins
    assert e.created_at == "2026-10-02T01:00:00.000Z"  # first present timestamp
    assert e.agent_version == "2.1.286"  # last present version field, no CLI call
    assert e.message_count == 6  # 3 user + 3 assistant; local/meta noise excluded
    main, aux = read_main(e)
    assert len(main) == 16
    assert sorted(aux) == ["subagents/agent-0123456789abcdef.jsonl",
                           "subagents/agent-0123456789abcdef.meta.json"]
    assert all(json.loads(line)["sessionId"] == SID for line in main)


def test_export_hint_dir(claude_env):
    e = C.ADAPTER.export(SID, hint_dir="/tmp/my-proj.v2")
    assert e.session_id == SID and e.message_count == 6
    # A wrong hint falls back to the glob and still finds it.
    assert C.ADAPTER.export(SID, hint_dir="/tmp/nowhere").session_id == SID
    with pytest.raises(AgentError):
        C.ADAPTER.export("00000000-0000-4000-8000-000000000000",
                         hint_dir="/tmp/nowhere")


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


def test_export_created_at_skips_lines_without_timestamp(claude_env):
    proj = claude_env["proj"]
    lines = (proj / f"{SID}.jsonl").read_text().splitlines()
    first = json.loads(lines[0])
    del first["timestamp"]
    lines.insert(0, json.dumps({"type": "summary", "sessionId": SID}))
    lines[1] = json.dumps(first)
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    # First line (summary) and second (user) have no timestamp: the
    # attachment's timestamp wins.
    assert C.ADAPTER.export(SID).created_at == "2026-10-02T01:00:01.000Z"


def test_export_version_from_last_line_no_cli(claude_env, monkeypatch):
    # P3: agent_version comes from the jsonl; the CLI is never called.
    monkeypatch.setenv("AGORA_CLAUDE_CMD", "/nonexistent/zz-claude")
    assert C.ADAPTER.export(SID).agent_version == "2.1.286"


def test_export_partial_last_line_dropped(claude_env, capsys):
    proj = claude_env["proj"]
    shutil.copy(FIX / "cl-partial.jsonl", proj / f"{SID}.jsonl")
    e = C.ADAPTER.export(SID)
    main, _aux = read_main(e)
    assert len(main) == 16  # the half-written 17th line is gone (S10)
    assert e.message_count == 6
    assert "最後一行" in capsys.readouterr().err


def test_export_blank_lines_skipped_and_binary_rejected(claude_env, tmp_path):
    proj = claude_env["proj"]
    text = (proj / f"{SID}.jsonl").read_text()
    (proj / f"{SID}.jsonl").write_text(text + "\n   \n", encoding="utf-8")
    assert C.ADAPTER.export(SID).message_count == 6  # CL8: blanks skipped
    (proj / f"{SID}.jsonl").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(AgentError):
        C.ADAPTER.export(SID)


def test_export_missing_raises(claude_env):
    with pytest.raises(AgentError):
        C.ADAPTER.export("00000000-0000-4000-8000-000000000000")


def test_export_no_version_anywhere_gives_none(claude_env):
    proj = claude_env["proj"]
    lines = [json.dumps({k: v for k, v in json.loads(line).items() if k != "version"})
             for line in (proj / f"{SID}.jsonl").read_text().splitlines()]
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    assert C.ADAPTER.export(SID).agent_version is None


# --- reading (U-RV-02, CL2, CL10) ----------------------------------------------

def test_reading_basic(claude_env):
    body = C.ADAPTER.reading(C.ADAPTER.export(SID).raw)
    assert "把 CSV 轉成 Markdown 表格" in body
    assert "三個步驟" in body
    assert "[tool] Bash" in body
    assert "[tool] WebSearch" in body  # server_tool_use is still a tool call
    assert "ZZTOOLOUT" not in body  # tool results are skipped
    assert "ZZWEBOUT" not in body
    assert "ZZTHINK" not in body  # thinking is skipped
    assert "command-name" not in body  # CL2: local commands are noise
    assert "Caveat" not in body  # CL2: isMeta lines are noise
    assert "[skip" not in body  # summary/system/meta/bookkeeping are silent
    assert "之前在做表格轉換的規劃" in body  # isCompactSummary is kept


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
                 '{"type": "zzwidget", "sessionId": "s"}',
                 '{"type": "assistant", "sessionId": "s", "message": {"content": [{"text": "x"}]}}'],
        "aux": {},
    }).encode()
    body = C.ADAPTER.reading(raw)
    assert "[skip zzwidget]" in body
    assert "[skip ]" in body  # CL10: a block without type must not crash


# --- start_native / collect (U-CON-04, U-CON-05) -------------------------------

def test_start_native_rewrites_ids_and_cwd(claude_env, tmp_path):
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    raw = C.ADAPTER.export(SID).raw
    launch = C.ADAPTER.start_native(raw, workdir)
    assert launch.argv[0] == str(claude_env["wrapper"])
    assert launch.argv[1] == "--resume"
    new_id = launch.agent_session_id
    assert new_id and new_id != SID
    uuid.UUID(new_id)
    assert launch.cwd == str(workdir)
    assert launch.before_count == 6
    new_file = (claude_env["home"] / ".claude" / "projects"
                / C.encode_project_dir(workdir) / f"{new_id}.jsonl")
    assert new_file.is_file()
    lines = new_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    assert all(json.loads(line)["sessionId"] == new_id for line in lines)
    with_cwd = [json.loads(line) for line in lines if "cwd" in json.loads(line)]
    assert with_cwd and all(o["cwd"] == str(workdir) for o in with_cwd)
    # CL7: lines that had no cwd gain none; Chinese stays literal (CL7).
    assert all("cwd" in json.loads(line) for line in lines
               if json.loads(line).get("type") in ("user", "assistant"))
    text = new_file.read_text(encoding="utf-8")
    assert "表格" in text and "\\u" not in text
    aux_file = (claude_env["home"] / ".claude" / "projects"
                / C.encode_project_dir(workdir) / new_id
                / "subagents" / "agent-0123456789abcdef.jsonl")
    assert aux_file.is_file()
    assert all(json.loads(line)["sessionId"] == new_id
               for line in aux_file.read_text(encoding="utf-8").splitlines())


def test_start_native_aux_partial_last_line(claude_env, tmp_path, capsys):
    sub = "subagents/agent-0123456789abcdef.jsonl"
    main = ['{"type": "user", "sessionId": "s", "message": "hi", "cwd": "/tmp/x"}']
    aux = {sub: '{"type": "user", "sessionId": "s"}\n{"type": "assi'}
    raw = json.dumps({"format": C.FORMAT, "main": main, "aux": aux}).encode()
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    launch = C.ADAPTER.start_native(raw, workdir)
    got = (claude_env["home"] / ".claude" / "projects"
           / C.encode_project_dir(workdir) / launch.agent_session_id
           / sub).read_text(encoding="utf-8")
    assert json.loads(got)["sessionId"] == launch.agent_session_id
    assert "最後一行" in capsys.readouterr().err


def test_start_native_rejects_aux_escape(claude_env, tmp_path):
    raw = json.dumps({"format": C.FORMAT, "main": [], "aux": {"../x": "y"}}).encode()
    with pytest.raises(AgentError):
        C.ADAPTER.start_native(raw, tmp_path)


def test_aux_binary_round_trip(claude_env, tmp_path):
    blob = bytes(range(256))
    raw = json.dumps({"format": C.FORMAT, "main": [],
                      "aux": {"bin/data": {"$base64": base64.b64encode(blob).decode()}}})
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    launch = C.ADAPTER.start_native(raw.encode(), workdir)
    got = (claude_env["home"] / ".claude" / "projects"
           / C.encode_project_dir(workdir) / launch.agent_session_id
           / "bin" / "data").read_bytes()
    assert got == blob


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
    proc = subprocess.run([str(claude_env["wrapper"]), "--resume",
                           launch.agent_session_id, "-p", "ZZSAY 再補一句"],
                          cwd=str(workdir), env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    got = C.ADAPTER.collect(launch)
    assert got is not None
    assert got.session_id == launch.agent_session_id
    assert got.message_count == 8
    assert got.model == "zz-fake-model"  # the turn the agent just made
    main, _aux = read_main(got)
    assert len(main) == 18
    assert "ZZSAY" in got.raw.decode("utf-8")


def test_collect_missing_session_raises(claude_env, tmp_path):
    with pytest.raises(AgentError):
        C.ADAPTER.collect(Launch(argv=[], cwd=str(tmp_path),
                                 agent_session_id=str(uuid.uuid4()), before_count=0))


# --- start_injected ------------------------------------------------------------

def test_start_injected(claude_env, tmp_path):
    reading = tmp_path / "reading.md"
    reading.write_text("# from agora:xxx\n\n測試\n", encoding="utf-8")
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    launch = C.ADAPTER.start_injected(reading, workdir)
    assert launch.argv[0] == str(claude_env["wrapper"])
    assert launch.argv[1] == "--session-id"
    uuid.UUID(launch.argv[2])
    assert launch.argv[2] == launch.agent_session_id
    assert launch.argv[3].startswith(f"@{reading.resolve()} ")
    assert launch.cwd == str(workdir)
    assert launch.before_count == 2  # CL9: prompt + auto-reply already count


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
