#!/usr/bin/env python3
"""Measure `agora continue`'s write-back memory (ticket impl-mem, phase 1).

NOT a pytest test: run by hand, one size at a time, from the worktree root::

    uv run python tests/perf/measure_continue_mem.py --size-mb 20 --rounds 8 \
        --out /tmp/agora-mem-20.json                 # true-RSS ladder
    uv run python tests/perf/measure_continue_mem.py --size-mb 20 --rounds 8 \
        --tracemalloc --out /tmp/agora-mem-20-tm.json  # python-level peaks + top lines

What it does:

1. Builds an isolated playground (HOME / AGORA_CONFIG / AGORA_CACHE_DIR /
   AGORA_STATE_DIR / FAKE_REMOTE all under one temp dir) and points
   AGORA_RCLONE at tests/fakes/fake_rclone.py, AGORA_FOLDER_NAME at
   agora-test. The user's real config, sessions and cache are never read.
2. Generates a fake but realistic claude session jsonl (user / assistant /
   tool_use / tool_result with large tool outputs, bookkeeping lines, a
   subagent sidecar, docs/spike/claude.md line shapes) of the requested
   size, plus a small seed session.
3. Imports the seed as an agora Session (the continue target), then runs N
   rounds of the write-back, growing the session a little every round the way
   a live session does. `--round-mode inproc` (the shape before
   impl-mem-subproc) does `cli._collect_new` + `cli._write_one` in THIS
   process; `--round-mode child` runs one real `cli._periodic_write` per round
   - the shell shape since impl-mem-subproc, heavy work in a fresh
   `agora.round_child` - and samples that child's RSS too: the shell's own
   deliberately stays flat.
4. Per round it records the tracemalloc peak (--tracemalloc), the true RSS
   peak (a sampler thread polling `ps -o rss=`) and the resident RSS after
   the round. Stage probes (monkeypatched wrappers; src/ untouched) say how
   deep the pipeline sat after each heavy call, and round 1 keeps a
   tracemalloc snapshot of the deepmost stage for the top-lines answer.
5. Writes the numbers to --out as JSON and deletes the playground.

Disk care: one run at a time; the playground holds the fake file plus at
most one raw copy each in outbox / mirror / fake remote (stale remote raws
are pruned between rounds); the whole playground is removed at the end.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

FAKE_RCLONE = REPO / "tests" / "fakes" / "fake_rclone.py"

BOOKKEEPING = ("summary", "queue-operation", "last-prompt", "cost-state", "mode")
AGENT_HEX = "0123456789abcdef"


def rss_kb(pid: int | None = None) -> int:
    """Resident set size in KB, via ps (no psutil in this repo's deps)."""
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid or os.getpid())],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


class RssSampler(threading.Thread):
    """Polls this process's RSS (and any watched child pids) in the background;
    per-round max via mark()/peak_of(). The watched set is how `--round-mode
    child` sees the round child's peak: the shell stays small by design, the
    peak lives in a process the shell's own RSS never covers."""

    def __init__(self, every: float = 0.05):
        super().__init__(daemon=True)
        self.every = every
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._marks: list[tuple[int, str, int]] = []   # (round, "shell"/"child", rss_kb)
        self._watched: set[int] = set()
        self.round = 0

    def mark(self, round_no: int) -> None:
        with self._lock:
            self.round = round_no

    def watch(self, pid: int) -> None:
        with self._lock:
            self._watched.add(pid)

    def unwatch(self) -> None:
        with self._lock:
            self._watched.clear()

    def run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                rnd = self.round
                watched = set(self._watched)
            self._marks.append((rnd, "shell", rss_kb()))
            for pid in watched:
                kb = rss_kb(pid)
                if kb:                                   # gone: ps prints nothing
                    self._marks.append((rnd, "child", kb))
            self._stop.wait(self.every)

    def stop(self) -> None:
        self._stop.set()

    def peak_of(self, round_no: int, kind: str = "shell") -> int:
        with self._lock:
            hits = [kb for r, k, kb in self._marks if r == round_no and k == kind]
        return max(hits) if hits else 0


