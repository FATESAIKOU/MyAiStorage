"""Drive storage, the local mirror and the search index (design.md section 4).

Layout on Drive, addressed by folder ID (never by name):

    <root>/sessions/<ULID>/session.md            header + reading version
    <root>/sessions/<ULID>/raw-<md5[:12]>.json   the agent's original export

Writes go through the outbox: raw first, session.md last, then both are
checked against Drive's md5 before the outbox entry is removed. A reader
that finds a session.md whose raw is missing or has another md5 treats the
session as unfinished and skips it.
"""

from __future__ import annotations

import codecs
import contextlib
import fcntl
import copy
import hashlib
from datetime import datetime, timezone
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from agora import header as h

SYNC_THROTTLE_S = 300
#: Bumped when the index's tables change: the index is a cache of the mirror, so
#: a new shape is rebuilt from the mirror rather than migrated (design, T1 3.1).
INDEX_VERSION = 2
#: What an unfinished session looked like the last time we downloaded it (#119):
#: `{ulid: {session_md5, raw_manifest, raw_fingerprint, legacy}}` (version 2; a version 1
#: file is ignored). Local state, never uploaded.
UNFINISHED_FILE = "unfinished.json"


class StoreError(RuntimeError):
    """Drive or local storage failed in a way the user has to know about.

    `returncode` is the rclone exit status when a run produced one: a caller that
    has to tell「資料夾不存在」(rc=3) from other failures reads it instead of
    parsing the message. It is None for errors that never got a process answer.
    """

    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


def _env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.expanduser(default))


@dataclass
class Paths:
    config: Path
    cache: Path
    state: Path

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(
            config=_env_path("AGORA_CONFIG", "~/.config/agora"),
            cache=_env_path("AGORA_CACHE_DIR", "~/.cache/agora"),
            state=_env_path("AGORA_STATE_DIR", "~/.local/state/agora"),
        )

    mirror = property(lambda s: s.cache / "sessions")
    outbox = property(lambda s: s.state / "outbox")
    pending = property(lambda s: s.state / "pending")
    trash_queue = property(lambda s: s.state / "trash-queue")   # waiting for the Drive trash
    reading = property(lambda s: s.cache / "reading")   # agent sessions' full text (design 5.10)


def now() -> float:
    """Seconds since the epoch; AGORA_NOW pins it for tests."""
    pinned = os.environ.get("AGORA_NOW")
    return float(pinned) if pinned else time.time()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fault(point: str) -> None:
    """Crash on purpose at a named point (AGORA_TEST_FAULT), to test recovery."""
    if os.environ.get("AGORA_TEST_FAULT") == point:
        os._exit(137)


def warn(message: str) -> None:
    """One line on stderr; stdout keeps only the results (spec「進度」)."""
    print(f"[agora] {message}", file=sys.stderr)


def progress(word: str, k: int, total: int, item: str = "") -> None:
    """The `k/N` line per item (T1 R3), on stderr."""
    print(f"[agora] {word} {k}/{total}  {item}".rstrip(), file=sys.stderr)


def write_atomic(path: Path, text: str) -> None:
    """Write a file so a reader sees the old one or the new one, never half of
    either. The staging name is unique: two merges over one source, or two
    sessions cached side by side, must not share it (K4, S1-9)."""
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)   # gone on success; a leftover never clutters


class _Utf8Reader:
    """A read-through for `shutil.copyfileobj` that rejects what `read_text` rejected.

    The incremental decoder keeps at most one character's bytes, so checking a raw
    costs no second copy of it (B1b-1). Only `read` is needed: that is all
    `copyfileobj` asks for.
    """

    def __init__(self, fileobj):
        self._file = fileobj
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def read(self, size: int = -1) -> bytes:
        data = self._file.read(size)
        if data:
            self._decoder.decode(data)
        else:
            self._decoder.decode(b"", final=True)   # a sequence cut off at EOF
        return data


