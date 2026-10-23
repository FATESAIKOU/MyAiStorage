"""`agora migrate inventory`: which old-format Sessions can go, decided from metadata only.

Read-only, by design (G.2): it never syncs, downloads, uploads or rebuilds the index, never
opens a mirror's `session.md`, never SELECTs a body or a whole header, and prints no title,
description, tag or body. The inputs are the local index (opened read-only, only through
`json_extract`), one listing of Drive (names and md5s), the existence of each Session's source
in the local agent stores, the local outbox's folder names and the `layout` of the header in
them, and the tombstones in `state/deleted`.

The must-keep list is a file outside the repo. It is read, never written back, and its content
never reaches an error message, a log or the output: an unknown line is reported by number only.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from agora import header as h
from agora import store

AGENT_NAMES = ("opencode", "claude", "agy")
_ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
_ID_LINE = re.compile(rf"(?:agora:)?({_ULID})")
_WS = " \t\v\f"                      # what is trimmed around a line: not Unicode spaces


class MigrateError(Exception):
    """Something the person has to fix before inventory can say anything. The message never
    carries the content of the must-keep list."""


# ---------------------------------------------------------------------------
# the must-keep list
# ---------------------------------------------------------------------------


def read_must_keep(path: str) -> set[str]:
    """The ULIDs in the list file; MigrateError for anything that is not a clean list.

    Fail-closed (F.1.1): unreadable, not UTF-8, no id at all, or a line that is not an id
    is an error and there are no candidates. The message holds the path, line numbers and
    the reason - never a line.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise MigrateError(f"必保清單 {path} 讀不到（{e.strerror or type(e).__name__}）；不給任何候選") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise MigrateError(f"必保清單 {path} 不是 UTF-8；不給任何候選") from None
    del data
    text = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    ids: set[str] = set()
    bad: list[int] = []
    for number, line in enumerate(text.split("\n"), 1):
        line = line.strip(_WS)
        if not line or line.startswith("#"):
            continue
        found = _ID_LINE.fullmatch(line)
        if found:
            ids.add(found.group(1))
        else:
            bad.append(number)
    if bad:
        shown = "、".join(str(n) for n in bad)
        raise MigrateError(f"必保清單 {path} 有 {len(bad)} 行不是可辨認的 id（第 {shown} 行；"
                           f"只認 agora:<ULID> 或 <ULID>，26 碼大寫 Crockford）；不給任何候選")
    if not ids:
        raise MigrateError(f"必保清單 {path} 解析出 0 個 id（空檔、只有註解或空白）；"
                           f"真的不要必保清單就不要給 --must-keep")
    return ids


# ---------------------------------------------------------------------------
# reading the index: metadata only
# ---------------------------------------------------------------------------


@dataclass
class Row:
    ulid: str
    md5: str | None
    agent: str | None
    source_id: str | None
    source_kind: str | None            # json_type of $.agora.source
    host: str | None
    dir: str | None
    relation: str | None
    raw: dict | None                   # md5 / size / layout / segments only; never `file`
    parents: list[str] = field(default_factory=list)   # ids of the parents ("agora:<ULID>")


_SELECT = """
SELECT ulid, md5, agent, source_id,
       json_type(header, '$.agora.source'),
       json_extract(header, '$.agora.source.host'),
       json_extract(header, '$.agora.source.dir'),
       json_extract(header, '$.agora.relation'),
       json_type(header, '$.agora.raw'),
       json_extract(header, '$.agora.raw.md5'),
       json_extract(header, '$.agora.raw.size'),
       json_extract(header, '$.agora.raw.layout'),
       json_extract(header, '$.agora.raw.segments'),
       json_extract(header, '$.agora.parents')
FROM sessions ORDER BY ulid
"""


def _raw_of(kind, md5, size, layout, segments) -> dict | None:
    if kind in (None, "null"):
        return None
    raw: dict = {}
    if md5 is not None:
        raw["md5"] = md5
    if size is not None:
        raw["size"] = size
    if layout is not None:
        raw["layout"] = layout
    if segments is not None:
        try:
            parsed = json.loads(segments)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            raw["segments"] = parsed
    return raw


def _parent_ids(text) -> list[str]:
    try:
        parents = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return []
    if not isinstance(parents, list):
        return []
    return [p["id"] for p in parents if isinstance(p, dict) and isinstance(p.get("id"), str)]


