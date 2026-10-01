#!/usr/bin/env python3
"""Split the 85s cold-sync number: listing vs downloads vs indexing.

Same temp-dir pattern as measure.py; stages 20 tiny sessions, then in a
fresh cache times each phase separately on a warm Drive. Prints timings
only. Usage: probe_split.py <work-root>
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))
from agora import header as h
from agora import store

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
ULIDS = []
LEDGER = os.environ.get("PERF_LEDGER")
N = 5


def record_ulid(ulid: str) -> None:
    """Write the ULID down the moment it exists, so cleanup is auditable."""
    ULIDS.append(ulid)
    if LEDGER:
        with open(LEDGER, "a") as f:
            f.write(ulid + "\n")


def paths_for(root, tag):
    config = root / tag / "config"
    config.mkdir(parents=True, exist_ok=True)
    link = config / "rclone.conf"
    if not link.exists():
        link.symlink_to(REAL_CONF)
    return store.Paths(config=config, cache=root / tag / "cache", state=root / tag / "state")


def header(title):
    ulid = h.new_ulid()
    record_ulid(ulid)
    return {"header": 1, "entity": "agora", "type": "session", "id": f"agora:{ulid}",
            "title": title, "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z", "refs": [], "case": None,
            "note": None, "tags": [], "relation": "import", "parents": [],
            "source": {"agent": "opencode", "session_id": f"ses_p{len(ULIDS)}",
                       "created_at": "2026-10-01T00:00:00Z"}}


def main(root):
    os.environ["AGORA_FOLDER_NAME"] = "agora-test"
    os.environ.pop("AGORA_RCLONE", None)
    warm = paths_for(root, "probe-warm")
    drive = store.Drive(warm)
    for i in range(N):
        folder = store.stage(warm, header(f"量測 {i:02d} 表格"),
                             f"## user\n量測第{i}個表格\n", f'{{"n": {i}}}'.encode())
        store.push_one(store.Drive(warm), folder)

    cold = paths_for(root, "probe-cold")
    out = {}
    mine = set(ULIDS)
    t0 = time.perf_counter()
    remote = drive.list_sessions()
    out["list_sessions_s"] = round(time.perf_counter() - t0, 2)
    out["sessions_listed"] = len(remote or {})
    mine_remote = {u: f for u, f in (remote or {}).items() if u in mine}
    out["mine_listed"] = len(mine_remote)

    t0 = time.perf_counter()
    for ulid, files in mine_remote.items():   # only our own sessions
        if files.get("session.md"):
            cold.mirror.mkdir(parents=True, exist_ok=True)
            drive.download(ulid, "session.md", cold.mirror / ulid / "session.md")
    out["download_all_s"] = round(time.perf_counter() - t0, 2)
    out["per_download_s"] = round(out["download_all_s"] / max(len(mine_remote), 1), 2)

    t0 = time.perf_counter()
    index = store.Index(cold)
    for ulid in mine_remote:
        path = cold.mirror / ulid / "session.md"
        if path.is_file():
            hdr, body = h.split_document(path.read_text(encoding="utf-8"))
            index.put(ulid, "x", hdr, body)
    out["index_put_all_s"] = round(time.perf_counter() - t0, 2)
    out["hits"] = len(index.search("表格", []))
    (root / "probe.json").write_text(json.dumps(out, indent=2))
    for k, v in out.items():
        print(f"{k}: {v}")


def cleanup(root):
    warm = paths_for(root, "probe-warm")
    try:
        fid = store.Drive(warm).folder_id()
    except Exception:
        return
    for ulid in ULIDS:
        subprocess.run(["rclone", "--config", str(REAL_CONF),
                        "--drive-root-folder-id", fid,
                        "purge", f"gdrive:sessions/{ulid}"], capture_output=True)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    try:
        main(root)
    finally:
        cleanup(root)
