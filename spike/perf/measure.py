#!/usr/bin/env python3
"""Perf measurement for docs/perf.md: timings against real Drive agora-test.

Temp AGORA_CONFIG (rclone.conf symlink), CACHE and STATE; AGORA_FOLDER_NAME
is agora-test. One self-made `claude -p` session, then 20 staged sessions.
Every item runs 3 times, median reported. Prints only shapes and timings,
never dialogue content or secrets.

Usage: measure.py <work-root>   (work-root holds config/cache/state/logs)
Writes results.json into work-root and prints a summary table.
"""

import hashlib
import json
import os
import pwd
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

from agora import header as h
from agora import store
from agora.agents import claude as C

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
DISALLOW = "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"
P1 = "請用繁體中文列出「把 CSV 轉成 Markdown 表格」的三個步驟。只要文字回答，不要呼叫任何工具。"

results: dict = {"items": {}}
ULIDS: list[str] = []
UUIDS: list[str] = []
LEDGER = os.environ.get("PERF_LEDGER")


def record_ulid(ulid: str) -> None:
    """Write the ULID down the moment it exists, so cleanup is auditable
    even if this process dies before its finally block."""
    ULIDS.append(ulid)
    if LEDGER:
        with open(LEDGER, "a") as f:
            f.write(ulid + "\n")


def timed(fn, n=3):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return sorted(samples)[n // 2]


def note(key, seconds):
    results["items"][key] = round(seconds, 2)


def setup(root: Path, tag: str) -> store.Paths:
    config = root / tag / "config"
    config.mkdir(parents=True, exist_ok=True)
    link = config / "rclone.conf"
    if not link.exists():
        link.symlink_to(REAL_CONF)
    return store.Paths(config=config, cache=root / tag / "cache", state=root / tag / "state")


def proj_dir() -> Path:
    proj = Path("/tmp/agora-perf/proj")
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=proj, check=True)
    return proj.resolve()


def make_header(title: str, agent="claude", sid="x") -> dict:
    ulid = h.new_ulid()
    record_ulid(ulid)  # record at creation for purge
    return {"header": 1, "entity": "agora", "type": "session", "id": f"agora:{ulid}",
            "title": title, "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z", "refs": [], "case": None,
            "note": None, "tags": [], "relation": "import", "parents": [],
            "source": {"agent": agent, "session_id": sid, "dir": "/tmp/agora-perf/proj",
                       "created_at": "2026-10-01T00:00:00Z"}}


def main(root: Path):
    os.environ["AGORA_FOLDER_NAME"] = "agora-test"
    os.environ.pop("AGORA_RCLONE", None)
    assert REAL_CONF.exists(), "need ~/.config/agora/rclone.conf"
    proj = proj_dir()
    paths = setup(root, "m0")
    drive = store.Drive(paths)

    # (1) first sync (empty mirror) x3 with fresh mirrors, then no-change x3.
    def fresh_sync(i=[0]):
        p = setup(root, f"empty{i[0]}")
        i[0] += 1
        store.sync(p)
    note("sync_empty_mirror_s", timed(fresh_sync))
    note("sync_no_change_s", timed(lambda: store.sync(paths)))

    # One self-made claude session.
    uuid1 = str(uuid.uuid4())
    UUIDS.append(uuid1)
    if LEDGER:
        with open(LEDGER, "a") as f:
            f.write("uuid " + uuid1 + "\n")
    env = {**os.environ, "HOME": str(REAL_HOME), "AGORA_CLAUDE_HOME": str(REAL_HOME)}
    t0 = time.perf_counter()
    proc = subprocess.run(
        ["claude", "-p", "--session-id", uuid1, P1,
         "--disallowedTools", DISALLOW],  # flags after the prompt
        cwd=proj, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-500:]
    note("claude_1_round_s", time.perf_counter() - t0)

    # (2) import of that session, split into phases, x3 (one ULID each,
    # since push consumes the outbox folder).
    raw = C.ADAPTER.export(uuid1).raw
    hdrs = [make_header("效能量測表格") for _ in range(3)]
    folders: dict = {}
    body = "## user\n效能量測表格\n"

    def do_export():
        C.ADAPTER.export(uuid1)

    def do_stage(i=[0]):
        folders[i[0]] = store.stage(paths, hdrs[i[0]], body, raw)
        i[0] += 1

    def do_push(i=[0]):
        store.push_one(store.Drive(paths), folders[i[0]])
        i[0] += 1

    def do_verify():
        hdr = hdrs[0]
        got = store.fetch_raw(paths, store.Drive(paths),
                              hdr["id"].split(":", 1)[1], dict(hdr))
        assert hashlib.md5(got).hexdigest() == hdr["raw"]["md5"]

    note("import_export_s", timed(do_export))
    note("import_stage_s", timed(do_stage))
    note("import_push_s", timed(do_push))
    note("import_verify_s", timed(do_verify))

    # (3) search with sync vs --no-sync.
    store.sync(paths)
    note("search_with_sync_s",
         timed(lambda: (store.sync(paths), store.Index(paths).search("表格", []))[1]))
    idx = store.Index(paths)
    note("search_no_sync_s", timed(lambda: idx.search("表格", [])))

    # (4) 20 more small sessions, then re-measure (1) and (3).
    for i in range(20):
        hdr_i = make_header(f"效能量測 {i:02d} 表格")
        folder = store.stage(paths, hdr_i, f"## user\n效能量測第{i}個表格\n",
                             f'{{"n": {i}}}'.encode())
        store.push_one(store.Drive(paths), folder)
    results["sessions_on_drive_added"] = 21

    def fresh_sync2(i=[0]):
        p = setup(root, f"full{i[0]}")
        i[0] += 1
        store.sync(p)
    note("sync_empty_mirror_20_sessions_s", timed(fresh_sync2))
    note("sync_no_change_20_sessions_s", timed(lambda: store.sync(paths)))
    note("search_with_sync_20_sessions_s",
         timed(lambda: (store.sync(paths), store.Index(paths).search("表格", []))[1]))
    idx2 = store.Index(paths)
    note("search_no_sync_20_sessions_s", timed(lambda: idx2.search("表格", [])))

    (root / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    for key, val in results["items"].items():
        print(f"{key}: {val}s")


def cleanup(root: Path):
    config = root / "m0" / "config"
    drive = store.Drive(store.Paths(config=config, cache=root / "m0" / "cache",
                                    state=root / "m0" / "state"))
    try:
        folder_id = drive.folder_id()
    except Exception as e:
        print(f"cleanup: folder_id failed: {type(e).__name__}")
        folder_id = None
    for ulid in ULIDS:  # only sessions this run created, one by one
        if folder_id is None:
            break
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", folder_id,
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
    encoded = C.encode_project_dir("/private/tmp/agora-perf/proj")
    basedir = REAL_HOME / ".claude" / "projects" / encoded
    cfgdir = REAL_HOME / ".claude"
    for sid in UUIDS:  # only our own uuids
        jsonl = basedir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = basedir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
        for extra in [cfgdir / "session-env" / sid, cfgdir / "file-history" / sid]:
            if extra.is_dir():
                shutil.rmtree(extra)
    for leftover in [basedir / "memory", basedir]:
        try:
            leftover.rmdir()
        except OSError:
            pass
    shutil.rmtree("/tmp/agora-perf/proj", ignore_errors=True)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    try:
        main(root)
    finally:
        cleanup(root)
