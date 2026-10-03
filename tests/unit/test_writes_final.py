"""The last four MUSTs the review found untested (docs/review/T3-final.md F1-F4).

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
"""

from __future__ import annotations

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