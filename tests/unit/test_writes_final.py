"""The last four MUSTs the review found untested (review T3-final, F1-F4).

Only tests here; the code is at HEAD. Each one says what it is holding, and each was
mutation-checked in a copy: taking the behaviour away has to turn one of them red.

* **F1** - 「更新既有 id」is decided when the command writes, not when the uploader
  looks: `continue` writing back, `edit`, and an import that updates an id we already
  have all mark the staged version `.update`; a brand-new import and a `merge` do not.
  Nothing tested that at the command level - every rescue test marked the marker by
  hand - so "never mark it" was invisible, and a session another machine had deleted
  would have been pushed back.
* **F2** - `push` waits: while a uploader holds the lock it does not send anything
  itself, and it returns when the ids it wrote have left the outbox.
* **F3** - the two things sync can say about waiting: 「背景上傳中，N 筆」while somebody
  is uploading, 「N 筆沒上傳成功」when nobody is and they are still there.
* **F4** - the sync in front of a batch import is throttled: two imports inside five
  minutes list Drive once.

And, from change `continue-fork` (tasks 1.1, F1 there), the other half of the same
rule: a session **Drive has never confirmed** - still waiting in the outbox, unmarked -
stays new when it is written back, so it goes up as itself instead of being saved under
a new id as though another machine had deleted it.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
from pathlib import Path
import sys

import pytest

from agora import cli, store

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"

# The command-level fixtures (a fake agent that talks in memory, a fake rclone) are
# test_cli's; they are imported rather than copied so there is one of each.
from test_cli import FakeAgent, env, run  # noqa: F401  (env is a fixture)


def _import(capsys, sid: str = "ses_a", *extra: str) -> tuple[str, str]:
    return run(capsys, "import", "session", "--external-session-id", sid,
               "--agent", "opencode", *extra)


def _is_update(paths: store.Paths, ulid: str) -> bool:
    """Whether the staged version says 「this updates a session Drive already has」."""
    return store._is_update(paths.outbox / ulid)


def _marks(monkeypatch) -> list[str]:
    """Record which staged folders a command decided were updates of a Drive session.

    Watching the decision rather than the marker's file afterwards: the marker only
    exists while the entry is in the outbox, and unit tests upload in the foreground
    before the command returns. The review's mutation is exactly "never mark it", so
    what has to be watched is the decision.
    """
    marked: list[str] = []
    real = store.mark_update

    def spy(folder, kind=""):
        # `kind` is the write's sort, added for G2: it only matters if the session is
        # gone by the time it is sent (a continue's rescue is a continue)
        marked.append(folder.name)
        return real(folder, kind)
    monkeypatch.setattr(store, "mark_update", spy)
    return marked


# --- F1: the marker's place is the foreground write -----------------------------


def test_a_fresh_import_is_not_an_update(env, capsys, monkeypatch):
    """A new session has no id on Drive yet, so `.update` would make the uploader look
    for one that was never there."""
    marked = _marks(monkeypatch)
    code, out, _ = _import(capsys)
    assert code == 0
    assert marked == [], "全新的匯入在 Drive 上還沒有同一個 id，不放 .update"


def test_edit_marks_the_new_version_as_an_update(env, capsys, monkeypatch):
    """T1 Q1: `edit` overwrites something Drive has. Without the mark, a session another
    machine deleted in the meantime would be pushed straight back."""
    _, out, _ = _import(capsys)
    agora_id = out.strip()
    ulid = agora_id.split(":")[1]
    marked = _marks(monkeypatch)

    code, _, err = run(capsys, "edit", "session", agora_id, "--header", "title=改了")

    assert code == 0, err
    assert marked == [ulid], "edit 覆寫的是 Drive 上已有的 id，要放 .update"


def test_continue_writing_back_marks_an_update(env, capsys, monkeypatch, tmp_path):
    """`continue` writes the grown session back onto the id it came from."""
    _, out, _ = _import(capsys)
    agora_id = out.strip()
    ulid = agora_id.split(":")[1]
    marked = _marks(monkeypatch)

    code, _, err = run(capsys, "continue", "session", agora_id, "--agent", "opencode",
                       "--dir", str(tmp_path))

    assert code == 0, err
    assert env.launched, "the agent really ran"
    assert marked == [ulid], "continue 是寫回同一個 id，要放 .update"


def test_an_import_that_updates_a_session_we_already_have_marks_it(env, capsys, monkeypatch):
    """The same id coming back from the agent with new content: the header keeps the
    ULID, so this is an update and has to be marked as one."""
    _, out, _ = _import(capsys, "ses_a")
    first = out.strip()
    marked = _marks(monkeypatch)
    env.sessions["ses_a"].append("又問了一句")            # the agent's session moved on

    code, again, err = _import(capsys, "ses_a")

    assert code == 0, err
    assert again.strip() == first, "same id, new content"
    assert marked == [first.split(":")[1]], \
        "匯入原地更新同一個 id，要放 .update（否則別台刪掉的會被悄悄傳回去）"


def test_a_merge_is_not_an_update(env, capsys, monkeypatch):
    """A merge writes a session of its own. Marking it would send the uploader looking
    for an id Drive has never heard of, and - worse - a merge of something another
    machine deleted would come back under the merged id."""
    _, a, _ = _import(capsys, "ses_a")
    env.sessions["ses_b"] = ["另一個來源", "好"]
    _, b, _ = _import(capsys, "ses_b")
    marked = _marks(monkeypatch)

    code, merged, err = run(capsys, "merge", "session", a, b, "--agent", "opencode")

    assert code == 0, err
    ulid = merged.strip().split(":")[1]
    assert ulid not in (a.split(":")[1], b.split(":")[1]), "a session of its own"
    assert marked == [], "merge 寫的是新的 id，Drive 上從來沒有過，不放 .update"


# --- F2: push waits ------------------------------------------------------------


def test_push_waits_for_the_uploader_instead_of_sending_itself(env, capsys, monkeypatch):
    """spec「push 等背景」: push's contract is "it is on Drive when this returns", so it
    takes the same lock rather than uploading behind the uploader's back (F2).

    Driven from another thread so "it is still waiting" is something the test can see:
    while the lock is held, push must not have returned - and must not have sent
    anything either.
    """
    import threading
    import time

    paths = store.Paths.from_env()
    _, out, _ = _import(capsys)
    agora_id = out.strip()
    held = store.hold_upload_lock(paths, blocking=True)
    assert held is not None, "the lock is what push has to wait for"
    calls = Path(os.environ["FAKE_REMOTE"]).parent / "calls.log"
    before = len(calls.read_text(encoding="utf-8").splitlines())

    codes: list[int] = []
    worker = threading.Thread(target=lambda: codes.append(cli.main(["push", "session", agora_id])))
    worker.start()
    time.sleep(0.5)                     # long enough that waiting is visible

    # it may still be talking to Drive (the sync in front of it lists), but it must not
    # be sending anything: that is the uploader's turn, and it is not finished
    sent = calls.read_text(encoding="utf-8").splitlines()[before:]
    assert not [line for line in sent if '"copy"' in line or '"delete"' in line], \
        f"鎖被別人拿著時 push 不該自己送：{sent}"
    assert not codes, "鎖被別人拿著時 push 應該等，不該自己送"

    held.__exit__()
    worker.join(timeout=60)
    assert not worker.is_alive(), "放開鎖之後 push 應該就結束了"
    assert codes == [0], f"push 回來了，但不是成功：{codes}"


# --- F3: the two things sync can say ---------------------------------------------


def test_sync_says_somebody_is_uploading_when_somebody_is(env, capsys):
    """spec「背景上傳」: while somebody holds the lock, the entry is not this command's
    business, and the user is told a person is already on it."""
    paths = store.Paths.from_env()
    _stage_one(paths, "還在等")
    held = store.hold_upload_lock(paths, blocking=True)      # somebody is uploading
    assert held is not None

    code, _, err = run(capsys, "search", "session", "--filter", "text~=還在等")

    held.__exit__()
    assert code == 0
    assert "背景上傳中，1 筆" in err, f"應該說有人正在傳：{err}"
    assert "沒上傳成功" not in err


def test_sync_says_nobody_is_sending_when_nobody_is(env, capsys, monkeypatch):
    """The other half of the same sentence: nothing running, the entry still there, is a
    failure - and 「背景上傳中」 would blame nobody for it."""
    paths = store.Paths.from_env()
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")        # so the entry stays
    _stage_one(paths, "還在等")

    code, _, err = run(capsys, "search", "session", "--filter", "text~=還在等")

    assert code == 0
    assert "1 筆沒上傳成功" in err, f"應該說沒有人在上傳：{err}"
    assert "背景上傳中" not in err


def _stage_one(paths: store.Paths, title: str) -> str:
    """One session waiting in the outbox, with the header a real import writes."""
    from agora import header as h
    ulid = h.new_ulid()
    hdr = {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}",
           "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                     "updated_at": "2026-10-02T00:00:00Z", "relation": "import",
                     "parents": [], "source": {"agent": "opencode", "session_id": "ses_a",
                                               "created_at": "2026-10-01T00:00:00Z"}}}
    folder = store.stage(paths, hdr, f"## user\n{title}\n", b'{"v": 1}')
    store.remember(paths, folder)
    return folder.name


# --- F4: the sync in front of a batch import is throttled -----------------------


def test_two_imports_inside_five_minutes_list_drive_once(env, capsys, monkeypatch):
    """batch-commands MODIFIED: the import's own sync is throttled, so a second import
    does not stand in front of Drive again - it would be the same listing it already
    has, and the listing is the slow part (F4).

    What is compared is the throttle stamp, not a call count: the upload path lists
    Drive too, so counting listings would count that instead.
    """
    env.sessions["ses_b"] = ["另一個來源", "好"]
    code, _, err = _import(capsys, "ses_a")
    assert code == 0, err
    stamp = store.Paths.from_env().state / "last-sync"
    assert stamp.exists(), "第一次匯入同步過，節流記錄在了"
    before = stamp.read_text()

    code, _, err = _import(capsys, "ses_b")

    assert code == 0, err
    assert stamp.read_text() == before, \
        "五分鐘內的第二次匯入不該再列一次檔（節流記錄沒有被更新）"

# --- G5: regressions that used to hang instead of fail ---------------------------


def test_push_says_it_is_still_waiting_while_the_lock_is_held(env, capsys, monkeypatch):
    """R7 / G5: push takes the lock without blocking so it can say why it is waiting.
    Back to a blocking flock, that line is dead code again - this fails rather than
    hangs: push runs in a daemon thread and everything here has a time limit."""
    import threading
    import time
    from agora import cache

    paths = store.Paths.from_env()
    _, out, _ = _import(capsys)
    monkeypatch.setattr(cache, "WAIT_SAY_EVERY", 0.3)
    held = store.hold_upload_lock(paths)
    assert held is not None
    codes: list[int] = []
    worker = threading.Thread(target=lambda: codes.append(cli.main(["push", "session", out.strip()])),
                              daemon=True)
    try:
        worker.start()
        time.sleep(1.2)
        err = capsys.readouterr().err
    finally:
        held.close()
    worker.join(timeout=30)
    assert "還在等" in err, err
    assert not worker.is_alive(), "放開鎖之後 push 應該在時限內結束"
    assert codes == [0]


def test_the_lock_is_free_after_the_background_and_after_asking_about_it(env, capsys):
    """E2 / G5: the lock not let go used to show up only as a push waiting forever.
    Asked directly instead: right after a background run, and right after
    `uploader_is_running`, another descriptor can take it."""
    from agora import background

    paths = store.Paths.from_env()
    _import(capsys)
    background.run(paths)
    again = store.hold_upload_lock(paths)
    assert again is not None, "背景做完沒有放開上傳鎖"
    again.close()
    assert store.uploader_is_running(paths) is False
    again = store.hold_upload_lock(paths)
    assert again is not None, "uploader_is_running 問完沒有放開上傳鎖"
    again.close()


# --- change continue-fork 1.1: what Drive has never confirmed is still new ------


def _drive() -> Path:
    """The fake Drive's root. `env` here is test_cli's fixture, which is the agent."""
    return Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions"


def test_writing_back_a_session_drive_never_had_goes_up_as_itself(env, capsys, monkeypatch, tmp_path):
    """local-first-writes MODIFIED「寫回還沒被 Drive 確認過的 Session 不算更新既有 id」.

    Offline: import X, edit X, continue X - all three while X is still waiting in the
    outbox and Drive has never heard of it. Then let the upload through. Marking those
    writes `.update` sent the uploader looking for an id that was never there, so it
    saved X's work as a session of its own and X never reached Drive at all. What has
    to be on Drive is X, with the edit and the continue in it - no other session, and
    nothing said about another machine having deleted anything.

    Another session is on Drive first, so the listing the uploader compares against is
    a complete one: with an empty Drive there is no `sessions/` to list, `list_sessions`
    answers None, and「Drive 上沒有它」is not evidence of anything (N12) - the rescue
    would never even be reached.
    """
    from agora import header as h

    paths = store.Paths.from_env()
    env.sessions["ses_b"] = ["另一個來源", "好"]
    _, other, _ = _import(capsys, "ses_b")
    assert (_drive() / other.split(":")[1]).is_dir(), "Drive 先有別的 Session"

    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")            # offline from here on
    code, out, _ = _import(capsys)
    ulid = out.split(":")[1]
    assert code == 3, "offline: the import is safe in the outbox, and says so"
    assert ulid in store.outbox_ulids(paths)

    code, _, err = run(capsys, "edit", "session", out, "--header", "title=離線時改的")
    assert code == 3, err
    code, same, err = run(capsys, "continue", "session", out, "--agent", "opencode",
                          "--dir", str(tmp_path))
    assert code == 3, err
    assert same == out, "continue 寫回同一個 id"
    assert env.launched, "the agent really ran"
    assert ulid in store.outbox_ulids(paths)

    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    capsys.readouterr()
    left = store.upload_batch(store.Drive(paths), paths)
    err = capsys.readouterr().err

    assert left == [], f"沒東西該留在 outbox：{left}"
    assert "已被別台刪除" not in err, f"Drive 從來沒有 X，不該說它被別台刪掉：{err}"
    assert store.take_notices(paths) == [], "沒有別存，就沒有下一次要說的事"
    on_drive = sorted(p.name for p in _drive().iterdir() if p.is_dir())
    assert on_drive == sorted([ulid, other.split(":")[1]]), \
        f"Drive 上應該就是 X 本身加本來就在的那個，沒有另存：{on_drive}"
    text = (_drive() / ulid / "session.md").read_text(encoding="utf-8")
    hdr, body = h.split_document(text)
    assert hdr["id"] == f"agora:{ulid}", "上傳的是 X 本身"
    assert hdr["title"] == "離線時改的" and "接著做完了" in body, "離線期間的兩次寫回都在"
    assert [p["id"] for p in h.agora_of(hdr).get("parents") or []] == [], "X 沒有另存成別的"


def test_a_session_drive_really_lost_is_still_saved_as_a_new_one(env, capsys, monkeypatch):
    """The other half of that rule, and the one it must not take away: an id Drive had
    and lost is an update, and an update whose id is gone is saved under a new one.

    X is on Drive, an edit of it is staged while we cannot reach Drive, another machine
    deletes X there, and then the upload goes through. Nothing in the outbox says X was
    never confirmed - there is no entry for it at all - so the marker is written and the
    rescue runs, exactly as before.
    """
    import shutil
    from agora import header as h

    paths = store.Paths.from_env()
    _, out, _ = _import(capsys)
    ulid = out.split(":")[1]
    assert (_drive() / ulid).is_dir()

    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    code, _, err = run(capsys, "edit", "session", out, "--header", "title=改過了")
    assert code == 3, err
    assert store._is_update(paths.outbox / ulid), "Drive 有過這個 id，edit 要放 .update"

    shutil.rmtree(_drive() / ulid)                             # another machine deletes it
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    capsys.readouterr()

    assert store.upload_batch(store.Drive(paths), paths) == []
    err = capsys.readouterr().err
    assert "已被別台刪除" in err, f"真的被刪掉的還是該另存：{err}"
    assert not (_drive() / ulid).exists(), "X 沒有被傳回去"
    new_ids = sorted(p.name for p in _drive().iterdir() if p.is_dir())
    assert len(new_ids) == 1 and new_ids[0] != ulid
    hdr, _ = h.split_document(
        (_drive() / new_ids[0] / "session.md").read_text(encoding="utf-8"))
    assert [p["id"] for p in h.agora_of(hdr)["parents"]] == [f"agora:{ulid}"]
    assert hdr["title"] == "改過了", "另存的是這次改過的那一份"


def test_two_offline_writes_then_a_later_delete_still_rescues(env, capsys, monkeypatch):
    """C3: the second write is the one the rule could get wrong.

    X is on Drive; while we cannot reach it, two writes land on it. The second one finds
    an X sitting in the outbox - so "there is an entry, so Drive has it" is exactly the
    reasoning that would exempt it, put it back on Drive, and quietly undo another
    machine's deletion. The entry says what it is, and it says Drive has this id, so the
    rescue still happens.
    """
    import shutil
    from agora import header as h

    paths = store.Paths.from_env()
    _, out, _ = _import(capsys)
    ulid = out.split(":")[1]

    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    for title in ("第一次改", "第二次改"):
        code, _, err = run(capsys, "edit", "session", out, "--header", f"title={title}")
        assert code == 3, err
        assert store._is_update(paths.outbox / ulid), \
            f"outbox 裡已經有 X（{title}）：Drive 本來就有這個 id，不該被當成還沒確認過"

    shutil.rmtree(_drive() / ulid)                             # another machine deletes it
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    capsys.readouterr()

    assert store.upload_batch(store.Drive(paths), paths) == []
    err = capsys.readouterr().err
    assert "已被別台刪除" in err, f"被刪掉的 X 還是要另存，不能傳回去：{err}"
    assert not (_drive() / ulid).exists(), "X 沒有被傳回去，別台的刪除沒有被撤銷"
    new_ids = sorted(p.name for p in _drive().iterdir() if p.is_dir())
    assert len(new_ids) == 1 and new_ids[0] != ulid, new_ids
    hdr, _ = h.split_document(
        (_drive() / new_ids[0] / "session.md").read_text(encoding="utf-8"))
    assert [p["id"] for p in h.agora_of(hdr)["parents"]] == [f"agora:{ulid}"]
    assert hdr["title"] == "第二次改", "另存的是最後寫到的那一份"


def test_an_offline_import_updated_in_place_goes_up_as_itself(env, capsys, monkeypatch):
    """C4: the same rule on the third road into `_save` - an import whose agent session
    moved on, written back over the id the first import made.

    Nothing about this one looks like a fresh session: same ULID, same source, and by now
    the outbox has an entry for it. But Drive has never confirmed it, so it goes up as X,
    and it is X with the new messages - not a second copy saved under a new id.
    """
    from agora import header as h

    paths = store.Paths.from_env()
    env.sessions["ses_b"] = ["另一個來源", "好"]
    _, other, _ = _import(capsys, "ses_b")                    # Drive has to have a listing

    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copy")
    _, out, _ = _import(capsys, "ses_a")
    ulid = out.split(":")[1]
    env.sessions["ses_a"].append("來源端又多了一則")            # the agent session moved on
    code, again, err = _import(capsys, "ses_a")

    assert code == 3, err
    assert again == out, "同一個來源原地更新，還是同一個 id"
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    capsys.readouterr()
    left = store.upload_batch(store.Drive(paths), paths)
    err = capsys.readouterr().err

    assert left == [], f"沒東西該留在 outbox：{left}"
    assert "已被別台刪除" not in err, f"Drive 從來沒有 X，不該說它被別台刪掉：{err}"
    on_drive = sorted(p.name for p in _drive().iterdir() if p.is_dir())
    assert on_drive == sorted([ulid, other.split(":")[1]]), \
        f"Drive 上應該就是 X 本身加本來就在的那個，沒有另存：{on_drive}"
    text = (_drive() / ulid / "session.md").read_text(encoding="utf-8")
    hdr, body = h.split_document(text)
    assert hdr["id"] == f"agora:{ulid}", "上傳的是 X 本身"
    assert "來源端又多了一則" in body, "X 是最新的那份"


def test_a_write_that_lands_while_the_entry_is_being_swapped_in(env, monkeypatch):
    """C11: `stage` renames the old entry aside and only then moves the new one in, so
    for a moment there is no `outbox/<ULID>` at all. A second writer asking in that window
    used to be told「Drive 從來沒有 X」and marked `.update` - the very bug the rule exists
    to stop, back on a #28 periodic write-back racing an `edit` in another terminal. The
    entry being swapped in is the answer, and it is still in the outbox under `.old-`.

    Asked at the instant the rename happened - between the two renames, not after the
    swap - because after it there is an entry again and the question is answered the
    ordinary way.
    """
    from agora import header as h

    paths = store.Paths.from_env()
    hdr = {"type": "Session", "title": "換資料夾", "tags": [], "id": f"agora:{h.new_ulid()}",
           "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                     "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                     "source": {"agent": "opencode", "session_id": "ses_a",
                                "created_at": "2026-10-01T00:00:00Z"}}}
    ulid = hdr["id"].split(":")[1]
    store.stage(paths, hdr, "## user\n第一版\n", b'{"v": 1}')
    assert store.never_confirmed(paths, ulid)

    asked: list[bool] = []
    real_rename = Path.rename

    def rename_and_ask(self, target):
        moved = real_rename(self, target)
        if self.name == ulid and Path(target).name == f".old-{ulid}":
            asked.append(store.never_confirmed(paths, ulid))    # the instant in between
        return moved

    monkeypatch.setattr(Path, "rename", rename_and_ask)
    store.stage(paths, hdr, "## user\n第二版\n", b'{"v": 2}')

    assert asked == [True], "換資料夾的那一瞬間，X 還在 outbox 裡（換名），不是沒有了"
    assert (paths.outbox / ulid).is_dir() and store.never_confirmed(paths, ulid)

    # and the other half: an entry that says Drive has this id is still an update there
    store.mark_update(paths.outbox / ulid)
    asked.clear()
    store.stage(paths, hdr, "## user\n第三版\n", b'{"v": 3}')
    assert asked == [False], "那個換名中的項目說了 Drive 有這個 id，就不能豁免"

def test_never_confirmed_is_only_about_an_entry_nobody_claimed(env):
    """What `never_confirmed` answers, one condition at a time.

    Every one of the three is load-bearing: the marker is what says Drive already had
    this id, `.done-<ULID>` says somebody is confirming it right now (and then we cannot
    tell), and an id with no entry here has been confirmed already - or was never ours.
    """
    from agora import header as h

    paths = store.Paths.from_env()
    fresh = _stage_one(paths, "Drive 從來沒有")
    assert store.never_confirmed(paths, fresh), "outbox 裡沒標成更新的，就是還沒確認過的"

    claimed = _stage_one(paths, "已經標成更新")
    store.mark_update(paths.outbox / claimed)
    assert not store.never_confirmed(paths, claimed), "有人說過 Drive 有這個 id"

    comparing = _stage_one(paths, "上傳程序正在比對")
    (paths.outbox / f".done-{comparing}").mkdir()
    assert not store.never_confirmed(paths, comparing), \
        "有人正在比對這個 id，就不能說它還沒被確認過"

    assert not store.never_confirmed(paths, h.new_ulid()), \
        "outbox 裡沒有這個 id：早就被確認過了，或者從來不是這裡的"