def open_index(paths: store.Paths) -> sqlite3.Connection:
    """The local index, read-only. A missing file or another `user_version` is an error: opening
    it through `store.Index` would create or rebuild it, and the rebuild reads every mirror's
    `session.md` (G.2)."""
    path = paths.cache / "index.sqlite"
    if not path.is_file():
        raise MigrateError("本機還沒有索引；先跑一次正常指令（例如 agora sync session）再盤點")
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        version = db.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as e:
        raise MigrateError(f"索引打不開：{e}") from None
    if version != store.INDEX_VERSION:
        db.close()
        raise MigrateError(
            f"索引版本是 {version}，這個版本要 {store.INDEX_VERSION}；先跑一次正常指令"
            f"（例如 agora sync session）讓索引重建完成，再盤點——盤點自己不重建（重建會讀鏡像的 session.md）")
    return db


def read_rows(db: sqlite3.Connection) -> tuple[list[Row], set[str], dict[str, list[str]]]:
    """(rows, ulids Drive no longer has, {parent ulid: [child ulids]})."""
    rows = []
    for (ulid, md5, agent, source_id, source_kind, host, dir_, relation, raw_kind_, raw_md5, raw_size,
         layout, segments, parents) in db.execute(_SELECT):
        rows.append(Row(ulid, md5, agent, source_id, source_kind, host if isinstance(host, str) else None,
                        dir_ if isinstance(dir_, str) else None,
                        relation if isinstance(relation, str) else None,
                        _raw_of(raw_kind_, raw_md5, raw_size, layout, segments), _parent_ids(parents)))
    missing = {r[0] for r in db.execute("SELECT ulid FROM cloud_missing")}
    children: dict[str, list[str]] = {}
    # the children of each Session, by the ids in `parents` only (not Index.children, which parses
    # every header); a Session Drive no longer has is not a child
    for child, parent in db.execute(
            "SELECT s.ulid, json_extract(j.value, '$.id') FROM sessions s, json_each(s.header, '$.agora.parents') j "
            "WHERE json_type(s.header, '$.agora.parents') = 'array'"):
        if child in missing or not isinstance(parent, str):
            continue
        children.setdefault(parent.removeprefix("agora:"), []).append(child)
    for kids in children.values():
        kids.sort()
    return rows, missing, children


# ---------------------------------------------------------------------------
# the local agent stores: existence only
# ---------------------------------------------------------------------------


class Stores:
    """Is the source of a Session on this machine? Existence only; nothing is opened for content."""

    def __init__(self) -> None:
        self._opencode: set[str] | None = None

    def available(self, agent: str) -> bool:
        if agent == "claude":
            from agora.agents import claude
            return claude.projects_dir().is_dir()
        if agent == "agy":
            from agora.agents import agy_store
            return agy_store.conversations_dir().is_dir()
        if agent == "opencode":
            from agora.agents import opencode
            return opencode._db_path().is_file()
        return False

    def has(self, agent: str, session_id: str, directory: str | None) -> bool:
        from agora.agents.base import AgentError
        if agent == "claude":
            from agora.agents import claude
            try:
                claude.find_jsonl(session_id, directory)
            except (AgentError, OSError):
                return False
            return True
        if agent == "agy":
            from agora.agents import agy_store
            try:
                return agy_store.db_path(session_id).is_file()
            except AgentError:
                return False
        if agent == "opencode":
            if self._opencode is None:
                from agora.agents import opencode
                self._opencode = {item.session_id for item in opencode.ADAPTER.list_sessions()}
            return session_id in self._opencode
        return False


# ---------------------------------------------------------------------------
# the outbox: folder names and the layout of their header
# ---------------------------------------------------------------------------


def outbox_legacy(paths: store.Paths) -> set[str]:
    """ULIDs whose entry in the local outbox is not `chunks/1`.

    The entries are the ones `store.waiting_ulids` counts - a plain entry, a `.old-` a crashed
    stage left (the next command would put it back) and a `.done-` set aside - found by
    `store.outbox_folders`, the same rules without the repair (a repair is not for a read-only
    command). One header read per entry."""
    legacy: set[str] = set()
    for ulid, entry in store.outbox_folders(paths).items():
        try:
            hdr, _body = h.split_document((entry / "session.md").read_text(encoding="utf-8"))
            raw = h.agora_of(hdr).get("raw")
        except (OSError, UnicodeDecodeError, h.HeaderError):
            legacy.add(ulid)                 # cannot be read: not something that may be dropped
            continue
        layout = raw.get("layout") if isinstance(raw, dict) else None
        if raw is not None and layout != store.RAW_LAYOUT:
            legacy.add(ulid)
    return legacy


