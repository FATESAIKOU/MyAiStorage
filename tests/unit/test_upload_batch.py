"""The batch uploader and the local copy (change local-first-writes, tasks 2.1-2.6).

Counting rclone calls: a fixed number per batch, raws before session.md (M4); only what
Drive confirms by md5 leaves the outbox (H1); an update whose id another machine deleted
is not sent back (L7); a failure is retried by the next command; and a session written
here can be rescued after another machine deletes it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from agora import background, header as h, store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setenv("AGORA_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "config").mkdir()
    return remote


def _header(title="批次上傳", raw=b'{"v": 1}'):
    ulid = h.new_ulid()
    hdr = {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [],
           "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                     "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                     "source": {"agent": "opencode", "session_id": "ses_x",
                                "created_at": "2026-10-01T00:00:00Z"}}}
    return hdr


def _stage(paths, title="批次上傳", raw=b'{"v": 1}') -> str:
    hdr = _header(title, raw)
    folder = store.stage(paths, hdr, f"## user\n{title}\n", raw)
    store.remember(paths, folder)
    return folder.name


def _kept_header(ulid: str, title: str) -> dict:
    hdr = _header(title)
    hdr["id"] = f"agora:{ulid}"
    return hdr


def _calls(remote: Path) -> list[list[str]]:
    log = remote.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _uploads(remote: Path) -> list[list[str]]:
    return [c for c in _calls(remote) if c[0] in ("copy", "copyto", "delete", "purge")]


def _record_batches(monkeypatch) -> list[tuple[str, list[str]]]:
    """The batch calls as they happen, with the names each one listed.

    The listing file is temporary and gone afterwards, so this has to read it while
    the call is running - which also means the order is the real one.
    """
    seen: list[tuple[str, list[str]]] = []
    real = store.Drive._run

    def spy(self, *args, **kwargs):
        call = list(args)
        if "--files-from" in call:
            listed = Path(call[call.index("--files-from") + 1]).read_text().split()
            seen.append((call[0], listed))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(store.Drive, "_run", spy)
    return seen


def test_the_mirror_keeps_the_whole_session(env):
    """2.1: session.md and the raw, so another machine's delete can be undone."""
    paths = store.Paths.from_env()
    ulid = _stage(paths)
    raw = (h.agora_of(store.read_entry(paths.outbox / ulid)).get("raw") or {})["file"]
    assert (paths.mirror / ulid / "session.md").is_file()
    assert (paths.mirror / ulid / raw).is_file()

    # a new version replaces the old raw, and only one is kept
    hdr = store.read_entry(paths.outbox / ulid)
    store.stage(paths, hdr, "## user\n第二版\n", b'{"v": 2}')
    store.remember(paths, paths.outbox / ulid)
    new_raw = (h.agora_of(store.read_entry(paths.outbox / ulid)).get("raw") or {})["file"]
    assert new_raw != raw
    assert [p.name for p in (paths.mirror / ulid).glob("raw-*")] == [new_raw]


def test_three_sessions_go_up_in_a_few_rclone_calls(env, monkeypatch):
    """M4: a fixed number of calls whatever the count, raws before session.md."""
    paths = store.Paths.from_env()
    batches = _record_batches(monkeypatch)
    ulids = [_stage(paths, f"第 {i} 個") for i in range(3)]
    assert store.upload_batch(store.Drive(paths), paths) == []

    raws = [b for b in batches if any("/raw-" in n for n in b[1])]
    sessions = [b for b in batches if any(n.endswith("/session.md") for n in b[1])]
    assert batches.index(raws[0]) < batches.index(sessions[0]), batches
    assert len(_uploads(env)) <= 4, _uploads(env)
    assert len(raws[0][1]) == 3 and len(sessions[0][1]) == 3
    for ulid in ulids:
        assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_a_failed_raw_upload_sends_no_session_md_at_all(env, monkeypatch, capsys):
    """M4: a reader tells a version is finished by its session.md, so a half-uploaded
    batch is worse than no upload."""
    paths = store.Paths.from_env()
    ulids = [_stage(paths, f"第 {i} 個") for i in range(3)]
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    capsys.readouterr()

    left = store.upload_batch(store.Drive(paths), paths)
    assert sorted(left) == sorted(ulids)
    assert "不傳 session.md" in capsys.readouterr().err
    for ulid in ulids:
        assert not (env / "agora" / "sessions" / ulid).exists()


def test_only_what_drive_confirmed_leaves_the_outbox(env, monkeypatch):
    """H1: one entry comes back with the wrong md5, the other two go."""
    paths = store.Paths.from_env()
    good = [_stage(paths, f"好的 {i}") for i in range(2)]
    bad = _stage(paths, "壞掉的那個")
    real = store.md5_file
    calls = {"n": 0}

    def flaky(path):
        """Report a wrong md5 for the third one, once, at the verification step."""
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(store, "md5_file", flaky)
    drive = store.Drive(paths)
    original = store._listing_with_md5

    def listing(d):
        got = original(d)
        if bad in got:
            got[bad]["session.md"] = "0" * 32
        return got

    monkeypatch.setattr(store, "_listing_with_md5", listing)
    left = store.upload_batch(drive, paths)
    assert left == [bad]
    assert bad in store.outbox_ulids(paths)
    for ulid in good:
        assert ulid not in store.outbox_ulids(paths)


