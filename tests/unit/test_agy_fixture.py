"""The agy fixture generator builds agy-shaped databases PR1 can test against.

Covers docs/agy-storage.md claims that PR0 proved against fake conversations:
the verbatim schema and pragmas, the resolved wire-format paths (user/model/tool
steps, display tree, opaque call blob), the metadata==payload-field-5 rule, the
trajectory/cascade/exec ids, reproducible `VACUUM INTO` exports, the live-writer
(db + -wal, no -shm) snapshot shape, unknown-step pass-through, and a privacy
scan of every generated artifact plus the fixture sources.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "fixtures" / "agy" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MUT = _load("make_agy_db")
PROTO = _load("agy_proto")

DEMO_TURNS = [
    {"user": "hello probe", "model": "hello probe"},
    {"user": "run echo hi and report its output", "model": "OK",
     "tool": {"name": "run_command",
              "args": {"CommandLine": "echo hi", "toolSummary": "Echo hi"},
              "command": "echo hi", "output": "hi"}},
]

# docs/agy-storage.md §2, verbatim (sqlite_master omits the statement ";").
DOCS_SCHEMA = [
    "CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,"
    "`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`))",
    "CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,"
    "`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL"
    " DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,"
    "`task_details` blob,`render_info` blob,`step_payload` blob,"
    "`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`))",
    "CREATE INDEX `idx_steps_status` ON `steps`(`status`)",
    "CREATE INDEX `idx_steps_step_type` ON `steps`(`step_type`)",
    "CREATE TABLE `gen_metadata` (`idx` integer,`data` blob,`size` integer"
    " NOT NULL DEFAULT 0,PRIMARY KEY (`idx`))",
    "CREATE TABLE `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`))",
    "CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`))",
    "CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT \"main\","
    "`data` blob,PRIMARY KEY (`id`))",
    "CREATE TABLE `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`))",
]

# Fixture-only exact sizes (the real-agy sizes in the docs are observations and
# are deliberately not reproduced; see docs §2/§9).
GEN_SIZES = [555, 39031]
EXECUTOR_SIZE = 15603

# Strings that must never appear in fixture sources or generated artifacts.
DENYLIST = tuple(n.lower() for n in (b"/Users/", b"/home/", b"C:\\Users\\", b".gemini/",
                                     b"google_accounts", b"oauth"))   # compared against lowercased bytes


def _make_demo(tmp_path: Path, *, name: str | None = None,
               with_wal: bool = False, raw_steps: list[dict] | None = None):
    """Generate DEMO_TURNS under the docs' `<cascade_id>.db` file name."""
    cascade_id = MUT._uuid("agy-fixture-cascade")
    db = tmp_path / f"{name or cascade_id}.db"
    meta = MUT.make_db(str(db), DEMO_TURNS, with_wal=with_wal,
                       raw_steps=raw_steps)
    return db, meta


def _rows(db: str | Path) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        return con.execute("SELECT idx, step_type, status, has_subtrajectory, "
                           "step_format FROM steps ORDER BY idx").fetchall()
    finally:
        con.close()


def _dump(db: str | Path) -> dict[str, list[tuple]]:
    """Every logical row, so regeneration can be compared row by row."""
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        return {
            "trajectory_meta": con.execute(
                "SELECT * FROM trajectory_meta ORDER BY trajectory_id").fetchall(),
            "steps": con.execute("SELECT * FROM steps ORDER BY idx").fetchall(),
            "gen_metadata": con.execute(
                "SELECT * FROM gen_metadata ORDER BY idx").fetchall(),
            "executor_metadata": con.execute(
                "SELECT * FROM executor_metadata ORDER BY idx").fetchall(),
            "parent_references": con.execute(
                "SELECT * FROM parent_references ORDER BY idx").fetchall(),
            "trajectory_metadata_blob": con.execute(
                "SELECT * FROM trajectory_metadata_blob ORDER BY id").fetchall(),
            "battle_mode_infos": con.execute(
                "SELECT * FROM battle_mode_infos ORDER BY idx").fetchall(),
        }
    finally:
        con.close()


def _vacuum(src: Path, out: Path) -> str:
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(out),))
    finally:
        con.close()
    return hashlib.md5(out.read_bytes()).hexdigest()


