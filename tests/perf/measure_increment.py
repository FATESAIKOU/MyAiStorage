#!/usr/bin/env python3
"""Measure what one round sends once a long session is cut in segments (R3b-2, #170).

NOT a pytest test: run by hand from the worktree root::

    python tests/perf/measure_increment.py --size-mb 800 --rounds 4 --grow-mb 1 --sessions 20

What it does (all under one temp dir in /tmp, removed at the end; fake rclone, fake HOME):

1. writes a claude session of the requested size (the R1 shape: a few turns plus big
   `tool_result` lines, so the reading version stays small),
2. runs one write-back round on it in a fresh child process (round 0: the first one, already
   cut in C-sized segments), then appends `--grow-mb` of new exchanges and runs another round, N times.
   A round is `export_increment` + `stage(plan)` + `remember` + `upload_batch`, not `agora import`
   (the child flag is still called `--child-import`; the name is historical, see the end),
3. per round it records the bytes and files the fake Drive received (`copies.log`), the number
   of rclone calls (`calls.log`), the child's wall time and its `ru_maxrss`,
4. at the end it lists the Drive folder: how many files one long session leaves there, and,
   for `--sessions N`, what a recursive listing of N such sessions weighs (the manifest of one
   session copied N times under other ULIDs - the listing is what is measured, not the content).

The rounds do not use `agora import`: its update path compares the new export with the old raw
("never overwrite with less"), which reads the whole old raw into memory and would hide what the
write-back needs. The child does what `agora continue` saves do - the same `stage` + `upload_batch`
+ cursor - in one process that `ru_maxrss` can be read from. `--child-import` only names that child.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import measure_import_mem as M  # noqa: E402  (the same session shape and playground as R1)

MIB = 1 << 20


def _calls(root: Path) -> int:
    log = root / "calls.log"
    return len(log.read_text().splitlines()) if log.is_file() else 0


def _copies(root: Path) -> list[dict]:
    log = root / "copies.log"
    return [json.loads(x) for x in log.read_text().splitlines()] if log.is_file() else []


def _child(session_id: str, ulid_file: str) -> int:
    """One write-back round, the way `agora continue` saves: the cursor, `export_increment`,
    `stage` with the plan, then the upload. `import` is not used for the rounds: its
    "never overwrite with less" check reads the whole old raw, which the write-back does not."""
    import resource
    from agora import header as h, store
    from agora.agents import claude as C

    started = time.perf_counter()
    paths = store.Paths.from_env()
    ulid_path = Path(ulid_file)
    ulid = ulid_path.read_text().strip() if ulid_path.exists() else h.new_ulid()
    first = not ulid_path.exists()
    inc = C.ClaudeAgent().export_increment(session_id, None, store.read_cursor(paths, ulid))
    exp = inc.exported
    hdr = {"type": "Session", "title": "perf", "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-22T00:00:00Z", "updated_at": "2026-10-22T00:00:00Z",
                     "relation": "import", "parents": [],
                     "source": {"agent": "claude", "session_id": session_id, "created_at": exp.created_at}}}
    plan = {"append_from": inc.append_from, "seal_upto": inc.seal_upto, "cursor": inc.cursor, "cursor_ulid": ulid}
    folder = store.stage(paths, hdr, "## user\nperf\n", exp.raw_file, update=not first, plan=plan)
    store.remember(paths, folder)
    left = store.upload_batch(store.Drive(paths), paths)
    ulid_path.write_text(ulid)
    seconds = time.perf_counter() - started
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"rc": 1 if left else 0, "seconds": seconds, "maxrss": maxrss, "stdout": ""}), flush=True)
    return 1 if left else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mb", type=int, default=800)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--grow-mb", type=float, default=1.0)
    ap.add_argument("--sessions", type=int, default=20)
    ap.add_argument("--chunk-mib", type=int, default=4)
    ap.add_argument("--child-import", action="store_true")
    ap.add_argument("--session-id")
    ap.add_argument("--ulid-file")
    args = ap.parse_args()
    if args.child_import:
        return _child(args.session_id, args.ulid_file)

    root = Path(tempfile.mkdtemp(prefix="r3b2-inc-", dir="/tmp"))
    print(f"[probe] playground {root}", flush=True)
    try:
        env = M.make_env(root)
        env["AGORA_RAW_CHUNK"] = str(args.chunk_mib * MIB)
        workdir = root / "proj"
        workdir.mkdir()
        pdir = root / "home" / ".claude" / "projects" / "-r3b2-proj"
        pdir.mkdir(parents=True)
        sid = str(uuid.uuid4())
        path = pdir / f"{sid}.jsonl"
        t0 = time.perf_counter()
        M.write_session(path, pdir / sid, args.size_mb * MIB, 1 * MIB, str(workdir))
        print(f"[probe] generated {path.stat().st_size / MIB:.1f} MiB in {time.perf_counter() - t0:.1f}s", flush=True)
        rows = []
        for rnd in range(args.rounds + 1):
            if rnd:
                gen = M.Gen(sid, str(workdir), M._body(int(args.grow_mb * MIB) // 2))
                gen.seconds, gen.turn, gen.uuid_n = 10_000 * rnd, 10_000 * rnd, 10_000_000 * rnd
                with path.open("ab") as f:
                    f.write(gen.exchange())
            calls0, copies0 = _calls(root), len(_copies(root))
            t = time.perf_counter()
            child = subprocess.run([sys.executable, str(Path(__file__)), "--child-import", "--session-id", sid,
                                    "--ulid-file", str(root / "ulid.txt")],
                                   env=env, capture_output=True, text=True)
            wall = time.perf_counter() - t
            if child.returncode != 0:
                raise SystemExit(f"round {rnd} failed: {child.stderr[-2000:]}")
            rep = json.loads(child.stdout.strip().splitlines()[-1])
            sent = [f for c in _copies(root)[copies0:] for f in c["files"]]
            row = {"round": rnd, "source_mib": round(path.stat().st_size / MIB, 1),
                   "sent_files": len(sent), "sent_mib": round(sum(f["size"] or 0 for f in sent) / MIB, 2),
                   "rclone_calls": _calls(root) - calls0, "seconds": round(wall, 2),
                   "peak_rss_mib": round(M.rss_bytes(rep["maxrss"]) / MIB, 1)}
            print(f"[probe] {json.dumps(row)}", flush=True)
            rows.append(row)
        sessions = root / "remote" / "agora-test" / "sessions"
        folder = next(sessions.iterdir())
        names = sorted(p.name for p in folder.iterdir())
        listing = {"files_one_session": len(names)}
        # N sessions: the same file names under other ULIDs, empty bodies - only the listing is measured
        for k in range(args.sessions - 1):
            twin = sessions / f"01M5TWIN{k:018d}"[:26]
            twin.mkdir()
            for n in names:
                (twin / n).write_bytes(b"")
        os.environ.update(env)
        from agora import store
        t = time.perf_counter()
        remote = store._listing_with_md5(store.Drive(store.Paths.from_env()))
        listing.update(sessions=args.sessions, listing_seconds=round(time.perf_counter() - t, 2),
                       listing_entries=sum(len(v) for v in remote.values()),
                       listing_json_kib=round(len(json.dumps(remote)) / 1024, 1))
        print(f"[probe] listing {json.dumps(listing)}", flush=True)
        print(json.dumps({"rounds": rows, "listing": listing}, indent=1))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        print(f"[probe] removed {root}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
