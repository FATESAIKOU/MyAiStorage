#!/usr/bin/env python3
"""Measure how soon the TUI's list is usable while the Drive sync runs (#491 C-22, design §6.3).

NOT a pytest test: run by hand from the worktree root::

    uv run python tests/perf/measure_tui_start.py --out /tmp/tui-start.json

Not run by CI. The sync child is the real one (`agora.sync_child`, started by the TUI); the
remote is the fake rclone's folder, so no Drive is touched. Per rep, a fresh playground:

* the local index holds `--count` sessions (3 KB bodies, md5 `md5`); the remote holds the same
  ULIDs with md5s that differ, so the sync has all of them to download;
* L=0: no local agent sessions; L=100×3: three fake agents with 100 sessions each.

It measures, per rep (medians over `--reps`):

1. `list_s`: from `run_test` until the list shows its rows and `down` moves the cursor 0 -> 1.
   This is the gate: with L=0 it must be under `--limit` (3 s), or the script exits 1.
2. `sync_s`: until the background sync has settled (the new list is shown or decided).
3. `freeze_s`: the longest gap between 50 ms ticks while the sync settles (the main thread's
   freeze when the new list is put on the screen; reported only).
4. `key_s`: the longest time from a `down`/`up` press until the cursor moved, while the sync
   is still running (reported only).
5. `baseline_s`: the same list time with the background sync not started (`sync_on_start=False`),
   for comparison (reported only).

The remote is built once per run (`--count` sessions, by another machine's state), then copied
into each rep. Isolation follows AGENTS.md「測試隔離」 (the same checks as measure_back_to_list.py).
"""

from __future__ import annotations

import argparse
import asyncio
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


def make_env(root: Path) -> None:
    for var in ISOLATION_VARS:
        os.environ.pop(var, None)
    home = root / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (root / "remote").mkdir(parents=True, exist_ok=True)
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


def body_of(kb: float) -> str:
    line = ("步驟：讀取設定檔 /Users/dev/proj/src/module.py 完成，"
            "warning: deprecated option 'legacy_mode' at line 42; exit code 0;  ")
    body, used = [], 0
    while used < kb * 1024:
        text = line + "x" * 40 + "\n"
        body.append(text)
        used += len(text.encode("utf-8"))
    return "".join(body)


def session_header(ulid: str, i: int) -> dict:
    return {
        "type": "Session", "title": f"會話 {i:04d}", "tags": [], "id": f"agora:{ulid}",
        "refs": [], "case": None,
        "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                  "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                  "source": {"agent": "opencode", "session_id": f"ses_{i}",
                             "created_at": "2026-10-01T00:00:00Z"}},
    }


def build_remote(base: Path, count: int) -> tuple[Path, list[str]]:
    """The remote, filled by another machine's state so that the sessions are really on Drive."""
    from agora import header as h
    from agora import store

    make_env(base)
    paths = store.Paths.from_env()
    other = store.Paths(config=paths.config, cache=base / "m2" / "cache", state=base / "m2" / "state")
    body = body_of(3)
    ulids = [h.new_ulid() for _ in range(count)]
    started = time.perf_counter()
    for i, ulid in enumerate(ulids):
        store.stage(other, session_header(ulid, i), body, b'{"x": 1}')
    store.upload_batch(store.Drive(other), other)
    print(f"[probe] remote with {count} sessions built in {time.perf_counter() - started:.1f} s", flush=True)
    return base / "remote", ulids


def build_local(paths, ulids: list[str]) -> None:
    """The local index: the same ULIDs, md5 `md5` (so every one of them differs from Drive's)."""
    from agora import store

    index = store.Index(paths)
    body = body_of(3)
    for i, ulid in enumerate(ulids):
        hdr = session_header(ulid, i)
        hdr["title"] = f"舊的 {i:04d}"          # Drive's title differs too, so every row changes when synced
        index.put(ulid, "md5", hdr, body)


class ProbeAgent:
    """L=100×3: three agents, each with 100 local sessions (what `import_rows` lists)."""

    def __init__(self, name: str, count: int):
        self.name = name
        self.count = count

    def list_sessions(self):
        from agora.agents.base import Listed
        return [Listed(f"{self.name}-{i}", "/tmp/p", f"本機 {i}", "2026-10-02T00:00:00Z")
                for i in range(self.count)]


