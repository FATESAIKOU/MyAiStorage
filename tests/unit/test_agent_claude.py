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
from agora.agents import base
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


# --- turns (U-RV-02, CL2, CL10) ------------------------------------------------

def body_of(claude_env, raw=None):
    from agora.agents.base import format_reading
    return format_reading(C.ADAPTER.turns(raw if raw is not None
                                          else C.ADAPTER.export(SID).raw))


def test_turns_basic(claude_env):
    body = body_of(claude_env)
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


def test_turns_truncates_long_tool_input(claude_env):
    body = body_of(claude_env)
    long_line = next(line for line in body.splitlines()
                     if line.startswith("[tool] Bash") and "xxx" in line)
    assert long_line.endswith("…")
    assert len(long_line.split(" ", 2)[2]) <= 201


def test_turns_unknown_type_is_skipped_explicitly():
    raw = json.dumps({
        "format": C.FORMAT,
        "main": ['{"type": "user", "sessionId": "s", "message": "hi"}',
                 '{"type": "zzwidget", "sessionId": "s"}',
                 '{"type": "assistant", "sessionId": "s", "message": {"content": [{"text": "x"}]}}'],
        "aux": {},
    }).encode()
    turns = C.ADAPTER.turns(raw)
    assert ("user", ["[skip zzwidget]"]) in turns
    assert any("[skip ]" in line for _role, lines in turns for line in lines)  # CL10


# --- native() round-trip (design v5 5.4) ---------------------------------------

def test_native_round_trip_keeps_the_text(claude_env):
    """turns -> native -> turns: identical turns come back.

    native() writes one line per turn in order, so even the grouping survives.
    """
    raw = C.ADAPTER.export(SID).raw
    turns = C.ADAPTER.turns(raw)
    assert turns, "the fixture has turns"
    again = C.ADAPTER.turns(C.ADAPTER.native(turns))
    assert again == turns
    assert base.format_reading(again) == base.format_reading(turns)


def test_native_builds_a_loadable_jsonl(claude_env, tmp_path):
    turns = [("user", ["把 CSV 轉成 Markdown 表格，先列三個步驟"]),
             ("assistant", ["1. 讀檔", "2. 組表頭", "3. 輸出資料列"])]
    raw = C.ADAPTER.native(turns)
    doc = json.loads(raw.decode("utf-8"))
    assert doc["format"] == C.FORMAT and doc["aux"] == {}
    lines = [json.loads(line) for line in doc["main"]]
    assert [o["type"] for o in lines] == ["user", "assistant"]  # alternating
    assert lines[0]["parentUuid"] is None
    assert lines[1]["parentUuid"] == lines[0]["uuid"]           # chained
    assert all(o["sessionId"] == C.PLACEHOLDER_ID for o in lines)
    assert all(o["cwd"] == C.PLACEHOLDER_CWD for o in lines)
    stamps = [o["timestamp"] for o in lines]
    assert stamps == sorted(stamps) and len(set(stamps)) == 2
    # Text survives as-is (no tool lines invented, nothing trimmed).
    assert C.ADAPTER.turns(raw) == turns
    assert base.format_reading(C.ADAPTER.turns(raw)) == base.format_reading(turns)

    # And start_native loads it: no sessionId/cwd placeholder survives.
    workdir = (tmp_path / "proj").resolve()
    workdir.mkdir()
    launch = C.ADAPTER.start_native(raw, workdir)
    written = [json.loads(line) for line in
               (claude_env["home"] / ".claude" / "projects"
                / C.encode_project_dir(workdir) / f"{launch.agent_session_id}.jsonl"
                ).read_text(encoding="utf-8").splitlines()]
    assert [o["sessionId"] for o in written] == [launch.agent_session_id] * 2
    assert [o["cwd"] for o in written] == [str(workdir)] * 2
    assert launch.before_count == 2


def test_native_writes_the_given_turns_in_order(claude_env):
    """cli guarantees user-first and strictly alternating turns; we keep the order."""
    turns = [("user", ["一句話", "同一則的第二句"]),
             ("assistant", ["回應一"]), ("user", ["追問"]), ("assistant", ["回應二"])]
    lines = [json.loads(line) for line
             in json.loads(C.ADAPTER.native(turns).decode("utf-8"))["main"]]
    assert [o["type"] for o in lines] == ["user", "assistant", "user", "assistant"]
    assert [b["text"] for b in lines[0]["message"]["content"]] == ["一句話", "同一則的第二句"]
    assert C.ADAPTER.turns(C.ADAPTER.native(turns)) == turns


