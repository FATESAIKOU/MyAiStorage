#!/usr/bin/env python3
"""End-to-end shell-RSS probe for `agora continue` (ticket impl-mem-subproc, phase 2).

NOT a pytest test. Runs the REAL command - `python -m agora.cli continue --headless`
in its own process, driven by the scripted fake claude (the same FAKE_AGENT_SCRIPT
mechanism the unit tests use) - and samples, once a second, the RSS of the agora
process and of every `agora.round_child` it spawns. Rounds are detected by the
lines continue.log leaves (one per round); each round boundary records the shell's
resident RSS then and its peak since the last boundary. This is the acceptance
number the ticket asked for: the shell's per-round RSS over >= 10 real rounds, on
a session that a real continue would grow.

Everything lives in a fresh temp playground (HOME / AGORA_* / FAKE_REMOTE under
it; fake rclone; AGORA_FOLDER_NAME and the pinned test env set by this script,
never exported into the caller's shell). The user's real config, sessions and
cache are never read. Disk care: the playground is removed at the end; one run
at a time; the fake session stays far under 300 MB.

Usage (worktree root)::

    uv run python tests/perf/measure_continue_shell_rss_e2e.py --size-mb 50 \
        --beats 12 --out /tmp/e2e-new.json
    # HEAD comparison: run the SAME script from the git-archive copy.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid as uuid_mod
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "perf"))

FAKE_RCLONE = REPO / "tests" / "fakes" / "fake_rclone.py"
FAKE_CLAUDE = REPO / "tests" / "fakes" / "fake_claude.py"


def rss_kb(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def make_playground(root: Path) -> dict:
    """Same isolation the unit harness uses: every agora dir and HOME point into
    `root`, the fake binaries are wrapper scripts, Drive is the fake remote."""
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (root / "remote").mkdir()
    rclone = root / "rclone"
    rclone.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    rclone.chmod(0o755)
    claude = root / "claude"
    claude.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} \"$@\"\n")
    claude.chmod(0o755)
    env = {**os.environ,
           "HOME": str(home), "AGORA_CLAUDE_HOME": str(home),
           "AGORA_CONFIG": str(root / "config"), "AGORA_CACHE_DIR": str(root / "cache"),
           "AGORA_STATE_DIR": str(root / "state"), "FAKE_REMOTE": str(root / "remote"),
           "AGORA_RCLONE": str(rclone), "AGORA_CLAUDE_CMD": str(claude),
           "AGORA_FOLDER_NAME": "agora-test", "AGORA_UPLOAD": "inline",
           "TMPDIR": str(root)}
    return env


def build_session(env: dict, root: Path, size_mb: int) -> tuple[str, str, Path]:
    """Generate a real-shape fake claude session and IMPORT IT as the continue
    target: the round then collects back the very ~size_mb session the fake
    agent keeps appending to (a seed-based target would make the rounds small -
    the first version of this harness measured a flat shell for exactly that
    bug)."""
    import measure_continue_mem as M
    os.environ.update({k: v for k, v in env.items() if k.startswith(("AGORA_", "FAKE_", "HOME", "TMPDIR"))})
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


class Sampler(threading.Thread):
    """Polls the shell + live round children once a second; keeps running maxima
    per round boundary (a list of (round, who, kb) marks)."""

    def __init__(self, shell_pid: int):
        super().__init__(daemon=True)
        self.shell_pid = shell_pid
        self._stop = threading.Event()
        self.lock = threading.Lock()
        self.marks: list[tuple[int, str, int]] = []
        self.round = 0
        self.children: set[int] = set()

    def watch_child(self, pid: int) -> None:
        with self.lock:
            self.children.add(pid)

    def boundary(self, round_no: int) -> None:
        with self.lock:
            self.round = round_no
            self.children.clear()

    def run(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                rnd = self.round
                kids = set(self.children)
            self.marks.append((rnd, "shell", rss_kb(self.shell_pid)))
            for pid in kids:
                kb = rss_kb(pid)
                if kb:
                    self.marks.append((rnd, "child", kb))
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()

    def peak(self, round_no: int, kind: str) -> int:
        with self.lock:
            hits = [kb for r, k, kb in self.marks
                    if k == kind and (r == round_no or (round_no == 1 and r == 0))]
            # round 0 = the marks before the first boundary was noticed: the
            # first round's own work (the shell only gets seen when it logs).
        return max(hits) if hits else 0


def discover_children(shell_pid: int, sampler: Sampler) -> None:
    out = subprocess.run(["ps", "-o", "pid=,ppid=,command=", "-ax"],
                         capture_output=True, text=True).stdout
    for ln in out.splitlines():
        parts = ln.strip().split(None, 2)
        if len(parts) == 3 and parts[1] == str(shell_pid) and "agora.round_child" in parts[2]:
            sampler.watch_child(int(parts[0]))


def main() -> int:
    ap = argparse.ArgumentParser(description="shell RSS end-to-end probe")
    ap.add_argument("--size-mb", type=int, default=50)
    ap.add_argument("--beats", type=int, default=12, help="append/round pairs, min 10")
    ap.add_argument("--sync-period-min", type=float, default=0.05, help="3 s rounds")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    assert args.beats >= 10, "the ticket wants >= 10 rounds"

    root = Path(tempfile.mkdtemp(prefix="agora-e2e-rss-"))
    result: dict = {"size_mb": args.size_mb, "beats": args.beats, "playground": str(root)}
    print(f"[e2e] playground {root}", flush=True)
    try:
        env = make_playground(root)
        target, big_id, workdir = build_session(env, root, args.size_mb)
        steps = ",".join(f"append:ZZE2E 第 {i} 句,wait-sync" for i in range(1, args.beats + 1))
        log_path = Path(env["AGORA_STATE_DIR"]) / "continue.log"
        env.update({"FAKE_AGENT_SCRIPT": steps, "FAKE_SYNC_TIMEOUT": "120",
                    "FAKE_SYNC_SIGNAL": str(log_path)})   # what wait-sync watches
        # the scripted fake appends to its own RESUMED session - the very jsonl
        # the periodic round collects back, which is how the file grows live.
        proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", "continue", "session", target,
             "--agent", "claude", "--dir", str(workdir), "--headless",
             "--sync-period", str(args.sync_period_min)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, env=env, cwd=str(root))
        sampler = Sampler(proc.pid)
        sampler.start()
        seen = 0
        deadline = time.monotonic() + 900
        rows: list[dict] = []
        last_round_at = time.monotonic()
        while seen < args.beats and time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"[e2e] shell exited early rc={proc.returncode}", flush=True)
                break
            time.sleep(0.25)
            discover_children(proc.pid, sampler)
            try:
                lines = [l for l in log_path.read_text(encoding="utf-8").splitlines()
                         if "定期寫回" in l]
            except OSError:
                lines = []
            while seen < len(lines):
                seen += 1
                sampler.boundary(seen)
                now = time.monotonic()
                rows.append({"round": seen, "line": lines[seen - 1].split(" ", 2)[-1],
                             "sec_since_last": round(now - last_round_at, 1),
                             "shell_now_mb": round(rss_kb(proc.pid) / 1024, 1)})
                last_round_at = now
                print(f"[e2e] {json.dumps(rows[-1])}", flush=True)
        if proc.poll() is None:
            proc.wait(timeout=240)
        code = proc.returncode
        time.sleep(1.5)
        for row in rows:
            r = row["round"]
            row["shell_peak_mb"] = round(sampler.peak(r, "shell") / 1024, 1)
            row["child_peak_mb"] = round(sampler.peak(r, "child") / 1024, 1)
        sampler.stop()
        shells = [row["shell_now_mb"] for row in rows]
        result.update({"shell_exit": code, "rounds": rows, "round_lines": seen,
                       "shells_now_mb": shells})
        if shells:
            result["flat_within_line"] = max(shells) <= shells[0] * 1.3 + 50
        print(f"[e2e] shell_exit={code} rounds={seen} shells_after={shells} "
              f"flat={result.get('flat_within_line')}", flush=True)
    finally:
        if args.keep:
            print(f"[e2e] playground kept at {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
            print("[e2e] playground removed", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"[e2e] results -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
