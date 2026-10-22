"""#159 (part 2): the paths that did something irreversible on one Drive listing now ask the folder.

A listing is one call, and on a bad day (Drive 500, #152) it can lose a live folder. Each test here
loses the folder from the listing (`FAKE_RCLONE_OMIT`) while the folder is still there: before the
fix the path deleted the local copy / the outbox, or marked the session as deleted on another
machine; now one directed look (`store._directed_look`) is asked first and only "not there" counts.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import shutil

import pytest

from agora import cache, cli, store
from test_segment_write import _drive_folder, _stage, _update_session, data_of, env  # noqa: F401


@pytest.fixture
def world(env, monkeypatch):
    """One uploaded session (X) with a newer version still in the outbox, and another session (Y)
    so that `sessions/` exists whatever happens to X."""
    paths = store.Paths.from_env()
    x, _ = _stage(paths, data_of(2))
    y, _ = _stage(paths, data_of(1, tag=5))
    store.upload_batch(store.Drive(paths), paths)
    _update_session(paths, env, x, "還沒上傳的新版", data=data_of(2, tag=9))
    return paths, x, y, env


def _alive_in_drive(env, ulid) -> bool:
    return (_drive_folder(env, ulid) / "session.md").is_file()


def _lose_from_listing(monkeypatch, ulid):
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)          # the whole listing misses it; a directed look sees it


def _look_fails(monkeypatch):
    def broken(self, ulid):
        raise store.StoreError("rclone lsjson 失敗（rc=1）：500 backend error")
    monkeypatch.setattr(store.Drive, "list_one", broken)


# --- 2: delete_session ---------------------------------------------------------------------------


def test_delete_keeps_the_local_copy_and_the_outbox_when_the_listing_only_missed_the_folder(world, monkeypatch):
    paths, x, y, env = world
    _lose_from_listing(monkeypatch, x)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "purge")

    with pytest.raises(store.StoreError):
        store.delete_session(paths, store.Drive(paths), x)

    assert (paths.outbox / x).is_dir(), "the version that never reached Drive is not thrown away"
    assert (paths.mirror / x / "session.md").is_file() and store.Index(paths).header(x) is not None
    assert _alive_in_drive(env, x)


@pytest.mark.parametrize("never_confirmed,message", [(False, "找不到 sessions/"), (True, "直接查還找得到它")])
def test_delete_with_no_sessions_listing_keeps_everything_when_the_folder_answers(world, monkeypatch,
                                                                                  never_confirmed, message):
    """No `sessions/` in the listing and a folder that answers: nothing to believe, nothing thrown away
    (for a session Drive once had, before the look is even made)."""
    paths, x, y, env = world
    monkeypatch.setattr(store.Drive, "list_sessions", lambda self: None)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "purge")

    with pytest.raises(store.StoreError, match=message):
        store.delete_session(paths, store.Drive(paths), x, never_confirmed=never_confirmed)

    assert (paths.outbox / x).is_dir() and (paths.mirror / x).is_dir()


def _wrong_root(paths):
    """The machine's config points at a root folder that does not exist: every listing says
    `sessions/` is not there, and so does every directed look under it."""
    import json
    settings = paths.config / "config.json"
    data = json.loads(settings.read_text())
    data["folders"] = {name: "BOGUS-ROOT-ID" for name in data["folders"]}
    settings.write_text(json.dumps(data))


def test_delete_keeps_a_session_drive_once_had_when_the_root_is_wrong(world):
    """The reviewer's BOGUS-ROOT-ID probe: nothing is wrong with the session, everything is wrong
    with where we look. A session Drive once had (it was uploaded) stays, and the purge failure stands."""
    paths, x, y, env = world
    _wrong_root(paths)

    with pytest.raises(store.StoreError, match="root／folder 設定錯"):
        store.delete_session(paths, store.Drive(paths), x)

    assert (paths.outbox / x).is_dir() and (paths.mirror / x).is_dir()
    assert store.Index(paths).header(x) is not None


def test_delete_still_settles_a_session_drive_never_confirmed_on_a_fresh_drive(world):
    """A session that only ever lived in the outbox: nothing could be on Drive, so the look settles it."""
    paths, x, y, env = world
    _wrong_root(paths)

    store.delete_session(paths, store.Drive(paths), x, never_confirmed=True)

    assert not (paths.outbox / x).exists() and not (paths.mirror / x).exists()


def test_the_queue_remembers_that_the_session_was_never_confirmed(env, monkeypatch):
    """`agora delete` removes the outbox entry before queueing the purge, so it is what writes down
    whether Drive ever confirmed the session; the background reads it back."""
    paths = store.Paths.from_env()
    from agora import background
    uploaded, _ = _stage(paths, data_of(1, tag=4))
    store.upload_batch(store.Drive(paths), paths)
    fresh, _ = _stage(paths, data_of(1, tag=8))               # a session only in the outbox
    monkeypatch.setattr(background, "start", lambda paths=None: background.STARTED)   # leave the queue to read
    assert cli.main(["delete", "session", fresh, uploaded, "--yes"]) == 0
    assert store.queue_never_confirmed(paths, fresh) is True
    assert store.queue_never_confirmed(paths, uploaded) is False
    assert store.queue_never_confirmed(paths, "2T" + "0" * 24) is False, "no queue file: not confirmed-never"


@pytest.mark.parametrize("how", ["look-fails", "look-empty", "orphans-only"])
def test_delete_keeps_everything_when_the_direct_look_cannot_say_it_is_gone(world, monkeypatch, how):
    paths, x, y, env = world
    _lose_from_listing(monkeypatch, x)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "purge")
    if how == "look-fails":
        _look_fails(monkeypatch)
    elif how == "look-empty":
        monkeypatch.setattr(store.Drive, "list_one", lambda self, ulid: {})
    else:
        monkeypatch.setattr(store.Drive, "list_one", lambda self, ulid: {"seg-0001-0000-aaaaaaaaaaaa.part": "m"})

    with pytest.raises(store.StoreError):
        store.delete_session(paths, store.Drive(paths), x)

    assert (paths.outbox / x).is_dir() and (paths.mirror / x).is_dir()


def test_delete_still_forgets_a_session_that_is_really_gone(world, capsys):
    """S1-4: an interrupted delete is finished, not failed - the folder is gone, `sessions/` is there."""
    paths, x, y, env = world
    shutil.rmtree(_drive_folder(env, x))

    store.delete_session(paths, store.Drive(paths), x)

    assert not (paths.outbox / x).exists() and not (paths.mirror / x).exists()
    assert store.Index(paths).header(x) is None
    assert _alive_in_drive(env, y), "the other session is untouched"
    assert "已經沒有了" in capsys.readouterr().err


# --- 3: pull --not-exist-delete ----------------------------------------------------------------------


def _pull_delete(paths, ulid):
    return cache.pull(paths, [ulid], {}, not_exist_delete=True)


def test_pull_not_exist_delete_keeps_the_copy_when_the_listing_only_missed_the_folder(world, monkeypatch, capsys):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)                       # nothing waiting: only the listing stands in the way
    _lose_from_listing(monkeypatch, x)

    assert _pull_delete(paths, x)[1] == 0

    assert (paths.mirror / x / "session.md").is_file() and store.Index(paths).header(x) is not None
    assert "本機的副本不動" in capsys.readouterr().err


@pytest.mark.parametrize("how", ["look-fails", "look-empty"])
def test_pull_not_exist_delete_keeps_the_copy_when_the_look_cannot_say(world, monkeypatch, how):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    _lose_from_listing(monkeypatch, x)
    if how == "look-fails":
        _look_fails(monkeypatch)
    else:
        monkeypatch.setattr(store.Drive, "list_one", lambda self, ulid: {})

    _pull_delete(paths, x)

    assert (paths.mirror / x / "session.md").is_file()


def test_pull_not_exist_delete_still_deletes_what_is_really_gone(world, capsys):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    shutil.rmtree(_drive_folder(env, x))

    assert _pull_delete(paths, x) == (1, 0)

    assert not (paths.mirror / x).exists() and store.Index(paths).header(x) is None
    assert "本機的副本已刪" in capsys.readouterr().err


# --- 4: sync's marker and cli._lost_in_cloud ------------------------------------------------------------


def test_sync_does_not_mark_a_session_the_listing_only_missed(world, monkeypatch):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    _lose_from_listing(monkeypatch, x)

    store.sync(paths)

    assert store.Index(paths).missing_in_cloud() == []
    assert store.Index(paths).cloud_has(x)


def test_sync_does_not_mark_when_the_look_fails(world, monkeypatch):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    _lose_from_listing(monkeypatch, x)
    _look_fails(monkeypatch)

    store.sync(paths)

    assert store.Index(paths).missing_in_cloud() == []


def test_sync_marks_a_session_that_is_really_gone_and_does_not_ask_again(world, monkeypatch):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    shutil.rmtree(_drive_folder(env, x))

    store.sync(paths)
    assert store.Index(paths).missing_in_cloud() == [x]

    asked = []
    real = store.Drive.list_one
    monkeypatch.setattr(store.Drive, "list_one", lambda self, ulid: asked.append(ulid) or real(self, ulid))
    store.sync(paths)
    assert store.Index(paths).missing_in_cloud() == [x]
    assert asked == [], "already marked and still absent: no new look"


def test_a_marked_session_that_comes_back_in_the_listing_is_unmarked(world):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    saved = _drive_folder(env, x)
    backup = env.parent / "backup-x"
    shutil.copytree(saved, backup)
    shutil.rmtree(saved)
    store.sync(paths)
    assert store.Index(paths).missing_in_cloud() == [x]
    shutil.copytree(backup, saved)

    store.sync(paths)

    assert store.Index(paths).missing_in_cloud() == []


def test_lost_in_cloud_is_false_when_the_listing_only_missed_the_folder(world, monkeypatch):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    _lose_from_listing(monkeypatch, x)
    assert cli._lost_in_cloud(paths, f"agora:{x}") is False


def test_lost_in_cloud_is_false_when_the_look_fails_and_true_when_it_is_really_gone(world, monkeypatch):
    paths, x, y, env = world
    shutil.rmtree(paths.outbox / x)
    shutil.rmtree(_drive_folder(env, x))
    with monkeypatch.context() as m:
        _look_fails(m)
        assert cli._lost_in_cloud(paths, f"agora:{x}") is False
    assert cli._lost_in_cloud(paths, f"agora:{x}") is True