def test_native_of_empty_turns_is_still_valid(claude_env):
    doc = json.loads(C.ADAPTER.native([]).decode("utf-8"))
    assert doc["main"] == [] and C.ADAPTER.turns(C.ADAPTER.native([])) == []


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


# --- summarize (design 5.3, review Y3) ------------------------------------------

def test_summarize_returns_text_and_model(claude_env, tmp_path):
    workdir = (tmp_path / "sum").resolve()
    workdir.mkdir()
    prompt = "請把下面兩段對話寫成三句話的摘要：\n\nA：規劃表格\nB：開始實作"
    text, model = C.ADAPTER.summarize(prompt, workdir)
    assert text == "ZZSUM 這是自編的要約"
    assert model == "zz-model"


def test_summarize_passes_the_prompt_on_stdin_and_blocks_tools(claude_env, tmp_path, monkeypatch):
    workdir = (tmp_path / "sum").resolve()
    workdir.mkdir()
    monkeypatch.setenv("FAKE_SUMMARIZE_MODE", "ok")
    C.ADAPTER.summarize("很長的材料" * 100, workdir)
    assert (claude_env["fake_home"] / "claude-stdin.log").read_text().strip() == "很長的材料" * 100
    argv = json.loads((claude_env["fake_home"] / "claude-args.log").read_text().splitlines()[-1])
    for flag, value in [("-p", None), ("--tools", ""), ("--strict-mcp-config", None),
                        ("--setting-sources", ""), ("--no-session-persistence", None)]:
        assert flag in argv, flag
        if value is not None:
            assert argv[argv.index(flag) + 1] == value
    # the prompt is never a command-line argument
    assert not any("很長的材料" in a for a in argv)


def test_summarize_runs_in_the_given_workdir(claude_env, tmp_path):
    workdir = (tmp_path / "sum").resolve()
    workdir.mkdir()
    C.ADAPTER.summarize("材料", workdir)
    assert (claude_env["fake_home"] / "claude-cwd.log").read_text().splitlines()[-1] == str(workdir)


