"""Golden bytes for the continue write-back pipeline (impl-mem 2a).

The pipeline's output may not move by one byte when its internals change:
these tests run one deterministic fake claude session through export /
turns / reading / a real `_write_one`, and - when `AGORA_GOLDEN_OUT` names a
directory - dump every output there. The golden run diffs the dump of a
`git archive HEAD` copy against this tree's (report 階段 2a), so a refactor
that changes any byte goes red.

Determinism: fixed session uuids, timestamps, cwd *string* (`/golden/proj`,
data, not a real directory), `AGORA_NOW` for the wall clock and a patched
`header.new_ulid` for the ids. The fake sessions carry CJK text, big
tool_results, a subagent sidecar, a mid-file broken line and a truncated
last line - the paths `_split_lines` distinguishes.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h
from agora.agents.base import AgentError, reading, reading_of
from agora.agents.claude import ClaudeAgent, encode_project_dir, projects_dir

SID = "11111111-2222-4333-8444-555555555555"       # the session under test
SEED = "99999999-2222-4333-8444-555555555555"      # the Session continued from
SUB = "0123456789abcdef"
CWD = "/golden/proj"
ULID = "01M00000000000000000000000"                # patched new_ulid
NOW = 1760000000.0                                 # AGORA_NOW
STAMP = "2026-10-19T09:00:00.000Z"

# Golden bytes (review 中2): the three md5s of `digests.json` from one deterministic run.
# The values are NOT this tree's: they come from a `git archive HEAD` copy of 6335f72 -
# the pipeline *before* impl-mem 2a - running this test file as 2a left it (2026-10-19,
# `AGORA_GOLDEN_OUT=/tmp/head_dump uv run pytest tests/unit/test_mem_golden.py::test_golden_pipeline`).
# The two-tree dump diff proved 2a moved no byte; these constants make `pytest tests/unit`
# keep that proof on its own. `raw` and `exported.raw` are the same bytes (asserted below),
# hence the equal md5s. Recompute all three: run this test with AGORA_GOLDEN_OUT pointing at
# an empty dir and read the dump's digests.json (diff -r the whole dump against a HEAD copy's
# for the byte-level guard). Changing these values means changing the pipeline's output -
# only with the owner's sign-off.
GOLDEN_MD5_EXPORTED_RAW = "d245594ea2e89f733749fa1dc4252622"
GOLDEN_MD5_OUTBOX_RAW = "d245594ea2e89f733749fa1dc4252622"
GOLDEN_MD5_SESSION_MD = "992991ad88bc5af3a844d5bcc0354c49"


def _line(o: dict) -> str:
    return json.dumps(o, ensure_ascii=False) + "\n"


def _uuid(n: int) -> str:
    return f"{n:08d}-aaaa-4bbb-8ccc-{SID[-12:]}"


def _big(kb: int, tag: str) -> str:
    """~kb of deterministic tool output: numbered CJK log lines."""
    chunk = (f"{tag} 步驟 {{n}}：讀取 /golden/proj/src/module_{{n}}.py 完成，"
             "warning: deprecated option 'legacy_mode'；exit code 0；")
    lines, used, n = [], 0, 0
    while used < kb * 1024:
        n += 1
        line = chunk.format(n=n) + "x" * 16
        lines.append(line)
        used += len(line.encode("utf-8"))
    return "\n".join(lines)


class _Gen:
    """Deterministic claude jsonl lines; counts its own blanks out."""

    def __init__(self, sid: str):
        self.sid = sid
        self.n = 0
        self.turn = 0
        self.seconds = 0
        self.leaf: str | None = None

    def uuid(self) -> str:
        self.n += 1
        return _uuid(self.n)

    def stamp(self) -> str:
        self.seconds += 7
        m, s = divmod(self.seconds, 60)
        return f"2026-10-19T09:{m:02d}:{s:02d}.000Z"

    def base(self, otype: str) -> dict:
        o = {"type": otype, "sessionId": self.sid, "uuid": self.uuid(),
             "parentUuid": self.leaf, "timestamp": self.stamp(), "cwd": CWD,
             "version": "2.1.286", "isSidechain": False, "userType": "external",
             "entrypoint": "cli", "gitBranch": "main"}
        self.leaf = o["uuid"]
        return o

    def exchange(self, kb: int) -> list[str]:
        self.turn += 1
        tool = f"toolu_{self.turn:04d}"
        a1 = self.base("assistant")
        a1["requestId"] = f"req_{self.turn:04d}"
        a1["message"] = {"id": f"msg_{self.turn:04d}", "type": "message", "role": "assistant",
                         "model": "golden-model", "stop_reason": "tool_use",
                         "content": [
                             {"type": "text",
                              "text": f"第 {self.turn} 輪：「引號」與 \\ 反斜線都會經過 json。"},
                             {"type": "tool_use", "id": tool, "name": "Read",
                              "input": {"file_path": f"/golden/proj/src/module_{self.turn}.py",
                                        "limit": 2000,
                                        "note": _big(1, f"[cmd {self.turn}]")}},
                         ]}
        body = _big(kb, f"[round {self.turn}]")
        u1 = self.base("user")
        u1["toolUseResult"] = {"type": "text", "content": body}
        u1["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool, "content": body}]}
        a2 = self.base("assistant")
        a2["requestId"] = f"req_{self.turn:04d}b"
        a2["message"] = {"id": f"msg_{self.turn:04d}b", "type": "message", "role": "assistant",
                         "model": "golden-model", "stop_reason": "end_turn",
                         "content": [{"type": "text",
                                      "text": f"看完了，第 {self.turn} 個模組沒有問題，繼續。"}]}
        return [_line(a1), _line(u1), _line(a2)]

    def noise(self) -> list[str]:
        out = []
        att = self.base("attachment")
        att.pop("message", None)
        att["attachment"] = {"type": "file", "snapshot": {"path": "/golden/proj/a.py"}}
        att["renderedRole"] = "system"
        att["rendered"] = []
        out.append(_line(att))
        meta = self.base("user")
        meta["isMeta"] = True
        meta["message"] = {"role": "user", "content": "<system-reminder>請忽略</system-reminder>"}
        out.append(_line(meta))
        sysl = self.base("system")
        sysl["subtype"] = "compact_boundary"
        out.append(_line(sysl))
        comp = self.base("user")
        comp["isCompactSummary"] = True
        comp["message"] = {"role": "user", "content": "壓縮後的摘要：做到第 3 輪。"}
        out.append(_line(comp))
        prog = self.base("progress")
        prog["content"] = "3/18"
        out.append(_line(prog))
        return out

    def bookkeeping(self) -> str:
        self.turn += 1
        o = self.base("summary")
        o["summary"] = f"階段 {self.turn} 的進度"
        o["leafUuid"] = self.leaf
        return _line(o)

    def head(self) -> str:
        u = self.base("user")
        u["message"] = {"role": "user", "content": "把這批模組的設定檢查一遍，有問題的列出來。"}
        return _line(u)


def _write_session(main: Path, sidecar: Path, *, exchanges: int, kb: int,
                   broken_at: int | None = None, half_tail: bool = False) -> int:
    """Write one fake session; returns its non-blank main line count."""
    gen = _Gen(main.stem)
    aux = sidecar / "subagents" / f"agent-{SUB}.jsonl"
    aux.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [gen.head()]
    for _ in range(exchanges):
        lines += gen.exchange(kb)
    lines += gen.noise()
    lines.append(gen.bookkeeping())
    if broken_at is not None:
        lines[broken_at] = lines[broken_at][:40]     # mid-file: a line that is not JSON
    if half_tail:
        lines[-1] = lines[-1][:60]                   # last line half-written
    non_blank = sum(1 for line in lines if line.strip())
    main.write_text("".join(lines), encoding="utf-8")
    sub = _Gen(main.stem)
    sub.leaf = None
    subs = []
    for k in range(3):
        o = sub.base("assistant" if k % 2 else "user")
        o["isSidechain"] = True
        o["message"] = {"role": o["type"], "content": [{"type": "text",
                                                       "text": _big(2, f"[sub {k}]")}]}
        subs.append(_line(o))
    aux.write_text("".join(subs), encoding="utf-8")
    (aux.parent / f"agent-{SUB}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "查設定", "requestNonInteractive": True,
         "requestShape": "direct", "spawnDepth": 1, "toolUseId": "toolu_meta"}))
    return non_blank


@pytest.fixture
def golden_env(tmp_path, monkeypatch):
    """Fake rclone + a pinned clock and id: two runs must produce the same bytes."""
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text("#!/bin/sh\nexec "
                       f"{sys.executable} {Path(__file__).resolve().parent.parent}"
                       f"/fakes/fake_rclone.py \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    monkeypatch.setenv("AGORA_NOW", str(NOW))
    monkeypatch.setattr(h, "new_ulid", lambda now_ms=None: ULID)
    return store.Paths.from_env()


def _make_sessions() -> tuple[Path, Path, Path, int]:
    """The good session (with sidecar) plus the broken-line and half-line ones."""
    pdir = projects_dir() / encode_project_dir(CWD)
    pdir.mkdir(parents=True)
    good = pdir / f"{SID}.jsonl"
    n_lines = _write_session(good, pdir / SID, exchanges=14, kb=48)
    _write_session(pdir / f"{SEED}.jsonl", pdir / SEED, exchanges=2, kb=4)
    bad = pdir / "22222222-2222-4333-8444-555555555555.jsonl"
    _write_session(bad, pdir / bad.stem, exchanges=6, kb=4, broken_at=9)
    half = pdir / "33333333-2222-4333-8444-555555555555.jsonl"
    _write_session(half, pdir / half.stem, exchanges=6, kb=4, half_tail=True)
    return good, bad, half, n_lines


def _dump(out_dir: Path, name: str, data: bytes | str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    (out_dir / name).write_bytes(data)


def test_golden_pipeline(golden_env, capsys):
    """export / turns / reading / _write_one over one fake session.

    Always asserts the pipeline's own contract; with AGORA_GOLDEN_OUT set it
    also dumps every output for the byte diff against the HEAD copy.
    """
    out_dir = os.environ.get("AGORA_GOLDEN_OUT")
    good, bad, half, _n = _make_sessions()
    agent = ClaudeAgent()

    exported = agent.export(SID)
    assert exported.raw.startswith(b'{"format": "claude-jsonl/1"')
    assert md5(exported.raw) == GOLDEN_MD5_EXPORTED_RAW   # the bytes may not move (中2)
    assert exported.dir == CWD and exported.model == "golden-model"
    assert exported.message_count == 1 + 14 * 3 + 1      # head + exchanges + compact summary
    turns = agent.turns(exported.raw)
    assert turns[0][0] == "user" and turns[0][1][0].startswith("把這批模組")
    assert any("[skip progress]" in line for _role, lines in turns for line in lines)
    body = reading(agent, exported.raw)
    assert reading_of(agent, exported) == body           # the carried arm must match raw's own turns() (中1)

    with pytest.raises(AgentError) as exc:
        agent.export(bad.stem)
    bad_error = str(exc.value)
    half_stderr = ""
    agent.export(half.stem)                              # drops the half line, warns
    half_stderr = capsys.readouterr().err
    assert "最後一行不完整" in half_stderr

    # One real write-back: a seed Session imported, then a continue round staged
    # into it through _write_one (the fake remote keeps it; the mirror mirrors it).
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["import", "session", "--external-session-id", SEED,
                       "--agent", "claude"])
    assert rc == 0
    target = out.getvalue().strip().splitlines()[-1]
    paths = golden_env
    record = {"agora_id": target, "agent": "claude", "agent_session_id": SID,
              "dir": CWD, "parent": {"id": target, "raw_md5": "0" * 32},
              "parent_headers": [], "title": "金標準", "before_count": exported.message_count - 1,
              "started_at": "2026-10-19T00:00:00Z", "header_updates": {},
              "add_tags": [], "saved_md5": "0" * 32}
    run = cli._Run(paths, record)
    saved, where, _note = cli._write_one(paths, run, exported, agent, check_drive=False)
    assert saved is not None and where == target
    ulid = target.split(":", 1)[1]
    md = (paths.mirror / ulid / "session.md").read_bytes()
    assert md5(md) == GOLDEN_MD5_SESSION_MD              # the header's bytes may not move (中2)
    raws = sorted((paths.mirror / ulid).glob("raw-*.json"))
    assert len(raws) == 1
    raw = raws[0].read_bytes()
    assert md5(raw) == GOLDEN_MD5_OUTBOX_RAW             # the staged raw's bytes may not move (中2)
    assert md5(raw) == hashlib.md5(exported.raw).hexdigest()
    # 沒有新內容不寫: same bytes again stage nothing.
    saved2, _w2, _n2 = cli._write_one(paths, run, exported, agent, check_drive=False)
    assert saved2 is None
    assert md5(raw) in md.decode("utf-8")                # the header names its raw's md5

    if out_dir:
        d = Path(out_dir)
        _dump(d, "good_raw.bin", exported.raw)
        _dump(d, "good_turns.json", json.dumps(
            [[role, lines] for role, lines in turns], ensure_ascii=False, indent=1))
        _dump(d, "good_reading.md", body)
        _dump(d, "good_meta.json", json.dumps(
            {k: getattr(exported, k) for k in ("session_id", "dir", "title", "created_at",
                                               "agent_version", "message_count", "model")},
            ensure_ascii=False, indent=1))
        _dump(d, "bad_error.txt", bad_error)
        _dump(d, "half_stderr.txt", half_stderr)
        _dump(d, "outbox_session.md", md)
        _dump(d, "outbox_raw.json", raw)
        _dump(d, "digests.json", json.dumps(
            {"session.md": md5(md), "raw": md5(raw),
             "exported.raw": hashlib.md5(exported.raw).hexdigest()}, indent=1))


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def test_the_round_child_stages_the_same_golden_bytes(golden_env):
    """Ticket impl-mem-subproc ⑥: the fresh-interpreter round child stages the
    same bytes the in-process pipeline does. Nothing is patched into the child -
    it inherits the pinned clock (`AGORA_NOW`) and the fake Drive through the
    environment, and its `已寫回` round must move no byte of the golden output.
    The child also cannot see this process's `new_ulid` patch, which is exactly
    the point: the update path mints no id, so it does not need one."""
    _make_sessions()
    agent = ClaudeAgent()
    exported = agent.export(SID)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["import", "session", "--external-session-id", SEED,
                       "--agent", "claude"])
    assert rc == 0
    target = out.getvalue().strip().splitlines()[-1]
    paths = golden_env
    record = {"agora_id": target, "agent": "claude", "agent_session_id": SID,
              "dir": CWD, "parent": {"id": target, "raw_md5": "0" * 32},
              "parent_headers": [], "title": "金標準", "before_count": exported.message_count - 1,
              "started_at": "2026-10-19T00:00:00Z", "header_updates": {},
              "add_tags": [], "saved_md5": "0" * 32}
    run = cli._Run(paths, record)

    cli._periodic_write(run, agent)                     # the REAL child: no seam patch

    log = (paths.state / cli.CONTINUE_LOG).read_text(encoding="utf-8")
    assert "定期寫回：已寫回" in log, log
    assert run.failures == 0
    ulid = target.split(":", 1)[1]
    md = (paths.mirror / ulid / "session.md").read_bytes()
    assert md5(md) == GOLDEN_MD5_SESSION_MD             # the child moved no byte
    raws = sorted((paths.mirror / ulid).glob("raw-*.json"))
    assert len(raws) == 1 and md5(raws[0].read_bytes()) == GOLDEN_MD5_OUTBOX_RAW
    assert run.last_md5 == md5(raws[0].read_bytes())
    assert isinstance(run.fingerprint, tuple)           # the wire's trap, end to end


def test_collect_parses_once(golden_env, monkeypatch):
    """One collect+write round parses the session's lines exactly once (B1a).

    Counters wrap `_parse_all`, `_unpack_raw` and json.loads-as-called-from-
    claude. Before B1a the shape was `_split_lines` ×1 plus `_parse_all` ×2 for
    the export side (`_split_lines` validated, `_parse_all` parsed, and the
    reading re-parsed raw). Now the streaming read validates and accumulates in
    the same json.loads, and the carried turns mean nothing parses raw again.
    """
    from agora.agents import claude

    good, _bad, _half, n_lines = _make_sessions()
    agent = ClaudeAgent()
    exported = agent.export(SID)                          # before the counters: setup only
    paths = golden_env
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["import", "session", "--external-session-id", SEED,
                       "--agent", "claude"])
    assert rc == 0
    target = out.getvalue().strip().splitlines()[-1]
    record = {"agora_id": target, "agent": "claude", "agent_session_id": SID,
              "dir": CWD, "parent": {"id": target, "raw_md5": "0" * 32},
              "parent_headers": [], "title": "計數", "before_count": exported.message_count - 1,
              "started_at": "2026-10-19T00:00:00Z", "header_updates": {},
              "add_tags": [], "saved_md5": "0" * 32}
    run = cli._Run(paths, record)

    counts = {"_parse_all": 0, "_unpack_raw": 0, "json.loads@claude": 0}
    real_parse, real_unpack, real_loads = claude._parse_all, claude._unpack_raw, json.loads

    def count_parse(lines):
        counts["_parse_all"] += 1
        return real_parse(lines)

    def count_unpack(raw):
        counts["_unpack_raw"] += 1
        return real_unpack(raw)

    def count_loads(*args, **kwargs):
        if sys._getframe(1).f_globals.get("__name__") == "agora.agents.claude":
            counts["json.loads@claude"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(claude, "_parse_all", count_parse)
    monkeypatch.setattr(claude, "_unpack_raw", count_unpack)
    monkeypatch.setattr(claude.json, "loads", count_loads)
    exported = cli._collect_new(run, agent)
    assert exported is not None
    cli._write_one(paths, run, exported, agent, check_drive=False)
    monkeypatch.undo()

    assert counts["_parse_all"] == 0, counts              # the export no longer builds dicts
    assert counts["_unpack_raw"] == 0, counts             # was 1: reading re-unpacked raw
    # line-level: the streaming read parses each non-blank line once (B1a)
    assert counts["json.loads@claude"] == n_lines, counts
