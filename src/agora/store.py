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

import fcntl
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


class StoreError(RuntimeError):
    """Drive or local storage failed in a way the user has to know about."""


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
        cmd = [self.rclone, "--config", str(self.conf)]
        if root:
            cmd += ["--drive-root-folder-id", self.folder_id()]
        try:
            proc = subprocess.run(cmd + list(args), capture_output=True, text=True)
        except OSError as e:   # rclone missing or not executable: a Drive problem, not a bad file (R1)
            raise StoreError(f"叫不起 rclone（{self.rclone}）：{e}") from None
        if proc.returncode != 0:
            raise StoreError(f"rclone {args[0]} 失敗（rc={proc.returncode}）：{proc.stderr.strip()[-300:]}")
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
        listing = mirror / ".files-from"
        listing.write_text("".join(f"{u}/session.md\n" for u in ulids))
        try:
            self._run("copy", "gdrive:sessions", str(mirror), "--files-from", str(listing), "--no-traverse")
        finally:
            listing.unlink(missing_ok=True)

    def delete(self, ulid: str, name: str) -> None:
        self._run("deletefile", f"gdrive:sessions/{ulid}/{name}")


# ---------------------------------------------------------------------------
# Outbox: a session is written here first and only leaves once Drive has it.
# ---------------------------------------------------------------------------


def stage(paths: Paths, header: dict, body: str, raw_bytes: bytes | None) -> Path:
    """Write a complete session into the outbox and return its folder."""
    ulid = header["id"].split(":", 1)[1]
    paths.outbox.mkdir(parents=True, exist_ok=True)
    folder = paths.outbox / ulid
    tmp = paths.outbox / f".tmp-{ulid}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    if raw_bytes is not None:
        md5 = hashlib.md5(raw_bytes).hexdigest()
        name = f"raw-{md5[:12]}.json"
        (tmp / name).write_bytes(raw_bytes)
        header.setdefault("agora", {})["raw"] = {"file": name, "md5": md5, "size": len(raw_bytes)}
    else:
        header.setdefault("agora", {}).pop("raw", None)
    (tmp / "session.md").write_text(h.dump_document(header, body), encoding="utf-8")
    # Swap in whole, so a crash never leaves an outbox entry without session.md
    # (C4): the old entry is renamed aside, the new one moved in, then the old removed (R6).
    old = paths.outbox / f".old-{ulid}"
    shutil.rmtree(old, ignore_errors=True)
    if folder.exists():
        folder.rename(old)
    tmp.rename(folder)
    shutil.rmtree(old, ignore_errors=True)
    return folder


def outbox_count(paths: Paths) -> int:
    return len(outbox_ulids(paths))


def outbox_ulids(paths: Paths) -> set[str]:
    if not paths.outbox.exists():
        return set()
    for old in paths.outbox.glob(".old-*"):
        ulid = old.name[len(".old-"):]
        target = paths.outbox / ulid
        # Only restore when no stage is in flight for this ULID (its .tmp still exists).
        if not target.exists() and not (paths.outbox / f".tmp-{ulid}").exists():
            old.rename(target)   # a stage crashed mid-swap: keep the previous complete entry
    return {p.name for p in paths.outbox.iterdir() if p.is_dir() and not p.name.startswith(".")}


def index_file(index: "Index", session_md: Path) -> None:
    """Index one session.md under its ULID. Raises on an unreadable one."""
    hdr, body = h.split_document(session_md.read_text(encoding="utf-8"))
    index.put(session_md.parent.name, md5_file(session_md), hdr, body)


def remember(paths: Paths, folder: Path, index: "Index | None" = None) -> None:
    """Put a session we just wrote into the local mirror and index right away."""
    mirror = paths.mirror / folder.name
    mirror.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / "session.md", mirror / "session.md")
    index_file(index or Index(paths), mirror / "session.md")


def _index_outbox(paths: Paths, index: "Index") -> None:
    """Sessions still waiting in the outbox are searchable here, marked as not uploaded."""
    for ulid in outbox_ulids(paths):
        try:
            remember(paths, paths.outbox / ulid, index)
        except (h.HeaderError, OSError, UnicodeDecodeError):
            continue


def read_entry(folder: Path) -> dict:
    """The header of an outbox entry; HeaderError if it is unreadable or incomplete."""
    try:
        hdr, _ = h.split_document((folder / "session.md").read_text(encoding="utf-8"))
        raw = h.agora_of(hdr).get("raw")
        if raw and not (folder / raw["file"]).is_file():
            raise h.HeaderError(f"缺少 {raw['file']}")
    except (OSError, UnicodeDecodeError, KeyError, TypeError) as e:
        raise h.HeaderError(str(e)) from None
    return hdr


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


def push_one(drive: Drive, folder: Path) -> None:
    """Upload one outbox entry, verify it, then clean the staging folder up."""
    ulid = folder.name
    hdr = read_entry(folder)
    raw = h.agora_of(hdr).get("raw") or {}
    remote = _upload_checked(drive, folder, ulid, raw, bool(raw.get("file")), "，留在 outbox")
    for name in remote:
        if name.startswith("raw-") and name != (raw or {}).get("file"):
            drive.delete(ulid, name)
    shutil.rmtree(folder)


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


def push_outbox(drive: Drive, paths: Paths, warn=warn) -> list[str]:
    """Push every outbox entry; return the ones that failed (they stay).

    `warn` is where the per-entry failures go when a caller wants its own sink -
    the interactive mode's waiting window, which does not see our stderr (review L5).
    """
    say = warn
    failed = []
    for ulid in sorted(outbox_ulids(paths)):
        folder = paths.outbox / ulid
        try:
            read_entry(folder)
        except h.HeaderError as e:
            quarantine(folder, paths.outbox / ".bad", f"outbox 的 {ulid} 壞了：{e}")
            continue
        try:
            push_one(drive, folder)
        except StoreError as e:
            say(str(e))
            failed.append(ulid)
    return failed


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


def sync(paths: Paths, drive: Drive | None = None, *, throttle: bool = False,
         warn=warn) -> Index:
    """Push the outbox, then pull session.md files whose md5 changed.

    Only session.md is mirrored; raws are fetched on demand. A session whose
    raw is missing or has another md5 than its header says is unfinished
    and is left out of the index until the next sync.

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
    try:
        failed = push_outbox(drive, paths, warn=say)
        remote = drive.list_sessions()
    except StoreError as e:
        say(f"連不上 Drive，改查本機索引：{e}")
        return index
    if failed:
        say(f"outbox 還有 {len(failed)} 筆沒上傳成功")
    missing = remote is None
    remote = remote or {}
    known = index.known()
    changed = [u for u, f in remote.items() if f.get("session.md")
               and not (known.get(u) == f["session.md"] and (paths.mirror / u / "session.md").exists())]
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
            continue
        index.put(ulid, md5, hdr, body)
    if missing:
        say("Drive 上找不到 sessions/，可能是 folder ID 或 token 有問題，標記不動")
    else:
        # The listing is complete, so this is the one moment markers may move.
        # Not up yet and in flight are not "deleted on another machine".
        staged = outbox_ulids(paths)
        local = set(known) | set(index.known())
        index.mark_missing(u for u in local - set(remote)
                           if u not in staged and not continuing(paths, u))
    _index_outbox(paths, index)
    paths.state.mkdir(parents=True, exist_ok=True)
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
