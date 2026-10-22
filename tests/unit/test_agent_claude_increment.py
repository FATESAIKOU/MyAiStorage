"""R3b-2: the claude adapter's side of the plan (design B.2, B.3, C): `export_increment`.

A real jsonl on disk, a real cursor written by `stage`, the real round trip: whatever the source
does between two rounds, the segments on Drive join to exactly the raw a plain `export` gives,
and `append_from` is set only when the sealed prefix is still the same bytes.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import random
from pathlib import Path

import pytest

from agora import header as h, store
from agora.agents import claude as C
from agora.agents.base import collect_increment
from test_segment_write import C as CH, _drive_folder, _header, _joined, _manifest, env, md5  # noqa: F401

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
STAMP = "2026-10-20T00:00:00.000Z"
_N = [0]


def _obj(otype, sid=SID, **kw) -> dict:
    _N[0] += 1
    o = {"type": otype, "sessionId": sid, "uuid": f"{_N[0]:08d}-1111-4222-8333-444444444444",
         "parentUuid": None, "isSidechain": False, "timestamp": STAMP, "version": "2.1.286",
         "cwd": "/golden/inc"}
    o.update(kw)
    return o


def _turn(n: int, size: int = 300, sid=SID) -> str:
    user = _obj("user", sid, message={"role": "user", "content": f"問題 {n} " + "字" * (size // 3)})
    bot = _obj("assistant", sid, message={"role": "assistant", "model": "m",
                                          "content": [{"type": "text", "text": f"回答 {n} " + "x" * size}]})
    return json.dumps(user, ensure_ascii=False) + "\n" + json.dumps(bot, ensure_ascii=False) + "\n"


@pytest.fixture
def src(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(tmp_path / "claude-home"))
    proj = tmp_path / "claude-home" / ".claude" / "projects" / "-golden-inc"
    proj.mkdir(parents=True)
    return proj


@pytest.fixture
def paths(env):
    return store.Paths.from_env()


class Session:
    def __init__(self, proj: Path, sid=SID):
        self.sid, self.path, self.proj = sid, proj / f"{sid}.jsonl", proj
        self.n = 0

    def turns(self, k: int, size: int = 300):
        with open(self.path, "a", encoding="utf-8") as f:
            for _ in range(k):
                self.n += 1
                f.write(_turn(self.n, size, self.sid))

    def sidecar(self, name: str, text: str):
        d = self.proj / self.sid
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")


def _round(paths, ulid, session: Session, *, hdr_title="x"):
    """What `agora continue`'s save does: read the cursor, ask the adapter, stage with the plan."""
    agent = C.ClaudeAgent()
    cursor = store.read_cursor(paths, ulid)
    inc = agent.export_increment(session.sid, None, cursor)
    plan = {"append_from": inc.append_from, "seal_upto": inc.seal_upto, "cursor": inc.cursor,
            "cursor_ulid": ulid}
    hdr = _header(hdr_title, ulid)
    folder = store.stage(paths, hdr, "x", inc.exported.raw_file, plan=plan,
                         update=(paths.mirror / ulid / "session.md").exists() and _on_drive(paths, ulid))
    store.remember(paths, folder)
    return inc, hdr["agora"]["raw"]


