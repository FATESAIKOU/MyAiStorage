"""`agora continue --fork`: the new session, and the original left alone (change continue-fork).

Its own fake agent, so these tests do not depend on `append_native` landing in the
harnesses the other change items share. This one implements the smallest version of the
contract (design「接在原生 raw 後面」): the fork's turns are appended to the raw as it is,
and a user turn with no reply gets a stand-in reply first, so A's own lines stay exactly
where they were.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h
from agora.agents.base import AgentError, Exported, Launch
from _round_seam import in_process_start_child

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


class ForkAgent:
    """Sessions in memory; launching 'talks' by appending a message."""

    name = "opencode"

    def __init__(self):
        self.sessions: dict[str, list[str]] = {"ses_a": ["把 CSV 轉成 Markdown 表格", "好的，三個步驟"]}
        self.launched: list[Launch] = []
        self.speak = True
        self.appended: list = []            # the (raw, turns) pairs a fork handed over

    def _exported(self, sid):
        msgs = self.sessions[sid]
        return Exported(session_id=sid, raw=json.dumps({"id": sid, "m": msgs}, ensure_ascii=False).encode(),
                        dir="/tmp", title=msgs[0][:20], created_at="2026-10-01T00:00:00Z",
                        agent_version="9.9", message_count=len(msgs))

    def export(self, sid):
        if sid not in self.sessions:
            raise AgentError(f"沒有這個 session：{sid}")
        return self._exported(sid)

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def native(self, turns):
        return json.dumps({"id": "synth", "m": ["\n".join(lines) for _role, lines in turns]},
                          ensure_ascii=False).encode()

    def append_native(self, raw, turns):
        self.appended.append((raw, turns))
        msgs = list(json.loads(raw)["m"])
        if len(msgs) % 2:          # this agent files the user turn at the odd position: unanswered
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    agent = ForkAgent()
    monkeypatch.setattr(cli, "load_agent", lambda name: agent)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    return agent


@pytest.fixture
def during_agent(monkeypatch):
    """Run something while the agent process is up (argv ["true"] is the fake's)."""
    def run(callback):
        real = cli.subprocess.Popen

        def spy(argv, **kwargs):
            if argv == ["true"]:
                callback()
            return real(argv, **kwargs)

        monkeypatch.setattr(cli.subprocess, "Popen", spy)
    return run


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def paths():
    return store.Paths.from_env()


def header_of(agora_id):
    return store.Index(paths()).header(agora_id.split(":")[1])


def document_of(agora_id):
    return (paths().mirror / agora_id.split(":")[1] / "session.md").read_text(encoding="utf-8")


def body_of(agora_id):
    return document_of(agora_id).split("\n---\n", 1)[1]


def index_row(ulid):
    return store.Index(paths()).db.execute(
        "SELECT md5, header, body FROM sessions WHERE ulid=?", (ulid,)).fetchone()


def on_drive(agora_id):
    return Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions" / agora_id.split(":")[1]


def _import(capsys, *extra):
    return run(capsys, "import", "session", "--external-session-id", "ses_a", "--agent", "opencode", *extra)


def _fork(capsys, agora_id, *extra):
    return run(capsys, "continue", "session", agora_id, "--agent", "opencode", "--fork", "--dir", "/tmp", *extra)


def _session_ids():
    return [hdr["id"] for _u, hdr, _s in store.Index(paths()).search([])]


def _fork_session():
    return next(hdr for _u, hdr, _s in store.Index(paths()).search([])
                if h.agora_of(hdr).get("relation") == "fork")


def _rewrite_header(agora_id, hdr):
    """Change a session's header by hand: the mirror first, then the index row."""
    path = paths().mirror / agora_id.split(":")[1] / "session.md"
    path.write_text(h.dump_document(hdr, body_of(agora_id)), encoding="utf-8")
    store.index_file(store.Index(paths()), path)


# --- the new session, and the original left alone -----------------------------------

def test_fork_makes_a_new_session_and_the_original_is_untouched(env, capsys):
    """Scenario「fork 一個 claude Session（同 agent）」: B is A's header with `-fork`, its
    conversation is A's own raw plus the two fork turns plus what was said after."""
    _, a, _ = _import(capsys, "--header", "title=表格排版", "--header", "case=mybrain:aistorage",
                      "--header", "tags=[ui, tui]", "--header", "description=先做讀取")
    before_doc = document_of(a)
    before_row = index_row(a.split(":")[1])

    code, b, err = _fork(capsys, a)

    assert code == 0 and b.startswith("agora:") and b != a
    # R2: the exact step-6 line, not the phrase - `沒有新內容；fork 已存成 …` at the end
    # would satisfy a bare substring and leave this line untested (review F7).
    assert f"[agora] fork 已存成 {b}" in err, "M7: the id is said as soon as it is saved"
    hdr = header_of(b)
    assert hdr["title"] == "表格排版-fork"
    assert hdr["case"] == "mybrain:aistorage" and hdr["tags"] == ["ui", "tui"]
    assert hdr["description"] == "先做讀取"
    assert h.agora_of(hdr)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr)["parents"]] == [a]
    assert h.agora_of(hdr)["parents"][0]["raw_md5"] == header_of(a)["agora"]["raw"]["md5"]
    assert h.agora_of(hdr)["source"]["session_id"] == env.launched[-1].agent_session_id

    # same agent: A's raw is loaded unconverted, with the fork's two turns appended to it
    (raw, turns), = env.appended
    assert json.loads(raw)["m"] == env.sessions["ses_a"], "A's own lines, one byte, in order"
    assert turns == [("user", [cli.FORK_NOTE.format(
                       id=a, header="title: 表格排版，description: 先做讀取，case: mybrain:aistorage，tags: ui, tui")]),
                     ("assistant", [cli.FORK_READY])]
    loaded = env.sessions[env.launched[-1].agent_session_id]
    assert loaded[:2] == env.sessions["ses_a"] and loaded[-1] == "接著做完了"
    body = body_of(b)
    assert all(line in body for line in ("把 CSV 轉成 Markdown 表格", "好的，三個步驟", cli.FORK_READY, "接著做完了"))

    assert document_of(a) == before_doc, "A's session.md must be byte for byte the same"
    assert index_row(a.split(":")[1]) == before_row, "and so must A's index row"


