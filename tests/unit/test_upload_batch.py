"""The batch uploader and the local copy (change local-first-writes, tasks 2.1-2.6).

Counting rclone calls: a fixed number per batch, raws before session.md (M4); only what
Drive confirms by md5 leaves the outbox (H1); an update whose id another machine deleted
is not sent back (L7); a failure is retried by the next command; and a session written
here can be rescued after another machine deletes it.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import errno
import hashlib
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
    raw = store.raw_name(h.agora_of(store.read_entry(paths.outbox / ulid)).get("raw"))
    assert (paths.mirror / ulid / "session.md").is_file()
    assert (paths.mirror / ulid / raw).is_file()

    # a new version replaces the old raw, and only one is kept
    hdr = store.read_entry(paths.outbox / ulid)
    store.stage(paths, hdr, "## user\n第二版\n", b'{"v": 2}')
    store.remember(paths, paths.outbox / ulid)
    new_raw = store.raw_name(h.agora_of(store.read_entry(paths.outbox / ulid)).get("raw"))
    assert new_raw != raw
    assert [p.name for p in (paths.mirror / ulid).glob("raw-*")] == [new_raw]


def test_three_sessions_go_up_in_a_few_rclone_calls(env, monkeypatch):
    """M4: a fixed number of calls whatever the count, segments before session.md."""
    paths = store.Paths.from_env()
    batches = _record_batches(monkeypatch)
    ulids = [_stage(paths, f"第 {i} 個") for i in range(3)]
    assert store.upload_batch(store.Drive(paths), paths) == []

    raws = [b for b in batches if any("/seg-" in n for n in b[1])]
    sessions = [b for b in batches if any(n.endswith("/session.md") for n in b[1])]
    assert batches.index(raws[0]) < batches.index(sessions[0]), batches
    assert len(_uploads(env)) <= 4, _uploads(env)
    assert len(raws[0][1]) == 3 and len(sessions[0][1]) == 3
    for ulid in ulids:
        assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def _stage_after_push_looked(paths, monkeypatch, ulid, title="推送途中才寫好的"):
    """The outbox entry for `ulid` appears after `push` took its snapshot - the case its
    second upload path was written for."""
    real = store.push_outbox

    def push_outbox_then_stage(drive, paths_, warn=store.warn):
        left = real(drive, paths_, warn)
        folder = store.stage(paths_, _kept_header(ulid, title), f"## user\n{title}\n", b'{"v": 2}')
        store.remember(paths_, folder)
        return left

    monkeypatch.setattr(store, "push_outbox", push_outbox_then_stage)


def test_a_session_staged_after_push_looked_goes_up_through_the_batch(env, monkeypatch, capsys):
    """E1: `push` had a second upload path for this case, and it deleted the folder after
    a check with no `.done-` comparison - so an edit made while it was sending went with
    it. It goes through `upload_batch` now, like every other entry (H1)."""
    from agora import cache
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    assert store.upload_batch(store.Drive(paths), paths) == []       # on Drive
    _stage_after_push_looked(paths, monkeypatch, ulid)

    done, failed = cache.push(paths, [f"agora:{ulid}"], {})
    capsys.readouterr()

    assert (done, failed) == (1, 0)
    assert ulid not in store.outbox_ulids(paths)
    assert "推送途中才寫好的" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_push_sends_a_late_entry_while_holding_the_upload_lock(env, monkeypatch, capsys):
    """G1: `upload_batch` works on the whole outbox, so push running one outside the lock
    is push and the background on the same outbox - renaming, rescuing and deleting the
    same folders, and writing the same `--files-from` name."""
    from agora import cache
    paths = store.Paths.from_env()
    ulid = _stage(paths, "本來就有的")
    assert store.upload_batch(store.Drive(paths), paths) == []
    _stage_after_push_looked(paths, monkeypatch, ulid)
    real_batch = store.upload_batch
    held = []

    def spy(drive, paths_, warn=store.warn, notices=False):
        held.append(store.uploader_is_running(paths_))
        return real_batch(drive, paths_, warn, notices)

    monkeypatch.setattr(store, "upload_batch", spy)

    cache.push(paths, [f"agora:{ulid}"], {})
    capsys.readouterr()

    assert len(held) == 2, held
    assert all(held), f"an upload ran without the lock: {held}"


def test_a_late_session_drive_does_not_confirm_stays_in_the_outbox(env, monkeypatch, capsys):
    """E1's other half, and the hole itself: until Drive's md5 agrees the folder stays.
    `push_one` deleted it anyway, so a version it had not verified was gone."""
    from agora import cache
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    assert store.upload_batch(store.Drive(paths), paths) == []
    _stage_after_push_looked(paths, monkeypatch, ulid)
    real_listing = store._listing_with_md5

    def stale(drive):
        got = real_listing(drive)
        if isinstance(got, dict) and ulid in got:
            got[ulid] = {**got[ulid], "session.md": "0" * 32}     # Drive reports another version
        return got

    monkeypatch.setattr(store, "_listing_with_md5", stale)

    done, failed = cache.push(paths, [f"agora:{ulid}"], {})

    assert (done, failed) == (0, 1)
    assert ulid in store.outbox_ulids(paths)
    assert "還沒上傳成功，仍在 outbox" in capsys.readouterr().err


def test_the_rescued_session_keeps_this_write_s_relation_and_continues_the_line(env):
    """V3: Y is this write, so `relation` is the kind this write was; and it continues the
    line - X's own parents first, then X."""
    paths = store.Paths.from_env()
    ulid = _deleted_on_drive(env, paths)
    hdr = _kept_header(ulid, "原本的")
    hdr["agora"]["relation"] = "merge"
    hdr["agora"]["parents"] = [{"id": f"agora:{h.new_ulid()}"}]     # X came from somewhere
    folder = store.stage(paths, hdr, "## user\n合併結果\n", b'{"v": 2}')
    store.mark_update(folder)
    assert store.upload_batch(store.Drive(paths), paths) == []

    new_id = [p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir()][0]
    new_hdr, _ = h.split_document(
        (env / "agora" / "sessions" / new_id / "session.md").read_text(encoding="utf-8"))
    assert h.agora_of(new_hdr)["relation"] == "merge"
    assert [p["id"] for p in h.agora_of(new_hdr)["parents"]] == \
        [hdr["agora"]["parents"][0]["id"], f"agora:{ulid}"]


def test_the_next_command_says_where_the_rescued_edit_went(env, capsys):
    """V4: with a real background process the rescue line only reaches upload.log, so the
    user never learns that X is gone and the edit is now Y. The next command says it,
    once. G3: only the detached uploader leaves that note - in the foreground the line
    already reached the terminal, and a file would say it twice."""
    import subprocess
    import sys
    from agora import cli
    paths = store.Paths.from_env()
    ulid = _deleted_on_drive(env, paths)
    folder = store.stage(paths, _kept_header(ulid, "原本的"), "## user\n救回來的\n", b'{"v": 2}')
    store.mark_update(folder)
    # the detached uploader, the way a command starts it - not `run(notices=True)` by hand
    subprocess.run([sys.executable, "-m", "agora.background"], check=True, timeout=60)
    new_id = [p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir()][0]
    capsys.readouterr()

    argv = ["search", "session", "--filter", "text~=救回來的", "--no-sync"]
    assert cli.main(argv) == 0
    assert f"{ulid} 已被別台刪除，這次的修改存成了 {new_id}" in capsys.readouterr().err

    cli.main(argv)          # once, not every command
    assert "已被別台刪除" not in capsys.readouterr().err


def test_a_rescue_in_the_foreground_is_not_said_twice(env, capsys):
    """G3: `push` and inline both run `upload_batch` where the user can see it, so no note
    is left for the next command to repeat."""
    from agora import cli
    paths = store.Paths.from_env()
    ulid = _deleted_on_drive(env, paths)
    folder = store.stage(paths, _kept_header(ulid, "原本的"), "## user\n救回來的\n", b'{"v": 2}')
    store.mark_update(folder)
    assert store.upload_batch(store.Drive(paths), paths) == []
    capsys.readouterr()

    assert store.take_notices(paths) == []          # said once, here
    assert cli.main(["search", "session", "--filter", "text~=救回來的", "--no-sync"]) == 0
    assert "已被別台刪除" not in capsys.readouterr().err



def test_a_sync_that_starts_the_uploader_itself_calls_nothing_a_failure(env, monkeypatch, capsys):
    """X1: `delete` takes the outbox entry away itself and starts the uploader at the end,
    so its opening sync must not call that entry「沒上傳成功」- nothing has been tried, and
    the entry may be on its way out. Every other sync still says it."""
    paths = store.Paths.from_env()
    _stage(paths, "還沒上傳的")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")      # it stays in the outbox
    capsys.readouterr()

    store.sync(paths)
    assert "沒上傳成功" in capsys.readouterr().err

    store.sync(paths, kick=False)
    assert "沒上傳成功" not in capsys.readouterr().err


