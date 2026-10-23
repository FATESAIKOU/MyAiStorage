"""#190: reopening a Session with `agora continue` keeps its Claude session id and its lines.

Before, `start_native` gave the reopened conversation a new uuid and re-encoded every line, so
the first round after a reopen was cut whole again (nothing of the old segments matched).
Now a continue that writes back to the same Session resumes the same id and the lines that
already say what they should come back as the original strings; only the new tail is sent.

Everything runs on the fake rclone with `AGORA_RAW_CHUNK=4096` and a fake Claude home. The one
test that starts the real `claude` is opt-in (`AGORA_TEST_REAL_CLAUDE=1`).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C
from test_agent_claude_increment import SID, Session, _check, _on_drive, _round, _turn, paths, src  # noqa: F401
from test_cli_more import paths as cli_paths, run
from test_segment_write import C as CH, _drive_folder, _joined, env  # noqa: F401

WORKDIR = Path("/golden/inc")        # what `_obj` writes into every line's `cwd`


def _reopen(env, paths, ulid, **kw):
    """What a continue loads: the raw as Drive holds it, handed to the adapter."""
    return C.ClaudeAgent().start_native(_joined(env, ulid), kw.pop("workdir", WORKDIR), **kw)


def _sent(env, paths, ulid) -> tuple[int, int]:
    """(segments, bytes) a round puts on Drive: what appeared there since the last look."""
    folder = _drive_folder(env, ulid)
    before = {p.name for p in folder.iterdir()} if folder.is_dir() else set()
    assert store.upload_batch(store.Drive(paths), paths) == []
    new = [p for p in folder.iterdir() if p.name not in before and p.name.endswith(".part")]
    return len(new), sum(p.stat().st_size for p in new)


def _two_rounds(env, paths, s):
    ulid = h.new_ulid()
    s.turns(60)
    _round(paths, ulid, s)
    s.turns(10)
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)
    return ulid


def _launched_once(env, paths, ulid, s: Session, proj: Path):
    """An earlier continue of ours: it wrote the Claude file itself (and left its marker)."""
    _gone(s, proj)
    assert _reopen(env, paths, ulid, keep_id=SID).agent_session_id == SID
    assert (proj / f"{SID}.jsonl").exists()


def _gone(s: Session, proj: Path):
    """The machine the user left: the Claude file is not here any more."""
    s.path.unlink()
    shutil.rmtree(proj / s.sid, ignore_errors=True)


# --- step 1: lines that need no change are not re-encoded ---------------------------------------


ODD = ['{"type":"user","sessionId":"%s","cwd":"/golden/inc","message":{"content":"a\\/b \\u00e9 é 1E5"},"n":1E5,"x":1.0}',
       '{"type":"summary","sessionId":"%s","summary":"緊湊\\ud83d\\ude00 \\u4e2d"}']


def test_lines_that_already_say_the_right_thing_come_back_byte_for_byte():
    lines = [line % SID for line in ODD]
    assert C._rewrite_lines(lines, SID, "/golden/inc") == lines


def test_a_line_that_has_to_change_is_the_only_one_rewritten():
    lines = [ODD[0] % SID, ODD[1] % "another-id", ODD[0].replace("/golden/inc", "/elsewhere") % SID]
    out = C._rewrite_lines(lines, SID, "/golden/inc")
    assert out[0] == lines[0]
    assert json.loads(out[1])["sessionId"] == SID and json.loads(out[1])["summary"] == json.loads(lines[1])["summary"]
    assert json.loads(out[2])["cwd"] == "/golden/inc"
    assert out[1] != lines[1] and out[2] != lines[2]


def test_a_line_with_a_lone_surrogate_that_must_change_does_not_crash_the_write(tmp_path):
    """#191: an escaped lone surrogate used to make `write_text` raise once the line was re-encoded."""
    bad = '{"type":"summary","sessionId":"old","summary":"x\\ud83dy"}'
    out = C._rewrite_lines([bad], SID, "/w")
    (tmp_path / "f.jsonl").write_text("\n".join(out) + "\n", encoding="utf-8")        # must not raise
    assert json.loads(out[0])["sessionId"] == SID and json.loads(out[0])["summary"] == "x\ud83dy"