# ---------------------------------------------------------------------------
# Fake claude session with real shape (docs/spike/claude.md line types)
# ---------------------------------------------------------------------------

class SessionGen:
    """Writes one exchange after another until a size target is reached."""

    def __init__(self, session_id: str, workdir: str, stamp: datetime):
        self.session_id = session_id
        self.workdir = workdir
        self.now = stamp
        self.uuid_n = 0
        self.turn_n = 0
        self.leaf: str | None = None

    def _uuid(self) -> str:
        self.uuid_n += 1
        return f"{self.uuid_n:08x}-aaaa-4bbb-8ccc-{self.session_id.replace('-', '')[:12]}"

    def _stamp(self) -> str:
        self.now += timedelta(seconds=7)
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @staticmethod
    def _line(o: dict) -> bytes:
        return (json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8")

    def _base(self, otype: str) -> dict:
        self.uuid_n += 1
        o = {"type": otype, "sessionId": self.session_id, "uuid": self._uuid(),
             "parentUuid": self.leaf, "timestamp": self._stamp(), "cwd": self.workdir,
             "version": "2.1.286", "isSidechain": False, "userType": "external",
             "entrypoint": "cli", "gitBranch": "main"}
        self.leaf = o["uuid"]
        return o

    def big_text(self, kb: int, tag: str) -> str:
        """~kb of plausible tool output: numbered log lines, CJK and ASCII."""
        chunk = (f"{tag} 步驟 {{n}}：讀取設定檔 /Users/dev/proj/src/module_{{n}}.py 完成，"
                 "warning: deprecated option 'legacy_mode' at line {n}; "
                 "exit code 0; elapsed 0.{d}s;  ")
        lines: list[str] = []
        used = 0
        n = 0
        while used < kb * 1024:
            n += 1
            line = chunk.format(n=n, d=n % 10) + "x" * 16
            lines.append(line)
            used += len(line.encode("utf-8"))
        return "\n".join(lines)

    def exchange(self, kb: int) -> list[bytes]:
        """assistant(text+tool_use) → user(big tool_result) → assistant(text).

        The tool result's bulk rides twice, the way real Read output does: in
        the message's tool_result block and in the toolUseResult field.
        """
        out = []
        self.turn_n += 1
        tool_id = f"toolu_{self.turn_n:06d}"
        a1 = self._base("assistant")
        a1["requestId"] = f"req_{self.turn_n:06d}"
        a1["message"] = {"id": f"msg_{self.turn_n:06d}", "type": "message", "role": "assistant",
                         "model": "claude-haiku", "stop_reason": "tool_use",
                         "content": [
                             {"type": "text", "text": f"第 {self.turn_n} 輪：我先看看檔案再決定做法。"},
                             {"type": "tool_use", "id": tool_id, "name": "Read",
                              "input": {"file_path": f"/Users/dev/proj/src/module_{self.turn_n}.py",
                                        "limit": 2000}},
                         ]}
        out.append(self._line(a1))
        body = self.big_text(kb, f"[round {self.turn_n}]")
        u1 = self._base("user")
        u1["toolUseResult"] = {"type": "text", "content": body}
        u1["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": body}]}
        out.append(self._line(u1))
        a2 = self._base("assistant")
        a2["requestId"] = f"req_{self.turn_n:06d}b"
        a2["message"] = {"id": f"msg_{self.turn_n:06d}b", "type": "message", "role": "assistant",
                         "model": "claude-haiku", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text":
                                      f"看完了。第 {self.turn_n} 個模組的設定沒問題，"
                                      "legacy_mode 還在用但可以先留著；下一步我會把結果整理進報告，"
                                      "再檢查下一個檔案是不是有一樣的問題。"}]}
        out.append(self._line(a2))
        return out

    def head(self) -> list[bytes]:
        return [self._line(self._base("user")
                           | {"message": {"role": "user",
                                          "content": "把這批模組的設定檢查一遍，有問題的列出來。"}})]

    def bookkeeping(self) -> bytes:
        o = {"type": BOOKKEEPING[self.turn_n % len(BOOKKEEPING)],
             "sessionId": self.session_id, "timestamp": self._stamp()}
        if o["type"] == "summary":
            o["summary"] = f"階段 {self.turn_n} 的進度"
            o["leafUuid"] = self.leaf
        elif o["type"] == "cost-state":
            o.update({"totalCostUSD": 0.12, "totalDuration": 3400, "totalLinesAdded": 3,
                      "totalLinesRemoved": 1, "totalAPIDuration": 2100,
                      "totalAPIDurationWithoutRetries": 2100, "totalToolDuration": 900,
                      "hasUnknownModelCost": False, "startTime": self._stamp(),
                      "modelUsage": {"claude-haiku": {"inputTokens": 1, "outputTokens": 2}}})
        elif o["type"] == "last-prompt":
            o.update({"lastPrompt": "繼續", "leafUuid": self.leaf})
        elif o["type"] == "queue-operation":
            o.update({"content": "繼續", "operation": "enqueue"})
        else:
            o["mode"] = "default"
        return self._line(o)

    def sidecar_lines(self, kb: int, count: int) -> list[bytes]:
        """Subagent transcript lines (spike S9: sessionId is the parent's uuid)."""
        out = []
        for _ in range(count):
            self.turn_n += 1
            role = "assistant" if self.turn_n % 2 == 0 else "user"
            o = {"type": role, "sessionId": self.session_id, "uuid": self._uuid(),
                 "parentUuid": self.leaf, "timestamp": self._stamp(), "cwd": self.workdir,
                 "version": "2.1.286", "isSidechain": True, "userType": "external",
                 "entrypoint": "cli", "gitBranch": "main"}
            o["message"] = {"role": role, "content": [
                {"type": "text", "text": self.big_text(kb, "[sub]")}]}
            out.append(self._line(o))
        return out