def test_the_note_names_the_original_and_its_header(env, capsys):
    """design F15: `從 agora:X（title: …，case: …，tags: …）fork` and nothing else - no
    `agora` block, no `generated`, no `sources`. The format is fixed by this test."""
    _, a, _ = _import(capsys, "--header", "title=表格排版", "--header", "case=mybrain:aistorage",
                      "--header", "tags=[ui, tui]", "--header", "refs=[mybrain:note/x]")
    _fork(capsys, a)

    loaded = env.sessions[env.launched[-1].agent_session_id]
    assert loaded[-3] == (f"從 {a}（title: 表格排版，description: 把 CSV 轉成 Markdown 表格，"
                          "case: mybrain:aistorage，tags: ui, tui，refs: mybrain:note/x）fork")
    assert loaded[-2] == cli.FORK_READY
    assert cli.FORK_NOTE.format(id=a, header="title: X") == f"從 {a}（title: X）fork"


def test_a_session_with_no_title_is_named_after_its_description_or_its_id(env, capsys):
    """design「title」: `A.title or A.description or A.id`, and the value is made a string
    first - YAML reads `title: 2026` as a number."""
    _, a, _ = _import(capsys)
    assert header_of(a)["title"] == "把 CSV 轉成 Markdown 表格", "the import named it"

    _, b, _ = _fork(capsys, a)

    assert header_of(b)["title"] == "把 CSV 轉成 Markdown 表格-fork"
    assert isinstance(header_of(b)["title"], str), "a string, always: YAML reads 2026 as a number"
    assert env.sessions[env.launched[-1].agent_session_id][-3].startswith(f"從 {a}（title: ")


def test_open_and_leave_keeps_the_fork_and_says_its_id(env, capsys):
    """Scenario「打開就離開」: nothing was said and the fork is still there. Nothing goes
    into `unsaved-launches` - B's source is this launch, so the import tab keeps comparing
    it (design「沒有新內容時」). M2: the step-6 B is the final one, so its source and its
    title are the ones saved here, and search lists the two sessions apart."""
    _, a, _ = _import(capsys, "--header", "title=表格排版")
    env.speak = False
    before = document_of(a)

    code, b, err = _fork(capsys, a)

    assert code == 0 and b.startswith("agora:") and b != a
    # R2: the step-6 line itself. The end-of-run line below also says `fork 已存成`, so
    # only the exact `[agora] `-prefixed form pins the one printed at step 6.
    assert f"[agora] fork 已存成 {b}" in err, "M7: B's id is said as soon as it is saved"
    assert f"沒有新內容；fork 已存成 {b}" in err
    assert header_of(b)["title"] == "表格排版-fork"
    assert h.agora_of(header_of(b))["source"]["session_id"] == env.launched[-1].agent_session_id, \
        "M12: B's source is this launch, or search would fold the two rows into one"
    assert cli.FORK_READY in body_of(b)
    assert not (paths().state / "unsaved-launches").exists(), \
        "B's own source is this launch, so the import tab must still compare it"
    assert document_of(a) == before
    assert on_drive(b).is_dir() and (on_drive(b) / "session.md").is_file()
    code, out, err = run(capsys, "search", "session", "--no-sync")
    assert {line.split()[0] for line in out.splitlines()} == {a, b}, "two rows, no merge"


def test_fork_across_agents_converts_the_history_and_appends_the_note(env, capsys):
    """Scenario「跨 agent fork」: A is opencode's, opened with claude - the history is
    converted, and the fork's two turns come after it."""
    _, a, _ = _import(capsys, "--header", "title=跨 agent")
    env.name = "claude"                     # the agent opening it is not the one that wrote it

    code, b, _ = _fork(capsys, a)

    assert code == 0 and b != a
    loaded = env.sessions[env.launched[-1].agent_session_id]
    assert loaded[0].startswith(cli.CONVERTED_NOTE)
    assert f"（以下來自 {a}，原本是 opencode 的對話）" in loaded[0]
    assert "把 CSV 轉成 Markdown 表格" in loaded[0]
    assert loaded[-3].startswith(f"從 {a}（title: 跨 agent，description: 把 CSV 轉成 Markdown 表格）fork")
    assert loaded[-2] == cli.FORK_READY and loaded[-1] == "接著做完了"
    assert not env.appended, "another agent: the raw is rebuilt, not appended to"
    assert header_of(b)["title"] == "跨 agent-fork"
    assert h.agora_of(header_of(b))["source"]["agent"] == "claude"


def test_fork_of_a_merge_reads_the_summary_and_drops_the_draft(env, capsys):
    """Scenario「fork 一個 merge 出來的 Session」: the agent gets the summary and the list
    of sources plus the fork note, and B has no `status: draft` of its own."""
    _, a, _ = _import(capsys, "--header", "title=甲")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert header_of(m)["status"] == "draft"

    code, forked, _ = _fork(capsys, m)

    assert code == 0 and forked not in (a, b, m)
    loaded = env.sessions[env.launched[-1].agent_session_id]
    assert loaded[0].startswith(cli.MERGE_NOTE.format(by="opencode/fake-model"))
    assert f"只有 B 內文" not in "\n".join(loaded)      # the sources' own text is not loaded
    assert loaded[-3].startswith(f"從 {m}（title: merge: 甲 + 讀取 CSV，"
                                 "description: 合併 2 個 Session：甲、讀取 CSV）fork")
    hdr = header_of(forked)
    assert "status" not in hdr and h.agora_of(hdr)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr)["parents"]] == [m]
    assert h.agora_of(hdr)["parents"][0]["raw_md5"] is None, "a merge has no raw of its own"


