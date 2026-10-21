"""The agy adapter (read-only PR1 of #26): snapshot, export, turns, listing.

Every db here is made by `tests/fixtures/agy/make_agy_db.py` inside a temp
`AGORA_AGY_HOME`; the summary table is built by a helper in this file because
PR0 never deciphered the real one. Nothing reads the user's agy home, a real
session, or a credential file.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import base64
import builtins
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from agora import cache, cli, store, tui
from agora.agents import agy
from agora.agents.base import AgentError, Launch
from agora import header as h

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agy"
FAKE_RCLONE = Path(__file__).resolve().parents[1] / "fakes" / "fake_rclone.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MUT = _load("make_agy_db")

TURNS = [
    {"user": "hello probe", "model": "hello probe"},
    {"user": "run echo hi and report its output", "model": "OK",
     "tool": {"name": "run_command",
              "args": {"CommandLine": "echo hi", "toolSummary": "Echo hi"},
              "command": "echo hi", "output": "hi"}},
]


@pytest.fixture(autouse=True)
def fresh_module_caches(monkeypatch):
    """The module's caches and warn-once flags must not leak between tests."""
    monkeypatch.setattr(agy, "_summaries_cache", None)
    monkeypatch.setattr(agy, "_warned_summary_for", False)
    monkeypatch.setattr(agy, "_warned_stamp", False)


@pytest.fixture
def agy_home(tmp_path, monkeypatch):
    home = tmp_path / "antigravity-cli"
    (home / "conversations").mkdir(parents=True)
    monkeypatch.setenv("AGORA_AGY_HOME", str(home))
    return home


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """A fake Drive and an rclone wrapper, the same seam test_cli.py uses."""
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def make_conversation(home: Path, turns: list[dict] | None = None, *,
                      seed: str = "pr1", with_wal: bool = False,
                      raw_steps: list[dict] | None = None) -> tuple[str, dict]:
    cascade_id = MUT._uuid(f"{seed}-cascade")
    path = home / "conversations" / f"{cascade_id}.db"
    meta = MUT.make_db(str(path), TURNS if turns is None else turns,
                       cascade_id=cascade_id, seed=seed, with_wal=with_wal,
                       raw_steps=raw_steps)
    return cascade_id, meta


def count_steps(home: Path, session_id: str) -> int:
    con = sqlite3.connect(f"file:{home / 'conversations' / (session_id + '.db')}?immutable=1",
                          uri=True)
    try:
        return con.execute("SELECT count(*) FROM steps").fetchone()[0]
    finally:
        con.close()


def make_summaries(home: Path, rows: list[dict]) -> Path:
    """A fake conversation_summaries.db - only the columns PR1 relies on."""
    path = home / "conversation_summaries.db"
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id text PRIMARY KEY, title text, preview text, "
            "step_count integer, last_modified_time integer, workspace_uris text)")
        for row in rows:
            con.execute(
                "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?)",
                (row["id"], row.get("title"), row.get("preview"),
                 row.get("step_count"), row.get("last_modified_time"),
                 row.get("workspace_uris")))
        con.commit()
    finally:
        con.close()
    return path


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


# ---------------------------------------------------------------------------
# snapshot: both paths, no -shm, retry, cleanup
# ---------------------------------------------------------------------------


def test_snapshot_idle_copies_the_db_alone(agy_home, monkeypatch):
    session_id, _ = make_conversation(agy_home)
    source = agy_home / "conversations" / f"{session_id}.db"
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    copied: list[str] = []
    real = agy.shutil.copyfile

    def spy(src, dst):
        copied.append(Path(src).name)
        return real(src, dst)

    monkeypatch.setattr(agy.shutil, "copyfile", spy)
    with agy.snapshot(session_id) as path:
        assert path.is_file() and path.parent != source.parent
        con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        try:
            assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    assert copied == [f"{session_id}.db"]          # one file: no wal, no shm
    assert (source.stat().st_size, source.stat().st_mtime_ns) == before


def test_snapshot_replays_a_live_wal_without_copying_shm(agy_home, monkeypatch):
    session_id, meta = make_conversation(agy_home, with_wal=True)
    copied: list[str] = []
    real = agy.shutil.copyfile

    def spy(src, dst):
        copied.append(Path(src).name)
        return real(src, dst)

    monkeypatch.setattr(agy.shutil, "copyfile", spy)
    try:
        with agy.snapshot(session_id) as path:
            con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
            try:
                assert con.execute("SELECT count(*) FROM steps").fetchone()[0] == 6
            finally:
                con.close()
    finally:
        meta["connection"].close()
    assert f"{session_id}.db" in copied and f"{session_id}.db-wal" in copied
    assert not any(name.endswith("-shm") for name in copied)