def _on_drive(paths, ulid) -> bool:
    return (Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions" / ulid / "session.md").exists()


def _check(env, paths, ulid, session: Session, block):
    """Drive joins to what a plain export gives, in the manifest's own sizes and md5s."""
    whole = C.ClaudeAgent().export(session.sid).raw
    assert block["md5"] == md5(whole) and block["size"] == len(whole)
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert _joined(env, ulid) == whole


def test_the_export_asks_the_pass_to_capture_the_sealed_boundary(src, paths, monkeypatch):
    """#158: without `capture` the pass does not snapshot the md5 and nothing can be verified."""
    s = Session(src)
    s.turns(40)
    ulid = h.new_ulid()
    _round(paths, ulid, s)
    seen = []
    real = C._exported
    cursor_before = store.read_cursor(paths, ulid)

    def spy(*a, **k):
        seen.append(k.get("capture"))
        return real(*a, **k)
    monkeypatch.setattr(C, "_exported", spy)
    s.turns(3)
    _round(paths, ulid, s)
    assert seen[-1] == (cursor_before["sealed_line_count"],), seen


def test_a_first_export_has_no_append_from_and_seals_the_main_content(src, paths):
    s = Session(src)
    s.turns(40)
    inc = C.ClaudeAgent().export_increment(SID, None, None)
    assert inc.append_from is None
    assert 0 < inc.seal_upto < inc.exported.raw_size
    assert inc.cursor["base_md5"] == inc.exported.raw_md5 and inc.cursor["agent_session_id"] == SID


def test_appended_turns_are_an_append_and_the_segments_join_to_the_raw(env, src, paths):
    s = Session(src)
    s.turns(40)
    ulid = h.new_ulid()
    _, b1 = _round(paths, ulid, s)
    _check(env, paths, ulid, s, b1)
    for _ in range(3):
        s.turns(15)
        inc, block = _round(paths, ulid, s)
        assert inc.append_from is not None
        _check(env, paths, ulid, s, block)
    assert _gen_count(block) == 1, "appends keep the generation"


def _gen_count(block) -> int:
    return len({seg["file"].split("-")[1] for seg in block["segments"]})


def test_a_half_written_last_line_is_left_out_and_then_counted_when_it_is_whole(env, src, paths):
    s = Session(src)
    s.turns(40)
    ulid = h.new_ulid()
    _round(paths, ulid, s)
    s.turns(5)
    whole = _turn(99).splitlines()[0] + "\n"             # one line
    with open(s.path, "a", encoding="utf-8") as f:
        f.write(whole[: len(whole) // 2])                   # the writer is mid-line
    inc, block = _round(paths, ulid, s)
    _check(env, paths, ulid, s, block)
    before_lines = inc.cursor["main_line_count"]
    with open(s.path, "a", encoding="utf-8") as f:
        f.write(whole[len(whole) // 2:])
    inc2, block2 = _round(paths, ulid, s)
    assert inc2.append_from is not None
    assert inc2.cursor["main_line_count"] == before_lines + 1
    _check(env, paths, ulid, s, block2)


@pytest.mark.parametrize("what", ["compact", "truncate_below_sealed", "edited_in_place", "other_session_id",
                                  "cursor_garbage", "cursor_wrong_version", "cursor_md5_wrong"])
def test_anything_that_is_not_a_plain_append_is_cut_whole_again(env, src, paths, what):
    s = Session(src)
    s.turns(60)
    ulid = h.new_ulid()
    _, b1 = _round(paths, ulid, s)
    s.turns(10)
    _, b1 = _round(paths, ulid, s)            # now something is sealed
    cursor = store.read_cursor(paths, ulid)
    assert cursor["raw_stable"] > 0
    text = s.path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if what == "compact":
        s.path.write_text("".join(lines[:2]) + _turn(500) + "".join(lines[40:]), encoding="utf-8")
    elif what == "truncate_below_sealed":
        s.path.write_text("".join(lines[:10]), encoding="utf-8")
    elif what == "edited_in_place":
        i = cursor["sealed_line_count"] // 2
        lines[i] = lines[i].replace("x", "y", 1) if "x" in lines[i] else lines[i].replace("字", "子", 1)
        s.path.write_text("".join(lines), encoding="utf-8")
    elif what == "cursor_garbage":
        store._cursor_path(paths, ulid).write_text("nope", encoding="utf-8")
    elif what == "cursor_wrong_version":
        c = dict(cursor, version=2)
        store._cursor_path(paths, ulid).write_text(json.dumps(c), encoding="utf-8")
    elif what == "cursor_md5_wrong":
        c = dict(cursor, sealed_md5=md5(b"not it"))
        store._cursor_path(paths, ulid).write_text(json.dumps(c), encoding="utf-8")
    if what == "other_session_id":
        inc = C.ClaudeAgent().export_increment(SID, None, dict(cursor, agent_session_id="someone-else"))
        assert inc.append_from is None
        return
    s.turns(1)
    inc, block = _round(paths, ulid, s)
    assert inc.append_from is None
    assert not {x["file"] for x in block["segments"]} & {x["file"] for x in b1["segments"]}
    _check(env, paths, ulid, s, block)


def test_a_sidecar_that_changes_only_changes_the_tail(env, src, paths):
    s = Session(src)
    s.turns(60)
    s.sidecar("a/notes.txt", "one")
    ulid = h.new_ulid()
    _, b1 = _round(paths, ulid, s)
    _check(env, paths, ulid, s, b1)
    s.sidecar("a/notes.txt", "two, longer")
    s.sidecar("b/more.txt", "new file")
    inc, b2 = _round(paths, ulid, s)
    assert inc.append_from is not None
    assert b2["segments"][:-1] == b1["segments"][:-1], "only the tail follows the aux"
    _check(env, paths, ulid, s, b2)


def test_aux_serialization_is_stable_between_two_exports_and_when_a_file_is_added(src):
    s = Session(src)
    s.turns(5)
    for name in ("z.txt", "a/b.txt", "m.txt"):
        s.sidecar(name, name)
    agent = C.ClaudeAgent()
    first = agent.export(SID).raw
    assert agent.export(SID).raw == first
    s.sidecar("c.txt", "added")
    aux_of = lambda raw: json.loads(raw[raw.index(b'"aux": ') + 7:-1].decode("utf-8"))
    before, after = aux_of(first), aux_of(agent.export(SID).raw)
    assert [k for k in after if k in before] == list(before)


def test_random_rounds_always_join_to_the_plain_export(env, src, paths, monkeypatch):
    rng = random.Random(1701)
    for chunk in (4096, 10000, 65536):
        monkeypatch.setenv("AGORA_RAW_CHUNK", str(chunk))
        s = Session(src, sid=f"{rng.randrange(10**8):08d}-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        s.turns(rng.choice([1, 30]), 200)
        ulid = h.new_ulid()
        for rnd in range(6):
            act = rng.choice(["turns", "turns", "turns", "half", "sidecar", "compact"])
            if act == "turns":
                s.turns(rng.choice([1, 5, 40]), rng.choice([50, 400]))
            elif act == "sidecar":
                s.sidecar(f"f{rng.randrange(3)}.txt", "v" * rng.randrange(1, 50))
            elif act == "half":
                line = _turn(7, 100, s.sid).splitlines()[0] + "\n"
                with open(s.path, "a", encoding="utf-8") as f:
                    f.write(line[:60])
                inc, block = _round(paths, ulid, s)          # the half line is not in the raw
                _check(env, paths, ulid, s, block)
                with open(s.path, "a", encoding="utf-8") as f:
                    f.write(line[60:])
            elif act == "compact":
                lines = s.path.read_text(encoding="utf-8").splitlines(keepends=True)
                if len(lines) > 4:
                    s.path.write_text("".join(lines[:1]) + _turn(900, 60, s.sid) + "".join(lines[3:]),
                                      encoding="utf-8")
            if not s.path.exists():
                continue
            inc, block = _round(paths, ulid, s)
            _check(env, paths, ulid, s, block)
            assert inc.seal_upto <= block["size"]


def test_collect_increment_wraps_an_agent_without_the_method(tmp_path):
    from agora.agents.base import Increment

    class Plain:
        def collect(self, launch):
            return "exported"
    inc = collect_increment(Plain(), object(), {"x": 1})
    assert isinstance(inc, Increment) and inc.exported == "exported"
    assert inc.append_from is None and inc.seal_upto is None and inc.cursor is None

    class Nothing:
        def collect(self, launch):
            return None
    assert collect_increment(Nothing(), object(), None) is None
