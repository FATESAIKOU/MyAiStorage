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
        proc = subprocess.run(cmd + list(args), capture_output=True, text=True)
        if proc.returncode != 0:
            raise StoreError(f"rclone {args[0]} 失敗（rc={proc.returncode}）：{proc.stderr.strip()[-300:]}")
        return proc.stdout

    def folder_id(self) -> str:
        """The root folder's ID, created on first use and kept in config.json."""
        if self.settings.get("folder_id"):
            return self.settings["folder_id"]
        name = self.settings.get("folder_name") or os.environ.get("AGORA_FOLDER_NAME", "agora")
        self._run("mkdir", f"gdrive:{name}", root=False)
        entries = json.loads(self._run("lsjson", "gdrive:", "--dirs-only", root=False))
        matches = [e for e in entries if e["Name"] == name]
        if len(matches) != 1:
            raise StoreError(f"Drive 根目錄有 {len(matches)} 個 {name}/，請先處理同名資料夾")
        self.settings.update(folder_name=name, folder_id=matches[0]["ID"])
        self.paths.config.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(self.settings, indent=2))
        return self.settings["folder_id"]

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
        header["raw"] = {"file": name, "md5": md5, "size": len(raw_bytes)}
    else:
        header.pop("raw", None)
    (tmp / "session.md").write_text(h.dump_document(header, body), encoding="utf-8")
    # Swap in whole, so a crash never leaves an outbox entry without session.md (C4).
    if folder.exists():
        shutil.rmtree(folder)
    tmp.rename(folder)
    return folder


def outbox_ulids(paths: Paths) -> set[str]:
    return _outbox_ulids(paths)


def outbox_count(paths: Paths) -> int:
    return len(_outbox_ulids(paths))


def _outbox_ulids(paths: Paths) -> set[str]:
    if not paths.outbox.exists():
        return set()
    return {p.name for p in paths.outbox.iterdir() if p.is_dir() and not p.name.startswith(".")}


def remember(paths: Paths, folder: Path, md5: str) -> None:
    """Put a session we just wrote into the local mirror and index right away."""
    ulid = folder.name
    mirror = paths.mirror / ulid
    mirror.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / "session.md", mirror / "session.md")
    hdr, body = h.split_document((mirror / "session.md").read_text(encoding="utf-8"))
    Index(paths).put(ulid, md5, hdr, body)


def _index_outbox(paths: Paths, index: "Index") -> None:
    """Sessions still waiting in the outbox are searchable here, marked as not uploaded."""
    for ulid in _outbox_ulids(paths):
        try:
            hdr, body = h.split_document((paths.outbox / ulid / "session.md").read_text(encoding="utf-8"))
        except (h.HeaderError, OSError, UnicodeDecodeError):
            continue
        index.put(ulid, "outbox", hdr, body)
        mirror = paths.mirror / ulid
        mirror.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths.outbox / ulid / "session.md", mirror / "session.md")


