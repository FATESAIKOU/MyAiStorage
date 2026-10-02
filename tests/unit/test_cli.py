"""The agora command over a fake rclone and a fake agent (test-plan U-CLI)."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h
from agora.agents.base import AgentError, Exported, Launch, format_reading

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


class FakeAgent:
    """Stores sessions in memory; launching 'talks' by appending a message."""

    name = "opencode"

    def __init__(self):
        self.sessions: dict[str, list[str]] = {"ses_a": ["把 CSV 轉成 Markdown 表格", "好的，三個步驟"]}
        self.launched: list[Launch] = []
        self.speak = True
        self.prompts: list = []
        self.answers: list = []          # canned summarize answers, used first

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

    def summarize(self, prompt, workdir):
        self.prompts.append((prompt, workdir))
        if self.answers:
            return self.answers.pop(0), "fake-model"
        first = prompt.split("以下是 Session 的內容：", 1)[1].strip().splitlines()
        return "```json\n" + json.dumps({"purpose": f"處理：{first[1] if len(first) > 1 else ''}",
                                         "decisions": [{"decision": "先列步驟", "reason": "使用者要求"}],
                                         "progress": "列完了", "open_questions": []},
                                        ensure_ascii=False) + "\n```", "fake-model"

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
    return agent


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def test_import_then_search(env, capsys):
    code, out, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode",
                       "--header", "title=CSV 規劃", "--header", "description=先做讀取")
    assert code == 0 and out.startswith("agora:")
    code, found, _ = run(capsys, "search", "session", "--filter", "text~=表格", "--no-sync")
    assert found.split()[0] == out
    hdr, _ = h.split_document((store.Paths.from_env().mirror / out.split(":")[1] / "session.md").read_text())
    assert hdr["title"] == "CSV 規劃" and hdr["description"] == "先做讀取"
    assert hdr["agora"]["source"]["session_id"] == "ses_a" and hdr["agora"]["relation"] == "import"


def test_reimport_unchanged_keeps_id(env, capsys):
    _, first, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    _, second, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert first == second


def test_reimport_changed_without_children_updates_same_id(env, capsys):
    _, first, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_a"].append("新的一則")
    _, second, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert first == second


def test_reimporting_the_agent_session_a_continue_moved_past_is_a_session_of_its_own(env, capsys):
    _, first, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    run(capsys, "continue", "session", first, "--agent", "opencode", "--dir", "/tmp")
    env.sessions["ses_a"].append("來源端又改了")
    _, second, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert second != first


def test_continue_writes_back_into_the_same_session(env, capsys):  # design 5.4, user's call
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    code, out, _ = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "/tmp",
                       "--header", "tags=next")
    assert code == 0 and out == parent                       # no new session
    paths = store.Paths.from_env()
    hdr = store.Index(paths).header(parent.split(":")[1])
    assert hdr["agora"]["relation"] == "import" and hdr["tags"] == ["next"]
    assert hdr["agora"]["source"]["session_id"] == env.launched[-1].agent_session_id
    assert hdr["agora"]["previous_sources"] == ["opencode:ses_a"]
    assert "接著做完了" in (paths.mirror / parent.split(":")[1] / "session.md").read_text()
    assert env.launched[-1].agent_session_id.startswith("ses_n")   # same agent → native
    assert not list(paths.pending.glob("*.json"))
    assert len(store.Index(paths).search([])) == 1


def test_continuing_a_merge_turns_it_into_that_conversation(env, capsys):
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    code, out, _ = run(capsys, "continue", "session", m, "--agent", "opencode", "--dir", "/tmp")
    hdr = store.Index(store.Paths.from_env()).header(m.split(":")[1])
    assert code == 0 and out == m and hdr["agora"]["relation"] == "continue" and "merge" not in hdr["agora"]
    assert [p["id"] for p in hdr["agora"]["parents"]] == [a, b] and hdr["agora"]["raw"]


def test_continue_with_nothing_new_saves_nothing(env, capsys):
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.speak = False
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "/tmp")
    assert code == 0 and out == "" and "沒有新內容" in err


def test_merge_lays_out_one_checked_section_per_source(env, capsys):  # design v7 5.3
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "只在 B 內文的一句"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    code, merged, _ = run(capsys, "merge", "session", f"{a},{b}", "--agent", "opencode")
    assert code == 0
    assert len(env.prompts) == 2                           # one AI call per source
    (pa, workdir), (pb, _) = env.prompts
    assert "把 CSV 轉成 Markdown 表格" in pa and "只在 B 內文的一句" not in pa   # each sees only its own
    assert "只在 B 內文的一句" in pb and pa.startswith(cli.SUMMARY_PROMPT)
    assert (workdir / ".git").is_dir()                     # opencode files the run under its own project
    paths = store.Paths.from_env()
    hdr = store.Index(paths).header(merged.split(":")[1])
    assert hdr["agora"]["relation"] == "merge" and [p["id"] for p in hdr["agora"]["parents"]] == [a, b]
    assert hdr["generated"]["by"] == "opencode/fake-model" and hdr["description"].startswith("合併 2 個 Session")
    assert hdr["status"] == "draft" and hdr["agora"]["merge"] == {"kind": "sections", "by": "opencode/fake-model",
                                                                 "prompt": cli.SUMMARY_PROMPT_VERSION}
    _, raw, _ = run(capsys, "show", "session", merged, "--raw")
    assert [sec["id"] for sec in json.loads(raw)["sections"]] == [a, b]
    _, out, _ = run(capsys, "show", "session", merged)
    assert f"### 「把 CSV 轉成 Markdown 表格」（原本是 opencode）\n`{a}`（原版：`agora show session {a}`）" in out
    assert "**目的**：處理：把 CSV 轉成 Markdown 表格" in out and "- 先列步驟 —— 理由：使用者要求" in out
    assert "**未解決**：\n- （沒有）" in out and f"- {b}「讀取 CSV」" in out
    assert "整體" not in out                                 # nothing across sources
    run(capsys, "continue", "session", merged, "--agent", "opencode", "--dir", "/tmp")
    loaded = env.sessions[env.launched[-1].agent_session_id]
    assert loaded[0].startswith(cli.MERGE_NOTE.format(by="opencode/fake-model")) and f"`{b}`" in loaded[0]
    assert "只在 B 內文的一句" not in "\n".join(loaded)       # the sources' text is not loaded
    assert loaded[1] == cli.MERGE_READY


def test_a_bad_answer_is_regenerated_then_gives_up(env, capsys):  # design v7: schema or retry
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    good = json.dumps({"purpose": "p", "decisions": [], "progress": "q", "open_questions": []})
    env.answers = ["不是 JSON", json.dumps({"purpose": "p"}), good]          # a: two bad, then good
    code, merged, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert code == 0 and len(env.prompts) == 4
    retried = env.prompts[1][0]
    assert retried.index("上一次的輸出不合格") < retried.index("以下是 Session 的內容")   # never inside the session
    assert "required" in env.prompts[2][0]
    env.answers = ["{}", "{}", "{}"]
    code, out, err = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert code == 2 and out == "" and "連續 3 次" in err


def test_merge_needs_an_agent(env, capsys):
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    code, _, err = run(capsys, "merge", "session", a, b)
    assert code == 1 and "--agent" in err


def test_a_failed_summary_saves_nothing(env, capsys, monkeypatch):
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    def fail(prompt, workdir):
        raise AgentError("逾時")
    monkeypatch.setattr(env, "summarize", fail)
    before = len(store.Index(store.Paths.from_env()).search([]))
    code, out, err = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert code == 2 and out == "" and "逾時" in err
    assert len(store.Index(store.Paths.from_env()).search([])) == before


def test_upload_failure_exits_3_and_stays_searchable(env, capsys, monkeypatch):  # N13
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    code, out, err = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert code == cli.EXIT_IN_OUTBOX and "outbox" in err
    _, found, _ = run(capsys, "search", "session", "--filter", "text~=CSV", "--no-sync")
    assert found.split()[0] == out and found.endswith("(未上傳)")   # C11


def test_pending_from_dead_agora_is_finished_later(env, capsys):  # S3, N2
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    paths = store.Paths.from_env()
    env.sessions["ses_x"] = ["a", "b"]
    record = {"agora_id": parent, "agent": "opencode",
              "agent_session_id": "ses_x", "dir": "/tmp", "before_count": 2,
              "parent": {"id": parent, "raw_md5": None}, "title": "t"}
    _, lock = cli._write_pending(paths, record)
    lock.close()                               # that agora died: nobody holds the lock
    _, _, err = run(capsys, "search", "session")
    assert "補存" in err
    assert store.Index(paths).header(parent.split(":")[1])["agora"]["source"]["session_id"] == "ses_x"


def test_pending_held_by_live_agora_is_left_alone(env, capsys):  # N2
    import fcntl
    paths = store.Paths.from_env()
    record = {"agora_id": "agora:01K6LIVE0000000000000000AA", "agent": "opencode",
              "agent_session_id": "ses_a", "dir": "/tmp", "before_count": 0,
              "parent": {"id": "agora:x", "raw_md5": None}}
    path, lock = cli._write_pending(paths, record)   # created already locked (C3)
    run(capsys, "search", "session")
    assert path.exists()
    lock.close()


def test_search_filter_needs_an_operator(env, capsys):
    code, _, err = run(capsys, "search", "session", "--filter", "表格", "--no-sync")
    assert code == 2 and "KEY=VALUE" in err


def test_search_by_header_path_and_contains(env, capsys):
    _, sid, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode",
                    "--header", "tags=[csv, 表格]", "--header", "generated.by=human:fatesaikou")
    for flt in ["tags=csv", "generated.by=human:fatesaikou", "generated.by~=fatesaikou", "agent=opencode",
                "type=Session", "title~=CSV"]:
        code, found, _ = run(capsys, "search", "session", "--filter", flt, "--no-sync")
        assert code == 0 and found.split()[0] == sid, flt
    code, found, _ = run(capsys, "search", "session", "--filter", "tags=nothing", "--no-sync")
    assert found == ""


def test_lock_survives_agora_death_while_agent_lives(env, capsys, tmp_path):  # C1
    import fcntl
    import os
    import subprocess
    import time
    paths = store.Paths.from_env()
    record = {"agora_id": "agora:01K6KILL0000000000000000AA", "agent": "opencode",
              "agent_session_id": "ses_a", "dir": "/tmp", "before_count": 0,
              "parent": {"id": "agora:x", "raw_md5": None}}
    path, lock = cli._write_pending(paths, record)
    agent = subprocess.Popen(["sleep", "30"], pass_fds=(lock.fileno(),))
    lock.close()                                # agora is gone, the agent is not
    try:
        with open(path) as f:
            with pytest.raises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(capsys, "search", "session")
        assert path.exists()                    # not finished early
    finally:
        agent.kill()
        agent.wait()
    run(capsys, "search", "session")
    assert not path.exists()                    # both gone: finished now


def test_broken_local_files_do_not_brick_commands(env, capsys):  # C2
    paths = store.Paths.from_env()
    paths.pending.mkdir(parents=True)
    (paths.pending / "01K6BROKEN000000000000000A.json").write_text("{not json")
    (paths.outbox / "01K6NOSESSION0000000000000").mkdir(parents=True)
    bad = paths.outbox / "01K6BADYAML000000000000000"
    bad.mkdir()
    (bad / "session.md").write_text("---\ntitle: [unclosed\n---\nbody\n")
    code, _, err = run(capsys, "search", "session")
    assert code == 0
    assert (paths.pending / ".bad" / "01K6BROKEN000000000000000A.json").exists()
    assert (paths.outbox / ".bad" / "01K6BADYAML000000000000000").exists()
    code, _, _ = run(capsys, "search", "session", "--filter", "text~=x", "--no-sync")
    assert code == 0


def test_relative_dir_and_header_title(env, capsys, tmp_path, monkeypatch):  # C5, C6
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.chdir(tmp_path)
    _, child, err = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "proj",
                        "--header", "title=接手後的標題")
    assert env.launched[-1].cwd == str(work.resolve())
    hdr = store.Index(store.Paths.from_env()).header(child.split(":")[1])
    assert hdr["title"] == "接手後的標題"


def test_failed_recovery_does_not_change_search_exit(env, capsys, monkeypatch):  # C12
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    paths = store.Paths.from_env()
    env.sessions["ses_y"] = ["a", "b", "c"]
    record = {"agora_id": "agora:01K6RECOVER00000000000000A", "agent": "opencode",
              "agent_session_id": "ses_y", "dir": "/tmp", "before_count": 2,
              "parent": {"id": parent, "raw_md5": None}}
    _, lock = cli._write_pending(paths, record)
    lock.close()
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    code, _, _ = run(capsys, "search", "session")
    assert code == 0


def test_agent_gets_ctrl_c_and_agora_survives(env, capsys, tmp_path):  # N1
    import os
    import signal
    import subprocess
    import textwrap
    script = tmp_path / "agent.py"
    script.write_text(textwrap.dedent("""
        import signal, sys, os
        h = signal.getsignal(signal.SIGINT)
        open(sys.argv[1], "w").write("default" if h is signal.default_int_handler else str(h))
        os.kill(os.getppid(), signal.SIGINT)   # Ctrl-C reaches the whole group
    """))
    marker = tmp_path / "handler.txt"
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    orig = env.start_native
    def start_native(raw, workdir):
        launch = orig(raw, workdir)
        launch.argv = [sys.executable, str(script), str(marker)]
        return launch
    env.start_native = start_native
    code, child, _ = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", str(tmp_path))
    assert marker.read_text() == "default"     # the agent can be stopped with Ctrl-C
    assert code == 0 and child.startswith("agora:")   # agora ignored it and finished


def test_missing_rclone_keeps_good_outbox_entry(env, capsys, monkeypatch):  # R1, R2
    monkeypatch.setenv("AGORA_RCLONE", "/nonexistent/rclone")
    code, out, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert code == cli.EXIT_IN_OUTBOX
    run(capsys, "search", "session")
    paths = store.Paths.from_env()
    assert store.outbox_count(paths) == 1 and store.bad_count(paths) == 0


def test_bad_entries_are_reported_every_command(env, capsys):  # R2
    paths = store.Paths.from_env()
    bad = paths.outbox / ".bad" / "x"
    bad.mkdir(parents=True)
    _, _, err = run(capsys, "search", "session", "--filter", "text~=x", "--no-sync")
    assert "壞檔" in err


def test_continue_hands_the_pending_lock_to_the_agent(env, capsys, monkeypatch):  # R3 (C1 through cmd_continue)
    import fcntl
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    seen = {}
    real_run = cli.subprocess.run

    def spy(argv, **kwargs):
        if argv != ["true"]:                     # rclone calls go through untouched
            return real_run(argv, **kwargs)
        paths = store.Paths.from_env()
        [pending] = list(paths.pending.glob("*.json"))
        seen["fds"] = kwargs.get("pass_fds")
        with open(pending) as f:                 # locked while the agent runs
            with pytest.raises(BlockingIOError):
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", spy)
    code, _, _ = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "/tmp")
    assert code == 0 and seen["fds"] and len(seen["fds"]) == 1


def test_agent_runs_with_pwd_set_to_the_workdir(env, capsys, monkeypatch, tmp_path):  # opencode reads PWD
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    seen = {}
    real_run = cli.subprocess.run

    def spy(argv, **kwargs):
        if argv == ["true"]:
            seen["pwd"] = kwargs["env"]["PWD"]
            seen["cwd"] = kwargs["cwd"]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", spy)
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "other"
    work.mkdir()
    run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", str(work))
    assert seen["pwd"] == seen["cwd"] == str(work.resolve())


def _import(capsys, *extra):
    return run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode", *extra)


def test_import_fills_okf_fields(env, capsys):  # design 3.4
    _, sid, _ = _import(capsys)
    hdr = store.Index(store.Paths.from_env()).header(sid.split(":")[1])
    assert hdr["type"] == "Session"
    assert hdr["description"] == "把 CSV 轉成 Markdown 表格"
    assert hdr["generated"]["by"] == "opencode" and hdr["generated"]["at"] == "2026-10-01T00:00:00Z"
    assert hdr["sources"][0]["id"] == "opencode:ses_a"
    assert hdr["agora"]["header"] == 2 and hdr["agora"]["raw"]["md5"]
    assert "verified" not in hdr


def test_header_file_then_header_overlay(env, capsys, tmp_path):  # design 3.5
    f = tmp_path / "h.yaml"
    f.write_text("title: 從檔案\nstatus: draft\ngenerated: {by: human:fatesaikou}\n")
    _, sid, _ = _import(capsys, "--header-file", str(f), "--header", "title=從參數")
    hdr = store.Index(store.Paths.from_env()).header(sid.split(":")[1])
    assert hdr["title"] == "從參數" and hdr["status"] == "draft"
    assert hdr["generated"] == {"by": "human:fatesaikou", "at": "2026-10-01T00:00:00Z"}   # mapping merged


def test_header_cannot_touch_system_fields(env, capsys):
    code, _, err = _import(capsys, "--header", "agora.relation=merge")
    assert code == 2 and "系統欄位" in err


def test_delete_needs_yes_and_moves_to_trash(env, capsys, tmp_path):
    _, sid, _ = _import(capsys)
    code, _, err = run(capsys, "delete", "session", sid)
    assert code == 1 and "--yes" in err
    code, out, _ = run(capsys, "delete", "session", sid, "--yes")
    assert code == 0 and out == sid
    paths = store.Paths.from_env()
    assert store.Index(paths).header(sid.split(":")[1]) is None
    remote = Path(os.environ["FAKE_REMOTE"])
    assert not (remote / "agora" / "sessions" / sid.split(":")[1]).exists()


def test_delete_refuses_a_session_with_children(env, capsys):
    _, sid, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    run(capsys, "merge", "session", sid, b, "--agent", "opencode")      # a merge is a child of its sources
    code, _, err = run(capsys, "delete", "session", sid, "--yes")
    assert code == 1 and "子 Session" in err


def test_edit_with_header_keeps_raw_and_system_fields(env, capsys):
    _, sid, _ = _import(capsys)
    paths = store.Paths.from_env()
    before = store.Index(paths).header(sid.split(":")[1])
    code, out, _ = run(capsys, "edit", "session", sid, "--header", "tags=[檢查]",
                       "--header", "verified=[{by: 'human:fatesaikou', at: 2026-10-02}]")
    assert code == 0 and out == sid
    after = store.Index(paths).header(sid.split(":")[1])
    assert after["tags"] == ["檢查"] and after["verified"][0]["by"] == "human:fatesaikou"
    assert after["agora"]["raw"] == before["agora"]["raw"] and after["id"] == before["id"]


def test_edit_in_editor(env, capsys, tmp_path, monkeypatch):
    _, sid, _ = _import(capsys)
    script = tmp_path / "ed.sh"
    script.write_text("#!/bin/sh\nsed -i '' 's/^title: .*/title: 編輯器改的/' \"$1\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    code, out, _ = run(capsys, "edit", "session", sid)
    assert code == 0
    assert store.Index(store.Paths.from_env()).header(sid.split(":")[1])["title"] == "編輯器改的"


def test_positional_rules(env, capsys):
    code, _, err = run(capsys, "import", "session", "agora:x", "--external-session-id", "ses_a", "--agent", "opencode")
    assert code == 1 and "--external-session-id" in err
    code, _, err = run(capsys, "search", "session", "agora:x")
    assert code == 1 and "--filter" in err
    code, _, err = run(capsys, "continue", "session")
    assert code == 1


def test_old_format_sessions_still_work(env, capsys):  # V1
    """Version-1 headers (before OKF) on Drive are read as the current shape."""
    paths = store.Paths.from_env()
    remote = Path(os.environ["FAKE_REMOTE"])
    parent_ulid, child_ulid = h.new_ulid(), h.new_ulid()
    old_parent = {"header": 1, "entity": "agora", "type": "session", "id": f"agora:{parent_ulid}",
                  "title": "舊格式", "note": "舊的備註", "tags": [], "refs": [], "case": None,
                  "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
                  "relation": "import", "parents": [],
                  "source": {"agent": "opencode", "session_id": "ses_a", "created_at": "2026-10-01T00:00:00Z"}}
    old_child = {**old_parent, "id": f"agora:{child_ulid}", "relation": "continue",
                 "parents": [{"id": f"agora:{parent_ulid}", "raw_md5": None}]}
    run(capsys, "search", "session")                      # creates the Drive folder
    for hdr in (old_parent, old_child):
        folder = remote / "agora" / "sessions" / hdr["id"].split(":")[1]
        folder.mkdir(parents=True)
        (folder / "session.md").write_text(h.dump_document(hdr, "## user\n表格\n"))
    (paths.state / "last-sync").unlink()                 # skip the 5-minute search throttle
    code, found, _ = run(capsys, "search", "session", "--filter", "agora.relation=import")
    assert code == 0 and f"agora:{parent_ulid}" in found
    code, _, err = run(capsys, "delete", "session", f"agora:{parent_ulid}", "--yes")
    assert code == 1 and "子 Session" in err             # the child check still works
    code, _, _ = run(capsys, "edit", "session", f"agora:{child_ulid}", "--header", "tags=[舊]")
    assert code == 0
    hdr = store.Index(paths).header(child_ulid)
    assert hdr["type"] == "Session" and hdr["agora"]["relation"] == "continue" and hdr["tags"] == ["舊"]
    assert hdr["description"] == "舊的備註"


def test_header_values_stay_text_unless_list_or_mapping(env, capsys):  # V2, V3
    _, sid, _ = _import(capsys, "--header", "description=把 CSV: 轉成表格", "--header", "title=no",
                        "--header", "stale_after=2027-01-01")
    hdr = store.Index(store.Paths.from_env()).header(sid.split(":")[1])
    assert hdr["description"] == "把 CSV: 轉成表格" and hdr["title"] == "no" and hdr["stale_after"] == "2027-01-01"
    code, found, _ = run(capsys, "search", "session", "--filter", "stale_after=2027-01-01", "--no-sync")
    assert found.split()[0] == sid


def test_a_merge_of_a_merge_reuses_its_sections(env, capsys):  # design v7 5.3
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "只在 B 內文的一句"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    env.sessions["ses_c"] = ["輸出表格", "好"]
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_c", "--agent", "opencode")
    _, ab, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    calls = len(env.prompts)
    _, abc, _ = run(capsys, "merge", "session", ab, c, "--agent", "opencode")
    assert len(env.prompts) == calls + 1                  # only c is summarized; ab is reused as is
    _, out, _ = run(capsys, "show", "session", abc)
    assert f"`{ab}`" in out and "（原本是 merge）" in out
    assert f"#### 「把 CSV 轉成 Markdown 表格」（原本是 opencode）\n`{a}`" in out   # nested one level down
    assert f"- {ab}「" in out and f"- {a}「" not in out     # the list names direct sources only


def test_cross_agent_single_segment_gets_the_note(env, capsys, monkeypatch):  # review W2, W4
    _, a, _ = _import(capsys)
    monkeypatch.setattr(env, "name", "claude", raising=False)
    run(capsys, "continue", "session", a, "--agent", "claude", "--dir", "/tmp")
    loaded = env.sessions[env.launched[-1].agent_session_id]
    assert loaded[0].startswith(cli.CONVERTED_NOTE)


def test_an_id_not_known_here_skips_the_throttle(env, capsys):
    """A session another machine imported a minute ago, or a deleted cache, is still found."""
    _, a, _ = _import(capsys)
    paths = store.Paths.from_env()
    shutil.rmtree(paths.cache)                            # last-sync in state stays fresh
    code, out, _ = run(capsys, "show", "session", a)
    assert code == 0 and "把 CSV 轉成 Markdown 表格" in out


def test_merge_refuses_the_same_session_twice(env, capsys):  # review X4
    _, a, _ = _import(capsys)
    code, _, err = run(capsys, "merge", "session", a, a, "--agent", "opencode")
    assert code == 1 and "重複" in err


def test_converted_turns_close_an_unanswered_question(monkeypatch):  # review W1, W6, X1
    turns = {b"q": [("user", ["問題"]), ("user", ["[skip image]"])], b"e": [("user", ["[skip image]"])]}
    monkeypatch.setattr(cli, "load_agent", lambda name: type("A", (), {"turns": staticmethod(turns.get)})())
    got = cli._converted_turns("opencode", "agora:Q", b"q")
    assert got == [("user", [cli.CONVERTED_NOTE, "（以下來自 agora:Q，原本是 opencode 的對話）", "問題"]),
                   ("assistant", [cli.NO_REPLY])]
    with pytest.raises(cli.InputError):
        cli._converted_turns("opencode", "agora:E", b"e")


def test_a_long_source_is_cut_in_the_middle(env, capsys, monkeypatch):  # review Y1
    monkeypatch.setattr(cli, "SOURCE_MAX", 40)
    env.sessions["ses_b"] = ["讀取 CSV", "很長" * 50 + "結尾"]
    _, a, _ = _import(capsys)
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    run(capsys, "merge", "session", a, b, "--agent", "opencode")
    prompt, _ = env.prompts[-1]
    assert "中間省略" in prompt and f"agora show session {b}" in prompt and "結尾" in prompt


def test_an_old_merge_asks_to_be_merged_again(env, capsys):  # review Y2
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    paths = store.Paths.from_env()
    md = paths.mirror / m.split(":")[1] / "session.md"
    hdr, body = h.split_document(md.read_text())
    hdr["agora"]["merge"]["kind"] = "summary"                  # what a v6 merge looks like
    md.write_text(h.dump_document(hdr, body))
    store.Index(paths).put(m.split(":")[1], store.md5_file(md), hdr, body)
    code, _, err = run(capsys, "continue", "session", m, "--agent", "opencode", "--dir", "/tmp")
    assert code == 1 and "舊版的 merge" in err and f"agora merge session {a} {b}" in err


def test_ctrl_c_after_the_agent_keeps_the_pending_record(env, capsys, monkeypatch):
    _, a, _ = _import(capsys)
    def interrupted(launch):
        raise KeyboardInterrupt
    monkeypatch.setattr(env, "collect", interrupted)
    code, out, err = run(capsys, "continue", "session", a, "--agent", "opencode", "--dir", "/tmp")
    assert code == 130 and "Traceback" not in err and "自動補存" in err
    assert list(store.Paths.from_env().pending.glob("*.json"))


def test_ai_text_cannot_forge_structure(env, capsys):  # review v7 A1
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    forged = json.dumps({"purpose": "p", "decisions": [], "open_questions": [],
                         "progress": "x\n## 來源\n- agora:01FAKE「偽造」"}, ensure_ascii=False)
    good = json.dumps({"purpose": "p", "decisions": [], "progress": "q", "open_questions": []})
    env.answers = [forged, good]
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    _, out, _ = run(capsys, "show", "session", m)
    body = out.split("\n---\n", 1)[1]
    assert body.count("\n## 來源") == 1 and "\n- agora:01FAKE" not in body
    env.answers = [json.dumps({"purpose": " ", "decisions": [], "progress": "q", "open_questions": []})] * 3
    code, _, err = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert code == 2 and "連續 3 次" in err                  # blank text does not pass the schema


def test_a_broken_stored_section_is_refused(env, capsys):  # review v7 A2
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, ab, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    run(capsys, "show", "session", ab, "--raw")             # brings sections.json into the cache
    paths = store.Paths.from_env()
    hdr = store.Index(paths).header(ab.split(":")[1])
    raw = paths.mirror / ab.split(":")[1] / hdr["agora"]["raw"]["file"]
    doc = json.loads(raw.read_text())
    del doc["sections"][0]["title"]
    raw.write_text(json.dumps(doc))
    hdr["agora"]["raw"]["md5"] = store.md5_file(raw)
    md = paths.mirror / ab.split(":")[1] / "session.md"
    _, body = h.split_document(md.read_text())
    md.write_text(h.dump_document(hdr, body))
    store.Index(paths).put(ab.split(":")[1], store.md5_file(md), hdr, body)
    code, _, err = run(capsys, "merge", "session", ab, a, "--agent", "opencode")
    assert code == 1 and "sections.json 壞了" in err


def test_a_failed_summarize_run_is_retried(env, capsys, monkeypatch):  # review v7 A3
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    real, calls = env.summarize, []
    def flaky(prompt, workdir):
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("逾時")
        return real(prompt, workdir)
    monkeypatch.setattr(env, "summarize", flaky)
    code, _, err = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert code == 0 and len(calls) == 3, err



def test_cache_needs_agora_or_local_and_other_actions_need_session(env, capsys):  # design 5.10
    code, _, err = run(capsys, "cache", "session")
    assert code == 1 and "agora 或 local" in err
    code, _, err = run(capsys, "search")
    assert code == 1 and "型態要寫 session" in err


def test_cache_agora_brings_every_raw_and_sync_writes_the_mirror_back(env, capsys):  # design 5.10
    _, a, _ = _import(capsys)
    paths = store.Paths.from_env()
    ulid = a.split(":")[1]
    hdr = store.Index(paths).header(ulid)
    raw = paths.mirror / ulid / hdr["agora"]["raw"]["file"]
    if raw.exists():
        raw.unlink()
    code, out, _ = run(capsys, "cache", "agora")
    assert code == 0 and raw.exists() and "快取完成：1 個" in out
    md = paths.mirror / ulid / "session.md"
    md.write_text(md.read_text() + "\n本機改的一行\n")
    code, out, _ = run(capsys, "sync")
    remote = Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions" / ulid / "session.md"
    assert code == 0 and "已寫回 Drive" in out and "本機改的一行" in remote.read_text()



def test_delete_several_at_once_children_first(env, capsys):  # user's call
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    env.sessions["ses_c"] = ["第三個", "好"]
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_c", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    code, _, err = run(capsys, "delete", "session", f"{a},{m}", c)
    assert code == 1 and "這 3 個" in err                       # without --yes: the list, nothing deleted
    code, out, err = run(capsys, "delete", "session", a, m, c, "--yes")   # m goes first, then a can go
    assert code == 0 and set(out.split()) == {a, m, c} and "已把 3 個" in err
    code, out, err = run(capsys, "delete", "session", b, "--yes")
    assert code == 0 and out == b


def test_delete_several_refuses_only_those_with_children_left(env, capsys):
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    env.sessions["ses_c"] = ["第三個", "好"]
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_c", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    code, out, err = run(capsys, "delete", "session", a, c, "--yes")    # a still has m as a child
    assert code == 1 and out == c and f"{a} 有子 Session" in err
