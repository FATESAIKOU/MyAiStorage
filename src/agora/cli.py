"""The agora command (design.md section 5).

    agora search session '<keyword>' [--header key=value]... [--no-sync]
    agora import --format opencode|claude --session-id <id> [--header ...]...
    agora merge-session <id> <id> [...] [--header ...]...
    agora continue-session <id> --agent opencode|claude [--dir <dir>]
    agora show <id> [--raw]
    agora sync

Every command prints the agora id first, so outputs chain into the next one.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import signal
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agora import header as h
from agora import store
from agora.agents.base import Agent, AgentError, Exported, Launch

AGENTS = ("opencode", "claude")
EXIT_INPUT = 1       # the request cannot be done: unknown id, nothing to import, too few ids
EXIT_ERROR = 2       # header, Drive, agent or unexpected error (argparse usage errors are 2 too)
EXIT_IN_OUTBOX = 3   # saved locally, not on Drive yet (N13)


class InputError(Exception):
    """The user asked for something that does not exist or cannot be done."""


def load_agent(name: str) -> Agent:
    if name not in AGENTS:
        raise InputError(f"不支援的 agent：{name}（可用：{', '.join(AGENTS)}）")
    module = importlib.import_module(f"agora.agents.{name}")
    return module.ADAPTER


def _now_iso() -> str:
    return datetime.fromtimestamp(store.now(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ulid_of(agora_id: str) -> str:
    return agora_id.split(":", 1)[1] if agora_id.startswith("agora:") else agora_id


def _new_header(relation: str, parents: list[dict], updates: dict) -> dict:
    stamp = _now_iso()
    hdr = {
        "header": h.HEADER_VERSION, "entity": "agora", "type": "session",
        "id": f"agora:{h.new_ulid(int(store.now() * 1000))}", "title": None,
        "created_at": stamp, "updated_at": stamp, "refs": [], "case": None,
        "note": None, "tags": [], "relation": relation, "parents": parents,
    }
    return _apply(hdr, updates)


def _apply(hdr: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if key in h.MULTI:
            hdr[key] = list(dict.fromkeys((hdr.get(key) or []) + value))
        else:
            hdr[key] = value
    return hdr


def _source(agent: Agent, exported: Exported) -> dict:
    return {
        "agent": agent.name, "session_id": exported.session_id, "dir": exported.dir,
        "host": socket.gethostname(), "agent_version": exported.agent_version,
        "created_at": exported.created_at,
    }


def _save(paths: store.Paths, hdr: dict, body: str, raw: bytes | None) -> tuple[str, bool]:
    """Stage into the outbox, then try to push it now."""
    folder = store.stage(paths, hdr, body, raw)
    store.remember(paths, folder)
    try:
        store.push_one(store.Drive(paths), folder)
    except store.StoreError as e:
        print(f"[agora] 上傳失敗，已存入 outbox，下次 sync 會再送：{e}", file=sys.stderr)
        return hdr["id"], False
    return hdr["id"], True


def _emit(saved: tuple[str, bool]) -> int:
    print(saved[0])
    return 0 if saved[1] else EXIT_IN_OUTBOX


def _header_for(index: store.Index, agora_id: str) -> dict:
    hdr = index.header(_ulid_of(agora_id))
    if hdr is None:
        raise InputError(f"找不到 {agora_id}（先 agora sync？）")
    return hdr


def _body_for(paths: store.Paths, agora_id: str) -> str:
    _, body = h.split_document((paths.mirror / _ulid_of(agora_id) / "session.md").read_text(encoding="utf-8"))
    return body


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_search(args, paths: store.Paths) -> int:
    filters = h.parse_search_filters(args.header)
    index = store.Index(paths) if args.no_sync else store.sync(paths, throttle=True)
    seen: dict[tuple, str] = {}
    outbox = store.outbox_ulids(paths)
    for ulid, hdr, snippet in index.search(args.keyword, filters):
        source = hdr.get("source") or {}
        key = (source.get("agent"), source.get("session_id"))
        if source.get("session_id") and key in seen:
            print(f"[agora] {seen[key]} 與 agora:{ulid} 來自同一個來源 Session", file=sys.stderr)
            continue
        seen[key] = f"agora:{ulid}"
        date = str(source.get("created_at") or hdr.get("created_at") or "")[:10]
        mark = "  (未上傳)" if ulid in outbox else ""
        print(f"agora:{ulid}  {date}  {source.get('agent') or hdr.get('relation')}  {snippet}{mark}")
    return 0


def cmd_import(args, paths: store.Paths) -> int:
    agent = load_agent(args.format)
    updates = h.parse_header_args(args.header)
    exported = agent.export(args.session_id)
    if exported.message_count <= 0:
        raise InputError(f"{args.session_id} 沒有任何訊息，不匯入")
    body = agent.reading(exported.raw)
    index = store.sync(paths)  # never throttled: we must see other machines' imports (S6)
    existing = index.by_source(agent.name, exported.session_id)
    if existing:
        old = index.header(existing[0])
        unchanged = (old.get("raw") or {}).get("md5") == store.hashlib.md5(exported.raw).hexdigest()
        if unchanged and not updates:
            print(f"agora:{existing[0]}")
            return 0
        if unchanged or not index.children(existing[0]):
            hdr = _apply(old, updates)
            hdr["source"] = _source(agent, exported)
            hdr["updated_at"] = _now_iso()
            hdr["title"] = hdr.get("title") or exported.title
            return _emit(_save(paths, hdr, body, exported.raw))
        # Already continued or merged from: keep the old version and branch (S7).
        parents = [{"id": f"agora:{existing[0]}", "raw_md5": (old.get("raw") or {}).get("md5")}]
        hdr = _new_header("import", parents, updates)
    else:
        hdr = _new_header("import", [], updates)
    hdr["source"] = _source(agent, exported)
    hdr["title"] = hdr.get("title") or exported.title
    return _emit(_save(paths, hdr, body, exported.raw))


def cmd_merge(args, paths: store.Paths) -> int:
    ids = [i.strip() for raw in args.ids for i in raw.split(",") if i.strip()]
    if len(ids) < 2:
        raise InputError("merge-session 至少要兩個 Session")
    index = store.sync(paths, throttle=True)
    parents, parts, titles = [], [], []
    for agora_id in ids:
        agora_id = f"agora:{_ulid_of(agora_id)}"
        parent = _header_for(index, agora_id)
        parents.append({"id": agora_id, "raw_md5": (parent.get("raw") or {}).get("md5")})
        parts.append(f"# from {agora_id}\n\n{_body_for(paths, agora_id)}")
        titles.append(str(parent.get("title") or agora_id))
    hdr = _new_header("merge", parents, h.parse_header_args(args.header))
    hdr["title"] = hdr.get("title") or "merge: " + " + ".join(titles)
    return _emit(_save(paths, hdr, "\n".join(parts), None))


def _write_pending(paths: store.Paths, record: dict):
    """Create the pending record already locked, so nobody can grab it in between (C3)."""
    paths.pending.mkdir(parents=True, exist_ok=True)
    path = paths.pending / f"{_ulid_of(record['agora_id'])}.json"
    tmp = path.with_suffix(".json.tmp")          # not matched by *.json, so nobody sees it yet (R5)
    lock = open(tmp, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    lock.write(json.dumps(record, ensure_ascii=False, indent=2))
    lock.flush()
    os.rename(tmp, path)                         # appears already locked
    return path, lock


def _finish(paths: store.Paths, record: dict) -> tuple[str, bool] | None:
    """Store what the agent produced as a new session; None if nothing new."""
    agent = load_agent(record["agent"])
    launch = Launch(argv=[], cwd=record["dir"], agent_session_id=record.get("agent_session_id"),
                    before_count=record.get("before_count", 0))
    exported = agent.collect(launch)
    if exported is None:
        return None
    hdr = _new_header("continue", [record["parent"]], record.get("header_updates") or {})
    hdr["id"] = record["agora_id"]
    hdr["source"] = _source(agent, exported)
    hdr["title"] = hdr.get("title") or record.get("title") or exported.title   # --header wins (C6)
    return _save(paths, hdr, agent.reading(exported.raw), exported.raw)


def recover_pending(paths: store.Paths, *, notice_only: bool = False) -> None:
    """Finish continue-sessions whose agora and agent both ended before collecting (S3).

    The lock is held by agora and inherited by the agent (pass_fds), so it is
    only free once both are gone (N2, C1).
    """
    if not paths.pending.exists():
        return
    for tmp in paths.pending.glob("*.json.tmp"):   # left by a crash before the rename
        if store.now() - tmp.stat().st_mtime > 86400:
            tmp.unlink(missing_ok=True)
    for path in sorted(paths.pending.glob("*.json")):
        try:
            f = open(path)
        except FileNotFoundError:
            continue
        with f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if not path.exists():          # another command just finished it (C3)
                continue
            if notice_only:
                print(f"[agora] 有中斷的接續待補存：{path.stem}（跑 agora sync 補存）", file=sys.stderr)
                continue
            try:
                record = json.loads(f.read())
                missing = [k for k in ("agora_id", "agent", "dir", "parent") if k not in record]
                if missing:
                    raise KeyError(", ".join(missing))
            except (json.JSONDecodeError, KeyError, UnicodeDecodeError, TypeError) as e:
                store.quarantine(path, paths.pending / ".bad", f"pending {path.name} 壞了：{e}")
                continue
            try:
                saved = _finish(paths, record)
            except Exception as e:   # a bug or outage while finishing: keep the record and retry (R4)
                print(f"[agora] 補存 {path.stem} 失敗，下次再試：{e}", file=sys.stderr)
                continue
            path.unlink(missing_ok=True)
        if saved:
            print(f"[agora] 補存了中斷的接續：{saved[0]}", file=sys.stderr)


def cmd_continue(args, paths: store.Paths) -> int:
    agent = load_agent(args.agent)
    index = store.sync(paths, throttle=True)
    source_id = f"agora:{_ulid_of(args.id)}"
    parent = _header_for(index, source_id)
    src = parent.get("source") or {}
    workdir = Path(args.dir or (src.get("dir") if src.get("dir") and Path(src["dir"]).is_dir() else os.getcwd()))
    workdir = workdir.expanduser().resolve()   # C5
    print(f"[agora] 工作目錄：{workdir}", file=sys.stderr)
    native = parent.get("relation") != "merge" and src.get("agent") == agent.name and parent.get("raw")
    parent_md5 = (parent.get("raw") or {}).get("md5")
    if native:
        raw = store.fetch_raw(paths, store.Drive(paths), _ulid_of(source_id), parent)
        parent_md5 = store.hashlib.md5(raw).hexdigest()   # what we really continued from (C9)
        launch = agent.start_native(raw, workdir)
    else:
        reading = paths.state / "reading" / f"{_ulid_of(source_id)}.md"
        reading.parent.mkdir(parents=True, exist_ok=True)
        reading.write_text(_body_for(paths, source_id), encoding="utf-8")
        launch = agent.start_injected(reading, workdir)
    record = {
        "agora_id": f"agora:{h.new_ulid(int(store.now() * 1000))}", "agent": agent.name,
        "agent_session_id": launch.agent_session_id, "dir": launch.cwd,
        "parent": {"id": source_id, "raw_md5": parent_md5},
        "title": parent.get("title"), "before_count": launch.before_count,
        "started_at": _now_iso(), "header_updates": h.parse_header_args(args.header),
    }
    pending, lock = _write_pending(paths, record)
    store._fault("before-agent-launch")
    # Ctrl-C belongs to the agent. The child gets the default handler back
    # before exec, otherwise it would inherit the ignore (N1). The child also
    # inherits the pending lock, so the record stays locked while the agent
    # lives even if agora itself is killed (C1).
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        subprocess.run(launch.argv, cwd=launch.cwd, env={**os.environ, **launch.env},
                       pass_fds=(lock.fileno(),),
                       preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    finally:
        signal.signal(signal.SIGINT, previous)
    store._fault("before-finalize")
    saved = _finish(paths, record)
    pending.unlink()
    lock.close()
    if not native:
        reading.unlink(missing_ok=True)   # only the agent needed it (D5)
    if saved is None:
        print("[agora] 這次沒有新內容，沒有存", file=sys.stderr)
        return 0
    return _emit(saved)


def cmd_show(args, paths: store.Paths) -> int:
    index = store.sync(paths, throttle=True)
    agora_id = f"agora:{_ulid_of(args.id)}"
    hdr = _header_for(index, agora_id)
    if args.raw:
        sys.stdout.buffer.write(store.fetch_raw(paths, store.Drive(paths), _ulid_of(agora_id), hdr))
        return 0
    print(h.dump_document(hdr, _body_for(paths, agora_id)), end="")
    return 0


def cmd_sync(args, paths: store.Paths) -> int:
    index = store.sync(paths)
    print(f"[agora] {len(index.known())} 個 Session，outbox {store.outbox_count(paths)} 筆", file=sys.stderr)
    return 0


def _with_header(parser: argparse.ArgumentParser, help: str | None = None) -> argparse.ArgumentParser:
    parser.add_argument("--header", action="append", default=[], help=help)
    return parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agora", description="找、合、接 coding agent 的 Session")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="找 Session")
    s.add_argument("kind", choices=["session"])
    s.add_argument("keyword", nargs="?", default="")
    _with_header(s, "key=value（agent、relation、case、tag、ref、title）")
    s.add_argument("--no-sync", action="store_true")
    s.set_defaults(func=cmd_search)

    i = sub.add_parser("import", help="初次引入一個 Session")
    i.add_argument("--format", required=True, choices=AGENTS)
    i.add_argument("--session-id", required=True)
    _with_header(i)
    i.set_defaults(func=cmd_import)

    m = sub.add_parser("merge-session", help="把幾個 Session 合成一個新的")
    m.add_argument("ids", nargs="+")
    _with_header(m)
    m.set_defaults(func=cmd_merge)

    c = sub.add_parser("continue-session", help="用某個 agent 接著做，結束時存回")
    c.add_argument("id")
    c.add_argument("--agent", required=True, choices=AGENTS)
    c.add_argument("--dir", default=None)
    _with_header(c)
    c.set_defaults(func=cmd_continue)

    sh = sub.add_parser("show", help="看 header 與閱讀版")
    sh.add_argument("id")
    sh.add_argument("--raw", action="store_true")
    sh.set_defaults(func=cmd_show)

    sy = sub.add_parser("sync", help="推 outbox、拉 Drive、重建索引")
    sy.set_defaults(func=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = store.Paths.from_env()
    try:
        recover_pending(paths, notice_only=getattr(args, "no_sync", False))
        if store.outbox_count(paths):
            print(f"[agora] outbox 有 {store.outbox_count(paths)} 筆未上傳", file=sys.stderr)
        if store.bad_count(paths):
            print(f"[agora] 有 {store.bad_count(paths)} 筆壞檔放在 {paths.state}/*/.bad，請檢查", file=sys.stderr)
        return args.func(args, paths)
    except InputError as e:
        print(f"[agora] {e}", file=sys.stderr)
        return EXIT_INPUT
    except (h.HeaderError, store.StoreError, AgentError) as e:
        print(f"[agora] {e}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:   # never let one broken file brick every command (C2)
        if os.environ.get("AGORA_DEBUG"):
            raise
        print(f"[agora] 非預期的錯誤：{type(e).__name__}: {e}（AGORA_DEBUG=1 看細節）", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
