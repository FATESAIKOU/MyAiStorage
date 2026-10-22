#!/usr/bin/env python3
"""K.1 Drive probe: the seven measurements in `openspec/changes/agora-diff-upload/design.md`
(「K.1 探針腳本規格」, lines 764-781), for #109 / MyAiStorage#152.

What this script answers
  1. `copy --files-from` of 1 / 100 / 500 / 1000 tiny files (500 is the extra
     data point K1's table asks for; the detailed K.1 list names 1/100/1000).
  2. recursive `lsjson -R --fast-list --hash --files-only` over 1k / 2k / 5k files,
     and the same listing *without* `--files-only`: do directory entries carry an
     `ID` (K17)?
  3. `delete --files-from` of the 1000 files the copy test just sent.
  4. upload then list immediately: does the md5 show up, how long does it take, how
     often does it fail (K2)? `ModTime` against the local `stat` (K13); with
     `--recheck-minutes 60` the same files are listed again an hour later.
  5. 100 MiB as one file vs as 25 x 4 MiB chunks: download and reassemble.
  6. purge a folder, recreate it under the same name (three times with `mkdir`, once
     with the one-file `copy` agora really uses), compare the two folder IDs (K17).
  7. directed `lsjson` on a folder that exists / does not exist: exit code and the
     shape of stderr, plus one injected network failure (#152's three states).

Safety (the ticket's rules, and the script does not bend them)
  * It never opens, reads, copies or prints `~/.config/agora/` - the path is only
    handed to rclone as `--config`. No `agora` module is imported, and no real
    session, cache or state directory is read.
  * The remote name and the flag shape come from `src/agora/store.py`'s `Drive`
    class: the executable is `AGORA_RCLONE` or `rclone` (line 203), every call gets
    `--config <config>/rclone.conf --ask-password=false` (line 210), and agora's own
    root folder is a separate `--drive-root-folder-id <id from config.json>` (line
    212, 227-247). This probe deliberately does *not* use that id: it creates
    `gdrive:agora-probe-k1-<8 random hex>` at the remote root and never writes under
    `agora/`. If the root refuses `mkdir`, it stops; it does not fall back. All
    `RCLONE_*` variables are removed from the child environment (otherwise
    `RCLONE_DRIVE_ROOT_FOLDER_ID` could move the "root" into agora's folder), and
    after creating the throwaway the remote root is listed once: a `sessions/`
    there means the root is not My Drive's root, and the probe stops.
  * All data is generated here (fixed fake bytes, fake names, a fake ULID). Nothing
    real is uploaded, and no real file names or file contents are printed.
  * `created` is set *before* `mkdir`, because a client timeout can leave a folder
    that really exists on Drive. A `finally` (and a SIGTERM handler that turns into
    the same exit path) purges the throwaway folder on success and on any failure,
    then looks for it again. SIGINT/SIGTERM are ignored while cleanup runs, so a
    second interrupt cannot cut the purge short. If the folder is still there, the
    name and the manual `rclone purge` line are printed loudly.
  * Every rclone call has a timeout. The first failed step jumps to cleanup.
  * Only stderr is kept, and only its first 200 characters after `safe_stderr()`
    blanks any line containing token / client_secret / refresh / password / secret /
    credential / bearer / authorization / ya29 / 1//.

Time and Drive use (estimate, to be replaced by the run's own numbers)
  * `--answers`: only ② (recursive listing without `--files-only`), ④ (`ModTime`
    with three different local mtimes), ⑥ (purge + same-name recreate, three
    `mkdir` rounds and one `copy` round), and ⑦ (directed `lsjson`: exists /
    missing / empty / network down). It does not run ①/③/⑤, builds no 1000-file
    set and no 100 MiB file. About 2-4 minutes, ~15 tiny files and < 1 MiB on
    Drive. This is the mode that answers the four questions R2/R3 are waiting on.
  * `--quick`: one repetition of 1-4 at reduced N, no 5/6/7 and no 100 MiB files.
    About 1-2 minutes, roughly 110 tiny files and < 1 MiB on Drive.
  * `--full`: 5 repetitions of 1-3, 20 rounds of 4, 3 rounds of 5, and 3 + 1
    folder-ID rounds. About 30-60 minutes on a home uplink: ~15.1k tiny files (the per-file
    round trip dominates; one rclone process is ~3.5 s by `docs/perf.md`) and 200 MiB
    of big files. Peak Drive use is ~270 MiB and ~15k files, all under the throwaway
    folder and all purged at the end. `--recheck-minutes 60` adds 60 minutes.
  * The 100 MiB download test moves 600 MiB down (3 reps x 2 shapes).

Run (one of --answers / --quick / --full is required; no flag exits 2 before
any rclone call, and --full must not run without the owner's consent)
  python3 experiments/agora-chunks/probe_drive.py --answers
  python3 experiments/agora-chunks/probe_drive.py --quick
  python3 experiments/agora-chunks/probe_drive.py --full
  python3 experiments/agora-chunks/probe_drive.py --full --recheck-minutes 60

Self-test without Drive (the fake is not executable, so pass it through python):
  FAKE_REMOTE=/tmp/k1-fake/remote FAKE_RCLONE_NOT_FOUND_RC=3 \
    python3 experiments/agora-chunks/probe_drive.py --quick \
    --rclone "python3 tests/fakes/fake_rclone.py" \
    --config /tmp/k1-fake/rclone.conf --json-out /tmp/k1-fake.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIB = 1024 * 1024
SMALL_BYTES = 4096
BIG_BYTES = 100 * MIB
CHUNK_BYTES = 4 * MIB
CHUNK_COUNT = BIG_BYTES // CHUNK_BYTES          # 25, K.1 item 5
DEFAULT_CONFIG = str(Path.home() / ".config" / "agora" / "rclone.conf")
DEFAULT_REMOTE = "gdrive"                       # store.py: `gdrive:sessions/...`
PREFIX = "agora-probe-k1-"
THROW_AWAY_RE = re.compile(r"^agora-probe-k1-[0-9a-f]{8}$")
SECRET_WORDS = ("token", "client_secret", "refresh", "password", "secret",
                "credential", "bearer", "authorization", "ya29", "1//")
MODTIME_TOLERANCE_S = 120.0                     # three mtimes are 1 h apart
BLOCK = bytes(range(256)) * 4096                # 1 MiB of fake bytes


# ---------------------------------------------------------------------------
# rclone calls: one place that builds the command, times it and filters stderr.
# ---------------------------------------------------------------------------


class ProbeError(RuntimeError):
    def __init__(self, message: str, *, rc: int | None = None, stderr: str = ""):
        super().__init__(message)
        self.rc = rc
        self.stderr = stderr


@dataclass
class Call:
    rc: int
    stdout: str
    stderr: str
    seconds: float


def safe_stderr(text: str, config: str, limit: int = 200) -> str:
    """The first `limit` characters of stderr, with secret-looking lines removed.

    The ticket names token / client_secret / refresh; password and secret are the
    same shape and are removed too. The config path is replaced with `<config>` so a
    path cannot travel into the report by accident.
    """
    text = (text or "").replace(config, "<config>")
    kept: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(word in low for word in SECRET_WORDS):
            kept.append("[redacted line]")
        else:
            kept.append(line.strip())
    flat = " / ".join(part for part in kept if part)
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", flat)
    flat = re.sub(r"\s+", " ", flat).strip()
    return flat[:limit]


class Rclone:
    def __init__(self, command: str, config: str, timeout: float):
        self.argv = shlex.split(command)
        if not self.argv:
            raise ProbeError("--rclone is empty")
        self.config = config
        self.timeout = timeout

    def _cmd(self, args: list[str]) -> list[str]:
        # The same global flags store.py's Drive._run puts in front of every command.
        return [*self.argv, "--config", self.config, "--ask-password=false", *args]

    def call(self, args: list[str], *, timeout: float | None = None,
             env_extra: dict[str, str] | None = None) -> Call:
        # rclone reads RCLONE_* variables. RCLONE_DRIVE_ROOT_FOLDER_ID would move
        # the "root" to another folder (agora's), so none of them reach the child.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RCLONE_")}
        if env_extra:
            env.update(env_extra)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                self._cmd(args),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,      # never let rclone wait for a prompt
                timeout=timeout if timeout is not None else self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise ProbeError(
                f"rclone {args[0]} timed out after {timeout or self.timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise ProbeError(f"cannot run rclone ({self.argv[0]}): {exc}") from None
        return Call(proc.returncode, proc.stdout or "", proc.stderr or "",
                    time.perf_counter() - started)

    def run(self, args: list[str], **kwargs) -> Call:
        call = self.call(args, **kwargs)
        if call.rc != 0:
            raise ProbeError(f"rclone {args[0]} failed (rc={call.rc})",
                             rc=call.rc, stderr=call.stderr)
        return call


def parse_listing(text: str) -> list[dict]:
    try:
        data = json.loads(text or "[]")
    except ValueError as exc:
        raise ProbeError(f"rclone returned unparseable JSON: {exc}") from None
    if not isinstance(data, list):
        raise ProbeError("rclone listing was not a JSON array")
    return data


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def summary(runs: list[dict]) -> dict:
    seconds = [run["seconds"] for run in runs]
    return {
        "reps": len(runs),
        "runs_s": [round(value, 3) for value in seconds],
        "median_s": round(median(seconds), 3) if seconds else None,
        "min_s": round(min(seconds), 3) if seconds else None,
        "max_s": round(max(seconds), 3) if seconds else None,
    }


# ---------------------------------------------------------------------------
# Local fake data. Nothing is read from disk except files written here.
# ---------------------------------------------------------------------------


def write_big(path: Path, blocks: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as out:
        for _ in range(blocks):
            out.write(BLOCK)


def md5_path(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_local(base: Path, quick: bool, answers: bool = False) -> dict:
    """Write every fake file the probe needs and return the paths/counts."""
    small_dir = base / "small"
    small_dir.mkdir(parents=True)
    small_count = 5 if answers else (100 if quick else 5000)
    names: list[str] = []
    md5s: dict[str, str] = {}
    for i in range(small_count):
        name = f"f{i:05d}.bin"
        payload = (f"agora-probe-k1 fake small file {i:05d}\n".encode() * 200)
        payload = payload[:SMALL_BYTES].ljust(SMALL_BYTES, b".")
        path = small_dir / name
        path.write_bytes(payload)
        names.append(name)
        md5s[name] = hashlib.md5(payload).hexdigest()

    copy_one = base / "copy-one"
    copy_one.mkdir()
    (copy_one / "one.bin").write_bytes(
        (b"agora-probe-k1 copy rebuild\n" * 200)[:SMALL_BYTES])

    modtime_dir = base / "modtime"
    modtime_dir.mkdir()
    now = time.time()
    modtime_files = [
        ("m-past.bin", 946684800.0),      # 2000-01-01: "local file time" is obvious
        ("m-hour.bin", now - 3600.0),     # exactly one hour before the upload
        ("m-now.bin", now),               # about the upload time
    ]
    for name, mtime in modtime_files:
        path = modtime_dir / name
        path.write_bytes((name.encode() + b"\n") * 256)
        os.utime(path, (mtime, mtime))

    data = {
        "small_dir": str(small_dir),
        "names": names,
        "md5s": md5s,
        "small_count": small_count,
        "copy_one_dir": str(copy_one),
        "modtime_dir": str(modtime_dir),
        "modtime_files": modtime_files,
        "big": None,
    }

    if not quick and not answers:
        big_dir = base / "big"
        single = big_dir / "single.bin"
        write_big(single, BIG_BYTES // MIB)
        chunks = big_dir / "chunks"
        chunks.mkdir(parents=True)
        for i in range(CHUNK_COUNT):
            write_big(chunks / f"seg-{i + 1:04d}.part", CHUNK_BYTES // MIB)
        data["big"] = {
            "single": str(single),
            "chunks": str(chunks),
            "single_md5": md5_path(single),
            "chunks_md5": md5_path(single),     # chunks are the same bytes, in order
        }
    return data


def files_from(list_dir: Path, tag: str, names: list[str]) -> Path:
    path = list_dir / f"{tag}.txt"
    path.write_text("\n".join(names) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The probe itself. Each function appends to `results` and prints counts only.
# ---------------------------------------------------------------------------


def make_throwaway(rc: Rclone, remote: str, name: str) -> str:
    """Create `remote:name` at the remote root and prove it is there.

    The caller sets its `created` flag *before* this runs: the mkdir may have
    completed on Drive even when the client times out, so cleanup must always try.
    """
    root = f"{remote}:{name}"
    # A short --call-timeout is also the mkdir timeout (used by the self-test);
    # the default 1800s is capped at 120s, which is plenty for a mkdir.
    timeout = min(120.0, rc.timeout)
    call = rc.call(["mkdir", root], timeout=timeout)
    if call.rc != 0:
        raise ProbeError(
            f"cannot create {root} at the remote root (rc={call.rc}); "
            "stopping instead of falling back to agora/",
            rc=call.rc, stderr=call.stderr)
    check = rc.call(["lsjson", root, "--dirs-only"], timeout=timeout)
    if check.rc != 0:
        raise ProbeError(f"created {root} but cannot list it (rc={check.rc})",
                         rc=check.rc, stderr=check.stderr)

    # A remote whose "root" is already agora/ would list `sessions` directly.
    # My Drive's root has `agora/` (and this throwaway), not `sessions/`.
    look = rc.call(["lsjson", f"{remote}:", "--dirs-only"], timeout=timeout)
    if look.rc != 0:
        raise ProbeError(
            f"cannot list {remote}: to verify the root before using it "
            f"(rc={look.rc}); stopping",
            rc=look.rc, stderr=look.stderr)
    entries = parse_listing(look.stdout)
    if any(entry.get("IsDir") and entry.get("Name") == "sessions"
           for entry in entries):
        raise ProbeError(
            f"{remote}: looks like the agora folder (it lists sessions/); "
            f"refusing to write there; stopping")
    print(f"  root check: {len(entries)} dirs at {remote}:, no sessions/")
    return root


def copy_batch(rc: Rclone, source: Path, target: str, names: list[str],
               list_dir: Path, tag: str) -> Call:
    listing = files_from(list_dir, tag, names)
    # The same two flags store.py's `_copy_batch` adds: do not traverse the
    # destination, and send the files even when a same-size copy is already there.
    return rc.run(["copy", str(source), target, "--files-from", str(listing),
                   "--no-traverse", "--ignore-times"])


def setup_remote(rc: Rclone, data: dict, root: str, list_dir: Path,
                 quick: bool, results: dict) -> None:
    """Put the two long-lived sets on Drive: sessions/<ULID>/ and listing5k/."""
    started = time.perf_counter()
    ulid = "01J0K1PROBE000000000000000"        # 26 chars, Crockford base32 alphabet
    session_names = data["names"][:(10 if quick else 2000)]
    copy_batch(rc, Path(data["small_dir"]), f"{root}/sessions/{ulid}",
               session_names, list_dir, "setup-sessions")
    results["sessions_upload_s"] = round(time.perf_counter() - started, 3)
    results["sessions_files"] = len(session_names)
    results["ulid"] = ulid

    if not quick:
        started = time.perf_counter()
        copy_batch(rc, Path(data["small_dir"]), f"{root}/listing5k",
                   data["names"][:5000], list_dir, "setup-listing5k")
        results["listing5k_upload_s"] = round(time.perf_counter() - started, 3)
        results["listing5k_files"] = 5000

    rc.run(["mkdir", f"{root}/probe/empty"], timeout=120)


def setup_minimal(rc: Rclone, data: dict, root: str, list_dir: Path,
                  results: dict) -> None:
    """The tiny setup `--answers` needs: five files and one empty folder."""
    ulid = "01J0K1PROBE000000000000000"
    copy_batch(rc, Path(data["small_dir"]), f"{root}/sessions/{ulid}",
               data["names"][:5], list_dir, "setup-answers")
    results["sessions_files"] = 5
    results["ulid"] = ulid
    rc.run(["mkdir", f"{root}/probe/empty"], timeout=120)


def item2_answers(rc: Rclone, root: str, reps: int) -> dict:
    """K17 part 1 only: recursive listing, no `--files-only`; do dirs carry ID?"""
    runs = []
    last: list[dict] = []
    for _ in range(reps):
        call = rc.run(["lsjson", f"{root}/sessions", "-R", "--fast-list",
                       "--hash"])
        runs.append({"seconds": call.seconds})
        last = parse_listing(call.stdout)
    dirs = [entry for entry in last if entry.get("IsDir")]
    with_id = [entry for entry in dirs if entry.get("ID")]
    entry = summary(runs)
    entry.update({
        "entries": len(last),
        "files": len(last) - len(dirs),
        "bytes": sum(e.get("Size", 0) for e in last if not e.get("IsDir")),
        "dir_entries": len(dirs),
        "dirs_with_id": len(with_id),
        "dirs_without_id": len(dirs) - len(with_id),
        "all_dirs_have_id": bool(dirs) and len(with_id) == len(dirs),
    })
    print(f"  lsjson -R (no --files-only): {len(dirs)} dir entries, "
          f"{len(with_id)} with ID; {entry['files']} files; median "
          f"{entry['median_s']:.3f}s")
    return entry


def item4_modtime(rc: Rclone, data: dict, root: str, rounds: int) -> dict:
    """K13 only: is Drive's ModTime the local file time or the upload time?"""
    mod_dir = Path(data["modtime_dir"])
    out: dict = {"rounds": [], "verdict": "not_computed"}
    for rep in range(rounds):
        now = time.time()
        data["modtime_files"] = [
            ("m-past.bin", 946684800.0),
            ("m-hour.bin", now - 3600.0),
            ("m-now.bin", now),
        ]
        for name, mtime in data["modtime_files"]:
            os.utime(mod_dir / name, (mtime, mtime))
        target = f"{root}/modtime/r{rep}"
        upload = rc.run(["copy", str(mod_dir), target])
        upload_end = time.time()
        listing = rc.run(["lsjson", target, "--hash", "--files-only"])
        rows = modtime_rows(parse_listing(listing.stdout), data, upload_end)
        local_close = bool(rows) and all(
            row["delta_local_s"] is not None
            and abs(row["delta_local_s"]) < MODTIME_TOLERANCE_S
            for row in rows)
        upload_close = bool(rows) and all(
            row["delta_upload_end_s"] is not None
            and abs(row["delta_upload_end_s"]) < MODTIME_TOLERANCE_S
            for row in rows)
        no_modtime = (not rows or any(
            not row["drive_modtime"] or row["delta_local_s"] is None
            or row["delta_upload_end_s"] is None for row in rows))
        if no_modtime:
            verdict = "no_modtime"
        elif local_close:
            verdict = "local_mtime"
        elif upload_close:
            verdict = "upload_time"
        else:
            verdict = "neither"
        out["rounds"].append({
            "round": rep,
            "upload_s": round(upload.seconds, 3),
            "listing_s": round(listing.seconds, 3),
            "files": rows,
            "verdict": verdict,
        })
        print(f"  ModTime round {rep}: {verdict}")
        print_modtime("ModTime", rows)
    verdicts = [row["verdict"] for row in out["rounds"]]
    if verdicts and all(value == verdicts[0] for value in verdicts):
        out["verdict"] = verdicts[0]
    else:
        out["verdict"] = "mixed"
    return out


