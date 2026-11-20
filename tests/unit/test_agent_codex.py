import _guard  # noqa: F401

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from agora import cache, cli, credscan, store, tui
from agora import header as h
from agora.agents import codex, claude
from agora.agents.base import AgentError, Listed
from _round_seam import in_process_start_child

A = codex.ADAPTER
SID = "00000000-0000-0000-0000-000000000001"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "rollout-sample.jsonl"
FAKES = Path(__file__).resolve().parents[1] / "fakes"


@pytest.fixture
def rollout():
    p = Path(os.environ["AGORA_CODEX_HOME"]) / "sessions" / "2026/11/08" / f"rollout-sample-{SID}.jsonl"
    p.parent.mkdir(parents=True)
    p.write_bytes(FIX.read_bytes())
    return p


@pytest.fixture
def lab(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    fake = tmp_path / "fake"
    fake.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("FAKE_HOME", str(fake))
    monkeypatch.setenv("FAKE_AGENT_MODE", "append")
    monkeypatch.setenv("FAKE_CLAUDE_REPLY", json.dumps({"purpose": "test", "decisions": [], "progress": "done", "open_questions": []}))
    for name, script in [("RCLONE", "fake_rclone.py"), ("CLAUDE_CMD", "fake_claude.py"),
                         ("OPENCODE_CMD", "fake_opencode.py")]:
        wrapper = tmp_path / name
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / script}" "$@"\n')
        wrapper.chmod(0o755)
        monkeypatch.setenv("AGORA_" + name, str(wrapper))
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)
    return work, fake


def run(capsys, *args):
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err


def imported(capsys):
    code, out, err = run(capsys, "import", "session", "--agent", "codex", "--external-session-id", SID)
    assert code == 0, err
    return out.splitlines()[-1]


def document(aid):
    return h.split_document((store.Paths.from_env().mirror / aid.split(":")[1] / "session.md").read_text())


def test_c1_sample_format_and_import_raw(rollout, lab, capsys):
    # Unknown types are absent from the sample: append the minimal synthetic row.
    rollout.write_bytes(rollout.read_bytes() + b'{"type":"future_event","payload":{}}\n')
    exported = A.export(SID)
    assert exported.message_count == 11  # response_item texts only, no duplicate item_completed
    assert exported.agent_version == "0.167.0"
    assert exported.model == "gpt-6.1-sol"
    assert exported.turns == A.turns(exported.raw)
    aid = imported(capsys)
    assert aid.startswith("agora:")
    hdr, body = document(aid)
    assert hdr["agora"]["source"]["agent"] == "codex"
    assert hdr["agora"]["source"]["session_id"] == SID
    assert hdr["generated"]["by"].startswith("codex")
    assert "樣本測試完成" in body and "## user" in body and "[tool] exec" in body
    assert "[skip future_event]" in body
    code, raw, err = run(capsys, "show", "session", aid, "--raw")
    assert code == 0, err
    assert hashlib.md5(raw.encode() + b"\n").hexdigest() == hashlib.md5(rollout.read_bytes()).hexdigest()


@pytest.mark.parametrize("raw", [b"", b'{"type":"future_event"}\n'])
def test_c2_parseable_empty_not_imported(rollout, lab, capsys, raw):
    rollout.write_bytes(raw)
    assert A.export(SID).message_count == 0
    code, out, err = run(capsys, "import", "session", "--agent", "codex", "--external-session-id", SID)
    assert code == 1 and not out and "沒有任何訊息" in err


def test_c3_reimport_unchanged(rollout, lab, capsys):
    aid = imported(capsys)
    before = (store.Paths.from_env().mirror / aid.split(":")[1] / "session.md").read_bytes()
    assert imported(capsys) == aid
    assert (store.Paths.from_env().mirror / aid.split(":")[1] / "session.md").read_bytes() == before


def test_c4_shorter_refused(rollout, lab, capsys):
    aid = imported(capsys)
    before = document(aid)
    rows = rollout.read_bytes().splitlines(keepends=True)
    rollout.write_bytes(b"".join(rows[:19]))
    code, out, err = run(capsys, "import", "session", "--agent", "codex", "--external-session-id", SID)
    assert code == 1 and "內容比原本少" in err
    assert document(aid) == before


@pytest.mark.parametrize("target", ["claude", "opencode"])
def test_c5_cross_agent_continue(rollout, lab, capsys, target):
    aid = imported(capsys)
    code, out, err = run(capsys, "continue", "session", aid, "--agent", target, "--dir", str(lab[0]))
    assert code == 0, err
    hdr, body = document(aid)
    assert hdr["agora"]["source"]["agent"] == target
    assert f"codex:{SID}" in hdr["agora"]["previous_sources"]
    assert "樣本測試完成" in body