# ---------------------------------------------------------------------------
# the classification
# ---------------------------------------------------------------------------


def layout_of(raw: dict | None) -> str:
    kind = store.raw_kind(raw)
    if kind == "none":
        return "none"
    if kind == "chunks":
        return store.RAW_LAYOUT
    return "legacy" if not (isinstance(raw, dict) and raw.get("layout")) else "unknown"


def classify(row: Row, host: str, stores: Stores) -> str:
    if layout_of(row.raw) == store.RAW_LAYOUT:
        return "new-format"
    if row.source_kind != "object" or row.agent not in AGENT_NAMES or not row.source_id:
        return "no-source"
    if row.host is None:
        return "host-unknown"
    if row.host != host:
        return "other-machine"
    if not stores.available(row.agent):
        return "agent-unavailable"
    return "local-present" if stores.has(row.agent, row.source_id, row.dir) else "local-missing"


def _relation(old: Row, new: Row, drive: dict, ) -> tuple[str | None, str]:
    """(`identical`/`different`/None, reason) for the replacement `new` of `old` (G.2)."""
    if layout_of(old.raw) == "none":
        return None, "old-has-no-raw"
    old_md5 = (old.raw or {}).get("md5")
    new_md5 = (new.raw or {}).get("md5")
    if not old_md5:
        return None, "no-old-md5"
    if not new_md5:
        return None, "new-has-no-raw"
    if old_md5 != new_md5 or (old.raw or {}).get("size") != (new.raw or {}).get("size"):
        return "different", "md5-differs"
    new_files = drive.get(new.ulid) or {}
    if not new.md5 or new_files.get("session.md") != new.md5:
        return "different", "drive-version-elsewhere"
    if store.raw_complete(new.raw, new_files) is not True:
        return "different", "segments-missing"
    old_files = drive.get(old.ulid)
    if old_files is None or "session.md" not in old_files:
        return "different", "old-not-on-drive"
    if not old.md5 or old_files.get("session.md") != old.md5:
        return "different", "old-drive-version-elsewhere"
    return "identical", "md5-equal"


def replacement_of(old: Row, by_source: dict, drive: dict) -> dict | None:
    """The `chunks/1` Session of the same source on Drive, newest first; any `identical` wins."""
    candidates = by_source.get((old.agent, old.source_id), [])
    best = None
    for new in candidates:
        if new.ulid == old.ulid:
            continue
        relation, reason = _relation(old, new, drive)
        entry = {"ulid": new.ulid, "relation_to_old": relation,
                 "raw_size": (new.raw or {}).get("size") if new.raw else None, "reason": reason}
        if relation == "identical":
            return entry
        if best is None:
            best = entry
    return best


def lineages(rows: list[Row]) -> dict[str, list[str]]:
    """{ulid: sorted connected set of ulids}, from the `parents` ids already read."""
    known = {r.ulid for r in rows}
    parent_of = {r.ulid: sorted(p.removeprefix("agora:") for p in r.parents
                                if p.removeprefix("agora:") in known) for r in rows}
    seen: dict[str, int] = {}
    adjacent: dict[str, set[str]] = {u: set() for u in known}
    for child, parents in parent_of.items():
        for parent in parents:
            adjacent[child].add(parent)
            adjacent[parent].add(child)
    groups: list[list[str]] = []
    for start in sorted(known):
        if start in seen:
            continue
        stack, group = [start], []
        seen[start] = len(groups)
        while stack:
            node = stack.pop()
            group.append(node)
            for nxt in adjacent[node]:
                if nxt not in seen:
                    seen[nxt] = len(groups)
                    stack.append(nxt)
        groups.append(sorted(group))
    return {u: groups[seen[u]] for u in known}


@dataclass
class Report:
    host: str
    counts: dict
    drop_list: list[str]
    must_keep_deleted: list[str]
    sessions: list[dict]

    def as_json(self) -> dict:
        return {"host": self.host, "counts": self.counts, "drop_list": self.drop_list,
                "must_keep_deleted": self.must_keep_deleted, "sessions": self.sessions}


def tombstones(paths: store.Paths) -> set[str]:
    try:
        lines = (paths.state / "deleted").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    return {line.strip().removeprefix("agora:") for line in lines if line.strip()}


