"""#222: several `agora import` at once must not die on `database is locked`.

The index is a cache in `<cache>/index.sqlite`; every import writes it a few times. These tests
run real processes against one isolated cache and a fake rclone, and check the two rules the
index follows when a write cannot get through: before the outbox has the session nothing is left
behind, and after it a lost index update is a warning, not a failed import.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C
from test_agent_claude_increment import Session
from test_cli_more import paths, run
from test_segment_write import env as _env  # noqa: F401  (the fake-rclone Drive)

N = 16          # the issue measured 8 on a slow disk; a fast one needs more to collide every time
LIMIT = 90          # seconds for the whole fleet; a hung child is killed, never waited for

#: Waits for the go-file, so all children reach the index together instead of one by one.
CHILD = textwrap.dedent("""
    import os, sys, time
    go = sys.argv[1]
    while not os.path.exists(go):
        time.sleep(0.005)
    from agora import cli
    sys.exit(cli.main(sys.argv[2:]))
""")


def _sid(k: int) -> str:
    return f"aaaaaaaa-bbbb-4ccc-8ddd-{k:012d}"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(tmp_path / "claude-home"))
    proj = tmp_path / "claude-home" / ".claude" / "projects" / "-golden-inc"
    proj.mkdir(parents=True)
    kids: list[subprocess.Popen] = []
    yield proj, kids
    for p in kids:                      # only the pids this test started
        if p.poll() is None:
            p.kill()
        p.wait()
    paths_ = store.Paths.from_env()     # the detached uploaders end on their own once the outbox is empty
    end = time.time() + 30
    while store.uploader_is_running(paths_) and time.time() < end:
        time.sleep(0.2)


def _spawn(kids, tmp_path, k: int, go):
    script = tmp_path / "child.py"
    if not script.exists():             # written once: a child must never read it half-written
        script.write_text(CHILD)
    p = subprocess.Popen(
        [sys.executable, str(script), str(go), "import", "session",
         "--external-session-id", _sid(k), "--agent", "claude"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "AGORA_UPLOAD": "detached"})   # the real uploader; inline ones fight over one lock
    kids.append(p)
    return p


def test_many_imports_at_once_all_succeed_and_the_index_matches_the_mirror(_env, fleet, tmp_path):
    proj, kids = fleet
    for k in range(N):
        Session(proj, _sid(k)).turns(30)
    go = tmp_path / "go"
    (tmp_path / "child.py").write_text(CHILD)
    procs = [_spawn(kids, tmp_path, k, go) for k in range(N)]
    time.sleep(1.5)                     # let every child get to its wait loop
    go.write_text("go")
    deadline = time.time() + LIMIT
    results = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=max(1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            pytest.fail(f"an import hung for {LIMIT}s: {err[-300:]}")
        results.append((p.returncode, out.strip(), err))
    bad = [r for r in results if r[0]]
    if bad:
        pytest.fail("\n".join(f"exit {r[0]}:\n{r[2]}" for r in bad), pytrace=False)
    assert all(r[1].startswith("agora:") for r in results), [(r[1], r[2]) for r in results]
    ulids = {r[1].split(":")[1] for r in results}
    assert len(ulids) == N
    p0 = store.Paths.from_env()
    on_disk = {d.name for d in p0.mirror.iterdir() if (d / "session.md").is_file()}
    assert ulids <= on_disk
    assert set(store.Index(p0).known()) == on_disk


def _import_args(sid):
    return ["import", "session", "--external-session-id", sid, "--agent", "claude"]


@pytest.fixture
def one(_env, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(tmp_path / "claude-home"))
    proj = tmp_path / "claude-home" / ".claude" / "projects" / "-golden-inc"
    proj.mkdir(parents=True)
    Session(proj, _sid(1)).turns(10)
    return _sid(1)


def _locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")


def test_a_lost_index_write_after_the_outbox_is_a_warning_and_the_session_is_whole(one, capsys, monkeypatch):
    real_put = store.Index.put
    monkeypatch.setattr(store.Index, "put", _locked)
    code, out, err = run(capsys, *_import_args(one))
    assert code == 0, err
    ulid = out.split(":")[1]
    assert "索引更新失敗" in err and "database is locked" in err
    p = paths()
    assert (p.mirror / ulid / "session.md").is_file()
    hdr, _ = h.split_document((p.mirror / ulid / "session.md").read_text(encoding="utf-8"))
    assert h.agora_of(hdr)["raw"]["segments"], "the raw is whole too"
    monkeypatch.setattr(store.Index, "put", real_put)    # the next sync puts it back into the index
    assert store.Index(p).header(ulid) is None
    index = store.sync(p)
    assert index.header(ulid) is not None


def test_a_failure_before_the_outbox_leaves_nothing_behind(one, capsys, monkeypatch):
    monkeypatch.setattr(store.Index, "by_source", _locked)   # read before anything is staged
    code, out, err = run(capsys, *_import_args(one))
    assert code != 0 and "database is locked" in err
    p = paths()
    assert not p.outbox.exists() or not any(p.outbox.iterdir())
    assert not p.mirror.exists() or not any(p.mirror.iterdir())
    assert out == ""


def test_the_index_is_wal_with_a_long_wait_and_an_existing_one_is_opened_without_ddl(_env):
    p = store.Paths.from_env()
    first = store.Index(p)
    assert first.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first.db.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000
    statements = []
    real = sqlite3.connect

    def spy(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(statements.append)
        return conn
    import unittest.mock as mock
    with mock.patch.object(sqlite3, "connect", spy):
        store.Index(p)
    assert not [s for s in statements if s.lstrip().upper().startswith(("CREATE", "DROP", "BEGIN", "PRAGMA USER_VERSION =", "COMMIT"))], statements


def test_two_writers_wait_for_each_other_instead_of_failing(_env):
    import threading
    p = store.Paths.from_env()
    store.Index(p)
    held, release = threading.Event(), threading.Event()

    def holder():                                        # another process's write, kept open for a moment
        db = sqlite3.connect(p.cache / "index.sqlite", isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        held.set()
        release.wait(10)
        db.execute("COMMIT")
        db.close()
    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(10)
    threading.Timer(0.5, release.set).start()
    start = time.time()
    store.Index(p).put("01M5RFRYEFFS0ZS6Y1Z1BX0GAT", "m" * 32,
                       {"type": "Session", "title": "x", "tags": [], "refs": [], "agora": {"header": 2}}, "body")
    t.join(10)
    assert time.time() - start >= 0.3, "it had to wait for the holder"
    assert "01M5RFRYEFFS0ZS6Y1Z1BX0GAT" in store.Index(p).known()


def test_the_read_only_inventory_opens_a_wal_index_and_the_extra_files_are_expected(_env):
    from agora import migrate
    p = store.Paths.from_env()
    idx = store.Index(p)
    idx.put("01M5RFRYEFFS0ZS6Y1Z1BX0GAT", "m" * 32, {"type": "Session", "title": "x", "tags": [], "refs": [],
                                                       "agora": {"header": 2}}, "body")
    assert (p.cache / "index.sqlite-wal").exists()       # the files WAL adds beside the db
    db = migrate.open_index(p)                           # mode=ro must still work with them there
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    db.close()


class _FlakyDb:
    """The connection, except that the first BEGINs say `database is locked`."""

    def __init__(self, db, failures):
        self.db, self.failures = db, failures

    def execute(self, sql, *a):
        if sql.startswith("BEGIN") and self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return self.db.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self.db, name)


def test_a_write_that_cannot_get_the_lock_is_tried_again_a_bounded_number_of_times(_env):
    idx = store.Index(store.Paths.from_env())
    hdr = {"type": "Session", "title": "x", "tags": [], "refs": [], "agora": {"header": 2}}
    assert store.INDEX_TRIES >= 3
    idx.db = _FlakyDb(idx.db, 2)
    idx.put("01M5RFRYEFFS0ZS6Y1Z1BX0GAT", "m" * 32, hdr, "body")      # the third try gets through
    assert "01M5RFRYEFFS0ZS6Y1Z1BX0GAT" in idx.known()
    idx.db = _FlakyDb(idx.db.db, store.INDEX_TRIES)
    with pytest.raises(sqlite3.OperationalError, match="locked"):     # then it gives up, loudly
        idx.put("01M5RFRYEFFS0ZS6Y1Z1BX0GAU", "m" * 32, hdr, "body")
    assert "01M5RFRYEFFS0ZS6Y1Z1BX0GAU" not in idx.known()


def test_put_is_one_transaction_that_takes_the_write_lock_first(_env):
    """Delete-then-insert of one ulid must not be split: that is the UNIQUE failure two processes hit."""
    idx = store.Index(store.Paths.from_env())
    seen = []
    idx.db.set_trace_callback(lambda sql: seen.append(sql) if not sql.startswith("--") else None)
    idx.put("01M5RFRYEFFS0ZS6Y1Z1BX0GAT", "m" * 32,
            {"type": "Session", "title": "x", "tags": [], "refs": [], "agora": {"header": 2}}, "body")
    words = [s.split()[0].upper() + (" " + s.split()[1].upper() if s.upper().startswith(("BEGIN", "INSERT")) else "")
             for s in seen]
    assert words[0] == "BEGIN IMMEDIATE" and words[-1] == "COMMIT"
    assert "DELETE" in words and "INSERT INTO" in words and words.count("COMMIT") == 1


def test_remember_never_shows_a_half_copied_session_md_to_a_reader(_env, tmp_path):
    """A sync in another process indexes the same outbox entry into the same mirror file."""
    import threading
    p = store.Paths.from_env()
    ulid = "01M5RFRYEFFS0ZS6Y1Z1BX0GAT"
    folder = p.outbox / ulid
    folder.mkdir(parents=True)
    hdr = {"type": "Session", "title": "x", "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
                     "relation": "import", "parents": [], "source": {"agent": "claude", "session_id": "s"}}}
    (folder / "session.md").write_text(h.dump_document(hdr, "字" * 1_500_000), encoding="utf-8")
    target = p.mirror / ulid / "session.md"
    store.remember(p, folder, store.Index(p))
    size = target.stat().st_size
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                n = len(target.read_bytes())
            except FileNotFoundError:
                continue
            if n != size:
                bad.append(n)
    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(5):
            store.remember(p, folder, store.Index(p))
    finally:
        stop.set()
        t.join(10)
    assert bad == []


def test_a_deleted_index_does_not_inherit_the_old_wal(_env):
    """`rm index.sqlite` is the way to rebuild; the -wal left beside it must not come back to life."""
    p = store.Paths.from_env()
    old = store.Index(p)
    old.put("01M5RFRYEFFS0ZS6Y1Z1BX0GAT", "m" * 32,
            {"type": "Session", "title": "x", "tags": [], "refs": [], "agora": {"header": 2}}, "body")
    assert (p.cache / "index.sqlite-wal").stat().st_size > 0      # still open: the row is only in the wal
    (p.cache / "index.sqlite").unlink()
    fresh = store.Index(p)
    assert fresh.known() == {}
    fresh.put("01M5RFRYEFFS0ZS6Y1Z1BX0GAU", "n" * 32,
              {"type": "Session", "title": "y", "tags": [], "refs": [], "agora": {"header": 2}}, "body")
    assert set(store.Index(p).known()) == {"01M5RFRYEFFS0ZS6Y1Z1BX0GAU"}
