#!/usr/bin/env python3
"""A fake rclone for unit tests (test-plan 0.3).

The "remote" is the folder $FAKE_REMOTE. Folder IDs are just folder names.
Every call is appended to $FAKE_REMOTE/../calls.log. FAKE_RCLONE_FAIL=<text>
makes any call whose arguments contain <text> fail with rc=1.
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

remote = Path(os.environ["FAKE_REMOTE"])
log = remote.parent / "calls.log"


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_RCLONE_FAIL") and os.environ["FAKE_RCLONE_FAIL"] in " ".join(args):
    fail("injected failure")


def snapshot():
    """Copy the whole remote aside after every successful call (test-plan 0.3b)."""
    snaps = remote.parent / "snapshots"
    snaps.mkdir(exist_ok=True)
    n = len([p for p in snaps.iterdir() if p.is_dir()])
    if remote.exists():
        shutil.copytree(remote, snaps / f"{n:04d}")

root = remote
rest = []
i = 0
while i < len(args):
    if args[i] == "--config":
        i += 2
        continue
    if args[i] == "--drive-root-folder-id":
        root = remote / args[i + 1]
        i += 2
        continue
    rest.append(args[i])
    i += 1

cmd, params = rest[0], rest[1:]


def resolve(target):
    if not target.startswith("gdrive:"):
        return None
    return root / target[len("gdrive:"):]


if cmd == "mkdir":
    resolve(params[0]).mkdir(parents=True, exist_ok=True)
elif cmd == "lsjson":
    base = resolve(params[0])
    if not base.exists():
        fail("error listing: directory not found")
    recursive = "-R" in params
    out = []
    items = base.rglob("*") if recursive else base.iterdir()
    for p in sorted(items):
        if "--dirs-only" in params and not p.is_dir():
            continue
        if "--files-only" in params and not p.is_file():
            continue
        entry = {"Path": str(p.relative_to(base)), "Name": p.name, "IsDir": p.is_dir(), "ID": p.name}
        if p.is_file() and "--hash" in params:
            entry["Hashes"] = {"md5": hashlib.md5(p.read_bytes()).hexdigest()}
        out.append(entry)
    dup = os.environ.get("FAKE_RCLONE_DUP")
    if dup and "--dirs-only" in params and params[0] == "gdrive:":
        out.append({"Path": dup, "Name": dup, "IsDir": True, "ID": dup + "-dup"})
    print(json.dumps(out))
elif cmd == "copyto":
    src, dst = params[0], params[1]
    src_p = resolve(src) or Path(src)
    dst_p = resolve(dst) or Path(dst)
    if not src_p.exists():
        fail("source not found")
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_p, dst_p)
elif cmd == "copy" and "--files-from" in params:
    src = resolve(params[0])
    dst = Path(params[1])
    listed = Path(params[params.index("--files-from") + 1]).read_text().split()
    for rel in listed:
        if (src / rel).exists():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / rel, dst / rel)
elif cmd == "deletefile":
    resolve(params[0]).unlink()
elif cmd == "purge":
    shutil.rmtree(resolve(params[0]))
else:
    fail(f"fake rclone does not support {cmd}")
snapshot()