def inventory(paths: store.Paths, must_keep_file: str | None = None, *, drive: dict | None = None) -> Report:
    """The whole report. `drive` is the listing (`{ulid: {name: md5}}`); None reads it from Drive."""
    must_keep = read_must_keep(must_keep_file) if must_keep_file is not None else set()
    db = open_index(paths)
    try:
        rows, missing, children = read_rows(db)
    finally:
        db.close()
    if drive is None:
        drive = _listing(paths)

    deleted = tombstones(paths)
    known = {r.ulid for r in rows} | set(drive)
    unknown = sorted(must_keep - known - deleted)
    if unknown:
        raise MigrateError("必保清單裡有 id 在這台的索引與 Drive 都找不到、也不在刪除記錄裡（打錯字，或清單沒更新）："
                           + "、".join(unknown) + "；不給任何候選，先問本人")
    gone = sorted(must_keep & deleted - known)

    host = socket.gethostname()
    stores = Stores()
    by_ulid = {r.ulid: r for r in rows}
    by_source: dict[tuple, list[Row]] = {}
    for r in sorted(rows, key=lambda r: r.ulid, reverse=True):
        if (layout_of(r.raw) == store.RAW_LAYOUT and r.ulid not in missing
                and r.agent in AGENT_NAMES and r.source_id):
            by_source.setdefault((r.agent, r.source_id), []).append(r)
    lineage = lineages(rows)
    legacy_outbox = outbox_legacy(paths)

    facts: dict[str, dict] = {}
    for r in rows:
        cls = classify(r, host, stores)
        replacement = None if cls == "new-format" else replacement_of(r, by_source, drive)
        continuing = store.continuing_details(paths, r.ulid) is not None
        stuck = r.ulid in legacy_outbox
        reason = None
        if cls != "local-present":
            reason = "class-not-local-present"
        elif continuing:
            reason = "continuing"
        elif replacement is None:
            reason = "no-replacement"
        elif replacement["relation_to_old"] is None:
            reason = "relation-null"
        elif replacement["relation_to_old"] != "identical":
            reason = "relation-different"
        elif stuck:
            reason = "outbox-legacy"
        facts[r.ulid] = {"class": cls, "replacement": replacement, "continuing": continuing,
                         "outbox_legacy": stuck, "own_reason": reason}

    sessions = []
    for r in rows:
        fact = facts[r.ulid]
        members = lineage[r.ulid]
        reason = fact["own_reason"]
        if reason is None and any(facts[m]["own_reason"] is not None for m in members):
            reason = "lineage-member-not-droppable"
        sessions.append({
            "ulid": r.ulid, "agent": r.agent, "external_id": r.source_id, "host": r.host,
            "relation": r.relation, "layout": layout_of(r.raw),
            "raw_size": (r.raw or {}).get("size") if r.raw else None,
            "class": fact["class"], "replacement": fact["replacement"],
            "old_children": children.get(r.ulid, []), "lineage": members,
            "must_keep": r.ulid in must_keep,
            "lineage_protected": any(m in must_keep for m in members),
            "continuing": fact["continuing"], "outbox_legacy": fact["outbox_legacy"],
            "droppable": reason is None, "not_droppable_reason": reason})
    for ulid in sorted(set(drive) - set(by_ulid)):
        if "session.md" not in drive[ulid]:
            continue                       # a folder with no session.md is not a Session
        sessions.append({
            "ulid": ulid, "agent": None, "external_id": None, "host": None, "relation": None,
            "layout": None, "raw_size": None, "class": "drive-only", "replacement": None,
            "old_children": [], "lineage": [ulid], "must_keep": ulid in must_keep,
            "lineage_protected": ulid in must_keep, "continuing": False, "outbox_legacy": ulid in legacy_outbox,
            "droppable": False, "not_droppable_reason": "class-not-local-present"})
    sessions.sort(key=lambda s: s["ulid"])
    counts: dict[str, int] = {}
    for s in sessions:
        counts[s["class"]] = counts.get(s["class"], 0) + 1
    drop = sorted(s["ulid"] for s in sessions if s["droppable"])
    return Report(host, dict(sorted(counts.items())), drop, gone, sessions)