def test_a_reopened_file_is_the_uploaded_bytes_odd_formatting_and_all(env, src, paths, tmp_path):
    s = Session(src)
    s.turns(40)
    with open(s.path, "a", encoding="utf-8") as f:
        f.write((ODD[0] % SID) + "\n" + (ODD[1] % SID) + "\n")
    s.turns(3)
    original = s.path.read_bytes()
    ulid = h.new_ulid()
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)
    _gone(s, src)
    launch = _reopen(env, paths, ulid, keep_id=SID)
    assert launch.agent_session_id == SID
    assert (src / f"{SID}.jsonl").read_bytes() == original


# --- step 2: the same id, and what it saves ---------------------------------------------------------


def test_a_reopen_resumes_the_same_session_id(env, src, paths, monkeypatch):
    monkeypatch.delenv("AGORA_CLAUDE_CMD", raising=False)
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    _launched_once(env, paths, ulid, s, src)
    launch = _reopen(env, paths, ulid, keep_id=SID)
    assert launch.agent_session_id == SID
    assert launch.argv[-2:] == ["--resume", SID]
    assert launch.cwd == str(WORKDIR)


def test_the_first_round_after_a_reopen_sends_the_new_segments_and_the_tail_only(env, src, paths):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    assert _sent(env, paths, ulid)[0] >= 0
    seen_before = {seg["file"] for seg in store.Index(paths).header(ulid)["agora"]["raw"]["segments"]}
    _launched_once(env, paths, ulid, s, src)
    _reopen(env, paths, ulid, keep_id=SID)                   # our own file, identical: used as it is
    s.turns(6)                                               # what the user does in the reopened session
    inc, block = _round(paths, ulid, s)
    assert inc.append_from is not None, "the reopened prefix must still be the uploaded prefix"
    segs, sent = _sent(env, paths, ulid)
    grown = inc.exported.raw_size - inc.append_from
    assert segs <= math.ceil(grown / CH) + 1 and sent < inc.exported.raw_size / 3
    _check(env, paths, ulid, s, block)
    assert {seg["file"] for seg in block["segments"]} >= seen_before - {sorted(seen_before)[-1]}


def test_when_the_claude_file_is_gone_it_is_written_from_the_raw_and_the_next_round_is_still_an_append(env, src, paths):
    """The file is not there (a new Claude home), so it is written from the raw: the same bytes.
    The local cursor is still here; with none (another machine) the first round is whole - not this ticket."""
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    cursor = store.read_cursor(paths, ulid)
    _gone(s, src)
    launch = _reopen(env, paths, ulid, keep_id=SID)
    assert launch.agent_session_id == SID
    s.turns(6)
    inc, block = _round(paths, ulid, s)
    assert cursor["agent_session_id"] == SID and inc.append_from is not None
    assert _sent(env, paths, ulid)[0] <= math.ceil((inc.exported.raw_size - inc.append_from) / CH) + 1
    _check(env, paths, ulid, s, block)


def test_without_keep_id_it_is_cut_whole_as_before(env, src, paths):
    """The old behaviour, and the measurement the ticket asks for (before / after)."""
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    _sent(env, paths, ulid)
    launch = _reopen(env, paths, ulid)                       # a fork, a merge: a new id, every line re-encoded
    assert launch.agent_session_id != SID
    s2 = Session(src, launch.agent_session_id)
    s2.n = s.n
    s2.turns(6)
    inc = C.ClaudeAgent().export_increment(launch.agent_session_id, None, store.read_cursor(paths, ulid))
    assert inc.append_from is None


# --- what still falls back to the whole raw ---------------------------------------------------------