def item1_copy(rc: Rclone, data: dict, root: str, list_dir: Path,
               reps: int, quick: bool) -> dict:
    sizes = [1, 100] if quick else [1, 100, 500, 1000]
    out: dict = {}
    for count in sizes:
        runs = []
        for rep in range(reps):
            target = f"{root}/send/n{count}/r{rep}"
            call = copy_batch(rc, Path(data["small_dir"]), target,
                              data["names"][:count], list_dir,
                              f"copy-{count}-{rep}")
            runs.append({"seconds": call.seconds})
        entry = summary(runs)
        entry.update({"files": count, "bytes": count * SMALL_BYTES})
        out[str(count)] = entry
        print(f"  copy --files-from {count:4d}: median {entry['median_s']:.3f}s "
              f"({entry['reps']} reps, {count * SMALL_BYTES} bytes)")
    return out


def item2_listing(rc: Rclone, root: str, reps: int, quick: bool) -> dict:
    out: dict = {}
    one_k = f"{root}/send/n{'100' if quick else '1000'}/r0"
    runs = []
    last: list[dict] = []
    for _ in range(reps):
        call = rc.run(["lsjson", one_k, "-R", "--fast-list", "--hash",
                       "--files-only"])
        runs.append({"seconds": call.seconds})
        last = parse_listing(call.stdout)
    entry = summary(runs)
    entry["files"] = len(last)
    entry["bytes"] = sum(e.get("Size", 0) for e in last if not e.get("IsDir"))
    out["files_1k" if not quick else "files_100"] = entry
    print(f"  lsjson --files-only {len(last):5d} files: median "
          f"{entry['median_s']:.3f}s ({reps} reps)")

    if not quick:
        for label, rel in (("files_2k", "sessions"),
                           ("files_5k", "listing5k")):
            runs = []
            last = []
            for _ in range(reps):
                call = rc.run(["lsjson", f"{root}/{rel}", "-R", "--fast-list",
                               "--hash", "--files-only"])
                runs.append({"seconds": call.seconds})
                last = parse_listing(call.stdout)
            entry = summary(runs)
            entry["files"] = len(last)
            entry["bytes"] = sum(e.get("Size", 0) for e in last
                                 if not e.get("IsDir"))
            out[label] = entry
            print(f"  lsjson --files-only {len(last):5d} files: median "
                  f"{entry['median_s']:.3f}s ({reps} reps)")

    # K17 part 1: the same recursive listing without --files-only. Directory
    # entries must be present for their IDs to be usable at all.
    runs = []
    last = []
    for _ in range(reps):
        call = rc.run(["lsjson", f"{root}/sessions", "-R", "--fast-list",
                       "--hash"])
        runs.append({"seconds": call.seconds})
        last = parse_listing(call.stdout)
    dirs = [entry for entry in last if entry.get("IsDir")]
    with_id = [entry for entry in dirs if entry.get("ID")]
    entry = summary(runs)
    entry.update({
        "entries": len(last),
        "files": len(last) - len(dirs),
        "bytes": sum(e.get("Size", 0) for e in last if not e.get("IsDir")),
        "dir_entries": len(dirs),
        "dirs_with_id": len(with_id),
        "dirs_without_id": len(dirs) - len(with_id),
        "all_dirs_have_id": bool(dirs) and len(with_id) == len(dirs),
    })
    out["recursive_no_files_only"] = entry
    print(f"  lsjson -R (no --files-only): {len(dirs)} dir entries, "
          f"{len(with_id)} with ID; {entry['files']} files; median "
          f"{entry['median_s']:.3f}s")
    return out