def _listing(paths: store.Paths) -> dict:
    """One listing of `sessions/` on Drive. It does not create the root folder: a machine that has
    never talked to Drive has nothing to inventory, and `Drive.folder_id` would `mkdir` it."""
    import os
    drive = store.Drive(paths)
    name = os.environ.get("AGORA_FOLDER_NAME", "agora")
    if not (drive.settings.get("folders") or {}).get(name):
        raise MigrateError("這台還沒連過 Drive（沒有 folder id）；先跑一次正常指令（例如 agora sync session）")
    try:
        listing = drive.list_sessions()
    except store.StoreError as e:
        raise MigrateError(f"列不出 Drive 的檔案：{e}") from None
    if listing is None:
        raise MigrateError("Drive 上找不到 sessions/；不能判斷任何一筆")
    return listing


def render(report: Report) -> str:
    """The human version: numbers and ids only."""
    lines = [f"host: {report.host}", "counts:"]
    lines += [f"  {name}: {n}" for name, n in report.counts.items()]
    lines.append(f"drop_list ({len(report.drop_list)}):")
    lines += [f"  {u}" for u in report.drop_list]
    if report.must_keep_deleted:
        lines.append(f"must_keep_deleted ({len(report.must_keep_deleted)}):")
        lines += [f"  {u}" for u in report.must_keep_deleted]
    blocked = [s for s in report.sessions if not s["droppable"] and s["class"] != "new-format"]
    lines.append(f"not droppable ({len(blocked)}):")
    lines += [f"  {s['ulid']}  {s['class']}  {s['not_droppable_reason']}" for s in blocked]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# `agora migrate copy-headers`: the user's fields of an old Session onto its re-import
# ---------------------------------------------------------------------------

#: Never copied: the id, the system block, and what the re-import generated from its own source.
NOT_COPIED = ("id", "agora", "sources", "generated", "type")
#: Lists of refs (`agora:<old>` in them is turned into the new id, or the element goes) and the one ref.
REF_LISTS = ("refs", "verified")
_SUPERSEDED = "superseded-by-"


@dataclass
class CopyItem:
    """One line of the plan. `kind`: `copy` (a pair), `na` (expected: nothing to copy and nothing is
    lost by deleting it), `broken` (it should have been paired, or the report cannot be trusted)."""
    new: str | None                       # ULID of the chunks/1 re-import (None: the report has none for it)
    old: str | None                       # ULID of the old Session it takes fields from (None: not copied)
    kind: str = "copy"
    status: str = "todo"                  # copy: todo / done / attention
    reason: str | None = None             # why it is na or broken
    fields: dict = field(default_factory=dict)    # {name: value} that are to be on the new header
    changes: list[str] = field(default_factory=list)   # names of the fields that differ now
    dropped: list[str] = field(default_factory=list)   # tags left out on purpose (superseded-by-*, naming an old id)
    lost: list[str] = field(default_factory=list)      # "refs: <element>" that named an old Session with no new one
    review: list[str] = field(default_factory=list)    # fields that name an old Session and need a person
    others: list[str] = field(default_factory=list)    # the other old Sessions (listed only)

    @property
    def blocking(self) -> bool:
        """Something was lost or needs a person: not "consistent", whatever the header reads now."""
        return bool(self.lost or self.review)


def _headers(db: sqlite3.Connection, ulids) -> dict[str, dict]:
    """The whole header of each ULID, read from the read-only index (and nothing is printed)."""
    out = {}
    for u in ulids:
        row = db.execute("SELECT header FROM sessions WHERE ulid=?", (u,)).fetchone()
        if row:
            try:
                out[u] = h.upgrade(json.loads(row[0]))
            except (TypeError, ValueError):
                pass
    return out


def _mentions(value, ulid: str) -> bool:
    return ulid in json.dumps(value, ensure_ascii=False, default=str)


def _superseded(hdr: dict) -> bool:
    tags = hdr.get("tags") or []
    return any(isinstance(t, str) and t.startswith(_SUPERSEDED) for t in (tags if isinstance(tags, list) else [tags]))


def _tags(hdr: dict) -> list:
    tags = hdr.get("tags") or []
    return list(tags) if isinstance(tags, list) else [tags]


def _rewrite(text, ids: set, new_of: dict, new_ulid: str, *, need_prefix: bool = True):
    """(text, state): `keep` (names no old Session), `replaced` (`agora:<old>` became `agora:<its new id>`),
    `self` (it would point at this very Session: dropped, nothing lost) or `lost` (an old Session with no new
    id, or a shape this does not rewrite). The caller drops the element on `self` and `lost`."""
    if not isinstance(text, str):
        return (None, "lost") if any(_mentions(text, u) for u in ids) else (text, "keep")
    hit = [u for u in ids if u in text]
    if not hit:
        return text, "keep"
    target = {u: (new_ulid if u == new_ulid else new_of.get(u)) for u in hit}
    if need_prefix and not text.startswith("agora:"):
        return None, "lost"
    if any(t is None for t in target.values()):
        return None, "lost"
    if any(t == new_ulid for t in target.values()):
        return None, "self"
    out = text
    for u in hit:
        out = out.replace(u, target[u])
    return out, "replaced"