def test_snapshot_retries_when_the_copy_moves(agy_home, monkeypatch):
    session_id, _ = make_conversation(agy_home)
    calls: list[str] = []
    real = agy.shutil.copyfile

    def moving(src, dst):
        calls.append(Path(src).name)
        real(src, dst)
        if len(calls) == 1:                       # a checkpoint lands mid-copy
            stat = Path(src).stat()
            os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    monkeypatch.setattr(agy.shutil, "copyfile", moving)
    with agy.snapshot(session_id) as path:
        assert path.is_file()
    assert len(calls) == 2                       # the second copy was stable


def test_snapshot_gives_up_after_three_moves_and_cleans_the_temp_dir(agy_home, monkeypatch):
    session_id, _ = make_conversation(agy_home)
    real = agy.shutil.copyfile
    calls = []

    def always_moving(src, dst):
        calls.append(1)
        real(src, dst)
        stat = Path(src).stat()
        os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    made: list[tempfile.TemporaryDirectory] = []

    class Recorder(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)          # the object, not its path: no GC finalizer may help

    monkeypatch.setattr(agy.shutil, "copyfile", always_moving)
    monkeypatch.setattr(agy.tempfile, "TemporaryDirectory", Recorder)
    with pytest.raises(AgentError, match="3 次"):
        with agy.snapshot(session_id):
            pass
    assert len(calls) == 3
    assert made and all(not Path(obj.name).exists() for obj in made)