def copy_atomic(src: Path, dst: Path, *, utf8: bool = False) -> None:
    """Copy a file so a reader sees the old one or the new one, never half of either.

    The staging name and the rename are `write_atomic`'s; the bytes are streamed, so
    a 100 MB raw does not become a second 100 MB string on its way to the mirror
    (B1b-1). `utf8` makes the copy reject the bytes `read_text` used to reject -
    without holding them - so the callers that catch UnicodeDecodeError keep
    catching it; the bytes themselves are still copied exactly, newlines and all.
    (The text round trip turned a bare CRLF into LF; no producer writes one - a raw is
    JSON, whose newlines are escaped - so the exact copy is the faithful one, and
    the mirror's md5 matches the header's. Do not "restore" the old conversion.)
    """
    handle, tmp = tempfile.mkstemp(dir=dst.parent, prefix=dst.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out, open(src, "rb") as source:
            shutil.copyfileobj(_Utf8Reader(source) if utf8 else source, out, 1 << 20)
        os.replace(tmp, dst)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _write_file(path: Path, data: bytes) -> None:
    """Write a file and get it onto the disk before anyone reads it back.

    A periodic write-back stages a session every few minutes for hours on end; a power
    cut in the middle of one used to be able to leave a `session.md` that is short but
    whole-looking, and that is exactly the version Drive would then be sent.
    """
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    """Make a rename or a removal durable: the directory entry is a write of its own."""
    try:
        fd = os.open(path, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass          # a filesystem that will not sync a directory (some network mounts)
    finally:
        os.close(fd)


def _md5(entry: dict) -> str:
    """The md5 Drive reported for a file, or "" when it has none."""
    return (entry.get("Hashes") or {}).get("md5", "")


# --- the raw's layout on Drive (design A, C.3, E; #168) ----------------------------------

RAW_LAYOUT = "chunks/1"

_HEX32 = re.compile(r"[0-9a-f]{32}")


def raw_layout(raw: dict | None) -> str | None:
    """The `layout` a header's `raw` declares, when it is a string; None otherwise.

    None is *not* "declares none": `3` or `[]` come back as None too. Whoever has to
    tell "no layout" from "a layout this version does not know" asks `raw_kind`.
    """
    layout = raw.get("layout") if isinstance(raw, dict) else None
    return layout if isinstance(layout, str) else None


def raw_segments(raw: dict | None) -> list[dict]:
    """The manifest: `raw.segments` as written, [] when there is none."""
    segs = raw.get("segments") if isinstance(raw, dict) else None
    return segs if isinstance(segs, list) else []


def _derived_name(raw: dict) -> str | None:
    md5 = raw.get("md5")
    if isinstance(md5, str) and _HEX32.fullmatch(md5):
        return f"raw-{md5[:12]}.json"
    return None


def raw_name(raw: dict) -> str:
    """The whole raw's file name in the mirror (Drive has only its segments).

    Always derived from `raw.md5` - `raw-<md5[:12]>.json` (design A.2) - and never
    from anything else in the header: a header is what another machine wrote, and a
    name taken from it can walk out of the mirror (`../../x`). The md5 has to be 32
    lowercase hex characters, or there is no name (StoreError).
    """
    name = _derived_name(raw) if isinstance(raw, dict) else None
    if name is None:
        raise StoreError("raw 標頭的 md5 不是 32 位小寫 hex，沒有可用的檔名")
    return name


def _safe_basename(name: object) -> bool:
    """A manifest name we may download or delete: a plain file name, nothing more."""
    return (isinstance(name, str) and bool(name) and name == os.path.basename(name)
            and "\\" not in name and "/" not in name and ".." not in name
            and not name.startswith("."))


#: A segment's name, and only that (design A.1): `seg-<gen>-<seq>-<md5[:12]>.part`, lower case.
#: Nothing else may be a segment - least of all `session.md` (or `SESSION.MD` on a volume that
#: ignores case), which would overwrite the completion marker.
_SEGMENT_NAME = re.compile(r"^seg-([0-9]{4,})-([0-9]{4,})-([0-9a-f]{12})\.part$")
RESERVED_NAMES = {"session.md"}


def _is_segment_name(name: object, md5: object = None) -> bool:
    """Is `name` a legal segment name - and, when `md5` is given, is its prefix that md5's?"""
    if not isinstance(name, str) or name.casefold() in RESERVED_NAMES:
        return False
    m = _SEGMENT_NAME.match(name)
    if not m:
        return False
    return md5 is None or (isinstance(md5, str) and m.group(3) == md5[:12])


def _manifest_ok(raw: dict) -> bool:
    """The static checks on a `chunks/1` manifest (design E.1), without any listing."""
    segs = raw_segments(raw)
    size = raw.get("size")
    if (not segs or not isinstance(size, int) or isinstance(size, bool)
            or _derived_name(raw) is None):
        return False
    total = 0
    seen: set[str] = set()
    for seg in segs:
        if not isinstance(seg, dict):
            return False
        seg_size = seg.get("size")
        if (not _safe_basename(seg.get("file")) or not isinstance(seg_size, int)
                or isinstance(seg_size, bool) or seg_size < 0
                or not isinstance(seg.get("md5"), str) or not _HEX32.fullmatch(seg["md5"])
                or not _is_segment_name(seg["file"], seg["md5"])
                or seg["file"].casefold() in seen):          # the same segment twice
            return False
        seen.add(seg["file"].casefold())
        total += seg_size
    return total == size


def raw_complete(raw: dict | None, files: dict) -> bool | None:
    """Is the raw this header names whole on Drive? True / False / **None**.

    None is not "no": it is a raw this version does not read (no `layout`, or one it
    does not know). The caller marks it unusable and keeps the session - it neither
    waits for Drive nor deletes anything (design E.1). `files` is Drive's listing of
    the one session folder, `{name: md5}`.
    """
    if raw is None:
        return True                    # no raw (a merge, a hand-written one): whole by itself
    if not isinstance(raw, dict) or raw_layout(raw) != RAW_LAYOUT:
        return None
    return _manifest_ok(raw) and all(
        files.get(seg["file"]) == seg["md5"] for seg in raw_segments(raw))


def raw_kind(raw: object) -> str:
    """How this version reads the header's raw: none / chunks / legacy.

    `none` is only "the key is missing or null". Everything else that is not a
    well-formed `chunks/1` manifest is `legacy` - "do not read it, do not delete it":
    no `layout` (the one-file raw older versions wrote), a layout this version does not
    know (any string but `chunks/1`, and any non-string value), a `raw` that is not a
    mapping or is an empty value (`[]`, `""`, `0`, `{}`), and a `chunks/1` whose manifest
    is not sound (bad whole md5, unsafe names, sizes that do not add up): a corrupt
    header is not "still being written", so the session stays and only its raw is
    unusable.
    """
    if raw is None:
        return "none"
    if isinstance(raw, dict) and raw.get("layout") == RAW_LAYOUT and _manifest_ok(raw):
        return "chunks"
    return "legacy"


def raw_state(raw: object, files: dict) -> bool | None:
    """True / False / None for sync: none and whole -> True, not yet whole -> False,
    a raw this version does not read -> None."""
    kind = raw_kind(raw)
    if kind == "none":
        return True
    if kind == "legacy":
        return None
    return raw_complete(raw, files)


def raw_manifest(raw: dict) -> list[tuple[str, str]]:
    """[(file, md5)] of everything on Drive the raw consists of."""
    return [(str(seg.get("file")), str(seg.get("md5"))) for seg in raw_segments(raw)
            if isinstance(seg, dict)]


def raw_fingerprint(manifest: list, files: dict) -> str:
    """md5 of the canonical JSON of the manifest's (file, md5) and what Drive lists
    for each now: the same fingerprint means a re-download could not answer differently
    (`unfinished.json` v2)."""
    rows = [[name, md5, files.get(name, "")] for name, md5 in manifest]
    return hashlib.md5(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


LEGACY_RAW_MESSAGE = ("這個 Session 的 raw 是舊格式或未知格式，這個版本不讀它；"
                      "要重新匯入請用 agora import … --as-new")


def require_usable_raw(header: dict) -> None:
    """StoreError, up front, for a session whose raw this version does not read (C.3).

    A session with no raw (a merge) passes - it has nothing to refuse.
    """
    if raw_kind(h.agora_of(header).get("raw")) == "legacy":
        raise StoreError(LEGACY_RAW_MESSAGE)


def _env_positive_int(name: str, default: int) -> int:
    """An integer environment setting; a bad value is an error, never the default."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except ValueError:
        raise StoreError(f"{name}={value!r} 不是整數") from None
    if number <= 0:
        raise StoreError(f"{name}={value!r} 要大於 0")
    return number


def raw_chunk_size() -> int:
    """`AGORA_RAW_CHUNK`: bytes per sealed segment (default 4 MiB; tests use 4096)."""
    return _env_positive_int("AGORA_RAW_CHUNK", 4 * 1024 * 1024)


def raw_orphan_grace() -> int:
    """`AGORA_RAW_ORPHAN_GRACE`: seconds an unreferenced segment is left alone (default 3600)."""
    return _env_positive_int("AGORA_RAW_ORPHAN_GRACE", 3600)


class Drive:
    """Thin wrapper over rclone. Never reads the token; rclone refreshes it."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.rclone = os.environ.get("AGORA_RCLONE", "rclone")
        self.conf = paths.config / "rclone.conf"
        self.settings_file = paths.config / "config.json"
        self.fetched = 0                  # downloads so far: pull tells a skip from a pull (P3)
        self.settings = json.loads(self.settings_file.read_text()) if self.settings_file.exists() else {}

    def _run(self, *args: str, root: bool = True) -> str:
        cmd = [self.rclone, "--config", str(self.conf), "--ask-password=false"]
        if root:
            cmd += ["--drive-root-folder-id", self.folder_id()]
        try:
            # DEVNULL, and never a prompt: a periodic write-back runs while the agent
            # owns the terminal, and rclone waiting for a password to be typed would
            # fight the agent for the keyboard - or hang until the write-back's own
            # timeout, holding the session back (design「不弄亂 agent 的畫面」, P9/P15).
            proc = subprocess.run(cmd + list(args), capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL)
        except OSError as e:   # rclone missing or not executable: a Drive problem, not a bad file (R1)
            raise StoreError(f"叫不起 rclone（{self.rclone}）：{e}") from None
        if proc.returncode != 0:
            raise StoreError(f"rclone {args[0]} 失敗（rc={proc.returncode}）：{proc.stderr.strip()[-300:]}",
                             returncode=proc.returncode)
        return proc.stdout

    def folder_id(self) -> str:
        """The root folder's ID, created on first use and kept in config.json.

        IDs are kept per folder name, so a run with AGORA_FOLDER_NAME=agora-test
        never makes the next normal run write into the test folder.
        """
        name = os.environ.get("AGORA_FOLDER_NAME", "agora")
        self.settings.pop("folder_id", None)       # old single-folder format (G1)
        self.settings.pop("folder_name", None)
        folders = self.settings.setdefault("folders", {})
        if folders.get(name):
            return folders[name]
        self._run("mkdir", f"gdrive:{name}", root=False)
        entries = json.loads(self._run("lsjson", "gdrive:", "--dirs-only", root=False))
        matches = [e for e in entries if e["Name"] == name]
        if len(matches) != 1:
            raise StoreError(f"Drive 根目錄有 {len(matches)} 個 {name}/，請先處理同名資料夾")
        folders[name] = matches[0]["ID"]
        self.paths.config.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(self.settings, indent=2))
        return folders[name]

    def list_sessions(self) -> dict[str, dict[str, str]] | None:
        """{ulid: {file name: md5}} in one recursive listing; None if sessions/ is missing."""
        try:
            out = self._run("lsjson", "gdrive:sessions", "-R", "--fast-list", "--hash", "--files-only")
        except StoreError as e:
            if "directory not found" in str(e):
                return None
            raise
        found: dict[str, dict[str, str]] = {}
        for entry in json.loads(out or "[]"):
            parts = entry["Path"].split("/")
            if len(parts) == 2:
                found.setdefault(parts[0], {})[parts[1]] = _md5(entry)
        return found

    def list_sessions_with_ids(self) -> "_Listing | None":
        """`list_sessions` plus each session folder's Drive ID (#164, design B.5.7).

        The same one recursive listing without `--files-only`: the directory entries
        come back too, and (K.1, real Drive) they carry an `ID`. `.ids` has an entry
        for every top-level folder that did. None if sessions/ is missing.
        """
        try:
            out = self._run("lsjson", "gdrive:sessions", "-R", "--fast-list", "--hash")
        except StoreError as e:
            if "directory not found" in str(e):
                return None
            raise
        found = _Listing()
        for entry in json.loads(out or "[]"):
            parts = entry["Path"].split("/")
            if entry.get("IsDir"):
                if len(parts) == 1 and entry.get("ID"):
                    found.ids[parts[0]] = entry["ID"]
            elif len(parts) == 2:
                found.setdefault(parts[0], {})[parts[1]] = _md5(entry)
                if entry.get("ModTime"):
                    found.mtimes.setdefault(parts[0], {})[parts[1]] = entry["ModTime"]
        return found

    def folder_ids(self) -> dict[str, str]:
        """{ULID: folder ID} from `lsjson gdrive:sessions --dirs-only` (the shape
        `folder_id()` uses): the fallback when a listing carried no directory IDs."""
        out = self._run("lsjson", "gdrive:sessions", "--dirs-only")
        return {e["Name"]: e["ID"] for e in json.loads(out or "[]") if e.get("ID")}

    def list_one(self, ulid: str) -> dict[str, str]:
        out = self._run("lsjson", f"gdrive:sessions/{ulid}", "--hash", "--files-only")
        return {e["Name"]: _md5(e) for e in json.loads(out or "[]")}

    def upload(self, local: Path, ulid: str, name: str) -> None:
        # copyto, never copy: copy treats the target as a directory and
        # silently creates an empty folder named after the file.
        self._run("copyto", str(local), f"gdrive:sessions/{ulid}/{name}")

    def download(self, ulid: str, name: str, local: Path) -> None:
        self.fetched += 1
        local.parent.mkdir(parents=True, exist_ok=True)
        self._run("copyto", f"gdrive:sessions/{ulid}/{name}", str(local))

    def download_many(self, ulids: list[str], mirror: Path) -> None:
        """Fetch several session.md files in one rclone run instead of one call each."""
        mirror.mkdir(parents=True, exist_ok=True)
        _with_files_from(self, [f"{u}/session.md" for u in ulids], mirror,
                         "copy", "gdrive:sessions", str(mirror), "--no-traverse")


    def download_segments(self, ulid: str, names: list[str], dest: Path) -> None:
        """Fetch several files of one session folder in one rclone run (`--files-from`)."""
        self.fetched += 1                 # pull tells a skip from a pull by this (P3)
        dest.mkdir(parents=True, exist_ok=True)
        _with_files_from(self, names, dest, "copy", f"gdrive:sessions/{ulid}", str(dest),
                         "--no-traverse")


class _Chunker:
    """Cuts a stream of blocks into pieces and keeps each piece's md5 and size, plus the
    whole thing's: the manifest, taken on the same pass that stages the raw.

    With no `points` the pieces are `chunk` bytes each (the last one shorter). With `points`
    (strictly increasing byte offsets) a piece ends at each of them, and the rest is the
    last piece - how a plan says where the sealed range ends and the tail begins.
    `skip_first` leaves the md5 of the first piece uncomputed (it is the prefix a previous
    manifest already describes): its entry is `(None, size)`.
    """

    def __init__(self, chunk: int, points: list[int] | None = None, skip_first: bool = False):
        self.chunk = chunk
        self.points = list(points) if points is not None else None
        self._skipping = skip_first
        self._next = 0
        self.whole = hashlib.md5()
        self.size = 0
        self.parts: list[tuple[str | None, int]] = []
        self._cur = hashlib.md5()
        self._cur_size = 0

    def _remaining(self) -> int | None:
        """Bytes to the next boundary, or None when the rest is one last piece."""
        if self.points is None:
            return self.chunk - self._cur_size
        if self._next < len(self.points):
            return self.points[self._next] - self.size
        return None

    def feed(self, data: bytes) -> None:
        view = memoryview(data)
        while len(view):
            limit = self._remaining()
            take = len(view) if limit is None else min(len(view), limit)
            piece = view[:take]
            self.whole.update(piece)
            if not self._skipping:
                self._cur.update(piece)
            self._cur_size += take
            self.size += take
            view = view[take:]
            if limit is not None and take == limit:
                self._close()

    def _close(self) -> None:
        self.parts.append((None if self._skipping else self._cur.hexdigest(), self._cur_size))
        self._skipping = False
        self._cur = hashlib.md5()
        self._cur_size = 0
        if self.points is not None:
            self._next += 1

    def finish(self) -> tuple[str, int, list[tuple[str | None, int]]]:
        if self._cur_size or not self.parts:       # the last piece; an empty raw is one empty piece
            self._close()
        return self.whole.hexdigest(), self.size, self.parts


def segment_name(gen: int, seq: int, md5: str) -> str:
    """`seg-<gen>-<seq>-<md5[:12]>.part` (design A.1)."""
    return f"seg-{gen:04d}-{seq:04d}-{md5[:12]}.part"


_SEG_GEN = re.compile(r"^seg-([0-9]{4,})-[0-9]{4,}-[0-9a-f]{12}\.part$")


def _generation(raw: object) -> int:
    """The generation the manifest's first segment names (0 when there is none)."""
    segs = raw_segments(raw) if isinstance(raw, dict) else []
    if segs and isinstance(segs[0], dict):
        m = _SEG_GEN.match(str(segs[0].get("file")))
        if m:
            return int(m.group(1))
    return 0


def _stage_raw(tmp: Path, raw: bytes | Path, chunk: int, points: list[int] | None = None,
               skip_first: bool = False) -> tuple[str, int, str, list[tuple[str | None, int]]]:
    """Put the raw into `tmp`; return (md5, size, file name, [(segment md5, size)]).

    The manifest comes from the same pass that reads the raw. Bytes are written directly.
    A path is first tried as a hard link (the export's temp file and the outbox are
    usually on one volume - one whole-raw write fewer for an 800 MB first import) and
    read once for the md5s; only when linking fails is it copied in 1 MiB blocks, hashing
    as it goes. The staging name is unique inside `tmp` and renamed to the raw-md5 name
    when it is done, so a crash leaves no `raw-*.json` half-file.
    """
    chunker = _Chunker(chunk, points, skip_first)
    if isinstance(raw, Path):
        staging = tmp / f".staging-{os.getpid()}.tmp"
        try:
            try:
                os.link(raw, staging)
                with open(staging, "rb") as src:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        chunker.feed(block)
            except OSError:
                staging.unlink(missing_ok=True)
                chunker = _Chunker(chunk, points, skip_first)
                with open(staging, "wb") as out, open(raw, "rb") as src:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        chunker.feed(block)
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
            md5, size, parts = chunker.finish()
            fd = os.open(staging, os.O_RDONLY)     # a linked file is already on disk (the export
            try:                                   # synced it), so this is cheap - and every file in
                os.fsync(fd)                       # the staging folder is synced, as #116 asks
            finally:
                os.close(fd)
            name = f"raw-{md5[:12]}.json"
            os.replace(staging, tmp / name)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        return md5, size, name, parts
    for start in range(0, len(raw), 1 << 20):
        chunker.feed(raw[start:start + (1 << 20)])
    md5, size, parts = chunker.finish()
    name = f"raw-{md5[:12]}.json"
    _write_file(tmp / name, raw)
    return md5, size, name, parts


EMPTY_MD5 = hashlib.md5(b"").hexdigest()


def _cursor_path(paths: Paths, ulid: str) -> Path:
    return paths.state / "cursors" / f"{ulid}.json"


def read_cursor(paths: Paths, ulid: str) -> dict | None:
    """The cursor `stage` last wrote for the session, or None: a missing, unreadable or
    malformed file is "no cursor", which means the next version is cut whole (fail-safe)."""
    try:
        data = json.loads(_cursor_path(paths, ulid).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def forget_cursor(paths: Paths, ulid: str) -> None:
    try:
        _cursor_path(paths, ulid).unlink(missing_ok=True)
    except OSError as e:
        warn(f"{ulid} 的 cursor 刪不掉（下一輪驗證不過就會整份重切，不影響正確性）：{e}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _plan_mode(plan: dict | None, previous_raw: object, stored: dict | None, ulid: str, size: int,
               chunk: int) -> dict:
    """How this version's raw is cut: `append`, `sealed` or `plain` (design B.2, C.1).

    append - a previous manifest of ours, whose sealed segments end exactly where the new bytes
        begin, and the cursor stored for it (`stored`, read from disk, not taken on the
        adapter's word) that says the same: reuse those segments, cut `[append_from,
        seal_upto)` by `chunk` (when it is at least one chunk), the rest is the tail.
    sealed - the whole raw is new but the adapter knows where its main content ends: cut
        `[0, seal_upto)` by `chunk` (nothing at all when it is not more than one chunk).
    plain  - no such notion (opencode, agy, merge): the whole raw cut by `chunk`.
    Anything that does not add up falls through to the next mode: a wrong increment is never sent.
    """
    seal_upto = plan.get("seal_upto") if plan else None
    if not _is_int(seal_upto) or not (0 < seal_upto < size):
        return {"mode": "plain"}
    append_from = plan.get("append_from")
    if (_is_int(append_from) and isinstance(stored, dict) and plan.get("cursor_ulid") == ulid
            and 0 <= append_from <= seal_upto and raw_kind(previous_raw) == "chunks"
            and previous_raw.get("md5") == stored.get("base_md5")
            and stored.get("raw_stable") == append_from
            and _is_int(stored.get("sealed_line_count")) and isinstance(stored.get("sealed_md5"), str)
            and sum(seg["size"] for seg in raw_segments(previous_raw)[:-1]) == append_from):
        sealed_now = seal_upto - append_from >= chunk
        points = [append_from] if append_from > 0 else []
        if sealed_now:
            points += [append_from + k * chunk for k in range(1, (seal_upto - append_from - 1) // chunk + 1)]
            points.append(seal_upto)
        return {"mode": "append", "points": points, "skip_first": append_from > 0,
                "append_from": append_from, "sealed_now": sealed_now, "seal_upto": seal_upto}
    sealed_now = seal_upto > chunk
    points = ([k * chunk for k in range(1, (seal_upto - 1) // chunk + 1)] + [seal_upto]) if sealed_now else []
    return {"mode": "sealed", "points": points, "skip_first": False, "append_from": 0,
            "sealed_now": sealed_now, "seal_upto": seal_upto}


def _final_cursor(plan: dict, decided: dict, md5: str, stored: dict | None) -> dict | None:
    """The cursor to write once the stage has landed: this version's `base_md5`, and the
    sealed values - moved up to this version's main content when it sealed something, kept when
    an append sealed nothing, back to "nothing sealed" for a version cut whole."""
    cursor = plan.get("cursor")
    if not isinstance(cursor, dict) or decided["mode"] == "plain":
        return None
    out = dict(cursor)
    out["base_md5"] = md5
    if decided["sealed_now"]:
        out.update(sealed_line_count=cursor.get("main_line_count"), sealed_md5=cursor.get("main_md5"),
                   raw_stable=decided["seal_upto"])
    elif decided["mode"] == "append":
        out.update(sealed_line_count=stored["sealed_line_count"], sealed_md5=stored["sealed_md5"],
                   raw_stable=stored["raw_stable"])
    else:
        out.update(sealed_line_count=0, sealed_md5=EMPTY_MD5, raw_stable=0)
    return out


def _previous_raw(paths: Paths, ulid: str, folder: Path) -> dict | None:
    """The `raw` block of the version this stage replaces: the outbox entry if there is
    one, else the mirror's session.md (`remember` keeps it after every stage)."""
    try:
        if folder.is_dir():
            return h.agora_of(read_entry(folder)).get("raw")
    except h.HeaderError:
        pass
    try:
        hdr, _ = h.split_document((paths.mirror / ulid / "session.md").read_text(encoding="utf-8"))
        return h.agora_of(hdr).get("raw")
    except (OSError, h.HeaderError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Outbox: a session is written here first and only leaves once Drive has it.
# ---------------------------------------------------------------------------


def stage(paths: Paths, header: dict, body: str,
          raw: bytes | Path | None, *, update: bool = False, kind: str = "",
          plan: dict | None = None) -> Path:
    """Write a complete session into the outbox and return its folder.

    `raw` may be the whole bytes, or a file path that holds them. When it is a path,
    the bytes are streamed into the staging folder and the md5/size are taken from
    the file without a second in-memory copy (impl-mem B1b-2).

    `plan` is an adapter's `Increment` as a dict - `append_from`, `seal_upto`, `cursor`,
    `cursor_ulid` (design B.1): where the new bytes start, where the main content ends, and the
    local state for the next round. It is checked here against the previous manifest and
    cursor and, if it does not add up, the version is cut whole instead. The cursor is
    written only after the swap, so a stage that fails leaves the old one in place.

    `update`／`kind` say what `mark_update` says, and are written into the folder that
    is about to go in, before the swap: the uploader reads the outbox whenever it likes,
    and a version that was in there for the moment between this call and a later
    `mark_update` looked like a new session Drive had never heard of (design
    「store 的小修」P13). Callers that decide whether this is an update at all have to ask
    `never_confirmed` *before* calling this: afterwards the answer is always "yes", the
    fresh folder having no marker.
    """
    ulid = header["id"].split(":", 1)[1]
    paths.outbox.mkdir(parents=True, exist_ok=True)
    folder = paths.outbox / ulid
    tmp = paths.outbox / f".tmp-{ulid}"
    old = paths.outbox / f".old-{ulid}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        new_cursor = None
        if raw is not None:
            chunk = raw_chunk_size()
            previous_raw = _previous_raw(paths, ulid, folder)
            raw_size = raw.stat().st_size if isinstance(raw, Path) else len(raw)
            stored = read_cursor(paths, ulid) if plan else None
            decided = _plan_mode(plan, previous_raw, stored, ulid, raw_size, chunk)
            md5, size, name, parts = _stage_raw(
                tmp, raw, chunk, decided.get("points") if decided["mode"] != "plain" else None,
                bool(decided.get("skip_first")))
            if (raw_kind(previous_raw) == "chunks" and previous_raw.get("md5") == md5
                    and previous_raw.get("size") == size):
                # The same raw under a new header (an edit): the same manifest, or the
                # same bytes would be renamed and sent whole again (design B.4). Nothing
                # new is sealed, so the cursor on disk stays as it is.
                segments = copy.deepcopy(previous_raw["segments"])
            else:
                if decided["mode"] == "append":
                    kept = copy.deepcopy(raw_segments(previous_raw)[:-1])     # the sealed ones, as they are
                    gen = _generation(previous_raw)
                    pieces = parts[1:] if decided["skip_first"] else parts
                else:
                    kept, gen, pieces = [], _generation(previous_raw) + 1, parts
                segments = kept + [
                    {"file": segment_name(gen, len(kept) + n, piece_md5), "size": piece_size,
                     "md5": piece_md5} for n, (piece_md5, piece_size) in enumerate(pieces)]
                if plan:
                    new_cursor = _final_cursor(plan, decided, md5, stored)
            header.setdefault("agora", {})["raw"] = {
                "md5": md5, "size": size, "layout": RAW_LAYOUT, "segments": segments}
            _fault("after-manifest")      # crash with the raw staged, before anything is swapped in
        else:
            header.setdefault("agora", {}).pop("raw", None)
        if folder.is_dir():
            # The raw the entry being replaced names comes along, and only that one: an
            # uploader may have built its list from that entry a moment ago, and rclone
            # reading it after this swap would otherwise stop the whole batch on
            # `source not found` (review T2). Older raws stay behind - nobody's list names
            # them any more, and carrying every version made each round's entry one raw
            # bigger and each pass send them all again (review N1).
            previous = _named_raw(folder)
            if previous and not (tmp / previous).exists():
                try:
                    shutil.copyfile(folder / previous, tmp / previous)
                except OSError:
                    pass                    # claimed by an uploader meanwhile: nothing to keep
        _write_file(tmp / "session.md", h.dump_document(header, body).encode("utf-8"))
        if update:
            mark_update(tmp, kind)          # into the folder that is about to go in, not the one that is there
        _fsync_dir(tmp)                    # the files and the names pointing at them
        # Swap in whole, so a crash never leaves an outbox entry without session.md
        # (C4): the old entry is renamed aside, the new one moved in, then the old removed (R6).
        shutil.rmtree(old, ignore_errors=True)
        if folder.exists():
            folder.rename(old)
        tmp.rename(folder)
        shutil.rmtree(old, ignore_errors=True)
        # `.done-<ULID>` is left alone (review R1): an uploader may be comparing it right
        # now, and what it holds is a version that was current a moment ago - deleting it
        # loses an edit. Whoever finds `outbox/<ULID>` already there drops it instead:
        # `_rename_back` while comparing, `restore_done` when the background takes the lock.
        _fsync_dir(paths.outbox)            # the rename itself, and the entry it removed
        if new_cursor is not None:
            # Only now: the version is in the outbox. A cursor written earlier would, after a
            # failed stage, claim a prefix that no manifest on disk describes.
            try:
                path = _cursor_path(paths, ulid)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(path, json.dumps(new_cursor, ensure_ascii=False, sort_keys=True))
            except OSError as e:
                warn(f"{ulid} 的 cursor 寫不進去（下一輪整份重切，不影響正確性）：{e}")
        return folder
    except BaseException:
        # A failure before the swap left a staging folder; the old entry is untouched,
        # so the staging folder is only a half-written copy (impl-mem B1b-2). A
        # SIGTERM is a BaseException here (`round_child._exit_on_sigterm` turns it
        # into SystemExit) and can land between the two renames: the old entry is
        # then aside and `outbox/<ULID>` is gone. Put the old version back before
        # leaving - the round died, but it must not lose the write-back that was
        # already there (#117).
        try:
            if not folder.is_dir() and old.is_dir():
                old.rename(folder)
            elif folder.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass                            # the original failure is the one to report
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def outbox_count(paths: Paths) -> int:
    return len(waiting_ulids(paths))


def queued_for_trash(paths: Paths) -> set[str]:
    """Sessions deleted here, still waiting for the background to move them.

    One place, because the answer is needed in several: the uploader's loop, the
    reminder at the start of a command, and pull/push refusing them (review N10).
    """
    if not paths.trash_queue.exists():
        return set()
    return {p.name for p in paths.trash_queue.iterdir() if p.is_file()}


def hold_upload_lock(paths: Paths, blocking: bool = False):
    """The upload lock, or None when somebody else has it.

    One lock for the background, a push and anything else that sends (review M1). The
    background and sync take it without waiting - if they cannot, somebody is already
    doing it. `push` waits, because its contract is "it is on Drive when this returns".
    """
    lock_path = paths.state / "upload.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a+")
    mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(lock, mode)
    except OSError:
        lock.close()
        return None
    return lock          # closing the file lets the flock go, so `with` is the protocol


def uploader_is_running(paths: Paths) -> bool:
    """Whether somebody holds the upload lock right now."""
    held = hold_upload_lock(paths)
    if held is None:
        return True
    held.close()
    return False


def restore_done(paths: Paths) -> None:
    """Put back `.done-<ULID>` folders an uploader left behind, so they go again.

    Only by whoever has just taken the upload lock - the background, at the start of
    its run (review N6, T3-size E3): restoring one while another uploader is comparing
    it would have the two of them grabbing the same folder. It used to run from
    `outbox_ulids` after a quick try of the lock, and that try is what made a
    background starting at the same moment think somebody was running (T3-sec3 R6).
    """
    if not paths.outbox.exists():
        return
    for done in paths.outbox.glob(".done-*"):
        _put_back(done, paths.outbox / done.name[len(".done-"):])


def _restorable_old(paths: Paths) -> dict[str, Path]:
    """`.old-<ULID>` with no `outbox/<ULID>` and no stage in flight (`.tmp-<ULID>`): {ULID: folder}.

    What a `stage` that crashed between its two renames leaves - the previous complete entry.
    `outbox_ulids` puts these back; a read-only caller (`outbox_folders`) only names them."""
    found = {}
    if paths.outbox.exists():
        for old in paths.outbox.glob(".old-*"):
            ulid = old.name[len(".old-"):]
            if not (paths.outbox / ulid).exists() and not (paths.outbox / f".tmp-{ulid}").exists():
                found[ulid] = old
    return found


def outbox_ulids(paths: Paths) -> set[str]:
    """The ULIDs waiting in the outbox. Never touches the upload lock (T3-sec3 R6)."""
    if not paths.outbox.exists():
        return set()
    for ulid, old in _restorable_old(paths).items():
        old.rename(paths.outbox / ulid)   # a stage crashed mid-swap: keep the previous complete entry
    return {p.name for p in paths.outbox.iterdir() if p.is_dir() and not p.name.startswith(".")}


def outbox_folders(paths: Paths) -> dict[str, Path]:
    """`waiting_ulids` without any repair: {ULID: the folder that holds its entry}.

    The same three shapes - a plain entry, a `.old-` that `outbox_ulids` would put back, a
    `.done-` set aside - found by the same rules, but nothing is renamed. For the commands
    that must not touch the outbox (`migrate inventory`)."""
    found: dict[str, Path] = {}
    if not paths.outbox.exists():
        return found
    for p in paths.outbox.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            found[p.name] = p
    for ulid, old in _restorable_old(paths).items():
        found.setdefault(ulid, old)
    for ulid, done in _set_aside(paths).items():
        found.setdefault(ulid, done)
    return found


def _set_aside(paths: Paths) -> dict[str, Path]:
    """`.done-<ULID>` with no `outbox/<ULID>` next to it: {ULID: folder}.

    Either an uploader comparing it right now, or one that died between the rename and
    the comparison (T3-final4 K1). Until an uploader has confirmed it, that version is
    not on Drive as far as anybody else knows - and only the lock holder may put it
    back (`restore_done`), so everybody else reads it where it is.
    """
    if not paths.outbox.exists():
        return {}
    return {d.name[len(".done-"):]: d for d in paths.outbox.glob(".done-*")
            if d.is_dir() and not (paths.outbox / d.name[len(".done-"):]).exists()}


def waiting_ulids(paths: Paths) -> set[str]:
    """Everything not yet confirmed on Drive: the outbox plus what was set aside (K1).

    For the commands that do not send - starting the uploader, the reminders, sync
    keeping its hands off the mirror. The ones that send hold the lock, put the set-aside
    ones back first, and use `outbox_ulids`.
    """
    return outbox_ulids(paths) | set(_set_aside(paths))


def index_file(index: "Index", session_md: Path) -> None:
    """Index one session.md under its ULID. Raises on an unreadable one."""
    hdr, body = h.split_document(session_md.read_text(encoding="utf-8"))
    index.put(session_md.parent.name, md5_file(session_md), hdr, body)


def remember(paths: Paths, folder: Path, index: "Index | None" = None, ulid: str | None = None) -> None:
    """Put a session we just wrote into the local mirror and index right away.

    The whole session, not just the reading version: the raw too, so a session written
    here can be rescued after another machine deletes it (change local-first-writes,
    「本機保留完整的一份」). A raw that is already there with the same name and size is
    left alone - sync calls this for every outbox entry on every run - and the ones the
    new header replaced are cleaned up, so a session does not keep both versions.

    `ulid` is for a folder not named after its session (a set-aside `.done-`, K1).
    """
    mirror = paths.mirror / (ulid or folder.name)
    mirror.mkdir(parents=True, exist_ok=True)
    # Atomic: a sync in another process indexes this same outbox entry into this same mirror
    # file, and a plain copy lets one of them read the other's half-written (#222).
    copy_atomic(folder / "session.md", mirror / "session.md")
    raw = (h.agora_of(read_entry(folder)).get("raw") or {})
    if raw and (name := raw_name(raw)) and (folder / name).is_file():
        wanted, target = folder / name, mirror / name
        if not (target.is_file() and target.stat().st_size == wanted.stat().st_size):
            copy_atomic(wanted, target, utf8=True)
        for other in mirror.glob("raw-*"):
            # Not another process's `copy_atomic` staging file (`raw-<md5>.json<random>.tmp`).
            if other.name != name and not other.name.endswith(".tmp"):
                other.unlink(missing_ok=True)
    try:
        index_file(index or Index(paths), mirror / "session.md")
    except sqlite3.Error as e:
        # The session is already whole in the outbox and the mirror; the index is a cache, so a
        # lost index write must not turn a saved import into a failed one (#222). A marker is
        # left for `sync` to index it again - before its throttle and offline returns, since
        # the next import looks the source up in the index and must find this one.
        pending = paths.cache / PENDING_INDEX
        pending.mkdir(parents=True, exist_ok=True)
        (pending / mirror.name).touch()     # empty: no staging file for `index_pending` to trip on
        warn(f"索引更新失敗（{e}），Session 已存好，已留標記，下次 sync 會補進索引：{mirror.name}")


def index_pending(paths: Paths, index: "Index", warn=warn) -> None:
    """Index again the sessions `remember` could not (#222): one marker each, gone once it is in.

    Only the local mirror and index are touched, so this runs on every `sync`, throttled or
    offline. A failure leaves the marker for the next time.
    """
    pending = paths.cache / PENDING_INDEX
    if not pending.is_dir():
        return
    for marker in sorted(pending.iterdir()):
        if not _ULID_NAME.fullmatch(marker.name):
            continue
        md = paths.mirror / marker.name / "session.md"
        try:
            if md.is_file():
                index_file(index, md)
        except sqlite3.OperationalError as e:
            warn(f"索引還是寫不進去（{e}），下次再補：{marker.name}")
            break       # the lock is held by someone else; the next marker would wait just as long
        except sqlite3.Error as e:
            warn(f"索引還是寫不進去（{e}），下次再補：{marker.name}")
            continue    # this one's own trouble: the others may still go in, and its marker stays
        except (h.HeaderError, OSError, UnicodeDecodeError):
            # the mirror copy is unreadable; the marker would never clear
            warn(f"mirror 裡的 Session 讀不到，標記不留：{marker.name}")
        marker.unlink(missing_ok=True)


def _index_outbox(paths: Paths, index: "Index") -> None:
    """Sessions still waiting in the outbox are searchable here, marked as not uploaded.

    The set-aside ones too (K1): Drive may still have an older version, and the sync
    that just mirrored it must not be the last word, or the next edit starts from it.
    """
    folders = {ulid: paths.outbox / ulid for ulid in outbox_ulids(paths)}
    for ulid, folder in {**_set_aside(paths), **folders}.items():
        try:
            remember(paths, folder, index, ulid)
        except (h.HeaderError, OSError, UnicodeDecodeError):
            continue   # e.g. the uploader just put it back or finished with it


def _entry_bytes(folder: Path) -> bytes:
    """One copy of the entry's session.md; HeaderError if it cannot be read.

    The uploader wants the bytes it hashed, the rest of the code wants the header;
    both read here and parse through `_entry_header`, so the two never disagree about
    what a readable entry is (review W1).
    """
    try:
        return (folder / "session.md").read_bytes()
    except OSError as e:
        raise h.HeaderError(str(e)) from None


def _entry_header(folder: Path, data: bytes) -> tuple[dict, str | None]:
    """The header and the raw it names, from these exact bytes.

    As strict as `read_entry` always was (review W1): a `raw` that is not a mapping, or
    that has no `file`, is a broken entry - HeaderError, never an AttributeError that
    escapes `upload_batch` and stops the whole round. The raw it names has to be there
    too, or the session would go up pointing at a file Drive does not have.
    """
    try:
        # `read_text()` used to turn CRLF/CR into LF before parsing; keep that for the header
        # (the bytes sent and hashed stay exactly what is in the file).
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        hdr, _ = h.split_document(text)
        raw = h.agora_of(hdr).get("raw")
        name = None
        if raw:
            name = raw_name(raw)
            if not (folder / name).is_file():
                raise h.HeaderError(f"缺少 {name}")
    except (OSError, UnicodeDecodeError, AttributeError, KeyError, TypeError, StoreError) as e:
        raise h.HeaderError(str(e)) from None
    return hdr, name


def read_entry(folder: Path) -> dict:
    """The header of an outbox entry; HeaderError if it is unreadable or incomplete."""
    return _entry_header(folder, _entry_bytes(folder))[0]


def _push_session_md_only(drive: Drive, folder: Path, ulid: str) -> None:
    """A session with no raw (a merge): session.md alone, then Drive's own md5 decides."""
    drive.upload(folder / "session.md", ulid, "session.md")
    _fault("after-session-upload")
    remote = drive.list_one(ulid)
    if remote.get("session.md") != md5_file(folder / "session.md"):
        raise StoreError(f"{ulid} 的 session.md 在 Drive 上的 md5 不符")


def _push_chunked(drive: Drive, paths: Paths, folder: Path, ulid: str, data: bytes, raw: dict) -> None:
    """Send one mirrored `chunks/1` session: the segments Drive lacks, then session.md.

    `data` is the one read of session.md this push works from: `raw` (the manifest) was
    parsed out of those very bytes, the batch dir gets those bytes, and the check at the end
    compares Drive with their md5 - the same rule as the uploader's `documents` snapshot
    (#130). A `stage` landing meanwhile cannot make a session.md go up with a manifest it
    does not belong to.

    Like the uploader (design B.5, 3.17): the missing segments are cut from the whole raw
    in the mirror into a batch dir, stamped with the current time, and go in **one**
    `copy --files-from` - not one call per segment, which for an 800 MB session would be
    ~195 calls - then session.md in another. Drive's own md5s decide afterwards: session.md
    and every segment. Nothing else goes (R7).
    """
    whole = folder / raw_name(raw)
    manifest = raw_segments(raw)
    try:
        on_drive = drive.list_one(ulid)
    except StoreError as e:
        if e.returncode == 3 or "directory not found" in str(e):
            on_drive = {}               # the session is not there (a revive): all of it goes
        else:
            raise
    needed = {seg["file"] for seg in manifest if on_drive.get(seg["file"]) != seg["md5"]}
    if not whole.is_file():
        if needed:
            raise StoreError(f"標頭指到的 {whole.name} 本機沒有，Drive 上的段又不齊，不傳半套")
        warn(f"{ulid} 標頭指到的 {whole.name} 本機沒有；Drive 上的段都在，只傳 session.md")
    where = paths.state / f"pushbatch-{os.getpid()}"     # not `batch-*`: the uploader sweeps those
    shutil.rmtree(where, ignore_errors=True)
    try:
        target = where / ulid
        target.mkdir(parents=True)
        _write_file(target / "session.md", data)
        try:
            if needed:
                _cut_segments(whole, target, manifest, needed)
        except ValueError as e:
            raise StoreError(f"{ulid} 切段失敗（{e}），不傳") from None
        for path in where.rglob("*"):
            if path.is_file():
                os.utime(path, None)
        if needed:
            _copy_batch(drive, paths, [f"{ulid}/{name}" for name in sorted(needed)], source=where)
        _fault("after-segment-upload")
        _copy_batch(drive, paths, [f"{ulid}/session.md"], source=where)
        _fault("after-session-upload")
    finally:
        shutil.rmtree(where, ignore_errors=True)
    remote = drive.list_one(ulid)
    if remote.get("session.md") != hashlib.md5(data).hexdigest():
        raise StoreError(f"{ulid} 的 session.md 在 Drive 上的 md5 不符")
    if any(remote.get(seg["file"]) != seg["md5"] for seg in manifest):
        raise StoreError(f"{ulid} 的段在 Drive 上缺或 md5 不符")


def push_mirror(drive: Drive, paths: Paths, ulid: str, header: dict) -> None:
    """Send one mirrored session up: `session.md` and, for a raw, its segments.

    session.md is read once here; the raw block that decides what to send is parsed from
    those bytes, not from `header` (which the caller read earlier)."""
    folder = paths.mirror / ulid
    try:
        data = (folder / "session.md").read_bytes()
        raw = _raw_block(data)
    except (OSError, h.HeaderError, UnicodeDecodeError) as e:
        raise StoreError(f"{ulid} 的 session.md 讀不了：{e}") from None
    kind = raw_kind(raw)
    if kind == "chunks":
        _push_chunked(drive, paths, folder, ulid, data, raw)
    elif kind == "none":
        _push_session_md_only(drive, folder, ulid)
    else:
        raise StoreError(f"{ulid}：{LEGACY_RAW_MESSAGE}")      # never write the old layout back


def mirror_one(paths: Paths, drive: Drive, index: "Index", ulid: str,
               files: dict) -> dict | None:
    """Put one session Drive has into the mirror and the index (sync and pull, once).

    None means it is not whole yet: its header names a raw (or segments) that are
    missing or have another md5, so the half-written session.md leaves the mirror and
    the index (G3). The caller words that case its own way. A raw this version does
    not read is not "not whole yet": the session stays, with its raw unusable.
    """
    local = paths.mirror / ulid / "session.md"
    if not (local.exists() and md5_file(local) == files.get("session.md")):
        drive.download(ulid, "session.md", local)
    hdr, body = h.split_document(local.read_text(encoding="utf-8"))
    # False: the raw is not whole on Drive yet - out of the mirror and the index (G3).
    # None: a raw this version does not read (legacy / unknown layout) - kept and
    # indexed like any other session, only its raw is unusable (design C.3, E.1).
    if raw_state(h.agora_of(hdr).get("raw"), files) is False:
        local.unlink(missing_ok=True)
        index.drop(ulid)
        return None
    index.put(ulid, files.get("session.md"), hdr, body)
    return hdr


UPDATE_MARK = ".update"      # written when the session is an existing id (review N4)


def mark_update(folder: Path, kind: str = "") -> None:
    """Say that this entry overwrites a session Drive already has, and what kind of write
    it is (`continue` for one; an edit is not a relation and keeps X's own).

    Decided by the writer, in the foreground, because by the time an uploader sees the
    folder `remember` has already put every new session in the index - judging there
    would treat every fresh import as an update, and those are not on Drive yet, so
    they would never be sent (review N4). The kind matters only if that session is gone
    by the time we send: the edit is then saved as a session of its own, and what it is
    depends on which command wrote it (review G2).

    `stage(..., update=True, kind=...)` writes the same thing before the swap, so a
    caller that already knows does not have to come back for it (P13); this stays for
    the callers that decide afterwards.
    """
    folder.mkdir(parents=True, exist_ok=True)
    _write_file(folder / UPDATE_MARK, kind.encode("utf-8"))


def update_kind(folder: Path) -> str:
    """What the writer said it was (`""` when it did not say)."""
    try:
        return (folder / UPDATE_MARK).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _is_update(folder: Path) -> bool:
    return (folder / UPDATE_MARK).exists()


def never_confirmed(paths: Paths, ulid: str) -> bool:
    """Whether Drive has never confirmed this id (change local-first-writes,
    「寫回還沒被 Drive 確認過的 Session 不算更新既有 id」).

    An outbox entry without `.update` is one nobody ever said Drive already had, and
    still being there is the rest of the answer: it never got there. Writing such a
    session back - a continue's `_finish`, an `edit`, an import that updates the same
    id - is still a *new* session, and marking it `.update` sent the uploader looking
    for an id Drive never heard of, which it then saved as a session of its own
    (design「還沒被 Drive 確認過的 Session，寫回時不算更新既有 id」).

    A `.done-<ULID>` next to it means an uploader is comparing that very id right now,
    and then we cannot tell what Drive has: no exemption. `.old-<ULID>` is the entry too:
    `stage` renames the old one aside for a moment before moving the new one in, so during
    a swap - or after a stage that died in that window - `outbox/<ULID>` alone is not the
    whole answer, and reading it that way brought the bug back (C11). This is read before
    `stage`, which swaps the entry for a fresh folder that never has the marker.
    """
    if (paths.outbox / f".done-{ulid}").exists():
        return False                     # somebody is comparing this id right now
    folder = paths.outbox / ulid
    if not folder.is_dir():
        folder = paths.outbox / f".old-{ulid}"
    if not folder.is_dir():
        return False
    return not _is_update(folder)


class _Listing(dict):
    """{ulid: {file name: md5}} that also knows each folder's Drive ID (`.ids`)."""

    def __init__(self):
        super().__init__()
        self.ids: dict[str, str] = {}
        self.mtimes: dict[str, dict[str, str]] = {}      # {ulid: {name: ModTime}}, for the cleanup grace


def _ids_of(drive: Drive, listing: "_Listing", ulids, say) -> dict[str, str]:
    """The folder IDs of `ulids`: from the listing, and for the ones it did not carry,
    from one `--dirs-only` call. Whoever has no ID after that is missing from the
    result - and whoever needs the ID then fails closed (#164, 2.1)."""
    have = getattr(listing, "ids", {})        # a plain dict (a test double) has none
    ids = {u: have[u] for u in ulids if u in have}
    missing = [u for u in ulids if u not in ids]
    if missing:
        try:
            extra = drive.folder_ids()
        except StoreError as e:
            say(f"補查資料夾 ID 失敗：{e}")
            extra = {}
        ids.update({u: extra[u] for u in missing if u in extra})
    return ids


def _listing_with_md5(drive: Drive):
    """Drive's file list with md5s (and the folder IDs, `.ids`), or the error that stopped us.

    A `None` inside it means Drive has no `sessions/` at all - not a failure, and not
    evidence of a deletion (N12). Whoever needs to tell「離線」from「空的」looks at the
    type, which is why the error comes back instead of being swallowed.
    """
    try:
        return drive.list_sessions_with_ids()
    except StoreError as e:
        return e


@dataclass
class _Look:
    """What one directed look at a folder said (#152, #155).

    `there` is True when rclone listed the folder, False when it says the folder
    itself is not there (rc=3, or the words older rclone builds print), and None when
    the look failed and says nothing either way. `files` is empty unless `there` is
    True, and an empty `files` is not proof of anything (V6).
    """
    there: bool | None
    files: dict[str, str]
    error: StoreError | None = None


def _directed_look(drive: Drive, ulid: str) -> _Look:
    """One look at `sessions/<ulid>`: the one place that tells「不存在」from a failed look.

    rclone says a folder is not there with rc=3 (and, in builds that do not set the
    status, with "directory not found"). Everything else that goes wrong is a look
    that failed, never a deletion: not seeing is not seeing it gone (V6).
    """
    try:
        return _Look(True, drive.list_one(ulid))
    except StoreError as e:
        # The fake says the same words with rc=1; a real rclone can answer either way.
        if e.returncode == 3 or "directory not found" in str(e):
            return _Look(False, {}, e)
        return _Look(None, {}, e)


def _folder_state(look: _Look) -> str:
    """What one directed look says about a session folder (design B.5.7, 2.1).

    gone     rclone said the folder is not there (rc=3 / directory not found) - the
             only answer that means "not there"
    alive    the folder answers and holds a `session.md`
    orphans  the folder answers, holds files, but no `session.md`: what a purge leaves
             when the first big copy after it broke - not a session, nothing to resume
    empty    the folder answers with nothing in it: not proof of anything (#152 B1)
    failed   the look itself failed (timeout, 5xx, permission, offline): says nothing
    """
    if look.there is None:
        return "failed"
    if not look.there:
        return "gone"
    if not look.files:
        return "empty"
    return "alive" if "session.md" in look.files else "orphans"


def _really_gone(drive: Drive, ulid: str, say) -> bool:
    """#152 / #164: is an id a listing left out (or showed without a session.md) gone?

    The one rule for every "is it still there" decision: a listing can lose a folder
    when Drive has a bad day (10-12, 10-21 - two long-running continues were saved as
    new sessions that were still on Drive), so one directed look at that folder decides.
    Only rclone saying the folder is not there, or a folder with files but no
    `session.md` (orphans: nothing a reader could resume, and completing it would
    bring a purged session back), means gone. Everything else waits a round.

    Not seeing is not seeing it gone (V6): a look that fails leaves the entry for the
    next round, exactly like a listing that fails.
    """
    look = _directed_look(drive, ulid)
    state = _folder_state(look)
    if state == "failed":
        say(f"存在檢查 {ulid}：列表裡沒有，直接查也失敗（{look.error}），"
            f"當成看不到，留到下一輪")
        return False
    if state == "gone":
        return True
    if state == "orphans":
        say(f"存在檢查 {ulid}：資料夾裡只剩沒有 session.md 的檔（孤兒），當成不在了")
        return True
    if state == "alive":
        say(f"存在檢查 {ulid}：列表裡沒有，但直接查還在，"
            f"當成 Drive 列表出錯，留到下一輪")
        return False
    say(f"存在檢查 {ulid}：列表裡沒有，直接查是空的，不能確認被刪，留到下一輪")
    return False


def _recreated_by_us(drive: Drive, ulid: str, ours: set[str], say, blocked: list | None = None) -> bool:
    """#164: the folder's ID changed between the re-check and the confirmation - is
    what stands there really our copies landing on a folder that was purged?

    The ID change is the evidence of the purge (it does not move on another machine's
    ordinary cleanup, a `gen+1` or a rename of the files inside - #155's case, now
    covered by the comparison itself). It is still not enough to take a folder down on:
    a directed look must find a folder holding a `session.md` and *only* files this
    round sent (`ours`). A file outside `ours` means somebody else wrote there since -
    a live folder, not the leftovers of our own copy - and an empty, failed or
    not-found answer contradicts the listing that just showed it. All of those wait.

    `blocked`, when given, is filled with the foreign file names for the one wait that does not
    go away by itself: the folder is alive and holds files this round did not send (#173).
    """
    look = _directed_look(drive, ulid)
    state = _folder_state(look)
    if state == "alive":
        others = sorted(set(look.files) - ours)
        if not others:
            return True
        if blocked is not None:
            blocked.extend(others)
        say(f"確認檢查 {ulid}：資料夾 ID 變了，但直接查還有這一輪沒送的檔"
            f"（{', '.join(others)}），資料夾是活的，不清資料夾，留到下一輪")
        return False
    why = {"failed": f"直接查也失敗（{look.error}）",
           "gone": "直接查說資料夾不在，和剛列出的結果矛盾",
           "empty": "直接查是空的",
           "orphans": "直接查沒有 session.md"}[state]
    say(f"確認檢查 {ulid}：資料夾 ID 變了，{why}，不能確認，不清資料夾，留到下一輪")
    return False


def _with_files_from(drive: Drive, names: list[str], where: Path, *args: str) -> None:
    """One rclone call driven by a `--files-from` list, written and cleaned up here."""
    if not names:
        return
    listing = where / f".files-from-{os.getpid()}-{len(names)}.txt"   # G1: two rounds cannot collide
    listing.write_text("\n".join(names) + "\n", encoding="utf-8")
    try:
        drive._run(*args, "--files-from", str(listing))
    finally:
        listing.unlink(missing_ok=True)


def _copy_batch(drive: Drive, paths: Paths, names: list[str],
                source: Path | None = None) -> None:
    """One rclone for a whole batch: `copy --files-from --no-traverse` (design M4).

    `source` is where the listed names are read from - `paths.outbox` unless this is
    the session.md step, which sends the snapshot its raws belonged to (#130).
    """
    where = source if source is not None else paths.outbox
    _with_files_from(drive, names, paths.state, "copy", str(where), "gdrive:sessions",
                     "--no-traverse", "--ignore-times")


def _delete_batch(drive: Drive, paths: Paths, names: list[str]) -> None:
    """The segments this upload made obsolete, gone in one call (design B.5.5)."""
    try:
        _with_files_from(drive, names, paths.state, "delete", "gdrive:sessions")
    except StoreError as e:
        warn(f"清掉舊的段失敗，下次再說：{e}")


def _build_batch(paths: Paths, documents: dict[str, bytes],
                 cuts: dict[str, tuple[Path, list[dict], set[str]]]) -> tuple[Path, dict[str, str]]:
    """This round's batch dir: every entry's session.md (the bytes its md5s were taken
    from, #130) and the segments to send, each cut from the entry's whole raw by the
    sizes the manifest gives and checked against the manifest md5 as it is cut.

    Returns the dir and `{ulid: why}` for the entries whose cut did not match their
    manifest - those are not sent (a version that does not agree with itself is not
    published) and their half-cut files are removed. Every file is stamped with the
    current time before it is returned: rclone gives Drive the file's mtime as its
    `ModTime`, and the orphan grace period is counted from that (K.1, design B.5.2).
    """
    where = paths.state / f"batch-{os.getpid()}"
    paths.state.mkdir(parents=True, exist_ok=True)
    for stale in paths.state.glob("batch-*"):       # every run that died before its cleanup
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    failed: dict[str, str] = {}
    try:
        for ulid, data in documents.items():
            folder = where / ulid
            folder.mkdir(parents=True)
            _write_file(folder / "session.md", data)
        for ulid, (raw_path, manifest, needed) in cuts.items():
            if ulid not in documents:
                continue
            try:
                _cut_segments(raw_path, where / ulid, manifest, needed)
            except (OSError, ValueError) as e:
                failed[ulid] = str(e)
                shutil.rmtree(where / ulid, ignore_errors=True)
        _fault("after-segment-cut")
        for path in where.rglob("*"):
            if path.is_file():
                os.utime(path, None)
    except BaseException:
        shutil.rmtree(where, ignore_errors=True)
        raise
    return where, failed


def _cut_segments(raw_path: Path, into: Path, manifest: list[dict], needed: set[str]) -> None:
    """Write the segments named in `needed` into `into`, cut from `raw_path`; ValueError
    on a mismatch.

    A segment is a byte range of the whole raw: its offset is the sum of the sizes
    before it in the manifest (design B.5.1), so the whole manifest is walked and only
    the needed ones are written.
    """
    offset = 0
    with open(raw_path, "rb") as src:
        for seg in manifest:
            size = seg["size"]
            if seg["file"] in needed:
                if not _is_segment_name(seg["file"]):
                    raise ValueError(f"{seg['file']!r} 不是合法的段名，不寫")
                src.seek(offset)
                digest = hashlib.md5()
                left = size
                with open(into / seg["file"], "wb") as out:
                    while left:
                        block = src.read(min(left, 1 << 20))
                        if not block:
                            raise ValueError(f"整份 raw 比清單短（缺 {seg['file']}）")
                        out.write(block)
                        digest.update(block)
                        left -= len(block)
                if digest.hexdigest() != seg["md5"]:
                    raise ValueError(f"切出來的 {seg['file']} md5 和清單不符")
            offset += size


def _iso_epoch(text: str) -> float | None:
    """An rclone `ModTime` (RFC 3339, any number of fractional digits) as epoch seconds."""
    if not isinstance(text, str) or not text:
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", text.strip())
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _published_path(paths: Paths, ulid: str) -> Path:
    return paths.state / "published" / f"{ulid}.json"


def _folder_record_path(paths: Paths, ulid: str) -> Path:
    return paths.state / "folder-ids" / f"{ulid}.json"


def _read_folder_record(paths: Paths, ulid: str) -> dict | None:
    """`{folder_id, sent}` this machine wrote before it last started copying into the session's
    folder, or None. A missing, unreadable or malformed file is None - the same as "no earlier
    attempt": it can only cost the one comparison it would have made, never a wrong verdict."""
    try:
        data = json.loads(_folder_record_path(paths, ulid).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if (isinstance(data, dict) and isinstance(data.get("folder_id"), str) and data["folder_id"]
            and isinstance(data.get("sent"), list) and all(isinstance(n, str) for n in data["sent"])):
        return data
    return None


def _note_folder_record(paths: Paths, ulid: str, folder_id: str, names: list[str], say) -> None:
    """Remember, before the copies, the folder ID this entry is about to write into and every
    name it has sent there since. An existing record keeps its ID - that old ID is the evidence a
    later round compares with - and only learns the new names. A write that fails only warns."""
    record = _read_folder_record(paths, ulid)
    if record is None:
        record = {"folder_id": folder_id, "sent": []}
    record["sent"] = sorted(set(record["sent"]) | set(names))
    try:
        path = _folder_record_path(paths, ulid)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(record, ensure_ascii=False, sort_keys=True))
    except OSError as e:
        say(f"{ulid} 資料夾 ID 記錄寫不進去（只少了跨輪偵測被刪的那一道）：{e}")


FOLDER_WAIT_ROUNDS = 3


def folder_wait_rounds() -> int:
    """How many rounds in a row an entry may wait on the same folder-ID mismatch before the
    person is told once (`AGORA_FOLDER_ID_WAIT_ROUNDS`, default 3). A background round is minutes
    apart, so three is long enough that another machine's copy in flight has finished and short
    enough that a permanent stand-off is said within the hour. A bad value is the default."""
    try:
        value = int(os.environ.get("AGORA_FOLDER_ID_WAIT_ROUNDS", FOLDER_WAIT_ROUNDS))
    except ValueError:
        return FOLDER_WAIT_ROUNDS
    return value if value >= 1 else FOLDER_WAIT_ROUNDS


def _note_blocked_wait(paths: Paths, ulid: str, record: dict, current_id: str, others: list[str],
                       say, notices: bool) -> None:
    """The entry waited again because the folder's ID changed *and* it holds files that are not
    ours (#173). Count the rounds in the folder record itself - the record is exactly "this
    entry is mid-way into that folder", it is dropped the moment the entry is confirmed, rescued
    or deleted, and a record that cannot be read is no record, so the counter can only be lost
    toward "say it later", never toward a wrong verdict. After `folder_wait_rounds()` rounds
    against the same current ID the person is told once (`warned` is in the record, so the next
    rounds stay quiet); a different current ID starts the count again. Nothing is purged or
    rescued here."""
    waits = record.get("waits") if record.get("wait_id") == current_id else 0
    waits = (waits if isinstance(waits, int) and not isinstance(waits, bool) else 0) + 1
    already = record.get("warned") is True and record.get("wait_id") == current_id
    tell = waits >= folder_wait_rounds() and not already
    updated = dict(record, wait_id=current_id, waits=waits, warned=already or tell)
    try:
        path = _folder_record_path(paths, ulid)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(updated, ensure_ascii=False, sort_keys=True))
    except OSError as e:
        say(f"{ulid} 資料夾 ID 記錄寫不進去（等待輪數記不住）：{e}")
    if not tell:
        return
    line = (f"{ulid} 的更新已經連續 {waits} 輪沒送出：Drive 上這個 Session 的資料夾 ID 和這台上次送出前記的不同"
            f"（可能被別台刪掉又重建過），而且資料夾裡有不是這台送的檔（{', '.join(others)}），"
            f"所以這台不自動清它、也不另存，更新留在 outbox。可以：① 等那台機器用完，這筆會在下一輪自己再判斷；"
            f"② 確定那個資料夾就是現在要用的：刪掉 {_folder_record_path(paths, ulid)}，下一輪就當一般更新送進去"
            f"（後寫的贏，會蓋掉那邊的版本；那個資料夾裡別台的舊段，超過寬限期的也會在送出確認後被清掉）。這則只說一次")
    say(line)
    if notices:
        _notice(paths, line)


def _forget_folder_record(paths: Paths, ulid: str) -> None:
    """Drop the record. A file that cannot be removed only warns: by now the version may be
    out of the outbox, and a permissions problem must not turn that into an exception."""
    try:
        _folder_record_path(paths, ulid).unlink(missing_ok=True)
    except OSError as e:
        warn(f"{ulid} 資料夾 ID 記錄刪不掉（下一輪會再比對一次，不影響正確性）：{e}")


def _read_published(paths: Paths, ulid: str) -> dict | None:
    """What this machine last published for the session: `{session_md5, segments}`."""
    try:
        data = json.loads(_published_path(paths, ulid).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if (isinstance(data, dict) and isinstance(data.get("session_md5"), str)
            and isinstance(data.get("segments"), list)
            and all(isinstance(n, str) for n in data["segments"])):
        return data
    return None


def _clean_segments(drive: Drive, paths: Paths, confirmed: list, remote, again, say) -> None:
    """Design B.5.5, for every entry this round confirmed and took out of the outbox.

    Decided on the confirmation's own listing: keep the manifest just published, the
    previous one this machine recorded (only when it really was what Drive had before
    this round - another machine may have written since), and every `seg-*` whose
    `ModTime` is within `AGORA_RAW_ORPHAN_GRACE` (another machine's segments that are not
    named by a session.md yet). Everything else `seg-*` goes in one call. A failure only
    warns - the next confirmation tries again. Then the new record is written.
    """
    if not confirmed:
        return
    try:
        grace = raw_orphan_grace()
    except StoreError as e:
        say(f"清理跳過（設定有誤）：{e}")
        grace = None
    mtimes = getattr(remote, "mtimes", {})
    deletions: list[str] = []
    _fault("before-cleanup")
    if grace is not None:
        stamp = now()
        for ulid, manifest, files in confirmed:
            keep = {seg["file"] for seg in manifest}
            before = (again.get(ulid) or {}).get("session.md") if isinstance(again, dict) else None
            previous = _read_published(paths, ulid)
            if previous and before and previous["session_md5"] == before:
                keep |= set(previous["segments"])
            for name in files:
                if not name.startswith("seg-") or name in keep:
                    continue
                modified = _iso_epoch(mtimes.get(ulid, {}).get(name, ""))
                if modified is None or stamp - modified < grace:
                    continue                  # fresh, or no time to judge by: not ours to remove
                deletions.append(f"{ulid}/{name}")
        if deletions:
            _delete_batch(drive, paths, deletions)
    _fault("after-cleanup")
    for ulid, manifest, files in confirmed:
        record = {"session_md5": files.get("session.md"),
                  "segments": [seg["file"] for seg in manifest]}
        try:
            _published_path(paths, ulid).parent.mkdir(parents=True, exist_ok=True)
            write_atomic(_published_path(paths, ulid), json.dumps(record, ensure_ascii=False))
        except OSError as e:
            say(f"{ulid} 發布記錄寫不進去（只影響下次清理的保護範圍）：{e}")


def _raw_block(data: bytes) -> object:
    """The `agora.raw` block of a session.md, from its bytes (None when it has none)."""
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    hdr, _ = h.split_document(text)
    return h.agora_of(hdr).get("raw")


def _warn_legacy_once(paths: Paths, ulid: str, say, notices: bool, corrupt: bool = False) -> None:
    """One warning per stuck entry, ever: the id goes in `state/legacy-warned` (ids only).
    A pre-upgrade entry (no `layout`) and a `chunks/1` entry whose manifest is broken are
    worded differently: the second is not "old", and nobody should be told to delete it.
    A record that cannot be written just means it is said again next round."""
    path = paths.state / "legacy-warned"
    try:
        if ulid in path.read_text(encoding="utf-8").split():
            return
    except OSError:
        pass
    if corrupt:
        line = (f"{ulid} 在 outbox 裡的 raw 清單不合法（段名、大小或 md5 有問題），這個版本不上傳它；"
                f"它留在 outbox，內容沒有動。請不要直接 delete——先看一下這個 header 是怎麼壞的")
    else:
        line = (f"{ulid} 在 outbox 裡是升級前的舊格式版本，這個版本不上傳它（會把舊格式寫回 Drive）；"
                f"它留在 outbox，要丟掉請 agora delete，或等遷移處理")
    if notices:
        _notice(paths, line)
    say(line)
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(ulid + "\n")
    except OSError:
        pass


def upload_batch(drive: Drive, paths: Paths, warn=warn, notices: bool = False) -> list[str]:
    """Send the whole outbox in a fixed number of rclone calls (spec「一批只連固定幾次 Drive」).

    raws first, then session.md: a reader tells a version is finished by its session.md,
    so that order is not negotiable. If the raws call is not clean, no session.md goes
    up in that round at all (M4). What may leave the outbox is only what this round sent
    *and* Drive confirms by md5 (H1) - a version staged while we were sending stays.

    Returns the ULIDs still waiting, so the caller can say what did not make it.

    `notices` is for the detached uploader, whose own words go to upload.log: it leaves
    the lines a person has to see in a file for the next command (review G3). In the
    foreground what it says reaches the terminal already, and a file would say it twice.
    """
    say = warn
    left = []
    entries = {}
    documents = {}          # the session.md bytes each entry's md5s were taken from
    manifests = {}          # {ulid: [segment dicts]} of every entry that has a raw
    for ulid in sorted(outbox_ulids(paths)):
        folder = paths.outbox / ulid
        try:
            entries[ulid], documents[ulid] = _entry_for_batch(folder)
            raw_block = _raw_block(documents[ulid])
        except (OSError, h.HeaderError):
            quarantine(folder, paths.outbox / ".bad", f"outbox 的 {ulid} 壞了")
            continue
        if raw_kind(raw_block) == "legacy":
            # A version staged before the upgrade (no `layout`, one whole raw) or one we do
            # not read: never published from here - that would write the old layout back to
            # Drive. Only this entry waits; the rest of the batch goes (design C.3, B.6).
            entries.pop(ulid)
            documents.pop(ulid)
            left.append(ulid)
            _warn_legacy_once(paths, ulid, say, notices,
                              corrupt=isinstance(raw_block, dict) and raw_block.get("layout") == RAW_LAYOUT)
            continue
        if raw_block is not None:
            manifests[ulid] = copy.deepcopy(raw_segments(raw_block))

    updates = [u for u in entries if _is_update(paths.outbox / u)]
    again = None                 # the listing the confirmation compares against (resurrect)
    ids_before = {}              # {ulid: folder ID} from that listing, before the copies
    if updates:
        # L7: an update needs its id to still be there, or we would put back what
        # another machine deleted.
        remote = _listing_with_md5(drive)
        if isinstance(remote, StoreError):
            # V6 (PM): not being able to see is not seeing it there either. Sending now
            # could put back what another machine just deleted, so the updates wait for
            # a round whose listing works; the new ids go up as usual.
            say(f"列不出 Drive 的檔案，{len(updates)} 筆更新留在 outbox 等下一次：{remote}")
            entries = {u: m for u, m in entries.items() if u not in updates}
            left.extend(updates)
            updates = []
        # None: Drive has no sessions/ at all - not evidence of a deletion (N12)
        gone = []
        # A folder with files but no session.md is not a session either (#164, 2.3): it
        # is what a purge leaves when the first big copy after it broke, and completing
        # it would bring the purged session back. `_really_gone` settles both the same way.
        for ulid in (u for u in updates
                     if remote is not None
                     and (u not in remote or "session.md" not in remote[u])):
            if not _really_gone(drive, ulid, say):
                # The listing missed it or the look failed. Do not send it from a list
                # we know is wrong, and do not rescue it: it waits for the next round.
                entries.pop(ulid, None)
                if (paths.outbox / ulid).is_dir():
                    left.append(ulid)
                continue
            gone.append(ulid)
        rescued = {}
        for ulid in gone:
            say(f"存在檢查 {ulid}：不在了")
            rescued[ulid] = _rescue_deleted(paths, paths.outbox / ulid, ulid, say, notices)
            _forget_folder_record(paths, ulid)
        entries = {u: m for u, m in entries.items() if u not in gone}
        # Y went into the outbox while we were here: this round takes it too, so a
        # foreground `push` ends with the edit in Drive rather than still waiting.
        for ulid, new_id in rescued.items():
            if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                _adopt_entry(paths, new_id, entries, documents, manifests)
        # X still in the outbox was not rescued: an edit landed while we were reading it,
        # or we could not read it. Either way it is still waiting - the next round meets
        #「.update 而 Drive 沒有」again - and Y goes up in this one (V1).
        left.extend(u for u in gone if (paths.outbox / u).is_dir())

    if any(u in entries for u in updates):
        # The listing above decided these ids were still there, but the copies below
        # only land after a whole batch of them - and another machine may delete one
        # in between, which would put the session back (review resurrect). So look
        # once more, right here, and rescue on the spot whatever is already gone;
        # the window shrinks to this one listing plus the copies, which are small
        # and fast now that a pass sends only what each entry names (review N1).
        # What is still left over - one call between this look and the copies - is
        # written down in the design's Risks; the next round's L7 catches it.
        again = _listing_with_md5(drive)
        if isinstance(again, StoreError):
            # Same rule as above (V6): a listing that fails is not seeing it there
            # either, so the updates wait for a round that can see.
            pending = [u for u in updates if u in entries]
            say(f"上傳前再列一次 Drive 失敗，{len(pending)} 筆更新留在 outbox 等下一次：{again}")
            for u in pending:
                entries.pop(u)
                left.append(u)
        elif again is None:
            # Drive has no sessions/ at all: not evidence of a deletion (N12), and no folder ID to
            # work with this round. A record from an earlier round would then be compared with the
            # ID of a folder *we* are about to re-create, and read as "purged and re-created by
            # us" - taking a live session down. So the stale records go; this round writes none
            # (no ID), and a later round starts from the folder as it is then.
            for u in [u for u in updates if u in entries]:
                _forget_folder_record(paths, u)
        elif isinstance(again, dict):
            for ulid in [u for u in updates if u in entries]:
                if ulid not in again or "session.md" not in again[ulid]:
                    if not _really_gone(drive, ulid, say):
                        # Same rule as the first check (#152): one listing missing an
                        # id is not proof, and a failed look is not either. It waits.
                        entries.pop(ulid, None)
                        if (paths.outbox / ulid).is_dir():
                            left.append(ulid)
                        continue
                    say(f"存在檢查 {ulid}：不在了")
                    new_id = _rescue_deleted(paths, paths.outbox / ulid, ulid, say, notices)
                    _forget_folder_record(paths, ulid)
                    entries.pop(ulid, None)
                    if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                        _adopt_entry(paths, new_id, entries, documents, manifests)
                    if (paths.outbox / ulid).is_dir():
                        left.append(ulid)
                else:
                    say(f"存在檢查 {ulid}：還在，這一輪送出")
            # The folder IDs the confirmation compares against (#164, 2.2). An update
            # whose folder has no ID we can read is not sent: without it the copy window
            # has no purge detection, so it waits for a round that can see (fail-closed).
            staying = [u for u in updates if u in entries]
            ids_before = _ids_of(drive, again, staying, say)
            for ulid in staying:
                if ulid not in ids_before:
                    say(f"{ulid} 拿不到資料夾 ID，沒辦法偵測 copy 期間被刪，"
                        f"這筆更新留在 outbox 等下一次")
                    entries.pop(ulid, None)
                    if (paths.outbox / ulid).is_dir():
                        left.append(ulid)
                    continue
                # An earlier round of this entry died (or could not confirm) after it started
                # copying: the folder ID it was aimed at is on disk. A different ID now means the
                # folder was purged and re-created since - by our own copy landing on a deleted
                # session - and nothing else would tell, because what stands there looks alive
                # (#166, design B.5.7). Same rule as the confirmation: only our own files on it
                # and the directed look agree -> take it down and keep the work as a session of
                # its own; anything else waits.
                record = _read_folder_record(paths, ulid)
                if record is not None and record["folder_id"] != ids_before[ulid]:
                    ours = ({"session.md"} | {seg["file"] for seg in manifests.get(ulid, [])}
                            | set(record["sent"]))
                    blocked: list[str] = []
                    if not _recreated_by_us(drive, ulid, ours, say, blocked):
                        if blocked:
                            _note_blocked_wait(paths, ulid, record, ids_before[ulid], blocked, say, notices)
                        entries.pop(ulid, None)
                        if (paths.outbox / ulid).is_dir():
                            left.append(ulid)
                        continue
                    say(f"{ulid} 上一輪送到一半時資料夾被別台刪掉又被我們重建，剛寫回去的資料夾已清掉")
                    try:
                        drive._run("purge", f"gdrive:sessions/{ulid}")
                    except StoreError as e:
                        # The old folder may still be there: rescuing now would leave X alive
                        # on Drive AND save the work as Y. Keep the record and the entry; the
                        # next round judges again.
                        say(f"{ulid} 清掉剛寫回去的資料夾失敗，不另存，留在 outbox 下一輪再判斷：{e}")
                        entries.pop(ulid, None)
                        if (paths.outbox / ulid).is_dir():
                            left.append(ulid)
                        continue
                    new_id = _rescue_deleted(paths, paths.outbox / ulid, ulid, say, notices)
                    entries.pop(ulid, None)
                    _forget_folder_record(paths, ulid)
                    if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                        _adopt_entry(paths, new_id, entries, documents, manifests)
                    if (paths.outbox / ulid).is_dir():
                        left.append(ulid)
        # None: Drive has no sessions/ at all - not evidence of a deletion (N12).

    # What Drive already has of each entry's segments (only an update was listed above): the
    # rest is cut from the entry's whole raw into this round's batch dir. Segments are
    # content-addressed, so a name that is there with the right md5 is not sent again.
    def on_drive(ulid: str) -> dict:
        if isinstance(again, dict) and ulid in updates:
            return again.get(ulid) or {}
        return {}

    sessions: list[str] = []
    cuts = {u: [seg for seg in manifests.get(u, []) if on_drive(u).get(seg["file"]) != seg["md5"]]
            for u in entries}
    for u in entries:
        if u in updates and u in ids_before:
            _note_folder_record(paths, u, ids_before[u],
                                ["session.md", *[seg["file"] for seg in cuts[u]]], say)
    batch_dir = None
    if entries:
        try:
            batch_dir, cut_failed = _build_batch(
                paths, documents,
                {u: (paths.outbox / u / entries[u][1], manifests[u], {seg["file"] for seg in cuts[u]})
                 for u in entries if cuts[u]})
        except OSError as e:
            # A full disk is not the round's end either (R1, review N2): every entry stays,
            # and the next round with room tries again.
            say(f"批次暫存寫不進 state：{e}，這一輪不傳")
            return sorted(set(entries) | set(left))
        for ulid, why in cut_failed.items():
            say(f"{ulid} 切段失敗（{why}），這一筆不送，留在 outbox")
            entries.pop(ulid, None)
            left.append(ulid)
    try:
        segment_files = [f"{u}/{seg['file']}" for u in entries for seg in cuts[u]]
        if segment_files:
            try:
                _copy_batch(drive, paths, segment_files, source=batch_dir)
            except StoreError as e:
                say(f"段沒傳好，這一輪不傳 session.md：{e}")
                return sorted(set(entries) | set(left))
            say(f"本輪送出的段：{', '.join(sorted({r.split('/', 1)[0] for r in segment_files}))}")
            _fault("after-segment-upload")      # segments up, session.md not: the new ones are orphans
        # #130: session.md comes from the bytes read above, written where rclone reads
        # exactly that version. The segments an entry's manifest names are all on Drive
        # now - already there, or sent a moment ago - so its session.md may go.
        sessions = list(entries)
        if sessions:
            try:
                _copy_batch(drive, paths, [f"{u}/session.md" for u in sessions],
                            source=batch_dir)
            except StoreError as e:
                say(f"session.md 沒傳好：{e}")
                return sorted(set(entries) | set(left))
            say(f"本輪送出的 session.md：{', '.join(sorted(sessions))}")
            _fault("after-session-upload")      # session.md up, nothing confirmed yet
    finally:
        if batch_dir is not None:
            shutil.rmtree(batch_dir, ignore_errors=True)

    remote = _listing_with_md5(drive)
    listed = isinstance(remote, dict)
    ids_after = None             # {ulid: folder ID} now; read once, only if an update needs it
    if not listed:
        remote = {}           # nothing confirmed, so nothing leaves the outbox
    confirmed = []               # (ulid, manifest, the folder's listing) of what left the outbox
    for ulid, (sent_md5, sent_name, sent_raw) in entries.items():
        folder = paths.outbox / ulid
        files = remote.get(ulid) or {}
        if files.get("session.md") != sent_md5:
            left.append(ulid)     # not confirmed, or changed underneath us: it stays
            if not listed:
                # Nothing could be confirmed (no `sessions/` listing at all): it stays, and
                # the reason is said (review N3).
                say(f"Drive 上沒有 sessions/ 可以確認，{ulid} 留在 outbox")
            else:
                now_md5 = _outbox_session_md5(folder)
                if now_md5 is not None and now_md5 != sent_md5:
                    say(f"{ulid} 上傳期間又有新版本，留在 outbox 下一輪再送")
                else:
                    say(f"{ulid} Drive 的 md5 與本機不符（session.md 或 raw），留在 outbox")
            continue
        if ulid in updates and isinstance(again, dict) and ulid in ids_before:
            # Was the folder purged while the copies were in flight? Its ID says so:
            # a purge then our copy re-creating it gives a new one (K.1, real Drive),
            # while another machine's ordinary cleanup - a replaced raw, a `gen+1` -
            # leaves it alone. The file names in it prove nothing (#164 replaces #155's
            # name comparison with this).
            if ids_after is None:
                ids_after = _ids_of(drive, remote,
                                    [u for u in ids_before if u in entries], say)
            now_id = ids_after.get(ulid)
            if now_id is None:
                left.append(ulid)
                say(f"{ulid} 確認時拿不到資料夾 ID，不能確認 copy 期間沒被刪，留在 outbox 下一輪再送")
                continue
            # everything this round put there: session.md and the segments of the manifest
            ours = {"session.md"} | {seg["file"] for seg in manifests.get(ulid, [])}
            if now_id != ids_before[ulid] and not _recreated_by_us(drive, ulid, ours, say):
                # The ID moved but the folder does not look like our own copy landing on
                # a purged one, or the look could not say: waiting costs a round, taking
                # a live folder down forks the session.
                if (paths.outbox / ulid).is_dir():
                    left.append(ulid)
                continue
            if now_id != ids_before[ulid]:
                # The folder is not the one we looked at before the copies: it was
                # purged meanwhile, and the copies just landed on the folder they
                # re-created (review resurrect). Another machine's delete stands - take
                # our copy down, and keep the work as a session of its own.
                say(f"{ulid} copy 期間被別台刪掉，剛寫回去的資料夾已清掉")
                try:
                    drive._run("purge", f"gdrive:sessions/{ulid}")
                except StoreError as e:
                    # Same rule as the cross-round path: no purge, no rescue.
                    say(f"{ulid} 清掉剛寫回去的資料夾失敗，不另存，留在 outbox 下一輪再判斷：{e}")
                    left.append(ulid)
                    continue
                new_id = _rescue_deleted(paths, folder, ulid, say, notices)
                _forget_folder_record(paths, ulid)
                if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                    left.append(new_id)          # Y goes up on the next pass
                if (paths.outbox / ulid).is_dir():
                    left.append(ulid)
                continue
        if any(files.get(seg["file"]) != seg["md5"] for seg in manifests.get(ulid, [])):
            # session.md is up but a segment it names is not (or has another md5): the
            # version is not whole on Drive. It stays; the next round cuts the missing
            # ones from the whole raw again (design B.5.4, B.6).
            left.append(ulid)
            say(f"{ulid} 的段在 Drive 上缺或 md5 不符，留在 outbox 下一輪補回")
            continue
        done = paths.outbox / f".done-{ulid}"
        try:
            folder.rename(done)                      # H1: compare what is *now* in there
        except OSError:
            left.append(ulid)                       # it may be gone, or a stage took it
            say(f"{ulid} 這一輪沒能確認（可能是新版本取代了它），留在 outbox 下一輪再送")
            continue
        try:
            now_md5, now_raw_name, now_raw = _entry_md5s(done)
        except (OSError, h.HeaderError) as e:        # R1: one bad entry is not the round's end
            warn(f"{ulid} 驗證不了，留下一次再說：{e}")
            _put_back(done, folder)
            left.append(ulid)
            continue
        if now_md5 != sent_md5 or (sent_raw and now_raw != sent_raw):
            _put_back(done, folder)              # a newer version: it stays for the next round
            left.append(ulid)
            if now_md5 != sent_md5:
                say(f"{ulid} 上傳期間又有新版本，留在 outbox 下一輪再送")
            else:
                say(f"{ulid} Drive 的 md5 與本機不符（session.md 或 raw），留在 outbox")
            continue
        if folder.exists():
            # R1: the edit landed after we renamed our copy aside. What we sent is on
            # Drive and confirmed, so the folder we compared can go; the outbox entry
            # is a newer version, so this ULID is still waiting either way.
            shutil.rmtree(done, ignore_errors=True)
            left.append(ulid)
            say(f"{ulid} 上傳期間又有新版本，留在 outbox 下一輪再送")
            continue
        shutil.rmtree(done, ignore_errors=True)
        _forget_folder_record(paths, ulid)          # confirmed: the folder is the one we think
        if ulid in manifests:
            confirmed.append((ulid, manifests[ulid], files))
    _fault("after-confirm")        # confirmed and out of the outbox, nothing cleaned yet
    _clean_segments(drive, paths, confirmed, remote, again, say)
    return sorted(left)


def _adopt_entry(paths: Paths, ulid: str, entries: dict, documents: dict, manifests: dict) -> None:
    """Take an outbox entry that appeared during the round (a rescued Y) into this round -
    its manifest too, or its session.md would go up without its segments."""
    entries[ulid], documents[ulid] = _entry_for_batch(paths.outbox / ulid)
    raw_block = _raw_block(documents[ulid])
    if raw_block is not None:
        manifests[ulid] = copy.deepcopy(raw_segments(raw_block))


def _rescue_deleted(paths: Paths, folder: Path, ulid: str, say, notices: bool = False) -> str | None:
    """N5: the session Drive no longer has - keep this edit as a session of its own.

    Returning the old version to that id would undo the other machine's deletion, so
    the content becomes a *new* session (new ULID, parents pointing at X) and X's
    outbox entry goes away with it (spec「只有自己驗過的版本離開 outbox」). Returns the
    new ULID, or None when even that failed - then X stays, so nothing is lost.
    """
    kind = update_kind(folder)   # read before the claim: the marker goes with the folder
    done = paths.outbox / f".done-{ulid}"
    try:
        folder.rename(done)      # V1: claim it before reading, like H1 does - what we
    except OSError:
        return None             # rescue is whatever we took, never a version a `stage`
                                # swaps in while we work (that one waits for the next round)
    try:
        text = (done / "session.md").read_text(encoding="utf-8")
        hdr, body = h.split_document(text)
        named = h.agora_of(hdr).get("raw")
        name = raw_name(named) if named else None
        raw = (done / name) if name else None      # the path: an 800 MB raw is not read into memory here
    except (OSError, h.HeaderError, StoreError) as e:
        say(f"{ulid} 雲端沒有，而且讀不出這筆（{e}）；留在 outbox 等你處理")
        _put_back(done, folder)
        return None
    index = Index(paths)
    agora = h.agora_of(hdr)
    source = agora.get("source") or {}
    if source.get("session_id"):
        for child in index.children(ulid):
            chdr = index.header(child) or {}
            cagora = h.agora_of(chdr)
            csource = cagora.get("source") or {}
            if (cagora.get("relation") == "continue"
                    and csource.get("agent") == source.get("agent")
                    and csource.get("session_id") == source.get("session_id")):
                shutil.rmtree(done, ignore_errors=True)
                line = f"{ulid} 已被別台刪除，這次的修改沿用已另存的 {child}"
                if notices:
                    _notice(paths, line)
                say(line)
                return child
    new_ulid = h.new_ulid()
    new_hdr = copy.deepcopy(hdr)
    # A continue is the one write that is a relation of its own; an edit is not, and
    # keeps X's (spec「continue 另存的用 continue，其他沿用」, review G2)
    if kind == "continue":
        new_hdr["agora"]["relation"] = "continue"
    new_hdr["id"] = f"agora:{new_ulid}"
    # Y continues the line: X's own parents, then X (spec「parents 指向 X」, review V3)
    new_hdr["agora"]["parents"] = [*(h.agora_of(hdr).get("parents") or []),
                                   {"id": f"agora:{ulid}"}]
    new_hdr["agora"].pop("raw", None)           # stage names the raw and hashes it again
    try:
        y = stage(paths, new_hdr, body, raw)
    except OSError as e:
        say(f"{ulid} 另存新 Session 失敗（{e}）；留在 outbox 等你處理")
        _put_back(done, folder)
        return None
    remember(paths, y)     # V2: Y is a session of this machine now - mirror and index,
                           # or it would only exist on Drive (P1 all over again)
    shutil.rmtree(done, ignore_errors=True)   # only the version we claimed; if an edit
                                              # landed in `folder` it stays for the next round
    line = f"{ulid} 已被別台刪除，這次的修改存成了 {new_ulid}"
    if notices:
        _notice(paths, line)
    say(line)
    return new_ulid


def _notice(paths: Paths, line: str) -> None:
    """Leave a line for the next command to say out loud.

    The background's own output goes to upload.log, which nobody reads: without this the
    user edits X, X is gone on Drive, and the new session is a name they never see
    (spec「下一個指令提醒」, review V4).
    """
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(paths.state / "notices", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def take_notices(paths: Paths) -> list[str]:
    """The notices waiting to be said, once: saying them is the whole point, so they go."""
    path = paths.state / "notices"
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return []
    path.unlink(missing_ok=True)
    return lines


def _entry_snapshot(folder: Path) -> tuple[str, str | None, bytes]:
    """session.md's md5, the raw it names, and its own bytes - all from one read.

    The uploader sends the bytes it hashed; reading session.md twice let a `stage` in
    between give the md5 of one version and the header of another (#130). The header
    is checked by `_entry_header`, the same one `read_entry` uses (review W1).
    """
    data = _entry_bytes(folder)
    _, name = _entry_header(folder, data)
    return hashlib.md5(data).hexdigest(), name, data


def _entry_for_batch(folder: Path) -> tuple[tuple[str, str | None, str | None], bytes]:
    """The entry as this round reads it, and the session.md bytes those md5s describe."""
    session_md5, name, data = _entry_snapshot(folder)
    return (session_md5, name, md5_file(folder / name) if name else None), data


def _outbox_session_md5(folder: Path) -> str | None:
    """session.md's md5 right now, or None when it cannot be read (it may be gone)."""
    try:
        return hashlib.md5((folder / "session.md").read_bytes()).hexdigest()
    except OSError:
        return None


def _entry_md5s(folder: Path) -> tuple[str, str | None, str | None]:
    """(session.md md5, the raw's file name and md5 - both None when there is no raw)."""
    session_md5, name, _ = _entry_snapshot(folder)
    return session_md5, name, md5_file(folder / name) if name else None



def _named_raw(folder: Path) -> str | None:
    """The raw this entry's session.md points at, or None (no entry, no raw, unreadable).

    An entry may also hold the raw of the version it replaced (`stage` keeps that one
    for an uploader that listed it a moment ago); this is the one that belongs to it.
    """
    try:
        named = h.agora_of(read_entry(folder)).get("raw")
        return raw_name(named) if named else None
    except (h.HeaderError, StoreError):
        return None


def _put_back(done: Path, folder: Path) -> None:
    """Undo a `.done-` rename: a version already in `folder` wins, otherwise the one we
    compared goes back where it was (N7). Used by the uploader comparing, by the
    rescue saving a session under a new id, and by the next command tidying up."""
    if folder.exists():
        shutil.rmtree(done, ignore_errors=True)
        return
    try:
        done.rename(folder)
    except OSError:
        # G6: only a newer version in its place makes ours safe to drop (a stage that
        # swapped in just now). Anything else - permissions and the like - keeps it
        # set aside, where it still counts as waiting (K1) and the next uploader tries.
        if folder.exists():
            shutil.rmtree(done, ignore_errors=True)


def push_outbox(drive: Drive, paths: Paths, warn=warn, notices: bool = False) -> list[str]:
    """Send the outbox in one batch; return the ULIDs still waiting (they stay).

    `warn` is where the per-entry failures go when a caller wants its own sink -
    the interactive mode's waiting window, which does not see our stderr (review L5).
    """
    return upload_batch(drive, paths, warn=warn, notices=notices)


def bad_count(paths: Paths) -> int:
    """Broken entries set aside in outbox/.bad and pending/.bad (R2)."""
    return sum(len(list(d.iterdir())) for d in (paths.outbox / ".bad", paths.pending / ".bad") if d.exists())


def quarantine(path: Path, bad_dir: Path, message: str) -> None:
    """Move a broken local file aside instead of deleting it; it may hold the only copy."""
    bad_dir.mkdir(parents=True, exist_ok=True)
    target = bad_dir / path.name
    if target.exists():
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    path.rename(target)
    warn(f"{message}（移到 {target}）")


# ---------------------------------------------------------------------------
# Mirror and index
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


#: How long ONE index write waits for another process's write (#222). A write is a few rows, so
#: the wait is almost always milliseconds; the bound is for a stuck writer, and past it the error
#: is raised for the caller to decide (see `remember`). A call with several writes can wait this
#: long for each.
INDEX_BUSY_MS = 30_000
#: Under `<cache>/`: one empty file per session `remember` could not index (see `index_pending`).
PENDING_INDEX = "pending-index"
_ULID_NAME = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_INDEX_TABLES = {"sessions", "fts", "cloud_missing"}


class Index:
    def __init__(self, paths: Paths):
        paths.cache.mkdir(parents=True, exist_ok=True)
        path = paths.cache / "index.sqlite"
        if path.exists():
            self._connect(path)
            if self._ready():       # the common case writes nothing: no DDL, no lock
                return
        # Setting up - and filling a new index from the mirror - is one step under the lock, and
        # `user_version` is written last: a second process either waits here or sees an index
        # that is not ready yet, never a half-filled one that looks done.
        with open(paths.cache / "index.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not path.exists():
                # A deleted index (the way to rebuild it) can leave its -wal / -shm behind, and a
                # new database must not inherit them.
                for stale in ("-wal", "-shm"):
                    path.with_name(path.name + stale).unlink(missing_ok=True)
            if not hasattr(self, "db"):
                self._connect(path)
            self._to_wal()      # only here: switching needs the database to itself
            if self._ready():       # another process finished while we waited
                return
            bumped = self.db.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION
            with self._tx():
                if bumped:
                    for table in sorted(_INDEX_TABLES):    # not executescript: it would COMMIT first
                        self.db.execute(f"DROP TABLE IF EXISTS {table}")
                self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                    ulid TEXT PRIMARY KEY, md5 TEXT, header TEXT, body TEXT,
                    agent TEXT, source_id TEXT, created TEXT)""")
                self.db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                    ulid UNINDEXED, text, tokenize='trigram')""")
                self.db.execute("CREATE TABLE IF NOT EXISTS cloud_missing (ulid TEXT PRIMARY KEY)")
            if bumped:
                self.rebuild_from_mirror(paths)
            with self._tx():
                self.db.execute(f"PRAGMA user_version = {INDEX_VERSION}")

    def _connect(self, path: Path) -> None:
        # Autocommit: a transaction is only what `_tx` opens, so no statement holds a lock by accident.
        # `timeout` is SQLite's busy handler: the one bound on how long a write waits (INDEX_BUSY_MS).
        self.db = sqlite3.connect(path, timeout=INDEX_BUSY_MS / 1000, isolation_level=None)
        self.db.execute("PRAGMA synchronous = NORMAL")

    def _to_wal(self) -> None:
        """WAL: readers and the one writer do not block each other, writers queue. Leaves
        index.sqlite-wal / -shm beside the file; they go with it (it is a cache: delete all three).

        The mode is kept in the file, so this is for a new index or one an older agora left in
        rollback-journal mode - and only under the build lock. A reader holding the index is
        waited out by SQLite's busy handler; a writer holding the write lock (an older agora
        mid-write) makes the switch fail at once, so that case is retried here. Every attempt
        gets only the time left, so the whole thing stays within INDEX_BUSY_MS.
        """
        deadline = time.monotonic() + INDEX_BUSY_MS / 1000
        while True:
            left = deadline - time.monotonic()
            self.db.execute(f"PRAGMA busy_timeout = {max(int(left * 1000), 1)}")
            try:
                self.db.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        self.db.execute(f"PRAGMA busy_timeout = {INDEX_BUSY_MS}")

    def _ready(self) -> bool:
        """The right version with all its tables: nothing to create."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:
            return False
        if self.db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            return False        # left by an older agora: switched under the lock, data kept
        have = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return _INDEX_TABLES <= have

    @contextlib.contextmanager
    def _tx(self):
        """One short write transaction, taking the write lock first (`BEGIN IMMEDIATE`).

        Waits up to `INDEX_BUSY_MS` for the lock, then `sqlite3.OperationalError` is raised.
        Taking the lock before the first read is what makes delete-then-insert one step: two
        processes indexing the same ulid cannot interleave into a UNIQUE failure.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.db.in_transaction:      # SQLite rolls some errors back by itself
                self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def rebuild_from_mirror(self, paths: Paths) -> "Index":
        """Fill an index that has just been emptied (a version bump, a deleted db)
        from the mirror, which is the local truth and needs no Drive (review M3)."""
        for md in sorted(paths.mirror.glob("*/session.md")):
            try:
                index_file(self, md)
            except (h.HeaderError, OSError, UnicodeDecodeError):
                continue
        return self

    def known(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT ulid, md5 FROM sessions"))

    def put(self, ulid: str, md5: str, header: dict, body: str) -> None:
        source = h.agora_of(header).get("source") or {}
        text = normalize("\n".join([
            str(header.get("title") or ""), str(header.get("description") or ""),
            " ".join(map(str, header.get("tags") or [])), " ".join(map(str, header.get("refs") or [])), body,
        ]))
        with self._tx():
            self._delete(ulid)
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                (ulid, md5, json.dumps(header, ensure_ascii=False, default=str), text,
                 source.get("agent"), source.get("session_id"), sort_date(header)))
            self.db.execute("INSERT INTO fts VALUES (?,?)", (ulid, text))

    def drop(self, ulid: str) -> None:
        with self._tx():
            self._delete(ulid)

    def _delete(self, ulid: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE ulid=?", (ulid,))
        self.db.execute("DELETE FROM fts WHERE ulid=?", (ulid,))
        self.db.execute("DELETE FROM cloud_missing WHERE ulid=?", (ulid,))

    def mark_missing(self, ulids) -> None:
        """Which sessions Drive does not have.

        Only ever called with a complete listing (Q4): a half-read drive must not
        move a single marker, or an offline machine would declare the whole
        library deleted on the next start.
        """
        with self._tx():
            self.db.execute("DELETE FROM cloud_missing")
            self.db.executemany("INSERT OR IGNORE INTO cloud_missing VALUES (?)",
                                [(ulid,) for ulid in sorted(ulids)])

    def cloud_has(self, ulid: str) -> bool:
        """Whether Drive still has this session (T1 R6). One we never indexed counts as local."""
        return self.db.execute("SELECT 1 FROM cloud_missing WHERE ulid=?", (ulid,)).fetchone() is None

    def missing_in_cloud(self) -> list[str]:
        """The sessions another machine deleted: kept here, searchable, marked."""
        return [r[0] for r in self.db.execute("SELECT ulid FROM cloud_missing ORDER BY ulid")]

    def header(self, ulid: str) -> dict | None:
        row = self.db.execute("SELECT header FROM sessions WHERE ulid=?", (ulid,)).fetchone()
        return h.upgrade(json.loads(row[0])) if row else None   # rows cached before v4 (V1)

    def by_source(self, agent: str, source_id: str) -> list[str]:
        """Sessions imported from this agent session, newest first.

        A session Drive no longer has is not among them (T1 3.4): importing that
        agent session again has to make a new one rather than write over the copy
        another machine deleted.
        """
        rows = self.db.execute(
            "SELECT ulid FROM sessions WHERE agent=? AND source_id=? "
            "AND ulid NOT IN (SELECT ulid FROM cloud_missing) ORDER BY ulid DESC",
            (agent, source_id))
        return [r[0] for r in rows]

    def children(self, ulid: str) -> list[str]:
        """Sessions that continue or merge from this one (refs alone do not count).

        A session Drive no longer has is not a child (T1 3.4): it must not hold up
        a delete here, and it must not make the import tab think this one already
        branched.
        """
        target = f"agora:{ulid}"
        rows = self.db.execute("SELECT ulid, header FROM sessions")
        return [u for u, hdr in rows if self.cloud_has(u)
                and any(p.get("id") == target
                        for p in h.agora_of(h.upgrade(json.loads(hdr))).get("parents") or [])]

    def search(self, filters: list[tuple[tuple[str, ...], str, str]]) -> list[tuple[str, dict, str]]:
        """[(ulid, header, snippet)] matching every filter, newest source first.

        A ("text",) filter matches the reading version plus the header's text
        fields; the first one picks candidates through FTS5 (or a scan when
        it is shorter than three characters, which trigram cannot match).
        """
        texts = [normalize(value) for path, _op, value in filters if path == (h.TEXT_KEY,)]
        others = [f for f in filters if f[0] not in ((h.TEXT_KEY,), ("cloud",))]
        # `cloud=no|yes` asks about the marker, which is not in the header (T1 3.4)
        clouds = {value for path, _op, value in filters if path == ("cloud",)}
        kw = texts[0] if texts else ""
        if not kw:
            rows = self.db.execute("SELECT ulid, header, body FROM sessions")
        elif len(kw) < 3:
            rows = self.db.execute(
                "SELECT ulid, header, body FROM sessions WHERE instr(body, ?) > 0", (kw,))
        else:
            phrase = '"' + kw.replace('"', '""') + '"'
            rows = self.db.execute(
                "SELECT s.ulid, s.header, s.body FROM fts JOIN sessions s USING (ulid) "
                "WHERE fts MATCH ?", (phrase,))
        hits = []
        for ulid, hdr_json, body in rows:
            if clouds and (("no" in clouds) == self.cloud_has(ulid)):
                continue        # the marker says the other thing
            hdr = h.upgrade(json.loads(hdr_json))
            if all(t in body for t in texts[1:]) and all(_matches(hdr, *f) for f in others):
                hits.append((ulid, hdr, _snippet(body, kw)))
        hits.sort(key=lambda hit: (sort_date(hit[1]), str(h.agora_of(hit[1]).get("updated_at")), hit[0]),
                  reverse=True)
        return hits


def sort_date(header: dict) -> str:
    """When the conversation happened: the source session, else generated.at, else the import."""
    agora = h.agora_of(header)
    generated = header.get("generated") if isinstance(header.get("generated"), dict) else {}
    return str((agora.get("source") or {}).get("created_at") or generated.get("at") or agora.get("created_at") or "")


def _matches(hdr: dict, path: tuple[str, ...], op: str, value: str) -> bool:
    node = hdr
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    items = node if isinstance(node, list) else [node]
    for item in items:
        if item is None:
            continue
        if op == "=" and str(item) == value:
            return True
        if op == "~=" and normalize(value) in normalize(str(item)):
            return True
    return False


def _snippet(body: str, kw: str, width: int = 30) -> str:
    pos = body.find(kw) if kw else 0
    pos = max(pos, 0)
    start = max(0, pos - width)
    text = body[start:pos + len(kw) + width].replace("\n", " ")
    return ("…" if start else "") + text + ("…" if pos + len(kw) + width < len(body) else "")


def continuing(paths: Paths, ulid: str) -> bool:
    """A continue is running on this session right now (T1 3.1, review G1).

    The record's own lock, not its existence: a run whose agent is gone, or one
    whose `_finish` keeps failing, leaves the record behind for good, and a file
    that merely exists would keep that session from being marked, from being
    pulled away, and from being deleted - for ever.
    """
    path = paths.pending / f"{ulid}.json"
    if not path.exists():
        return False
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True        # somebody holds it: a live continue (C1)
    return False           # nobody does: what is left of an interrupted one


def continuing_details(paths: Paths, ulid: str) -> dict | None:
    """Who is continuing this Session right now, from the winner's pending record (#90).

    `continuing` stays the authority on yes/no: a record nobody holds is a leftover,
    not a run, and must not be described as one. When somebody does hold it, this
    answers the record's own fields (`agent`, `dir`, `started_at`) so a refusal can
    say who is in there; a record that cannot be read answers `{}` - the same thing the
    old message said, "somebody is continuing", and nothing more.
    """
    if not continuing(paths, ulid):
        return None
    try:
        record = json.loads((paths.pending / f"{ulid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def kick_uploader(paths: Paths) -> str | None:
    """Make sure something is looking after the outbox and the trash queue.

    The command that notices them does not send them itself: a sync in the foreground
    is the wait T3 is here to remove, and after a failed upload the first `search` would
    be slow again (review N3). Not being able to start one means one is already running,
    and it looks again before it lets the lock go.

    Returns what the starter said, so the caller can word its reminder by what is
    actually true: somebody uploading, or nothing running and entries still here
    (review P5). None when there was nothing to do.
    """
    from agora import background        # imported here: background imports this module
    if waiting_ulids(paths) or queued_for_trash(paths):   # K1: set-aside ones too
        return background.start(paths)
    return None


def _unfinished(paths: Paths) -> dict[str, dict]:
    """What sync last saw for sessions whose raw was not whole, or not readable (#119).

    A record is `{session_md5, raw_manifest, raw_fingerprint, legacy}`. For a raw that
    is still being written: the same session.md and the same fingerprint (the manifest's
    (file, md5) with what Drive lists for each now) mean downloading again could only
    reach the same "not written yet" - so it does not. For a raw this version does not
    read (`legacy: true`, design E.1): the session is indexed and kept, and the record
    only says that nobody needs to look at it again until its session.md changes.
    A missing, broken or version-1 file is the same as no memory: the next sync
    downloads once and writes it again.
    """
    try:
        data = json.loads((paths.state / UNFINISHED_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 2:
        return {}
    entries = data.get("sessions")
    if not isinstance(entries, dict):
        return {}
    found: dict[str, dict] = {}
    for ulid, record in entries.items():
        if not isinstance(ulid, str) or not isinstance(record, dict):
            continue
        manifest = record.get("raw_manifest")
        if (isinstance(record.get("session_md5"), str)
                and isinstance(record.get("raw_fingerprint"), str)
                and isinstance(manifest, list)
                and all(isinstance(row, list) and len(row) == 2
                        and all(isinstance(x, str) for x in row) for row in manifest)):
            found[ulid] = {"session_md5": record["session_md5"], "raw_manifest": manifest,
                           "raw_fingerprint": record["raw_fingerprint"],
                           "legacy": record.get("legacy") is True}
    return found


def _write_unfinished(paths: Paths, entries: dict[str, dict]) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    write_atomic(paths.state / UNFINISHED_FILE,
                 json.dumps({"version": 2, "sessions": entries},
                            ensure_ascii=False, sort_keys=True))


def _folder_files(remote: dict, ulid: str) -> dict:
    """The listing of one session folder (`{}` when Drive lists none)."""
    return remote.get(ulid) or {}


def sync(paths: Paths, drive: Drive | None = None, *, throttle: bool = False,
         kick: bool = True, warn=warn) -> Index:
    """Start the uploader if anything is waiting, then pull the session.md files whose md5 changed.

    `kick=False` is for a command that is about to take an entry out of the outbox
    itself: `delete` must not have the background send the version it is deleting
    (spec「改完馬上刪」, review W1). It starts the uploader once it is done instead.

    Only session.md is mirrored; raws are fetched on demand. A session whose
    raw is missing or has another md5 than its header says is unfinished
    and is left out of the index until the next sync. What that looked like is
    remembered in the state directory, so the same unfinished session.md is not
    downloaded again until its md5 or its raw entry changes (#119).

    A session Drive no longer has is *not* removed here (T1 R6): the mirror, the
    index row and its search entry stay, and it is marked instead, so that
    `pull --not-exist-delete` or `push --not-exist-upload` is a decision rather
    than a side effect of running any command. Markers move only when the listing
    came back whole (Q4).
    """
    say = warn      # the interactive mode passes its own sink, not the screen's (review K3)
    index = Index(paths)   # a bumped index is already back from the mirror
    index_pending(paths, index, say)    # local only: before the throttle and offline returns (#222)
    stamp = paths.state / "last-sync"
    if throttle and stamp.exists() and now() - float(stamp.read_text()) < SYNC_THROTTLE_S:
        return index
    drive = drive or Drive(paths)
    if kick:
        kick_uploader(paths)
    try:
        remote = drive.list_sessions()
    except StoreError as e:
        say(f"連不上 Drive，改查本機索引：{e}")
        return index
    if kick and (waiting := waiting_ulids(paths)):
        # L5 / review P5: "someone is uploading" while the lock is held; if nothing holds
        # it and the entries are still here, then nothing is trying and it is a failure.
        # Not said when the caller is about to start the uploader itself (`delete`): the
        # entry may be on its way out, so nothing has failed yet (review X1).
        say(f"背景上傳中，{len(waiting)} 筆" if uploader_is_running(paths)
            else f"outbox 還有 {len(waiting)} 筆沒上傳成功")
    missing = remote is None
    remote = remote or {}
    # Sessions waiting to be moved to the Drive trash are on their way out: do not pull
    # them back into the list, and do not call them "deleted on another machine" - they
    # were deleted here (change local-first-writes 2.4).
    trashing = queued_for_trash(paths)
    known = index.known()
    unfinished = _unfinished(paths)
    still_unfinished: dict[str, dict] = {}
    changed = []
    for u, f in remote.items():
        if not f.get("session.md") or u in trashing:
            continue
        seen = unfinished.get(u)
        if known.get(u) == f["session.md"] and (paths.mirror / u / "session.md").exists():
            if seen and seen["legacy"] and seen["session_md5"] == f["session.md"]:
                still_unfinished[u] = seen       # still a raw we do not read: remember it
            continue
        if (seen and not seen["legacy"] and seen["session_md5"] == f["session.md"]
                and raw_fingerprint(seen["raw_manifest"], _folder_files(remote, u))
                == seen["raw_fingerprint"]):
            # Same half-written session.md and the same raw on Drive: downloading it
            # again would read the same header and reach the same "not written yet"
            # answer, once per command (#119). Keep it out of the index and out of an
            # offline rebuild (G3) without downloading it again; the message was said
            # when this state was first seen. Any change to either value - the raw
            # appearing, or a new session.md - falls through and is read again.
            index.drop(u)
            (paths.mirror / u / "session.md").unlink(missing_ok=True)
            still_unfinished[u] = seen
            continue
        changed.append(u)
    if len(changed) > 1:
        # One rclone run for all of them: each call costs seconds (docs/perf.md).
        try:
            drive.download_many(changed, paths.mirror)
        except StoreError as e:
            say(f"批次下載失敗，改成逐一下載：{e}")
    for ulid in changed:
        md5 = remote[ulid]["session.md"]
        local = paths.mirror / ulid / "session.md"
        try:
            if not (local.exists() and md5_file(local) == md5):
                drive.download(ulid, "session.md", local)
            hdr, body = h.split_document(local.read_text(encoding="utf-8"))
            for warning in h.validate(hdr, strict_refs=False):
                say(f"{ulid}：{warning}")
        except (StoreError, h.HeaderError, UnicodeDecodeError) as e:
            say(f"{ulid} 讀不到，先跳過：{e}")
            continue
        raw = h.agora_of(hdr).get("raw")
        state = raw_state(raw, remote[ulid])
        if state is False:
            say(f"{ulid} 還沒寫完（raw 不在或 md5 不符），下次再試")
            index.drop(ulid)
            local.unlink(missing_ok=True)   # keep unfinished sessions out of an offline rebuild (G3)
            manifest = [list(row) for row in raw_manifest(raw)]
            still_unfinished[ulid] = {
                "session_md5": md5, "raw_manifest": manifest, "legacy": False,
                "raw_fingerprint": raw_fingerprint(manifest, remote[ulid])}
            continue
        if state is None:
            # A raw this version does not read: the reading version is as good as any
            # other, the session stays in the mirror and the index (C.3, E.1) - only
            # its raw is unusable, which `show --raw` / `continue` / `edit` say.
            say(f"{ulid} 的 raw 是舊格式或未知格式，這個版本不讀它（閱讀版照常可用）")
            still_unfinished[ulid] = {"session_md5": md5, "raw_manifest": [],
                                      "legacy": True, "raw_fingerprint": ""}
        index.put(ulid, md5, hdr, body)
    if missing:
        say("Drive 上找不到 sessions/，可能是 folder ID 或 token 有問題，標記不動")
    else:
        # The listing is complete, so this is the one moment markers may move.
        # Not up yet and in flight are not "deleted on another machine".
        staged = waiting_ulids(paths)
        local = set(known) | set(index.known())
        # The listing is what Drive said on one call, and a bad day can drop a live folder from it
        # (#152). A marker steers the person to `--not-exist-upload` / `--not-exist-delete`, so a
        # session that is newly missing is asked about once, directly (#159): only "not there" marks
        # it, a folder that answers or a look that fails leaves it unmarked (unknown is not deleted).
        # One that was already marked and is still absent is not asked again on every sync.
        marked = set(index.missing_in_cloud())
        drive = drive or Drive(paths)
        candidates = sorted(u for u in local - set(remote)
                            if u not in staged and u not in trashing and not continuing(paths, u))
        index.mark_missing(u for u in candidates if u in marked or _really_gone(drive, u, say))
    _index_outbox(paths, index)
    paths.state.mkdir(parents=True, exist_ok=True)
    if not missing and still_unfinished != unfinished:
        # A complete listing is the only one that may change what we remember (#119);
        # a failed or missing one leaves the file alone, like the cloud_missing marks.
        _write_unfinished(paths, still_unfinished)
    stamp.write_text(str(now()))
    return index


def cloud_lost(agora_id: str, header: dict | None = None) -> str:
    """The one place the two ways out of a deleted session are worded (review L2).

    With the marker itself, which is how the interactive mode asks: it shows this
    same message rather than a second wording that can drift from it (T2 3.3).
    A session whose raw this version does not read cannot be pushed back (it would
    write the old layout to Drive again): for those the way to keep it is a re-import.
    """
    if header is not None and raw_kind(h.agora_of(header).get("raw")) == "legacy":
        return (f"{agora_id} 雲端沒有（別台機器刪掉了），不再寫回去；它的 raw 是舊格式或未知格式，"
                f"不能傳回去。要保留內容請用 agora import … --as-new 從原始紀錄重匯，"
                f"要刪掉本機這份用 agora pull session {agora_id} --not-exist-delete")
    return (f"{agora_id} 雲端沒有（別台機器刪掉了），不再寫回去；"
            f"要傳回去用 agora push session {agora_id} --not-exist-upload，"
            f"要刪掉本機這份用 agora pull session {agora_id} --not-exist-delete")


def cloud_gone(index: "Index", agora_id: str) -> str | None:
    """Why this session cannot be written to, or None (T1 3.3 / T2 3.3)."""
    ulid = agora_id.split(":", 1)[-1]
    return None if index.cloud_has(ulid) else cloud_lost(agora_id, index.header(ulid))


def forget_local(paths: Paths, ulid: str) -> None:
    """Drop the local copy of a session: its index row, its search entry, its marker."""
    Index(paths).drop(ulid)
    shutil.rmtree(paths.mirror / ulid, ignore_errors=True)
    _forget_folder_record(paths, ulid)
    forget_cursor(paths, ulid)
    try:
        _published_path(paths, ulid).unlink(missing_ok=True)       # the previous-manifest record
    except OSError:
        pass


#: What `delete` writes into a trash-queue file when the session had never been confirmed by Drive
#: (an outbox entry without `.update`, read before the delete removed it): the background then knows a
#: purge that finds nothing is the whole story, and not a Drive that cannot be trusted (#159).
QUEUE_NEVER_CONFIRMED = "never-confirmed"


def queue_never_confirmed(paths: Paths, ulid: str) -> bool:
    try:
        return (paths.trash_queue / ulid).read_text(encoding="utf-8").strip() == QUEUE_NEVER_CONFIRMED
    except (OSError, UnicodeDecodeError):
        return False


def delete_session(paths: Paths, drive: Drive, ulid: str, *, never_confirmed: bool = False) -> None:
    """Move sessions/<ULID>/ to the Drive trash (restorable for 30 days) and forget it here.

    A folder Drive does not have counts as deleted (review S1-4). rclone refuses
    to purge it, and the caller turns that into a failure - so being interrupted
    between the purge and forgetting it locally left a session that could never
    be cleaned up: the rerun purged a folder that was already gone, failed, and
    said so. The same road is 3.4's "delete only the local copy" for a session
    another machine removed, so it is settled here, once.

    "Not found" alone is not enough to believe (S1-4b): the Drive API says the
    same thing when the folder id is wrong or the token cannot see anything, and
    a listing can lose a live folder on a bad day (#152). What this does next is
    irreversible - the mirror and, above all, the outbox (a version that never
    reached Drive) are thrown away - so it takes two answers that agree: a listing
    that did not show this ULID (it came back without it, or `sessions/` is not
    there at all, which is also what a session that never got uploaded to a fresh
    Drive looks like - but that case needs `never_confirmed`, see below) and one
    directed look at the folder that says rclone could not find it (#159). A listing that fails or still has the ULID, and a look that finds
    the folder, finds it empty, finds only orphans or fails, all leave the local copy
    and the outbox alone and the purge failure stands (the trash queue tries again).

    With no `sessions/` in the listing the look alone is not enough (a wrong root id answers "not
    there" for every folder): `never_confirmed` - a session Drive never confirmed, taken from the
    queue file - is the only thing that lets that case through.
    """
    # rclone purge on Drive honours drive.use_trash, which defaults to true.
    try:
        drive._run("purge", f"gdrive:sessions/{ulid}")
    except StoreError as purge_error:
        try:
            remote = drive.list_sessions()
        except StoreError:
            raise          # cannot tell what happened: the purge failure stands
        if remote is not None and ulid in remote:
            raise          # it is still there, so the purge failed for another reason
        if remote is None and not never_confirmed:
            # No `sessions/` at all is what a wrong root folder id or a token that sees nothing
            # looks like (N12), and a directed look at a folder under that root says "not there"
            # too. A session Drive once had must not be taken for deleted on that: it stays in
            # the queue. Only one Drive never confirmed (nothing could be there) is settled by the
            # look alone - the same strictness as `pull --not-exist-delete`.
            raise StoreError(f"{ulid}：purge 失敗，而且 Drive 上找不到 sessions/，可能是 root／folder 設定錯；"
                             f"移到垃圾桶這一步先不做，留在佇列裡之後再試（本機若還有這個 Session 或 outbox 的版本，"
                             f"也不動）") from purge_error
        look = _directed_look(drive, ulid)
        if _folder_state(look) != "gone":
            why = (f"直接查也失敗（{look.error}）" if look.there is None else "直接查還找得到它")
            raise StoreError(f"{ulid}：purge 失敗，列表裡沒有它，但{why}——列表可能漏了它，"
                             f"不能確定它已經沒有了；本機與 outbox 都先不動，之後再試") from purge_error
        warn(f"{ulid} 在 Drive 上已經沒有了，當成刪掉")
    forget_local(paths, ulid)
    shutil.rmtree(paths.outbox / ulid, ignore_errors=True)


def _read_header(path: Path) -> dict:
    hdr, _ = h.split_document(path.read_text(encoding="utf-8"))
    return hdr


def ensure_raw_file(paths: Paths, drive: Drive, ulid: str, header: dict, *,
                    retry: bool = True) -> Path:
    """The session's whole raw as a file in the mirror, fetched and verified on demand (E.2).

    1. A raw this version does not read (`legacy`: no or unknown layout, a corrupt
       manifest): StoreError - before looking at the mirror, so a legacy raw that happens
       to be there is not handed out either.
    2. The mirror already has `raw-<md5[:12]>.json` with the right md5: done, no network.
    3. `chunks/1`: the missing segments in **one** `copy --files-from` into
       `<cache>/segments/<ULID>/`, each checked against its manifest md5, joined in
       manifest order into a temp file next to the target, whole md5 and size checked,
       then `os.replace`d in - and the temp segments and any other `raw-*.json` go.
       A segment that is gone (another machine cleaned up after a new version) makes us
       fetch session.md once more and follow its manifest, keeping the segments that
       are still in it (same name, same md5); still short -> StoreError, nothing left.
    The segments live only for this call, success or not.
    """
    raw = h.agora_of(header).get("raw")
    if raw is None:
        raise StoreError(f"{ulid} 沒有 raw：merge 出來的 Session 沒有自己的 raw，請看 agora.parents 裡各個來源的 raw")
    kind = raw_kind(raw)
    if kind == "legacy":                 # before the mirror: a raw we do not read stays unread
        raise StoreError(f"{ulid}：{LEGACY_RAW_MESSAGE}")
    local = paths.mirror / ulid / raw_name(raw)
    if local.is_file() and md5_file(local) == raw.get("md5"):
        return local
    return _ensure_segmented(paths, drive, ulid, raw, retry=retry)


def _ensure_segmented(paths: Paths, drive: Drive, ulid: str, raw: dict, *,
                      retry: bool) -> Path:
    segdir = paths.cache / "segments" / ulid
    shutil.rmtree(segdir, ignore_errors=True)          # nothing from an earlier call is trusted
    segdir.mkdir(parents=True)
    try:
        have: dict[str, str] = {}                      # segment name -> md5, verified this call
        for attempt in range(2 if retry else 1):
            if not _manifest_ok(raw):
                raise StoreError(f"{ulid} 的 raw 清單不合法（檔名、大小或 md5 有問題），不讀")
            wanted = [(s["file"], s["md5"]) for s in raw_segments(raw)]
            missing = [name for name, md5 in wanted if have.get(name) != md5]
            for name in {n for n in have if n not in {w[0] for w in wanted}}:
                (segdir / name).unlink(missing_ok=True)   # not in this manifest any more
            failure: StoreError | None = None
            try:
                if missing:
                    drive.download_segments(ulid, missing, segdir)
            except StoreError as e:
                failure = e
            # What arrived is checked either way: after a failed call the segments that
            # did come are still good, and a retry on a newer manifest must not fetch them again.
            for name, md5 in wanted:
                if have.get(name) == md5:
                    continue
                path = segdir / name
                if not path.is_file():
                    continue
                if md5_file(path) != md5:
                    path.unlink(missing_ok=True)
                    raise StoreError(f"{ulid} 的段 {name} 下載後 md5 不符，不拼半份")
                have[name] = md5
            if failure is not None:
                if not _segments_gone(drive, ulid, missing) or attempt + 1 >= (2 if retry else 1):
                    raise StoreError(f"{ulid} 的 raw 段下載失敗：{failure}") from None
                raw = _fresh_raw(paths, drive, ulid)    # 404: follow the newer manifest
                keep = {seg.get("file") for seg in raw_segments(raw)}
                have = {n: m for n, m in have.items() if n in keep}
                continue
            if any(have.get(name) != md5 for name, md5 in wanted):
                raise StoreError(f"{ulid} 的 raw 段沒有下載齊")
            return _join_segments(paths, ulid, raw, segdir)
        raise StoreError(f"{ulid} 的 raw 段不齊")          # not reached
    finally:
        shutil.rmtree(segdir, ignore_errors=True)


def _segments_gone(drive: Drive, ulid: str, names: list[str]) -> bool:
    """Is a failed segment download because Drive no longer has them (404), not the network?

    A listing that works and lacks a wanted name says so; one that fails says nothing,
    and the download's own error stands.
    """
    try:
        files = drive.list_one(ulid)
    except StoreError:
        return False
    return any(name not in files for name in names)


def _fresh_raw(paths: Paths, drive: Drive, ulid: str) -> dict:
    """session.md again (once), and the `raw` it names now."""
    local = paths.mirror / ulid / "session.md"
    drive.download(ulid, "session.md", local)
    raw = h.agora_of(_read_header(local)).get("raw") or {}
    if raw_kind(raw) != "chunks":          # a newer header that is not `chunks/1` is not followed
        raise StoreError(LEGACY_RAW_MESSAGE)
    return raw


def _join_segments(paths: Paths, ulid: str, raw: dict, segdir: Path) -> Path:
    mirror = paths.mirror / ulid
    mirror.mkdir(parents=True, exist_ok=True)
    final = mirror / raw_name(raw)
    tmp = mirror / f".{final.name}.joining-{os.getpid()}"
    digest = hashlib.md5()
    size = 0
    try:
        with open(tmp, "wb") as out:
            for seg in raw_segments(raw):
                with open(segdir / seg["file"], "rb") as part:
                    for block in iter(lambda: part.read(1 << 20), b""):
                        out.write(block)
                        digest.update(block)
                        size += len(block)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != raw.get("md5") or size != raw.get("size"):
            raise StoreError(f"{ulid} 的 raw 接起來 md5／大小和 header 不符，不安裝")
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for other in mirror.glob("raw-*"):
        if other.name != final.name:
            other.unlink(missing_ok=True)
    return final


def fetch_raw(paths: Paths, drive: Drive, ulid: str, header: dict, *, retry: bool = True) -> bytes:
    """Download a session's raw on demand and verify it against the header (bytes)."""
    return ensure_raw_file(paths, drive, ulid, header, retry=retry).read_bytes()