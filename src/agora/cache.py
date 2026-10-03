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
import re
import time
from datetime import datetime
from pathlib import Path

from agora import header as h
from agora import store
from agora.agents.base import reading

_UUID = re.compile(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


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

    The write is atomic and its staging name unique (review K4): a fixed `.tmp`
    name meant two threads caching two sessions in one directory raced.
    """
    path, when = paths.reading / agent.name / f"{session_id}.md", _stamp(updated_at)
    if is_fresh(paths, agent.name, session_id, updated_at):
        return path.read_text(encoding="utf-8")
    text = reading(agent, agent.export(session_id).raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_atomic(path, text)
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


_line = store.warn


def _plan_one(session_id: str, agents: dict) -> tuple[str, str | None, Exception | None]:
    """(kind, id, complaint): parse up front so a bad id is that id's failure only."""
    try:
        kind, bare = _split(session_id, agents)
    except ValueError as e:
        return "", None, e
    return kind, bare, None


def _split(session_id: str, agents: dict) -> tuple[str, str]:
    """(`agora` or an agent name, the id under it) for one id given to pull or push.

    A bare id is an agora one (R5). A bare `ses_…` or a bare uuid is an agent
    session id, and reading it as an agora ULID would go looking for a folder
    that cannot exist and report the session as missing from Drive - so the user
    is told which prefix to write instead (review Q5).
    """
    prefix, sep, bare = session_id.partition(":")
    if not sep:
        if session_id.startswith("ses_") or _UUID.fullmatch(session_id):
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
    text is what goes - but only after asking the agent, which may well still have
    it (review M2); if it does, this is an ordinary pull.
    """
    index = store.Index(paths)
    # A batch of agent ids needs no Drive at all: asking anyway would make an
    # offline machine fail a pull that has nothing to do with Drive (review S2-3).
    wanted = list(dict.fromkeys(ids))   # the same id twice is one job, one line (S2-7)
    plan = [_plan_one(session_id, agents) for session_id in wanted]
    drive, remote = None, None
    listed: dict[str, dict] = {}
    done = failed = skipped = 0
    for k, (session_id, (kind, bare, complaint)) in enumerate(zip(wanted, plan), 1):
        store.progress("pull", k, len(plan))
        fresh = False         # P3: had nothing to fetch, so it is a skip and not a pull
        try:
            if complaint:
                raise complaint
            if kind == "agora":
                if drive is None:
                    drive = store.Drive(paths)
                    remote = store._listing_with_md5(drive)   # once for the batch (docs/perf.md)
                if isinstance(remote, store.StoreError):
                    raise store.StoreError(f"連不上 Drive：{remote}")
                before = drive.fetched
                _pull_agora(paths, drive, index, remote, bare, not_exist_delete)
                fresh = (drive.fetched == before and bare in (remote or {})   # fetched nothing,
                         and not (paths.outbox / bare).is_dir() and index.header(bare) is not None)
            else:
                if kind not in listed:
                    listed[kind] = _listed(agents, kind)
                if bare in listed[kind] or not not_exist_delete:
                    fresh = is_fresh(paths, kind, bare, listed[kind].get(bare))
                    local_reading(paths, agents[kind], bare, listed[kind].get(bare))
                elif not listed[kind] and _cached(paths, kind):
                    # F5: an agent that lists nothing while its cache has something
                    # is an agent we could not read, not one that lost everything.
                    _line(f"{kind} 那邊的清單讀不到（可能是資料庫沒了或認不出結構），不刪快取")
                else:
                    _drop_reading(paths, kind, bare)   # the agent really lost it (review M2)
            done, skipped = done + (not fresh), skipped + fresh
        except Exception as e:      # one session must not stop the rest (review K5)
            failed += 1
            _line(f"{session_id} 拉不到：{e}")
    if skipped:
        _line(f"已經是新的，略過 {skipped} 個")
    return done, failed


def _cached(paths: store.Paths, agent_name: str) -> bool:
    """Whether we hold any full text for this agent."""
    folder = paths.reading / agent_name
    return folder.is_dir() and any(folder.glob("*.md"))


def _drop_reading(paths: store.Paths, agent_name: str, session_id: str) -> None:
    """The cached full text of an agent session that is gone from the agent."""
    path = paths.reading / agent_name / f"{session_id}.md"
    had = path.is_file()
    path.unlink(missing_ok=True)
    _line(f"{agent_name}:{session_id} 那邊已經沒有，" + ("快取已刪" if had else "本機本來就沒有快取"))


def _absent(paths: store.Paths, ulid: str, said: str) -> None:
    """Drive does not have this session: one line, nothing done (`said` words it)."""
    if not (paths.mirror / ulid).exists():
        raise store.StoreError("本機和雲端都沒有這個 Session")   # a typo, not a deletion
    _line(f"{ulid} 雲端沒有，{said}")


def _pull_agora(paths: store.Paths, drive: store.Drive, index: store.Index,
                remote: dict | None, ulid: str, not_exist_delete: bool) -> None:
    """One `agora:` id from Drive, into the mirror and the index."""
    if ulid in store.queued_for_trash(paths):
        # raised, not just said: pull counts what it refused as done and would end
        # with「拉下 1 個」and exit 0 (review S3)
        raise store.StoreError(f"{ulid} 正在刪除，不能 pull")
    if ulid not in (remote or {}):
        if not not_exist_delete:
            _absent(paths, ulid, "本機的不動")
            return
        if (paths.outbox / ulid).is_dir():
            _line(f"{ulid} 還沒上傳，不能刪")
            return
        if store.continuing(paths, ulid):
            _line(f"{ulid} 正在接續，不能刪")
            return
        if remote is None:
            # G3: Drive has no sessions/ at all - a broken folder id or token, not
            # evidence of a delete (N12). sync and `_lost_in_cloud` both read it as
            # "do not know"; deleting on it would throw away a real session.
            raise store.StoreError("Drive 上找不到 sessions/，不能確定它是被刪掉的（先修好連線再說）")
        store.forget_local(paths, ulid)
        _line(f"{ulid} 雲端沒有，本機的副本已刪")
        return
    files = remote[ulid]
    if (paths.outbox / ulid).is_dir():
        # What is in the outbox is newer than anything on Drive: pulling would put
        # the older copy in the mirror and the session would go backwards (S2-7).
        _line(f"{ulid} 還沒上傳，不覆蓋本機這一份")
        return
    # S1/G3: an unfinished session (its header names a raw that is not there yet)
    # stays out of the index and out of the mirror - otherwise it is searchable and
    # continuable. One place decides that, for sync and for pull alike.
    hdr = store.mirror_one(paths, drive, index, ulid, remote[ulid])
    if hdr is None:
        _line(f"{ulid} 雲端上的 raw 還沒齊，先不建索引")
        return
    if (h.agora_of(hdr).get("raw") or {}).get("file"):
        store.fetch_raw(paths, drive, ulid, hdr)   # a no-op when the local copy is the right one
    # `mirror_one` already indexed it; this second pass is what picks up the raw
    # that `fetch_raw` may just have fetched (N8), so the row has the whole session.
    store.index_file(index, paths.mirror / ulid / "session.md")   # searchable again



def push(paths: store.Paths, ids: list[str], agents: dict, *,
         not_exist_upload: bool = False) -> tuple[int, int]:
    """`agora push session <agora id>…`: send the given sessions to Drive; (pushed, failed).

    Only `session.md` and the raw its header names go up (R7), and only for the
    ids given (R5). A session Drive no longer has is *not* quietly brought back -
    that was K1, and it is how another machine's delete gets undone without
    anyone deciding to - so it gets one line and nothing happens (review Q1).
    """
    wanted = list(dict.fromkeys(ids))   # the same id twice is one job, one line (S2-7)
    drive = store.Drive(paths)
    # The lock, and wait for it: push is the one command whose contract is "it is on
    # Drive when this returns" (N2). No timeout - one throttled rclone call alone can
    # take 50 seconds - but Ctrl-C still gets out, and every 10 seconds it says why.
    # The lock is taken without blocking, or that saying could never happen (review R7).
    held, said_at = None, time.monotonic()
    while held is None:
        held = store.hold_upload_lock(paths)
        if held is None:
            if time.monotonic() - said_at >= 10:
                _line("背景上傳中，還在等…")
                said_at = time.monotonic()
            time.sleep(0.2)
    with held:
        staged = store.outbox_ulids(paths)
        left = store.push_outbox(drive, paths)   # staged writes first; those are the same sessions
    if left:
        _line(f"outbox 還有 {len(left)} 筆沒上傳成功")
    listing = store._listing_with_md5(drive)   # offline: every id here fails, and says so
    done = failed = 0
    for k, agora_id in enumerate(wanted, 1):
        store.progress("push", k, len(wanted))
        try:
            kind, ulid = _split(agora_id, agents)
            if kind != "agora":
                raise ValueError(f"push 只吃 agora 的 session id，收到 {agora_id}")
            if ulid in store.queued_for_trash(paths):   # M5/N10: on its way to the trash
                raise store.StoreError(f"{agora_id} 正在刪除，不能 push")
            if isinstance(listing, store.StoreError):
                raise store.StoreError(f"連不上 Drive：{listing}")
            if ulid in staged:
                if ulid in left:
                    raise store.StoreError("還沒上傳成功，仍在 outbox")   # review S2-2
            elif (paths.outbox / ulid).is_dir():
                # staged after our snapshot, so it goes through the same batch: it is
                # verified against Drive before the folder goes, like every other entry
                # (H1 - `push_one` uploaded, checked and deleted with no comparison at all)
                if ulid in store.upload_batch(drive, paths):
                    raise store.StoreError("還沒上傳成功，仍在 outbox")   # review S2-2
            elif listing is None or ulid not in listing:
                if not not_exist_upload:
                    _absent(paths, ulid, "沒有傳")
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