def test_snapshot_success_cleans_the_temp_dir_immediately(agy_home, monkeypatch):
    """m10: cleanup is explicit, not the TemporaryDirectory finalizer on GC."""
    session_id, _ = make_conversation(agy_home)
    made: list[tempfile.TemporaryDirectory] = []

    class Recorder(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(agy.tempfile, "TemporaryDirectory", Recorder)
    with agy.snapshot(session_id) as path:
        assert path.is_file()
    assert made and all(not Path(obj.name).exists() for obj in made)


def test_snapshot_of_a_broken_db_fails_quick_check(agy_home):
    """m9: a file that is not a database is an AgentError, not a silent empty read."""
    (agy_home / "conversations" / "deadbeef.db").write_bytes(b"not a database" * 100)
    with pytest.raises(AgentError):
        with agy.snapshot("deadbeef"):
            pass


def test_snapshot_reports_a_page_quick_check_rejects(agy_home):
    """N2: a db that opens but has a corrupt index page hits `result != ["ok"]`."""
    session_id, _ = make_conversation(agy_home)
    db = agy_home / "conversations" / f"{session_id}.db"
    data = bytearray(db.read_bytes())
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        row = con.execute(
            "SELECT pageno FROM dbstat WHERE name='idx_steps_status'").fetchone()
    except sqlite3.Error:                  # dbstat not compiled in: the fixture's page
        row = None
    finally:
        con.close()
    page = int(row[0]) if row else 5
    data[(page - 1) * MUT.PAGE_SIZE] = 0   # an invalid b-tree page type, not a bad header
    db.write_bytes(data)
    with pytest.raises(AgentError, match="quick_check 沒過"):
        with agy.snapshot(session_id):
            pass


def test_snapshot_retries_when_only_the_wal_moves(agy_home, monkeypatch):
    """m11: the (db, wal) pair is compared, so a wal-only change is a retry too."""
    session_id, meta = make_conversation(agy_home, with_wal=True)
    wal = agy_home / "conversations" / f"{session_id}.db-wal"
    copied: list[str] = []
    real = agy.shutil.copyfile

    def moving_wal(src, dst):
        copied.append(Path(src).name)
        real(src, dst)
        if len(copied) == 1:
            stat = wal.stat()
            os.utime(wal, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    monkeypatch.setattr(agy.shutil, "copyfile", moving_wal)
    try:
        with agy.snapshot(session_id) as path:
            assert path.is_file()
    finally:
        meta["connection"].close()
    assert copied.count(f"{session_id}.db") == 2


def test_snapshot_retries_when_the_wal_vanishes(agy_home, monkeypatch):
    """W2: the checkpoint that removes the -wal mid-copy lands on the idle path."""
    session_id, meta = make_conversation(agy_home, with_wal=True)
    wal = agy_home / "conversations" / f"{session_id}.db-wal"
    copied: list[str] = []
    real = agy.shutil.copyfile

    def vanishing_wal(src, dst):
        copied.append(Path(src).name)
        if Path(src).name.endswith("-wal"):
            meta["connection"].close()      # the last connection: checkpoint + remove
            wal.unlink(missing_ok=True)
            raise FileNotFoundError(src)
        return real(src, dst)

    monkeypatch.setattr(agy.shutil, "copyfile", vanishing_wal)
    try:
        with agy.snapshot(session_id) as path:
            con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
            try:
                assert con.execute("SELECT count(*) FROM steps").fetchone()[0] == 6
            finally:
                con.close()
    finally:
        meta["connection"].close()
    assert copied.count(f"{session_id}.db") == 2      # second attempt: idle single file
    assert copied.count(f"{session_id}.db-wal") == 1  # the attempt that lost it


def test_snapshot_gives_up_when_files_keep_vanishing(agy_home, monkeypatch):
    session_id, _ = make_conversation(agy_home)
    calls = []

    def missing(src, dst):
        calls.append(1)
        raise FileNotFoundError(src)

    monkeypatch.setattr(agy.shutil, "copyfile", missing)
    with pytest.raises(AgentError, match="3 次"):
        with agy.snapshot(session_id):
            pass
    assert len(calls) == 3


def test_snapshot_cross_check_only_warns(agy_home, capsys):
    session_id, _ = make_conversation(agy_home)
    make_summaries(agy_home, [{"id": session_id, "title": "x",
                               "step_count": count_steps(agy_home, session_id) + 1,
                               "last_modified_time": MUT.EPOCH}])
    with agy.snapshot(session_id) as path:
        assert path.is_file()
    err = capsys.readouterr().err
    assert "summaries 說" in err and "以快照為準" in err


# ---------------------------------------------------------------------------
# export: the golden standard (two exports, same md5) and byte fidelity
# ---------------------------------------------------------------------------


def test_export_twice_has_the_same_md5_and_keeps_the_db_whole(agy_home):
    session_id, meta = make_conversation(agy_home)
    make_summaries(agy_home, [{"id": session_id, "title": "Echo test",
                               "preview": "hello probe",
                               "step_count": count_steps(agy_home, session_id),
                               "last_modified_time": MUT.EPOCH,
                               "workspace_uris": '["file:///tmp/agy-fixture-ws"]'}])
    first = agy.ADAPTER.export(session_id)
    second = agy.ADAPTER.export(session_id)
    assert first.raw_md5 == second.raw_md5
    assert first.raw_size == second.raw_size
    assert first.raw_file is not None and second.raw_file != first.raw_file

    raw = first.raw_file.read_bytes()
    document = json.loads(raw)
    assert document["format"] == "agy-conversation/1"
    assert document["agy_version"] is None
    assert document["trajectory_id"] == meta["trajectory_id"]
    assert "hello probe" in document["scan_text"]
    assert any("CommandLine" in line for line in document["scan_text"])
    assert any("Command exited with code 0" in line for line in document["scan_text"])
    # The db is the snapshot, byte for byte - gen_metadata and all (R1).
    assert base64.b64decode(document["db_b64"]) == (
        agy_home / "conversations" / f"{session_id}.db").read_bytes()
    assert first.message_count == 5          # 132 is silent; 14/15 with content only
    assert first.title == "Echo test" and first.dir == "/tmp/agy-fixture-ws"
    assert first.turns == agy.ADAPTER.turns(raw)


def test_export_of_a_missing_conversation_is_an_error(agy_home):
    with pytest.raises(AgentError, match="找不到 agy 對話"):
        agy.ADAPTER.export("00000000-0000-0000-0000-000000000000")


def test_export_refuses_an_id_that_walks_out_of_conversations(agy_home):
    with pytest.raises(AgentError, match="不合法"):
        agy.ADAPTER.export("../../etc/passwd")


# ---------------------------------------------------------------------------
# turns: user / model / tool / unknown / broken protobuf
# ---------------------------------------------------------------------------


def test_turns_call_once_and_never_the_tool_output(agy_home):
    """W3: 15's 20.7 is the one call line; 132 is known and silent (design 4.4)."""
    session_id, _ = make_conversation(agy_home)
    exported = agy.ADAPTER.export(session_id)
    turns = exported.turns
    assert turns is not None
    assert ("user", ["hello probe"]) in turns
    assert ("assistant", ["hello probe"]) in turns
    assert ("user", ["run echo hi and report its output"]) in turns
    tool_lines = [line for role, lines in turns if role == "assistant"
                  for line in lines if line.startswith("[tool] ")]
    assert len(tool_lines) == 1
    assert "run_command" in tool_lines[0] and "CommandLine" in tool_lines[0]
    reading = "\n".join(line for _role, lines in turns for line in lines)
    assert "→" not in reading and "Command exited" not in reading
    assert not any(line.startswith("[skip") for _role, lines in turns for line in lines)
    assert exported.message_count == 5


def test_scan_text_carries_tool_output_in_full_but_reading_does_not(agy_home):
    """W1: scan_text is its own coverage surface - no truncation, tool result included."""
    secret = "sk-SECRETSECRET1234567890"
    output = "A" * 300 + " " + secret
    session_id, _ = make_conversation(
        agy_home, turns=[{"user": "run it", "model": "done",
                          "tool": {"name": "run_command", "args": {"CommandLine": "tool"},
                                   "command": "tool", "output": output}}])
    exported = agy.ADAPTER.export(session_id)
    raw = exported.raw_file.read_bytes()
    document = json.loads(raw)
    assert secret in "\n".join(document["scan_text"])
    assert any(secret in text for text in document["scan_text"])
    reading = "\n".join(line for _role, lines in exported.turns for line in lines)
    assert secret not in reading and "A" * 300 not in reading


def test_scan_text_reads_render_info_too(agy_home):
    """N1: a string that exists only in `steps.render_info` still reaches scan_text."""
    session_id, _ = make_conversation(agy_home)
    secret = "sk-RENDERONLY1234567890"
    db = agy_home / "conversations" / f"{session_id}.db"
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE steps SET render_info=? WHERE step_type=132",
                    (MUT._render_info(secret, "/tmp/render-only", "ok"),))
        con.commit()
    finally:
        con.close()
    exported = agy.ADAPTER.export(session_id)
    document = json.loads(exported.raw_file.read_bytes())
    assert any(secret in text for text in document["scan_text"])


def test_scan_text_survives_deep_nesting_and_keeps_shallow_strings():
    """N1: >12 levels stop instead of recursing forever; the shallow string is kept."""
    shallow = "shallow-scan-string"
    blob = MUT.enc_text(1, shallow)
    deep = b""
    for _ in range(30):
        deep = MUT.enc_msg(2, deep)
    blob += deep
    out: list[str] = []
    assert agy._scan_strings(blob, out, set()) is None
    assert shallow in out


def test_multi_line_user_text_is_not_cut_to_its_first_line(agy_home):
    """r20: the reading keeps the whole prompt, not just the first line."""
    text = "first line\nsecond line\nthird line"
    session_id, _ = make_conversation(agy_home, [{"user": text, "model": "ok"}])
    turns = agy.ADAPTER.export(session_id).turns
    assert ("user", [text]) in turns
    assert "second line" in "\n".join(line for _role, lines in turns for line in lines)


def test_unknown_step_is_visible_and_warned_once(agy_home, capsys):
    raw = [{"idx": 4, "step_type": 77, "status": 0, "step_format": 0,
            "has_subtrajectory": 0, "payload": b""}]
    session_id, _ = make_conversation(agy_home, turns=TURNS[:1], raw_steps=raw)
    turns = agy.ADAPTER.export(session_id).turns
    assert ("user", ["[skip agy-step-4/77]"]) in turns
    err = capsys.readouterr().err
    assert err.count("[agora] agy：") == 1
    assert "1 個步驟不認得" in err


def test_a_broken_payload_skips_that_step_and_says_so(agy_home, capsys):
    raw = [{"idx": 4, "step_type": 14, "status": 0, "step_format": 0,
            "has_subtrajectory": 0, "payload": b"\x08"}]
    session_id, _ = make_conversation(agy_home, turns=TURNS[:1], raw_steps=raw)
    turns = agy.ADAPTER.export(session_id).turns
    assert ("user", ["[skip agy-step-4/14]"]) in turns
    err = capsys.readouterr().err
    assert "1 個步驟解不開" in err


def test_a_length_past_the_message_end_is_a_skip_not_a_truncation(agy_home, capsys):
    """m22: field 2 says 100 bytes but only 3 follow; without the check this read 'abc'."""
    payload = (MUT.enc_varint(1, 14) + MUT.enc_varint(4, 3)
               + MUT.enc_msg(19, b"\x12\x64abc"))
    raw = [{"idx": 4, "step_type": 14, "status": 0, "step_format": 0,
            "has_subtrajectory": 0, "payload": payload}]
    session_id, _ = make_conversation(agy_home, turns=[], raw_steps=raw)
    turns = agy.ADAPTER.export(session_id).turns
    assert ("user", ["[skip agy-step-4/14]"]) in turns
    assert all(line != "abc" for _role, lines in turns for line in lines)
    err = capsys.readouterr().err
    assert "1 個步驟解不開" in err


def test_fields_rejects_a_declared_length_past_the_buffer():
    with pytest.raises(ValueError, match="長度超過訊息"):
        list(agy._fields(b"\x0a\x05ab"))


# ---------------------------------------------------------------------------
# list_sessions / search_text / last_message
# ---------------------------------------------------------------------------


def test_list_sessions_reads_the_summaries_and_caches_by_mtime(agy_home, monkeypatch):
    older, _ = make_conversation(agy_home, [{"user": "older", "model": "ok"}], seed="older")
    newer, _ = make_conversation(agy_home, [{"user": "newer", "model": "ok"}], seed="newer")
    path = make_summaries(agy_home, [
        {"id": older, "title": "Old", "preview": "older", "step_count": 2,
         "last_modified_time": MUT.EPOCH, "workspace_uris": '["file:///tmp/older"]'},
        {"id": newer, "title": "New", "preview": "newer", "step_count": 2,
         "last_modified_time": MUT.EPOCH + 60, "workspace_uris": "/tmp/plain-dir"},
    ])
    copied: list[str] = []
    real = agy.shutil.copyfile

    def spy(src, dst):
        copied.append(str(src))
        return real(src, dst)

    monkeypatch.setattr(agy.shutil, "copyfile", spy)
    listed = agy.ADAPTER.list_sessions()
    assert [item.session_id for item in listed] == [newer, older]
    assert listed[0].title == "New" and listed[0].dir == "/tmp/plain-dir"
    assert listed[1].dir == "/tmp/older"
    assert listed[0].updated_at.endswith("Z")
    first_copies = len(copied)
    assert first_copies == 1

    assert [item.session_id for item in agy.ADAPTER.list_sessions()] == [newer, older]
    assert len(copied) == first_copies              # the (mtime, size) cache hit

    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
    agy.ADAPTER.list_sessions()
    assert len(copied) == first_copies + 1          # the file moved: read again


def test_list_sessions_without_summaries_is_empty(agy_home):
    assert agy.ADAPTER.list_sessions() == []


def test_an_unrecognised_summaries_table_is_an_error_not_an_empty_list(agy_home):
    """W4: an empty list would claim there are no conversations; the TUI shows errors."""
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        con.execute("CREATE TABLE unrelated (x text)")
        con.commit()
    finally:
        con.close()
    with pytest.raises(AgentError, match="結構認不出來"):
        agy.ADAPTER.list_sessions()


def test_summaries_column_names_are_matched_case_insensitively(agy_home):
    session_id, _ = make_conversation(agy_home, [{"user": "hello", "model": "ok"}])
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        con.execute("CREATE TABLE weird (CONVERSATION_ID text PRIMARY KEY, TITLE text, "
                    "PREVIEW text, STEP_COUNT integer, LAST_MODIFIED_TIME integer, "
                    "WORKSPACE_URIS text)")
        con.execute("INSERT INTO weird VALUES (?,?,?,?,?,?)",
                    (session_id, "Upper", "preview", 2, MUT.EPOCH, "/tmp/upper"))
        con.commit()
    finally:
        con.close()
    listed = agy.ADAPTER.list_sessions()
    assert [(item.session_id, item.title, item.dir) for item in listed] == [
        (session_id, "Upper", "/tmp/upper")]


def test_a_non_finite_step_count_does_not_take_the_listing_down(agy_home):
    session_id, _ = make_conversation(agy_home, [{"user": "hello", "model": "ok"}])
    make_summaries(agy_home, [{"id": session_id, "title": "x", "step_count": float("inf"),
                               "last_modified_time": MUT.EPOCH}])
    assert [item.session_id for item in agy.ADAPTER.list_sessions()] == [session_id]
    assert agy._summary_rows()[0].step_count is None


@pytest.mark.parametrize("value", [
    1_767_225_600,              # seconds
    1_767_225_600_000,          # milliseconds
    1_767_225_600_000_000,      # microseconds
    "2026-01-01T00:00:00Z",     # ISO
    "1767225600",               # numeric string
])
def test_iso_accepts_the_plausible_time_units(value):
    assert agy._iso(value) == "2026-01-01T00:00:00Z"


def test_iso_outside_the_window_is_unknown_and_warned_once(capsys):
    assert agy._iso(0) is None
    assert agy._iso(4_102_444_800) is None       # 2100-01-01 is outside (exclusive)
    err = capsys.readouterr().err
    assert err.count("時間欄位看不懂") == 1
    assert agy._iso(5) is None
    assert "時間欄位看不懂" not in capsys.readouterr().err


def test_a_broken_summaries_file_warns_once_but_the_export_still_works(agy_home, capsys):
    session_id, _ = make_conversation(agy_home)
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        con.execute("CREATE TABLE unrelated (x text)")
        con.commit()
    finally:
        con.close()
    first = agy.ADAPTER.export(session_id)
    second = agy.ADAPTER.export(session_id)
    assert first.raw_md5 == second.raw_md5
    assert first.title is None and first.dir is None
    err = capsys.readouterr().err
    assert err.count("讀不到 conversation_summaries.db") == 1


def test_search_text_sees_only_title_and_preview(agy_home):
    session_id, _ = make_conversation(agy_home, [{"user": "body-only-needle", "model": "ok"}])
    make_summaries(agy_home, [{"id": session_id, "title": "alpha title",
                               "preview": "beta preview", "step_count": 2,
                               "last_modified_time": MUT.EPOCH}])
    assert list(agy.ADAPTER.search_text("needle")) == []       # the body is not searched
    assert list(agy.ADAPTER.search_text("alpha")) == [session_id]
    assert list(agy.ADAPTER.search_text("BETA")) == [session_id]
    assert list(agy.ADAPTER.search_text("alpha", only=set())) == []


def test_last_message_role_comes_from_step_type(agy_home):
    answered, _ = make_conversation(agy_home, [{"user": "hello", "model": "the answer"}],
                                    seed="answered")
    assert agy.ADAPTER.last_message(answered) == ("assistant", "the answer")
    asked, _ = make_conversation(agy_home, [{"user": "still open?"}], seed="asked")
    assert agy.ADAPTER.last_message(asked) == ("user", "still open?")
    assert agy.ADAPTER.last_message("00000000-0000-0000-0000-000000000000") is None


def test_last_message_skips_a_tool_call_model_step_with_no_text(agy_home):
    """m28: a 15 step whose only content is a 20.7 call has no 20.1 to show."""
    session_id, _ = make_conversation(
        agy_home, turns=[{"user": "ask", "tool": {"name": "run_command",
                                                  "args": {"CommandLine": "echo hi"},
                                                  "command": "echo hi", "output": "hi"}}])
    assert agy.ADAPTER.last_message(session_id) == ("user", "ask")


def test_last_message_is_capped_at_preview_max(agy_home):
    long = "x" * (agy.PREVIEW_MAX + 500)
    session_id, _ = make_conversation(agy_home, [{"user": long}])
    role, text = agy.ADAPTER.last_message(session_id)
    assert role == "user"
    assert len(text) == agy.PREVIEW_MAX and text == long[-agy.PREVIEW_MAX:]


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", ".", "a\\b"])
def test_a_conversation_id_that_is_not_a_plain_name_is_refused(bad, agy_home):
    with pytest.raises(AgentError, match="不合法"):
        agy.ADAPTER.export(bad)


# ---------------------------------------------------------------------------
# the boundary: the adapter's stubs and the command's early refusal
# ---------------------------------------------------------------------------


def test_the_five_unsupported_methods_refuse_with_one_message(tmp_path):
    calls = [
        lambda: agy.ADAPTER.native([]),
        lambda: agy.ADAPTER.append_native(b"", []),
        lambda: agy.ADAPTER.summarize("prompt", tmp_path),
        lambda: agy.ADAPTER.start_native(b"", tmp_path),
        lambda: agy.ADAPTER.collect(Launch(argv=[], cwd=str(tmp_path), agent_session_id=None)),
    ]
    for call in calls:
        with pytest.raises(AgentError, match="agy 第一版只支援 import／讀取"):
            call()


@pytest.mark.parametrize("argv", [
    ["merge", "session", "agora:01M3XB78", "agora:01M3XED5", "--agent", "agy"],
    ["continue", "session", "agora:01M3XB78", "--agent", "agy"],
    ["continue", "session", "agora:01M3XB78", "--agent", "agy", "--fork"],
])
def test_cli_refuses_agy_at_the_boundary(argv, capsys):
    code, out, err = run_cli(capsys, *argv)
    assert code == cli.EXIT_INPUT
    assert not out
    assert "agy 第一版只支援 import／讀取；接續在 PR2、fork／merge 為後續（#26）" in err


def _import_one_agy(home: Path, capsys, seed: str) -> str:
    session_id, _ = make_conversation(home, [{"user": f"hello {seed}", "model": "ok"}], seed=seed)
    code, out, err = run_cli(capsys, "import", "session",
                             "--external-session-id", session_id, "--agent", "agy")
    assert code == 0, err
    return out.splitlines()[-1]


@pytest.mark.parametrize("argv", [
    ["continue", "session", "{first}", "--agent", "claude"],
    ["continue", "session", "{first}", "--agent", "claude", "--fork"],
    ["continue", "session", "{first}", "--agent", "opencode"],
    ["merge", "session", "{first}", "{second}", "--agent", "claude"],
])
def test_a_session_imported_from_agy_is_refused_as_a_source(argv, agy_home, drive, capsys):
    """N7: a cross-agent move whose *source* is agy is outside PR1 too."""
    first = _import_one_agy(agy_home, capsys, "agy-source-1")
    second = _import_one_agy(agy_home, capsys, "agy-source-2")
    code, out, err = run_cli(capsys, *[arg.format(first=first, second=second) for arg in argv])
    assert code == cli.EXIT_INPUT
    assert not out
    assert "agy 第一版只支援 import／讀取" in err


@pytest.mark.parametrize("argv", [
    ["continue", "session", "{first}", "--agent", "opencode"],
    ["continue", "session", "{first}", "--agent", "claude", "--fork"],
    ["merge", "session", "{first}", "{second}", "--agent", "claude"],
])
def test_a_drive_only_agy_source_is_refused_before_any_agent(argv, agy_home, drive,
                                                             tmp_path, monkeypatch, capsys):
    """W5: after the cache is gone the source is known only after the sync.

    The two commands point at files that do not exist: reaching an agent at all
    would be an exit 2, so this pins the post-sync refusal, not just the local
    index check (review r07/r08).
    """
    first = _import_one_agy(agy_home, capsys, "agy-drive-1")
    second = _import_one_agy(agy_home, capsys, "agy-drive-2")
    shutil.rmtree(store.Paths.from_env().cache)
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(tmp_path / "no-opencode"))
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(tmp_path / "no-claude"))
    code, out, err = run_cli(capsys, *[arg.format(first=first, second=second) for arg in argv])
    assert code == cli.EXIT_INPUT
    assert not out
    assert "agy 第一版只支援 import／讀取" in err