def write_session(path: Path, sidecar: Path, target_bytes: int, reserve_bytes: int,
                  workdir: str) -> None:
    """Generate the main jsonl (+subagent sidecar) up to ~target_bytes."""
    gen = SessionGen(path.stem, workdir, datetime(2026, 10, 19, 9, 0, 0, tzinfo=timezone.utc))
    aux_path = sidecar / "subagents" / f"agent-{AGENT_HEX}.jsonl"
    aux_path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f, aux_path.open("wb") as g:
        for line in gen.head():
            f.write(line)
        next_mark = 0
        while f.tell() < target_bytes - reserve_bytes:
            kb = min(64, max(4, (target_bytes - f.tell()) // 2048))
            for line in gen.exchange(kb):
                f.write(line)
            if f.tell() >= next_mark:            # bookkeeping every ~1/16 of the file
                f.write(gen.bookkeeping())
                next_mark = f.tell() + target_bytes // 16
            if gen.turn_n % 30 < 3:
                for sub in gen.sidecar_lines(2, 3):
                    g.write(sub)
        f.write(gen.bookkeeping())
    (sidecar / "subagents" / f"agent-{AGENT_HEX}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "查設定", "requestNonInteractive": True,
         "requestShape": "direct", "spawnDepth": 1, "toolUseId": "toolu_meta"}))


