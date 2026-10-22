"""#173: two edges of the folder-ID record (#169, design B.5.7).

1. The folder's ID changed but somebody else's files stand in it: every round just waits. After N
   rounds against the same ID the person is told once - and nothing is purged or rescued.
2. `push --not-exist-upload` revives a session on purpose: a record a crash left for the folder
   that is gone must not make the next round read the revived folder as "re-created by us".
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import shutil

import pytest

from agora import cache, store
from test_segment_write import (_drive_folder, _joined, _record, _stage, _update_session, data_of,  # noqa: F401
                                env)


def _stale_record(paths, ulid, folder_id="an-id-this-folder-no-longer-has"):
    store._note_folder_record(paths, ulid, folder_id, ["session.md"], lambda *_: None)


def _sessions(env) -> list[str]:
    return sorted(p.name for p in (env / "agora" / "sessions").iterdir())


# --- 1: told once --------------------------------------------------------------------------------


@pytest.fixture
def stuck(env, monkeypatch):
    """An update waiting on a folder whose ID differs from the record, with a foreign file in it."""
    monkeypatch.setenv("AGORA_FOLDER_ID_WAIT_ROUNDS", "3")
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    (_drive_folder(env, ulid) / "somebody-elses.txt").write_text("another machine wrote this")
    _stale_record(paths, ulid)
    _update_session(paths, env, ulid)
    return paths, ulid


def _told(paths) -> list[str]:
    return store.take_notices(paths)


def test_the_third_round_in_a_row_tells_the_person_once_and_never_again(stuck, env):
    paths, ulid = stuck
    for rnd in (1, 2):
        assert store.upload_batch(store.Drive(paths), paths, notices=True) == [ulid]
        assert _told(paths) == [], f"round {rnd}: not yet"
    assert store.upload_batch(store.Drive(paths), paths, notices=True) == [ulid]
    lines = _told(paths)
    assert len(lines) == 1 and ulid in lines[0] and "somebody-elses.txt" in lines[0]
    assert str(paths.state / "folder-ids" / f"{ulid}.json") in lines[0], "says how to let it go on"
    for _ in range(3):
        assert store.upload_batch(store.Drive(paths), paths, notices=True) == [ulid]
    assert _told(paths) == [], "said once, not every round"
    assert _record(paths, ulid)["warned"] is True


def test_waiting_purges_nothing_and_rescues_nothing(stuck, env):
    paths, ulid = stuck
    for _ in range(5):
        store.upload_batch(store.Drive(paths), paths, notices=True)
    assert _sessions(env) == [ulid]
    assert (_drive_folder(env, ulid) / "somebody-elses.txt").is_file(), "their file is untouched"
    assert (paths.outbox / ulid).is_dir(), "the update waits in the outbox"


def test_the_foreground_says_it_in_its_own_output_and_leaves_no_notice(stuck, capsys):
    paths, ulid = stuck
    for _ in range(3):
        store.upload_batch(store.Drive(paths), paths)
    assert f"{ulid} 的更新已經連續 3 輪沒送出" in capsys.readouterr().err
    assert _told(paths) == []


def test_removing_the_record_lets_the_update_through_as_an_ordinary_one(stuck, env):
    paths, ulid = stuck
    for _ in range(3):
        store.upload_batch(store.Drive(paths), paths, notices=True)
    _told(paths)
    (paths.state / "folder-ids" / f"{ulid}.json").unlink()          # what the message tells them to do
    assert store.upload_batch(store.Drive(paths), paths, notices=True) == []
    assert _sessions(env) == [ulid] and _joined(env, ulid) == data_of(2)


def test_a_different_current_id_starts_the_count_again(env, tmp_path):
    paths = store.Paths.from_env()
    ulid = "2T" + "0" * 24
    store._note_folder_record(paths, ulid, "old", ["session.md"], lambda *_: None)
    record = store._read_folder_record(paths, ulid)
    said = []

    def wait(current):
        store._note_blocked_wait(paths, ulid, store._read_folder_record(paths, ulid), current, ["x"],
                                 said.append, False)

    for current in ("id-a", "id-a", "id-b", "id-b"):
        wait(current)
    assert said == [], "id-a waited twice and id-b twice: neither reached three"
    wait("id-b")
    assert len(said) == 1 and "連續 3 輪" in said[0]
    assert store._read_folder_record(paths, ulid)["folder_id"] == "old", "the evidence ID is kept"
    assert record["folder_id"] == "old"


@pytest.mark.parametrize("value,rounds", [("1", 1), ("5", 5), ("0", 3), ("many", 3), ("", 3)])
def test_the_number_of_rounds_is_read_from_the_environment(monkeypatch, value, rounds):
    monkeypatch.setenv("AGORA_FOLDER_ID_WAIT_ROUNDS", value)
    assert store.folder_wait_rounds() == rounds


def test_a_wait_for_any_other_reason_is_never_counted_or_told(env, monkeypatch):
    """An empty or failing look, a missing session.md: those answer themselves next round and say
    nothing - only 'somebody else's files' is a stand-off."""
    monkeypatch.setenv("AGORA_FOLDER_ID_WAIT_ROUNDS", "1")
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _stale_record(paths, ulid)
    _update_session(paths, env, ulid)
    monkeypatch.setattr(store, "_directed_look",
                        lambda drive, u: store._Look(there=None, files=set(), error="boom"))
    assert store.upload_batch(store.Drive(paths), paths, notices=True) == [ulid]
    assert _told(paths) == [] and "waits" not in _record(paths, ulid)


# --- 2: push --not-exist-upload ---------------------------------------------------------------------


def _revive_setup(paths, env):
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _stale_record(paths, ulid)                       # a crash left this behind
    shutil.rmtree(_drive_folder(env, ulid))          # another machine deleted the session
    return ulid


def test_a_session_revived_on_purpose_is_not_taken_down_by_a_record_from_before(env):
    paths = store.Paths.from_env()
    ulid = _revive_setup(paths, env)

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (1, 0)
    assert _record(paths, ulid) is None, "the record described a folder that is gone"

    _update_session(paths, env, ulid, "復活之後的編輯", data=data_of(2, tag=3))
    assert store.upload_batch(store.Drive(paths), paths) == []

    assert _sessions(env) == [ulid], "no purge, no second session"
    assert _joined(env, ulid) == data_of(2, tag=3)
    assert store.take_notices(paths) == []


def test_a_push_that_fails_keeps_the_record(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid = _revive_setup(paths, env)
    monkeypatch.setattr(store, "push_mirror", lambda *a, **k: (_ for _ in ()).throw(store.StoreError("no")))
    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)
    assert _record(paths, ulid) is not None