@pytest.mark.parametrize("action,extra", [("continue", []), ("continue", ["--fork"]),
    ("continue", ["--headless"]), ("continue", ["--harness-file", "/nonexistent/harness"]),
    ("continue", ["--agent-arg=--effort=high"]), ("merge", [])])
def test_c6_boundary_has_no_side_effects(lab, tmp_path, capsys, monkeypatch, action, extra):
    # Any id read, pass-through validation or source lookup would fail this test.
    def forbidden(*a, **kw):
        pytest.fail("refused codex operation accessed a source or agent arguments")
    monkeypatch.setattr(cli, "_refuse_agent_args", forbidden)
    monkeypatch.setattr(cli, "_sync_for", forbidden)
    monkeypatch.setattr(cli.private_tmp, "sweep", forbidden)
    code, out, err = run(capsys, action, "session", "not-even-an-id", "--agent", "codex", *extra)
    assert code == 1 and not out
    assert (codex.CODEX_MERGE_LATER if action == "merge" else codex.CODEX_CONTINUE_LATER) in err
    assert not (tmp_path / "calls.log").exists()
    assert not list(lab[1].glob("*-args.log"))
    assert not list(store.Paths.from_env().state.glob("pending/*.json"))


def test_c7_reverse_direction_refused(rollout, lab, tmp_path, capsys):
    raw = claude.ADAPTER.native([("user", ["hello"]), ("assistant", ["ok"])])
    launch = claude.ADAPTER.start_native(raw, lab[0])
    sid = launch.agent_session_id
    code, aid, err = run(capsys, "import", "session", "--agent", "claude", "--external-session-id", sid)
    assert code == 0, err
    calls = (tmp_path / "calls.log").read_bytes()
    code, out, err = run(capsys, "continue", "session", aid, "--agent", "codex")
    assert code == 1 and codex.CODEX_CONTINUE_LATER in err and not out
    assert (tmp_path / "calls.log").read_bytes() == calls
    assert not list(lab[1].glob("*-args.log"))
    assert not list(store.Paths.from_env().state.glob("pending/*.json"))


def test_c8_merge_codex_source(rollout, lab, capsys):
    aid = imported(capsys)
    launch = claude.ADAPTER.start_native(claude.ADAPTER.native([("user", ["hello"])]), lab[0])
    code, other, err = run(capsys, "import", "session", "--agent", "claude", "--external-session-id", launch.agent_session_id)
    assert code == 0, err
    code, merged, err = run(capsys, "merge", "session", aid, other, "--agent", "claude")
    assert code == 0, err
    hdr, body = document(merged.splitlines()[-1])
    assert aid.split(":")[1] in str(hdr)
    assert "codex" in str(hdr) or "codex" in body


def test_c9_tui_search_cache(rollout):
    paths = store.Paths.from_env()
    rows = tui.import_rows(store.Index(paths), [A], paths)
    assert [r.key for r in rows] == [f"codex:{SID}"]
    assert list(A.search_text("樣本測試完成")) == [SID]
    assert list(A.search_text("樣本測試完成", only=set())) == []
    assert A.last_message(SID)[0] == "assistant"
    assert cache.pull(paths, [f"codex:{SID}"], {"codex": A}) == (1, 0)
    assert "樣本測試完成" in (paths.reading / "codex" / f"{SID}.md").read_text()
    assert cache.pull(paths, [f"codex:{SID}"], {"codex": A}) == (0, 0)


def test_c9_missing_root_no_stderr(capsys):
    assert A.list_sessions() == []
    Path(os.environ["AGORA_CODEX_HOME"]).mkdir()
    assert A.list_sessions() == []
    paths = store.Paths.from_env()
    assert tui.import_rows(store.Index(paths), [A], paths) == []
    assert not capsys.readouterr().err


def test_c9_unreadable_rollout_errors(rollout, monkeypatch):
    original = Path.read_bytes
    def denied(p):
        if p == rollout:
            raise PermissionError("denied")
        return original(p)
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(AgentError):
        A.list_sessions()