def item3_delete(rc: Rclone, data: dict, root: str, list_dir: Path,
                 reps: int, quick: bool) -> dict:
    count = 100 if quick else 1000
    runs = []
    verified = None
    for rep in range(reps):
        target = f"{root}/send/n{count}/r{rep}"
        listing = files_from(list_dir, f"delete-{count}-{rep}",
                             data["names"][:count])
        call = rc.run(["delete", target, "--files-from", str(listing)])
        runs.append({"seconds": call.seconds})
        if rep == 0:
            left = rc.run(["lsjson", target, "--files-only"])
            verified = len(parse_listing(left.stdout)) == 0
    entry = summary(runs)
    entry.update({"files": count, "bytes": count * SMALL_BYTES,
                  "first_target_empty_after": verified})
    print(f"  delete --files-from {count:4d}: median {entry['median_s']:.3f}s "
          f"({reps} reps, first target empty={verified})")
    return entry


def item4_consistency(rc: Rclone, data: dict, root: str, list_dir: Path,
                      rounds: int, wait_s: float,
                      recheck_minutes: int) -> dict:
    out: dict = {"rounds": [], "wait_s": wait_s}
    immediate_missing = 0
    never = 0
    appeared: list[float] = []
    per_round = 5
    for rep in range(rounds):
        start = rep * per_round
        names = data["names"][start:start + per_round]
        target = f"{root}/consistency/r{rep}"
        copy = copy_batch(rc, Path(data["small_dir"]), target, names,
                          list_dir, f"consistency-{rep}")
        finished = time.time()
        look = rc.run(["lsjson", target, "--hash", "--files-only"])
        entries = {entry.get("Name"): (entry.get("Hashes") or {}).get("md5", "")
                   for entry in parse_listing(look.stdout)}
        missing = [name for name in names
                   if entries.get(name) != data["md5s"][name]]
        row = {
            "round": rep,
            "copy_s": round(copy.seconds, 3),
            "listing_s": round(look.seconds, 3),
            "immediate_missing": len(missing),
        }
        if missing:
            immediate_missing += 1
            found_at = None
            deadline = time.time() + wait_s
            while time.time() < deadline:
                time.sleep(0.5)
                look2 = rc.run(["lsjson", target, "--hash", "--files-only"])
                entries2 = {entry.get("Name"):
                            (entry.get("Hashes") or {}).get("md5", "")
                            for entry in parse_listing(look2.stdout)}
                if all(entries2.get(name) == data["md5s"][name]
                       for name in names):
                    found_at = time.time() - finished
                    break
            row["appeared_after_s"] = round(found_at, 3) if found_at else None
            if found_at is None:
                never += 1
            else:
                appeared.append(found_at)
        out["rounds"].append(row)

    out.update({
        "rounds_run": rounds,
        "immediate_missing_rounds": immediate_missing,
        "never_appeared_rounds": never,
        "appeared_after_s": [round(value, 3) for value in appeared],
        "median_appeared_after_s": (round(median(appeared), 3)
                                    if appeared else None),
    })
    if rounds:
        print(f"  upload -> immediate listing: {rounds} rounds, "
              f"{immediate_missing} missing at once, {never} never appeared"
              + (f", median appear {out['median_appeared_after_s']:.3f}s"
                 if appeared else ""))

    # K13: one fixed past mtime, one an hour old, one ~now. If Drive's ModTime
    # tracks the local file, the past file stays in 2000; if it tracks the upload,
    # all three land near `upload_end`. Reset the two relative mtimes right before
    # the upload so the "upload time" and "local time" answers are far apart.
    mod_dir = Path(data["modtime_dir"])
    now = time.time()
    data["modtime_files"] = [
        ("m-past.bin", 946684800.0),
        ("m-hour.bin", now - 3600.0),
        ("m-now.bin", now),
    ]
    for name, mtime in data["modtime_files"]:
        os.utime(mod_dir / name, (mtime, mtime))
    upload = rc.run(["copy", str(mod_dir), f"{root}/modtime"])
    upload_end = time.time()
    first = rc.run(["lsjson", f"{root}/modtime", "--hash", "--files-only"])
    rows = modtime_rows(parse_listing(first.stdout), data, upload_end)
    out["modtime"] = {
        "upload_s": round(upload.seconds, 3),
        "upload_end_epoch": round(upload_end, 3),
        "files": rows,
        "recheck": {"skipped": True, "minutes": 0},
    }
    print_modtime("ModTime", rows)

    if recheck_minutes > 0:
        print(f"  waiting {recheck_minutes} min for the ModTime recheck ...",
              flush=True)
        time.sleep(recheck_minutes * 60)
        second = rc.run(["lsjson", f"{root}/modtime", "--hash", "--files-only"])
        rows2 = modtime_rows(parse_listing(second.stdout), data, upload_end)
        unchanged = {
            row["name"]: row["drive_modtime"] == next(
                (old["drive_modtime"] for old in rows
                 if old["name"] == row["name"]), None)
            for row in rows2
        }
        out["modtime"]["recheck"] = {
            "skipped": False,
            "minutes": recheck_minutes,
            "files": rows2,
            "unchanged": unchanged,
        }
        print_modtime(f"ModTime after {recheck_minutes} min", rows2)

    return out