def test_a_version_staged_mid_upload_is_reported_as_still_waiting(env, monkeypatch):
    """H1, the part a later round cannot cover: after one round, an entry that was
    replaced while we were sending it must come back as "still waiting". Comparing only
    what we sent would report it as sent and leave the new version nowhere."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    hdr = store.read_entry(paths.outbox / ulid)
    real = store._listing_with_md5

    def listing(drive):
        got = real(drive)
        if not getattr(listing, "done", False):
            listing.done = True
            hdr["title"] = "第二版"
            store.stage(paths, hdr, "## user\n第二版\n", b'{"v": 2}')
            store.remember(paths, paths.outbox / ulid)
        return got

    monkeypatch.setattr(store, "_listing_with_md5", listing)
    assert store.upload_batch(store.Drive(paths), paths) == [ulid]
    assert ulid in store.outbox_ulids(paths)
    assert "第二版" in (paths.outbox / ulid / "session.md").read_text(encoding="utf-8")


def test_an_update_someone_else_deleted_is_not_sent_back(env, monkeypatch, capsys):
    """L7 / N5: the `.update` marker says this overwrites an id Drive had. If that id is
    gone now, putting it back is not ours to decide."""
    import shutil
    paths = store.Paths.from_env()
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []   # it is on Drive now
    shutil.rmtree(env / "agora" / "sessions" / ulid)            # another machine deletes it
    hdr = store.read_entry(paths.outbox / ulid) if (paths.outbox / ulid).exists() else None
    hdr = hdr or _kept_header(ulid, "原本的")
    folder = store.stage(paths, hdr, "## user\n改過了\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)                   # written by whoever updates an id
    capsys.readouterr()

    left = store.upload_batch(store.Drive(paths), paths)
    assert left == [ulid]
    assert ulid in store.outbox_ulids(paths)     # it stays, for the user to decide
    assert not (env / "agora" / "sessions" / ulid).exists()
    err = capsys.readouterr().err
    assert ulid in err and "雲端沒有" in err


def test_a_fresh_import_is_not_mistaken_for_an_update(env):
    """N4: the marker is the only thing that says "this one already existed"."""
    paths = store.Paths.from_env()
    ulid = _stage(paths)                          # no marker: a brand new session
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_the_next_command_after_a_failure_sends_it(env, monkeypatch, capsys):
    """Spec「背景失敗之後補傳」: nothing was lost, and the next run tries again."""
    paths = store.Paths.from_env()
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    ulid = _stage(paths)
    assert store.upload_batch(store.Drive(paths), paths) == [ulid]

    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    capsys.readouterr()
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_a_session_written_here_can_be_rescued(env, monkeypatch, capsys):
    """Spec「救回在這台寫的」: the local copy is complete, so putting it back works."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "在這台寫的")
    assert store.upload_batch(store.Drive(paths), paths) == []

    import shutil
    shutil.rmtree(env / "agora" / "sessions" / ulid)     # another machine deleted it
    store.mark_update(paths.outbox / ulid)
    capsys.readouterr()

    # the local mirror still has session.md and the raw, so the upload can send both
    assert (paths.mirror / ulid / "session.md").is_file()
    assert len(list((paths.mirror / ulid).glob("raw-*"))) == 1


def test_sync_leaves_a_queued_for_deletion_session_alone(env, monkeypatch, capsys):
    """2.4 / spec「delete 先在本機」: while it waits for the Drive trash it comes neither
    back into the list nor gets marked as deleted elsewhere."""
    import shutil
    paths = store.Paths.from_env()
    ulid = _stage(paths, "排隊刪除中")
    assert store.upload_batch(store.Drive(paths), paths) == []
    # what `delete` does in the foreground: gone from here, then queued for the trash
    store.forget_local(paths, ulid)
    paths.trash_queue.mkdir(parents=True, exist_ok=True)
    (paths.trash_queue / ulid).write_text("", encoding="utf-8")
    shutil.rmtree(env / "agora" / "sessions" / ulid)
    capsys.readouterr()

    index = store.sync(paths)
    assert index.missing_in_cloud() == []              # not "deleted on another machine"
    assert index.header(ulid) is None                  # and not back in the list
    assert ulid in store.queued_for_trash(paths)       # still waiting to be trashed


def test_push_refuses_a_session_queued_for_deletion(env, capsys):
    from agora import cache
    paths = store.Paths.from_env()
    ulid = _stage(paths, "排隊刪除中")
    paths.trash_queue.mkdir(parents=True, exist_ok=True)
    (paths.trash_queue / ulid).write_text("", encoding="utf-8")

    done, failed = cache.push(paths, [ulid], {})
    assert (done, failed) == (0, 1)
    assert "正在刪除" in capsys.readouterr().err


def test_the_background_loop_compares_versions_not_names(env, monkeypatch):
    """P2: the loop's stopping rule is what keeps an edit from being left behind."""
    paths = store.Paths.from_env()
    _stage(paths)
    first = background._waiting(paths)
    _stage(paths, "同一個以外的一個")
    assert background._waiting(paths) != first
    assert len(background._waiting(paths)) == 2


def test_the_uploader_is_started_for_a_waiting_outbox(env, monkeypatch):
    """N3: a command that finds one waiting starts the uploader rather than sending."""
    paths = store.Paths.from_env()
    _stage(paths)
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)
    started = []
    monkeypatch.setattr(background, "start", lambda paths=None: started.append(1))

    store.kick_uploader(paths)
    assert started