def wanted(old: dict, new: dict, old_ulid: str, new_ulid: str, all_old=frozenset(), new_of=None) -> dict:
    """What the new header should have, as {fields, dropped, lost, review}.

    The user's own fields of `old` go over `new`'s. `refs`, `verified` and `case` are handled element by
    element: `agora:<old>` becomes the new id when the pairing table has one, any other element that names an
    old Session (any one: `all_old`) or the new one is left out and listed in `lost`. Every other field that
    names one is not guessed at: it is not copied and goes to `review` (a person decides). Tags: the new
    Session's own, then the old ones that are new to it, plus `migrated-from-<old>`; a tag naming an old id is
    treated like a ref (replaced by the new id, or `lost`); `superseded-by-*` alone is left out on purpose
    (`dropped`) and is not a loss. A ref that would point at the new Session itself is dropped, not lost."""
    new_of = new_of or {}
    ids = set(all_old) | {old_ulid, new_ulid}
    out: dict = {}
    lost: list[str] = []
    review: list[str] = []
    for key, value in old.items():
        if key in NOT_COPIED or key == "tags" or value is None or value == [] or value == {}:
            continue
        if key in REF_LISTS and isinstance(value, list):
            kept = []
            for el in value:
                text, state = _rewrite(el, ids, new_of, new_ulid)
                if state == "lost":
                    lost.append(f"{key}: {el}")
                elif text is not None:
                    kept.append(text)
            if kept:
                out[key] = kept
        elif key == "case" and isinstance(value, str):
            text, state = _rewrite(value, ids, new_of, new_ulid)
            if state == "lost":
                lost.append(f"{key}: {value}")
            elif text is not None:
                out[key] = text
        elif any(_mentions(value, u) for u in ids):
            review.append(key)
        else:
            out[key] = value
    tags = _tags(new)
    dropped: list[str] = []
    for tag in _tags(old):
        if isinstance(tag, str) and tag.startswith(_SUPERSEDED):
            dropped.append(tag)               # on purpose, and not a loss: it says something about the old Session
            continue
        text, state = _rewrite(tag, ids, new_of, new_ulid, need_prefix=False)
        if state == "lost":
            lost.append(f"tags: {tag}")
            continue
        if text is None:
            continue
        if text not in tags:
            tags.append(text)
    mark = f"migrated-from-{old_ulid}"
    if mark not in tags:
        tags.append(mark)
    out["tags"] = tags
    return {"fields": out, "dropped": sorted(set(dropped)), "lost": lost, "review": sorted(review)}


def check_report(report) -> list[dict]:
    """The `sessions` of an `inventory --json` output, or MigrateError: a list of objects with a string `ulid`."""
    sessions = report.get("sessions") if isinstance(report, dict) else None
    if (not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions)
            or not all(isinstance(s.get("ulid"), str) for s in sessions)):
        raise MigrateError("inventory 輸出的格式不對：要有 sessions 清單，每一筆有 ulid（migrate inventory --json 的輸出）")
    return sessions