def test_c10_unified_isolation_and_private_paths(rollout, tmp_path, monkeypatch):
    assert Path(os.environ["AGORA_CODEX_HOME"]).is_relative_to(tmp_path)
    assert "CODEX_HOME" not in os.environ
    monkeypatch.setenv("CODEX_HOME", "/nonexistent/forbidden-codex")
    seen = []
    original = Path.open
    def spy(p, *a, **kw):
        seen.append(p)
        return original(p, *a, **kw)
    monkeypatch.setattr(Path, "open", spy)
    A.export(SID)
    A.list_sessions()
    A.last_message(SID)
    list(A.search_text("樣本"))
    assert seen and all(p.is_relative_to(Path(os.environ["AGORA_CODEX_HOME"]) / "sessions") for p in seen)
    assert not any(p.name in ("auth.json", "config.toml") for p in seen)


def test_c11_truncated_tail_once(rollout, lab, capsys):
    rollout.write_bytes(rollout.read_bytes() + b'{"type":')
    code, out, err = run(capsys, "import", "session", "--agent", "codex", "--external-session-id", SID)
    assert code == 0 and out.startswith("agora:"), err
    assert err.count("最後一行不完整") == 1


@pytest.mark.parametrize("raw", [b"not json\n", b"not json", b'{}\nbroken\n{}\n'])
def test_c11_bad_jsonl_no_partial_session(rollout, lab, capsys, raw):
    rollout.write_bytes(raw)
    code, out, err = run(capsys, "import", "session", "--agent", "codex", "--external-session-id", SID)
    assert code == 2 and not out
    assert not list(store.Paths.from_env().mirror.glob("*/session.md"))


def test_c11_missing_file(rollout):
    rollout.unlink()
    with pytest.raises(AgentError):
        A.export(SID)


def test_c12_fixture_no_secret_patterns():
    raw = FIX.read_text()
    assert "sk" + "-" not in raw
    assert "ghp" + "_" not in raw
    assert credscan.scan_text(raw) == []


def test_refusal_stubs(tmp_path):
    for call in [lambda: A.native([]), lambda: A.append_native(b"", []),
                 lambda: A.summarize("", tmp_path), lambda: A.start_native(b"", tmp_path),
                 lambda: A.collect(None)]:
        with pytest.raises(AgentError, match="codex 第一版"):
            call()


def test_root_precedence(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("AGORA_CODEX_HOME", str(first))
    monkeypatch.setenv("CODEX_HOME", str(second))
    assert codex._root() == first / "sessions"
    monkeypatch.delenv("AGORA_CODEX_HOME")
    assert codex._root() == second / "sessions"
    monkeypatch.delenv("CODEX_HOME")
    assert codex._root() == Path(os.environ["HOME"]) / ".codex/sessions"


def test_sample_excludes_tool_results_reasoning_and_system(rollout):
    turns = A.export(SID).turns
    text = str(turns)
    assert "REDACTED (developer/system prompt)" not in text
    assert "encrypted_content" not in text
    assert "Script completed" not in text
    assert text.count("[tool] exec") == 2


def test_c11_unreadable_export(rollout, monkeypatch):
    def denied(p):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(AgentError):
        A.export(SID)


def test_last_message_only_reads_tail(rollout, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("preview must not export or read the whole rollout")
    monkeypatch.setattr(A, "export", forbidden)
    monkeypatch.setattr(codex, "_read", forbidden)
    assert A.last_message(SID) == ("assistant", "已停下，等候指示。")
    assert A.last_message("missing") is None


@pytest.mark.parametrize("bad", [b'{}\nBROKEN\n{}\n', b'{}\n{"type":'])
def test_fix1_unrelated_bad_rollout_imports(rollout, lab, capsys, bad):
    other = rollout.with_name("rollout-a-00000000-0000-0000-0000-000000000002.jsonl")
    other.write_bytes(bad)
    assert other < rollout
    code, out, err = run(capsys, "import", "session", "--agent", "codex", "--external-session-id", SID)
    assert code == 0 and out.startswith("agora:"), err
    assert "最後一行不完整" not in err
    assert other.name not in err


def test_fix1_renamed_target_skips_bad_and_unreadable_headers(rollout, monkeypatch, capsys):
    renamed = rollout.with_name("rollout-z-renamed.jsonl")
    rollout.rename(renamed)
    bad = renamed.with_name("rollout-a-bad.jsonl")
    bad.write_bytes(b'BROKEN\n')
    denied = renamed.with_name("rollout-b-denied.jsonl")
    denied.touch()
    original = Path.open
    def open_path(p, *args, **kwargs):
        if p == denied:
            raise PermissionError("denied")
        return original(p, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_path)
    assert A.export(SID).message_count == 11
    assert A.last_message(SID) == ("assistant", "已停下，等候指示。")
    assert not capsys.readouterr().err


def test_fix1_search_reads_each_rollout_once(rollout, monkeypatch):
    other_sid = "00000000-0000-0000-0000-000000000002"
    other = rollout.with_name(f"rollout-other-{other_sid}.jsonl")
    other.write_bytes(rollout.read_bytes().replace(SID.encode(), other_sid.encode()))
    seen = []
    original = codex._read
    def read(p):
        seen.append(p)
        return original(p)
    monkeypatch.setattr(codex, "_read", read)
    assert set(A.search_text("樣本測試完成")) == {SID, other_sid}
    assert sorted(seen) == sorted([rollout, other])


def test_fix1_invalid_metadata_id_not_listed(rollout):
    rollout.write_bytes(rollout.read_bytes().replace(SID.encode(), b"invalid-id"))
    assert A.list_sessions() == []
    assert list(A.search_text("樣本")) == []
    assert A.last_message(SID) is None
    with pytest.raises(AgentError):
        A.export(SID)


def test_fix1_only_sessions_scanned(rollout, monkeypatch):
    outside = Path(os.environ["AGORA_CODEX_HOME"]) / "rollout-outside.jsonl"
    outside.write_bytes(b'BROKEN\n')
    assert A.export(SID).message_count == 11
    assert [s.session_id for s in A.list_sessions()] == [SID]
    assert list(A.search_text("樣本")) == [SID]
    assert outside not in codex._files()


def test_fix1_preview_discards_partial_first_tail_line(rollout):
    # Synthetic whitespace-padded message: the 64 KiB tail starts inside this
    # line but its suffix is independently valid JSON. It must still be dropped.
    message = json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "partial-line"}]}}).encode()
    header = rollout.read_bytes().splitlines(keepends=True)[0]
    rollout.write_bytes(header + b' ' * 70000 + message + b'\n{}\n')
    assert A.last_message(SID) is None