def test_forking_a_fork_again_appends_fork_to_the_title(env, capsys):
    """Scenario「fork 的 fork」: `X-fork-fork`, pointing at the first fork."""
    _, a, _ = _import(capsys, "--header", "title=實驗")
    _, b, _ = _fork(capsys, a)

    code, c, _ = _fork(capsys, b)

    assert code == 0 and c not in (a, b)
    assert header_of(c)["title"] == "實驗-fork-fork"
    assert [p["id"] for p in h.agora_of(header_of(c))["parents"]] == [b]
    assert env.sessions[env.launched[-1].agent_session_id][-3].startswith(f"從 {b}（title: 實驗-fork"), \
        "the first fork's turns are part of B's own raw"


def test_a_given_title_wins_over_the_suffix_and_stays(env, capsys, during_agent):
    """Scenario「指定 title」: the user's title is used as it is - on the step-6 B that is
    never written again (M15), in the pending record (M27), and after a write-back."""
    _, a, _ = _import(capsys, "--header", "title=表格排版")
    env.speak = False
    seen = {}

    def peek():
        [record] = [json.loads(p.read_text(encoding="utf-8")) for p in paths().pending.glob("*.json")]
        seen.update(record)

    during_agent(peek)
    code, b, _ = _fork(capsys, a, "--header", "title=實驗")

    assert code == 0 and header_of(b)["title"] == "實驗"
    assert seen["title"] == "實驗", "the pending record names B the way B is named"
    assert seen["header_updates"] == {"title": "實驗"}, "and keeps the user's overlay"
    run(capsys, "continue", "session", b, "--agent", "opencode", "--dir", "/tmp")
    assert header_of(b)["title"] == "實驗", "the write-back must not put `-fork` back"


def test_the_users_own_generated_and_sources_survive_step_six(env, capsys):
    """M4: `_fork_after_launch` runs after the user's overlay; a given `generated.*` or
    `sources` must not be overwritten by the automatic ones - most visibly when the agent
    is never written back and step 6 is all there is."""
    _, a, _ = _import(capsys)
    env.speak = False

    code, b, err = _fork(capsys, a, "--header", "generated.by=human:fatesaikou",
                         "--header", 'sources=[{id: "mybrain:note", title: "備註"}]')

    assert code == 0, err
    hdr = header_of(b)
    assert hdr["generated"]["by"] == "human:fatesaikou", hdr["generated"]
    assert hdr["sources"] == [{"id": "mybrain:note", "title": "備註"}], \
        "3.5: the user's overlay is applied last, sources and all"


def test_a_fork_whose_session_is_deleted_while_the_agent_runs_saves_the_work(env, capsys, during_agent):
    """Scenario「fork 途中新 Session 被刪掉」: the conversation becomes a session of its own
    as a `continue` pointing at B, exactly as a continue does; A is not touched."""
    _, a, _ = _import(capsys, "--header", "title=表格排版")
    gone = {}

    def delete_b():
        gone["id"] = _fork_session()["id"]
        shutil.rmtree(on_drive(gone["id"]))       # another machine deleted it
        store.forget_local(paths(), gone["id"].split(":")[1])

    during_agent(delete_b)
    code, out, err = _fork(capsys, a)

    assert code == 0 and out.startswith("agora:") and out != a and out != gone["id"]
    kept = [hdr for _u, hdr, _s in store.Index(paths()).search([]) if hdr["id"] != a]
    assert [hdr["id"] for hdr in kept] == [out]
    assert h.agora_of(kept[0])["relation"] == "continue"
    assert [p["id"] for p in h.agora_of(kept[0])["parents"]] == [gone["id"]]
    assert "接著做完了" in document_of(out), "the work is not thrown away"
    assert header_of(a)["title"] == "表格排版", "A is not touched by any of this"


def test_fork_then_import_of_the_original_updates_it_in_place(env, capsys):
    """Scenario「fork 之後再匯入原來源」: a fork is a copy with a source of its own, so A
    is still the main line (F9)."""
    _, a, _ = _import(capsys, "--header", "title=實驗")
    _, b, _ = _fork(capsys, a)
    env.sessions["ses_a"].append("來源端又聊了一句")

    code, out, _ = _import(capsys)

    assert code == 0 and out == a, "A was updated where it is; no second branch"
    assert header_of(a)["title"] == "實驗" == header_of(b)["title"].removesuffix("-fork")
    assert "來源端又聊了一句" in document_of(a)