def modtime_rows(entries: list[dict], data: dict, upload_end: float) -> list[dict]:
    local = dict(data["modtime_files"])
    rows = []
    for entry in entries:
        name = entry.get("Name", "")
        raw = entry.get("ModTime", "")
        epoch = parse_modtime(raw)
        local_mtime = local.get(name)
        rows.append({
            "name": name,
            "drive_modtime": raw,
            "local_mtime_epoch": round(local_mtime, 3) if local_mtime else None,
            "delta_local_s": (round(epoch - local_mtime, 3)
                              if epoch is not None and local_mtime is not None
                              else None),
            "delta_upload_end_s": (round(epoch - upload_end, 3)
                                   if epoch is not None else None),
        })
    return rows


def parse_modtime(text: str) -> float | None:
    """Parse rclone's RFC3339 ModTime, including nanoseconds and an offset.

    `datetime.fromisoformat` only takes microseconds, so a run of more than six
    fractional digits is cut first (`2026-10-22T09:21:00.123456789Z`). A `Z` is
    spelled as `+00:00`; a value without an offset is treated as UTC.
    """
    if not text:
        return None
    value = text.strip()
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def print_modtime(label: str, rows: list[dict]) -> None:
    for row in rows:
        if row["drive_modtime"]:
            print(f"  {label} {row['name']}: Drive={row['drive_modtime']} "
                  f"delta_local={row['delta_local_s']}s "
                  f"delta_upload={row['delta_upload_end_s']}s")
        else:
            print(f"  {label} {row['name']}: Drive did not return ModTime")