@pytest.mark.parametrize("what", ["prefix_byte", "compact", "rewrite_line"])
def test_anything_but_a_plain_append_is_still_cut_whole(env, src, paths, what):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    _sent(env, paths, ulid)
    lines = s.path.read_text(encoding="utf-8").splitlines()
    if what == "prefix_byte":                                # one byte inside the sealed part
        assert "問題" in lines[2]
        lines[2] = lines[2].replace("問題", "問提", 1)
    elif what == "compact":                                  # /compact: the early lines are gone
        lines = lines[40:]
    elif what == "rewrite_line":
        lines[5] = lines[5].replace("回答", "回客", 1)
    s.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    s.turns(4)
    inc, block = _round(paths, ulid, s)
    assert inc.append_from is None
    _check(env, paths, ulid, s, block)


def test_a_different_workdir_points_the_cwds_at_it_and_cuts_the_raw_whole_once(env, src, paths):
    """A `cwd` outside the new workdir (another machine, another path) is rewritten as a fork's is:
    the prefix changes for this one reopen, and `source.dir` follows the machine."""
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    _sent(env, paths, ulid)
    _gone(s, src)
    other = Path("/golden/elsewhere")
    launch = _reopen(env, paths, ulid, keep_id=SID, workdir=other)
    assert launch.agent_session_id == SID
    moved = C.find_jsonl(SID, other)
    assert {json.loads(x)["cwd"] for x in moved.read_text(encoding="utf-8").splitlines()} == {"/golden/elsewhere"}
    s2 = Session(moved.parent)
    s2.n = s.n
    s2.turns(4)
    inc = C.ClaudeAgent().export_increment(SID, other, store.read_cursor(paths, ulid))
    assert inc.append_from is None
    assert inc.exported.dir == "/golden/elsewhere"


def test_subdirectories_of_the_workdir_keep_their_cwd_and_the_next_round_is_an_append(env, src, paths):
    path = src / f"{SID}.jsonl"
    _write_real(path, SID, 1, 12)
    s = Session(src)
    ulid = h.new_ulid()
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)
    original = path.read_bytes()
    _gone(s, src)
    assert _reopen(env, paths, ulid, keep_id=SID).agent_session_id == SID
    assert path.read_bytes() == original
    _write_real(path, SID, 13, 16)
    inc, block2 = _round(paths, ulid, s)
    assert inc.append_from is not None
    _check(env, paths, ulid, s, block2)


def test_a_different_file_under_the_same_id_is_never_overwritten(env, src, paths, capfd):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    s.turns(2)                                               # the user went on in claude, outside agora
    theirs = s.path.read_bytes()
    launch = _reopen(env, paths, ulid, keep_id=SID)
    assert s.path.read_bytes() == theirs, "never overwrite"
    assert launch.agent_session_id != SID and launch.argv[-1] == launch.agent_session_id
    assert (src / f"{launch.agent_session_id}.jsonl").exists()
    err = capfd.readouterr().err
    assert SID in err and "無法原樣沿用" in err and "內容不同" not in err   # neutral: it may not be the user's edit


def test_a_file_under_the_same_id_in_another_project_is_a_collision_too(env, src, paths):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    launch = _reopen(env, paths, ulid, keep_id=SID, workdir=Path("/golden/elsewhere"))
    assert launch.agent_session_id != SID and s.path.exists()


def test_a_sidecar_file_that_differs_is_a_collision_and_stays(env, src, paths):
    s = Session(src)
    s.sidecar("subagents/a.jsonl", _turn(1, 50, SID))
    ulid = _two_rounds(env, paths, s)
    _launched_once(env, paths, ulid, s, src)                 # the jsonl is ours and identical; only the sidecar differs
    mine = src / SID / "subagents/a.jsonl"
    mine.write_text("theirs\n", encoding="utf-8")
    launch = _reopen(env, paths, ulid, keep_id=SID)
    assert launch.agent_session_id != SID and mine.read_text(encoding="utf-8") == "theirs\n"