def test_a_failed_raw_upload_sends_no_session_md_at_all(env, monkeypatch, capsys):
    """M4: a reader tells a version is finished by its session.md, so a half-uploaded
    batch is worse than no upload."""
    paths = store.Paths.from_env()
    ulids = [_stage(paths, f"第 {i} 個") for i in range(3)]
    real_copy = store._copy_batch

    def raws_only(drive, paths_, names, **kw):
        # Only the raws call fails. The session.md call must still be reachable, or
        # this test cannot tell "stopped after the raws" from "everything failed".
        if any("/seg-" in n for n in names):
            raise store.StoreError("段傳不上去")
        return real_copy(drive, paths_, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", raws_only)
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


def test_one_entry_that_cannot_be_verified_does_not_end_the_round(env, monkeypatch):
    """R1's other half: if the folder we renamed aside cannot be read back, that entry
    is reported as still waiting and put back where it was - the other entries in the
    same round still go up."""
    paths = store.Paths.from_env()
    ulids = [_stage(paths, f"第 {i} 個") for i in range(2)]
    broken, real = ulids[0], store._entry_md5s

    def md5s(folder):
        if folder.name == f".done-{broken}":
            raise h.HeaderError("讀不出來了")
        return real(folder)

    monkeypatch.setattr(store, "_entry_md5s", md5s)
    assert store.upload_batch(store.Drive(paths), paths) == [broken]
    assert (paths.outbox / broken / "session.md").is_file()   # back where it was
    assert ulids[1] not in store.outbox_ulids(paths)         # the other one went up
    assert (env / "agora" / "sessions" / ulids[1] / "session.md").is_file()


def test_an_edit_that_lands_while_we_compare_is_not_lost(env, monkeypatch, capsys):
    """R1: the window between renaming our copy aside and comparing it. An edit that
    arrives here used to be deleted by the same `stage` call (`stage` removed
    `.done-<ULID>`, which by then held the version we had just sent) - the round then
    broke and, after a sync, the edit was gone. The edit stays, and the ULID is still
    reported as waiting."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    hdr = store.read_entry(paths.outbox / ulid)
    real = store._entry_md5s

    def md5s(folder):
        if folder.name.startswith(".done-") and not getattr(md5s, "done", False):
            md5s.done = True
            hdr["title"] = "上傳途中改的"
            store.stage(paths, hdr, "## user\n上傳途中改的\n", b'{"v": 2}')
            store.remember(paths, paths.outbox / ulid)
        return real(folder)

    monkeypatch.setattr(store, "_entry_md5s", md5s)
    capsys.readouterr()

    assert store.upload_batch(store.Drive(paths), paths) == [ulid]
    assert "上傳途中改的" in (paths.outbox / ulid / "session.md").read_text(encoding="utf-8")
    # Drive has the version that was sent; the newer one waits here for the next round
    assert "第一版" in (env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8")
    assert "驗證不了" not in capsys.readouterr().err


def test_an_edit_that_only_changes_the_header_is_not_reported_as_sent(env, monkeypatch):
    """H1 for `edit --header`: that version's raw is byte-for-byte the one we sent, so
    the raw comparison cannot catch it. The session.md md5 is the only thing standing
    between a title changed mid-upload and a silently dropped edit."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    hdr = store.read_entry(paths.outbox / ulid)
    raw = (paths.outbox / ulid / store.raw_name(h.agora_of(hdr)["raw"])).read_bytes()
    real = store._listing_with_md5

    def listing(drive):
        got = real(drive)
        if not getattr(listing, "done", False):
            listing.done = True
            hdr["title"] = "換標題"
            store.stage(paths, hdr, "## user\n第一版\n", raw)   # the same bytes
            store.remember(paths, paths.outbox / ulid)
        return got

    monkeypatch.setattr(store, "_listing_with_md5", listing)
    assert store.upload_batch(store.Drive(paths), paths) == [ulid]
    assert ulid in store.outbox_ulids(paths)
    assert "換標題" in (paths.outbox / ulid / "session.md").read_text(encoding="utf-8")


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


def test_an_update_someone_else_deleted_is_saved_as_a_new_session(env, monkeypatch, capsys):
    """L7 / N5: the `.update` marker says this overwrites an id Drive had. If that id is
    gone now, putting it back is not ours to decide - so the edit becomes a session of
    its own (spec「另存成一個新的 Session」), and X leaves the outbox for good."""
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
    assert left == []                            # nothing is left waiting
    assert ulid not in store.outbox_ulids(paths)          # X is gone from here
    assert not (env / "agora" / "sessions" / ulid).exists()   # and it did not come back
    assert len(store.outbox_ulids(paths)) == 0          # and nothing else is waiting
    # the new session is on Drive, with its parents pointing at X
    new_ids = sorted(p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir())
    assert len(new_ids) == 1 and new_ids[0] != ulid
    text = (env / "agora" / "sessions" / new_ids[0] / "session.md").read_text(encoding="utf-8")
    new_hdr, body = h.split_document(text)
    assert new_hdr["id"] == f"agora:{new_ids[0]}"
    assert [p["id"] for p in h.agora_of(new_hdr)["parents"]] == [f"agora:{ulid}"]
    assert "改過了" in body
    err = capsys.readouterr().err
    assert ulid in err and new_ids[0] in err and "已被別台刪除" in err


def _deleted_on_drive(env, paths):
    """X on Drive, then another machine removes it: the state N5's rescue is for."""
    import shutil
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []
    shutil.rmtree(env / "agora" / "sessions" / ulid)
    return ulid


def test_an_edit_that_lands_while_the_rescue_is_staging_y_is_not_deleted(env, monkeypatch):
    """V1: the rescue used to read `outbox/X` and then `rmtree` it, so an edit made in
    between went with it - not on Drive, not in the outbox, only in X's local mirror,
    where the next sync marks it cloud-missing and nobody is told. It claims X the way
    H1 does and deletes only what it claimed; a newer version waits its turn."""
    paths = store.Paths.from_env()
    ulid = _deleted_on_drive(env, paths)
    hdr = _kept_header(ulid, "原本的")
    store.stage(paths, hdr, "## user\n第一次改\n", b'{"v": 2}')
    store.mark_update(paths.outbox / ulid)
    real_stage = store.stage

    def stage_y_then_edit(paths_, hdr_, body_, raw_):
        got = real_stage(paths_, hdr_, body_, raw_)
        if hdr_["id"] != f"agora:{ulid}":        # Y is being staged: this is the window
            folder = store.stage(paths, hdr, "## user\n第二次改\n", b'{"v": 3}')
            store.remember(paths, folder)
            store.mark_update(folder)      # what `edit` does: this id already existed
        return got

    monkeypatch.setattr(store, "stage", stage_y_then_edit)
    assert ulid in store.upload_batch(store.Drive(paths), paths)   # still waiting
    assert "第二次改" in (paths.outbox / ulid / "session.md").read_text(encoding="utf-8")

    # the next round rescues it too, and this time it is the only version there
    monkeypatch.setattr(store, "stage", real_stage)
    assert store.upload_batch(store.Drive(paths), paths) == []
    saved = [p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir()]
    assert ulid not in saved
    held = [(env / "agora" / "sessions" / u / "session.md").read_text(encoding="utf-8")
            for u in saved]
    assert sum("第二次改" in t for t in held) == 1


def test_the_rescued_session_is_in_the_local_index_and_mirror(env):
    """V2: Y went to Drive but not to this machine - search could not find it and the
    raw was not here, which is P1 (Drive-only) all over again for the very session we
    just rescued."""
    paths = store.Paths.from_env()
    ulid = _deleted_on_drive(env, paths)
    folder = store.stage(paths, _kept_header(ulid, "原本的"), "## user\n救回來的\n", b'{"v": 2}')
    store.mark_update(folder)
    assert store.upload_batch(store.Drive(paths), paths) == []

    new_id = [p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir()][0]
    assert (paths.mirror / new_id / "session.md").is_file()      # the mirror
    assert list((paths.mirror / new_id).glob("raw-*")), "the raw too"
    index = store.Index(paths)                                   # and searchable now
    hits = index.search([(("text",), "~=", "救回來的")])
    assert [hit[0] for hit in hits] == [new_id]


def test_an_entry_that_cannot_be_read_is_quarantined_not_uploaded(env, capsys):
    """N5's fallback sits one layer up: an entry we cannot read never reaches the
    rescue. It is moved aside whole (so the edit is not thrown away) and Drive does
    not get a half-written session."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []
    import shutil
    shutil.rmtree(env / "agora" / "sessions" / ulid)
    folder = store.stage(paths, _kept_header(ulid, "原本的"), "## user\n改過了\n", None)
    store.mark_update(folder)
    (folder / "session.md").unlink()             # cannot read it back
    capsys.readouterr()

    assert store.upload_batch(store.Drive(paths), paths) == []
    assert ulid not in store.outbox_ulids(paths)
    assert (paths.outbox / ".bad" / ulid).is_dir()      # kept, not deleted
    assert not (env / "agora" / "sessions" / ulid).exists()
    assert "壞了" in capsys.readouterr().err


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


def test_sync_does_not_mark_a_queued_session_that_drive_lost(env, monkeypatch):
    """W3, the other half: the marker loop carries the same `u not in trashing` guard, and
    a queued session can still have its row - `delete` drops the row before queueing, so
    this is what an interrupted delete (or another process's queue) leaves behind."""
    import shutil
    from agora import background
    paths = store.Paths.from_env()
    ulid = _stage(paths, "佇列裡的")
    assert store.upload_batch(store.Drive(paths), paths) == []      # on Drive and indexed
    shutil.rmtree(env / "agora" / "sessions" / ulid)                # and then Drive lost it
    paths.trash_queue.mkdir(parents=True, exist_ok=True)
    (paths.trash_queue / ulid).write_text("", encoding="utf-8")
    assert store.Index(paths).header(ulid) is not None             # the row is still there
    monkeypatch.setattr(background, "process_trash_queue", lambda *a: None)

    assert store.sync(paths).missing_in_cloud() == []


def test_sync_neither_lists_nor_marks_a_session_queued_for_deletion(env, monkeypatch):
    """2.4 / N10: the delete queue is not「被別台刪掉」. Drive still has the folder - only
    the background's purge takes it away - so sync must leave it out of the list and
    leave no marker on it. The two mutations to watch are the `u not in trashing` checks
    in the changed/missing loops (review W3)."""
    from agora import background
    paths = store.Paths.from_env()
    ulid = _stage(paths, "要刪掉的")
    assert store.upload_batch(store.Drive(paths), paths) == []     # it is on Drive now
    store.forget_local(paths, ulid)                               # what delete does locally
    paths.trash_queue.mkdir(parents=True, exist_ok=True)
    (paths.trash_queue / ulid).write_text("", encoding="utf-8")
    assert (env / "agora" / "sessions" / ulid).is_dir()           # Drive still has it
    monkeypatch.setattr(background, "process_trash_queue", lambda *a: None)   # not yet purged

    index = store.sync(paths)
    assert index.header(ulid) is None                             # not back in the list
    assert index.missing_in_cloud() == []                         # and not marked
    assert (env / "agora" / "sessions" / ulid).is_dir()           # Drive untouched
    assert ulid in store.queued_for_trash(paths)                  # still waiting to be trashed


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

def test_a_background_starting_while_a_command_lists_the_outbox_still_sends_it(env, monkeypatch, capsys):
    """T3-sec3 R6: listing the outbox used to take the upload lock for a moment (to
    decide whether `.done-` could be restored). A background that tried the lock in
    that moment read it as "somebody is already running" and quit - but the lister
    sends nothing, so these sessions waited for the next command, which the spec
    (「背景上傳」) does not allow. Here the background starts exactly while another
    command lists the outbox; this round must still send all of them."""
    paths = store.Paths.from_env()
    ulids = [_stage(paths, f"第 {i} 個") for i in range(3)]
    real_hold = store.hold_upload_lock
    started = []

    def hold(paths_, blocking=False):
        if started:                           # the background's own calls, and later ones
            return real_hold(paths_, blocking)
        started.append(True)
        held = real_hold(paths_, blocking)    # the lister has the lock right now ...
        background.run(paths_)                # ... and this is when the background starts
        return held

    monkeypatch.setattr(store, "hold_upload_lock", hold)
    store.outbox_ulids(paths)                 # another command reading the outbox
    if not started:                           # the lister never touched the lock: the
        started.append(True)                  # background starts with nobody in its way
        background.run(paths)
    capsys.readouterr()

    assert store.outbox_ulids(paths) == set()
    for ulid in ulids:
        assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_the_background_puts_back_what_a_crashed_uploader_renamed_aside(env, capsys):
    """E3's other half: `.done-<ULID>` left by an uploader that died while comparing is
    restored by the next background once it has the lock, and sent in that same run."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "當掉時改名的")
    (paths.outbox / ulid).rename(paths.outbox / f".done-{ulid}")
    assert store.outbox_ulids(paths) == set()      # listing no longer touches it

    background.run(paths)
    capsys.readouterr()

    assert not (paths.outbox / f".done-{ulid}").exists()
    assert store.outbox_ulids(paths) == set()
    assert "當掉時改名的" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_an_update_waits_when_drive_cannot_be_listed_but_a_new_session_goes(env, monkeypatch, capsys):
    """V6 (PM): a failed listing is not proof that the id is still there. Sending the
    update anyway could put back what another machine just deleted, so it stays in the
    outbox for a round whose listing works; a new session has no such risk and goes."""
    paths = store.Paths.from_env()
    old = _stage(paths, "既有的")
    assert store.upload_batch(store.Drive(paths), paths) == []
    store.stage(paths, _kept_header(old, "改過的"), "## user\n改過的\n", b'{"v": 2}')
    store.mark_update(paths.outbox / old)
    new = _stage(paths, "新的")
    real = store._listing_with_md5
    calls = []

    def listing(drive):
        calls.append(1)
        if len(calls) == 1:                    # the L7 check, before anything is sent
            return store.StoreError("連不上 Drive")
        return real(drive)

    monkeypatch.setattr(store, "_listing_with_md5", listing)
    capsys.readouterr()

    assert store.upload_batch(store.Drive(paths), paths) == [old]
    assert "更新留在 outbox" in capsys.readouterr().err
    assert old in store.outbox_ulids(paths)
    assert "既有的" in (env / "agora" / "sessions" / old / "session.md").read_text(encoding="utf-8")
    assert new not in store.outbox_ulids(paths)
    assert (env / "agora" / "sessions" / new / "session.md").is_file()


def test_a_version_left_aside_by_a_crash_is_kept_by_sync_and_sent(env, monkeypatch, capsys):
    """T3-final4 K1: an uploader that died between renaming `outbox/X` to `.done-X` and
    comparing it leaves the only copy of that edit set aside. Until a background puts it
    back, sync must not treat it as sent: it starts the uploader, and Drive's older
    version does not overwrite the mirror - otherwise the next edit starts from the old
    one and the background then drops `.done-X` for it, losing the edit for good."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    assert store.upload_batch(store.Drive(paths), paths) == []        # Drive: 第一版
    store.sync(paths)
    store.stage(paths, _kept_header(ulid, "第二版"), "## user\n第二版\n", b'{"v": 2}')
    store.mark_update(paths.outbox / ulid)
    store.remember(paths, paths.outbox / ulid)
    (paths.outbox / ulid).rename(paths.outbox / f".done-{ulid}")      # the crash
    started = []
    monkeypatch.setattr(background, "start", lambda p=None: started.append(1) or background.STARTED)
    (paths.state / "last-sync").unlink(missing_ok=True)

    index = store.sync(paths)
    capsys.readouterr()

    assert started, "something has to put it back and send it"
    assert store.outbox_count(paths) == 1
    assert index.header(ulid)["title"] == "第二版"
    assert "第二版" in (paths.mirror / ulid / "session.md").read_text(encoding="utf-8")
    assert not (paths.mirror / f".done-{ulid}").exists()

    background.run(paths)                                             # the one it started
    capsys.readouterr()
    assert "第二版" in (env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8")
    assert store.outbox_count(paths) == 0
    assert not (paths.outbox / f".done-{ulid}").exists()


def test_a_set_aside_version_that_cannot_be_put_back_is_kept(env, monkeypatch):
    """G6: `.done-X` that will not rename back, with no newer `X` in its place, stays
    where it is (still waiting, K1) instead of being deleted - it may be the only copy."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "唯一的一份")
    done = paths.outbox / f".done-{ulid}"
    (paths.outbox / ulid).rename(done)
    real = Path.rename

    def refuse(self, target):
        if self == done:
            raise PermissionError("不讓改名")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", refuse)
    store._put_back(done, paths.outbox / ulid)
    assert (done / "session.md").is_file()
    assert ulid in store.waiting_ulids(paths)


# --- N1: the entry does not grow ---------------------------------------------------


def _entry_raw(paths, ulid: str) -> str:
    return store.raw_name(h.agora_of(store.read_entry(paths.outbox / ulid))["raw"])


def test_an_entry_staged_round_after_round_keeps_at_most_two_raws(env):
    """N1: five rounds while nothing gets through (the arch probe counted 1, 2, 3,
    4): the entry holds its own raw and the one before it, never the whole history."""
    paths = store.Paths.from_env()
    hdr = _header()
    folder = None
    names = []
    for i in range(5):
        folder = store.stage(paths, hdr, f"## user\n第 {i} 輪\n", f'{{"round": {i}}}'.encode(),
                             update=True, kind="continue")
        names.append(_entry_raw(paths, folder.name))

    kept = sorted(p.name for p in folder.glob("raw-*"))
    assert kept == sorted(names[-2:]), kept
    assert store._is_update(folder), "the marker still goes in with the swap"


def test_a_pass_sends_only_the_segments_the_entry_names(env, monkeypatch):
    """N1: the carried-over whole raw is for a list built before the swap - it is never
    sent (Drive has no whole raw any more), and the batch carries the segments of the
    manifest session.md names, nothing else."""
    paths = store.Paths.from_env()
    hdr = _header()
    store.stage(paths, hdr, "## user\n第一版\n", b'{"v": 1}')
    folder = store.stage(paths, hdr, "## user\n第二版\n", b'{"v": 2}')
    ulid = folder.name
    assert len(list(folder.glob("raw-*"))) == 2, "the previous raw came along"
    batches = _record_batches(monkeypatch)

    assert store.upload_batch(store.Drive(paths), paths) == []

    hdr = store.Index(paths).header(ulid) or h.split_document(
        (env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8"))[0]
    manifest = [seg["file"] for seg in h.agora_of(hdr)["raw"]["segments"]]
    sent = [names for cmd, names in batches if cmd == "copy" and any("/seg-" in n for n in names)]
    assert sent == [[f"{ulid}/{name}" for name in manifest]], batches
    assert not any("/raw-" in n for _, names in batches for n in names), "no whole raw goes up"
    on_drive = sorted(p.name for p in (env / "agora" / "sessions" / ulid).iterdir())
    assert on_drive == sorted([*manifest, "session.md"])


def test_a_stage_between_the_list_and_the_raw_copy_does_not_stop_the_batch(env, monkeypatch):
    """T2 (review R3): the list names the raw of the version read at the start; a round
    staging the next version before rclone reads it must not turn into `source not
    found` and a round with no session.md at all."""
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\n第一版\n", b'{"v": 1}')
    real_copy = store._copy_batch
    staged = {"done": False}

    def stage_then_copy(drive, paths_, names, **kw):
        if not staged["done"] and any("/seg-" in n for n in names):
            staged["done"] = True
            store.stage(paths_, hdr, "## user\n第二版\n", b'{"v": 2}')   # the next round
        return real_copy(drive, paths_, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", stage_then_copy)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert staged["done"]
    assert not any("段沒傳好" in s for s in said), said
    assert _drive_has_segments_of(env, folder.name, "第一版"), "the version this round read went up whole"
    # the version this round read is not the one in the folder any more, so it stays
    # until a round confirms what it read
    assert left == [folder.name]
    assert store.upload_batch(store.Drive(paths), paths) == []
    doc = (env / "agora" / "sessions" / folder.name / "session.md").read_text(encoding="utf-8")
    assert "第二版" in doc
    _drive_sessions_consistent(env)


def _rescue_child(paths, ulid: str, title: str = "已另存的那份") -> str:
    """A session saved out of X the way a rescue would: relation continue, X's own
    source, parents pointing at X - staged as new and searchable right away."""
    src = (h.agora_of(store.read_entry(paths.outbox / ulid)).get("source") or {})
    hdr = _header(title)
    hdr["agora"]["relation"] = "continue"
    hdr["agora"]["parents"] = [{"id": f"agora:{ulid}"}]
    hdr["agora"]["source"] = dict(src)
    folder = store.stage(paths, hdr, f"## user\n{title}\n", b'{"rescued": 1}')
    store.remember(paths, folder)
    return folder.name


def test_an_uploader_that_finds_an_adopted_rescue_mints_nothing(env):
    """T3 / R7: X deleted on Drive, but a rescued child Y (same source, relation
    continue) already exists: the uploader adopts Y instead of minting a second
    session, so one deletion is never two new sessions."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []   # X confirmed
    folder = store.stage(paths, _kept_header(ulid, "改過了"), "## user\n改過了\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)
    y = _rescue_child(paths, ulid)
    import shutil
    shutil.rmtree(env / "agora" / "sessions" / ulid)              # another machine deletes X
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == []

    assert not (env / "agora" / "sessions" / ulid).exists(), "X 沒有被傳回去"
    names = sorted(p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir())
    assert names == [y], f"沿用已有的另存，不該再建一個：{names}"
    assert "已另存的那份" in (env / "agora" / "sessions" / y / "session.md").read_text(
        encoding="utf-8")
    assert any(f"沿用已另存的 {y}" in s for s in said), \
        f"沿用也要留一句話在 log，不然下次查不到這筆去哪了：{said}"


def test_obsolete_segments_are_cleaned_even_when_the_entry_still_holds_the_old_raw(env, monkeypatch):
    """T2 / R8, under the new cleanup rule (design B.5.5): the entry still holds the previous
    whole raw (for lists built before the swap), but once the grace period is over Drive
    ends with only what the session points at. The clock is pinned two hours ahead, so
    every segment on Drive is older than the one-hour grace and nothing is protected by
    its age: only the manifest (and a record that matches Drive) is kept."""
    import time
    monkeypatch.setenv("AGORA_NOW", str(time.time() + 7200))
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    assert store.upload_batch(store.Drive(paths), paths) == []
    hdr = store.Index(paths).header(ulid)
    first_segment = h.agora_of(hdr)["raw"]["segments"][0]["file"]
    assert (env / "agora" / "sessions" / ulid / first_segment).is_file()

    store.stage(paths, _kept_header(ulid, "第二版"), "## user\n第二版\n", b'{"v": 2}',
                update=True, kind="continue")
    real_copy = store._copy_batch
    swapped = {"done": False}

    def copy_then_swap(drive, paths_, names, **kw):
        if not swapped["done"]:
            swapped["done"] = True
            # the next version lands while this round's copies are in flight: the
            # round sends v2's segments and v2's session.md (its own snapshot, #130), v3
            # stays waiting, and the entry keeps v2's raw for lists built before the
            # swap
            store.stage(paths_, _kept_header(ulid, "第三版"), "## user\n第三版\n", b'{"v": 3}',
                        update=True, kind="continue")
        return real_copy(drive, paths_, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", copy_then_swap)
    left = store.upload_batch(store.Drive(paths), paths)
    assert swapped["done"]
    assert left == [ulid], "v3 superseded v2 mid-round: it stays for the next round"
    folder = paths.outbox / ulid
    named = _entry_raw(paths, ulid)
    assert "第三版" in (folder / "session.md").read_text(encoding="utf-8")
    assert sorted(p.name for p in folder.glob("raw-*")) != [named], \
        "the previous raw came along for in-flight lists"

    assert store.upload_batch(store.Drive(paths), paths) == []

    final, _ = h.split_document((env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8"))
    current = [seg["file"] for seg in h.agora_of(final)["raw"]["segments"]]
    on_drive = sorted(p.name for p in (env / "agora" / "sessions" / ulid).iterdir())
    assert on_drive == sorted([*current, "session.md"]), on_drive

def test_an_update_round_decides_from_a_late_listing(env, monkeypatch):
    """The round's decision about updates comes from listings, and the last word is
    the re-check right before the copies: a purge between the first listing and the
    copies must still meet a rescue, not a blind send (review resurrect). So an
    update round lists sessions twice before copying (existence, re-check) plus the
    confirmation afterwards - no more, no fewer."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []   # confirmed
    folder = store.stage(paths, _kept_header(ulid, "改過了"), "## user\n改過了\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)
    seen = []
    real = store.Drive._run

    def spy(self, *args, **kwargs):
        if args and args[0] == "lsjson" and "gdrive:sessions" in args:
            seen.append(list(args))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(store.Drive, "_run", spy)
    said: list[str] = []
    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == []
    assert len(seen) == 3, f"existence, re-check, confirmation: {seen}"
    assert any(f"存在檢查 {ulid}：還在" in s for s in said), said
    assert any("本輪送出的 session.md" in s and ulid in s for s in said), said


def test_an_update_deleted_under_the_copies_is_taken_down_again(env, monkeypatch):
    """resurrect: the re-check saw A, another machine deleted it while this round's
    copies were in flight, and the session.md copy re-created the folder. The
    confirmation sees files that were there a moment ago and are now gone - files
    this round never deleted - so it takes the re-created folder down again and keeps
    the work as a session of its own. Without that, A is alive on Drive again and
    nothing will ever notice.

    The fake purges A right after the raws copy landed (FAKE_RCLONE_PURGE_AFTER), so
    the deletion falls exactly between the re-check and the session.md copy.
    """
    paths = store.Paths.from_env()
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []   # A is on Drive
    folder = store.stage(paths, _kept_header(ulid, "改過了"), "## user\n改過了\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)
    drive = store.Drive(paths)
    trigger = env.parent / "purge-after.json"
    trigger.write_text(json.dumps({"after": "seg-", "purge": f"sessions/{ulid}"}),
                       encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(trigger))
    said: list[str] = []

    left = store.upload_batch(drive, paths, warn=said.append)

    assert not trigger.exists(), "the purge hook never fired - the test proved nothing"
    assert not (env / "agora" / "sessions" / ulid).exists(), "A 被刪掉後又被留在 Drive 上"
    assert any("copy 期間被別台刪掉" in s for s in said), said
    (new,) = store.outbox_ulids(paths)      # Y is staged, for the next pass
    assert new != ulid and left == [new], f"該只多出一個另存的 Session：{left}"
    assert store.upload_batch(store.Drive(paths), paths) == []
    hdr, body = h.split_document(
        (env / "agora" / "sessions" / new / "session.md").read_text(encoding="utf-8"))
    assert [p["id"] for p in h.agora_of(hdr)["parents"]] == [f"agora:{ulid}"]
    assert "改過了" in body


def test_two_stages_between_the_list_and_the_copy_still_send(env, monkeypatch):
    """R4: the send list is filtered to what is there when it is built. A version
    superseded twice over between the round's listing and its copies must not turn
    the whole batch into `source not found` with no session.md sent at all - the
    round sends what it still has, and the newest version follows next round."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    assert store.upload_batch(store.Drive(paths), paths) == []   # v1 confirmed
    folder = store.stage(paths, _kept_header(ulid, "第二版"), "## user\n第二版\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)
    real_listing = store._listing_with_md5
    swapped = {"done": False}

    def list_then_swap_twice(drive):
        if not swapped["done"]:
            swapped["done"] = True
            # two rounds land between the entries this batch read and its copies:
            # the listed raw is gone from the folder by the time rclone reads it
            store.stage(paths, _kept_header(ulid, "第三版"), "## user\n第三版\n", b'{"v": 3}')
            store.stage(paths, _kept_header(ulid, "第四版"), "## user\n第四版\n", b'{"v": 4}')
        return real_listing(drive)

    monkeypatch.setattr(store, "_listing_with_md5", list_then_swap_twice)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert swapped["done"]
    assert not any("原始檔沒傳好" in s for s in said), said
    assert left == [ulid], "this round read v2 but the folder moved on: it stays"
    assert store.upload_batch(store.Drive(paths), paths) == []
    doc = (env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8")
    assert "第四版" in doc


# --- the listing->copy window --------------------------------------------------


def test_an_update_deleted_between_the_listing_and_the_copy_is_rescued_not_sent(env, monkeypatch):
    """The resurrect race (review resurrect): the round's listing saw A, another machine
    deletes A, then the copies land. Without a look right before the copies, the batch
    puts A back and no rescue ever happens; with it, A stays deleted and the work goes
    up as a session of its own.

    Deterministic: the fake itself purges A the moment the round's listing returns
    (FAKE_RCLONE_PURGE_AFTER, one shot), and every call is slow, so the shape is the
    real slow-upload one without racing a sleep.
    """
    paths = store.Paths.from_env()
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []   # A is on Drive
    folder = store.stage(paths, _kept_header(ulid, "改過了"), "## user\n改過了\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)                     # whoever updates an id marks it
    drive = store.Drive(paths)                    # folder id cached: no calls below
    trigger = env.parent / "purge-after.json"
    trigger.write_text(json.dumps({"after": "lsjson", "purge": f"sessions/{ulid}"}),
                       encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(trigger))
    monkeypatch.setenv("FAKE_RCLONE_DELAY", "0.5")
    said: list[str] = []

    left = store.upload_batch(drive, paths, warn=said.append)

    assert left == [], f"沒東西該留在 outbox：{left}"
    assert not trigger.exists(), "the purge hook never fired - the test proved nothing"
    assert not (env / "agora" / "sessions" / ulid).exists(), "A 被刪掉後又被傳回去了"
    assert any("已被別台刪除" in s for s in said), said
    new_ids = sorted(p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir())
    assert len(new_ids) == 1 and new_ids[0] != ulid, new_ids
    text = (env / "agora" / "sessions" / new_ids[0] / "session.md").read_text(encoding="utf-8")
    new_hdr, body = h.split_document(text)
    assert [p["id"] for p in h.agora_of(new_hdr)["parents"]] == [f"agora:{ulid}"]
    assert new_hdr["title"] == "改過了" and "改過了" in body, "另存的是這次改過的那一份"


# --- #130: the two copies must describe the same version --------------------------


def _drive_has_segments_of(remote: Path, ulid: str, word: str) -> bool:
    """The session.md on Drive mentions `word` and every segment it names is there."""
    folder = remote / "agora" / "sessions" / ulid
    text = (folder / "session.md").read_text(encoding="utf-8")
    hdr, _ = h.split_document(text)
    return word in text and all((folder / seg["file"]).is_file()
                                for seg in h.agora_of(hdr)["raw"]["segments"])


def _drive_sessions_consistent(remote: Path) -> None:
    """Every session.md on the fake Drive names segments that are in its own folder (#130)."""
    sessions = remote / "agora" / "sessions"
    if not sessions.is_dir():
        return
    for folder in sorted(p for p in sessions.iterdir() if p.is_dir()):
        text = (folder / "session.md").read_text(encoding="utf-8")
        hdr, _ = h.split_document(text)
        raw = h.agora_of(hdr).get("raw")
        for seg in (raw or {}).get("segments", []):
            assert (folder / seg["file"]).is_file(), \
                f"{folder.name} 的 session.md 指名 {seg['file']}，但 Drive 上沒有"
            assert store.md5_file(folder / seg["file"]) == seg["md5"]


def _swap_after_the_raws(monkeypatch, paths, ulid: str) -> dict:
    """A `stage` in the window between the raws copy and the session.md copy (#130)."""
    real_copy = store._copy_batch
    done = {"swapped": False}

    def copy(drive, paths_, names, **kw):
        got = real_copy(drive, paths_, names, **kw)
        if not done["swapped"] and any("/seg-" in n for n in names):
            done["swapped"] = True
            hdr = store.read_entry(paths.outbox / ulid)
            store.stage(paths_, hdr, "## user\n第二版\n", b'{"v": 2}')
            store.remember(paths_, paths.outbox / ulid)
        return got

    monkeypatch.setattr(store, "_copy_batch", copy)
    return done


def test_a_version_staged_between_the_copies_never_points_at_a_missing_raw(env, monkeypatch):
    """#130: the raw copy sends v1's raw, the session.md copy used to read the entry
    again and send v2 - which names a raw that never went up. Drive then has a
    session.md pointing at a file nobody has. The session.md copy must send the
    version the raws belonged to; the newer entry stays in the outbox."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    done = _swap_after_the_raws(monkeypatch, paths, ulid)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert done["swapped"], "the stage never landed between the copies - the test proved nothing"
    _drive_sessions_consistent(env)
    assert left == [ulid]
    assert ulid in store.outbox_ulids(paths)
    assert "第二版" in (paths.outbox / ulid / "session.md").read_text(encoding="utf-8")
    # Drive kept what this round actually sent, and its raw is there too
    text = (env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8")
    hdr, _ = h.split_document(text)
    assert "第一版" in text
    assert _drive_has_segments_of(env, ulid, "第一版")
    assert any(f"{ulid} 上傳期間又有新版本" in s for s in said), said


def test_the_round_after_the_swap_sends_the_new_version(env, monkeypatch):
    """#130, the other half: the version left in the outbox goes up in full next
    round, and Drive never held a session.md naming a raw it does not have."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    _swap_after_the_raws(monkeypatch, paths, ulid)
    assert store.upload_batch(store.Drive(paths), paths) == [ulid]       # v2 waits

    assert store.upload_batch(store.Drive(paths), paths) == []
    _drive_sessions_consistent(env)
    assert ulid not in store.outbox_ulids(paths)
    text = (env / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8")
    hdr, _ = h.split_document(text)
    assert "第二版" in text
    folder = env / "agora" / "sessions" / ulid
    joined = b"".join((folder / seg["file"]).read_bytes() for seg in h.agora_of(hdr)["raw"]["segments"])
    assert hashlib.md5(joined).hexdigest() == h.agora_of(hdr)["raw"]["md5"]


def test_an_unconfirmed_version_says_why_it_stays(env, monkeypatch):
    """#130: 「留在 outbox」 with no reason sent the user looking at upload.log in vain.
    A Drive md5 that is not ours is one of the two reasons."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    real_listing = store._listing_with_md5

    def stale(drive):
        got = real_listing(drive)
        if isinstance(got, dict) and ulid in got:
            got[ulid] = {**got[ulid], "session.md": "0" * 32}
        return got

    monkeypatch.setattr(store, "_listing_with_md5", stale)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]
    assert any(f"{ulid} Drive 的 md5 與本機不符（session.md 或 raw），留在 outbox" in s
               for s in said), said


def test_no_batch_snapshot_is_left_behind(env, monkeypatch):
    """#130: the private copy of session.md lives under <state>/batch-<pid> and goes on
    the way out - after a good round, a failed one, and an exception."""
    paths = store.Paths.from_env()
    _stage(paths, "第一版")
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert not list(paths.state.glob("batch-*"))

    real_copy = store._copy_batch

    def fail_sessions(drive, paths_, names, **kw):
        if any(n.endswith("/session.md") for n in names):
            raise store.StoreError("session.md 傳不上去")
        return real_copy(drive, paths_, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", fail_sessions)
    second = _stage(paths, "第二版")
    assert store.upload_batch(store.Drive(paths), paths) == [second]
    assert not list(paths.state.glob("batch-*"))

    def blow_up(drive, paths_, names, **kw):
        if any(n.endswith("/session.md") for n in names):
            raise RuntimeError("boom")
        return real_copy(drive, paths_, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", blow_up)
    _stage(paths, "第三版")
    with pytest.raises(RuntimeError):
        store.upload_batch(store.Drive(paths), paths)
    assert not list(paths.state.glob("batch-*"))


# --- #130 review: W1, W2, N1, N2, N3 ------------------------------------------------


def _set_raw(paths, ulid: str, raw) -> None:
    """Rewrite an entry's header with `raw`, as a hand edit or damage would leave it.

    `stage` always writes `{file, md5, size}`, and `h.validate()` does not look at the
    shape of `raw`, so `edit --header agora.raw=…` and a damaged file are the two ways
    a session.md ends up naming its raw some other way.
    """
    path = paths.outbox / ulid / "session.md"
    hdr, body = h.split_document(path.read_text(encoding="utf-8"))
    hdr["agora"]["raw"] = raw
    path.write_text(h.dump_document(hdr, body), encoding="utf-8")


@pytest.mark.parametrize("raw", ["abc", [1], {"md5": "0" * 32, "size": 12}],
                         ids=["字串", "list", "缺file"])
def test_a_header_raw_of_another_shape_is_quarantined_not_uploaded(env, raw, capsys):
    """W1: the old `read_entry` isolated these three; the #130 snapshot let a string or
    a list escape as AttributeError (the round died, again on every round) and took a
    mapping without `file` for "no raw" (Drive got a session.md pointing nowhere). The
    uploader's single read has to be exactly as strict - and one bad entry is not the
    round's end (R1), so the other entry still goes up."""
    paths = store.Paths.from_env()
    good = _stage(paths, "正常的")
    bad = _stage(paths, "壞掉的")
    _set_raw(paths, bad, raw)
    capsys.readouterr()

    assert store.upload_batch(store.Drive(paths), paths) == []
    assert (paths.outbox / ".bad" / bad).is_dir()          # kept whole, not deleted
    assert bad not in store.outbox_ulids(paths)
    assert (env / "agora" / "sessions" / good / "session.md").is_file()
    assert not (env / "agora" / "sessions" / bad).exists()
    assert "壞了" in capsys.readouterr().err


def _two_stages_before_the_raws(monkeypatch, paths, ulid: str) -> dict:
    """Two `stage`s after the entries were read and before the raws copy (#130, W2).

    The second one drops v1's raw (the first stage kept it for a list built a moment
    ago; the second takes v2's and leaves v1's behind), so this round has no raw for
    that entry left to send.
    """
    real_is_update = store._is_update
    done = {"swapped": False}

    def twice(folder):
        got = real_is_update(folder)
        if not done["swapped"] and folder.name == ulid:
            done["swapped"] = True
            for title, raw in (("第二版", b'{"v": 2}'), ("第三版", b'{"v": 3}')):
                hdr = store.read_entry(paths.outbox / ulid)
                store.stage(paths, hdr, f"## user\n{title}\n", raw)
                store.remember(paths, paths.outbox / ulid)
        return got

    monkeypatch.setattr(store, "_is_update", twice)
    return done


def test_two_stages_before_the_segments_are_cut_send_nothing_at_all(env, monkeypatch):
    """W2: v1's session.md must not go up on its own. The second stage dropped v1's whole
    raw (the first kept it for a list built a moment ago; the second takes v2's and leaves
    v1's behind), so this round cannot cut v1's segments - and a session.md whose segments
    are not on Drive is exactly what must never be published. The cut fails, the entry
    says so and waits, nothing goes up, and the next round sends v3 whole. Sending
    session.md without checking that its segments are in the batch turns this red."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    done = _two_stages_before_the_raws(monkeypatch, paths, ulid)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert done["swapped"], "the stages never landed - the test proved nothing"
    sessions = env / "agora" / "sessions"
    assert not (sessions / ulid / "session.md").exists(), "v1's session.md went up alone"
    assert ulid in store.outbox_ulids(paths)
    assert any(f"{ulid} 切段失敗" in s for s in said), said
    assert "第三版" in (paths.outbox / ulid / "session.md").read_text(encoding="utf-8")
    _drive_sessions_consistent(env)

    assert store.upload_batch(store.Drive(paths), paths) == []
    _drive_sessions_consistent(env)
    assert ulid not in store.outbox_ulids(paths)
    text = (sessions / ulid / "session.md").read_text(encoding="utf-8")
    assert "第三版" in text
    assert _drive_has_segments_of(env, ulid, "第三版")

def test_a_batch_snapshot_left_by_a_dead_run_is_cleared(env):
    """N1: a run killed before its cleanup leaves <state>/batch-<pid> - copies of
    session.md - behind for good. Whoever holds upload.lock is the only writer, so a
    round clears every batch-* and touches nothing else in state/."""
    paths = store.Paths.from_env()
    stale = paths.state / "batch-99999" / "01M5STALE"
    stale.mkdir(parents=True)
    (stale / "session.md").write_text("---\n", encoding="utf-8")
    listing = paths.state / ".files-from-keep.txt"
    listing.write_text("x\n", encoding="utf-8")
    notices = paths.state / "notices"
    notices.write_text("keep me\n", encoding="utf-8")
    # neighbours whose names only look a bit like it (review N1): none of them may go
    neighbours = [paths.state / "mybatch-1", paths.state / "xbatch-2", paths.state / "batchfoo",
                  paths.state / "agy-backups"]
    for neighbour in neighbours:
        neighbour.mkdir()
        (neighbour / "keep").write_text("x", encoding="utf-8")
    file_neighbour = paths.state / "batch-notes.txt"       # a FILE with the prefix is not a snapshot dir
    file_neighbour.write_text("keep me too\n", encoding="utf-8")
    ulid = _stage(paths, "第一版")

    assert store.upload_batch(store.Drive(paths), paths) == []

    assert not (paths.state / "batch-99999").exists()
    assert not [p for p in paths.state.glob("batch-*") if p.is_dir()]
    assert listing.is_file() and notices.is_file() and file_neighbour.is_file()
    assert all((n / "keep").is_file() for n in neighbours), "only batch-* directories are cleared"
    assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_a_snapshot_that_cannot_be_written_leaves_every_entry_for_the_next_round(
        env, monkeypatch):
    """N2: a full disk while writing the batch dir is a failed round, not a crash - like
    every other send failure: say it, keep the entries, no batch-* left, and nothing sent
    this round."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    real_write = store._write_file

    def no_room(path, data):
        if any(part.startswith("batch-") for part in Path(path).parts):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(path, data)

    monkeypatch.setattr(store, "_write_file", no_room)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]
    assert ulid in store.outbox_ulids(paths)
    assert not list(paths.state.glob("batch-*"))
    assert not (env / "agora" / "sessions" / ulid / "session.md").exists()
    assert any("批次暫存寫不進 state" in s for s in said), said


def test_an_entry_that_cannot_be_moved_aside_says_what_we_know(env, monkeypatch):
    """N3: the rename into `.done-` can fail for permissions too, so the line may not
    claim a newer version replaced it - only that this round could not confirm it."""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    real_rename = Path.rename

    def refuse(self, target):
        if Path(target).name == f".done-{ulid}":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_rename(self, Path(target))

    monkeypatch.setattr(Path, "rename", refuse)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]
    assert ulid in store.outbox_ulids(paths)
    assert said[-1] == f"{ulid} 這一輪沒能確認（可能是新版本取代了它），留在 outbox 下一輪再送", said
    assert (env / "agora" / "sessions" / ulid / "session.md").is_file()


def test_a_round_that_sends_nothing_says_why_even_with_no_sessions_folder(env, monkeypatch):
    """N3: the very first session has no sessions/ on Drive to land in, and when nothing
    was sent the entry must not just sit there without a reason. (Since the batch dir is
    cut before anything is copied, the race that used to leave an entry unsent silently now
    ends in an explicit "切段失敗" for that entry.)"""
    paths = store.Paths.from_env()
    ulid = _stage(paths, "第一版")
    _two_stages_before_the_raws(monkeypatch, paths, ulid)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]
    assert not (env / "agora" / "sessions").exists()
    assert len(said) == 1 and said[0].startswith(f"{ulid} 切段失敗"), said

    after: list[str] = []
    assert store.upload_batch(store.Drive(paths), paths, warn=after.append) == []
    assert not any("切段失敗" in s for s in after), after

def test_a_crlf_session_md_still_parses_like_it_did_with_read_text(env):
    """#130 fix: `read_entry` moved from text mode to bytes; a CRLF header still has to parse."""
    paths = store.Paths.from_env()
    folder = store.stage(paths, _header(), "## user\nhi\n", b'{"x": 1}')
    path = folder / "session.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    hdr = store.read_entry(folder)
    assert store.raw_name(h.agora_of(hdr)["raw"]), "the header parsed and names its raw"


# --- #152: a listing that lost a folder is not proof that it is gone ----------------


def _update_on_drive(env, paths, title="改過了"):
    """X confirmed on Drive, then an edit of X waiting in the outbox (`.update`)."""
    ulid = _stage(paths, "原本的")
    assert store.upload_batch(store.Drive(paths), paths) == []
    folder = store.stage(paths, _kept_header(ulid, title), f"## user\n{title}\n", b'{"v": 2}')
    store.remember(paths, folder)
    store.mark_update(folder)
    return ulid


def _session_folder_names(env) -> list[str]:
    return sorted(p.name for p in (env / "agora" / "sessions").iterdir() if p.is_dir())


def test_a_listing_that_misses_a_live_update_waits_instead_of_rescuing(env, monkeypatch):
    """#152: the existence check decides from one listing. A Drive error had left a
    folder out of it (twice for real), and the edit was saved as a new session while X
    was still on Drive - two continues diverging. The directed look says X is there,
    so the entry waits for a round whose listing works, and X keeps its id."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid], "列表出錯不該另存成新的 Session"
    assert any("列表裡沒有，但直接查還在" in s for s in said), said

    monkeypatch.delenv("FAKE_RCLONE_OMIT")
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert ulid not in store.outbox_ulids(paths)
    assert "改過了" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_a_recheck_that_misses_a_live_update_waits_instead_of_rescuing(env, monkeypatch):
    """#152's second check, right before the copies: the first listing saw X, the
    re-check silently lost it. It must not rescue X either - the folder is still there,
    and the next round sends the same id."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FROM", "2")     # the re-check, not the first
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid], "列表出錯不該另存成新的 Session"
    assert any("列表裡沒有，但直接查還在" in s for s in said), said

    monkeypatch.delenv("FAKE_RCLONE_OMIT")
    monkeypatch.delenv("FAKE_RCLONE_OMIT_FROM")
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert "改過了" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_a_directed_look_that_comes_back_empty_waits(env, monkeypatch):
    """#152 B1: an empty answer is not proof of a deletion. The same Drive failure that
    left X out of the whole listing can answer the directed look with `[]` and exit 0;
    rescuing on that would fork a session that is still on Drive. X waits, and the next
    round whose listing works sends the same id."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    monkeypatch.setattr(store.Drive, "list_one", lambda self, _ulid: {})
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid], "空結果不該另存成新的 Session"
    assert any("直接查是空的，不能確認被刪" in s for s in said), said

    monkeypatch.delenv("FAKE_RCLONE_OMIT")
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert ulid not in store.outbox_ulids(paths)
    assert "改過了" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_a_recheck_whose_directed_look_is_empty_also_waits(env, monkeypatch):
    """#152 B1 at the second check: the first listing saw X, the re-check lost it, and
    the directed look is empty too. Still no proof of a deletion - X waits in the
    outbox and the next round sends the same id."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FROM", "2")     # the re-check, not the first
    monkeypatch.setattr(store.Drive, "list_one", lambda self, _ulid: {})
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid], "空結果不該另存成新的 Session"
    assert any("直接查是空的，不能確認被刪" in s for s in said), said

    monkeypatch.delenv("FAKE_RCLONE_OMIT")
    monkeypatch.delenv("FAKE_RCLONE_OMIT_FROM")
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert ulid not in store.outbox_ulids(paths)
    assert "改過了" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_a_failed_directed_look_after_a_missed_listing_waits(env, monkeypatch):
    """#152 / V6: not seeing it is not seeing it gone. The listing missed X and the
    directed look failed too; that is not proof of a deletion, so no rescue - X waits
    in the outbox for a round that can see."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", ulid)         # only `lsjson …/<ulid>` fails
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid]
    assert any("直接查也失敗" in s for s in said), said


def test_a_failed_directed_look_at_the_recheck_also_waits(env, monkeypatch):
    """#152: the same rule at the second check - a failed look there leaves X in the
    outbox, never rescued on a listing alone."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FROM", "2")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", ulid)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid]
    assert any("直接查也失敗" in s for s in said), said


def test_a_listing_that_misses_a_deleted_update_still_rescues(env, monkeypatch):
    """#152: the directed look is only a second opinion. When it agrees with the
    listing that X is gone, the existing rescue is exactly what happens."""
    import shutil
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    shutil.rmtree(env / "agora" / "sessions" / ulid)      # another machine deleted it
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)          # and the listing lost it too
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == []

    assert ulid not in store.outbox_ulids(paths)
    assert _session_folder_names(env) != [ulid], "X 不該被傳回去"
    (new_id,) = _session_folder_names(env)
    assert new_id != ulid
    assert any("已被別台刪除" in s for s in said), said


def test_a_directed_look_that_says_rc3_is_a_deletion(env, monkeypatch):
    """#152: rclone's rc=3 for a missing folder is the proof a missing listing lacks.
    The fake's rc=3 message has no "directory not found" in it, so only the
    returncode can tell this from a failed look - an empty answer does not count."""
    import shutil
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    shutil.rmtree(env / "agora" / "sessions" / ulid)      # another machine deleted it
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)          # and the listing lost it too
    monkeypatch.setenv("FAKE_RCLONE_NOT_FOUND_RC", "3")
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == []

    assert ulid not in store.outbox_ulids(paths)
    assert _session_folder_names(env) != [ulid], "X 不該被傳回去"
    (new_id,) = _session_folder_names(env)
    assert new_id != ulid
    assert any("已被別台刪除" in s for s in said), said


# --- #155: a missing file in the confirmation is not proof of a deletion ------------


def _raws_on_drive(env, ulid: str) -> list[str]:
    return sorted(p.name for p in (env / "agora" / "sessions" / ulid).glob("seg-*"))


def _update_with_old_raw(env, paths, title="改過了"):
    """X confirmed on Drive, then an edit of X waiting; the old raw's name on Drive."""
    ulid = _update_on_drive(env, paths, title)
    (old_raw,) = _raws_on_drive(env, ulid)
    return ulid, old_raw


# --- #155/#164 -------------------------------------------------------------


# #164: with the folder ID compared, the file names a confirmation lists no longer decide
# anything - the five tests below are #155's, rewritten for the ID-based rule.


def _purge_after_raws(env, ulid):
    """Another machine purges the folder right after this round's raws copy; the
    session.md copy then re-creates it - a new folder ID, holding only our file."""
    trigger = env.parent / "purge-after.json"
    trigger.write_text(json.dumps({"after": "seg-", "purge": f"sessions/{ulid}"}),
                       encoding="utf-8")
    return trigger


def test_a_confirmation_that_loses_a_file_is_not_a_deletion(env, monkeypatch):
    """#155 -> #164: the confirmation listing drops the old raw while the folder is
    alive. The names prove nothing now: the folder ID is the same before and after, so
    nothing is looked up, nothing is purged or rescued, and the update is confirmed in
    this very round."""
    paths = store.Paths.from_env()
    ulid, old_raw = _update_with_old_raw(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FILES", old_raw)
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FILES_FROM", "3")   # the confirmation only
    looks: list[str] = []
    monkeypatch.setattr(store.Drive, "list_one",
                        lambda self, looked_at: looks.append(looked_at) or {})
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [], f"ID 沒變，這一輪就該確認：{left}"
    assert looks == [], f"ID 沒變不需要定向查詢：{looks}"
    assert _session_folder_names(env) == [ulid], "活的資料夾不該被清掉或另存"
    assert not any("copy 期間被別台刪掉" in s for s in said), said
    assert "改過了" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_another_machines_cleanup_during_the_copies_is_not_a_deletion(env, monkeypatch):
    """#155 非阻擋 1 -> #164: two machines write the same session; while our copies are
    in flight the other one deletes the old raw it replaced and puts its own raw-Z in.
    Names change, the folder (its ID) does not: no purge, no rescue."""
    paths = store.Paths.from_env()
    ulid, old_raw = _update_with_old_raw(env, paths)
    trigger = env.parent / "clean-after.json"
    trigger.write_text(json.dumps({
        "after": "seg-",                       # after this round's segments copy
        "folder": f"sessions/{ulid}",
        "remove": [old_raw],
        "add": {"raw-Z.json": '{"other": 1}'},
    }), encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_CLEAN_AFTER", str(trigger))
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert not trigger.exists(), "the cleanup hook never fired - the test proved nothing"
    assert left == []
    assert _session_folder_names(env) == [ulid], "活著的資料夾不該被清掉或另存"
    assert not any("copy 期間被別台刪掉" in s for s in said), said
    # (Not asserted: whether raw-Z survives. After a confirmed update `_replaced_raws`
    # still deletes every other raw in the folder, the other machine's included - that
    # is today's single-file behaviour and goes away with R3's segments.)


def test_an_id_change_with_an_empty_directed_look_does_not_purge(env, monkeypatch):
    """#164 / #152 B1: the ID moved (the folder was purged and our copy re-created it),
    but the directed look that must back a purge answers `[]` with exit 0 - the Drive
    failure shape of #152. Not proof: no purge, no rescue, the entry waits."""
    paths = store.Paths.from_env()
    ulid, _ = _update_with_old_raw(env, paths)
    trigger = _purge_after_raws(env, ulid)
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(trigger))
    monkeypatch.setattr(store.Drive, "list_one", lambda self, _ulid: {})
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert not trigger.exists(), "the purge hook never fired - the test proved nothing"
    assert left == [ulid]
    assert ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid], "沒有另存成新的 Session"
    assert any("直接查是空的" in s for s in said), said
    assert not any("copy 期間被別台刪掉" in s for s in said), said


def test_an_id_change_with_a_failed_directed_look_does_not_purge(env, monkeypatch):
    """#164 / V6: the ID moved and the directed look failed. Not seeing is not seeing
    it gone: no purge, no rescue, the entry waits."""
    paths = store.Paths.from_env()
    ulid, _ = _update_with_old_raw(env, paths)
    trigger = _purge_after_raws(env, ulid)
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(trigger))

    def failed(self, _ulid):
        raise store.StoreError("injected failure")

    monkeypatch.setattr(store.Drive, "list_one", failed)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid]
    assert _session_folder_names(env) == [ulid]
    assert any("直接查也失敗" in s for s in said), said
    assert not any("copy 期間被別台刪掉" in s for s in said), said


