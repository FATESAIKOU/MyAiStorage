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

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from agora import header as h

SYNC_THROTTLE_S = 300


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

    @property
    def mirror(self) -> Path:
        return self.cache / "sessions"

    @property
    def outbox(self) -> Path:
        return self.state / "outbox"

    @property
    def pending(self) -> Path:
        return self.state / "pending"


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


def _warn(message: str) -> None:
    print(f"[agora] {message}", file=sys.stderr)


class Drive:
    """Thin wrapper over rclone. Never reads the token; rclone refreshes it."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.rclone = os.environ.get("AGORA_RCLONE", "rclone")
        self.conf = paths.config / "rclone.conf"
        self.settings_file = paths.config / "config.json"
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
                found.setdefault(parts[0], {})[parts[1]] = (entry.get("Hashes") or {}).get("md5", "")
        return found

    def list_one(self, ulid: str) -> dict[str, str]:
        out = self._run("lsjson", f"gdrive:sessions/{ulid}", "--hash", "--files-only")
        return {e["Name"]: (e.get("Hashes") or {}).get("md5", "") for e in json.loads(out or "[]")}

    def upload(self, local: Path, ulid: str, name: str) -> None:
        # copyto, never copy: copy treats the target as a directory and
        # silently creates an empty folder named after the file.
        self._run("copyto", str(local), f"gdrive:sessions/{ulid}/{name}")

    def download(self, ulid: str, name: str, local: Path) -> None:
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


def remember(paths: Paths, folder: Path, index: "Index | None" = None) -> None:
    """Put a session we just wrote into the local mirror and index right away."""
    mirror = paths.mirror / folder.name
    mirror.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / "session.md", mirror / "session.md")
    hdr, body = h.split_document((mirror / "session.md").read_text(encoding="utf-8"))
    (index or Index(paths)).put(folder.name, md5_file(mirror / "session.md"), hdr, body)


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


def push_one(drive: Drive, folder: Path) -> None:
    """Upload one outbox entry in the safe order, verify, then clean up."""
    ulid = folder.name
    hdr = read_entry(folder)
    raw = h.agora_of(hdr).get("raw")
    if raw:
        drive.upload(folder / raw["file"], ulid, raw["file"])
        _fault("after-raw-upload")
    drive.upload(folder / "session.md", ulid, "session.md")
    _fault("after-session-upload")
    remote = drive.list_one(ulid)
    if remote.get("session.md") != md5_file(folder / "session.md"):
        raise StoreError(f"{ulid} 的 session.md 在 Drive 上的 md5 不符，留在 outbox")
    if raw and remote.get(raw["file"]) != raw["md5"]:
        raise StoreError(f"{ulid} 的 raw 在 Drive 上的 md5 不符，留在 outbox")
    for name in remote:
        if name.startswith("raw-") and (not raw or name != raw["file"]):
            drive.delete(ulid, name)
    shutil.rmtree(folder)


def push_outbox(drive: Drive, paths: Paths) -> list[str]:
    """Push every outbox entry; return the ones that failed (they stay)."""
    failed = []
    if not paths.outbox.exists():
        return failed
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
            _warn(str(e))
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
    _warn(f"{message}（移到 {target}）")


# ---------------------------------------------------------------------------
# Mirror and index
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


class Index:
    def __init__(self, paths: Paths):
        paths.cache.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(paths.cache / "index.sqlite")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                ulid TEXT PRIMARY KEY, md5 TEXT, header TEXT, body TEXT,
                agent TEXT, source_id TEXT, created TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                ulid UNINDEXED, text, tokenize='trigram');
        """)

    def rebuild_from_mirror(self, paths: Paths) -> "Index":
        """An empty index next to a filled mirror (the db was deleted): rebuild it offline."""
        if not self.known() and paths.mirror.exists():
            for md in paths.mirror.glob("*/session.md"):
                try:
                    hdr, body = h.split_document(md.read_text(encoding="utf-8"))
                except (h.HeaderError, OSError, UnicodeDecodeError):
                    continue
                self.put(md.parent.name, md5_file(md), hdr, body)
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
        self.db.commit()

    def header(self, ulid: str) -> dict | None:
        row = self.db.execute("SELECT header FROM sessions WHERE ulid=?", (ulid,)).fetchone()
        return h.upgrade(json.loads(row[0])) if row else None   # rows cached before v4 (V1)

    def by_source(self, agent: str, source_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT ulid FROM sessions WHERE agent=? AND source_id=? ORDER BY ulid DESC",
            (agent, source_id))
        return [r[0] for r in rows]

    def children(self, ulid: str) -> list[str]:
        """Sessions that continue or merge from this one (refs alone do not count)."""
        target = f"agora:{ulid}"
        rows = self.db.execute("SELECT ulid, header FROM sessions")
        return [u for u, hdr in rows
                if any(p.get("id") == target for p in h.agora_of(h.upgrade(json.loads(hdr))).get("parents") or [])]

    def search(self, filters: list[tuple[tuple[str, ...], str, str]]) -> list[tuple[str, dict, str]]:
        """[(ulid, header, snippet)] matching every filter, newest source first.

        A ("text",) filter matches the reading version plus the header's text
        fields; the first one picks candidates through FTS5 (or a scan when
        it is shorter than three characters, which trigram cannot match).
        """
        texts = [normalize(value) for path, _op, value in filters if path == (h.TEXT_KEY,)]
        others = [f for f in filters if f[0] != (h.TEXT_KEY,)]
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


