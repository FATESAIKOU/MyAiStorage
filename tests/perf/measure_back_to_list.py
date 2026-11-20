#!/usr/bin/env python3
"""Measure how long `agora`'s TUI takes to get back to the list after an action (#488).

NOT a pytest test: run by hand from the worktree root::

    uv run python tests/perf/measure_back_to_list.py --out /tmp/back-to-list.json

Not run by CI (it lives outside `tests/unit` and is not named `test_*.py`). What it
measures is the tail of `act()` - the reload and the table rebuild after the result
window is closed - with 3000 indexed sessions and no local agent sessions. Before
#488 the table was rebuilt twice there (the reload's `show`, then another `show` to
clear the marks); now the marks are cleared first and the table is rebuilt once.

Status after #488 (measured by the reviewer, 3000 sessions, headless): the tail is
still about 0.7-1.0 s, so the `--limit 0.5` target below is NOT met and the script
exits 1. Only part of the tail is the table: `show()` is about 0.13 s and the whole
`reload()` about 0.26 s; the rest (about 0.4 s) is outside `reload()` and is not yet
attributed (probably the redraw after the result window closes). Dropping the second
rebuild is a small win, below this script's noise. The limit is kept as the goal.

Two numbers are recorded per rep:

* `total_s`  - from confirming the delete window to the list being back.
* `tail_s`   - from the reload that follows the result window to `act()` returning
  (the rebuild itself, including the second `show` before #488).

What it does:

1. Builds a private playground under a new temp dir (HOME / AGORA_CONFIG /
   AGORA_CACHE_DIR / AGORA_STATE_DIR / AGORA_CODEX_HOME / TMPDIR), points
   AGORA_RCLONE at `tests/fakes/fake_rclone.py`, and clears the inherited
   XDG_* / CLAUDE_CONFIG_DIR / OPENCODE_CONFIG_CONTENT / CODEX_HOME knobs
   (AGENTS.md「測試隔離」). Nothing real is read or written.
2. Fills one index with `count` sessions (3 KB bodies by default) and lists no
   local agent sessions, so `import_rows` has nothing to do.
3. Drives the real screen headless (`App.run_test`): presses `d` (delete the row
   under the cursor - nothing marked, so the action still carries one row), confirms,
   closes the result window, and times the way back. It also counts how many times
   the table was rebuilt per action, which is the assertion #488 makes.
4. Prints the median of `--reps` (3) and exits 1 when the tail median is over
   `--limit` seconds.
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


class Done:
    """What `spawn` hands the Run window: it finished, and it said nothing."""

    pid = 4242

    def __init__(self):
        self.stdout = iter([])

    def poll(self):
        return 0

    def wait(self):
        return 0

    def send_signal(self, sig):
        pass


class FakeCli:
    AGENTS: tuple = ()


def make_app(paths):
    from agora import tui

    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    app.spawn = lambda argv: Done()
    return app


async def measure(app, reps: int) -> dict:
    """Drive `reps` deletes and time the way back to the list after each."""
    from agora import tui

    keeps: list = []                # the `keep` of every `show`, in order
    original_show = tui.AgoraApp.show

    def counting(self, keep=None):
        keeps.append(keep)
        return original_show(self, keep)

    tui.AgoraApp.show = counting

    reloaded_at: list[float] = []   # when the rebuild after the result window started
    original_reload = tui.AgoraApp.reload

    def timing_reload(self, keep_marked=False):
        reloaded_at.append(time.perf_counter())
        return original_reload(self, keep_marked)

    tui.AgoraApp.reload = timing_reload

    done: list = []
    act = app.act

    async def recording(*args, **kwargs):
        result = await act(*args, **kwargs)
        done.append(result)
        return result

    app.act = recording
    total, tail, shown = [], [], []
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert app.rows["agora"], "the list never loaded"
            print(f"[probe] start-up table rebuilds: {len(keeps)}", flush=True)
            for _ in range(reps):
                already = done[:]
                keeps_before, reloads_before = len(keeps), len(reloaded_at)
                await pilot.press("d")                 # the row under the cursor
                await pilot.pause()
                confirm = time.perf_counter()
                await pilot.press("down", "enter")     # 確定
                for _ in range(300):
                    if isinstance(app.screen, tui.Tell):
                        break
                    await pilot.pause(0.01)
                assert isinstance(app.screen, tui.Tell), "the result window never appeared"
                await pilot.press("space")             # close it: the way back starts here
                for _ in range(300):
                    if done != already:
                        break
                    await pilot.pause(0.01)
                assert done != already, "the action never came back to the list"
                now = time.perf_counter()
                assert len(reloaded_at) > reloads_before, "the action never reloaded the list"
                total.append(now - confirm)
                tail.append(now - reloaded_at[-1])
                shown.append(len(keeps) - keeps_before)
                print(f"[probe] rep {len(total)}: total {total[-1]:.4f} s "
                      f"tail {tail[-1]:.4f} s, table rebuilt {shown[-1]} time(s)", flush=True)
    finally:
        tui.AgoraApp.show = original_show
        tui.AgoraApp.reload = original_reload
    return {"total_s": total, "tail_s": tail, "shows": shown}


def main() -> int:
    ap = argparse.ArgumentParser(description="agora TUI back-to-list probe (#488)")
    ap.add_argument("--count", type=int, default=3000, help="indexed sessions")
    ap.add_argument("--body-kb", type=float, default=3.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--limit", type=float, default=0.5,
                    help="median tail seconds the probe calls good (the issue's number)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep", action="store_true", help="keep the playground (debug)")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="agora-back-to-list-"))
    print(f"[probe] playground {root}", flush=True)
    result: dict = {"count": args.count, "body_kb": args.body_kb, "reps": args.reps,
                    "limit": args.limit, "playground": str(root)}
    try:
        make_env(root)
        assert_isolated(root)
        from agora import store, tui
        print(f"[probe] agora from {store.__file__}", flush=True)
        paths = store.Paths.from_env()
        index, build_s = build_index(paths, args.count, body_of(args.body_kb))
        print(f"[probe] index build {build_s:.2f} s", flush=True)
        app = make_app(paths)
        measured = asyncio.run(measure(app, args.reps))

        total_median = statistics.median(measured["total_s"])
        tail_median = statistics.median(measured["tail_s"])
        result.update(measured)
        result.update({"total_median": total_median, "tail_median": tail_median,
                       "ok": tail_median < args.limit})
        print(f"[probe] back to the list: total median {total_median:.4f} s, "
              f"tail median {tail_median:.4f} s (limit {args.limit:.2f} s), "
              f"table rebuilt {measured['shows']} -> {'PASS' if result['ok'] else 'FAIL'}",
              flush=True)
        if args.out:
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        return 0 if result["ok"] else 1
    finally:
        if args.keep:
            print(f"[probe] kept {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