def test_an_id_change_with_a_not_found_look_waits_for_the_next_round(env, monkeypatch):
    """#155 非阻擋 2 -> #164: the listing shows the folder (with a new ID) and the
    directed look says rc=3: the two contradict. Neither is trusted this round."""
    paths = store.Paths.from_env()
    ulid, _ = _update_with_old_raw(env, paths)
    trigger = _purge_after_raws(env, ulid)
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(trigger))

    def gone(self, _ulid):
        raise store.StoreError("error listing: object not found", returncode=3)

    monkeypatch.setattr(store.Drive, "list_one", gone)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid], f"同一筆更新該留在 outbox：{left}"
    assert _session_folder_names(env) == [ulid], "證據互相矛盾時不該 purge 或另存"
    assert any("矛盾" in s for s in said), said
    assert not any("copy 期間被別台刪掉" in s for s in said), said


def test_an_id_change_with_files_we_did_not_send_is_a_live_folder(env, monkeypatch):
    """#164 (what #155's `ours` check kept): the folder was purged and then written to
    by someone else (raw-Z) before our session.md copy finished. The ID moved, but the
    folder holds a file this round never sent - a live folder, not our own copy on a
    purged one. Taking it down would delete their work: wait."""
    paths = store.Paths.from_env()
    ulid, _ = _update_with_old_raw(env, paths)
    purge = _purge_after_raws(env, ulid)
    monkeypatch.setenv("FAKE_RCLONE_PURGE_AFTER", str(purge))
    clean = env.parent / "clean-after.json"
    clean.write_text(json.dumps({"after": "session.md", "folder": f"sessions/{ulid}",
                                 "add": {"raw-Z.json": '{"other": 1}'}}), encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_CLEAN_AFTER", str(clean))
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert not purge.exists() and not clean.exists(), "a hook never fired"
    assert left == [ulid]
    assert (env / "agora" / "sessions" / ulid / "raw-Z.json").is_file()
    assert _session_folder_names(env) == [ulid]
    assert any("這一輪沒送的檔" in s and "raw-Z.json" in s for s in said), said


def test_a_normal_update_round_does_not_look_at_the_folder_again(env, monkeypatch):
    """#155: the directed look only happens when the confirmation lost a name. A round
    whose confirmation matches the listing before the copies must not add an rclone
    call at all."""
    paths = store.Paths.from_env()
    ulid, _ = _update_with_old_raw(env, paths)
    looks: list[str] = []

    def spy(self, looked_at):
        looks.append(looked_at)
        return {}

    monkeypatch.setattr(store.Drive, "list_one", spy)
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert looks == [], f"正常路徑不該多打 rclone：{looks}"
    assert ulid not in store.outbox_ulids(paths)

# --- #164: folder IDs, and the existence check that needs a session.md ------------------


def test_a_listing_without_directory_ids_falls_back_to_dirs_only(env, monkeypatch):
    """#164 2.1: the recursive listing carried no directory `ID`: the update asks
    `lsjson gdrive:sessions --dirs-only` once before the copies and once for the
    confirmation, and the round goes through as usual."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_NO_DIR_ID", "1")

    assert store.upload_batch(store.Drive(paths), paths) == []

    calls = [json.loads(line) for line in (env.parent / "calls.log").read_text().splitlines()]
    dirs_only = [c for c in calls if "--dirs-only" in c and c[-2:-1] != ["gdrive:"]
                 and any(a.endswith("gdrive:sessions") for a in c)]
    assert len(dirs_only) == 2, f"送出前與確認各補一次：{dirs_only}"
    assert "改過了" in (env / "agora" / "sessions" / ulid / "session.md").read_text(
        encoding="utf-8")


def test_an_update_without_any_folder_id_is_not_sent(env, monkeypatch):
    """#164 2.1 fail-closed: neither the listing nor `--dirs-only` has an ID. Without it
    a purge during the copies could not be told, so the update is not sent at all: it
    stays in the outbox with a warning, and Drive is unchanged."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    before = (env / "agora" / "sessions" / ulid / "session.md").read_bytes()
    monkeypatch.setenv("FAKE_RCLONE_NO_DIR_ID", "all")
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid] and ulid in store.outbox_ulids(paths)
    assert (env / "agora" / "sessions" / ulid / "session.md").read_bytes() == before
    assert any("拿不到資料夾 ID" in s for s in said), said


