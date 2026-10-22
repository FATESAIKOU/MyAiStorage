#!/usr/bin/env python3
"""Measure `agora import`'s memory, time and disk (R1 / design K.2, I.8).

NOT a pytest test: run by hand from the worktree root::

    uv run python tests/perf/measure_import_mem.py --out /tmp/r1-import.json
    uv run python tests/perf/measure_import_mem.py --sizes 100,400,800 \
        --long-lines 1,10 --out /tmp/r1-import.json

Not run by CI (it lives outside `tests/unit` and is not named `test_*.py`); anybody can run it by
hand. It reads nothing real and connects to nothing: every directory is a private temp one and the
Drive is `tests/fakes/fake_rclone.py`. The numbers it prints are the ones in `docs/perf.md`.

What it does:

1. For each size, builds a private playground under `/tmp/r1-*` (HOME /
   AGORA_CONFIG / AGORA_CACHE_DIR / AGORA_STATE_DIR / TMPDIR), points
   AGORA_RCLONE at `tests/fakes/fake_rclone.py`, AGORA_FOLDER_NAME at
   agora-test, AGORA_UPLOAD at inline. Nothing real is read or written.
2. Generates a fake claude jsonl of that size with a fixed shape: one short
   conversation plus a run of `tool_result` lines of a *constant* size (~1 MB),
   so the reading version (`session.md`) stays small while the tool output
   grows - otherwise the measurement would be the reading version's growth
   (design B.2). The sidecar is a fixed 64 KB.
3. Runs `agora import` in a **fresh child interpreter** (`--child-import`) and
   reads that child's `resource.getrusage(RUSAGE_SELF).ru_maxrss` after the
   import: the generator, the fake rclone and this script's own allocations are
   not in the number. Wall time is the same child's.
4. Records the disk the import leaves (session + outbox + mirror + fake Drive),
   the uploaded bytes/file count from `calls.log`, and the reading version's
   size. Removes the playground right away unless `--keep`.
5. The assert (design I.8 / L15): `peak(800) <= 1.2 * peak(100)` and
   `peak(800) <= 256 MiB`. Exit code 1 when either fails. The three wall times
   are printed and recorded; they have no threshold.

The long-line cases (`--long-lines 1,10`) are design K14: one JSON line of
about that many MB. They are recorded, not asserted.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

FAKE_RCLONE = REPO / "tests" / "fakes" / "fake_rclone.py"
AGENT_HEX = "0123456789abcdef"
MIB = 1 << 20

#: The fixed text of the conversation: a handful of short turns, whatever the
#: tool output's size. The reading version is O(these turns), not O(tool output).
HEAD_TEXT = "把這批模組的設定檢查一遍，有問題的列出來。"
ANSWER_TEXT = "看完了，沒有問題；下一步整理成報告。"


class Gen:
    """A deterministic claude session whose tool_result lines have one size."""

    def __init__(self, session_id: str, workdir: str, body: str):
        self.session_id = session_id
        self.workdir = workdir
        self.body = body
        self.turn = 0
        self.uuid_n = 0
        self.seconds = 0
        self.leaf: str | None = None

    def _uuid(self) -> str:
        self.uuid_n += 1
        return f"{self.uuid_n:08x}-aaaa-4bbb-8ccc-0123456789ab"

    def _stamp(self) -> str:
        self.seconds += 7
        return (datetime(2026, 10, 22, 9, 0, 0, tzinfo=timezone.utc)
                + timedelta(seconds=self.seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _base(self, otype: str) -> dict:
        o = {"type": otype, "sessionId": self.session_id, "uuid": self._uuid(),
             "parentUuid": self.leaf, "timestamp": self._stamp(), "cwd": self.workdir,
             "version": "2.1.286", "isSidechain": False, "userType": "external",
             "entrypoint": "cli", "gitBranch": "main"}
        self.leaf = o["uuid"]
        return o

    def head(self) -> bytes:
        o = self._base("user")
        o["message"] = {"role": "user", "content": HEAD_TEXT}
        return _line(o)

    def exchange(self) -> bytes:
        """assistant(tool_use) + user(tool_result, the big line) + assistant(text)."""
        self.turn += 1
        tool_id = f"toolu_{self.turn:06d}"
        a1 = self._base("assistant")
        a1["message"] = {"id": f"msg_{self.turn:06d}", "type": "message", "role": "assistant",
                         "model": "claude-haiku", "stop_reason": "tool_use",
                         "content": [{"type": "tool_use", "id": tool_id, "name": "Read",
                                      "input": {"file_path": f"/proj/src/module_{self.turn}.py"}}]}
        u1 = self._base("user")
        # The bulk rides twice, the way a real Read result does (K.2's shape).
        u1["toolUseResult"] = {"type": "text", "content": self.body}
        u1["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": self.body}]}
        a2 = self._base("assistant")
        a2["message"] = {"id": f"msg_{self.turn:06d}b", "type": "message", "role": "assistant",
                         "model": "claude-haiku", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": ANSWER_TEXT}]}
        return _line(a1) + _line(u1) + _line(a2)


def _line(o: dict) -> bytes:
    return (json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8")


def _body(bytes_wanted: int) -> str:
    """A plausible tool output of about `bytes_wanted` bytes (CJK + ASCII)."""
    chunk = "步驟 {n}：讀取設定檔 /proj/src/module.py 完成，exit code 0；" + "x" * 24
    parts: list[str] = []
    used = 0
    n = 0
    while used < bytes_wanted:
        n += 1
        piece = chunk.format(n=n)
        parts.append(piece)
        used += len(piece.encode("utf-8"))
    return "\n".join(parts)


def write_session(path: Path, sidecar: Path, target_bytes: int, line_bytes: int,
                  workdir: str, *, one_big_line: bool = False) -> int:
    """Write the fake session; return the longest line's byte length."""
    # One user line carries the body twice (`toolUseResult` + `tool_result`).
    body = _body(max(1024, line_bytes // 2))
    gen = Gen(path.stem, workdir, body)
    aux = sidecar / "subagents" / f"agent-{AGENT_HEX}.jsonl"
    aux.parent.mkdir(parents=True, exist_ok=True)
    (aux.parent / f"agent-{AGENT_HEX}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "查設定", "requestShape": "direct"}))
    longest = 0
    with path.open("wb") as f, aux.open("wb") as g:
        head = gen.head()
        f.write(head)
        longest = max(longest, len(head))
        per_exchange = len(body.encode("utf-8")) * 2 + 2048
        exchanges = 1 if one_big_line else max(1, (target_bytes - len(head)) // per_exchange)
        for _ in range(exchanges):
            chunk = gen.exchange()
            f.write(chunk)
            longest = max(longest, max(len(part) + 1 for part in chunk.split(b"\n") if part))
        if not one_big_line:
            g.write(_line({"type": "sidecar", "payload": "x" * (64 * 1024)}))
    return longest


def make_env(root: Path) -> dict[str, str]:
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (root / "remote").mkdir()
    wrapper = root / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "AGORA_CLAUDE_HOME": str(home),
        "AGORA_CONFIG": str(root / "config"), "AGORA_CACHE_DIR": str(root / "cache"),
        "AGORA_STATE_DIR": str(root / "state"), "FAKE_REMOTE": str(root / "remote"),
        "AGORA_RCLONE": str(wrapper), "AGORA_FOLDER_NAME": "agora-test",
        "AGORA_UPLOAD": "inline", "AGORA_CLAUDE_CMD": "/nonexistent/claude",
        "TMPDIR": str(root),
    })
    return env


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _uploads(root: Path) -> tuple[int, int, int]:
    """(bytes, files, copy calls) the fake Drive received, from the playground.

    A fresh playground has no earlier uploads, so what is under the remote is
    exactly this import's upload. The call count comes from calls.log (the
    subcommand sits after rclone's own `--config` flags).
    """
    base = root / "remote" / "agora-test" / "sessions"
    files = [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []
    calls = 0
    log = root / "calls.log"
    if log.is_file():
        for raw in log.read_text().splitlines():
            args = json.loads(raw)
            if any(tok in ("copy", "copyto", "move") for tok in args):
                calls += 1
    return sum(p.stat().st_size for p in files), len(files), calls


def run_child_import(session_id: str) -> int:
    """The measured process: one `agora import`, nothing else (--child-import)."""
    from agora import cli
    started = time.perf_counter()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["import", "session", "--external-session-id", session_id,
                       "--agent", "claude"])
    seconds = time.perf_counter() - started
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"rc": rc, "seconds": seconds, "maxrss": maxrss,
                      "stdout": out.getvalue()}), flush=True)
    return rc


