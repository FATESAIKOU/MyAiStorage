#!/usr/bin/env python3
"""A fake agy CLI for unit tests (#26 PR2b) - never the real agy.

`agy --conversation <id>` appends one user + one model step (a plain text
turn) to `$AGORA_AGY_HOME/conversations/<id>.db`, which must already exist
(the restore put it there), then leaves the file again in the idle shape:
checkpointed, no `-wal`/`-shm` left behind, exactly the way agy behaves
between turns (docs/agy-storage.md §1.2). FAKE_AGENT_MODE=noop changes
nothing and exits. `--version` prints a version line. Every call appends its
argv + cwd to `$FAKE_HOME/agy-args.log` / `agy-cwd.log` so a test can prove
the launch really ran where agora sent it.

The step payloads are built with `tests/fixtures/agy/make_agy_db.py`
encoders - the same schema and field numbers, fake text, no real agy bytes.
"""

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

FIXTURE = (Path(__file__).resolve().parents[1] / "fixtures" / "agy" / "make_agy_db.py")
_spec = importlib.util.spec_from_file_location("make_agy_db", FIXTURE)
MUT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MUT)


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


args = sys.argv[1:]
fake_home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-home"))
fake_home.mkdir(parents=True, exist_ok=True)
with open(fake_home / "agy-args.log", "a") as f:
    f.write(json.dumps(args) + "\n")
with open(fake_home / "agy-cwd.log", "a") as f:
    f.write(os.getcwd() + "\n")

if args[:1] == ["--version"]:
    print(os.environ.get("FAKE_AGY_VERSION", "9.9.9 (Fake agy)"))
    sys.exit(0)

if "--conversation" in args:
    sid = args[args.index("--conversation") + 1]
    if os.environ.get("FAKE_AGENT_MODE", "append") == "noop":
        sys.exit(0)
    db = Path(os.environ.get("AGORA_AGY_HOME", "/nonexistent")) / "conversations" / f"{sid}.db"
    if not db.is_file():
        fail(f"no such conversation: {sid}")
    con = sqlite3.connect(db, isolation_level=None, timeout=30.0)
    try:
        meta = con.execute(
            "SELECT trajectory_id, cascade_id FROM trajectory_meta LIMIT 1").fetchone()
        if not meta or not meta[0] or not meta[1]:
            fail("conversation db has no trajectory_meta ids")
        trajectory_id, cascade_id = str(meta[0]), str(meta[1])
        exec_uuid = MUT._uuid(f"{cascade_id}-exec")
        t = con.execute("SELECT coalesce(max(idx), -1) + 1 FROM steps").fetchone()[0]
        event_hex = MUT._hex32(f"{cascade_id}-event-{t}")
        con.execute("BEGIN IMMEDIATE")
        ctx = MUT._context(4, exec_uuid, trajectory_id, cascade_id, event_hex, t,
                           history=(3,))
        con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (t, MUT.STEP_USER, MUT.STATUS_DONE, 0, ctx, None, None, None, None,
                     MUT._user_payload(MUT.STEP_USER, ctx, "ZZSAY 再補一句", t,
                                       later_turn=True), 0))
        ctx = MUT._context(2, exec_uuid, trajectory_id, cascade_id, event_hex, t + 1,
                           gen={"in": 1319, "out": 6860, "parts": 74, "aux": 24,
                                "bot": "bot-" + MUT._uuid(f"{cascade_id}-bot"),
                                "session_kv": "-3750763034362895579", "limit1": 34,
                                "limit2": 40, "id23": event_hex[:23], "ctx_tokens": 1319},
                           history=(8, 3), timestamps=True)
        payload = (MUT.enc_varint(1, MUT.STEP_MODEL) + MUT.enc_varint(4, MUT.STATUS_DONE)
                   + MUT.enc_msg(5, ctx)
                   + MUT.enc_msg(20, MUT._text_part(
                       "ZZSAY 好的", "bot-" + MUT._uuid(f"{cascade_id}-bot"), t + 1)))
        con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (t + 1, MUT.STEP_MODEL, MUT.STATUS_DONE, 0, ctx, None, None, None,
                     None, payload, 0))
        con.execute("COMMIT")
        # agy is WAL mode too: close like its own last connection does - a
        # checkpoint, then no sidecars on disk.
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            sidecar.unlink(missing_ok=True)
    print("ZZSAY 好的")
    sys.exit(0)

fail(f"fake agy does not support {args}")