def item5_big(rc: Rclone, data: dict, root: str, base: Path,
              reps: int) -> dict:
    big = data["big"]
    assert big, "item5 needs the big files"
    single = Path(big["single"])
    chunks = Path(big["chunks"])

    up_single = rc.run(["copyto", str(single), f"{root}/big/single.bin"],
                       timeout=3600)
    up_chunks = rc.run(["copy", str(chunks), f"{root}/big/chunks"],
                       timeout=3600)

    single_runs: list[dict] = []
    chunk_runs: list[dict] = []
    for rep in range(reps):
        target = base / "download" / f"r{rep}"
        target.mkdir(parents=True, exist_ok=True)

        one = target / "single.bin"
        call = rc.run(["copyto", f"{root}/big/single.bin", str(one)],
                      timeout=3600)
        single_runs.append({"seconds": call.seconds,
                            "md5_ok": md5_path(one) == big["single_md5"],
                            "bytes": one.stat().st_size})

        chunk_dir = target / "chunks"
        call = rc.run(["copy", f"{root}/big/chunks", str(chunk_dir)],
                      timeout=3600)
        download_s = call.seconds
        started = time.perf_counter()
        assembled = target / "assembled.bin"
        with open(assembled, "wb") as out:
            for part in sorted(chunk_dir.iterdir()):
                with open(part, "rb") as handle:
                    shutil.copyfileobj(handle, out, 1 << 20)
        reassemble_s = time.perf_counter() - started
        chunk_runs.append({
            "download_s": round(download_s, 3),
            "reassemble_s": round(reassemble_s, 3),
            "total_s": round(download_s + reassemble_s, 3),
            "md5_ok": md5_path(assembled) == big["chunks_md5"],
            "bytes": assembled.stat().st_size,
        })

    def report(runs: list[dict], key: str) -> dict:
        entry = summary([{"seconds": run[key]} for run in runs])
        entry.update({
            "all_md5_ok": all(run["md5_ok"] for run in runs),
            "bytes": runs[0]["bytes"] if runs else 0,
        })
        return entry

    out = {
        "bytes": BIG_BYTES,
        "chunks": CHUNK_COUNT,
        "chunk_bytes": CHUNK_BYTES,
        "upload_single_s": round(up_single.seconds, 3),
        "upload_chunks_s": round(up_chunks.seconds, 3),
        "single": report(single_runs, "seconds"),
        "chunks": report(chunk_runs, "download_s"),
        "chunks_total": report(chunk_runs, "total_s"),
        "chunks_reassemble_median_s": round(median(
            [run["reassemble_s"] for run in chunk_runs]) or 0.0, 3),
    }
    print(f"  100 MiB single: download median {out['single']['median_s']:.3f}s, "
          f"md5 ok={out['single']['all_md5_ok']}")
    print(f"  25 x 4 MiB: download median {out['chunks']['median_s']:.3f}s, "
          f"reassemble {out['chunks_reassemble_median_s']:.3f}s, "
          f"total {out['chunks_total']['median_s']:.3f}s, "
          f"md5 ok={out['chunks']['all_md5_ok']}")
    return out