def rss_bytes(value: int) -> int:
    """`ru_maxrss` is bytes on macOS and KiB everywhere else."""
    return value if sys.platform == "darwin" else value * 1024


def measure(label: str, size_mb: int, *, line_bytes: int, one_big_line: bool,
            keep: bool) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"r1-import-{label}-", dir="/tmp"))
    print(f"[probe] {label}: playground {root}", flush=True)
    started = time.perf_counter()
    try:
        env = make_env(root)
        workdir = root / "proj"
        workdir.mkdir()
        pdir = root / "home" / ".claude" / "projects" / "-r1-proj"
        pdir.mkdir(parents=True)
        session_id = str(uuid.uuid4())
        path = pdir / f"{session_id}.jsonl"
        longest = write_session(path, pdir / session_id, size_mb * MIB, line_bytes,
                                str(workdir), one_big_line=one_big_line)
        print(f"[probe] {label}: generated {path.stat().st_size / MIB:.1f} MB "
              f"(longest line {longest / MIB:.2f} MB) in {time.perf_counter() - started:.1f}s",
              flush=True)

        child = subprocess.run([sys.executable, str(Path(__file__)), "--child-import",
                                "--session-id", session_id],
                               env=env, capture_output=True, text=True)
        if child.returncode != 0:
            raise SystemExit(f"[probe] {label}: child failed: {child.stderr[-2000:]}")
        report = json.loads(child.stdout.strip().splitlines()[-1])
        if report["rc"] != 0:
            raise SystemExit(f"[probe] {label}: import rc={report['rc']}: "
                             f"{report['stdout'][-2000:]}")
        peak = rss_bytes(report["maxrss"])

        outbox = _tree_bytes(Path(env["AGORA_STATE_DIR"]) / "outbox")
        mirror = _tree_bytes(Path(env["AGORA_CACHE_DIR"]) / "sessions")
        remote = _tree_bytes(root / "remote")
        uploaded, upload_files, upload_calls = _uploads(root)
        reading = sum(p.stat().st_size for p in (Path(env["AGORA_STATE_DIR"]) / "outbox")
                      .rglob("session.md")) or sum(
            p.stat().st_size for p in (Path(env["AGORA_CACHE_DIR"]) / "sessions")
            .rglob("session.md"))
        row = {
            "label": label,
            "size_mb": round(path.stat().st_size / MIB, 1),
            "longest_line_mb": round(longest / MIB, 2),
            "peak_rss_mb": round(peak / MIB, 1),
            "seconds": round(report["seconds"], 2),
            "reading_kb": round(reading / 1024, 1),
            "disk_mb": {
                "session": round(path.stat().st_size / MIB, 1),
                "outbox": round(outbox / MIB, 1),
                "mirror": round(mirror / MIB, 1),
                "fake_drive": round(remote / MIB, 1),
                "total": round((path.stat().st_size + outbox + mirror + remote) / MIB, 1),
            },
            "uploaded_mb": round(uploaded / MIB, 1),
            "upload_files": upload_files,
            "upload_calls": upload_calls,
            "wall_seconds": round(time.perf_counter() - started, 1),
            "ulid": report["stdout"].strip().splitlines()[-1] if report["stdout"].strip() else None,
        }
        print(f"[probe] {label}: {json.dumps({k: v for k, v in row.items() if k != 'ulid'})}",
              flush=True)
        return row
    finally:
        if keep:
            print(f"[probe] {label}: playground kept at {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="agora import memory / time / disk probe (R1)")
    ap.add_argument("--sizes", default="100,400,800", help="MB, comma separated")
    ap.add_argument("--long-lines", default="1,10", help="single-line MB, comma separated")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep", action="store_true", help="keep the playgrounds (debug)")
    ap.add_argument("--child-import", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--session-id", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child_import:
        return run_child_import(args.session_id)

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    long_lines = [float(s) for s in args.long_lines.split(",") if s.strip()]
    # A constant line size keeps the per-line bound (and the reading version) the
    # same at every total size; 512 KB of body is one ~1 MB user line (K14 uses
    # the long-line runs for bigger ones).
    rows = []
    for size in sizes:
        rows.append(measure(f"{size}mb", size, line_bytes=512 * 1024, one_big_line=False,
                            keep=args.keep))
    for line_mb in long_lines:
        rows.append(measure(f"line-{line_mb:g}mb", max(8, int(line_mb * 4)),
                            line_bytes=int(line_mb * MIB), one_big_line=True,
                            keep=args.keep))

    by_label = {row["label"]: row for row in rows}
    result: dict = {"rows": rows, "python": sys.version.split()[0],
                    "platform": sys.platform}
    failed = False
    if "100mb" in by_label and "800mb" in by_label:
        small = by_label["100mb"]["peak_rss_mb"]
        big = by_label["800mb"]["peak_rss_mb"]
        ratio = big / small if small else float("inf")
        checks = {"peak_800_le_1.2x_peak_100": big <= 1.2 * small,
                  "peak_800_le_256mib": big <= 256,
                  "ratio_800_over_100": round(ratio, 3)}
        result["checks"] = checks
        failed = not (checks["peak_800_le_1.2x_peak_100"] and checks["peak_800_le_256mib"])
        print(f"[probe] CHECK peak(800)={big} MiB <= 1.2 x peak(100)={small} MiB "
              f"-> {checks['peak_800_le_1.2x_peak_100']} (ratio {ratio:.2f})", flush=True)
        print(f"[probe] CHECK peak(800)={big} MiB <= 256 MiB -> "
              f"{checks['peak_800_le_256mib']}", flush=True)
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(f"[probe] wrote {args.out}", flush=True)
    print(f"[probe] {'FAIL' if failed else 'PASS'}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