@pytest.mark.parametrize("bad", ["../../x", "not-a-uuid", "", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"])
def test_an_id_that_is_not_a_claude_session_id_is_not_used_as_a_file_name(env, src, paths, bad):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    launch = _reopen(env, paths, ulid, keep_id=bad)
    assert launch.agent_session_id != bad and re.fullmatch(r"[0-9a-f-]{36}", launch.agent_session_id)


# --- R1: a session file the user made is never resumed by us, even holding the same bytes ------------


def _stat(path: Path):
    st = path.stat()
    return path.read_bytes(), st.st_mtime_ns, st.st_ino


def test_the_users_own_session_file_is_not_resumed_even_when_it_holds_the_same_bytes(env, src, paths):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)                        # what an import leaves: the user's file, unchanged
    before = _stat(s.path)
    launch = _reopen(env, paths, ulid, keep_id=SID)
    assert launch.agent_session_id != SID, "the user's Claude may still be open on that file"
    assert _stat(s.path) == before                           # not a byte, not the mtime, not even replaced
    assert not C._launched_marker(SID).exists()


def test_a_file_we_wrote_and_a_marker_for_another_path_are_told_apart(env, src, paths):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    _launched_once(env, paths, ulid, s, src)
    assert C._launched_by_agora(SID, src / f"{SID}.jsonl")
    assert not C._launched_by_agora(SID, Path("/golden/elsewhere") / f"{SID}.jsonl")
    before = _stat(src / f"{SID}.jsonl")
    assert _reopen(env, paths, ulid, keep_id=SID).agent_session_id == SID        # ours: resumed, not rewritten
    assert _stat(src / f"{SID}.jsonl") == before


def test_every_launch_marks_the_file_it_wrote_so_the_next_reopen_can_resume_it(env, src, paths):
    """The new id of a collision is a file of ours too: the Session's source id becomes it."""
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    first = _reopen(env, paths, ulid, keep_id=SID)           # the user's file is in the way: a new id
    assert first.agent_session_id != SID
    assert C._launched_by_agora(first.agent_session_id, src / f"{first.agent_session_id}.jsonl")


# --- N1: the plan is a look, the write is a claim ---------------------------------------------------


def _race(monkeypatch, make):
    real = C.ClaudeAgent._plan_same_id

    def plan(self, keep_id, main, aux, workdir_str, subdir):
        got = real(self, keep_id, main, aux, workdir_str, subdir)
        make(subdir)                                          # somebody else gets in between
        return got
    monkeypatch.setattr(C.ClaudeAgent, "_plan_same_id", plan)


def test_a_file_that_appears_between_the_look_and_the_write_is_not_used_or_overwritten(env, src, paths, monkeypatch):
    s = Session(src)
    ulid = _two_rounds(env, paths, s)
    _gone(s, src)
    theirs = b'{"somebody":"else"}\n'
    _race(monkeypatch, lambda d: (d / f"{SID}.jsonl").write_bytes(theirs))
    with pytest.raises(C.AgentError):
        _reopen(env, paths, ulid, keep_id=SID)
    assert (src / f"{SID}.jsonl").read_bytes() == theirs
    assert not C._launched_marker(SID).exists()


def test_a_sidecar_file_that_appears_in_between_is_not_overwritten_either(env, src, paths, monkeypatch):
    s = Session(src)
    s.sidecar("subagents/a.jsonl", _turn(1, 50, SID))
    ulid = _two_rounds(env, paths, s)
    _gone(s, src)

    def make(d):
        (d / SID / "subagents").mkdir(parents=True, exist_ok=True)
        (d / SID / "subagents/a.jsonl").write_text("theirs\n", encoding="utf-8")
    _race(monkeypatch, make)
    with pytest.raises(C.AgentError):
        _reopen(env, paths, ulid, keep_id=SID)
    assert (src / SID / "subagents/a.jsonl").read_text(encoding="utf-8") == "theirs\n"


# --- the cli hands keep_id only to a continue that writes back in place ------------------------------


class _Spy(C.ClaudeAgent):
    calls: list = []

    def start_native(self, raw, workdir, **kw):
        _Spy.calls.append(dict(kw))
        return super().start_native(raw, workdir, **kw)


@pytest.fixture
def imported(env, src, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_agent", lambda name: _Spy())
    _Spy.calls = []
    s = Session(src)
    s.turns(6)
    code, out, _ = run(capsys, "import", "session", "--external-session-id", SID, "--agent", "claude")
    assert code == 0
    return out.split(":")[1]


def _state(ulid, *, fork):
    return {"agent": "claude", "source_id": f"agora:{ulid}", "parent": store.Index(cli_paths()).header(ulid),
            "fork": fork, "updates": {}, "add_tags": [], "workdir": str(WORKDIR),
            "headless": False, "headless_port": None, "harness_file": None}


def test_a_plain_continue_keeps_the_id_and_a_fork_does_not(imported):
    p = cli_paths()
    cli._load_for_launch(p, _state(imported, fork=False))
    assert _Spy.calls[-1].get("keep_id") == SID
    cli._load_for_launch(p, _state(imported, fork=True))
    assert "keep_id" not in _Spy.calls[-1]


def test_a_continue_of_a_merge_does_not_pass_keep_id(imported, monkeypatch):
    state = _state(imported, fork=False)
    state["parent"] = json.loads(json.dumps(state["parent"]))
    agora = state["parent"]["agora"]
    agora["relation"] = "merge"
    agora["merge"] = {"by": "claude", "sources": []}
    monkeypatch.setattr(cli, "_require_sections", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_body_for", lambda *a, **k: "## 重點\n合併後的內容\n")
    _Spy.calls = []
    cli._load_for_launch(cli_paths(), state)
    assert len(_Spy.calls) == 1 and "keep_id" not in _Spy.calls[0]


def test_a_continue_into_another_agent_does_not_pass_keep_id(imported, monkeypatch):
    class Other(_Spy):
        name = "opencode"

    monkeypatch.setattr(cli, "load_agent", lambda name: Other())
    try:
        cli._load_for_launch(cli_paths(), _state(imported, fork=False))
    except Exception:
        pass                                                  # the fake cannot read claude's raw; only the call matters
    assert all("keep_id" not in c for c in _Spy.calls)


# --- #195: a line Claude wrote without a sessionId gets none from us -----------------------------------
#
# The shapes follow what a real Claude Code session holds (keys and kinds only, every value made up):
# bookkeeping lines with a sessionId and no cwd, turns with both, and a file-history-snapshot with
# neither a sessionId nor a cwd.

_C = (",", ":")                                              # Claude writes compact JSON


def _line(**o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=_C)


def _real_turn(sid: str, n: int, cwd="/golden/inc") -> list[str]:
    u1, a1, u2, a2, a3 = (f"{n:04d}{k}000-1111-4222-8333-444444444444" for k in range(5))
    common = dict(cwd=cwd, entrypoint="cli", gitBranch="main", isSidechain=False, sessionId=sid,
                  session_id=sid, slug="fake-slug", timestamp="2026-10-20T00:00:00.000Z", userType="external",
                  version="2.1.286")
    return [
        _line(type="queue-operation", operation="enqueue", content=f"問題 {n}", sessionId=sid,
              timestamp="2026-10-20T00:00:00.000Z"),
        _line(type="user", uuid=u1, parentUuid=None, promptId=f"p{n}", message={"role": "user", "content": f"問題 {n} a\\/b"}, **common),
        _line(type="assistant", uuid=a1, parentUuid=u1, requestId=f"r{n}", effort="medium",
              message={"role": "assistant", "model": "m", "content": [
                  {"type": "thinking", "thinking": "想 " + "x" * 80, "signature": "fakesig"},
                  {"type": "text", "text": f"回答 {n} " + "y" * 120},
                  {"type": "tool_use", "id": f"tu{n}", "name": "Bash", "input": {"command": "echo 1E5"}}]}, **common),
        _line(type="user", uuid=u2, parentUuid=a1, sourceToolAssistantUUID=a1, toolUseResult={"stdout": "1E5"},
              message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{n}", "content": "1E5"}]},
              **common),
        _line(type="assistant", uuid=a2, parentUuid=u2, message={"role": "assistant", "model": "m",
              "content": [{"type": "text", "text": "好 " + "z" * 60}]}, **common),
        _line(type="system", uuid=a3, parentUuid=a2, subtype="turn_duration", durationMs=12, isMeta=False,
              messageCount=5, **common),
        _line(type="file-history-snapshot", messageId=a1, isSnapshotUpdate=False,       # no sessionId, no cwd
              snapshot={"messageId": a1, "trackedFileBackups": {}, "timestamp": "2026-10-20T00:00:00.000Z"}),
        _line(type="last-prompt", lastPrompt=f"問題 {n}", leafUuid=a3, sessionId=sid),
    ]


def _cd(n: int) -> str:
    """The user cds into a subdirectory and back, several times over the session."""
    return "/golden/inc/pkg/sub" if n % 6 in (3, 4) else "/golden/inc"


def _write_real(path: Path, sid: str, first: int, last: int):
    with open(path, "a", encoding="utf-8") as f:
        for n in range(first, last + 1):
            f.write("\n".join(_real_turn(sid, n, _cd(n))) + "\n")


def _has_no_session_id(line: str) -> bool:
    return "sessionId" not in json.loads(line)


def test_a_line_without_a_session_id_comes_back_as_the_original_string():
    lines = _real_turn(SID, 1)
    snaps = [x for x in lines if _has_no_session_id(x)]
    assert len(snaps) == 1 and json.loads(snaps[0])["type"] == "file-history-snapshot"
    out = C._rewrite_lines(lines, SID, "/golden/inc")
    assert out == lines


def test_a_line_without_a_session_id_is_kept_byte_for_byte_whatever_the_new_id():
    """A fork, a merge or a cross-agent load: every other line takes the new id, this one is not touched."""
    lines = _real_turn(SID, 1)
    out = C._rewrite_lines(lines, "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff", "/golden/inc")
    for before, after in zip(lines, out):
        if _has_no_session_id(before):
            assert after == before
        else:
            assert json.loads(after)["sessionId"] == "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    assert sum(_has_no_session_id(x) for x in out) == 1


def test_a_line_without_a_session_id_still_gets_its_cwd_rewritten_when_it_has_one():
    line = '{"type":"x","cwd":"/elsewhere","n":1E5}'
    out = C._rewrite_lines([line], SID, "/golden/inc")
    o = json.loads(out[0])
    assert o["cwd"] == "/golden/inc" and "sessionId" not in o


def test_a_fork_load_leaves_the_snapshot_lines_of_the_file_it_writes_alone(env, src, paths):
    raw = json.dumps({"main": _real_turn(SID, 1) + _real_turn(SID, 2)}, ensure_ascii=False).encode("utf-8")
    launch = C.ClaudeAgent().start_native(raw, WORKDIR)                # no keep_id: a fork, merge, other agent
    assert launch.agent_session_id != SID
    written = (src / f"{launch.agent_session_id}.jsonl").read_text(encoding="utf-8").splitlines()
    snaps = [x for x in written if json.loads(x)["type"] == "file-history-snapshot"]
    assert len(snaps) == 2 and all(_has_no_session_id(x) for x in snaps)
    assert all(json.loads(x)["sessionId"] == launch.agent_session_id for x in written if not _has_no_session_id(x))


def test_a_real_shaped_session_reopened_twice_is_a_whole_cut_once_and_then_an_append(env, src, paths):
    """The user's own file is in the way the first time (no marker of ours): a new id, a whole raw.
    The second time the file is ours and holds the uploaded bytes: same id, only the new tail is sent."""
    path = src / f"{SID}.jsonl"
    _write_real(path, SID, 1, 40)
    s = Session(src)
    ulid = h.new_ulid()
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)

    first = _reopen(env, paths, ulid, keep_id=SID)
    new_id = first.agent_session_id
    assert new_id != SID and (src / f"{new_id}.jsonl").exists()
    s1 = Session(src, new_id)
    _write_real(s1.path, new_id, 41, 44)                               # the reopened session goes on
    inc1, block1 = _round(paths, ulid, s1)
    assert inc1.append_from is None
    _check(env, paths, ulid, s1, block1)

    second = _reopen(env, paths, ulid, keep_id=new_id)
    assert second.agent_session_id == new_id, "the second reopen must not mint another id"
    _write_real(s1.path, new_id, 45, 48)
    inc2, block2 = _round(paths, ulid, s1)
    assert inc2.append_from is not None, "the reopened prefix must be the uploaded prefix"
    segs, sent = _sent(env, paths, ulid)
    grown = inc2.exported.raw_size - inc2.append_from
    assert segs <= math.ceil(grown / CH) + 1 and sent < inc2.exported.raw_size / 2
    _check(env, paths, ulid, s1, block2)


def test_keep_cwd_keeps_the_workdir_and_what_is_under_it_and_a_fork_rewrites_them_all():
    new = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    lines = [x for n in (1, 3, 4) for x in _real_turn(SID, n, _cd(n))]
    assert C._rewrite_lines(lines, SID, "/golden/inc", keep_cwd=True) == lines
    other = C._rewrite_lines(lines, new, "/golden/other")
    assert {json.loads(x)["cwd"] for x in other if "cwd" in json.loads(x)} == {"/golden/other"}
    kept_new = C._rewrite_lines(lines, new, "/golden/inc", keep_cwd=True)
    assert {json.loads(x)["cwd"] for x in kept_new if "cwd" in json.loads(x)} == {"/golden/inc", "/golden/inc/pkg/sub"}


@pytest.mark.parametrize("cwd", ["/golden/incx", "/golden", "/elsewhere/golden/inc", "relative/golden/inc"])
def test_keep_cwd_decides_by_path_segments_not_by_string_prefix(cwd):
    line = _line(type="user", sessionId=SID, cwd=cwd)
    out = C._rewrite_lines([line], SID, "/golden/inc", keep_cwd=True)
    assert json.loads(out[0])["cwd"] == "/golden/inc"


def test_a_session_that_cds_into_a_subdirectory_reopened_twice_is_a_whole_cut_once_and_then_an_append(env, src, paths):
    """#197: two kinds of `cwd` in one file, a sidecar too. Second reopen: same id, only the tail is sent."""
    path = src / f"{SID}.jsonl"
    _write_real(path, SID, 1, 40)
    assert {json.loads(x).get("cwd") for x in path.read_text(encoding="utf-8").splitlines()} >= {"/golden/inc", "/golden/inc/pkg/sub"}
    s = Session(src)
    s.sidecar("subagents/agent-a1.jsonl", "\n".join(_real_turn(SID, 90, "/golden/inc/pkg/sub")) + "\n")
    ulid = h.new_ulid()
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)

    first = _reopen(env, paths, ulid, keep_id=SID)
    new_id = first.agent_session_id
    assert new_id != SID                                      # the user's file, no marker of ours
    s1 = Session(src, new_id)
    _write_real(s1.path, new_id, 41, 46)
    inc1, block1 = _round(paths, ulid, s1)
    assert inc1.append_from is None
    _check(env, paths, ulid, s1, block1)
    cwds = {json.loads(x).get("cwd") for x in s1.path.read_text(encoding="utf-8").splitlines()}
    assert "/golden/inc/pkg/sub" in cwds                     # the lines added in the reopened session

    second = _reopen(env, paths, ulid, keep_id=new_id)
    assert second.agent_session_id == new_id, "the second reopen must not mint another id"
    assert (src / new_id / "subagents/agent-a1.jsonl").exists()
    _write_real(s1.path, new_id, 47, 52)
    inc2, block2 = _round(paths, ulid, s1)
    assert inc2.append_from is not None
    segs, sent = _sent(env, paths, ulid)
    grown = inc2.exported.raw_size - inc2.append_from
    assert segs <= math.ceil(grown / CH) + 1 and sent < inc2.exported.raw_size / 2
    _check(env, paths, ulid, s1, block2)


def test_a_sidecar_line_added_in_a_subdirectory_after_the_first_reopen_still_lets_the_second_reuse_the_id(env, src, paths):
    """R2: the sidecar goes through the same `keep_cwd` rule as the main file."""
    path = src / f"{SID}.jsonl"
    _write_real(path, SID, 1, 20)
    s = Session(src)
    s.sidecar("subagents/agent-a1.jsonl", "\n".join(_real_turn(SID, 90, "/golden/inc")) + "\n")
    ulid = h.new_ulid()
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)

    new_id = _reopen(env, paths, ulid, keep_id=SID).agent_session_id
    assert new_id != SID
    s1 = Session(src, new_id)
    _write_real(s1.path, new_id, 21, 22)
    side = src / new_id / "subagents/agent-a1.jsonl"
    with open(side, "a", encoding="utf-8") as f:                 # a subagent run from inside a subdirectory
        f.write("\n".join(_real_turn(new_id, 91, "/golden/inc/pkg/sub")) + "\n")
    _, block1 = _round(paths, ulid, s1)
    _check(env, paths, ulid, s1, block1)

    second = _reopen(env, paths, ulid, keep_id=new_id)
    assert second.agent_session_id == new_id, "the sidecar's subdirectory cwd must not look like a collision"
    assert side.read_text(encoding="utf-8").count("/golden/inc/pkg/sub") > 0