def test_a_merge_child_still_makes_the_import_branch(env, capsys):
    """The other half of S7/F9: only a fork stops counting as a branch."""
    _, a, _ = _import(capsys)
    env.sessions["ses_b"] = ["另一個", "好"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    run(capsys, "merge", "session", a, b, "--agent", "opencode")
    env.sessions["ses_a"].append("又聊了一句")

    code, out, _ = _import(capsys)

    assert code == 0 and out != a, "a merge moved the conversation on: the old one is kept"
    assert [p["id"] for p in h.agora_of(header_of(out))["parents"]] == [a]


# --- what the fork is made of ---------------------------------------------------------

def test_b_is_this_launch_imported_and_saved_before_the_agent_starts(env, capsys, during_agent):
    """design「流程」6: B's source is the launch itself - never A's, or search would call
    the two rows one session (T7 F2) - and it is safe here before the agent runs."""
    _, a, _ = _import(capsys)
    seen = {}

    def peek():
        fork = _fork_session()
        seen.update(id=fork["id"], sources=[s["id"] for s in fork["sources"]],
                    generated=fork.get("generated"), outbox=sorted(store.outbox_ulids(paths())))

    during_agent(peek)
    _fork(capsys, a)

    launched = env.launched[-1].agent_session_id
    assert seen["sources"] == [f"opencode:{launched}", a]
    assert seen["generated"] == {"by": "opencode", "at": "2026-10-01T00:00:00Z"}
    assert seen["outbox"] == [], "Drive has it: the command is done when the session is safe here"
    assert hashlib.md5(env._exported(launched).raw).hexdigest() == header_of(seen["id"])["agora"]["raw"]["md5"]


def test_the_pending_record_says_it_is_a_fork_of_b(env, capsys, during_agent):
    """design「pending 記錄」: what a later finish needs, so an interrupted fork is
    finished as B and never as A."""
    _, a, _ = _import(capsys, "--header", "title=表格排版")
    seen = {}

    def peek():
        [record] = [json.loads(p.read_text(encoding="utf-8")) for p in paths().pending.glob("*.json")]
        seen.update(record, fork_md5=h.agora_of(_fork_session())["raw"]["md5"],
                    exported_md5=hashlib.md5(env._exported(env.launched[-1].agent_session_id).raw).hexdigest())

    during_agent(peek)
    _, b, _ = _fork(capsys, a)

    assert seen["fork"] is True
    assert seen["agora_id"] == b
    assert seen["parent"] == {"id": b, "raw_md5": seen["fork_md5"]}
    assert seen["saved_md5"] == seen["fork_md5"]
    assert seen["fork_md5"] == seen["exported_md5"], "the fork's md5 is the staged B's raw"
    assert seen["title"] == "表格排版-fork"
    assert seen["header_updates"] == {}
    assert seen["agent_session_id"] == env.launched[-1].agent_session_id
    assert a not in json.dumps(seen), "nothing in the record points at A any more"


def test_a_fork_whose_child_reported_no_saved_b_is_a_named_error(env, capsys, monkeypatch):
    """`saved_fork` is only taken on the fork path. An empty answer there means the
    shell cannot finish the fork and says so - a clear AgentError (exit 2), not the
    bare TypeError a `tuple(None)` would raise."""
    _, a, _ = _import(capsys)
    payload = {"launch": {"argv": ["true"], "cwd": "/tmp", "agent_session_id": "ses_b",
                          "before_count": 1, "env": {}},
               "parent_md5": None, "saved_md5": None,
               "fork_id": "agora:01TEST00000000000000000000", "fork_title": "x",
               "fork_md5": "0" * 32, "saved_fork": None}
    monkeypatch.setattr(cli, "_start_child", lambda paths_, state: payload)
    args = cli.build_parser().parse_args(
        ["continue", "session", a, "--agent", "opencode", "--fork",
         "--dir", "/tmp", "--sync-period", "0"])

    with pytest.raises(cli.AgentError) as e:
        cli.cmd_continue(args, paths())

    assert "saved_fork" in str(e.value) and "沒有" in str(e.value)


def _break_case_of(agora_id, entity="mybrain-plus"):
    """Rewrite A's own header to name an entity this version does not know - a header
    written by a newer agora, read by this one."""
    path = paths().mirror / agora_id.split(":")[1] / "session.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("case: mybrain:aistorage", f"case: {entity}:x"), encoding="utf-8")
    store.index_file(store.Index(paths()), path)


def test_a_header_this_version_does_not_know_stops_before_the_agent(env, capsys):
    """F16: A's own header is checked before `start_native`, so a `case` or `ref` from a
    newer version fails without leaving a session nobody is looking after."""
    _, a, _ = _import(capsys, "--header", "case=mybrain:aistorage")
    _break_case_of(a)

    code, out, err = _fork(capsys, a)

    assert code == 2 and out == "" and "mybrain-plus" in err
    assert not env.launched, "no agent session was opened"
    assert _session_ids() == [a], "and nothing new was saved"


def test_a_bad_header_argument_also_fails_before_the_agent(env, capsys):
    """The same check for the user's own `--header`: it is folded into B before the agent
    runs, so a typo does not cost a session either."""
    _, a, _ = _import(capsys)

    code, out, err = _fork(capsys, a, "--header", "refs=[mybrain-plus:x]")

    assert code == 2 and out == "" and "mybrain-plus" in err
    assert not env.launched and _session_ids() == [a]


def test_a_launch_with_no_session_id_saves_nothing(env, capsys, monkeypatch):
    """Step 4: without an agent session id there is nothing to point B at, so the fork is
    refused rather than saved as a session that names no source."""
    _, a, _ = _import(capsys)
    original = env.start_native

    def start_native(raw, workdir):
        launch = original(raw, workdir)
        return Launch(argv=launch.argv, cwd=launch.cwd, agent_session_id=None,
                      before_count=launch.before_count)

    monkeypatch.setattr(env, "start_native", start_native)
    code, out, err = _fork(capsys, a)

    assert code == 2 and out == "" and "session id" in err
    assert _session_ids() == [a], "B was never created"


def test_a_launch_that_does_not_match_what_was_loaded_saves_nothing(env, capsys, monkeypatch):
    """F3: B is this launch, so the export right after `start_native` has to be exactly
    what was loaded - otherwise the header would describe a session that is not there.
    M7: the words carry the agent session id and say it was never saved."""
    _, a, _ = _import(capsys)
    original = env.export

    def export(sid):
        exported = original(sid)
        return Exported(session_id=exported.session_id,
                        raw=exported.raw.replace("好的，三個步驟".encode(), "短少一則".encode()),
                        dir=exported.dir, title=exported.title,
                        created_at=exported.created_at, agent_version=exported.agent_version,
                        message_count=exported.message_count - 1)

    monkeypatch.setattr(env, "export", export)
    code, out, err = _fork(capsys, a)

    assert code == 2 and out == "" and "對不上" in err
    assert env.launched[-1].agent_session_id in err and "可以不管它" in err, \
        "M7: name the agent session that was left behind"
    assert _session_ids() == [a]


def test_a_launch_agora_cannot_read_back_names_the_leftover_session(env, capsys, monkeypatch):
    """M7: when the export itself is what fails, the message still names the agent
    session - otherwise the not-imported tab shows a stranger with no explanation."""
    _, a, _ = _import(capsys)

    def broken(sid):
        raise AgentError("opencode export 壞了")

    monkeypatch.setattr(env, "export", broken)
    code, out, err = _fork(capsys, a)

    assert code == 2 and out == ""
    assert env.launched[-1].agent_session_id in err
    assert "讀不回來" in err and "沒有存成 agora Session" in err
    assert _session_ids() == [a]


def test_verified_is_copied_into_the_fork(env, capsys):
    """The person's own 10-10 call: a fork is the same work under a new id, so B keeps
    A's `verified` instead of silently dropping it (only a merge's auto-filled `status`
    is still dropped)."""
    assert "verified" not in cli.FORK_DROP_KEYS
    _, a, _ = _import(capsys, "--header", "title=實驗")
    hdr = header_of(a)
    hdr["verified"] = ["human:fatesaikou"]
    path = paths().mirror / a.split(":")[1] / "session.md"
    path.write_text(h.dump_document(hdr, body_of(a)), encoding="utf-8")
    store.index_file(store.Index(paths()), path)

    _, b, _ = _fork(capsys, a)

    assert header_of(a)["verified"] == ["human:fatesaikou"], "A still says so"
    assert header_of(b)["verified"] == ["human:fatesaikou"], "and so does B"
    assert f"從 {a}（title: 實驗" in env.sessions[env.launched[-1].agent_session_id][-3], \
        "and the note does not mention it either"


def test_status_is_copied_from_a_plain_session(env, capsys):
    """M04: only a merge's auto-filled `draft` is dropped (5.4); a status the user set on
    an ordinary session is a user field like any other."""
    _, a, _ = _import(capsys, "--header", "status=wip")

    _, b, _ = _fork(capsys, a)

    assert header_of(b)["status"] == "wip"


def test_the_title_falls_back_to_the_description_then_the_id(env, capsys):
    """M14, F5: `A.title or A.description or A.id`, and the value is a string before the
    suffix goes on."""
    _, a, _ = _import(capsys)
    hdr = header_of(a)
    hdr["title"] = None
    _rewrite_header(a, hdr)

    _, b, _ = _fork(capsys, a)

    assert header_of(b)["title"] == "把 CSV 轉成 Markdown 表格-fork"

    hdr["description"] = None
    _rewrite_header(a, hdr)
    _, c, _ = _fork(capsys, a)
    assert header_of(c)["title"] == f"{a}-fork"


def test_an_int_title_from_a_header_file_becomes_a_string(env, capsys, tmp_path):
    """F5: YAML reads `title: 2026` as a number; the fork's title is a string anyway."""
    f = tmp_path / "hdr.yaml"
    f.write_text("title: 2026\n", encoding="utf-8")
    _, a, err = _import(capsys, "--header-file", str(f))
    assert header_of(a)["title"] == 2026

    _, b, _ = _fork(capsys, a)

    assert header_of(b)["title"] == "2026-fork"


def test_the_launch_is_read_back_with_its_directory_when_the_adapter_takes_one():
    """M17: claude is told where the launch ran, so the file it just wrote is read
    instead of a scan over every project; an adapter that takes no hint is called
    without one."""
    calls = []

    class Hinted:
        name = "claude"

        def export(self, session_id, hint_dir=None):
            calls.append((session_id, hint_dir))
            return Exported(session_id=session_id, raw=b"{}")

    launch = Launch(argv=[], cwd="/tmp/work", agent_session_id="uuid-1")
    exported = cli._export_launch(Hinted(), launch)
    assert calls == [("uuid-1", "/tmp/work")] and exported.session_id == "uuid-1"

    class Plain:
        name = "opencode"

        def export(self, session_id):
            calls.append((session_id, None))
            return Exported(session_id=session_id, raw=b"{}")

    cli._export_launch(Plain(), launch)
    assert calls[-1] == ("uuid-1", None)


def test_a_fork_rescue_carries_the_users_header(env, capsys, during_agent):
    """M9 (R3): the session saved out of a deleted B keeps the user's fields from B's own
    header - here the ones the fork copied off A - instead of resetting to the
    auto-generated header.

    Deliberately no `--header` on the fork: the record's `header_updates` is empty, so
    `_save`'s overlay cannot supply these and only the rescue's merge of the old header
    can (the review's F4 mutation has to make this red)."""
    _, a, _ = _import(capsys, "--header", "case=mybrain:aistorage",
                      "--header", "tags=[實驗]", "--header", "refs=[mybrain:note/x]",
                      "--header", "description=先做讀取")
    gone, seen = {}, {}

    def delete_b():
        [record] = [json.loads(p.read_text(encoding="utf-8"))
                    for p in paths().pending.glob("*.json")]
        seen["header_updates"] = record["header_updates"]
        gone["id"] = _fork_session()["id"]
        shutil.rmtree(on_drive(gone["id"]))     # only Drive: the header is still here
    during_agent(delete_b)

    code, out, err = _fork(capsys, a)

    assert seen["header_updates"] == {}, \
        "使用者這次沒有下 --header；這條路不靠 updates，只能靠從舊 header 抄"
    assert code == 0 and out not in (a, gone["id"])
    hdr = header_of(out)
    assert hdr["title"] == "把 CSV 轉成 Markdown 表格-fork"
    assert hdr["case"] == "mybrain:aistorage" and hdr["tags"] == ["實驗"]
    assert hdr["refs"] == ["mybrain:note/x"] and hdr["description"] == "先做讀取"


def test_a_rescued_merge_drops_its_draft_status(env, capsys, during_agent):
    """M9's exception: a merge's auto-filled `draft` does not come along to the rescue -
    a continued merge is that conversation now, not a summary (5.4)."""
    _, a, _ = _import(capsys, "--header", "title=甲")
    env.sessions["ses_b"] = ["讀取 CSV", "完成"]
    _, b, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    _, m, _ = run(capsys, "merge", "session", a, b, "--agent", "opencode")
    assert header_of(m)["status"] == "draft"

    def delete_m():
        shutil.rmtree(on_drive(m))             # only Drive: the header is still here
    during_agent(delete_m)

    code, out, err = run(capsys, "continue", "session", m, "--agent", "opencode", "--dir", "/tmp")

    assert code == 0 and out != m
    hdr = header_of(out)
    assert "status" not in hdr and h.agora_of(hdr)["relation"] == "continue"


def test_a_continue_rescue_carries_the_users_header(env, capsys, during_agent):
    """M9, the ordinary continue: a session written back in place carries the same
    guarantee when it is the one that turns out to be gone."""
    _, a, _ = _import(capsys, "--header", "case=mybrain:aistorage", "--header", "tags=[實驗]",
                      "--header", "refs=[mybrain:note/x]",
                      "--header", "verified=[{by: human:fatesaikou}]")

    def delete_a():
        shutil.rmtree(on_drive(a))              # only Drive: the header is still here
    during_agent(delete_a)

    code, out, err = run(capsys, "continue", "session", a, "--agent", "opencode", "--dir", "/tmp")

    assert code == 0 and out != a
    hdr = header_of(out)
    assert hdr["case"] == "mybrain:aistorage" and hdr["tags"] == ["實驗"]
    assert hdr["refs"] == ["mybrain:note/x"] and hdr["verified"] == [{"by": "human:fatesaikou"}]


def test_a_fork_refuses_a_session_the_cloud_lost(env, capsys):
    """B's parents must not point at a session another machine deleted (F14)."""
    _, a, _ = _import(capsys)
    shutil.rmtree(on_drive(a))
    assert store.sync(paths()).missing_in_cloud() == [a.split(":")[1]]

    code, out, err = _fork(capsys, a)

    assert code == 1 and "雲端沒有" in err and out == ""
    assert not env.launched and not env.appended


# --- what is printed and what it exits with ------------------------------------------

def test_the_exit_code_is_the_fork_save_when_the_agent_said_nothing(env, capsys, monkeypatch):
    """design「沒有新內容時」: the exit code is the one from saving B - 3 while it is only
    here."""
    _, a, _ = _import(capsys, "--header", "title=表格排版")
    env.speak = False
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")

    code, b, err = _fork(capsys, a)

    assert code == cli.EXIT_IN_OUTBOX and b.startswith("agora:") and b != a
    assert f"沒有新內容；fork 已存成 {b}" in err
    assert b.split(":")[1] in store.outbox_ulids(paths())


def test_the_exit_code_is_the_worse_of_the_two_writes(env, capsys, monkeypatch):
    """A fork writes twice: B, then whatever the agent said after. The id printed is the
    last one; the exit code is whichever of the two did not reach Drive (N13)."""
    _, a, _ = _import(capsys)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")

    code, b, err = _fork(capsys, a)

    assert code == cli.EXIT_IN_OUTBOX and "outbox" in err
    assert b.startswith("agora:") and b != a
    assert "接著做完了" in document_of(b)


@pytest.mark.parametrize("which,expected", [("the fork", 0), ("the write-back", cli.EXIT_IN_OUTBOX)])
def test_the_answer_follows_the_last_write_to_b_itself(env, capsys, monkeypatch, which, expected):
    """M6: the write-back's outbox entry replaces step 6's, so when both land on B the
    last one's answer is the truth - a fork save that reported failure and was then
    superseded is not worth an exit 3 (the outbox is empty by then)."""
    _, a, _ = _import(capsys)
    real_save = cli._save
    calls = []

    def flaky(paths, hdr, body, raw, **kwargs):
        calls.append(hdr["id"])
        saved = real_save(paths, hdr, body, raw, **kwargs)
        which_one = (which == "the fork") == (len(calls) == 1)
        return (saved[0], False) if which_one else saved   # "Drive did not take it"

    monkeypatch.setattr(cli, "_save", flaky)
    code, b, err = _fork(capsys, a)

    assert len(calls) == 2, "a fork writes twice: B, then the conversation after"
    assert calls[0] == calls[1] == b, "both writes land on B itself"
    assert code == expected, err
    assert b.startswith("agora:") and b != a, "the id printed is the last one written"


def test_a_rescue_to_another_session_takes_the_worse_of_the_two(env, capsys, monkeypatch, during_agent):
    """M6's other half: the write-back saved the work as C, not B - the two answers are
    about different sessions, so the one that failed cannot be forgotten."""
    _, a, _ = _import(capsys)
    real_save = cli._save
    calls = []
    gone = {}

    def delete_b():
        gone["id"] = _fork_session()["id"]
        shutil.rmtree(on_drive(gone["id"]))          # another machine deletes B
    during_agent(delete_b)

    def flaky(paths, hdr, body, raw, **kwargs):
        saved = real_save(paths, hdr, body, raw, **kwargs)
        calls.append(saved[0])
        if len(calls) == 1:                         # step 6's save of B
            return (saved[0], False)                # reported as not through
        return saved                                # the rescue's save went through

    monkeypatch.setattr(cli, "_save", flaky)
    code, out, err = _fork(capsys, a)

    assert len(calls) == 2, "the fork's save and the rescue's"
    assert out == calls[1] and out not in (a, gone["id"]), "the rescue's id is the answer"
    assert code == cli.EXIT_IN_OUTBOX, "the fork's failed save is not forgotten"
    assert header_of(out)["agora"]["relation"] == "continue"


def test_fork_is_continue_s_only(env, capsys):
    """F18: like `--dir`, `--fork` only means something to continue."""
    _, a, _ = _import(capsys, "--fork")
    assert h.agora_of(header_of(a))["relation"] == "import"
    assert _session_ids() == [a]

    code, out, _ = run(capsys, "continue", "session", a, "--agent", "opencode", "--dir", "/tmp")

    assert code == 0 and out == a, "a plain continue still writes back into A"
    assert h.agora_of(header_of(a))["relation"] == "import"
    assert not env.appended


def test_continuing_and_merging_a_fork_keeps_it_a_fork(env, capsys):
    """design「relation: fork 對其他地方的影響」: a plain continue writes B back with
    `fork` still on it, and B merges like any other session."""
    _, a, _ = _import(capsys, "--header", "title=實驗")
    _, b, _ = _fork(capsys, a)

    code, out, _ = run(capsys, "continue", "session", b, "--agent", "opencode", "--dir", "/tmp")

    assert code == 0 and out == b, "still B, not a third session"
    assert h.agora_of(header_of(b))["relation"] == "fork"
    env.sessions["ses_b"] = ["另一件事", "好"]
    _, c, _ = run(capsys, "import", "session", "--external-session-id", "ses_b", "--agent", "opencode")
    code, merged, _ = run(capsys, "merge", "session", f"{b},{c}", "--agent", "opencode")

    assert code == 0 and merged not in (b, c)
    assert [p["id"] for p in h.agora_of(header_of(merged))["parents"]] == [b, c]


def test_search_shows_the_fork_as_a_session_of_its_own(env, capsys):
    """T7 F2: B's source is the launch, so search does not fold it into A's row."""
    _, a, _ = _import(capsys)
    _, b, _ = _fork(capsys, a)

    code, out, err = run(capsys, "search", "session", "--no-sync")

    kinds = {line.split()[0]: line.split()[2] for line in out.splitlines()}
    assert kinds == {a: "opencode", b: "opencode"}     # the source's agent, not `fork`
    assert "來自同一個來源 Session" not in err
    assert not (paths().state / "unsaved-launches").exists()


def test_a_fork_with_nothing_to_say_still_reaches_drive_as_itself(env, capsys, monkeypatch):
    """The whole point of the offline rule, in its smallest form: B is created and left
    alone, Drive gets B, and there is no second session anywhere."""
    _, a, _ = _import(capsys)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    env.speak = False

    code, b, _ = _fork(capsys, a)

    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    assert store.upload_batch(store.Drive(paths()), paths()) == []      # the line is back
    assert code == cli.EXIT_IN_OUTBOX and b != a
    assert sorted(p.name for p in (Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions").iterdir()) \
        == sorted([a.split(":")[1], b.split(":")[1]])


def test_an_offline_fork_reaches_drive_as_itself(env, capsys, monkeypatch):
    """Scenario「離線 fork」: B waits in the outbox, the agent talks, and when the line
    comes back Drive gets B itself - not a copy saved under another id, and no「被別台
    刪掉」."""
    _, a, _ = _import(capsys, "--header", "title=表格排版")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")

    code, b, _ = _fork(capsys, a)

    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    ulid = b.split(":")[1]
    assert code == cli.EXIT_IN_OUTBOX and ulid in store.outbox_ulids(paths()), "B is not on Drive yet"
    assert store.upload_batch(store.Drive(paths()), paths()) == []      # the line is back
    assert ulid not in store.outbox_ulids(paths())
    assert sorted(p.name for p in (Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions").iterdir()) \
        == sorted([a.split(":")[1], ulid]), "B itself, and nothing saved again"
    assert "接著做完了" in (on_drive(b) / "session.md").read_text(encoding="utf-8")


# --- the real adapters through the fork (review probes P1, P2) ----------------------
# F3's count check and M17's `hint_dir` have no unit test otherwise: the fakes do not
# have a file to read back. The agents are the fake CLIs; only the adapters are real.

FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fakes" / "fake_claude.py"
FAKE_OPENCODE = Path(__file__).resolve().parent.parent / "fakes" / "fake_opencode.py"
CL_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
OC_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
CL_SID = "11111111-2222-4333-8444-555555555555"


def _wrapper(tmp_path, name, target):
    w = tmp_path / name
    w.write_text(f"#!/bin/sh\nexec {sys.executable} {target} \"$@\"\n")
    w.chmod(0o755)
    return w


@pytest.mark.parametrize("mode", ["noop", "append"])
def test_the_real_claude_adapter_forks(env, capsys, tmp_path, monkeypatch, mode):
    """P1: through cmd_continue with the real adapter, B's raw md5 is the file claude
    really holds - and the count check did not stop a good launch."""
    from agora.agents import claude as C
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    proj = home / ".claude" / "projects" / C.encode_project_dir("/tmp/my-proj.v2")
    proj.mkdir(parents=True)
    shutil.copy(CL_FIX / "cl-basic.jsonl", proj / f"{CL_SID}.jsonl")
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(_wrapper(tmp_path, "claude", FAKE_CLAUDE)))
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    work = tmp_path / "work"
    work.mkdir()

    code, a, err = run(capsys, "import", "session", "--external-session-id", CL_SID,
                       "--agent", "claude")
    assert code == 0, err
    before = document_of(a)

    code, b, err = run(capsys, "continue", "session", a, "--agent", "claude", "--fork",
                       "--dir", str(work))

    assert code == 0, err
    assert b != a and b.startswith("agora:")
    hdr = header_of(b)
    assert h.agora_of(hdr)["relation"] == "fork"
    sid = h.agora_of(hdr)["source"]["session_id"]
    jsonl = home / ".claude" / "projects" / C.encode_project_dir(work) / f"{sid}.jsonl"
    assert jsonl.is_file(), "the launch wrote its own session"
    latest = C.ADAPTER.export(sid, hint_dir=str(work))          # M17: found via the dir
    assert h.agora_of(hdr)["raw"]["md5"] == hashlib.md5(latest.raw).hexdigest()
    assert document_of(a) == before
    if mode == "noop":
        assert "沒有新內容；fork 已存成" in err
    else:
        assert "ZZSAY" in document_of(b)


def test_the_real_opencode_adapter_forks_v2_and_refuses_v1_honestly(env, capsys, tmp_path, monkeypatch):
    """Step 2 (2.3/2.5): a v2 raw forks via the flat append (tool calls kept, new
    rows carry A's model), and a v1 raw is refused honestly before anything is
    imported - not a bare SchemaError, and the Session stays untouched."""
    from agora.agents import opencode as oc
    fh = tmp_path / "fh"
    (fh / "opencode-sessions").mkdir(parents=True)
    shutil.copyfile(OC_FIX / "oc-v2-basic.json", fh / "opencode-sessions" / "default.json")
    shutil.copyfile(OC_FIX / "oc-basic.json", fh / "opencode-sessions" / "old.json")
    monkeypatch.setenv("FAKE_HOME", str(fh))
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(_wrapper(tmp_path, "opencode", FAKE_OPENCODE)))
    monkeypatch.setattr(cli, "load_agent", lambda name: oc.ADAPTER)
    work = tmp_path / "work"
    work.mkdir()

    code, importing, err = run(capsys, "import", "session", "--external-session-id",
                               "default", "--agent", "opencode")
    assert code == 0, err
    before = document_of(importing)
    code, forked, err = run(capsys, "continue", "session", importing, "--agent", "opencode",
                            "--fork", "--dir", str(work))
    assert code == 0, err
    assert forked != importing
    assert document_of(importing) == before  # A untouched
    a_raw = json.loads(store.fetch_raw(paths(), store.Drive(paths()),
                                       importing.split(":")[1], header_of(importing)))
    b_raw = json.loads(store.fetch_raw(paths(), store.Drive(paths()),
                                       forked.split(":")[1], header_of(forked)))
    assert len(b_raw["messages"]) == len(a_raw["messages"]) + 2
    assert [m.get("type") for m in b_raw["messages"][-2:]] == ["user", "assistant"]
    assert any(c.get("type") == "tool" and c.get("name") == "read"
               for m in b_raw["messages"] for c in (m.get("content") or []))
    last_a = next(m for m in reversed(a_raw["messages"]) if m.get("type") == "assistant")
    assert b_raw["messages"][-1]["model"] == last_a["model"]

    code, old, err = run(capsys, "import", "session", "--external-session-id",
                         "old", "--agent", "opencode")
    assert code == 0, err
    old_document = document_of(old)
    code, _b, err = run(capsys, "continue", "session", old, "--agent", "opencode",
                        "--fork", "--dir", str(work))
    assert code != 0 and ("1.x" in err and "重新匯入" in err)  # honest, not SchemaError
    assert document_of(old) == old_document


# --- the shared fakes answer the whole protocol (C12) ---------------------------------

def _fake_from(path: str, name: str = "FakeAgent"):
    """One of the fakes the other command tests share, loaded from its own file."""
    import importlib.util

    module = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location(
        f"_forkcheck_{path.replace('.', '_')}", Path(__file__).with_name(path)))
    spec.loader.exec_module(module)
    return getattr(module, name)


@pytest.mark.parametrize("path", ["test_cli.py", "test_cli_more.py", "test_tui.py"])
def test_the_shared_fakes_implement_append_native(path):
    """`append_native` joined the Agent protocol (e080004), so every fake that stands in
    for an agent implements it - otherwise a test that forks later finds out at the one
    moment it cannot report anything useful (C12)."""
    fake = _fake_from(path)
    assert callable(getattr(fake, "append_native", None)), f"{path}'s fake has no append_native"


@pytest.mark.parametrize("path", ["test_cli.py", "test_cli_more.py", "test_tui.py"])
def test_the_shared_fakes_append_onto_the_raw_without_touching_it(path):
    """What a fork needs of them: A's own turns come through unchanged and the new ones
    come after, so the same-agent path keeps the raw it had. The TUI fake takes its
    arguments differently; nothing here needs them."""
    cls = _fake_from(path)
    try:
        fake = cls()
    except TypeError:
        fake = cls("opencode", [])      # the TUI fake is a listing fake and takes these
    # the shape all three read: the user turn at the even position
    raw = json.dumps({"m": ["問題", "回答"]}, ensure_ascii=False).encode()
    before = fake.turns(raw)

    appended = fake.append_native(raw, [("user", ["新的"]), ("assistant", ["好的"])])

    assert fake.turns(appended) == [*before, ("user", ["新的"]), ("assistant", ["好的"])]
    assert before[0][1] == ["問題"] and before[1][1] == ["回答"], "A's own lines, untouched"