def push_one(drive: Drive, folder: Path) -> None:
    """Upload one outbox entry in the safe order, verify, then clean up."""
    ulid = folder.name
    hdr, _ = h.split_document((folder / "session.md").read_text(encoding="utf-8"))
    raw = hdr.get("raw")
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
    for ulid in sorted(_outbox_ulids(paths)):
        folder = paths.outbox / ulid
        try:
            push_one(drive, folder)
        except StoreError as e:
            _warn(str(e))
            failed.append(ulid)
        except (h.HeaderError, OSError) as e:
            quarantine(folder, paths.outbox / ".bad", f"outbox 的 {ulid} 壞了：{e}")
    return failed


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

    def known(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT ulid, md5 FROM sessions"))

    def put(self, ulid: str, md5: str, header: dict, body: str) -> None:
        source = header.get("source") or {}
        text = normalize("\n".join([
            str(header.get("title") or ""), str(header.get("note") or ""),
            " ".join(header.get("tags") or []), " ".join(header.get("refs") or []), body,
        ]))
        self.drop(ulid)
        self.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
            (ulid, md5, json.dumps(header, ensure_ascii=False, default=str), text,
             source.get("agent"), source.get("session_id"),
             str(source.get("created_at") or header.get("created_at") or "")))
        self.db.execute("INSERT INTO fts VALUES (?,?)", (ulid, text))
        self.db.commit()

    def drop(self, ulid: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE ulid=?", (ulid,))
        self.db.execute("DELETE FROM fts WHERE ulid=?", (ulid,))
        self.db.commit()

    def header(self, ulid: str) -> dict | None:
        row = self.db.execute("SELECT header FROM sessions WHERE ulid=?", (ulid,)).fetchone()
        return json.loads(row[0]) if row else None

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
                if any(p.get("id") == target for p in json.loads(hdr).get("parents") or [])]

    def search(self, keyword: str, filters: list[tuple[tuple[str, ...], str]]) -> list[tuple[str, dict, str]]:
        """[(ulid, header, snippet)], newest source first."""
        kw = normalize(keyword)
        if not kw:
            rows = self.db.execute("SELECT ulid, header, body FROM sessions")
        elif len(kw) < 3:
            # trigram cannot match fewer than 3 characters (e.g. 表格), so scan.
            rows = self.db.execute(
                "SELECT ulid, header, body FROM sessions WHERE instr(body, ?) > 0", (kw,))
        else:
            phrase = '"' + kw.replace('"', '""') + '"'
            rows = self.db.execute(
                "SELECT s.ulid, s.header, s.body FROM fts JOIN sessions s USING (ulid) "
                "WHERE fts MATCH ?", (phrase,))
        hits = []
        for ulid, hdr_json, body in rows:
            hdr = json.loads(hdr_json)
            if all(_matches(hdr, path, value) for path, value in filters):
                hits.append((ulid, hdr, _snippet(body, kw)))
        hits.sort(key=lambda hit: (str((hit[1].get("source") or {}).get("created_at")
                                       or hit[1].get("created_at")),
                                   str(hit[1].get("updated_at")), hit[0]), reverse=True)
        return hits


def _matches(hdr: dict, path: tuple[str, ...], value: str) -> bool:
    node = hdr
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, list):
        return value in node
    return node is not None and str(node) == value


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
    for ulid, files in remote.items():
        md5 = files.get("session.md")
        if not md5:
            continue
        local = paths.mirror / ulid / "session.md"
        if known.get(ulid) == md5 and local.exists():
            continue
        try:
            drive.download(ulid, "session.md", local)
            hdr, body = h.split_document(local.read_text(encoding="utf-8"))
            for warning in h.validate(hdr):
                _warn(f"{ulid}：{warning}")
        except (StoreError, h.HeaderError, UnicodeDecodeError) as e:
            _warn(f"{ulid} 讀不到，先跳過：{e}")
            continue
        raw = hdr.get("raw")
        if raw and files.get(raw["file"]) != raw.get("md5"):
            _warn(f"{ulid} 還沒寫完（raw 不在或 md5 不符），下次再試")
            index.drop(ulid)
            continue
        index.put(ulid, md5, hdr, body)
    gone = set(known) - set(remote) - _outbox_ulids(paths)
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


def fetch_raw(paths: Paths, drive: Drive, ulid: str, header: dict) -> bytes:
    """Download a session's raw on demand and verify it against the header."""
    raw = header.get("raw")
    if not raw:
        raise StoreError(f"{ulid} 沒有 raw（merge 出來的 Session 只能用閱讀版接續）")
    local = paths.mirror / ulid / raw["file"]
    if not local.exists() or md5_file(local) != raw["md5"]:
        try:
            drive.download(ulid, raw["file"], local)
        except StoreError:
            # Another machine may have re-imported it and removed this raw:
            # refresh session.md once and follow its pointer.
            drive.download(ulid, "session.md", paths.mirror / ulid / "session.md")
            header, _ = h.split_document((paths.mirror / ulid / "session.md").read_text(encoding="utf-8"))
            raw = header.get("raw") or raw
            local = paths.mirror / ulid / raw["file"]
            drive.download(ulid, raw["file"], local)
    if md5_file(local) != raw["md5"]:
        raise StoreError(f"{ulid} 的 raw md5 和 header 不符")
    return local.read_bytes()