def test_a_real_shaped_session_written_back_from_the_raw_is_the_uploaded_bytes(env, src, paths):
    """The file is gone (another Claude home): written from the raw, snapshot lines and all, the same bytes."""
    path = src / f"{SID}.jsonl"
    _write_real(path, SID, 1, 12)
    original = path.read_bytes()
    s = Session(src)
    ulid = h.new_ulid()
    _, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)
    _gone(s, src)
    assert _reopen(env, paths, ulid, keep_id=SID).agent_session_id == SID
    assert path.read_bytes() == original


# --- opt-in: the real claude ----------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("AGORA_TEST_REAL_CLAUDE") != "1",
                    reason="starts the real claude (set AGORA_TEST_REAL_CLAUDE=1 and AGORA_REAL_CLAUDE_HOME)")
@pytest.mark.integration
def test_the_real_claude_resumes_a_file_written_back_as_it_was(tmp_path, monkeypatch):
    """`claude --resume <id>` on a file `start_native(keep_id=...)` wrote untouched knows the whole history.

    Needs a real claude on PATH. It builds a short conversation of its own in a scratch
    CLAUDE_CONFIG_DIR (never the user's), writes it back through the adapter, resumes it and
    asks for the first thing said. PM decides when this runs.
    """
    claude = shutil.which("claude")
    if not claude:
        pytest.skip("no claude on PATH")
    cfg = tmp_path / "claude-config"
    work = tmp_path / "work"
    work.mkdir()
    cfg.mkdir()
    for key in ("CLAUDE_CONFIG_DIR",):
        monkeypatch.setenv(key, str(cfg))
    env_ = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
    first = subprocess.run([claude, "-p", "Remember the word PELICAN-190 and say OK.", "--output-format", "json"],
                           cwd=work, env=env_, capture_output=True, text=True, timeout=240)
    assert first.returncode == 0, first.stderr
    sid = json.loads(first.stdout)["session_id"]
    exported = C.ClaudeAgent().export(sid, hint_dir=work)
    original = C.find_jsonl(sid, work).read_bytes()
    C.find_jsonl(sid, work).unlink()
    launch = C.ClaudeAgent().start_native(exported.raw_file.read_bytes(), work, keep_id=sid)
    assert launch.agent_session_id == sid
    assert C.find_jsonl(sid, work).read_bytes() == original
    again = subprocess.run([claude, "-p", "What was the word? Answer with the word only.", "--resume", sid,
                            "--output-format", "json"], cwd=work, env=env_, capture_output=True, text=True, timeout=240)
    assert again.returncode == 0, again.stderr
    assert "PELICAN-190" in json.loads(again.stdout)["result"]