def _private_hits(path: Path) -> list[str]:
    """Find denylisted bytes; report only name + offset, never the blob."""
    low = path.read_bytes().lower()
    hits = []
    for needle in DENYLIST:
        pos = low.find(needle)
        if pos >= 0:
            hits.append(f"{path.name}: {needle.decode()} at offset {pos}")
    return hits


def test_private_scan_catches_what_it_claims_to(tmp_path):
    """The denylist itself is checked: a mac path, a windows path and a token word must hit."""
    for text in (b"x /Users/bob y", b"C:\\Users\\bob", b"/USERS/bob", b"~/.gemini/antigravity-cli", b"OAuth"):
        f = tmp_path / "probe.bin"
        f.write_bytes(text)
        assert _private_hits(f), text
    clean = tmp_path / "clean.bin"
    clean.write_bytes(b"hello probe")
    assert _private_hits(clean) == []


def test_schema_and_pragmas_match_docs(tmp_path):
    db, meta = _make_demo(tmp_path)
    assert set(meta) == {"trajectory_id", "cascade_id", "exec_uuid"}
    # stray sidecars must not survive a clean close (agy's idle shape)
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()

    con = sqlite3.connect(str(db))  # normal connection: immutable reports delete
    try:
        sqls = [sql for (sql,) in
                con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")]
        page_size, = con.execute("PRAGMA page_size").fetchone()
        user_version, = con.execute("PRAGMA user_version").fetchone()
        journal_mode, = con.execute("PRAGMA journal_mode").fetchone()
        auto_vacuum, = con.execute("PRAGMA auto_vacuum").fetchone()
        encoding, = con.execute("PRAGMA encoding").fetchone()
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    assert sorted(sqls) == sorted(DOCS_SCHEMA)
    assert page_size == 32768
    assert user_version == 1
    assert journal_mode == "wal"
    assert auto_vacuum == 0
    assert encoding == "UTF-8"
    assert quick == "ok"
    # WAL mode is persisted in the header (2/2) even without sidecars
    assert Path(db).read_bytes()[18:20] == b"\x02\x02"
    assert _rows(db) == [
        (0, PROTO.STEP_USER, 3, 0, 0),
        (1, PROTO.STEP_MODEL, 3, 0, 0),
        (2, PROTO.STEP_USER, 3, 0, 0),
        (3, PROTO.STEP_MODEL, 3, 0, 0),
        (4, PROTO.STEP_TOOL, 3, 0, 0),
        (5, PROTO.STEP_MODEL, 3, 0, 0),
    ]


def test_filename_and_trajectory_ids_match_meta(tmp_path):
    db, meta = _make_demo(tmp_path)
    assert Path(db).stem == meta["cascade_id"]
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        row = con.execute("SELECT trajectory_id, cascade_id, trajectory_type, "
                          "source FROM trajectory_meta").fetchone()
    finally:
        con.close()
    assert row == (meta["trajectory_id"], meta["cascade_id"], 4, 17)


def test_turns_decode_roundtrip(tmp_path):
    db, meta = _make_demo(tmp_path)
    rows = PROTO.read_steps(str(db))
    by_type: dict[int, list] = {}
    for row in rows:
        by_type.setdefault(row.step_type, []).append(row)

    user, tool_step, first_model, model_call, after = (
        by_type[PROTO.STEP_USER][0], by_type[PROTO.STEP_TOOL][0],
        by_type[PROTO.STEP_MODEL][0], by_type[PROTO.STEP_MODEL][1],
        by_type[PROTO.STEP_MODEL][-1])
    assert PROTO.text_of(user.step_payload, *PROTO.PATH_USER_TEXT) == "hello probe"
    assert user.tool_name is None and user.tool_args is None
    # text-only model steps have no 20.7 part: accessors must not explode
    assert first_model.tool_name is None and first_model.tool_args is None
    assert after.tool_name is None and after.tool_args is None
    assert model_call.tool_name == "run_command"
    assert json.loads(model_call.tool_args) == {
        "CommandLine": "echo hi", "toolSummary": "Echo hi"}
    assert tool_step.tool_name == "run_command"
    assert json.loads(tool_step.tool_args) == {
        "CommandLine": "echo hi", "toolSummary": "Echo hi"}
    out = PROTO.text_of(tool_step.step_payload, *PROTO.PATH_TOOL_OUTPUT)
    assert "Command exited with code 0." in out and out.endswith("hi\r\n")
    # display tree: 140.2.3.10.3.1 carries the command (docs §3)
    assert PROTO.text_of(tool_step.step_payload, 140, 2, 3, 10, 3, 1) == "echo hi"
    assert PROTO.text_of(tool_step.step_payload, 140, 2, 3, 5) == "Ran"
    # 140.7 embeds the conversation uuid under /brain/
    url = PROTO.text_of(tool_step.step_payload, 140, 7)
    assert f"/brain/{meta['cascade_id']}/" in url and url.endswith("/output.txt")
    assert PROTO.text_of(after.step_payload, *PROTO.PATH_MODEL_TEXT) == "OK"
    # 5.4.7.2.1 is the same 264 B opaque blob as the model part 20.7.7.2.1
    call_blob = PROTO.first_at(tool_step.step_payload, 5, 4, 7, 2, 1)
    model_blob = PROTO.first_at(model_call.step_payload, 20, 7, 7, 2, 1)
    assert call_blob == model_blob and len(call_blob) == 264
    # ids written inside every step and identical across steps
    ids = {row.embedded_ids["cascade_id"] for row in rows}
    assert ids == {meta["cascade_id"]}
    assert {row.embedded_ids["trajectory_id"] for row in rows} == {
        meta["trajectory_id"]}
    # metadata == payload field 5 on every generated step
    for row in rows:
        assert row.metadata == PROTO.first_at(row.step_payload, 5)
        assert row.status == 3 and row.has_subtrajectory == 0
        assert row.step_format == 0
    # second user turn gains 19.13, the first one does not
    assert PROTO.at(by_type[PROTO.STEP_USER][1].step_payload, 19, 13)
    assert not PROTO.at(user.step_payload, 19, 13)


def test_context_ids_and_bookkeeping_sizes(tmp_path):
    db, meta = _make_demo(tmp_path)
    rows = PROTO.read_steps(str(db))
    # 5.12 is the conversation-scoped execution uuid on every step
    assert {PROTO.text_of(r.step_payload, 5, 12) for r in rows} == {
        meta["exec_uuid"]}
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        gen = con.execute(
            "SELECT idx, data, size FROM gen_metadata ORDER BY idx").fetchall()
        executor = con.execute(
            "SELECT idx, data FROM executor_metadata ORDER BY idx").fetchall()
        blob, = con.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    finally:
        con.close()
    # one bootstrap + one main row per turn, sizes pinned (fixture shape)
    assert [idx for idx, _, _ in gen] == [0, 1, 2, 3]
    assert [len(data) for _, data, _ in gen] == [
        GEN_SIZES[0], GEN_SIZES[1], GEN_SIZES[0], GEN_SIZES[1]]
    assert all(size == len(data) for _, data, size in gen)
    assert all(PROTO.text_of(data, 4) == meta["exec_uuid"] for _, data, _ in gen)
    # exactly one executor row per user turn, pinned fixture size
    assert [idx for idx, _ in executor] == [0, 1]
    assert all(len(data) == EXECUTOR_SIZE for _, data in executor)
    # trajectory_metadata_blob repeats both ids and the workspace URI
    assert PROTO.text_of(blob, 3) == meta["cascade_id"]
    assert PROTO.text_of(blob, 6) == meta["trajectory_id"]
    assert PROTO.text_of(blob, 7) == "file:///tmp/agy-fixture-ws"
    assert len(PROTO.first_at(blob, 15)) == 492


def test_idle_generation_is_reproducible(tmp_path):
    """C1: independently generated files are identical byte for byte (one transaction,
    so no header change counter churn), and so are their VACUUM INTO exports."""
    one, _ = _make_demo(tmp_path, name="one")
    two, _ = _make_demo(tmp_path, name="two")
    assert hashlib.md5(one.read_bytes()).hexdigest() == hashlib.md5(two.read_bytes()).hexdigest()
    assert _dump(one) == _dump(two)

    h1 = _vacuum(one, tmp_path / "v1.db")
    h2 = _vacuum(one, tmp_path / "v2.db")
    h3 = _vacuum(two, tmp_path / "v3.db")
    assert h1 == h2  # same source, exported twice
    assert h1 == h3  # two independently generated sources, same logical rows
    for src in (one, two):
        leftover = Path(str(src) + "-wal")
        assert not leftover.exists() or leftover.stat().st_size == 0


def test_live_writer_snapshot_replays_via_vacuum(tmp_path):
    live = tmp_path / "live.db"
    meta = MUT.make_db(str(live), DEMO_TURNS, with_wal=True)
    wal = Path(str(live) + "-wal")
    shm = Path(str(live) + "-shm")
    try:
        assert wal.exists() and wal.stat().st_size > 0
        assert shm.exists()

        # a db-only copy mid-flight is stale: either it cannot see the schema
        # or it misses rows - both are acceptable (W9)
        only = tmp_path / "db-only"
        only.mkdir()
        (only / "live.db").write_bytes(live.read_bytes())
        con = sqlite3.connect(f"file:{only / 'live.db'}?immutable=1", uri=True)
        try:
            try:
                got = con.execute("SELECT count(*) FROM steps").fetchone()[0]
            except sqlite3.OperationalError as exc:
                assert "no such table" in str(exc)
                got = 0
        finally:
            con.close()
        # never complete: the committed rows are all in the wal at this point
        assert 0 <= got < 6

        # interface §1.2②: copy .db + -wal only (no -shm), open normally,
        # VACUUM INTO, quick_check, then compare (count, max idx)
        snap = tmp_path / "snapshot"
        snap.mkdir()
        (snap / "live.db").write_bytes(live.read_bytes())
        (snap / "live.db-wal").write_bytes(wal.read_bytes())
        assert not (snap / "live.db-shm").exists()
        con = sqlite3.connect(str(snap / "live.db"))
        try:
            quick = con.execute("PRAGMA quick_check").fetchone()[0]
            con.execute("VACUUM INTO ?", (str(snap / "vacuumed.db"),))
            got = con.execute("SELECT count(*), max(idx) FROM steps").fetchone()
        finally:
            con.close()
        assert quick == "ok"
    finally:
        meta["connection"].close()

    # W9: closing may delete the wal instead of truncating it
    assert not wal.exists() or wal.stat().st_size == 0

    expected = _dump(live)
    assert _dump(snap / "vacuumed.db") == expected
    assert got == (len(expected["steps"]), expected["steps"][-1][0])
    replayed = PROTO.read_steps(str(snap / "vacuumed.db"))
    assert PROTO.text_of(replayed[0].step_payload,
                         *PROTO.PATH_USER_TEXT) == "hello probe"


def test_raw_steps_pass_through_unknown_shape(tmp_path):
    payload = MUT.enc_varint(1, 77) + MUT.enc_msg(5, MUT.enc_varint(3, 9))
    raw = {"idx": 6, "step_type": 77, "status": 9, "step_format": 2,
           "has_subtrajectory": 1, "payload": payload}
    db, _ = _make_demo(tmp_path, raw_steps=[raw])
    rows = PROTO.read_steps(str(db))
    assert len(rows) == 7
    unknown = rows[-1]
    assert (unknown.idx, unknown.step_type, unknown.status,
            unknown.step_format, unknown.has_subtrajectory) == (6, 77, 9, 2, 1)
    assert unknown.step_payload == payload
    # W2: an unknown shape must not explode in the accessors
    assert unknown.tool_name is None
    assert unknown.tool_args is None
    assert unknown.embedded_ids == {}


def test_artifacts_and_fixture_sources_have_no_private_paths(tmp_path):
    idle, _ = _make_demo(tmp_path, name="privacy-idle")
    live = tmp_path / "privacy-live.db"
    meta = MUT.make_db(str(live), DEMO_TURNS, with_wal=True)
    vacuumed = tmp_path / "privacy-vac.db"
    try:
        con = sqlite3.connect(str(idle))
        try:
            con.execute("VACUUM INTO ?", (str(vacuumed),))
        finally:
            con.close()
        artifacts = [idle, live, Path(str(live) + "-wal"),
                     Path(str(live) + "-shm"), vacuumed]
        for path in artifacts:
            assert path.exists(), path.name
            hits = _private_hits(path)
            assert not hits, hits
    finally:
        meta["connection"].close()

    sources = Path(__file__).resolve().parents[1] / "fixtures" / "agy"
    for path in sorted(sources.glob("*.py")):
        hits = _private_hits(path)
        assert not hits, hits
