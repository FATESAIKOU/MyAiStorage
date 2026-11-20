#!/usr/bin/env python3
"""Measure `agora` TUI's `import_rows` with many indexed and many local sessions (#486).

NOT a pytest test: run by hand from the worktree root::

    uv run python tests/perf/measure_import_rows.py --out /tmp/import-rows.json

Not run by CI (it lives outside `tests/unit` and is not named `test_*.py`). The
assert (#486): with 3000 indexed sessions (3 KB bodies) and 3000 listed local
agent sessions, `import_rows` finishes in under a second. Before the one-pass
map it was one `Index.by_source` full scan per listed session.

What it does:

1. Builds a private playground under a new temp dir (HOME / AGORA_CONFIG /
   AGORA_CACHE_DIR / AGORA_STATE_DIR / AGORA_CODEX_HOME / TMPDIR), points
   AGORA_RCLONE at `tests/fakes/fake_rclone.py`, and clears the inherited
   XDG_* / CLAUDE_CONFIG_DIR / OPENCODE_CONFIG_CONTENT / CODEX_HOME knobs
   (AGENTS.md「測試隔離」). Nothing real is read or written.
2. Fills one index with `count` sessions whose source ids differ from the
   listed local ones, then builds `per_agent` fake listed sessions for each of
   the three agents. No agent binary or store is touched.
3. Runs `tui.import_rows` `--reps` times and records each wall time; the
   assertion uses the median. Exit code 1 when it is over `--limit` seconds.
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
sys.path.insert(0, str(REPO / "src"))

FAKE_RCLONE = REPO / "tests" / "fakes" / "fake_rclone.py"

# AGENTS.md「測試隔離」: keep every agent adapter from finding a real store.
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
          f"HOME={os.environ['HOME']} TMPDIR={os.environ['TMPDIR']} (all under {base})", flush=True)


def make_env(root: Path) -> None:
    for var in ISOLATION_VARS:
        os.environ.pop(var, None)
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (root / "remote").mkdir()
    wrapper = root / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    os.environ.update({
        "HOME": str(home),
        "AGORA_CLAUDE_HOME": str(home),
        "AGORA_CODEX_HOME": str(home / ".codex"),
        "AGORA_AGY_HOME": str(home / ".gemini"),
        "AGORA_CONFIG": str(root / "config"),
        "AGORA_CACHE_DIR": str(root / "cache"),
        "AGORA_STATE_DIR": str(root / "state"),
        "FAKE_REMOTE": str(root / "remote"),
        "AGORA_RCLONE": str(wrapper),
        "AGORA_FOLDER_NAME": "agora-test",
        "AGORA_UPLOAD": "inline",
        "AGORA_CLAUDE_CMD": "/nonexistent/claude",
        "AGORA_OPENCODE_CMD": "/nonexistent/opencode",
        "TMPDIR": str(root),
    })


class FakeAgent:
    """Only the part of an agent adapter `import_rows` uses: the listed sessions."""

    def __init__(self, name: str, listed: list):
        self.name, self.listed = name, listed

    def list_sessions(self):
        return self.listed


def body_of(kb: float) -> str:
    line = ("步驟：讀取設定檔 /Users/dev/proj/src/module.py 完成，"
            "warning: deprecated option 'legacy_mode' at line 42; exit code 0;  ")
    body, used = [], 0
    while used < kb * 1024:
        text = line + "x" * 40 + "\n"
        body.append(text)
        used += len(text.encode("utf-8"))
    return "".join(body)


def build_index(paths, count: int, body: str):
    from agora import header as h
    from agora import store

    index = store.Index(paths)
    names = ("claude", "opencode", "codex")
    started = time.perf_counter()
    for i in range(count):
        ulid = h.new_ulid()
        hdr = {
            "type": "Session", "title": f"已匯入 {i:04d}", "tags": [], "id": f"agora:{ulid}",
            "refs": [], "case": None,
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                      "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                      "source": {"agent": names[i % len(names)], "session_id": f"index-{i}",
                                 "created_at": "2026-10-01T00:00:00Z"}},
        }
        index.put(ulid, "md5", hdr, body)
    return index, time.perf_counter() - started


def build_agents(per_agent: int):
    from agora.agents.base import Listed

    names = ("claude", "opencode", "codex")
    agents = []
    for name in names:
        listed = [Listed(f"{name}-local-{i:04d}", "/tmp/p", f"本機 {i:04d}",
                         f"2026-10-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z")
                  for i in range(per_agent)]
        agents.append(FakeAgent(name, listed))
    return agents


def main() -> int:
    ap = argparse.ArgumentParser(description="agora TUI import_rows probe (#486)")
    ap.add_argument("--count", type=int, default=3000, help="indexed sessions")
    ap.add_argument("--per-agent", type=int, default=1000, help="listed local sessions per agent")
    ap.add_argument("--body-kb", type=float, default=3.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--limit", type=float, default=1.0, help="median seconds import_rows must stay under")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep", action="store_true", help="keep the playground (debug)")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="agora-import-rows-"))
    print(f"[probe] playground {root}", flush=True)
    result: dict = {"count": args.count, "per_agent": args.per_agent,
                    "body_kb": args.body_kb, "reps": args.reps, "limit": args.limit,
                    "playground": str(root)}
    try:
        make_env(root)
        assert_isolated(root)
        from agora import store, tui
        print(f"[probe] agora from {store.__file__}", flush=True)
        paths = store.Paths.from_env()
        body = body_of(args.body_kb)
        index, build_s = build_index(paths, args.count, body)
        agents = build_agents(args.per_agent)

        times = []
        rows = []
        for rep in range(args.reps):
            started = time.perf_counter()
            rows = tui.import_rows(index, agents)
            times.append(time.perf_counter() - started)
            print(f"[probe] rep {rep + 1}: {times[-1]:.4f} s, {len(rows)} rows", flush=True)

        median = statistics.median(times)
        result.update({"times": times, "median": median, "rows": len(rows),
                       "index_build_s": build_s, "ok": median < args.limit})
        print(f"[probe] index build {build_s:.2f} s; import_rows median {median:.4f} s "
              f"(limit {args.limit:.2f} s) -> {'PASS' if result['ok'] else 'FAIL'}", flush=True)
        if args.out:
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0 if result["ok"] else 1
    finally:
        if args.keep:
            print(f"[probe] kept {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