def folder_id(rc: Rclone, parent: str, name: str) -> str:
    look = rc.run(["lsjson", parent, "--dirs-only"])
    matches = [entry for entry in parse_listing(look.stdout)
               if entry.get("Name") == name]
    if len(matches) != 1:
        raise ProbeError(f"expected one folder named {name!r} in {parent}, "
                         f"found {len(matches)}")
    return matches[0].get("ID", "")


def item6_folder_id(rc: Rclone, root: str, rounds: int,
                    copy_source: Path | None = None) -> list[dict]:
    """Compare folder IDs before/after purge, rebuilding with mkdir and with copy.

    agora rebuilds a purged session folder with `copy`, not `mkdir` (K17's
    assumption (b)), so one round recreates with a real one-file `copy` too.
    """
    out = []

    def record(label, rel_name: str, first: str, second: str,
               recreate_with: str) -> None:
        listing = rc.run(["lsjson", f"{root}/probe", "-R", "--fast-list",
                          "--hash"])
        entries = parse_listing(listing.stdout)
        from_listing = next(
            (entry.get("ID", "") for entry in entries
             if entry.get("IsDir") and entry.get("Path") == rel_name), "")
        files_after = sum(
            1 for entry in entries
            if not entry.get("IsDir")
            and entry.get("Path", "").startswith(rel_name + "/"))
        # Two missing IDs are not evidence that the ID changed.
        same = ("unknown" if not first or not second else first == second)
        row = {
            "round": label,
            "recreate_with": recreate_with,
            "first_id": first,
            "second_id": second,
            "same_after_purge_recreate": same,
            "listing_id": from_listing,
            "listing_matches_second": bool(second) and second == from_listing,
            "listing_has_id": bool(from_listing),
            "files_after_recreate": files_after,
        }
        out.append(row)
        print(f"  purge/recreate {label} ({recreate_with}): same ID={same} "
              f"first={first[:8] or '-'} second={second[:8] or '-'} "
              f"listing={from_listing[:8] or '-'} files={files_after}")

    for rep in range(rounds):
        rel = f"probe/p{rep}"
        rc.run(["mkdir", f"{root}/{rel}"], timeout=120)
        first = folder_id(rc, f"{root}/probe", f"p{rep}")
        rc.run(["purge", f"{root}/{rel}"], timeout=300)
        rc.run(["mkdir", f"{root}/{rel}"], timeout=120)
        second = folder_id(rc, f"{root}/probe", f"p{rep}")
        record(rep, f"p{rep}", first, second, "mkdir")

    if copy_source is not None:
        rel = "probe/copy"
        rc.run(["mkdir", f"{root}/{rel}"], timeout=120)
        first = folder_id(rc, f"{root}/probe", "copy")
        rc.run(["purge", f"{root}/{rel}"], timeout=300)
        # The actual agora path: copy a file into the old name and let rclone
        # create the folder again.
        rc.run(["copy", str(copy_source), f"{root}/{rel}"])
        second = folder_id(rc, f"{root}/probe", "copy")
        record("copy", "copy", first, second, "copy")
    return out


def item7_directed(rc: Rclone, root: str, ulid: str) -> dict:
    missing_ulid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    proxy = {
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "all_proxy": "http://127.0.0.1:9",
        "NO_PROXY": "",
        "no_proxy": "",
    }
    cases: list[dict] = []

    look_timeout = min(120.0, rc.timeout)

    def look(label: str, rel: str, extra: list[str] | None = None,
             env: dict[str, str] | None = None) -> None:
        args = ["lsjson", f"{root}/{rel}", *(extra or [])]
        try:
            call = rc.call(args, timeout=look_timeout, env_extra=env)
        except ProbeError as exc:
            # A timeout is an answer here ("not the directory-not-found shape"),
            # and the earlier cases must stay in the result.
            stderr = safe_stderr(exc.stderr, rc.config)
            cases.append({
                "label": label,
                "rc": exc.rc,
                "seconds": None,
                "entries": None,
                "verdict": "error",
                "stderr_200": stderr,
                "error": str(exc),
            })
            print(f"  {label}: error={exc} stderr={stderr or '-'}")
            return
        entries = None
        if call.rc == 0:
            entries = parse_listing(call.stdout)
        stderr = safe_stderr(call.stderr, rc.config)
        low = stderr.lower()
        if call.rc == 0:
            verdict = "present" if entries else "present_empty"
        elif call.rc == 3 or "directory not found" in low:
            verdict = "missing"
        else:
            verdict = "error"
        cases.append({
            "label": label,
            "rc": call.rc,
            "seconds": round(call.seconds, 3),
            "entries": len(entries) if entries is not None else None,
            "verdict": verdict,
            "stderr_200": stderr,
        })
        print(f"  {label}: rc={call.rc} entries="
              f"{len(entries) if entries is not None else '-'} "
              f"verdict={verdict} stderr={stderr or '-'}")

    look("existing", f"sessions/{ulid}")
    look("existing_dirs_only", f"sessions/{ulid}", ["--dirs-only"])
    look("empty_existing_dirs_only", "probe/empty", ["--dirs-only"])
    look("missing", f"sessions/{missing_ulid}")
    look("missing_dirs_only", f"sessions/{missing_ulid}", ["--dirs-only"])
    look("missing_recursive", f"sessions/{missing_ulid}", ["-R"])
    look("network_failure", f"sessions/{ulid}",
         ["--contimeout", "5s", "--timeout", "30s",
          "--retries", "1", "--low-level-retries", "1"], proxy)

    missing = next(case for case in cases if case["label"] == "missing")
    network = next(case for case in cases if case["label"] == "network_failure")
    separated = (missing["verdict"] == "missing"
                 and network["verdict"] == "error")
    return {
        "cases": cases,
        "three_state_separated": separated,
        "notes": [
            "wrong token and permission errors were deliberately not injected: "
            "both need changes to the credential, which this probe must not touch",
        ],
    }


# ---------------------------------------------------------------------------
# Cleanup and report.
# ---------------------------------------------------------------------------


