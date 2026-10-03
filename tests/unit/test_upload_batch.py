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

    def raws_only(drive, paths_, names):
        # Only the raws call fails. The session.md call must still be reachable, or
        # this test cannot tell "stopped after the raws" from "everything failed".
        if any("/raw-" in n for n in names):
            raise store.StoreError("原始檔傳不上去")
        return real_copy(drive, paths_, names)

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
    raw = (paths.outbox / ulid / h.agora_of(hdr)["raw"]["file"]).read_bytes()
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