async def one_rep(root: Path, remote_src: Path, ulids: list[str], locals_per_agent: int,
                  with_sync: bool) -> dict:
    from agora import store, tui

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    make_env(root)
    shutil.copytree(remote_src, root / "remote", dirs_exist_ok=True)
    assert_isolated(root)
    paths = store.Paths.from_env()
    build_local(paths, ulids)
    agents = [ProbeAgent(name, locals_per_agent) for name in ("claude", "opencode", "codex")] \
        if locals_per_agent else []

    class _Cli:
        AGENTS = ()

    app = tui.AgoraApp(paths, _Cli(), agents=agents, check_setup=False, sync_on_start=with_sync)
    ticks: list[float] = []
    out: dict = {}
    t0 = time.perf_counter()
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one("#table")
        for _ in range(2000):                                        # the list and a move of the cursor
            if table.row_count == len(ulids) and table.cursor_row == 0:
                await pilot.press("down")
                await pilot.pause()
                if table.cursor_row == 1:
                    break
            await pilot.pause()
        out["list_s"] = time.perf_counter() - t0
        if not with_sync:
            out["baseline_s"] = out["list_s"]
            return out
        # Keys while the child runs: press, then wait for the cursor to move (the latency).
        worst_key = 0.0
        last = time.perf_counter()
        key_name = "down"
        while not app.sync_settled.is_set():
            now = time.perf_counter()
            ticks.append(now - last)
            last = now
            before = table.cursor_row
            pressed = time.perf_counter()
            await pilot.press(key_name)
            for _ in range(200):
                if table.cursor_row != before or app.sync_settled.is_set():
                    break
                await pilot.pause()
            if table.cursor_row != before:
                worst_key = max(worst_key, time.perf_counter() - pressed)
            key_name = "up" if key_name == "down" else "down"
            await asyncio.sleep(0.2)
        await asyncio.wait_for(app.sync_settled.wait(), 600)
        await pilot.pause()
        out["sync_s"] = time.perf_counter() - t0
        out["key_s"] = worst_key
        out["freeze_s"] = max(ticks, default=0.0)
        out["rows"] = table.row_count
    return out


def median_of(reps: list[dict], key: str) -> float | None:
    values = [r[key] for r in reps if key in r]
    return statistics.median(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--count", type=int, default=3000, help="sessions on the remote and locally (default 3000)")
    parser.add_argument("--reps", type=int, default=3, help="reps per setting (default 3)")
    parser.add_argument("--limit", type=float, default=3.0, help="L=0 list_s gate in seconds (default 3)")
    parser.add_argument("--out", type=Path, help="write the JSON here as well")
    args = parser.parse_args()

    base = Path(tempfile.mkdtemp(prefix="tui-start-"))
    result: dict = {"count": args.count, "reps": args.reps, "limit_s": args.limit, "settings": {}}
    try:
        remote_src, ulids = build_remote(base / "build", args.count)
        settings = [("L=0", 0, True), ("L=100x3", 100, True)]
        for label, locals_n, _ in settings:
            reps = [asyncio.run(one_rep(base / f"rep-{label}-{i}", remote_src, ulids, locals_n, True))
                    for i in range(args.reps)]
            base_reps = [asyncio.run(one_rep(base / f"base-{label}-{i}", remote_src, ulids, locals_n, False))
                         for i in range(args.reps)]
            result["settings"][label] = {
                "list_s": median_of(reps, "list_s"),
                "sync_s": median_of(reps, "sync_s"),
                "freeze_s": median_of(reps, "freeze_s"),
                "key_s": median_of(reps, "key_s"),
                "baseline_list_s": median_of(base_reps, "baseline_s"),
                "rows": median_of(reps, "rows"),
                "raw": reps,
            }
            print(json.dumps({label: {k: v for k, v in result["settings"][label].items() if k != "raw"}},
                             ensure_ascii=False), flush=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    gate = result["settings"]["L=0"]["list_s"]
    if gate is None or gate >= args.limit:
        print(f"[probe] FAIL: L=0 list_s median {gate} is not under {args.limit} s", file=sys.stderr)
        return 1
    print(f"[probe] OK: L=0 list_s median {gate:.2f} s < {args.limit} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