def cleanup(rc: Rclone, root: str | None, name: str, created: bool,
            timeout: float) -> dict:
    out: dict = {"name": name, "purged": False, "verified_gone": False}
    if not created or not root:
        out["skipped"] = "throwaway folder was never created"
        print("  [cleanup] nothing to purge (folder was never created)")
        return out
    if not THROW_AWAY_RE.fullmatch(name):
        # A name this script did not generate is never purged, whatever happened.
        out["refused"] = "name does not match the probe's own pattern"
        print(f"  [cleanup] !! refusing to purge unexpected name {name!r}")
        return out
    try:
        purge = rc.call(["purge", root], timeout=timeout)
        out["purge_rc"] = purge.rc
        out["purge_stderr_200"] = safe_stderr(purge.stderr, rc.config)
        if purge.rc == 0:
            out["purged"] = True
        else:
            # purge on a folder another actor already removed is success for us.
            look = rc.call(["lsjson", root, "--dirs-only"], timeout=120)
            if look.rc != 0 and (look.rc == 3
                                 or "directory not found"
                                 in safe_stderr(look.stderr, rc.config).lower()):
                out["purged"] = True
                out["already_gone"] = True
    except ProbeError as exc:
        out["purge_error"] = str(exc)
        out["purge_stderr_200"] = safe_stderr(exc.stderr, rc.config)

    try:
        look = rc.call(["lsjson", root, "--dirs-only"], timeout=120)
        if look.rc != 0:
            stderr = safe_stderr(look.stderr, rc.config)
            if look.rc == 3 or "directory not found" in stderr.lower():
                out["verified_gone"] = True
            else:
                out["verify_stderr_200"] = stderr
        else:
            out["verify_entries"] = len(parse_listing(look.stdout))
    except ProbeError as exc:
        out["verify_error"] = str(exc)

    if out["verified_gone"]:
        print(f"  [cleanup] purged and verified gone: {name}")
    else:
        print(f"  [cleanup] !! THROWAWAY FOLDER STILL EXISTS: {root}")
        print("  [cleanup] !! remove it by hand, now:")
        print("  rclone --config ~/.config/agora/rclone.conf purge "
              f"{root}")
        out["needs_manual_cleanup"] = True
    return out


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def remote_name(value: str) -> str:
    """A bare remote name, never a path. Keeps `--remote` from pointing at agora/."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise argparse.ArgumentTypeError(
            "remote must be a bare name such as gdrive, not a path")
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="K.1 Google Drive probe (#109 / MyAiStorage#152). "
                    "Creates one throwaway folder at the remote root, runs fake "
                    "content only, and purges it at the end.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--answers", action="store_true",
                      help="only items 2, 4 (ModTime), 6 and 7 with the smallest "
                           "fake data set; ~2-4 minutes, <1 MiB on Drive")
    mode.add_argument("--quick", action="store_true",
                      help="1 rep of items 1-4, small files only, no 5/6/7; "
                           "~1-2 minutes")
    mode.add_argument("--full", action="store_true",
                      help="all seven measurements (~30-60 minutes, ~270 MiB, "
                           "~15k files); do not run without the owner's consent")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="rclone config path handed to --config; the script "
                             "never opens it (default: %(default)s)")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, type=remote_name,
                        help="rclone remote name from store.py (default: %(default)s)")
    parser.add_argument("--rclone", default=os.environ.get("AGORA_RCLONE", "rclone"),
                        help="rclone executable, or a quoted command such as "
                             "'python3 tests/fakes/fake_rclone.py' (default: %(default)s)")
    parser.add_argument("--json-out", default=None,
                        help="default: /tmp/agora-probe-k1-<timestamp>.json")
    parser.add_argument("--call-timeout", type=float, default=1800.0,
                        help="seconds per rclone call (default: %(default)s)")
    parser.add_argument("--recheck-minutes", type=int, default=0,
                        help="wait this long before the K13 ModTime recheck; "
                             "0 skips it (60 is the design's hour)")
    parser.add_argument("--keep-local", action="store_true",
                        help="keep the local fake-data folder for debugging")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rclone commands the chosen mode would run and exit; "
                             "nothing is run, created or read (no rclone, no network, no files)")
    return parser.parse_args(argv)


def dry_run_plan(mode: str, remote: str, rclone: str) -> list[str]:
    """The rclone commands `mode` runs, in order, as text. Pure: it touches nothing.

    `<cfg>` stands for the path given to --config and `<probe>` for the throwaway folder
    (`agora-probe-k1-<8 hex>`) the real run makes at the root of the remote."""
    base = f"{rclone} --config <cfg> --ask-password=false"
    root = f"{remote}:<probe>"
    lines = [f"{base} version",
             f"{base} mkdir {root}                                  # the throwaway folder",
             f"{base} lsjson {remote}: --dirs-only                  # check it exists and the remote root is My Drive"]
    if mode in ("answers", "quick", "full"):
        lines += [
            f"{base} copy --files-from <list> <local>/sessions {root}/sessions   # small fake files, 4 KiB each"]
    lines += [
        "# K17(2): the folder ID across a purge",
        f"{base} mkdir {root}/probe",
        f"{base} lsjson {root} --dirs-only                       # record ID #1",
        f"{base} purge {root}/probe",
        f"{base} mkdir {root}/probe",
        f"{base} lsjson {root} --dirs-only                       # record ID #2; compare with #1 (repeat 3x)",
        f"{base} lsjson {root} -R --hash                         # the directory entries carry an ID? (K17 1)",
        "# K13: ModTime of three small files with different local mtimes",
        f"{base} copy --files-from <list> <local>/modtime {root}/modtime",
        f"{base} lsjson {root}/modtime --hash                     # compare ModTime with the local stat",
        "# directed look at one session folder (#152, B.5.7)",
        f"{base} lsjson {root}/sessions/<fake ULID>              # exists: rc=0",
        f"{base} lsjson {root}/sessions/<missing ULID>           # missing: rc=3 + 'directory not found'",
    ]
    if mode in ("quick", "full"):
        lines += [f"{base} copy --files-from <list> <local>/send {root}/send       # 1/100/(500/1000) files",
                  f"{base} lsjson {root}/listing5k -R --fast-list --hash --files-only",
                  f"{base} delete --files-from <list> {root}/send"]
    if mode == "full":
        lines += [f"{base} copy <local>/big/one.bin {root}/big",
                  f"{base} copy --files-from <list> <local>/big/chunks {root}/big/chunks   # 25 x 4 MiB",
                  f"{base} copy {root}/big <download dir>            # one 100 MiB file vs 25 segments"]
    lines += [f"{base} purge {root}                                  # cleanup, always, then lsjson to verify it is gone",
              f"{base} lsjson {remote}: --dirs-only"]
    return lines


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.dry_run:
        mode = "answers" if args.answers else ("quick" if args.quick else "full")
        print(f"K.1 Drive probe ({mode} mode) - dry run: nothing is executed")
        for line in dry_run_plan(mode, args.remote, args.rclone):
            print("  " + line)
        return 0
    answers = args.answers
    quick = args.quick
    mode = "answers" if answers else ("quick" if quick else "full")
    reps = 1 if quick else 5
    big_reps = 1 if quick else 3
    consistency_rounds = 1 if quick else 20
    folder_rounds = 0 if quick else 3
    answers_reps = 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_out = Path(args.json_out or f"/tmp/agora-probe-k1-{stamp}.json")
    name = PREFIX + secrets.token_hex(4)
    root = f"{args.remote}:{name}"
    results: dict = {
        "schema": 1,
        "probe": "K.1 Drive probe",
        "started_at": utc_now(),
        "mode": mode,
        "python": sys.version.split()[0],
        "rclone_command": args.rclone,
        "rclone_version": None,
        "remote": args.remote,
        "throwaway": name,
        "throwaway_path": root,
        "config_arg": "<path passed via --config; never read>",
        "measurements": {},
        "not_tested": [
            "wrong token: needs a credential change",
            "permission error: needs a credential or sharing change",
            "real sessions: the probe never reads real data",
        ],
        "cleanup": None,
    }
    rc = Rclone(args.rclone, args.config, args.call_timeout)
    workspace = Path(tempfile.mkdtemp(prefix="agora-probe-k1-local-"))
    list_dir = workspace / "lists"
    list_dir.mkdir()
    created = False
    exit_code = 0

    def on_term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, on_term)

    print(f"K.1 Drive probe ({results['mode']} mode)")
    if answers and args.recheck_minutes:
        print("  (--answers ignores --recheck-minutes: it does not wait an hour)")
    print(f"  throwaway: {root}")
    print("  config: <path only; contents are never read>")
    try:
        version = rc.call(["version"], timeout=60)
        if version.rc == 0:
            first = (version.stdout or "").splitlines()
            results["rclone_version"] = first[0] if first else "unknown"
        else:
            results["rclone_version"] = f"unknown (rc={version.rc})"
            results["rclone_version_stderr_200"] = safe_stderr(
                version.stderr, args.config)
        print(f"  rclone: {results['rclone_version']}")

        print("  generating fake data locally ...")
        data = make_local(workspace, quick, answers)
        has_big = not quick and not answers
        results["data"] = {
            "small_files": data["small_count"],
            "small_bytes": data["small_count"] * SMALL_BYTES,
            "big_bytes": BIG_BYTES if has_big else 0,
            "big_shapes": [BIG_BYTES, CHUNK_COUNT] if has_big else [],
        }

        # B2: mkdir can complete on Drive while the client times out, and the
        # next lsjson can fail too. Cleanup must try even if make_throwaway raises.
        created = True
        root = make_throwaway(rc, args.remote, name)

        if answers:
            print("  [setup] uploading the minimal set (5 files + one empty folder)")
            setup_minimal(rc, data, root, list_dir, results["measurements"])
            results["measurements"]["item2_answers"] = item2_answers(
                rc, root, answers_reps)
            print("[4] ModTime: local file time or upload time?")
            results["measurements"]["item4_modtime"] = item4_modtime(
                rc, data, root, answers_reps)
            print("[6] folder ID after purge + same-name recreate")
            results["measurements"]["item6_folder_id"] = item6_folder_id(
                rc, root, answers_reps,
                copy_source=Path(data["copy_one_dir"]))
            print("[7] directed lsjson: existing / missing / empty / network down")
            results["measurements"]["item7_directed"] = item7_directed(
                rc, root, results["measurements"]["ulid"])
        else:
            print("  [setup] uploading the two long-lived sets ...")
            setup_remote(rc, data, root, list_dir, quick, results["measurements"])

            print("[1] copy --files-from")
            results["measurements"]["item1_copy_files_from"] = item1_copy(
                rc, data, root, list_dir, reps, quick)

            print("[2] recursive listing (files-only, then without it)")
            results["measurements"]["item2_listing"] = item2_listing(
                rc, root, reps, quick)

            print("[3] delete --files-from")
            results["measurements"]["item3_delete_files_from"] = item3_delete(
                rc, data, root, list_dir, reps, quick)

            print("[4] upload -> immediate listing, and ModTime")
            results["measurements"]["item4_consistency"] = item4_consistency(
                rc, data, root, list_dir, consistency_rounds,
                wait_s=15.0,
                recheck_minutes=(args.recheck_minutes if not quick else 0))

            if quick:
                print("[5] skipped in --quick (100 MiB shapes)")
                print("[6] skipped in --quick (folder ID rebuild)")
                print("[7] skipped in --quick (directed look error shapes)")
            else:
                print("[5] 100 MiB single vs 25 x 4 MiB")
                results["measurements"]["item5_big"] = item5_big(
                    rc, data, root, workspace, big_reps)

                print("[6] folder ID after purge + same-name recreate")
                results["measurements"]["item6_folder_id"] = item6_folder_id(
                    rc, root, folder_rounds,
                    copy_source=Path(data["copy_one_dir"]))

                print("[7] directed lsjson: existing / missing / network down")
                results["measurements"]["item7_directed"] = item7_directed(
                    rc, root, results["measurements"]["ulid"])

    except KeyboardInterrupt:
        results["error"] = "interrupted"
        exit_code = 130
        print("\n  interrupted; cleaning up")
    except ProbeError as exc:
        results["error"] = str(exc)
        results["error_stderr_200"] = safe_stderr(exc.stderr, args.config)
        exit_code = 1
        print(f"\n  probe failed: {exc}")
        if exc.stderr:
            print(f"  stderr: {safe_stderr(exc.stderr, args.config)}")
    except Exception as exc:            # noqa: BLE001 - the report must survive anything
        results["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        print(f"\n  unexpected failure: {type(exc).__name__}: {exc}")
    finally:
        # A second Ctrl-C / SIGTERM must not interrupt the purge and its check.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        print("[cleanup]")
        results["cleanup"] = cleanup(rc, root, name, created,
                                     args.call_timeout)
        if not results["cleanup"].get("verified_gone") and created:
            exit_code = 3
        results["ended_at"] = utc_now()
        results["json_out"] = str(json_out)
        try:
            write_json(json_out, results)
            print(f"  JSON: {json_out}")
        except OSError as exc:
            print(f"  could not write JSON to {json_out}: {exc}")
            exit_code = exit_code or 1
        if args.keep_local:
            print(f"  local fake data kept: {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    print("done.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