# ---------------------------------------------------------------------------
# import end to end, and the credential files it must never open
# ---------------------------------------------------------------------------


def test_import_agy_end_to_end(agy_home, drive, capsys):
    session_id, _ = make_conversation(agy_home)
    make_summaries(agy_home, [{"id": session_id, "title": "Echo test",
                               "preview": "hello probe",
                               "step_count": count_steps(agy_home, session_id),
                               "last_modified_time": MUT.EPOCH,
                               "workspace_uris": '["file:///tmp/agy-fixture-ws"]'}])
    code, out, err = run_cli(capsys, "import", "session",
                             "--external-session-id", session_id, "--agent", "agy")
    assert code == 0, err
    agora_id = out.splitlines()[-1]
    assert agora_id.startswith("agora:")
    paths = store.Paths.from_env()
    document = (paths.mirror / agora_id.split(":")[1] / "session.md").read_text()
    hdr, body = h.split_document(document)
    assert hdr["agora"]["source"]["agent"] == "agy"
    assert hdr["agora"]["source"]["session_id"] == session_id
    assert "hello probe" in body and "[tool] run_command" in body
    raw_files = list((paths.mirror / agora_id.split(":")[1]).glob("raw-*.json"))
    assert len(raw_files) == 1
    assert json.loads(raw_files[0].read_text())["format"] == "agy-conversation/1"

    # The same bytes again: no new version, the same id, no error.
    code, again, err = run_cli(capsys, "import", "session",
                               "--external-session-id", session_id, "--agent", "agy")
    assert code == 0 and again == agora_id, err