def plan_copy(paths: store.Paths, report: dict) -> list[CopyItem]:
    """Which old Session's fields go to which new one, and everything else the report says about old ones.

    The pairs come from `inventory`'s `replacement`, each checked again against the index (same source,
    chunks/1, `import`, neither gone from Drive). Every old-format Session of the report is an item: a
    pair (`copy`), expected to have none (`na`: not `local-present` and not in `drop_list`) or `broken`.
    So is every old-format Session of the *index* the report does not name (`missing-from-inventory`): the
    file is not trusted. Which old one to copy is decided over all the old-format Sessions of that source
    in the index, not over the ones the report happens to name."""
    entries = check_report(report)
    drop = {u for u in (report.get("drop_list") or []) if isinstance(u, str)}
    db = open_index(paths)
    try:
        rows, missing, _children = read_rows(db)
        by_ulid = {r.ulid: r for r in rows}
        old_rows = [r for r in rows if layout_of(r.raw) != store.RAW_LAYOUT]
        # every old Session there is: in the index, or only named by the report (a Drive-only one has no row)
        all_old = {r.ulid for r in old_rows} | {s["ulid"] for s in entries if s.get("class") != "new-format"}
        live_old: dict[tuple, list[str]] = {}
        for r in old_rows:
            if r.ulid not in missing and r.agent in AGENT_NAMES and r.source_id:
                live_old.setdefault((r.agent, r.source_id), []).append(r.ulid)
        groups: dict[str, set[str]] = {}       # new ulid -> old ulids the report pairs with it
        items: list[CopyItem] = []
        seen: set[str] = set()
        for s in entries:
            old = s["ulid"]
            if s.get("class") in ("new-format", "drive-only") or old in seen:
                continue
            seen.add(old)
            rep = s.get("replacement")
            new = rep.get("ulid") if isinstance(rep, dict) else None
            o, n = by_ulid.get(old), by_ulid.get(new) if isinstance(new, str) else None
            expected = False
            if not isinstance(new, str):
                why = "no-replacement"
                expected = s.get("class") != "local-present" and old not in drop
            elif o is None:
                why = "old-not-in-index"
            elif n is None:
                why = "replacement-not-in-index"
            elif old in missing:
                why, expected = "old-gone-from-drive", old not in drop
            elif new in missing:
                why = "replacement-gone-from-drive"
            elif layout_of(o.raw) == store.RAW_LAYOUT:
                why = "old-is-already-chunks"
            elif layout_of(n.raw) != store.RAW_LAYOUT or n.relation != "import":
                why = "replacement-is-not-a-chunks-import"
            elif not o.source_id or (o.agent, o.source_id) != (n.agent, n.source_id):
                why = "replacement-is-of-another-source"
            else:
                groups.setdefault(new, set()).add(old)
                continue
            items.append(CopyItem(new if isinstance(new, str) else None, None, "na" if expected else "broken",
                                  "skipped", why, others=[old]))
        for r in old_rows:
            if r.ulid not in seen and r.ulid not in missing:
                items.append(CopyItem(None, None, "broken", "skipped", "missing-from-inventory", others=[r.ulid]))
        new_of: dict[str, str] = {}
        for new in groups:
            key = (by_ulid[new].agent, by_ulid[new].source_id)
            for o in set(live_old.get(key, [])) | groups[new]:
                new_of[o] = new
        headers = _headers(db, {u for k, v in groups.items() for u in
                                [k, *live_old.get((by_ulid[k].agent, by_ulid[k].source_id), []), *v]})
    finally:
        db.close()

    for new in sorted(groups):
        key = (by_ulid[new].agent, by_ulid[new].source_id)
        olds = sorted(set(live_old.get(key, [])) | groups[new])
        if new not in headers or any(o not in headers for o in olds):
            items.append(CopyItem(new, None, "broken", "skipped", "header-unreadable", others=olds))
            continue
        pick = olds
        if len(olds) > 1:
            pick = [o for o in olds if not _superseded(headers[o])]
            if len(pick) != 1:
                items.append(CopyItem(new, None, "broken", "skipped",
                                      "several-old-all-superseded" if not pick else "several-old-none-or-many-not-superseded",
                                      others=olds))
                continue
        old = pick[0]
        w = wanted(headers[old], headers[new], old, new, all_old, new_of)
        for k, v in list(w["fields"].items()):
            have = headers[new].get(k)
            if isinstance(v, list) and isinstance(have, list):
                # what a person added to the new Session stays: the target is what it has plus what is missing
                w["fields"][k] = list(have) + [e for e in v if e not in have]
        changes = [k for k, v in w["fields"].items() if headers[new].get(k) != v]
        item = CopyItem(new, old, "copy", "todo" if changes else "done", None, w["fields"], changes,
                        w["dropped"], w["lost"], w["review"], [o for o in olds if o != old])
        if not changes and item.blocking:
            item.status = "attention"
        items.append(item)
    items.sort(key=lambda i: (i.new or "", i.others))
    return items


@dataclass
class Summary:
    counts: dict
    blocking: int                         # pairs with something lost or waiting for a person, not accepted
    accepted: list[str]                   # new ULIDs whose lost/review a person has accepted (`--accept`)
    uncovered: list[str]                  # in the report's drop_list, and not an old Session that is consistent
    pre_reimport: bool                    # a local-present old Session, and none of them has a replacement

    @property
    def clean(self) -> bool:
        """The condition for deleting: nothing to write, nothing broken, nothing lost or waiting for a person
        (unless accepted), and every Session of `drop_list` is covered by a consistent pair."""
        c = self.counts
        return not (c["todo"] or self.blocking or c["broken"] or self.uncovered)