def append_exchanges(path: Path, sidecar: Path, grow_bytes: int, workdir: str) -> None:
    """Grow a session the way a live one does: a few more exchanges at the end."""
    gen = SessionGen(path.stem, workdir, datetime.now(timezone.utc))
    aux_path = sidecar / "subagents" / f"agent-{AGENT_HEX}.jsonl"
    with path.open("ab") as f:
        start = f.tell()
        while f.tell() - start < grow_bytes:
            kb = min(64, max(4, grow_bytes // 2048))
            for line in gen.exchange(kb):
                f.write(line)
        f.write(gen.bookkeeping())
    with aux_path.open("ab") as g:
        for sub in gen.sidecar_lines(2, 1):
            g.write(sub)


def count_messages(path: Path) -> int:
    from agora.agents import claude
    return claude._count_messages(claude._parse_all(claude._read_lines(path)))


# ---------------------------------------------------------------------------
# Stage probes: wrappers around the pipeline's heavy calls (src untouched)
# ---------------------------------------------------------------------------

PROBED = (("agora.agents.claude", "_read_lines"), ("agora.agents.claude", "_exported"),
          ("agora.agents.claude", "_parse_all"), ("agora.agents.claude", "_pack_raw"),
          ("agora.cli", "reading_of"),
          ("agora.agents.base", "format_reading"), ("agora.store", "stage"))

# Low-5 (review 低5): after impl-mem 2a the continue round runs `reading_of` ->
# `format_reading`, never `reading`. The probes wrap where the call actually
# resolves the name: cli's own binding (install() runs after cli is imported, so
# patching base.reading_of would miss the call from cli.py) and the base global
# reading_of/format_reading look up at call time.
SNAPSHOT_STAGES = {"_read_lines", "_exported", "_pack_raw", "reading_of",
                   "format_reading", "stage"}


class StageProbes:
    """Per-stage depth (traced current after the call) and timing, every round.

    Round peaks come from one reset_peak() at round start - resetting per stage
    would clobber it. In round 1 the wrapper keeps a tracemalloc snapshot after
    each heavy stage; the deepmost one answers "which objects are the big ones
    inside the peak".
    """

    def __init__(self, traced: bool, snapshot_round: int = 1):
        self.traced = traced
        self.snapshot_round = snapshot_round
        self.rows: list[dict] = []
        self.snapshots: dict[str, tuple[int, object]] = {}
        self.round_no = 0
        self._originals: list[tuple] = []
        self._tm = None
        if traced:
            import tracemalloc
            self._tm = tracemalloc

    def install(self) -> None:
        import importlib
        for mod_name, attr in PROBED:
            mod = importlib.import_module(mod_name)
            original = getattr(mod, attr)
            probes = self

            def wrapper(*args, _orig=original, _probes=probes, _stage=attr, **kwargs):
                t0 = time.perf_counter()
                try:
                    return _orig(*args, **kwargs)
                finally:
                    dt = time.perf_counter() - t0
                    cur = None
                    if _probes.traced:
                        cur = _probes._tm.get_traced_memory()[0]
                        if (_probes.round_no == _probes.snapshot_round
                                and _stage in SNAPSHOT_STAGES):
                            _probes.snapshots[_stage] = (cur, _probes._tm.take_snapshot())
                    _probes.rows.append({"round": _probes.round_no, "stage": _stage,
                                         "current_mb": round(cur / (1 << 20), 1)
                                         if cur is not None else None,
                                         "seconds": round(dt, 2)})

            setattr(mod, attr, wrapper)
            self._originals.append((mod, attr, original))

    def uninstall(self) -> None:
        for mod, attr, original in self._originals:
            setattr(mod, attr, original)

    def drop_snapshots(self) -> None:
        self.snapshots.clear()

    def top_lines(self, top: int) -> list[str]:
        """Top allocations of the deepmost probed stage (traced runs only)."""
        if not (self.traced and self.snapshots):
            return []
        stage = max(self.snapshots, key=lambda s: self.snapshots[s][0])
        _cur, snap = self.snapshots[stage]
        stats = snap.statistics("lineno")[:top]
        out = [f"deepmost-stage={stage}"]
        for s in stats:
            frame = s.traceback.format()[0].strip()
            out.append(f"{s.size / (1 << 20):7.1f} MB  {s.count:8d} blocks  {frame}")
        return out


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------

def make_env(root: Path) -> None:
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (root / "remote").mkdir()
    wrapper = root / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    os.environ.update({
        "HOME": str(home), "AGORA_CLAUDE_HOME": str(home),
        "AGORA_CONFIG": str(root / "config"), "AGORA_CACHE_DIR": str(root / "cache"),
        "AGORA_STATE_DIR": str(root / "state"), "FAKE_REMOTE": str(root / "remote"),
        "AGORA_RCLONE": str(wrapper), "AGORA_FOLDER_NAME": "agora-test",
        "AGORA_UPLOAD": "inline", "AGORA_CLAUDE_CMD": "/nonexistent/claude",
        "AGORA_OPENCODE_CMD": "/nonexistent/opencode",
        "TMPDIR": str(root),
    })


def prune_remote_raws(remote: Path) -> None:
    """Keep only the newest raw per session in the fake Drive (disk care)."""
    sessions = remote / "sessions"
    if not sessions.is_dir():
        return
    for ulid in sessions.iterdir():
        raws = sorted(ulid.glob("raw-*.json"), key=lambda p: p.stat().st_mtime)
        for old in raws[:-1]:
            old.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description="agora continue write-back memory probe")
    ap.add_argument("--size-mb", type=int, default=20)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--growth-pct", type=float, default=2.0,
                    help="per-round growth as %% of the initial size")
    ap.add_argument("--tracemalloc", action="store_true")
    ap.add_argument("--round-mode", choices=("inproc", "child"), default="inproc",
                    help="inproc: _collect_new + _write_one here (HEAD shape); "
                         "child: one full cli._periodic_write round per cycle - the "
                         "heavy work in a fresh agora.round_child process, which is "
                         "what the shell actually does since impl-mem-subproc")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--keep", action="store_true", help="keep the playground (debug)")
    args = ap.parse_args()
    if args.round_mode == "child" and args.tracemalloc:
        sys.exit("--tracemalloc only makes sense with --round-mode inproc: "
                 "in child mode the allocations live in another process")

    root = Path(tempfile.mkdtemp(prefix=f"agora-mem-{args.size_mb}mb-"))
    result: dict = {"size_mb": args.size_mb, "rounds": args.rounds,
                    "growth_pct": args.growth_pct, "tracemalloc": args.tracemalloc,
                    "round_mode": args.round_mode,
                    "python": sys.version.split()[0], "playground": str(root)}
    print(f"[probe] playground {root}", flush=True)
    try:
        make_env(root)
        from agora import cli, store
        from agora.agents.claude import ClaudeAgent, encode_project_dir, projects_dir

        workdir = root / "proj"
        workdir.mkdir()
        pdir = projects_dir() / encode_project_dir(workdir)
        pdir.mkdir(parents=True)
        seed_id, big_id = str(uuid_mod.uuid4()), str(uuid_mod.uuid4())
        seed_path = pdir / f"{seed_id}.jsonl"
        big_path = pdir / f"{big_id}.jsonl"
        target = args.size_mb << 20
        write_session(big_path, pdir / big_id, target, reserve_bytes=256_000,
                      workdir=str(workdir))
        write_session(seed_path, pdir / seed_id, 256_000, reserve_bytes=0,
                      workdir=str(workdir))
        msgs0 = count_messages(big_path)
        result["main_bytes"] = big_path.stat().st_size
        result["sidecar_bytes"] = sum(p.stat().st_size for p in (pdir / big_id).rglob("*")
                                      if p.is_file())
        result["message_count"] = msgs0
        print(f"[probe] fake session: main {result['main_bytes'] >> 20} MB, "
              f"sidecar {result['sidecar_bytes'] >> 10} KB, {msgs0} messages", flush=True)

        # Import the seed: the Session this run "continues", so the write-back
        # updates it in place exactly like a real non-fork continue.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["import", "session", "--external-session-id", seed_id,
                           "--agent", "claude"])
        assert rc == 0, out.getvalue()
        target_id = out.getvalue().strip().splitlines()[-1]
        result["target"] = target_id

        paths = store.Paths.from_env()
        record = {"agora_id": target_id, "agent": "claude", "agent_session_id": big_id,
                  "dir": str(workdir),
                  "parent": {"id": target_id, "raw_md5": "0" * 32},
                  "parent_headers": [], "title": "量測用 Session", "before_count": msgs0,
                  "started_at": "2026-10-19T00:00:00Z", "header_updates": {},
                  "add_tags": [], "saved_md5": "0" * 32}
        run = cli._Run(paths, record)
        agent = ClaudeAgent()

        child_mode = args.round_mode == "child"
        probes = StageProbes(args.tracemalloc)
        if args.tracemalloc:
            import tracemalloc
            tracemalloc.start()
        if not child_mode:
            probes.install()        # the stage lines describe the in-process pipeline
        sampler = RssSampler()
        sampler.start()
        real_popen = cli.subprocess.Popen
        if child_mode:
            # watch every round child the shell spawns, so its peak is in the table
            # too - the shell's own RSS deliberately does not carry the peak anymore.
            def _watch_round_child(argv, **kw):
                proc = real_popen(argv, **kw)
                if isinstance(argv, (list, tuple)) and list(argv[1:3]) == ["-m", "agora.round_child"]:
                    sampler.watch(proc.pid)
                return proc
            cli.subprocess.Popen = _watch_round_child

        rounds: list[dict] = []
        try:
            for r in range(1, args.rounds + 1):
                probes.round_no = r
                sampler.mark(r)
                if args.tracemalloc:
                    import tracemalloc
                    tracemalloc.reset_peak()
                grow = int(target * args.growth_pct / 100)
                append_exchanges(big_path, pdir / big_id, grow, str(workdir))
                size_mb = big_path.stat().st_size / (1 << 20)
                t0 = time.perf_counter()
                before_md5, before_fail = run.last_md5, run.failures
                exported = saved = None
                if child_mode:
                    cli._periodic_write(run, agent)     # the shell's real round shape
                    written, failed = run.last_md5 != before_md5, run.failures > before_fail
                    sampler.unwatch()                   # the round child is reaped
                else:
                    exported = cli._collect_new(run, agent)
                    if exported is not None:
                        saved, _target, _note = cli._write_one(paths, run, exported, agent,
                                                               check_drive=False)
                    written, failed = saved is not None, False
                dt = time.perf_counter() - t0
                cur = peak = None
                if args.tracemalloc:
                    cur, peak = tracemalloc.get_traced_memory()
                row = {"round": r, "session_mb": round(size_mb, 1),
                       "collected": (exported is not None) if not child_mode else None,
                       "written": written, "failed": failed,
                       "seconds": round(dt, 1),
                       "tm_current_mb": round(cur / (1 << 20), 1) if cur is not None else None,
                       "tm_peak_mb": round(peak / (1 << 20), 1) if peak is not None else None,
                       "rss_after_mb": round(rss_kb() / 1024, 1),
                       "rss_peak_mb": round(sampler.peak_of(r) / 1024, 1)}
                if child_mode:
                    row["child_peak_mb"] = round(sampler.peak_of(r, "child") / 1024, 1)
                if r == 1 and args.tracemalloc:
                    row["top"] = probes.top_lines(args.top)
                    probes.drop_snapshots()
                rounds.append(row)
                print(f"[probe] round {r}: "
                      f"{json.dumps({k: v for k, v in row.items() if k != 'top'})}",
                      flush=True)
                for line in row.get("top", []):
                    print(f"[probe]   {line}", flush=True)
                prune_remote_raws(root / "remote")
        finally:
            probes.uninstall()
            if child_mode:
                cli.subprocess.Popen = real_popen
            sampler.stop()
        result["rounds"] = rounds
        result["stage_rows"] = probes.rows
        shells = [row["rss_after_mb"] for row in rounds]
        if shells:
            result["shell"] = {"first_mb": shells[0], "last_mb": shells[-1],
                               "max_mb": max(shells),
                               "flat_within_line": max(shells) <= shells[0] * 1.3 + 50}
            if child_mode:
                kids = [row.get("child_peak_mb") or 0 for row in rounds]
                result["child_peak_max_mb"] = max(kids)
    finally:
        if args.keep:
            print(f"[probe] playground kept at {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
            print("[probe] playground removed", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"[probe] results → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
