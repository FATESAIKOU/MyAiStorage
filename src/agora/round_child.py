"""One periodic write-back round, in a throwaway interpreter.

`agora continue` runs for a day at a time, and a round reads, parses and stages
the whole agent session - which peaks at several times the session file and, in
a long-lived process, leaves the allocator holding that peak for the rest of
the run. The round therefore runs HERE, in a child process that exits when the
round is done: whatever it allocated is memory the OS takes back. The outer
shell (`cli._round_child` calls us, `cli._periodic_write` owns the outcome)
keeps only the small state diff we send back on stdout.

Why a new interpreter and not `os.fork()`: on macOS, once the parent has used
`sqlite3` - which `cmd_continue` unavoidably does before the first round, and
the write-back keeps doing every round (`store.Index`) - a forked child that
touches `sqlite3` dies: SIGSEGV at `connect()` in almost every run, or a hang,
observed nondeterministically on two different interpreters. A fresh process
has no such inherited state. The probe that shows it lives outside the repo
(team `experiments/subproc/probe_fork_sqlite.py`, 2026-10-19).

Protocol: the outer shell writes the run's small state as one JSON document on
stdin; we rebuild a `_Run`, do exactly what the in-process round did
(`_collect_new` + `_write_one`, `check_drive=False`), and answer with one JSON
payload on fd 1. Everything the round PRINTS goes to continue.log, stamped and
tagged - the terminal belongs to the agent (design「不弄亂 agent 的畫面」). An
exception inside the round is the payload's `err` field, not our exit code;
only a death that prevents any answer leaves a nonzero exit, and the shell
counts that round failed and looks again next round.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

from agora import cli, store


def state_of(run) -> dict:
    """The outer shell's `_Run`, minus everything big. `record` crosses whole
    because the round only reads it (nothing on the round path writes it)."""
    return {"record": run.record,
            "last_md5": run.last_md5,
            "fingerprint": run.fingerprint,
            "collect_failed": run.collect_failed,
            "target": run.target,
            "wrote": run.wrote,
            "first_note": run.first_note,
            "pending": str(run.pending) if run.pending else None}


def decode_fingerprint(fp):
    """JSON turns every tuple into a list; `_collect_new` compares a stat MARK
    against `run.fingerprint` with `==`, and a tuple never equals a list -
    restore it or the skip check silently re-reads every session every round."""
    if isinstance(fp, list):
        return tuple(decode_fingerprint(x) for x in fp)
    return fp


def decode_run(state: dict, paths: store.Paths) -> cli._Run:
    pending = Path(state["pending"]) if state["pending"] else None
    run = cli._Run(paths, state["record"], pending=pending, last_md5=state["last_md5"])
    run.fingerprint = decode_fingerprint(state["fingerprint"])
    run.collect_failed = state["collect_failed"]
    run.target = state["target"]
    run.wrote = state["wrote"]
    run.first_note = state["first_note"]
    return run


def run_round(run, agent) -> dict:
    """The round body, on a run we own: collect, write, and report the fields
    plus every mutation - the same six outcomes the in-process round produced,
    decided by the shell from this payload."""
    err = None
    saved, target = None, run.target
    try:
        exported = cli._collect_new(run, agent)
        saved, target, _note = cli._write_one(run.paths, run, exported, agent,
                                              check_drive=False)
    except Exception as e:                            # what the shell caught before
        err = str(e) or type(e).__name__              # (it counted, never raised on)
        # SystemExit/KeyboardInterrupt are NOT caught (review #3): they take the
        # child down, and the shell reads that dead process as a failed round -
        # the same range the in-process round has always used.
    return {"err": err,
            "saved": list(saved) if saved else None,
            "target": target,
            "last_md5": run.last_md5,
            "fingerprint": run.fingerprint,
            "collect_failed": run.collect_failed,
            "wrote": run.wrote,
            "pending": str(run.pending) if run.pending else None,
            "first_note": run.first_note}


def main() -> int:
    state = json.loads(sys.stdin.read())
    paths = store.Paths.from_env()
    run = decode_run(state, paths)
    writer = cli._LogWriter(paths.state / cli.CONTINUE_LOG, run.target)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            run.logger = writer
            payload = run_round(run, cli.load_agent(state["record"]["agent"]))
    finally:
        writer.close()
    os.write(1, json.dumps(payload).encode())        # fd 1: the pipe, not the writer
    return 0


if __name__ == "__main__":
    sys.exit(main())