def test_summarize_timeout_is_configurable(claude_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_SUMMARIZE_TIMEOUT", "12")
    assert C._summarize_timeout() == 12.0
    monkeypatch.delenv("AGORA_SUMMARIZE_TIMEOUT")
    assert C._summarize_timeout() == 600.0


def test_summarize_failures_raise_agent_error(claude_env, tmp_path, monkeypatch):
    workdir = (tmp_path / "sum").resolve()
    workdir.mkdir()
    for mode in ("fail", "empty"):
        monkeypatch.setenv("FAKE_SUMMARIZE_MODE", mode)
        with pytest.raises(AgentError):
            C.ADAPTER.summarize("材料", workdir)
    monkeypatch.setenv("FAKE_CLAUDE_CMD", "/nonexistent/zz-claude")
    with pytest.raises(AgentError):
        C.ADAPTER.summarize("材料", workdir)


def test_summarize_asks_for_no_session_file(claude_env, tmp_path):
    """--no-session-persistence is what keeps projects/ clean (Y3); the real
    check that nothing is written lives in tests/integration/test_claude_summarize.py."""
    workdir = (tmp_path / "sum").resolve()
    workdir.mkdir()
    C.ADAPTER.summarize("材料", workdir)
    argv = json.loads((claude_env["fake_home"] / "claude-args.log").read_text().splitlines()[-1])
    assert "--no-session-persistence" in argv


def test_summarize_falls_back_to_plain_stdout(claude_env, tmp_path, monkeypatch):
    """A CLI that prints text instead of JSON still gives us the summary."""
    workdir = (tmp_path / "sum").resolve()
    workdir.mkdir()
    monkeypatch.setenv("FAKE_SUMMARIZE_MODE", "garbage")
    text, model = C.ADAPTER.summarize("材料", workdir)
    assert text == "not json at all"
    assert model is None


# --- list_sessions / last_message (design 5.9, interactive mode) ----------------

def test_list_sessions_reads_dir_title_and_stamp(claude_env):
    listed = C.ADAPTER.list_sessions()
    assert [item.session_id for item in listed] == [SID]
    item = listed[0]
    assert item.dir == "/tmp/my-proj.v2"          # from the jsonl's cwd
    assert item.title == "把 CSV 轉成 Markdown 表格，先列三個步驟"
    # updated_at is the file's mtime (T15: no full scan for the last stamp)
    assert item.updated_at and item.updated_at.endswith("Z") and "T" in item.updated_at


def test_list_sessions_skips_files_without_messages(claude_env, tmp_path):
    """What /clear leaves behind: attachment lines only, no conversation."""
    proj = claude_env["proj"]
    empty = "22222222-2222-4333-8444-555555555555"
    (proj / f"{empty}.jsonl").write_text(
        '{"type": "attachment", "sessionId": "%s", "cwd": "/tmp/x"}\n' % empty)
    meta_only = "33333333-2222-4333-8444-555555555555"
    (proj / f"{meta_only}.jsonl").write_text(
        '{"type": "user", "sessionId": "%s", "isMeta": true, "message": "Caveat"}\n' % meta_only)
    assert [i.session_id for i in C.ADAPTER.list_sessions()] == [SID]


def test_list_sessions_covers_every_project_and_sorts_newest_first(claude_env, tmp_path):
    proj = claude_env["proj"]
    other = proj.parent / "-tmp-other-proj"
    other.mkdir()
    lines = (proj / f"{SID}.jsonl").read_text().splitlines()
    older = [json.dumps({**json.loads(l), "timestamp": "2026-09-01T00:00:00.000Z"})
             for l in lines]
    (other / "44444444-2222-4333-8444-555555555555.jsonl").write_text(
        "\n".join(older) + "\n", encoding="utf-8")
    listed = C.ADAPTER.list_sessions()
    assert {i.session_id for i in listed} == {SID, "44444444-2222-4333-8444-555555555555"}
    stamps = [i.updated_at for i in listed]
    assert stamps == sorted(stamps, reverse=True)      # newest first
    assert listed[0].session_id == SID                  # this one was written last


def test_list_sessions_falls_back_to_the_file_time(claude_env):
    (claude_env["proj"] / f"{SID}.jsonl").write_text(
        '{"type": "user", "sessionId": "%s", "cwd": "/tmp/x", "message": "只有一句"}\n' % SID,
        encoding="utf-8")
    item = C.ADAPTER.list_sessions()[0]
    assert item.updated_at and item.updated_at.endswith("Z") and "T" in item.updated_at
    assert item.title == "只有一句"


def test_list_sessions_tolerates_a_half_written_last_line(claude_env):
    proj = claude_env["proj"]
    text = (proj / f"{SID}.jsonl").read_text()
    (proj / f"{SID}.jsonl").write_text(text + '{"type": "user", "mess', encoding="utf-8")
    assert [i.session_id for i in C.ADAPTER.list_sessions()] == [SID]


def test_list_sessions_when_there_are_none(claude_env):
    shutil.rmtree(claude_env["home"] / ".claude")
    assert C.ADAPTER.list_sessions() == []


def test_last_message_returns_the_last_real_one(claude_env):
    role, text = C.ADAPTER.last_message(SID)
    # the fixture ends with local-command, isMeta, system and a compact summary
    assert (role, text) == ("user", "之前在做表格轉換的規劃")


def test_last_message_skips_noise_tail(claude_env):
    proj = claude_env["proj"]
    tail = [
        {"type": "user", "sessionId": SID, "message": "<command-name>/exit</command-name>"},
        {"type": "assistant", "sessionId": SID, "message": {"content": [{"type": "text", "text": "最後一句回覆"}]}},
        {"type": "user", "sessionId": SID, "isMeta": True, "message": "Caveat: 系統"},
    ]
    with (proj / f"{SID}.jsonl").open("a", encoding="utf-8") as f:
        for o in tail:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    assert C.ADAPTER.last_message(SID) == ("assistant", "最後一句回覆")


def test_last_message_none_when_no_real_message(claude_env):
    only_meta = "55555555-2222-4333-8444-555555555555"
    (claude_env["proj"] / f"{only_meta}.jsonl").write_text(
        '{"type": "user", "sessionId": "%s", "isMeta": true, "message": "Caveat"}\n' % only_meta,
        encoding="utf-8")
    assert C.ADAPTER.last_message(only_meta) is None
    assert C.ADAPTER.last_message("66666666-2222-4333-8444-555555555555") is None  # T15: no raise


def test_last_message_caps_the_text(claude_env):
    long_text = "字" * 5000
    sid = "77777777-2222-4333-8444-555555555555"
    (claude_env["proj"] / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": sid, "cwd": "/tmp/x",
                    "message": {"role": "user", "content": [{"type": "text", "text": long_text}]}},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    role, text = C.ADAPTER.last_message(sid)
    assert role == "user"
    assert len(text) == C.PREVIEW_MAX <= 2000
    assert text == long_text[-C.PREVIEW_MAX:]


def test_last_message_reads_only_the_tail(claude_env, monkeypatch):
    """A huge file must not be read whole (review T15)."""
    sid = "88888888-2222-4333-8444-555555555555"
    path = claude_env["proj"] / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "sessionId": sid, "cwd": "/tmp/x",
                            "message": {"role": "user", "content": "開頭那句"}},
                           ensure_ascii=False) + "\n")
        for _ in range(4000):
            f.write(json.dumps({"type": "user", "sessionId": sid,
                                "message": {"role": "user", "content": "填充" * 200}},
                               ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "user", "sessionId": sid, "cwd": "/tmp/x",
                            "message": {"role": "user", "content": "結尾那句"}},
                           ensure_ascii=False) + "\n")
    assert path.stat().st_size > C.TAIL_BYTES
    seen = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        if self == path:
            seen.append(kwargs.get("mode", args[0] if args else "r"))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    assert C.ADAPTER.last_message(sid) == ("user", "結尾那句")
    assert seen == ["rb"], seen          # only the binary tail read