def test_agy_never_opens_a_credential_file(agy_home, drive, tmp_path, monkeypatch, capsys):
    session_id, _ = make_conversation(agy_home)
    make_summaries(agy_home, [{"id": session_id, "title": "Echo test",
                               "step_count": count_steps(agy_home, session_id),
                               "last_modified_time": MUT.EPOCH}])
    (agy_home / "google_accounts.json").write_text("{}", encoding="utf-8")
    (agy_home / "antigravity-oauth-token").write_text("token", encoding="utf-8")
    seen: list[str] = []
    real_open = builtins.open
    real_path_open = Path.open
    real_connect = sqlite3.connect

    def spy_open(file, *args, **kwargs):
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_path_open(self, *args, **kwargs):
        seen.append(str(self))
        return real_path_open(self, *args, **kwargs)

    def spy_connect(database, *args, **kwargs):
        seen.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "open", spy_path_open)
    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    agy.ADAPTER.list_sessions()
    agy.ADAPTER.export(session_id)
    agy.ADAPTER.last_message(session_id)
    code, _out, err = run_cli(capsys, "import", "session",
                              "--external-session-id", session_id, "--agent", "agy")
    assert code == 0, err
    assert seen and any(session_id in path for path in seen)
    assert not [path for path in seen
                if path.endswith("google_accounts.json") or path.endswith("antigravity-oauth-token")]