def test_an_id_that_vanishes_at_the_confirmation_keeps_the_entry(env, monkeypatch):
    """#164 2.1: the IDs were there before the copies but the confirmation cannot read
    them (listing without IDs, `--dirs-only` failing). The copies are done, but whether
    the folder was purged meanwhile is unknown, so nothing leaves the outbox."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    real = store.Drive.list_sessions_with_ids
    seen = {"n": 0}

    def third_without_ids(self):
        got = real(self)
        seen["n"] += 1
        if got is not None and seen["n"] >= 3:
            got.ids.clear()
        return got

    monkeypatch.setattr(store.Drive, "list_sessions_with_ids", third_without_ids)
    monkeypatch.setattr(store.Drive, "folder_ids", lambda self: {})
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid] and ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid]
    assert any("確認時拿不到資料夾 ID" in s for s in said), said


def test_a_folder_with_only_orphan_files_is_gone_not_resumed(env, monkeypatch):
    """#164 2.3: after a purge the first big copy broke and left only a raw (no
    session.md). That is not a session: the next round must treat the id as gone
    and save the edit as a new session - completing the folder would bring the
    purged session back."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    (env / "agora" / "sessions" / ulid / "session.md").unlink()   # only the raw is left
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == []

    assert not (env / "agora" / "sessions" / ulid / "session.md").exists(), "孤兒資料夾被補完復活了"
    assert any("孤兒" in s for s in said), said
    assert any("已被別台刪除" in s for s in said), said
    (new_id,) = [n for n in _session_folder_names(env) if n != ulid]
    assert (env / "agora" / "sessions" / new_id / "session.md").is_file()