def test_fix1_title_first_user(rollout):
    assistant = json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "assistant-before-user"}]}}).encode() + b'\n'
    lines = rollout.read_bytes().splitlines(keepends=True)
    rollout.write_bytes(lines[0] + assistant + b''.join(lines[1:]))
    expected = next(text[:80] for role, text in codex._parse(codex._rows(rollout.read_bytes()))[1] if role == "user")
    assert A.export(SID).title == expected
    assert A.list_sessions()[0].title == expected


# --- #487: list_sessions keeps (path, mtime, size) -> listed fields ----------


@pytest.fixture(autouse=True)
def clear_list_cache():
    """The cache is module state: no test may see the previous test's keys."""
    codex._LIST_CACHE.clear()
    yield
    codex._LIST_CACHE.clear()


def sessions_dir():
    d = Path(os.environ["AGORA_CODEX_HOME"]) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_rollout(name, sid=None, *, user="hello", cwd="/work", assistant="ok"):
    rows = []
    meta = {"timestamp": "2026-11-08T00:00:00Z"}
    if sid:
        meta["id"] = sid
    rows.append({"type": "session_meta", "payload": meta})
    if user is not None:
        rows.append({"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": user}]}})
    if assistant is not None:
        rows.append({"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": assistant}]}})
    path = sessions_dir() / name
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows))
    return path


def read_calls(monkeypatch):
    """Count `codex._read` calls and hand back the list the calls land in."""
    calls = []
    original = codex._read

    def counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(codex, "_read", counting)
    return calls


def old_list_sessions():
    """The pre-#487 body, verbatim: the result the cache must not change."""
    listed = []
    for path in codex._files():
        rows = codex._rows(codex._read(path))
        sid = codex._id(path, rows)
        if sid:
            texts = codex._parse(rows)[1]
            updated = codex.iso_utc(path.stat().st_mtime)
            listed.append(Listed(sid, codex._meta(rows).get("cwd"),
                                 next((t[:80] for r, t in texts if r == "user"), None), updated))
    return listed


def test_f3_second_call_does_not_read(monkeypatch):
    write_rollout("rollout-a-00000000-0000-0000-0000-000000000002.jsonl",
                  "00000000-0000-0000-0000-000000000002")
    first = A.list_sessions()
    calls = read_calls(monkeypatch)
    second = A.list_sessions()
    assert second == first
    assert calls == []


