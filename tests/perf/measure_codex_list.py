#!/usr/bin/env python3
"""Measure `codex.list_sessions` cold and hot (#487: (path, mtime, size) cache).

NOT a pytest test: run by hand from the worktree root::

    uv run python tests/perf/measure_codex_list.py                    # this checkout
    uv run python tests/perf/measure_codex_list.py --src /tmp/main-487/src   # before #487

Not run by CI (outside `tests/unit`, not named `test_*.py`). The assertion
(#487): with 1000 rollouts of ~100 KB, the hot call - a second listing with no
file changed, which is what the TUI does after every action - finishes in under
0.05 s. Before the cache every call read and parsed all of them, so cold and hot
were the same and both grew with the bytes on disk.

What it does:

1. Builds a private playground under a new temp dir (HOME / AGORA_CONFIG /
   AGORA_CACHE_DIR / AGORA_STATE_DIR / AGORA_CODEX_HOME / TMPDIR), clears the
   inherited XDG_* / CLAUDE_CONFIG_DIR / OPENCODE_CONFIG_CONTENT / CODEX_HOME
   knobs, and asserts every directory resolves under it before measuring
   (AGENTS.md「測試隔離」; same NOT ISOLATED check as measure_continue_mem.py).
2. Writes `--sessions` fake rollouts of about `--size-kb` KB each: a
   session_meta header, one short user message (the title) and large
   assistant messages, so the whole file is parsed as before.
3. Measures the cold call (cache cleared) and the hot call (nothing changed)
   `--rounds` times each, reports medians, and exits 1 when the hot median is
   over `--limit`. `--src` points at another checkout's `src` to measure the
   pre-#487 version.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# AGENTS.md「測試隔離」: keep every adapter away from a real agent store.
ISOLATION_VARS = ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                  "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "CODEX_HOME")


def assert_isolated(root: Path) -> None:
    """Refuse to run unless config / cache / state / HOME / TMPDIR are under the temp root."""
    from agora.store import Paths
    p = Paths.from_env()
    base = root.resolve()
    for name, d in (("config", p.config), ("cache", p.cache), ("state", p.state)):
        assert d.resolve().is_relative_to(base), f"NOT ISOLATED: {name}={d} not under {base}"
    assert Path(os.environ["HOME"]).resolve().is_relative_to(base), "NOT ISOLATED: HOME"
    assert Path(os.environ["TMPDIR"]).resolve().is_relative_to(base), "NOT ISOLATED: TMPDIR"
    leaked = [var for var in ISOLATION_VARS if var in os.environ]
    assert not leaked, f"NOT ISOLATED: still set {', '.join(leaked)}"
    print(f"[probe] isolation OK: config={p.config} cache={p.cache} state={p.state} "
          f"HOME={os.environ['HOME']} TMPDIR={os.environ['TMPDIR']} "
          f"cleared={' '.join(ISOLATION_VARS)} (all under {base})", flush=True)


def make_env(root: Path) -> None:
    for var in ISOLATION_VARS:
        os.environ.pop(var, None)
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    os.environ.update({
        "HOME": str(home),
        "AGORA_CONFIG": str(root / "config"),
        "AGORA_CACHE_DIR": str(root / "cache"),
        "AGORA_STATE_DIR": str(root / "state"),
        "AGORA_CODEX_HOME": str(root / "codex"),
        "TMPDIR": str(root),
    })


def write_rollout(path: Path, sid: str, size_bytes: int) -> None:
    """One rollout of about `size_bytes`: a header, the title's user message, filler."""
    rows = [json.dumps({"type": "session_meta", "payload": {
        "id": sid, "cwd": "/work/repo", "timestamp": "2026-11-08T00:00:00Z"}})]
    rows.append(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "標題"}]}}))
    filler = "x" * 900
    i = 0
    total = sum(len(r) + 1 for r in rows)
    while total < size_bytes:
        role = "assistant" if i % 2 == 0 else "user"
        rows.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": role,
            "content": [{"type": "output_text" if role == "assistant" else "input_text",
                         "text": f"{i} {filler}"}]}}))
        total += len(rows[-1]) + 1
        i += 1
    path.write_text("\n".join(rows) + "\n")


def build_playground(root: Path, count: int, size_bytes: int) -> Path:
    sessions = Path(os.environ["AGORA_CODEX_HOME"]) / "sessions" / "2026" / "11" / "08"
    sessions.mkdir(parents=True)
    for n in range(count):
        sid = f"00000000-0000-0000-0000-{n:012d}"
        write_rollout(sessions / f"rollout-2026-11-08T00-00-{n:02d}-{sid}.jsonl", sid, size_bytes)
    return sessions


def main() -> int:
    ap = argparse.ArgumentParser(description="codex list_sessions cold/hot probe (#487)")
    ap.add_argument("--sessions", type=int, default=1000)
    ap.add_argument("--size-kb", type=int, default=100)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--limit", type=float, default=0.05, help="hot median ceiling, seconds")
    ap.add_argument("--src", default=str(REPO / "src"),
                    help="checkout's src to measure (another one for the pre-#487 numbers)")
    ap.add_argument("--keep", action="store_true", help="leave the playground on disk")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="codex-list-487-", dir="/tmp/opencode"))
    try:
        make_env(root)
        sys.path.insert(0, args.src)
        from agora.agents import codex
        assert_isolated(root)
        print(f"[probe] agora from {codex.__file__}", flush=True)

        size_bytes = args.size_kb * 1024
        started = time.perf_counter()
        sessions = build_playground(root, args.sessions, size_bytes)
        built = time.perf_counter() - started
        used = sum(p.stat().st_size for p in sessions.iterdir())
        print(f"[probe] {args.sessions} rollouts, {used / (1 << 20):.1f} MB, "
              f"built in {built:.2f}s", flush=True)

        cache = getattr(codex, "_LIST_CACHE", None)
        cold, hot = [], []
        for _ in range(args.rounds):
            if cache is not None:
                cache.clear()
            t = time.perf_counter()
            rows = codex.ADAPTER.list_sessions()
            cold.append(time.perf_counter() - t)
            assert len(rows) == args.sessions, f"listed {len(rows)} of {args.sessions}"
            t = time.perf_counter()
            again = codex.ADAPTER.list_sessions()
            hot.append(time.perf_counter() - t)
            assert again == rows

        cold_med, hot_med = statistics.median(cold), statistics.median(hot)
        print(f"[probe] cold rounds={['%.4f' % v for v in cold]} median={cold_med:.4f}s", flush=True)
        print(f"[probe] hot  rounds={['%.4f' % v for v in hot]} median={hot_med:.4f}s "
              f"(limit {args.limit}s)", flush=True)
        if hot_med >= args.limit:
            print(f"[probe] FAIL: hot median {hot_med:.4f}s >= {args.limit}s", flush=True)
            return 1
        print("[probe] OK", flush=True)
        return 0
    finally:
        if args.keep:
            print(f"[probe] kept {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
