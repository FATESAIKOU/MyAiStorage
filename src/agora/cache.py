"""Local caches (design 5.10), filled one session at a time by `agora pull`.

The Drive mirror (cache/sessions/) is agora's own cache: session.md holds a
session's reading version and raws are fetched when first needed. Agent
sessions not in agora get their reading version in cache/reading/<agent>/<id>.md,
its mtime set to the session's own update time, so a newer session is stale.

`agora pull` and `agora push` take the ids they act on (T1 R5) - there is no
`--all`, because everything else agora does is id-shaped too, and "all of it" is
a thing to ask for from the screen where you can see it. Neither of them invents
a session that is not there: an id Drive no longer has gets one line and nothing
else, because putting it back is not what anybody asked for (review Q1).
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from agora import header as h
from agora import store
from agora.agents.base import reading


def _stamp(updated_at: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).timestamp() if updated_at else None
    except ValueError:
        return None


def is_fresh(paths: store.Paths, agent_name: str, session_id: str, updated_at: str | None) -> bool:
    """Whether the cached full text is there and not older than the session."""
    path, when = paths.reading / agent_name / f"{session_id}.md", _stamp(updated_at)
    return path.exists() and (when is None or path.stat().st_mtime >= when)


def local_reading(paths: store.Paths, agent, session_id: str, updated_at: str | None = None) -> str:
    """An agent session's full text: from the cache when it is fresh, else read now and kept.

    The staging file is unique per call (review K4): a fixed `.tmp` name meant two
    threads caching two sessions in one directory raced, and one of them lost the
    write it was in the middle of.
    """
    path, when = paths.reading / agent.name / f"{session_id}.md", _stamp(updated_at)
    if is_fresh(paths, agent.name, session_id, updated_at):
        return path.read_text(encoding="utf-8")
    text = reading(agent, agent.export(session_id).raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)   # gone on success; a leftover never clutters the cache
    if when is not None:
        os.utime(path, (when, when))
    return text


def search_cached(paths: store.Paths, agent_name: str, keyword: str):
    """Ids of cached agent sessions whose full text contains keyword (NFKC, case-insensitive), as found."""
    needle = store.normalize(keyword)
    folder = paths.reading / agent_name
    if not needle or not folder.is_dir():
        return
    for path in sorted(folder.glob("*.md")):
        try:
            if needle in store.normalize(path.read_text(encoding="utf-8")):
                yield path.stem
        except OSError:
            continue


def _progress(word: str, k: int, total: int) -> None:
    """One line per item, '… k/N …' (T1 R3); stderr so the ids stay on stdout."""
    print(f"[agora] {word} {k}/{total}", file=sys.stderr)


def _line(message: str) -> None:
    print(f"[agora] {message}", file=sys.stderr)


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _split(session_id: str, agents: dict) -> tuple[str, str]:
    """(`agora` or an agent name, the id under it) for one id given to pull or push.

    A bare id is an agora one (R5). A bare `ses_…` or a bare uuid is an agent
    session id, and reading it as an agora ULID would go looking for a folder
    that cannot exist and report the session as missing from Drive - so the user
    is told which prefix to write instead (review Q5).
    """
    prefix, sep, bare = session_id.partition(":")
    if not sep:
        if session_id.startswith("ses_") or _looks_like_uuid(session_id):
            names = " 或 ".join(f"{name}:" for name in agents)
            raise ValueError(f"{session_id} 是 agent 的 session id，請寫前綴（{names}）")
        return "agora", session_id
    if prefix not in ("agora", *agents):
        raise ValueError(f"看不懂的 id：{session_id}")
    return prefix, bare


def _listed(agents: dict, name: str) -> dict:
    """One agent's sessions as {id: updated_at}, so `is_fresh` can tell stale from uncached."""
    return {s.session_id: s.updated_at for s in agents[name].list_sessions()}


def pull(paths: store.Paths, ids: list[str], agents: dict, *,
         not_exist_delete: bool = False) -> tuple[int, int]:
    """`agora pull session <id>…`: bring the given sessions here; (pulled, failed).

    An `agora:` id (or a bare ULID) comes off Drive: session.md, plus the raw its
    header names, so the mirror is whole. An `opencode:`/`claude:` id is an agent
    session on this machine and its reading version goes into the cache. Cached
    and not stale is a skip, which is what makes a re-run after Ctrl-C cheap (R4).

    An id Drive does not have is left alone with one line saying so: pulling is
    not deleting, and dropping a session is the user's call, not this command's
    (Q1). `--not-exist-delete` is that call: the local copy goes, except when the
    session is still in the outbox or a continue is running on it - work that has
    not reached Drive yet is not something to tidy away. For an agent id the flag
    means "the agent does not have this session any more", so its cached full
    text is what goes.
    """
    drive = store.Drive(paths)
    index = store.Index(paths)
    remote = drive.list_sessions()          # one listing for the whole batch (docs/perf.md)
    listed: dict[str, dict] = {}
    done = failed = 0
    for k, session_id in enumerate(ids, 1):
        _progress("pull", k, len(ids))
        try:
            kind, bare = _split(session_id, agents)
            if kind == "agora":
                _pull_agora(paths, drive, index, remote, bare, not_exist_delete)
            elif not_exist_delete:
                _drop_reading(paths, kind, bare)
            else:
                if kind not in listed:
                    listed[kind] = _listed(agents, kind)
                local_reading(paths, agents[kind], bare, listed[kind].get(bare))
            done += 1
        except Exception as e:      # one session must not stop the rest (review K5)
            failed += 1
            _line(f"{session_id} 拉不到：{e}")
    return done, failed