def sync(paths: Paths, drive: Drive | None = None, *, throttle: bool = False) -> Index:
    """Push the outbox, then pull session.md files whose md5 changed.

    Only session.md is mirrored; raws are fetched on demand. A session whose
    raw is missing or has another md5 than its header says is unfinished
    and is left out of the index until the next sync.
    """
    index = Index(paths)
    stamp = paths.state / "last-sync"
    if throttle and stamp.exists() and now() - float(stamp.read_text()) < SYNC_THROTTLE_S:
        return index
    drive = drive or Drive(paths)
    try:
        failed = push_outbox(drive, paths)
        remote = drive.list_sessions()
    except StoreError as e:
        _warn(f"連不上 Drive，改查本機索引：{e}")
        return index
    if failed:
        _warn(f"outbox 還有 {len(failed)} 筆沒上傳成功")
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
            _warn(f"批次下載失敗，改成逐一下載：{e}")
    for ulid in changed:
        md5 = remote[ulid]["session.md"]
        local = paths.mirror / ulid / "session.md"
        try:
            if not (local.exists() and md5_file(local) == md5):
                drive.download(ulid, "session.md", local)
            hdr, body = h.split_document(local.read_text(encoding="utf-8"))
            for warning in h.validate(hdr, strict_refs=False):
                _warn(f"{ulid}：{warning}")
        except (StoreError, h.HeaderError, UnicodeDecodeError) as e:
            _warn(f"{ulid} 讀不到，先跳過：{e}")
            continue
        raw = h.agora_of(hdr).get("raw")
        if raw and remote[ulid].get(raw["file"]) != raw.get("md5"):
            _warn(f"{ulid} 還沒寫完（raw 不在或 md5 不符），下次再試")
            index.drop(ulid)
            local.unlink(missing_ok=True)   # keep unfinished sessions out of an offline rebuild (G3)
            continue
        index.put(ulid, md5, hdr, body)
    gone = set(known) - set(remote) - outbox_ulids(paths)
    if gone and missing:
        _warn("Drive 上找不到 sessions/，可能是 folder ID 或 token 有問題，先不刪鏡像")
    else:
        for ulid in gone:
            index.drop(ulid)
            shutil.rmtree(paths.mirror / ulid, ignore_errors=True)
    _index_outbox(paths, index)
    paths.state.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(now()))
    return index


def delete_session(paths: Paths, drive: Drive, ulid: str) -> None:
    """Move sessions/<ULID>/ to the Drive trash (restorable for 30 days) and forget it here."""
    # rclone purge on Drive honours drive.use_trash, which defaults to true.
    drive._run("purge", f"gdrive:sessions/{ulid}")
    Index(paths).drop(ulid)
    shutil.rmtree(paths.mirror / ulid, ignore_errors=True)
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
