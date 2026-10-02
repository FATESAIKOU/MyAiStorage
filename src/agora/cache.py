"""Local caches (design 5.10), filled lazily and refreshed in one go by `agora cache`.

The Drive mirror (cache/sessions/) is agora's own cache: session.md holds a
session's reading version and raws are fetched when first needed. Agent
sessions not in agora get their reading version in cache/reading/<agent>/<id>.md,
its mtime set to the session's own update time, so a newer session is stale.
"""

from __future__ import annotations

import os
from datetime import datetime

from agora import store
from agora.agents.base import reading


def _stamp(updated_at: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).timestamp() if updated_at else None
    except ValueError:
        return None


def local_reading(paths: store.Paths, agent, session_id: str, updated_at: str | None = None) -> str:
    """An agent session's full text: from the cache when it is fresh, else read now and kept."""
    path = paths.reading / agent.name / f"{session_id}.md"
    when = _stamp(updated_at)
    if path.exists() and (when is None or path.stat().st_mtime >= when):
        return path.read_text(encoding="utf-8")
    text = reading(agent, agent.export(session_id).raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    if when is not None:
        os.utime(path, (when, when))
    return text


def refresh_local(paths: store.Paths, agents: list) -> tuple[int, int]:
    """`agora cache local`: every agent session on this machine into the cache; (written or fresh, failed)."""
    done = failed = 0
    for agent in agents:
        listed = agent.list_sessions()
        for n, s in enumerate(listed, 1):
            print(f"[agora] {agent.name} {n}/{len(listed)}  {s.session_id[-8:]}")
            try:
                local_reading(paths, agent, s.session_id, s.updated_at)
                done += 1
            except Exception as e:   # one unreadable session must not stop the rest
                failed += 1
                print(f"[agora] {agent.name} {s.session_id} 讀不到：{e}")
    return done, failed


def refresh_agora(paths: store.Paths) -> tuple[int, int]:
    """`agora cache agora`: sync session.md files, then every raw not here yet; (done, failed)."""
    index = store.sync(paths)
    drive = store.Drive(paths)
    ulids = sorted(index.known())
    done = failed = 0
    for n, ulid in enumerate(ulids, 1):
        print(f"[agora] Agora {n}/{len(ulids)}  {ulid[-8:]}")
        hdr = index.header(ulid) or {}
        try:
            if (hdr.get("agora") or {}).get("raw"):
                store.fetch_raw(paths, drive, ulid, hdr)
            done += 1
        except store.StoreError as e:
            failed += 1
            print(f"[agora] {ulid} 下載不到：{e}")
    return done, failed


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


def sync_up(paths: store.Paths) -> None:
    """`agora sync`: the outbox, then every mirrored file written back to Drive, same names overwritten."""
    drive = store.Drive(paths)
    failed = store.push_outbox(drive, paths)
    if paths.mirror.is_dir():
        drive.upload_tree(paths.mirror)
    print(f"[agora] 已寫回 Drive" + (f"；outbox 還有 {len(failed)} 筆沒上傳成功" if failed else ""))
