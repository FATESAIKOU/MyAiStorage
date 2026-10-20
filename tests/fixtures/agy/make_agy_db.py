"""Programmatic generator of fake agy (Antigravity CLI) conversation databases.

Everything is derived from the probe notes in docs/agy-storage.md: same schema,
WAL journal mode, page size 32768, and the resolved protobuf field numbers for
user (14), model (15) and tool (132) steps. No byte is copied from a real agy
database: uuids are deterministic uuid5 values, timestamps are a fixed synthetic
epoch, and the large opaque blobs real agy fills with (encrypted/compressed)
internal context are sha256-expansion filler bytes.

Usage from tests (also runnable as a script for manual inspection):

    meta = make_db(path, turns=[...])              # idle: closed file, no sidecars
    meta = make_db(path, turns=[...], with_wal=True)  # live writer: close it yourself
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid

# ---------------------------------------------------------------- schema

SCHEMA = [
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

PAGE_SIZE = 32768
USER_VERSION = 1

# step_type / status values as observed in fake conversations.
STEP_USER = 14
STEP_MODEL = 15
STEP_TOOL = 132
STATUS_DONE = 3

# Synthetic base time so the tests never need a clock: every generated db stamps
# the same epoch and just walks forward a few seconds per step.
EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z
NANOS_STEP = 250_000_000

NAMESPACE = uuid.UUID("a3bb1890-adc0-4a55-a727-71cc4742ce4a")


def _uuid(seed: str) -> str:
    return str(uuid.uuid5(NAMESPACE, seed))


def _hex32(seed: str) -> str:
    return _uuid(seed).replace("-", "")


def _fill(seed: str, size: int) -> bytes:
    """Deterministic pseudo-random filler for blobs whose real content is opaque."""
    out = b""
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return out[:size]


# ---------------------------------------------------------------- encoder

def _varint(value: int) -> bytes:
    out = b""
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out += bytes([bits | 0x80])
        else:
            return out + bytes([bits])


def _tag(num: int, wt: int) -> bytes:
    return _varint((num << 3) | wt)


def enc_varint(num: int, value: int) -> bytes:
    return _tag(num, 0) + _varint(value)


def enc_bytes(num: int, value: bytes) -> bytes:
    return _tag(num, 2) + _varint(len(value)) + value


def enc_text(num: int, value: str) -> bytes:
    return enc_bytes(num, value.encode("utf-8"))


def enc_msg(num: int, body: bytes) -> bytes:
    return enc_bytes(num, body)


def _ts(num: int, seconds: int, nanos: int) -> bytes:
    return enc_msg(num, enc_varint(1, seconds) + enc_varint(2, nanos))


# ---------------------------------------------------------------- payloads

def _context(actor: int, exec_uuid: str, trajectory_id: str, cascade_id: str,
             event_hex: str, t: int, *, gen: dict | None = None,
             history: tuple[int, ...] = (3,), timestamps: bool = False) -> bytes:
    """Field 5 of step_payload: the per-step context envelope.

    Includes the id evidence the fixture relies on: 5.12 = conversation-scoped
    execution uuid, 5.20 = {1: trajectory_id, 4: cascade_id}.
    """
    body = enc_msg(1, enc_varint(1, EPOCH + t) + enc_varint(2, t * NANOS_STEP))
    body += enc_varint(3, actor)
    if timestamps:
        for i, num in enumerate((6, 7, 8)):
            body += _ts(num, EPOCH + t + i, t * NANOS_STEP)
    if gen:
        body += enc_msg(9, (
            enc_varint(1, gen["in"]) + enc_varint(2, gen["out"])
            + enc_varint(3, gen["parts"]) + enc_varint(6, gen["aux"])
            + enc_text(7, gen["bot"])
            + enc_msg(8, enc_text(1, "sessionID") + enc_text(2, gen["session_kv"]))
            + enc_varint(9, gen["limit1"]) + enc_varint(10, gen["limit2"])
            + enc_text(11, gen["id23"])))
        body += enc_varint(11, gen["ctx_tokens"])
    body += enc_text(12, exec_uuid)
    body += enc_msg(20, enc_text(1, trajectory_id) + enc_text(4, cascade_id))
    for i, code in enumerate(history):
        body += enc_msg(26, enc_varint(1, code)
                        + enc_msg(2, enc_varint(1, EPOCH + t)
                                  + enc_varint(2, (t + i) * NANOS_STEP)))
    if timestamps:
        body += _ts(32, EPOCH + t, t * NANOS_STEP)
    body += enc_text(35, event_hex)
    return body


def _user_payload(step_type: int, ctx: bytes, text: str, t: int, *,
                  later_turn: bool = False) -> bytes:
    payload = enc_varint(1, step_type) + enc_varint(4, STATUS_DONE) + enc_msg(5, ctx)
    nested = (enc_text(2, text)
              + enc_msg(3, enc_text(1, text))       # wrapped twin of the text, as observed
              + enc_msg(4, b"")                     # empty sub-message, as observed
              + enc_bytes(12, _fill(f"ctx{t}", 1329)))  # per-step generation context class
    if later_turn:
        nested += enc_bytes(13, _fill(f"ctx13-{t}", 1329))  # second and later user turns
    payload += enc_msg(19, nested)
    return payload


def _text_part(text: str, bot_id: str, t: int) -> bytes:
    return (enc_text(1, text) + enc_text(6, bot_id) + enc_text(8, text)
            + enc_varint(12, 2)
            + enc_msg(14, enc_msg(2, enc_bytes(1, _fill(f"opaque-text-{t}", 149)))))


def _toolcall_part(call_id: str, name: str, args_json: str, call_blob: bytes,
                   t: int) -> bytes:
    return (enc_text(6, "bot-" + _uuid(f"bot-call-{t}"))
            + enc_msg(7, (enc_text(1, call_id) + enc_text(2, name) + enc_text(3, args_json)
                          + call_blob))
            + enc_varint(12, 2))


def _tool_payload(ctx: bytes, call_id: str, name: str, args: dict,
                  command: str, output: str, cwd: str, cascade_id: str, t: int) -> bytes:
    """Field 140 of a tool (132) step: call echo + result echo."""
    crlf_out = output.replace("\n", "\r\n") + "\r\n"
    result = enc_msg(140, (
        enc_msg(1, enc_text(1, "toolSummary") + enc_text(2, args.get("toolSummary", name)))
        + enc_msg(1, enc_text(1, "CommandLine") + enc_text(2, command))
        + enc_msg(2, (
            enc_text(1, f"Command exited with code 0.\n\nCommand output:\n"
                        f"{output.replace(chr(10), chr(13) + chr(10))}{"\r\n" if not output.endswith("\n") else ""}")
            + enc_msg(3, (enc_text(5, "Ran") + enc_text(6, "terminal") + enc_text(7, "terminal")
                          + enc_msg(9, enc_text(1, command))
                          + enc_msg(10, enc_msg(3, (
                              enc_text(1, command) + enc_text(2, cwd)
                              + enc_varint(3, 0) + enc_text(4, crlf_out))))))
            + enc_msg(6, (enc_text(1, "type.googleapis.com/exa.cortex.core.tools.RunCommand")
                          + enc_msg(2, (enc_text(5, "zsh") + enc_text(6, command)
                                        + enc_text(7, cwd)))))))
        + enc_text(7, f"file:///tmp/brain/{cascade_id}"
                      f"/.system_generated/steps/{t}/output.txt")))
    return (enc_varint(1, STEP_TOOL) + enc_varint(4, STATUS_DONE) + enc_msg(5, ctx)
            + result)


def _render_info(command: str, cwd: str, output: str, label: str = "Ran") -> bytes:
    """steps.render_info for tool steps, as observed (small display summary)."""
    crlf_out = output.replace("\n", "\r\n") + "\r\n"
    return (enc_text(5, label) + enc_text(6, "terminal")
            + enc_msg(9, enc_text(1, command))
            + enc_msg(10, (enc_text(1, command) + enc_text(2, cwd)
                           + enc_varint(3, 0) + enc_text(4, crlf_out))))


# ---------------------------------------------------------------- tables

def _gen_metadata_rows(exec_uuid: str, turn_idx: int) -> list[tuple[int, bytes, int]]:
    """Synthetic gen_metadata: a small bootstrap row plus one large context row.

    Real agy writes 1-2 rows per generation with an identical shape family; the
    classes (uuid + big opaque block + repeated small config messages + a 492B
    blob) are what the fixture reproduces, with fake content throughout.
    """
    bootstrap = (enc_msg(1, (enc_varint(3, 1319) + enc_msg(4, b"")
                             + enc_varint(11, 8) + enc_varint(12, 72)
                             + enc_varint(17, 17) + enc_varint(19, 19)
                             + enc_varint(20, 20)))
                 + enc_varint(2, 2) + enc_text(4, exec_uuid)
                 + enc_bytes(8, _fill("g8", 492)))
    config_msgs = (
        [enc_msg(2, enc_bytes(2, _fill(f"gm2-{i}", 180 + 40 * (i % 4)))) for i in range(4)]
        + [enc_msg(8, enc_bytes(1, _fill(f"gm8-{i}", 400 + 120 * (i % 6)))) for i in range(12)]
        + [enc_msg(16, enc_bytes(1, _fill(f"gm16-{i}", 270 + 300 * (i % 5)))) for i in range(10)])
    big = (enc_msg(1, (enc_bytes(1, _fill(f"gm-big1-{turn_idx}", 15280))
                       + b"".join(config_msgs)
                       + enc_varint(3, 1319)
                       + enc_msg(4, enc_msg(1, b""))
                       + enc_msg(9, enc_bytes(1, _fill("gm9", 565)))))
           + enc_msg(3, enc_bytes(21, _fill("gm3", 4400)))
           + enc_text(4, exec_uuid)
           + enc_bytes(8, _fill("g8", 492)))
    return [(0, bootstrap, len(bootstrap)), (1, big, len(big))]


def _executor_row(turn_idx: int) -> bytes:
    return (enc_varint(1, 4) + enc_varint(2, turn_idx % 3 + 1)
            + enc_varint(3, turn_idx % 3 + 1)
            + enc_text(9, _uuid(f"exec{turn_idx}"))
            + enc_bytes(10, _fill(f"exec10-{turn_idx}", 7300))
            + enc_bytes(11, _fill("g8", 492))
            + enc_bytes(16, _fill(f"exec16-{turn_idx}", 14))
            + enc_varint(17, 1329)
            + enc_bytes(18, _fill(f"exec18-{turn_idx}", 7736)))


def _trajectory_blob(trajectory_id: str, cascade_id: str, workspace: str) -> bytes:
    return (enc_msg(1, enc_msg(1, enc_text(1, "default-cli-project")))
            + enc_msg(2, enc_bytes(1, _fill("tb2", 8)))
            + enc_text(3, cascade_id) + enc_text(6, trajectory_id)
            + enc_text(7, f"file://{workspace}")
            + enc_bytes(15, _fill("g8", 492))
            + enc_text(18, "default-cli-project"))


# ---------------------------------------------------------------- top level

def make_db(path: str, turns: list[dict], *, trajectory_id: str | None = None,
            cascade_id: str | None = None, exec_uuid: str | None = None,
            workspace: str = "/tmp/agy-fixture-ws",
            with_wal: bool = False, seed: str = "agy-fixture",
            raw_steps: list[dict] | None = None) -> dict:
    """Create an agy-shaped conversation database at *path*.

    *turns* is a list of dicts, each describing one user turn; the contents are
    fake text ("hello probe"-style is customary). Keys per turn:

      user: user prompt text (required)
      model: model reply text; required for a text-only turn
      tool: optional dict(name, args, command, output, cwd) - when present the
            turn gains a model tool-call step and a tool (step_type 132) step
            that carries the call echo and the result echo.

    *raw_steps* appends arbitrary step rows after the generated turns, e.g.
    [{"idx": 6, "step_type": 77, "status": 9, "step_format": 2,
      "has_subtrajectory": 1, "payload": b"..."}] - the hook PR1 uses to test
    unknown step types/statuses/formats passing through unchanged (optional
    "metadata" and "render_info" keys are accepted too).

    Callers should name the file `<cascade_id>.db` (docs/agy-storage.md §1);
    the generator does not enforce that convention, the unit test does.

    Returns an id dict {"trajectory_id", "cascade_id", "exec_uuid"} (plus the
    still-open connection under key "connection" in with_wal mode). Schema and
    rows are written in one explicit transaction. Without with_wal the returned
    file mirrors an idle agy database: the generator checkpoints and DELETES
    `db-wal`/`db-shm` before returning, and raises if a non-empty -wal is left
    behind after close. With with_wal the connection is intentionally left open
    on committed-but-uncheckpointed rows - a mid-turn / live-writer shape, not
    the idle between-turns shape (agy checkpoints and drops the sidecars within
    a second of a turn ending). The caller copies `.db` + `-wal` only (SQLite
    rebuilds `-shm` on open) and MUST eventually close the connection.
    """
    trajectory_id = trajectory_id or _uuid(f"{seed}-trajectory")
    cascade_id = cascade_id or _uuid(f"{seed}-cascade")
    exec_uuid = exec_uuid or _uuid(f"{seed}-exec")
    event_hex = _hex32(f"{seed}-event")
    gen = {"in": 1319, "out": 6860, "parts": 74, "aux": 24,
           "bot": "bot-" + _uuid(f"{seed}-bot"),
           "session_kv": "-3750763034362895579",
           "limit1": 34, "limit2": 40, "id23": event_hex[:23], "ctx_tokens": 1319}

    for stale in (path, path + "-wal", path + "-shm"):
        if os.path.exists(stale):
            os.remove(stale)
    # Autocommit mode so BEGIN/COMMIT below are the only write transactions:
    # a variable number of implicit commits is what made the SQLite header
    # change counter (and thus whole-file md5) differ between runs.
    con = sqlite3.connect(path, isolation_level=None)
    con.execute(f"PRAGMA page_size = {PAGE_SIZE}").fetchone()
    con.execute(f"PRAGMA user_version = {USER_VERSION}").fetchone()
    con.execute("PRAGMA journal_mode = WAL").fetchone()
    con.execute("BEGIN")
    for sql in SCHEMA:
        con.execute(sql)
    con.execute("INSERT INTO trajectory_meta VALUES (?,?,?,?)",
                (trajectory_id, cascade_id, 4, 17))

    ctx_common = dict(exec_uuid=exec_uuid, trajectory_id=trajectory_id,
                      cascade_id=cascade_id, event_hex=event_hex)
    t = 0
    for turn_idx, turn in enumerate(turns):
        # 1) user step
        ctx = _context(4, gen=None, t=t, history=(3,), **ctx_common)
        con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (t, STEP_USER, STATUS_DONE, 0, ctx, None, None, None, None,
                     _user_payload(STEP_USER, ctx, turn["user"], t,
                                   later_turn=turn_idx > 0), 0))
        t += 1
        # 2) model step: final text, or a tool call
        tool = turn.get("tool")
        ctx_assist = _context(2, gen=gen, t=t, history=(8, 3), timestamps=True,
                              **ctx_common)
        if tool:
            name = tool.get("name", "run_command")
            call_id = tool.get("call_id", f"call_{event_hex[:5]}{t:02d}")
            args = tool.get("args") or {}
            call_blob = enc_msg(7, enc_msg(2, enc_bytes(1, _fill(f"opaque-call-{t}", 264))))
            con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (t, STEP_MODEL, STATUS_DONE, 0, ctx_assist, None, None,
                         None, None,
                         enc_varint(1, STEP_MODEL) + enc_varint(4, STATUS_DONE)
                         + enc_msg(5, ctx_assist)
                         + enc_msg(20, _toolcall_part(call_id, name,
                                                      json.dumps(args), call_blob, t)), 0))
            t += 1
            # 3) tool step echoing the call alongside its result
            ctx_tool = _context(2, gen=None, t=t, history=(1, 2, 3), timestamps=True,
                                **ctx_common)
            ctx_tool += enc_msg(4, (enc_text(1, call_id) + enc_text(2, name)
                                    + enc_text(3, json.dumps(args)) + call_blob))
            ctx_tool += enc_msg(34, enc_text(4, "allow")) + enc_varint(38, 1)
            command = tool.get("command", "echo hi")
            output = tool.get("output", "hi")
            cwd = tool.get("cwd", workspace)
            con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (t, STEP_TOOL, STATUS_DONE, 0, ctx_tool, None, None, None,
                         None,
                         _tool_payload(ctx_tool, call_id, name, args, command,
                                       output, cwd, cascade_id, t), 0))
            con.execute("UPDATE steps SET render_info = ? WHERE idx = ?",
                        (_render_info(command, cwd, output), t))
            t += 1
            if turn.get("model"):
                ctx_after = _context(2, gen=gen, t=t, history=(8, 3), timestamps=True,
                                     **ctx_common)
                con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (t, STEP_MODEL, STATUS_DONE, 0, ctx_after, None, None,
                             None, None,
                             enc_varint(1, STEP_MODEL) + enc_varint(4, STATUS_DONE)
                             + enc_msg(5, ctx_after)
                             + enc_msg(20, _text_part(turn["model"], gen["bot"], t)),
                             0))
                t += 1
        elif turn.get("model"):
            con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (t, STEP_MODEL, STATUS_DONE, 0, ctx_assist, None, None,
                         None, None,
                         enc_varint(1, STEP_MODEL) + enc_varint(4, STATUS_DONE)
                         + enc_msg(5, ctx_assist)
                         + enc_msg(20, _text_part(turn["model"], gen["bot"], t)), 0))
            t += 1
        # 4) per-turn bookkeeping rows, shape as observed
        for gidx, (idx, blob, size) in enumerate(_gen_metadata_rows(exec_uuid, turn_idx)):
            con.execute("INSERT INTO gen_metadata VALUES (?,?,?)",
                        (turn_idx * 2 + gidx, blob, size))
        con.execute("INSERT INTO executor_metadata VALUES (?,?)",
                    (turn_idx, _executor_row(turn_idx)))

    for raw in raw_steps or ():
        con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (raw["idx"], raw["step_type"], raw["status"],
                     raw["has_subtrajectory"], raw.get("metadata"), None, None,
                     None, raw.get("render_info"), raw["payload"],
                     raw["step_format"]))

    con.execute("INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
                (_trajectory_blob(trajectory_id, cascade_id, workspace),))
    con.execute("COMMIT")

    ids = {"trajectory_id": trajectory_id, "cascade_id": cascade_id,
           "exec_uuid": exec_uuid}
    if with_wal:
        # Intentionally leave the connection open: committed rows still live in
        # `db-wal` - the mid-turn / live-writer shape, NOT agy's idle shape.
        return {"connection": con, **ids}
    # Checkpoint explicitly, then close; a non-empty wal after close means the
    # returned file would silently be incomplete for an idle reader.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    con.close()
    wal = path + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        raise RuntimeError(f"non-empty -wal left after close: {wal}")
    # Mirror agy's own cleanup: a closed agy database has no leftover sidecars
    # (Python leaves a checkpointed 0-byte -wal and its -shm behind; deleting
    # both at closed state is safe and reproduces the "no -wal" idle shape agy
    # has, while the main header keeps the WAL-mode bit).
    for sidecar in (wal, path + "-shm"):
        try:
            os.remove(sidecar)
        except FileNotFoundError:
            pass
    return ids


if __name__ == "__main__":  # manual smoke check: python make_agy_db.py /tmp/x.db
    import sys
    demo = [{"user": "hello probe", "model": "hello probe"},
            {"user": "run `echo hi` and report its output", "model": "OK",
             "tool": {"name": "run_command", "args": {"CommandLine": "echo hi"},
                      "command": "echo hi", "output": "hi"}}]
    print(make_db(sys.argv[1] if len(sys.argv) > 1 else "/tmp/agy-fx.db", demo))