def _drop_reading(paths: store.Paths, agent_name: str, session_id: str) -> None:
    """The cached full text of an agent session that is gone from the agent."""
    path = paths.reading / agent_name / f"{session_id}.md"
    if path.is_file():
        path.unlink()
        _line(f"{agent_name}:{session_id} 那邊已經沒有，快取已刪")
    else:
        _line(f"{agent_name}:{session_id} 那邊已經沒有，本機本來就沒有快取")


def _pull_agora(paths: store.Paths, drive: store.Drive, index: store.Index,
                remote: dict | None, ulid: str, not_exist_delete: bool) -> None:
    """One `agora:` id from Drive, into the mirror and the index."""
    if remote is None or ulid not in remote:
        if not not_exist_delete:
            _line(f"{ulid} 雲端沒有，本機的不動")
            return
        if (paths.outbox / ulid).is_dir():
            _line(f"{ulid} 還沒上傳，不能刪")
            return
        if store.continuing(paths, ulid):
            _line(f"{ulid} 正在接續，不能刪")
            return
        store.forget_local(paths, ulid)
        _line(f"{ulid} 雲端沒有，本機的副本已刪")
        return
    files = remote[ulid]
    local = paths.mirror / ulid / "session.md"
    if not (local.exists() and store.md5_file(local) == files.get("session.md")):
        drive.download(ulid, "session.md", local)
    hdr, _ = h.split_document(local.read_text(encoding="utf-8"))
    raw = h.agora_of(hdr).get("raw") or {}
    if raw.get("file") and files.get(raw["file"]) != raw.get("md5"):
        _line(f"{ulid} 雲端上的 raw 還沒齊，下次再拉")
    elif raw.get("file"):
        store.fetch_raw(paths, drive, ulid, hdr)   # a no-op when the local copy is the right one
    store.index_mirror(paths, ulid, index)   # readable, indexed and searchable again


def push(paths: store.Paths, ids: list[str], agents: dict, *,
         not_exist_upload: bool = False) -> tuple[int, int]:
    """`agora push session <agora id>…`: send the given sessions to Drive; (pushed, failed).

    Only `session.md` and the raw its header names go up (R7), and only for the
    ids given (R5). A session Drive no longer has is *not* quietly brought back -
    that was K1, and it is how another machine's delete gets undone without
    anyone deciding to - so it gets one line and nothing happens (review Q1).
    """
    drive = store.Drive(paths)
    staged = store.outbox_ulids(paths)
    left = store.push_outbox(drive, paths)      # staged writes first: those are the same sessions
    if left:
        _line(f"outbox 還有 {len(left)} 筆沒上傳成功")
    listing = drive.list_sessions()
    done = failed = 0
    for k, agora_id in enumerate(ids, 1):
        _progress("push", k, len(ids))
        try:
            kind, ulid = _split(agora_id, agents)
            if kind != "agora":
                raise ValueError(f"push 只吃 agora 的 session id，收到 {agora_id}")
            if ulid in staged:
                pass                            # the flush above is what sent this one up
            elif (paths.outbox / ulid).is_dir():
                store.push_one(drive, paths.outbox / ulid)
            elif listing is None or ulid not in listing:
                if not not_exist_upload:
                    _line(f"{ulid} 雲端沒有，沒有傳")
                    continue
                _push_mirrored(paths, drive, ulid, need_raw=True)
            else:
                _push_mirrored(paths, drive, ulid)
            done += 1
        except Exception as e:      # one session must not stop the rest (review K5)
            failed += 1
            _line(f"{agora_id} 傳不上去：{e}")
    return done, failed


def _push_mirrored(paths: store.Paths, drive: store.Drive, ulid: str, *,
                   need_raw: bool = False) -> None:
    """One session up from the mirror: the two files its header names.

    `need_raw` is for putting back a session Drive no longer has: there the raw is
    the point of reviving it, so a header whose raw is not here is refused instead
    of sending session.md alone.
    """
    local = paths.mirror / ulid / "session.md"
    if not local.is_file():
        raise store.StoreError("本機沒有這個 Session，先 pull 或匯入")
    hdr, _ = h.split_document(local.read_text(encoding="utf-8"))
    if need_raw:
        raw = h.agora_of(hdr).get("raw") or {}
        if raw.get("file") and not (paths.mirror / ulid / raw["file"]).is_file():
            raise store.StoreError(f"標頭指到的 {raw['file']} 本機沒有，不傳半套")
    store.push_mirror(drive, paths, ulid, hdr)