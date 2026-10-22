#!/usr/bin/env python3
"""A fake rclone for unit tests (test-plan 0.3).

The "remote" is the folder $FAKE_REMOTE. Folder IDs are just folder names.
Every call is appended to $FAKE_REMOTE/../calls.log. FAKE_RCLONE_FAIL=<text>
makes any call whose arguments contain <text> fail with rc=1.

FAKE_RCLONE_RECORD_STDIN=1 also appends what stdin was to
$FAKE_REMOTE/../stdin.log: `/dev/null` when the caller opened it on the void,
`fifo` when it inherited one (a pipe the test is holding open, so a child that
tried to read would block), `closed`, or `other` (a terminal, a file).

FAKE_RCLONE_OMIT=<name>[,<name>]: the whole `lsjson -R` listing leaves those
folders (and everything under them) out, while a directed `lsjson` on the folder
still sees it - a Drive listing that silently lost a folder (#152).
FAKE_RCLONE_OMIT_FROM=<n> starts leaving them out at the Nth whole listing, so a
test can break the re-check and leave the first check intact (default 1).

FAKE_RCLONE_OMIT_FILES=<name>[,<name>]: the whole `lsjson -R` listing leaves those
files out, while a directed `lsjson` on the folder still sees them - a listing that
lost the files themselves, not the folder (#155). FAKE_RCLONE_OMIT_FILES_FROM=<n>
does the same from the Nth whole listing on, so a test can break only the
confirmation listing after the copies and leave the rounds before it intact.

FAKE_RCLONE_NOT_FOUND_RC=3 makes an `lsjson` on a folder that is not there exit
with the status a real rclone uses for it. The message then carries no
"directory not found" (the default rc=1 keeps those words for older tests), so a
caller can only tell this apart from a failed look by the returncode.

FAKE_RCLONE_PURGE_AFTER=<file>: another machine deletes a folder *now*. The file
holds {"after": <needle>, "purge": <path under the folder id>}. The first call
matching the needle (matched the way FAKE_RCLONE_FAIL matches, listing included)
does its own work first, then that folder goes, and the file goes with it - one
shot. A test can put a deletion exactly between two of the uploader's calls, e.g.
right after the listing that decided an update's id was still there, without
racing a sleep.

FAKE_RCLONE_CLEAN_AFTER=<file>: another machine's ordinary cleanup of an old file
*now*. The file holds {"after": <needle>, "folder": <path under the folder id>,
"remove": [<name>...], "add": {<name>: <text>}}. The first call matching the
needle finishes its own work, then those files go and those appear - one shot.
A test can put「刪掉舊 raw、放上別台的新 raw」exactly between the raws copy and
the session.md copy (#155, 非阻擋 1).
"""

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

remote = Path(os.environ["FAKE_REMOTE"])
log = remote.parent / "calls.log"


def fail(msg, rc=1):
    print(msg, file=sys.stderr)
    sys.exit(rc)


if os.environ.get("FAKE_RCLONE_DELAY"):     # let a test watch a slow upload happen
    import time
    # FAKE_RCLONE_DELAYED=<path>: touch it the moment an UPLOAD - a copy/copyto/move
    # whose destination is the remote (push_outbox batches `copy <outbox>
    # gdrive:sessions`, downloads run the other way and never mark) - starts
    # sleeping, so a caller can tell "an upload is verifiably in flight" without
    # racing a sleep.
    _marker = os.environ.get("FAKE_RCLONE_DELAYED")
    _a = sys.argv[1:]
    if _marker:
        for _i, _t in enumerate(_a[:-2]):
            if _t in ("copy", "copyto", "move") and _a[_i + 2].startswith("gdrive:"):
                try:
                    Path(_marker).touch()
                except OSError:
                    pass
                break
    time.sleep(float(os.environ["FAKE_RCLONE_DELAY"]))

args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")


def stdin_kind():
    """What fd 0 is, for FAKE_RCLONE_RECORD_STDIN: the point is that it is never the
    terminal the agent is using, and never something that could be read from."""
    try:
        if os.path.samefile(0, "/dev/null"):
            return "/dev/null"
        return "fifo" if stat.S_ISFIFO(os.fstat(0).st_mode) else "other"
    except OSError:
        return "closed"