def test_f3_size_change_rereads_even_with_same_mtime(monkeypatch):
    write_rollout("rollout-same-00000000-0000-0000-0000-000000000002.jsonl",
                  "00000000-0000-0000-0000-000000000002")
    changed = write_rollout("rollout-changed-00000000-0000-0000-0000-000000000003.jsonl",
                            "00000000-0000-0000-0000-000000000003")
    before = {s.session_id: s for s in A.list_sessions()}
    stat = changed.stat()
    calls = read_calls(monkeypatch)
    changed.write_bytes(changed.read_bytes().replace(b'"hello"', b'"hello again"'))
    os.utime(changed, ns=(stat.st_atime_ns, stat.st_mtime_ns))   # mtime put back: size differs
    after = {s.session_id: s for s in A.list_sessions()}
    assert calls == [changed]                       # only this one was read
    assert after["00000000-0000-0000-0000-000000000003"].title == "hello again"
    assert after["00000000-0000-0000-0000-000000000002"] == before["00000000-0000-0000-0000-000000000002"]


def test_f3_mtime_change_rereads(monkeypatch):
    path = write_rollout("rollout-moved-00000000-0000-0000-0000-000000000002.jsonl",
                         "00000000-0000-0000-0000-000000000002")
    first = A.list_sessions()[0]
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))   # size unchanged
    calls = read_calls(monkeypatch)
    second = A.list_sessions()[0]
    assert calls == [path]
    assert second.updated_at != first.updated_at


def test_f3_deleted_file_drops_and_cache_pruned():
    gone = write_rollout("rollout-gone-00000000-0000-0000-0000-000000000002.jsonl",
                         "00000000-0000-0000-0000-000000000002")
    keep = write_rollout("rollout-keep-00000000-0000-0000-0000-000000000003.jsonl",
                         "00000000-0000-0000-0000-000000000003")
    assert len(A.list_sessions()) == 2
    assert {k[0] for k in codex._LIST_CACHE} == {str(gone), str(keep)}
    gone.unlink()
    assert [s.session_id for s in A.list_sessions()] == ["00000000-0000-0000-0000-000000000003"]
    assert {k[0] for k in codex._LIST_CACHE} == {str(keep)}


def test_f3_no_sid_not_listed_and_cached(monkeypatch):
    write_rollout("rollout-noid.jsonl", None, user="no id anywhere")
    assert A.list_sessions() == []
    assert len(codex._LIST_CACHE) == 1
    calls = read_calls(monkeypatch)
    assert A.list_sessions() == []
    assert calls == []                    # "not listed" is a result too


def test_f3_corrupt_file_raises_as_before_and_is_not_cached():
    bad = sessions_dir() / "rollout-bad-00000000-0000-0000-0000-000000000002.jsonl"
    bad.write_bytes(b'{}\nBROKEN\n{}\n')
    write_rollout("rollout-ok-00000000-0000-0000-0000-000000000003.jsonl",
                  "00000000-0000-0000-0000-000000000003")
    # Pre-#487 (checked on a copy of main): the whole list raises, not just skips.
    for _ in range(2):
        with pytest.raises(AgentError, match="不是有效 JSONL"):
            A.list_sessions()
        assert not any(k[0] == str(bad) for k in codex._LIST_CACHE)
    bad.unlink()
    assert [s.session_id for s in A.list_sessions()] == ["00000000-0000-0000-0000-000000000003"]


def test_f3_same_results_as_old_implementation(rollout):
    write_rollout("rollout-long-00000000-0000-0000-0000-000000000002.jsonl",
                  "00000000-0000-0000-0000-000000000002", user="長" * 120, cwd="/long")
    write_rollout("rollout-nouser-00000000-0000-0000-0000-000000000003.jsonl",
                  "00000000-0000-0000-0000-000000000003", user=None, assistant="hi")
    write_rollout("rollout-nameless-00000000-0000-0000-0000-000000000004.jsonl",
                  None, user="id only in the file name")
    write_rollout("rollout-noid.jsonl", None, user="unlisted")
    expected = old_list_sessions()
    got = A.list_sessions()
    assert [(s.session_id, s.dir, s.title, s.updated_at) for s in got] == \
           [(s.session_id, s.dir, s.title, s.updated_at) for s in expected]
    assert [s.title for s in got if s.session_id.endswith("0002")] == ["長" * 80]


def test_f3_cache_does_not_leak_into_other_readers(rollout):
    # export / search_text / last_message must keep reading the file itself.
    assert [s.session_id for s in A.list_sessions()] == [SID]
    rollout.write_bytes(rollout.read_bytes() + b'{"type":"future_event","payload":{}}\n')
    assert A.export(SID).message_count == 11
    assert "future_event" in str(A.export(SID).turns)
    assert list(A.search_text("樣本測試完成")) == [SID]
    assert A.last_message(SID) is not None
