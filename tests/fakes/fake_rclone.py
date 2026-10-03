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


if os.environ.get("FAKE_RCLONE_DELAY"):     # let a test watch a slow upload happen
    import time
    time.sleep(float(os.environ["FAKE_RCLONE_DELAY"]))

args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
def _should_fail(needle: str) -> bool:
    if needle in " ".join(args):
        return True
    # The batch uploader sends with `copy --files-from` where it used to be one
    # `copyto` per file, so a test that breaks "copyto" means "break the upload".
    return needle == "copyto" and "copy" in args and "--files-from" in args


if os.environ.get("FAKE_RCLONE_FAIL") and _should_fail(os.environ["FAKE_RCLONE_FAIL"]):
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
    # local -> Drive, one call for a whole batch (local-first-writes M4)
    src = resolve(params[0]) or Path(params[0])
    dst = resolve(params[1]) or Path(params[1])
    listed = Path(params[params.index("--files-from") + 1]).read_text().split()
    for rel in listed:
        if not (src / rel).exists():
            fail(f"source not found: {rel}")
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, dst / rel)
elif cmd == "delete" and "--files-from" in params:
    # the superseded raws, gone in one call
    dst = resolve(params[0])
    listed = Path(params[params.index("--files-from") + 1]).read_text().split()
    for rel in listed:
        target = dst / rel
        if target.is_file():
            target.unlink()
        parent = target.parent
        while parent != dst and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
elif cmd == "copy":          # a local tree up to the remote, same names overwritten (agora sync)
    src, dst = Path(params[0]), resolve(params[1])
    excluded = [params[n + 1] for n, p in enumerate(params) if p == "--exclude"]
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src).as_posix()
        if path.is_file() and not any(rel == e or rel.startswith(e.rstrip("*")) for e in excluded):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dst / rel)
elif cmd == "deletefile":
    target = resolve(params[0])
    if not target.is_file():
        fail("error: object not found")      # what rclone says, so callers can match on it
    target.unlink()
elif cmd == "purge":
    target = resolve(params[0])
    if not target.is_dir():
        fail("error: directory not found")   # ditto
    shutil.rmtree(target)
else:
    fail(f"fake rclone does not support {cmd}")
snapshot()