# ---------------------------------------------------------------------------
# the three surfaces AGENTS puts agy on: import tab, content search, cache
# ---------------------------------------------------------------------------


def test_tui_import_tab_lists_agy_sessions(agy_home):
    session_id, _ = make_conversation(agy_home)
    make_summaries(agy_home, [{"id": session_id, "title": "Echo test",
                               "step_count": 4, "last_modified_time": MUT.EPOCH,
                               "workspace_uris": '["file:///tmp/agy-fixture-ws"]'}])
    paths = store.Paths.from_env()
    rows = tui.import_rows(store.Index(paths), [agy.ADAPTER], paths)
    assert [row.key for row in rows] == [f"agy:{session_id}"]
    assert rows[0].agent == "agy" and "Echo test" in rows[0].text


def test_tui_content_search_explains_agy_only_sees_title_and_preview(agy_home):
    session_id, _ = make_conversation(agy_home, [{"user": "body-only-needle", "model": "ok"}])
    make_summaries(agy_home, [{"id": session_id, "title": "alpha",
                               "preview": "beta", "step_count": 2,
                               "last_modified_time": MUT.EPOCH}])

    class Other:
        name = "claude"

    assert tui.content_search_note([Other(), agy.ADAPTER]) == tui.AGY_SEARCH_NOTE
    assert "agy 只搜標題與 preview（不搜內文）" in tui.AGY_SEARCH_NOTE
    assert tui.content_search_note([Other()]) is None
    assert list(agy.ADAPTER.search_text("needle")) == []
    assert list(agy.ADAPTER.search_text("alpha")) == [session_id]


def test_cache_prefill_of_an_agy_session(agy_home):
    session_id, _ = make_conversation(agy_home)
    make_summaries(agy_home, [{"id": session_id, "title": "Echo test",
                               "step_count": count_steps(agy_home, session_id),
                               "last_modified_time": MUT.EPOCH}])
    paths = store.Paths.from_env()
    done, failed = cache.pull(paths, [f"agy:{session_id}"], {"agy": agy.ADAPTER})
    assert (done, failed) == (1, 0)
    cached = paths.reading / "agy" / f"{session_id}.md"
    assert cached.is_file() and "hello probe" in cached.read_text(encoding="utf-8")
    done, failed = cache.pull(paths, [f"agy:{session_id}"], {"agy": agy.ADAPTER})
    assert (done, failed) == (0, 0)                 # the cache is fresh: a skip
