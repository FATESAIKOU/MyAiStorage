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
import fcntl
import copy
import hashlib
import json
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
#: `{ulid: {session_md5, raw_file, raw_md5}}`. Local state, never uploaded.
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


class _Md5Reader:
    """A read-through that feeds `shutil.copyfileobj` while computing md5/size."""

    def __init__(self, fileobj):
        self._file = fileobj
        self.digest = hashlib.md5()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self._file.read(size)
        if data:
            self.digest.update(data)
            self.size += len(data)
        return data


def _stage_raw(tmp: Path, raw: bytes | Path) -> tuple[str, int, str]:
    """Put the raw into `tmp` and return (md5, size, file name).

    Bytes are written directly; a path is copied in 1 MiB chunks so the staging
    folder never holds a second whole copy. The staging name is unique inside `tmp`
    and is renamed to the raw-md5 name once the copy finishes, so a crash leaves no
    `raw-*.json` half-file.
    """
    if isinstance(raw, Path):
        handle, staging = tempfile.mkstemp(dir=tmp, prefix="raw-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as out, open(raw, "rb") as src:
                reader = _Md5Reader(src)
                shutil.copyfileobj(reader, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
            name = f"raw-{reader.digest.hexdigest()[:12]}.json"
            os.replace(staging, tmp / name)
        except Exception:
            Path(staging).unlink(missing_ok=True)
            raise
        return reader.digest.hexdigest(), reader.size, name
    md5 = hashlib.md5(raw).hexdigest()
    name = f"raw-{md5[:12]}.json"
    _write_file(tmp / name, raw)
    return md5, len(raw), name


# ---------------------------------------------------------------------------
# Outbox: a session is written here first and only leaves once Drive has it.
# ---------------------------------------------------------------------------


def stage(paths: Paths, header: dict, body: str,
          raw: bytes | Path | None, *, update: bool = False, kind: str = "") -> Path:
    """Write a complete session into the outbox and return its folder.

    `raw` may be the whole bytes, or a file path that holds them. When it is a path,
    the bytes are streamed into the staging folder and the md5/size are taken from
    the file without a second in-memory copy (impl-mem B1b-2).

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
        if raw is not None:
            md5, size, name = _stage_raw(tmp, raw)
            header.setdefault("agora", {})["raw"] = {"file": name, "md5": md5, "size": size}
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


def outbox_ulids(paths: Paths) -> set[str]:
    """The ULIDs waiting in the outbox. Never touches the upload lock (T3-sec3 R6)."""
    if not paths.outbox.exists():
        return set()
    for old in paths.outbox.glob(".old-*"):
        ulid = old.name[len(".old-"):]
        target = paths.outbox / ulid
        # Only restore when no stage is in flight for this ULID (its .tmp still exists).
        if not target.exists() and not (paths.outbox / f".tmp-{ulid}").exists():
            old.rename(target)   # a stage crashed mid-swap: keep the previous complete entry
    return {p.name for p in paths.outbox.iterdir() if p.is_dir() and not p.name.startswith(".")}


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
    shutil.copyfile(folder / "session.md", mirror / "session.md")
    raw = (h.agora_of(read_entry(folder)).get("raw") or {})
    if (name := raw.get("file")) and (folder / name).is_file():
        wanted, target = folder / name, mirror / name
        if not (target.is_file() and target.stat().st_size == wanted.stat().st_size):
            copy_atomic(wanted, target, utf8=True)
        for other in mirror.glob("raw-*"):
            if other.name != name:
                other.unlink()
    index_file(index or Index(paths), mirror / "session.md")


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
            name = raw["file"]                      # KeyError when it has no file
            if not (folder / name).is_file():
                raise h.HeaderError(f"缺少 {name}")
    except (OSError, UnicodeDecodeError, AttributeError, KeyError, TypeError) as e:
        raise h.HeaderError(str(e)) from None
    return hdr, name


def read_entry(folder: Path) -> dict:
    """The header of an outbox entry; HeaderError if it is unreadable or incomplete."""
    return _entry_header(folder, _entry_bytes(folder))[0]


def _upload_checked(drive: Drive, folder: Path, ulid: str, raw: dict,
                    uploaded_raw: bool, tail: str) -> dict:
    """The safe order - the raw, then session.md - then Drive's own md5s (S2-7).

    Nothing else goes (R7): an older raw, a `*.partial` left by an interrupted
    write, a `.DS_Store` - none of them is what the session is, and uploading one
    is how a half-written file comes back to life on Drive. A copyto that returned
    zero is not proof that the bytes arrived, so the md5s Drive reports decide.
    """
    if uploaded_raw:
        drive.upload(folder / raw["file"], ulid, raw["file"])
        _fault("after-raw-upload")        # crash between the two uploads (AGORA_TEST_FAULT)
    elif raw.get("file"):
        warn(f"{ulid} 標頭指到的 {raw['file']} 本機沒有{tail}")
    drive.upload(folder / "session.md", ulid, "session.md")
    _fault("after-session-upload")
    remote = drive.list_one(ulid)
    if remote.get("session.md") != md5_file(folder / "session.md"):
        raise StoreError(f"{ulid} 的 session.md 在 Drive 上的 md5 不符{tail}")
    if uploaded_raw and remote.get(raw["file"]) != raw.get("md5"):
        raise StoreError(f"{ulid} 的 raw 在 Drive 上的 md5 不符{tail}")
    return remote


def push_mirror(drive: Drive, paths: Paths, ulid: str, header: dict) -> None:
    """Send one mirrored session up: `session.md` and the raw its header names."""
    folder = paths.mirror / ulid
    raw = h.agora_of(header).get("raw") or {}
    _upload_checked(drive, folder, ulid, raw,
                    bool(raw.get("file")) and (folder / raw["file"]).is_file(), "")


def mirror_one(paths: Paths, drive: Drive, index: "Index", ulid: str,
               files: dict) -> dict | None:
    """Put one session Drive has into the mirror and the index (sync and pull, once).

    None means it is not whole yet: its header names a raw that is missing or has
    another md5, so the half-written session.md leaves the mirror and the index
    (G3). The caller words that case its own way.
    """
    local = paths.mirror / ulid / "session.md"
    if not (local.exists() and md5_file(local) == files.get("session.md")):
        drive.download(ulid, "session.md", local)
    hdr, body = h.split_document(local.read_text(encoding="utf-8"))
    raw = h.agora_of(hdr).get("raw") or {}
    if raw.get("file") and files.get(raw["file"]) != raw.get("md5"):
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


def _recreated_by_us(drive: Drive, ulid: str, ours: set[str], say) -> bool:
    """#164: the folder's ID changed between the re-check and the confirmation - is
    what stands there really our copies landing on a folder that was purged?

    The ID change is the evidence of the purge (it does not move on another machine's
    ordinary cleanup, a `gen+1` or a rename of the files inside - #155's case, now
    covered by the comparison itself). It is still not enough to take a folder down on:
    a directed look must find a folder holding a `session.md` and *only* files this
    round sent (`ours`). A file outside `ours` means somebody else wrote there since -
    a live folder, not the leftovers of our own copy - and an empty, failed or
    not-found answer contradicts the listing that just showed it. All of those wait.
    """
    look = _directed_look(drive, ulid)
    state = _folder_state(look)
    if state == "alive":
        others = sorted(set(look.files) - ours)
        if not others:
            return True
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


def _write_batch_snapshot(paths: Paths, documents: dict[str, bytes]) -> Path:
    """This round's session.md files, where `_copy_batch` reads exactly them (#130).

    `upload_batch` takes an entry's header and md5 from one copy of session.md; the
    outbox folder can be replaced by a `stage` before the session.md copy, and reading
    it then sent a version naming a raw the raws copy never sent. The bytes go here
    once, and the session.md copy reads from here instead.
    """
    where = paths.state / f"batch-{os.getpid()}"
    # Every run that died before its cleanup - not just this pid (review N1). The callers
    # of `upload_batch` all hold `upload.lock`, so whoever is here is the only writer and
    # nothing under `state/` but these copies may go.
    paths.state.mkdir(parents=True, exist_ok=True)
    for stale in paths.state.glob("batch-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    try:
        for ulid, data in documents.items():
            folder = where / ulid
            folder.mkdir(parents=True)
            _write_file(folder / "session.md", data)
    except BaseException:
        shutil.rmtree(where, ignore_errors=True)
        raise
    return where


def _delete_batch(drive: Drive, paths: Paths, names: list[str]) -> None:
    """The raws this upload replaced, gone in one call (design M4 step 5)."""
    try:
        _with_files_from(drive, names, paths.state, "delete", "gdrive:sessions")
    except StoreError as e:
        warn(f"清掉被取代的舊原始檔失敗，下次再說：{e}")


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
    for ulid in sorted(outbox_ulids(paths)):
        folder = paths.outbox / ulid
        try:
            entries[ulid], documents[ulid] = _entry_for_batch(folder)
        except (OSError, h.HeaderError):
            quarantine(folder, paths.outbox / ".bad", f"outbox 的 {ulid} 壞了")
            continue

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
        entries = {u: m for u, m in entries.items() if u not in gone}
        # Y went into the outbox while we were here: this round takes it too, so a
        # foreground `push` ends with the edit in Drive rather than still waiting.
        for ulid, new_id in rescued.items():
            if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                entries[new_id], documents[new_id] = _entry_for_batch(paths.outbox / new_id)
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
                    entries.pop(ulid, None)
                    if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                        entries[new_id], documents[new_id] = _entry_for_batch(paths.outbox / new_id)
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
        # None: Drive has no sessions/ at all - not evidence of a deletion (N12).

    # The raw each entry names, as read with its md5s - not every raw-* in the
    # folder: the one `stage` carried over from the previous version is only there
    # for a list built before the swap, and sending it again is what made each pass
    # grow (review N1).
    raws = [f"{u}/{name}" for u, (_, name, _) in entries.items() if name]
    raws = [r for r in raws if (paths.outbox / r).is_file()]
    if raws:
        try:
            _copy_batch(drive, paths, raws)
        except StoreError as e:
            say(f"原始檔沒傳好，這一輪不傳 session.md：{e}")
            return sorted(set(entries) | set(left))
        say(f"本輪送出的原始檔：{', '.join(sorted({r.split('/', 1)[0] for r in raws}))}")
    # #130: session.md comes from the bytes read above, written where rclone reads
    # exactly that version. Copying the outbox folder again could send a session.md a
    # `stage` swapped in between the copies - one naming a raw this round never sent.
    # An entry whose raw was not part of this round waits: its session.md would point
    # at a raw that is not on Drive either (an entry replaced twice before the raws
    # copy is the one way that happens).
    sent_raws = {r.split("/", 1)[0] for r in raws}
    sessions = [u for u, (_, name, _) in entries.items() if not name or u in sent_raws]
    if sessions:
        try:
            batch_dir = _write_batch_snapshot(paths, {u: documents[u] for u in sessions})
        except OSError as e:
            # A full disk is not the round's end either (R1, review N2): the raws are up,
            # every entry stays, and the next round with room sends session.md.
            say(f"session.md 快照寫不進 state：{e}，這一輪不傳 session.md")
            return sorted(set(entries) | set(left))
        try:
            _copy_batch(drive, paths, [f"{u}/session.md" for u in sessions],
                        source=batch_dir)
        except StoreError as e:
            say(f"session.md 沒傳好：{e}")
            return sorted(set(entries) | set(left))
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)
        say(f"本輪送出的 session.md：{', '.join(sorted(sessions))}")

    remote = _listing_with_md5(drive)
    listed = isinstance(remote, dict)
    ids_after = None             # {ulid: folder ID} now; read once, only if an update needs it
    no_sessions_yet = remote is None      # Drive has no sessions/ at all (review N3)
    if not listed:
        remote = {}           # nothing confirmed, so nothing leaves the outbox
    replaced = []
    for ulid, (sent_md5, sent_name, sent_raw) in entries.items():
        folder = paths.outbox / ulid
        files = remote.get(ulid) or {}
        if files.get("session.md") != sent_md5:
            left.append(ulid)     # not confirmed, or changed underneath us: it stays
            if no_sessions_yet and ulid not in sessions:
                # The one round where nothing went up because there was nowhere yet:
                # say so, or the entry just sits there with no reason (review N3).
                say(f"Drive 上還沒有 sessions/，{ulid} 留在 outbox")
            elif listed and ulid not in sessions:
                say(f"{ulid} 上傳期間又有新版本，留在 outbox 下一輪再送")
            elif listed:
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
            ours = {"session.md"} | ({sent_name} if sent_name else set())
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
                    say(f"{ulid} 清掉剛寫回去的資料夾失敗：{e}")
                new_id = _rescue_deleted(paths, folder, ulid, say, notices)
                if new_id and new_id not in entries and (paths.outbox / new_id).exists():
                    left.append(new_id)          # Y goes up on the next pass
                if (paths.outbox / ulid).is_dir():
                    left.append(ulid)
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
        if now_md5 != sent_md5 or (sent_raw and files.get(now_raw_name) != sent_raw):
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
        for old_raw in _replaced_raws(done, now_raw_name, files):
            replaced.append(f"{ulid}/{old_raw}")
        shutil.rmtree(done, ignore_errors=True)
    _delete_batch(drive, paths, replaced)
    return sorted(left)


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
        name = (h.agora_of(hdr).get("raw") or {}).get("file")
        raw = (done / name).read_bytes() if name else None
    except (OSError, h.HeaderError) as e:
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



def _replaced_raws(folder: Path, keep: str | None, remote_files: dict) -> list[str]:
    """The older raws this upload replaced, to clear in one call (N7: only for the
    entries that actually left the outbox, so a later round can do the rest)."""
    return [n for n in remote_files
            if n.startswith("raw-") and n != keep]


def _named_raw(folder: Path) -> str | None:
    """The raw this entry's session.md points at, or None (no entry, no raw, unreadable).

    An entry may also hold the raw of the version it replaced (`stage` keeps that one
    for an uploader that listed it a moment ago); this is the one that belongs to it.
    """
    try:
        return (h.agora_of(read_entry(folder)).get("raw") or {}).get("file")
    except h.HeaderError:
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


class Index:
    def __init__(self, paths: Paths):
        paths.cache.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(paths.cache / "index.sqlite")
        bumped = self.db.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION
        if bumped:
            self.db.executescript("DROP TABLE IF EXISTS sessions; DROP TABLE IF EXISTS fts;"
                                  " DROP TABLE IF EXISTS cloud_missing;")
            self.db.execute(f"PRAGMA user_version = {INDEX_VERSION}")
            self.db.commit()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                ulid TEXT PRIMARY KEY, md5 TEXT, header TEXT, body TEXT,
                agent TEXT, source_id TEXT, created TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                ulid UNINDEXED, text, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS cloud_missing (ulid TEXT PRIMARY KEY);
        """)
        if bumped:
            self.rebuild_from_mirror(paths)

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
        self.drop(ulid)
        self.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
            (ulid, md5, json.dumps(header, ensure_ascii=False, default=str), text,
             source.get("agent"), source.get("session_id"), sort_date(header)))
        self.db.execute("INSERT INTO fts VALUES (?,?)", (ulid, text))
        self.db.commit()

    def drop(self, ulid: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE ulid=?", (ulid,))
        self.db.execute("DELETE FROM fts WHERE ulid=?", (ulid,))
        self.db.execute("DELETE FROM cloud_missing WHERE ulid=?", (ulid,))
        self.db.commit()

    def mark_missing(self, ulids) -> None:
        """Which sessions Drive does not have.

        Only ever called with a complete listing (Q4): a half-read drive must not
        move a single marker, or an offline machine would declare the whole
        library deleted on the next start.
        """
        self.db.execute("DELETE FROM cloud_missing")
        self.db.executemany("INSERT OR IGNORE INTO cloud_missing VALUES (?)",
                            [(ulid,) for ulid in sorted(ulids)])
        self.db.commit()

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


def _unfinished(paths: Paths) -> dict[str, dict[str, str]]:
    """What sync last saw for sessions whose raw was missing or wrong (#119).

    The pair is enough to tell whether downloading the same session.md again could
    reach a different answer: the same session.md and the same raw listing mean the
    header still names a raw Drive does not have (or has with another md5), so it is
    still unfinished. A missing or broken file is the same as no memory: the next
    sync downloads once and writes it again.
    """
    try:
        data = json.loads((paths.state / UNFINISHED_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    entries = data.get("sessions")
    if not isinstance(entries, dict):
        return {}
    found: dict[str, dict[str, str]] = {}
    for ulid, record in entries.items():
        if not isinstance(ulid, str) or not isinstance(record, dict):
            continue
        values = (record.get("session_md5"), record.get("raw_file"), record.get("raw_md5"))
        if all(isinstance(value, str) for value in values):
            found[ulid] = {"session_md5": values[0], "raw_file": values[1],
                           "raw_md5": values[2]}
    return found


def _write_unfinished(paths: Paths, entries: dict[str, dict[str, str]]) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    write_atomic(paths.state / UNFINISHED_FILE,
                 json.dumps({"version": 1, "sessions": entries},
                            ensure_ascii=False, sort_keys=True))


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
    still_unfinished: dict[str, dict[str, str]] = {}
    changed = []
    for u, f in remote.items():
        if not f.get("session.md") or u in trashing:
            continue
        if known.get(u) == f["session.md"] and (paths.mirror / u / "session.md").exists():
            continue
        seen = unfinished.get(u)
        if (seen and seen["session_md5"] == f["session.md"]
                and f.get(seen["raw_file"], "") == seen["raw_md5"]):
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
        if raw and remote[ulid].get(raw["file"]) != raw.get("md5"):
            say(f"{ulid} 還沒寫完（raw 不在或 md5 不符），下次再試")
            index.drop(ulid)
            local.unlink(missing_ok=True)   # keep unfinished sessions out of an offline rebuild (G3)
            still_unfinished[ulid] = {"session_md5": md5, "raw_file": raw["file"],
                                      "raw_md5": remote[ulid].get(raw["file"], "")}
            continue
        index.put(ulid, md5, hdr, body)
    if missing:
        say("Drive 上找不到 sessions/，可能是 folder ID 或 token 有問題，標記不動")
    else:
        # The listing is complete, so this is the one moment markers may move.
        # Not up yet and in flight are not "deleted on another machine".
        staged = waiting_ulids(paths)
        local = set(known) | set(index.known())
        index.mark_missing(u for u in local - set(remote)
                           if u not in staged and u not in trashing and not continuing(paths, u))
    _index_outbox(paths, index)
    paths.state.mkdir(parents=True, exist_ok=True)
    if not missing and still_unfinished != unfinished:
        # A complete listing is the only one that may change what we remember (#119);
        # a failed or missing one leaves the file alone, like the cloud_missing marks.
        _write_unfinished(paths, still_unfinished)
    stamp.write_text(str(now()))
    return index


def cloud_lost(agora_id: str) -> str:
    """The one place the two ways out of a deleted session are worded (review L2).

    With the marker itself, which is how the interactive mode asks: it shows this
    same message rather than a second wording that can drift from it (T2 3.3).
    """
    return (f"{agora_id} 雲端沒有（別台機器刪掉了），不再寫回去；"
            f"要傳回去用 agora push session {agora_id} --not-exist-upload，"
            f"要刪掉本機這份用 agora pull session {agora_id} --not-exist-delete")


def cloud_gone(index: "Index", agora_id: str) -> str | None:
    """Why this session cannot be written to, or None (T1 3.3 / T2 3.3)."""
    return None if index.cloud_has(agora_id.split(":", 1)[-1]) else cloud_lost(agora_id)


def forget_local(paths: Paths, ulid: str) -> None:
    """Drop the local copy of a session: its index row, its search entry, its marker."""
    Index(paths).drop(ulid)
    shutil.rmtree(paths.mirror / ulid, ignore_errors=True)


def delete_session(paths: Paths, drive: Drive, ulid: str) -> None:
    """Move sessions/<ULID>/ to the Drive trash (restorable for 30 days) and forget it here.

    A folder Drive does not have counts as deleted (review S1-4). rclone refuses
    to purge it, and the caller turns that into a failure - so being interrupted
    between the purge and forgetting it locally left a session that could never
    be cleaned up: the rerun purged a folder that was already gone, failed, and
    said so. The same road is 3.4's "delete only the local copy" for a session
    another machine removed, so it is settled here, once.

    "Not found" alone is not enough to believe (S1-4b): the Drive API says the
    same thing when the folder id is wrong or the token cannot see anything, and
    then forgetting it here would drop a session that is perfectly safe. So a
    failed purge is followed by a listing, and only a listing that came back
    without this ULID means it is gone. A listing that fails as well leaves the
    original error alone.
    """
    # rclone purge on Drive honours drive.use_trash, which defaults to true.
    try:
        drive._run("purge", f"gdrive:sessions/{ulid}")
    except StoreError:
        try:
            remote = drive.list_sessions()
        except StoreError:
            raise          # cannot tell what happened: the purge failure stands
        if remote is not None and ulid in remote:
            raise          # it is still there, so the purge failed for another reason
        warn(f"{ulid} 在 Drive 上已經沒有了，當成刪掉")
    forget_local(paths, ulid)
    shutil.rmtree(paths.outbox / ulid, ignore_errors=True)


def fetch_raw(paths: Paths, drive: Drive, ulid: str, header: dict, *, retry: bool = True) -> bytes:
    """Download a session's raw on demand and verify it against the header."""
    raw = h.agora_of(header).get("raw")
    if not raw:
        raise StoreError(f"{ulid} 沒有 raw：merge 出來的 Session 沒有自己的 raw，請看 agora.parents 裡各個來源的 raw")
    local = paths.mirror / ulid / raw["file"]
    if not local.exists() or md5_file(local) != raw["md5"]:
        try:
            drive.download(ulid, raw["file"], local)
        except StoreError:
            if not retry:
                raise
            # Another machine may have re-imported it and removed this raw:
            # refresh session.md once and follow its new pointer (N8).
            drive.download(ulid, "session.md", paths.mirror / ulid / "session.md")
            fresh, _ = h.split_document((paths.mirror / ulid / "session.md").read_text(encoding="utf-8"))
            return fetch_raw(paths, drive, ulid, fresh, retry=False)
    if md5_file(local) != raw["md5"]:
        raise StoreError(f"{ulid} 的 raw md5 和 header 不符")
    return local.read_bytes()
