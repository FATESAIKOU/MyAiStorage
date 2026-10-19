#!/usr/bin/env python3
"""Shell RSS after `agora continue` has STARTED (ticket impl-mem-start, phase 1).

NOT a pytest test. The second slice made each periodic round run in a child that
exits, but the shell itself still sits at about 1.3x the session right after the
launch: this probe measures exactly that. It runs the REAL command - `python -m
agora.cli continue session <id> --agent claude --headless` with the scripted fake
claude - on a 100 / 150 MB fake session, and takes the shell's resident memory
with `top -l 1 -pid <pid> -stats pid,mem` (the ticket's number; `ps -o rss` reads
wrong in this sandbox) at two points:

  * 30 s after the launch finished (the fixed `agora-agent-session` line), with
    the first periodic round deliberately still ahead (`--sync-period 1`, 60 s);
  * right after the first periodic round's line lands in continue.log.

The fake agent then waits for a release file so the shell can be measured for as
long as needed without the agent leaving first. Everything is a fresh temp
playground (HOME / AGORA_* / FAKE_REMOTE under it, fake rclone, AGORA_FOLDER_NAME
set by the script); the user's real config, sessions and cache are never read.
Disk care: one run at a time, fake file <= 150 MB, whole playground removed at
the end.

Usage (worktree root)::

    uv run python tests/perf/measure_continue_start_rss.py --size-mb 100 \
        --out /tmp/start-new-100.json
    # HEAD comparison: run the SAME script from the git-archive copy, with
    # PYTHONPATH=<head>/src so the launched `-m agora.cli` is the HEAD one.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid as uuid_mod
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "perf"))

FAKE_RCLONE = REPO / "tests" / "fakes" / "fake_rclone.py"
FAKE_CLAUDE = REPO / "tests" / "fakes" / "fake_claude.py"

_UNITS = {"K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}


def ps_rss_kb(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def top_mem_mb(pid: int) -> dict:
    """The shell's `top -l 1 -pid <pid>` numbers, in MB.

    The only kind of number the ticket accepts: `ps -o rss` is wrong inside
    this sandbox (a 100 MB session reported 11 MB). MEM is the resident size
    the ticket asks for, RSIZE the same column, and CMPRS says how much of the
    process macOS has compressed - under memory pressure a freed transient can
    sit in the compressor, which is worth seeing next to MEM. `top` prints e.g.
    `231M` / `1552K` and may append `+`/`-` (the direction the value moved);
    neither belongs to the number.
    """
    out = subprocess.run(["top", "-l", "1", "-pid", str(pid),
                          "-stats", "pid,mem,rsize,compressed"],
                         capture_output=True, text=True).stdout
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = re.match(rf"^{pid}\s+([0-9.]+)([KMGT])\s+([0-9.]+)([KMGT])\s+([0-9.]+)([KMGT])", ln)
        if m:
            return {"mem_mb": round(float(m.group(1)) * _UNITS[m.group(2)], 1),
                    "rsize_mb": round(float(m.group(3)) * _UNITS[m.group(4)], 1),
                    "cmprs_mb": round(float(m.group(5)) * _UNITS[m.group(6)], 1),
                    "ps_kb": ps_rss_kb(pid)}
    raise RuntimeError(f"top gave no MEM row for pid {pid}: {out!r}")


def make_playground(root: Path) -> dict:
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (root / "remote").mkdir()
    rclone = root / "rclone"
    # The fake copies the whole remote aside after every successful call (its
    # test-plan 0.3b snapshot). At 100-150 MB that is most of the run's I/O and
    # disk; this probe does not read those snapshots, so drop them after each call.
    rclone.write_text(f"#!/bin/sh\n{sys.executable} {FAKE_RCLONE} \"$@\"\n"
                      f"rc=$?\nrm -rf {root / 'snapshots'}\nexit $rc\n")
    rclone.chmod(0o755)
    claude = root / "claude"
    claude.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} \"$@\"\n")
    claude.chmod(0o755)
    return {**os.environ,
            "HOME": str(home), "AGORA_CLAUDE_HOME": str(home),
            "AGORA_CONFIG": str(root / "config"), "AGORA_CACHE_DIR": str(root / "cache"),
            "AGORA_STATE_DIR": str(root / "state"), "FAKE_REMOTE": str(root / "remote"),
            "AGORA_RCLONE": str(rclone), "AGORA_CLAUDE_CMD": str(claude),
            "AGORA_FOLDER_NAME": "agora-test", "AGORA_UPLOAD": "inline",
            "TMPDIR": str(root)}


def build_session(env: dict, root: Path, size_mb: int) -> tuple[str, str, Path]:
    """Generate a real-shape fake claude session and IMPORT IT as the target, so
    the launch reads back the very ~size_mb session (not a seed)."""
    import measure_continue_mem as M
    os.environ.update({k: v for k, v in env.items()
                       if k.startswith(("AGORA_", "FAKE_", "HOME", "TMPDIR", "PYTHONPATH"))})
    from agora import cli
    from agora.agents.claude import encode_project_dir, projects_dir
    workdir = root / "proj"
    workdir.mkdir()
    pdir = projects_dir() / encode_project_dir(workdir)
    pdir.mkdir(parents=True)
    big_id = str(uuid_mod.uuid4())
    M.write_session(pdir / f"{big_id}.jsonl", pdir / big_id, size_mb << 20,
                    reserve_bytes=256_000, workdir=str(workdir))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["import", "session", "--external-session-id", big_id,
                       "--agent", "claude"])
    if rc != 0:
        sys.exit(f"import failed rc={rc}: {out.getvalue()}")
    target = out.getvalue().strip().splitlines()[-1]
    return target, big_id, workdir


def wait_until(condition, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    if sys.platform != "darwin":
        print("measure_continue_start_rss.py 是 macOS 專用的量測腳本（它用 macOS 的 `top -l 1 -pid` 讀 RSS）；"
              f"這台是 {sys.platform}，請在 macOS 上跑。", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="shell RSS after continue has started")
    ap.add_argument("--size-mb", type=int, default=100)
    ap.add_argument("--startup-settle", type=float, default=30.0,
                    help="seconds after the launch lines to take the first reading")
    ap.add_argument("--sync-period", type=float, default=1.0,
                    help="minutes between rounds; keep it above --startup-settle")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    assert args.sync_period * 60 > args.startup_settle + 5, \
        "the first round must still be ahead of the first reading"

    root = Path(tempfile.mkdtemp(prefix="agora-start-rss-"))
    result: dict = {"size_mb": args.size_mb, "playground": str(root),
                    "python": sys.version.split()[0]}
    print(f"[start] playground {root}", flush=True)
    try:
        env = make_playground(root)
        target, big_id, workdir = build_session(env, root, args.size_mb)
        log_path = Path(env["AGORA_STATE_DIR"]) / "continue.log"
        err_path = root / "shell.err"
        release = root / "release-agent"
        env.update({"FAKE_AGENT_SCRIPT": f"append:ZZSTART,wait-sync,wait-file:{release}",
                    "FAKE_SYNC_TIMEOUT": "600", "FAKE_SYNC_SIGNAL": str(log_path)})

        t0 = time.monotonic()
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "agora.cli", "continue", "session", target,
                 "--agent", "claude", "--dir", str(workdir), "--headless",
                 "--sync-period", str(args.sync_period)],
                stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL,
                env=env, cwd=str(root))

            def started() -> bool:
                try:
                    text = err_path.read_text(errors="replace")
                except OSError:
                    return False
                return "agora-agent-session" in text

            if not wait_until(started, 300, "launch lines"):
                proc.kill()
                sys.exit("shell never printed the launch lines")
            startup_s = round(time.monotonic() - t0, 1)
            print(f"[start] launched in {startup_s}s pid={proc.pid}", flush=True)

            time.sleep(args.startup_settle)
            before_round = {"at_s": round(time.monotonic() - t0, 1),
                            **top_mem_mb(proc.pid)}
            print(f"[start] after settle: {json.dumps(before_round)}", flush=True)
            series = [before_round]
            # Is a high settle reading a steady resident set or a freed startup
            # transient the allocator has not handed back yet? Five more readings
            # before the first round tell decay from resident.
            for _ in range(5):
                time.sleep(5.0)
                series.append({"at_s": round(time.monotonic() - t0, 1),
                               **top_mem_mb(proc.pid)})
            print(f"[start] settle series: {json.dumps([(r['at_s'], r['mem_mb'], r['cmprs_mb'], r['ps_kb']) for r in series])}",
                  flush=True)

            def first_round() -> bool:
                try:
                    return any("定期寫回" in ln
                               for ln in log_path.read_text(encoding="utf-8").splitlines())
                except OSError:
                    return False

            if not wait_until(first_round, 600, "the first periodic round"):
                proc.kill()
                sys.exit("the first periodic round never landed")
            time.sleep(2.0)
            after_round = {"at_s": round(time.monotonic() - t0, 1),
                           **top_mem_mb(proc.pid)}
            print(f"[start] after first round: {json.dumps(after_round)}", flush=True)

            release.write_text("go")
            proc.wait(timeout=300)
            code = proc.returncode
        rounds = [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
                  if "定期寫回" in ln]
        result.update({"target": target, "big_session_id": big_id,
                       "startup_seconds": startup_s,
                       "after_settle": before_round, "settle_series": series,
                       "after_first_round": after_round,
                       "shell_exit": code, "round_lines": rounds})
        print(f"[start] shell_exit={code} settle={before_round['mem_mb']}MB "
              f"after_round={after_round['mem_mb']}MB "
              f"(ps: {before_round['ps_kb']}/{after_round['ps_kb']} KB)", flush=True)
    finally:
        if args.keep:
            print(f"[start] playground kept at {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
            print("[start] playground removed", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"[start] results -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