def test_a_listing_that_hides_session_md_waits_when_the_look_sees_it(env, monkeypatch):
    """#164 2.3 + #152: the listing shows the folder without its session.md (a Drive
    error), the directed look finds it. A listing error, not an orphan: no rescue."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FILES", "session.md")
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid] and ulid in store.outbox_ulids(paths)
    assert _session_folder_names(env) == [ulid]
    assert any("直接查還在" in s for s in said), said
    assert not any("已被別台刪除" in s for s in said), said


def test_the_fake_gives_a_recreated_folder_a_new_id(env):
    """The fake's contract for #164: the same path purged and created again carries a
    new ID, a top-level name stays its own ID (the agora root relies on that)."""
    paths = store.Paths.from_env()
    drive = store.Drive(paths)
    ulid = _stage(paths)
    assert store.upload_batch(drive, paths) == []
    first = drive.list_sessions_with_ids().ids[ulid]
    drive._run("purge", f"gdrive:sessions/{ulid}")
    drive._run("mkdir", f"gdrive:sessions/{ulid}")
    second = drive._run("lsjson", "gdrive:sessions", "--dirs-only")
    assert first not in second, "the recreated folder kept its old ID"


def test_a_listing_that_exits_non_zero_makes_no_deletion_call(env, monkeypatch):
    """#164 2.2: every whole listing fails (rc != 0, an rclone error and not an empty
    answer). A listing that did not come back is never a complete one: the update
    stays in the outbox, nothing is purged or rescued, and Drive is unchanged."""
    paths = store.Paths.from_env()
    ulid = _update_on_drive(env, paths)
    before = (env / "agora" / "sessions" / ulid / "session.md").read_bytes()
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "--fast-list")      # only the whole listings
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid] and ulid in store.outbox_ulids(paths)
    assert (env / "agora" / "sessions" / ulid / "session.md").read_bytes() == before
    assert _session_folder_names(env) == [ulid]
    assert any("列不出 Drive 的檔案" in s for s in said), said      # no judgement was even tried
    assert not any("存在檢查" in s for s in said), said
    assert not any("已被別台刪除" in s for s in said), said