def test_last_message_ignores_a_half_written_tail(claude_env):
    sid = "99999999-2222-4333-8444-555555555555"
    good = json.dumps({"type": "user", "sessionId": sid, "cwd": "/tmp/x",
                       "message": {"role": "user", "content": "完整那句"}}, ensure_ascii=False)
    (claude_env["proj"] / f"{sid}.jsonl").write_text(
        good + "\n" + '{"type": "user", "message": "半句', encoding="utf-8")
    assert C.ADAPTER.last_message(sid) == ("user", "完整那句")


def test_list_sessions_does_not_reread_an_unchanged_file(claude_env):
    C._LIST_CACHE.clear()
    calls = []
    real = C._peek_session

    def counted(path):
        calls.append(path)
        return real(path)

    import unittest.mock
    with unittest.mock.patch.object(C, "_peek_session", counted):
        first = C.ADAPTER.list_sessions()
        assert len(calls) == 1
        second = C.ADAPTER.list_sessions()
        assert len(calls) == 1, "an unchanged file should come from the cache"
        assert [i.session_id for i in first] == [i.session_id for i in second]
    path = claude_env["proj"] / f"{SID}.jsonl"
    path.write_text(path.read_text() + "\n", encoding="utf-8")   # size changed
    with unittest.mock.patch.object(C, "_peek_session", counted):
        C.ADAPTER.list_sessions()
    assert len(calls) == 2


def test_list_sessions_only_reads_the_head(claude_env):
    sid = "aaaaaaaa-2222-4333-8444-555555555555"
    path = claude_env["proj"] / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i in range(300):        # the title lives in line 1
            f.write(json.dumps({"type": "user", "sessionId": sid, "cwd": "/tmp/deep",
                                "message": {"role": "user", "content": f"第{i}句"}},
                               ensure_ascii=False) + "\n")
    listed = {i.session_id: i for i in C.ADAPTER.list_sessions()}
    assert listed[sid].title == "第0句"          # read from the head, not the tail
    assert listed[sid].dir == "/tmp/deep"


def test_config_dir_follows_xdg_when_nothing_else_is_set(monkeypatch):
    monkeypatch.delenv("AGORA_CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/zz-xdg")
    assert C.config_dir() == Path("/tmp/zz-xdg") / "claude"
    assert C.projects_dir() == Path("/tmp/zz-xdg") / "claude" / "projects"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert C.config_dir() == Path(os.path.expanduser("~")) / ".claude"


def test_list_and_last_message_survive_unreadable_files(claude_env):
    broken = claude_env["proj"] / "bbbbbbbb-2222-4333-8444-555555555555.jsonl"
    broken.write_bytes(b"\xff\xfe not utf-8 but text-replaceable")
    assert C.ADAPTER.list_sessions()                        # no raise, still lists
    assert C.ADAPTER.last_message("cccccccc-2222-4333-8444-555555555555") is None


# --- adapter surface -------------------------------------------------------------

def test_adapter_surface():
    assert C.ADAPTER.name == "claude"
    assert hasattr(C.ADAPTER, "export")
    assert hasattr(C.ADAPTER, "summarize")
    assert hasattr(C.ADAPTER, "list_sessions")
    assert hasattr(C.ADAPTER, "last_message")
    assert hasattr(C.ADAPTER, "turns")
    assert hasattr(C.ADAPTER, "native")
    assert hasattr(C.ADAPTER, "start_native")
    assert hasattr(C.ADAPTER, "collect")
    assert not hasattr(C.ADAPTER, "reading")          # gone in design v5
    assert not hasattr(C.ADAPTER, "start_injected")   # gone in design v5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