_ULID_LINE = re.compile(rf"(?:agora:)?({_ULID})")


def read_accept(path: str, items: list[CopyItem]) -> set[str]:
    """The new ULIDs in an accept file (one per line, `#` comments and blank lines ignored), or MigrateError.

    Fail-closed like the must-keep list: a file that cannot be read, a line that is not a ULID, or a ULID that
    is not the new Session of any pair in this plan (a typo) is an error. The message holds line numbers only."""
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except OSError as e:
        raise MigrateError(f"accept 檔 {path} 讀不到（{e.strerror or type(e).__name__}）") from None
    except UnicodeDecodeError:
        raise MigrateError(f"accept 檔 {path} 不是 UTF-8") from None
    known = {i.new for i in items if i.kind == "copy"}
    ids: set[str] = set()
    bad: list[int] = []
    for n, line in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        line = line.split("#", 1)[0].strip(" \t")
        if not line:
            continue
        m = _ULID_LINE.fullmatch(line)
        if not m or m.group(1) not in known:
            bad.append(n)
        else:
            ids.add(m.group(1))
    if bad:
        raise MigrateError(f"accept 檔 {path} 第 {', '.join(map(str, bad))} 行不是這次計畫裡某一對的新 ULID；全部不採用")
    return ids


def summarize(items: list[CopyItem], report: dict, accept: "set[str] | frozenset[str]" = frozenset()) -> Summary:
    sessions = check_report(report)
    drop = {u for u in (report.get("drop_list") or []) if isinstance(u, str)}
    covered = set()
    accepted: list[str] = []
    blocking = 0
    for i in items:
        if i.kind != "copy":
            continue
        ok = i.status == "done" or (i.status == "attention" and i.new in accept)
        if i.blocking and i.new in accept and i.status != "todo":
            accepted.append(i.new)
        elif i.blocking:
            blocking += 1
        if ok:
            covered.add(i.old)
            covered.update(i.others)          # the superseded ones: listed only, on purpose
    counts = {"todo": 0, "done": 0, "attention": 0, "na": 0, "broken": 0}
    for i in items:
        if i.kind != "copy":
            counts[i.kind] += 1
            continue
        counts["todo" if i.status == "todo" else "done" if i.status == "done" else "attention"] += 1
    local = [s for s in sessions if s.get("class") == "local-present"]
    return Summary(counts, blocking, sorted(accepted), sorted(drop - covered),
                   bool(local) and all(not s.get("replacement") for s in local))


def current_matches(paths: store.Paths, item: CopyItem) -> list[str]:
    """Names of the fields whose value on the new header is not what the plan says (read again, read-only)."""
    db = open_index(paths)
    try:
        hdr = _headers(db, [item.new]).get(item.new)
    finally:
        db.close()
    if hdr is None:
        return sorted(item.fields)
    return sorted(k for k, v in item.fields.items() if hdr.get(k) != v)


def describe(item: CopyItem) -> str:
    """One line: lengths for the free text, values for case/refs/tags. Never a title or a description."""
    if item.old is None:
        olds = "、".join(item.others)
        label = "預期不適用" if item.kind == "na" else "不抄"
        if item.new is None:
            return f"agora:{olds} {label}：{item.reason}"
        return f"agora:{item.new} {label}：{item.reason}（舊的：{olds}）"
    parts = []
    for k in item.changes:
        v = item.fields[k]
        if k in ("case", "refs", "tags", "verified"):
            parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        else:
            parts.append(f"{k}(長度 {len(json.dumps(v, ensure_ascii=False))})")
    head = f"agora:{item.new} ← agora:{item.old}："
    body = ("已一致" if item.status == "done" else "會改 " + "、".join(parts) if item.changes else "欄位已寫入，但有東西要人處理")
    extra = f"；沒抄（刻意）：{'、'.join(item.dropped)}" if item.dropped else ""
    extra += f"；丟掉了（指向舊 id、沒有新 id）：{'；'.join(item.lost)}" if item.lost else ""
    extra += f"；需人決定（欄位提到舊 id，不猜）：{'、'.join(item.review)}" if item.review else ""
    extra += f"；同來源另有舊的：{'、'.join(item.others)}（只列出）" if item.others else ""
    return head + body + extra