if os.environ.get("FAKE_RCLONE_RECORD_STDIN"):
    with open(remote.parent / "stdin.log", "a") as f:
        f.write(json.dumps({"stdin": stdin_kind(), "args": args}) + "\n")


def _should_fail(needle: str) -> bool:
    if needle in " ".join(args):
        return True
    # The batch uploader sends with `copy --files-from`, where the names are in the
    # listing and not in the arguments. A test that breaks "session.md" or "raw-" means
    # the same thing here, so the listing is read before deciding - and a needle that
    # names the old per-file command still means "break this send".
    if "--files-from" in args:
        listing = Path(args[args.index("--files-from") + 1])
        try:
            if needle in listing.read_text(encoding="utf-8"):
                return True
        except OSError:
            pass
    return needle == "copyto" and "copy" in args


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
    if args[i].startswith("--ask-password="):     # a global flag the real rclone takes; not a command
        i += 1
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
        # #152: a real rclone exits 3 for a folder that is not there. The default
        # stays rc=1 + the words older tests match; with the switch the message says
        # "object not found", so only the returncode tells it from a failed look.
        if os.environ.get("FAKE_RCLONE_NOT_FOUND_RC") == "3":
            fail("error listing: object not found", rc=3)
        fail("error listing: directory not found")
    recursive = "-R" in params
    # #152: a whole listing that silently leaves a folder out, while the folder itself
    # is still there - the Drive error the directed look exists for. Comma-separated
    # names, and only in the recursive listing: `lsjson gdrive:sessions/<ulid>` still
    # sees the folder, which is what makes it a directed look.
    omit = {name for name in os.environ.get("FAKE_RCLONE_OMIT", "").split(",") if name}
    # #155: files the whole listing leaves out while the folder's own listing sees
    # them - a listing that lost a file, not a folder. Only recursive listings.
    omit_files = {name for name in os.environ.get("FAKE_RCLONE_OMIT_FILES", "").split(",") if name}
    if (omit or omit_files) and recursive:
        # FAKE_RCLONE_OMIT_FROM=N: leave them out only from the Nth whole listing on, so
        # a test can break the second (re-check) listing and leave the first intact.
        nth = int(os.environ.get("FAKE_RCLONE_OMIT_FROM", "1"))
        files_nth = int(os.environ.get("FAKE_RCLONE_OMIT_FILES_FROM", "1"))
        counter = remote.parent / "listings.count"
        calls = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(calls))
        if calls < nth:
            omit = set()
        if calls < files_nth:
            omit_files = set()
    out = []
    items = base.rglob("*") if recursive else base.iterdir()
    for p in sorted(items):
        rel = p.relative_to(base)
        if omit and rel.parts[0] in omit:
            continue
        if recursive and omit_files and p.is_file() and rel.name in omit_files:
            continue
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


def clean_after():
    """Another machine's ordinary cleanup, placed exactly between two calls.

    It deletes the old raws it replaced and puts its own new raw in, while our
    uploader is between its raws copy and its session.md copy (#155, 非阻擋 1).
    """
    trigger = os.environ.get("FAKE_RCLONE_CLEAN_AFTER")
    if not trigger:
        return
    try:
        spec = json.loads(Path(trigger).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not _should_fail(spec["after"]):
        return
    Path(trigger).unlink(missing_ok=True)   # one shot, before the work below
    folder = root / spec["folder"]
    for name in spec.get("remove", []):
        (folder / name).unlink(missing_ok=True)
    for name, text in spec.get("add", {}).items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")


def purge_after():
    """Another machine's delete, placed exactly between two calls (see the docstring)."""
    trigger = os.environ.get("FAKE_RCLONE_PURGE_AFTER")
    if not trigger:
        return
    try:
        spec = json.loads(Path(trigger).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not _should_fail(spec["after"]):
        return
    Path(trigger).unlink(missing_ok=True)   # one shot, before the work below
    shutil.rmtree(root / spec["purge"], ignore_errors=True)


clean_after()
purge_after()
snapshot()
