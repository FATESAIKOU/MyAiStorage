"""The agora command over a fake rclone and a fake agent (test-plan U-CLI)."""

from __future__ import annotations

import json
import os
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


def test_reimport_after_continue_branches(env, capsys):  # S7
    _, first, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    run(capsys, "continue", "session", first, "--agent", "opencode", "--dir", "/tmp")
    env.sessions["ses_a"].append("來源端又改了")
    _, second, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    assert second != first
    hdr = store.Index(store.Paths.from_env()).header(second.split(":")[1])
    assert hdr["agora"]["parents"][0]["id"] == first


def test_continue_native_creates_child(env, capsys):
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    code, child, _ = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "/tmp",
                         "--header", "tags=next")
    assert code == 0 and child.startswith("agora:") and child != parent
    hdr = store.Index(store.Paths.from_env()).header(child.split(":")[1])
    assert hdr["agora"]["relation"] == "continue" and hdr["agora"]["parents"][0]["id"] == parent
    assert hdr["tags"] == ["next"]
    assert env.launched[-1].agent_session_id.startswith("ses_n")   # same agent → native
    assert not list(store.Paths.from_env().pending.glob("*.json"))


def test_continue_with_nothing_new_saves_nothing(env, capsys):
    _, parent, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.speak = False
    code, out, err = run(capsys, "continue", "session", parent, "--agent", "opencode", "--dir", "/tmp")
    assert code == 0 and out == "" and "沒有新內容" in err


def test_merge_then_continue_uses_injection(env, capsys):  # L3
    _, a, _ = run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    code, merged, _ = run(capsys, "merge", "session", f"{a},{b}")
    assert code == 0
    paths = store.Paths.from_env()
    hdr = store.Index(paths).header(merged.split(":")[1])
    assert hdr["agora"]["relation"] == "merge" and [p["id"] for p in hdr["agora"]["parents"]] == [a, b]
    assert "raw" not in hdr["agora"]
    _, child, _ = run(capsys, "continue", "session", merged, "--agent", "opencode", "--dir", "/tmp")
    assert env.launched[-1].agent_session_id.startswith("ses_i")   # merge → injection


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
    record = {"agora_id": "agora:01K6DEADBEEF000000000000AA", "agent": "opencode",
              "agent_session_id": "ses_x", "dir": "/tmp", "before_count": 2,
              "parent": {"id": parent, "raw_md5": None}, "title": "t"}
    _, lock = cli._write_pending(paths, record)
    lock.close()                               # that agora died: nobody holds the lock
    _, _, err = run(capsys, "search", "session")
    assert "補存" in err
    assert store.Index(paths).header("01K6DEADBEEF000000000000AA")["agora"]["relation"] == "continue"


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
    run(capsys, "continue", "session", sid, "--agent", "opencode", "--dir", "/tmp")
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
