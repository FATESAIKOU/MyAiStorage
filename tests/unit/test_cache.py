"""Local caches (design 5.10): `agora pull` / `agora push` (T1 R1/R5/R7, K4/K5).

Every test drives the fake rclone and a fake agent: nothing here reaches Drive,
nor the user's own agent sessions.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import threading

import pytest

from agora import cache, store
from agora.agents.base import Exported, Listed

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


class Agent:
    name = "claude"

    def __init__(self, texts, listed=(), name=None):
        self.texts, self.listed, self.exports = texts, list(listed), 0
        self.lock = threading.Lock()
        if name:
            self.name = name

    def export(self, session_id):
        with self.lock:
            self.exports += 1
        if session_id not in self.texts:
            raise RuntimeError("gone")
        return Exported(session_id=session_id, raw=json.dumps({"m": self.texts[session_id]}).encode())

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def list_sessions(self):
        return self.listed


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """A Drive that is a folder, and an rclone that is a python script."""
    root = tmp_path / "remote"
    root.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(root))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return root


def sessions_on(drive: Path) -> Path:
    return drive / "agora" / "sessions"


def calls(drive: Path) -> list[list[str]]:
    log = drive.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def copyto_since(drive: Path, mark: int, ulid: str) -> list[str]:
    """The remote file names the calls after `mark` wrote, in order."""
    return [c[-1].rsplit("/", 1)[-1] for c in calls(drive)[mark:]
            if "copyto" in c and c[-1].startswith(f"gdrive:sessions/{ulid}/")]


def _header(ulid=None):
    return {"type": "Session", "title": "CSV 規劃", "tags": [], "refs": [], "case": None,
            "id": f"agora:{ulid or store.h.new_ulid()}",
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                      "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                      "source": {"agent": "opencode", "session_id": "ses_source",
                                 "created_at": "2026-10-01T00:00:00Z"}}}


def _on_drive(paths, body="## user\n把 CSV 轉成 Markdown 表格\n", raw=b'{"x": 1}') -> str:
    """One session staged and pushed, so it is really on the fake Drive."""
    hdr = _header()
    store.stage(paths, hdr, body, raw)
    store.upload_batch(store.Drive(paths), paths)
    return hdr["id"].split(":", 1)[1]


def _uploaded(drive: Path, ulid: str) -> set[str]:
    folder = sessions_on(drive) / ulid
    return {p.name for p in folder.iterdir()} if folder.is_dir() else set()


def _raw_name(paths, ulid: str) -> str:
    hdr, _ = store.h.split_document((paths.mirror / ulid / "session.md").read_text())
    return hdr["agora"]["raw"]["file"]


# --- the agent full-text cache ------------------------------------------------


def test_full_text_is_kept_and_reread_only_when_the_session_is_newer():
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"]})
    first = cache.local_reading(paths, agent, "s", "2026-10-02T00:00:00Z")
    assert "## user\n問" in first and agent.exports == 1
    assert cache.local_reading(paths, agent, "s", "2026-10-02T00:00:00Z") == first and agent.exports == 1
    agent.texts["s"] = ["問", "答", "又問"]
    assert "又問" in cache.local_reading(paths, agent, "s", "2026-10-03T00:00:00Z") and agent.exports == 2
    assert (paths.reading / "claude" / "s.md").exists()


def test_search_finds_cached_text_whatever_the_width_and_case():
    paths = store.Paths.from_env()
    cache.local_reading(paths, Agent({"s": ["Ｈｅｌｌｏ 表格"]}), "s")
    cache.local_reading(paths, Agent({"t": ["別的"]}), "t")
    assert list(cache.search_cached(paths, "claude", "hello")) == ["s"]
    assert list(cache.search_cached(paths, "opencode", "hello")) == []


def test_two_threads_caching_two_sessions_do_not_collide(drive):   # review K4
    """The staging file used to be a fixed `.tmp` next to the target: two threads
    writing two sessions in one directory raced on it and one write was lost."""
    paths = store.Paths.from_env()
    both_in = threading.Barrier(2, timeout=10)
    started = []

    class Slow(Agent):
        def export(self, session_id):
            started.append(session_id)
            both_in.wait()        # both threads are inside export at the same time
            return Exported(session_id=session_id,
                            raw=json.dumps({"m": [session_id, "答"]}).encode())

    agent = Slow({s: [s] for s in ("s1", "s2")})
    errors = []

    def pull_one(session_id):
        try:
            cache.local_reading(paths, agent, session_id, "2026-10-02T00:00:00Z")
        except Exception as e:      # pragma: no cover - the point is that this stays empty
            errors.append(e)

    threads = [threading.Thread(target=pull_one, args=(s,)) for s in ("s1", "s2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    assert sorted(started) == ["s1", "s2"]      # they really overlapped
    for session_id in ("s1", "s2"):
        assert session_id in (paths.reading / "claude" / f"{session_id}.md").read_text()
    assert not list((paths.reading / "claude").glob("*.tmp"))   # no staging file left behind


# --- pull ---------------------------------------------------------------------


def test_pull_brings_one_session_down_with_its_raw_and_says_k_of_n(drive, capsys):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    assert cache.pull(paths, [ulid], {}) == (1, 0)
    assert "pull 1/1" in capsys.readouterr().err
    mirror = paths.mirror / ulid
    assert (mirror / "session.md").is_file()
    assert (mirror / _raw_name(paths, ulid)).is_file()   # the raw came too
    assert store.Index(paths).header(ulid) is not None    # searchable again


def test_pull_takes_a_bare_ulid_and_an_agora_prefixed_one(drive, capsys):
    """R5: no prefix means an agora id; `agora:` says the same thing."""
    paths = store.Paths.from_env()
    first = _on_drive(paths)
    second = _on_drive(paths)
    assert cache.pull(paths, [first, f"agora:{second}"], {}) == (2, 0)
    err = capsys.readouterr().err
    assert "pull 1/2" in err and "pull 2/2" in err
    assert (paths.mirror / first / "session.md").is_file()
    assert (paths.mirror / second / "session.md").is_file()


def test_pull_of_an_agent_session_caches_its_full_text(drive):
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"]}, [Listed("s", "/tmp/p", "t", "2026-10-02T00:00:00Z")], name="opencode")
    assert cache.pull(paths, ["opencode:s"], {"opencode": agent}) == (1, 0)
    assert "## user\n問" in (paths.reading / "opencode" / "s.md").read_text()
    assert agent.exports == 1


def test_pull_of_an_agent_session_skips_one_that_is_not_stale(drive, capsys):
    """R4: a re-run after Ctrl-C does not pay for what it already has - and P3: what it
    skips is not counted as pulled, it is counted as skipped."""
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"]}, [Listed("s", "/tmp/p", "t", "2026-10-02T00:00:00Z")], name="claude")
    assert cache.pull(paths, ["claude:s"], {"claude": agent}) == (1, 0)
    capsys.readouterr()
    assert cache.pull(paths, ["claude:s"], {"claude": agent}) == (0, 0)
    assert "已經是新的，略過 1 個" in capsys.readouterr().err
    assert agent.exports == 1


def test_pull_of_a_bare_agent_id_says_which_prefix_to_write(drive, capsys):   # review Q5
    """`ses_…` and a bare uuid are agent ids. Reading one as an agora ULID would go
    looking for a Drive folder that cannot exist and report it as gone."""
    paths = store.Paths.from_env()
    agent = Agent({}, name="opencode")
    assert cache.pull(paths, ["ses_abc", "1b2f5a54-0b0a-4a3e-9c6a-0f0d0f0d0f0d"],
                      {"opencode": agent, "claude": agent}) == (0, 2)
    err = capsys.readouterr().err
    assert err.count("請寫前綴") == 2 and "opencode:" in err and "claude:" in err
    assert agent.exports == 0            # it never went looking for a session


def test_pull_of_an_id_the_cloud_does_not_have_changes_nothing(drive, capsys):
    """A session another machine deleted is not this command's to drop (Q1)."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    shutil.rmtree(sessions_on(drive) / ulid)          # ... then deleted over there

    assert cache.pull(paths, [ulid], {}) == (1, 0)
    assert "雲端沒有" in capsys.readouterr().err
    assert (paths.mirror / ulid / "session.md").is_file()
    assert store.Index(paths).header(ulid) is not None


def test_pull_of_one_that_is_already_fresh_asks_rclone_for_nothing(drive, capsys):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    assert cache.pull(paths, [ulid], {}) == (1, 0)          # the first one is a pull
    mark = len(calls(drive))
    capsys.readouterr()
    assert cache.pull(paths, [ulid], {}) == (0, 0)          # P3: the second is a skip
    assert "已經是新的，略過 1 個" in capsys.readouterr().err
    assert not [c for c in calls(drive)[mark:] if "copyto" in c]   # nothing downloaded again
    assert "雲端沒有" not in capsys.readouterr().err


def test_pull_goes_on_past_a_failure_and_reports_k_of_n(drive, capsys):   # review K5
    """One unreadable session must not take the rest of the batch with it."""
    paths = store.Paths.from_env()
    good = _on_drive(paths)
    other = _on_drive(paths)
    agent = Agent({}, name="opencode")
    assert cache.pull(paths, ["opencode:gone", good, other], {"opencode": agent}) == (2, 1)
    err = capsys.readouterr().err
    assert "pull 3/3" in err and "拉不到" in err
    assert (paths.mirror / good / "session.md").is_file()


# --- push ---------------------------------------------------------------------


def test_push_sends_session_md_and_the_raw_it_names_and_nothing_else(drive, capsys):
    """R7: an older raw, a *.partial and a .DS_Store stay here."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})                     # so the mirror really has the raw
    raw = _raw_name(paths, ulid)
    for name in ("raw-000000000000.json", "session.md.partial", ".DS_Store"):
        (paths.mirror / ulid / name).write_text("不該被傳上去", encoding="utf-8")

    mark = len(calls(drive))
    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert "push 1/1" in capsys.readouterr().err
    assert sorted(copyto_since(drive, mark, ulid)) == sorted([raw, "session.md"])
    assert _uploaded(drive, ulid) == {"session.md", raw}
    assert (paths.mirror / ulid / ".DS_Store").is_file()      # still here, still not up there


def test_push_overwrites_the_copy_on_drive(drive):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    edited = (paths.mirror / ulid / "session.md").read_text() + "\n本機又改了\n"
    (paths.mirror / ulid / "session.md").write_text(edited, encoding="utf-8")

    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert (sessions_on(drive) / ulid / "session.md").read_text() == edited


def test_push_sends_only_session_md_when_the_raw_is_not_here(drive, capsys):   # Q7
    """A header can name a raw this machine does not have - a merge has none. The
    reading version is the session, so it still goes up."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    (paths.mirror / ulid / _raw_name(paths, ulid)).unlink()

    mark = len(calls(drive))
    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert "本機沒有" in capsys.readouterr().err
    assert copyto_since(drive, mark, ulid) == ["session.md"]
    # Drive keeps the raw it already had: push overwrites, it does not tidy up
    assert _uploaded(drive, ulid) == {"session.md", _raw_name_on_drive(drive, ulid)}


def _raw_name_on_drive(drive: Path, ulid: str) -> str:
    """The raw name Drive has for this session (the mirror copy is gone)."""
    hdr, _ = store.h.split_document((sessions_on(drive) / ulid / "session.md").read_text())
    return hdr["agora"]["raw"]["file"]


def test_push_does_not_revive_a_session_the_cloud_lost(drive, capsys):
    """K1/Q1: the old sync pushed whatever was local, so another machine's delete
    came back as a resurrected session. Push only touches what Drive still has."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    shutil.rmtree(sessions_on(drive) / ulid)

    assert cache.push(paths, [ulid], {}) == (0, 0)
    assert "雲端沒有" in capsys.readouterr().err
    assert not (sessions_on(drive) / ulid).exists()    # still deleted over there
    assert (paths.mirror / ulid / "session.md").is_file()


def test_push_sends_the_outbox_first(drive):
    """A session this machine just wrote is still in the outbox; push is how it
    goes up, and it must not be refused as 'not on Drive'."""
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    store.stage(paths, hdr, "## user\n還沒上傳\n", b'{"x": 9}')

    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert not (paths.outbox / ulid).exists()
    assert _uploaded(drive, ulid) == {"session.md", hdr["agora"]["raw"]["file"]}


def test_push_goes_on_past_a_failure_and_reports_k_of_n(drive, capsys):   # review K5
    paths = store.Paths.from_env()
    first, second, broken = _on_drive(paths), _on_drive(paths), _on_drive(paths)
    cache.pull(paths, [first, second, broken], {})
    shutil.rmtree(paths.mirror / broken)      # Drive has it, here it is gone: this one fails
    done, failed = cache.push(paths, [first, broken, second], {})
    assert (done, failed) == (2, 1)
    err = capsys.readouterr().err
    assert "push 1/3" in err and "push 3/3" in err and "傳不上去" in err


def test_one_unreadable_id_does_not_stop_the_others(drive, capsys, monkeypatch):
    """review H1: push looks at the outbox once for the whole batch, and reading an id is
    what tells it whether that id arrived late. An id it cannot read is that one id's
    problem - the rest still goes up, and the batch still counts it as failed."""
    paths = store.Paths.from_env()
    wanted = _on_drive(paths)
    cache.pull(paths, [wanted], {})
    real = store.push_outbox

    def push_outbox_then_stage(drive_, paths_, warn=store.warn, notices=False):
        left = real(drive_, paths_, warn, notices)
        folder = store.stage(paths_, _header("快照之後才寫好的"),
                             "## user\n晚到的那筆\n", b'{"x": 9}')
        store.remember(paths_, folder)
        return left

    monkeypatch.setattr(store, "push_outbox", push_outbox_then_stage)
    agent = Agent({}, name="opencode")

    done, failed = cache.push(paths, ["ses_x", wanted], {"opencode": agent})

    assert (done, failed) == (1, 1), "the good one went up, the bad one is a failure"
    assert "ses_x 傳不上去" in capsys.readouterr().err


def test_push_only_takes_agora_ids(drive, capsys):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    agent = Agent({}, name="opencode")
    assert cache.push(paths, ["opencode:s", ulid], {"opencode": agent}) == (1, 1)
    assert "push 只吃 agora" in capsys.readouterr().err

# --- review T1-sec2: what the first version of these two got wrong ----------


def test_push_does_not_count_an_outbox_entry_that_failed(drive, capsys):   # S2-2
    """With every copyto failing, push used to report one pushed and zero failed
    while the session was still in the outbox."""
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    store.stage(paths, hdr, "## user\n還沒上傳\n", b'{"x": 9}')
    os.environ["FAKE_RCLONE_FAIL"] = "copyto"
    try:
        assert cache.push(paths, [ulid], {}) == (0, 1)
    finally:
        os.environ.pop("FAKE_RCLONE_FAIL")
    assert "仍在 outbox" in capsys.readouterr().err
    assert (paths.outbox / ulid).is_dir()


def test_pull_of_agent_ids_never_asks_drive(drive, capsys):   # S2-3
    """Nothing in a batch of agent ids needs Drive, so an offline machine still
    caches them."""
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"]}, [Listed("s", "/tmp/p", "t", None)], name="claude")
    os.environ["FAKE_RCLONE_FAIL"] = "lsjson"
    try:
        assert cache.pull(paths, ["claude:s"], {"claude": agent}) == (1, 0)
    finally:
        os.environ.pop("FAKE_RCLONE_FAIL")
    assert "連不上 Drive" not in capsys.readouterr().err
    assert (paths.reading / "claude" / "s.md").is_file()


def test_pull_marks_every_agora_id_failed_when_drive_is_gone(drive, capsys):   # S2-3
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    os.environ["FAKE_RCLONE_FAIL"] = "lsjson"
    try:
        assert cache.pull(paths, [ulid], {}) == (0, 1)
    finally:
        os.environ.pop("FAKE_RCLONE_FAIL")
    assert "連不上 Drive" in capsys.readouterr().err


def test_pull_does_not_index_a_session_whose_raw_is_not_there_yet(drive, capsys):   # S2-4
    """S1/G3: an unfinished session is not searchable and not continuable."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    (sessions_on(drive) / ulid / _raw_name_on_drive(drive, ulid)).unlink()   # Drive lost the raw

    assert cache.pull(paths, [ulid], {}) == (1, 0)
    assert "先不建索引" in capsys.readouterr().err
    assert store.Index(paths).header(ulid) is None
    assert not (paths.mirror / ulid / "session.md").exists()


def test_an_id_nowhere_is_not_found(drive, capsys):   # S2-5
    paths = store.Paths.from_env()
    assert cache.pull(paths, ["01ARZ3NDEKTSV4RRFFQ69G5FAV"], {}) == (0, 1)
    assert cache.push(paths, ["01ARZ3NDEKTSV4RRFFQ69G5FAV"], {}) == (0, 1)
    err = capsys.readouterr().err
    assert err.count("本機和雲端都沒有") == 2


def test_the_same_id_twice_is_one_line_of_work(drive, capsys):   # S2-7
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    assert cache.pull(paths, [ulid, ulid], {}) == (1, 0)
    assert "pull 1/1" in capsys.readouterr().err and "pull 2/2" not in capsys.readouterr().err


def test_push_checks_the_md5_drive_reports(drive, monkeypatch):   # S2-7
    """A copyto that returned zero is not proof that the bytes arrived."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    drive = store.Drive(paths)
    monkeypatch.setattr(store.Drive, "list_one", lambda self, one: {"session.md": "0" * 32})
    with pytest.raises(store.StoreError, match="md5 不符"):
        store.push_mirror(drive, paths, ulid, store.h.split_document(
            (paths.mirror / ulid / "session.md").read_text())[0])


def test_pull_leaves_a_session_that_is_still_in_the_outbox_alone(drive, capsys):   # S2-7
    """What is staged here is newer than anything on Drive; pulling would put the
    older copy in the mirror and the session would go backwards."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    hdr = _header(ulid)
    store.stage(paths, hdr, "## user\n本機剛寫的\n", None)     # not uploaded, mirror untouched
    assert cache.pull(paths, [ulid], {}) == (1, 0)
    assert "不覆蓋" in capsys.readouterr().err
    assert not (paths.mirror / ulid / "session.md").exists()
    assert (paths.outbox / ulid).is_dir()


# --- 3.2 the two flags: delete or revive, only when asked -------------------


def _lost_on_drive(paths, drive, ulid: str) -> None:
    """A session another machine deleted: mirrored and indexed here, gone from there."""
    cache.pull(paths, [ulid], {})
    shutil.rmtree(sessions_on(drive) / ulid)
    store.sync(paths)          # the sync that notices, and marks it


def test_not_exist_delete_drops_the_local_copy(drive, capsys):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)

    assert cache.pull(paths, [ulid], {}, not_exist_delete=True) == (1, 0)
    assert "本機的副本已刪" in capsys.readouterr().err
    assert not (paths.mirror / ulid).exists()
    assert store.Index(paths).header(ulid) is None


def test_not_exist_delete_keeps_what_has_not_been_uploaded(drive, capsys):
    """Q2: work that never reached Drive is not something to tidy away."""
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    store.stage(paths, hdr, "## user\n還沒上傳\n", b'{"x": 9}')

    cache.pull(paths, [ulid], {}, not_exist_delete=True)
    assert "還沒上傳，不能刪" in capsys.readouterr().err
    assert (paths.outbox / ulid).is_dir()


def test_not_exist_delete_keeps_a_session_being_continued(drive, capsys):
    """Only a *locked* record counts: a leftover from a crashed continue does not
    keep a session from being pulled away for ever (review G1)."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    paths.pending.mkdir(parents=True, exist_ok=True)
    lock = open(paths.pending / f"{ulid}.json", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)

    cache.pull(paths, [ulid], {}, not_exist_delete=True)
    assert "正在接續，不能刪" in capsys.readouterr().err
    assert (paths.mirror / ulid / "session.md").is_file()
    lock.close()


def test_not_exist_delete_goes_through_when_the_pending_record_is_a_leftover(drive, capsys):
    """G1: nobody holds this lock - the continue could not be finished. That must not
    block the delete for ever."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    paths.pending.mkdir(parents=True, exist_ok=True)
    (paths.pending / f"{ulid}.json").write_text("{}", encoding="utf-8")

    cache.pull(paths, [ulid], {}, not_exist_delete=True)
    assert "本機的副本已刪" in capsys.readouterr().err
    assert not (paths.mirror / ulid).exists()


def test_not_exist_delete_refuses_when_there_is_no_sessions_folder_at_all(drive, capsys):
    """G3: Drive without sessions/ is a broken folder id or token, not a deletion."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    assert cache.pull(paths, [ulid], {}) == (1, 0)      # a local copy to lose
    import shutil
    shutil.rmtree(drive / "agora" / "sessions")

    assert cache.pull(paths, [ulid], {}, not_exist_delete=True) == (0, 1)
    assert "不能確定它是被刪掉的" in capsys.readouterr().err
    assert (paths.mirror / ulid / "session.md").is_file()


def test_not_exist_delete_for_an_agent_id_drops_its_cached_text(drive, capsys):
    """For an agent id the flag means "the agent does not have this one any more".
    It lists another session, so we know the listing is readable and `s` is gone."""
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"], "t": ["別的", "對話"]},
                  [Listed("t", "/tmp/p", "別的", "2026-10-02T00:00:00Z")], name="claude")
    cache.local_reading(paths, agent, "s", "2026-10-02T00:00:00Z")

    assert cache.pull(paths, ["claude:s"], {"claude": agent}, not_exist_delete=True) == (1, 0)
    assert "快取已刪" in capsys.readouterr().err
    assert not (paths.reading / "claude" / "s.md").exists()


def test_not_exist_delete_for_an_agent_id_the_agent_still_has(drive, capsys):
    """M2: the flag says "the agent lost it", so ask the agent. It still has this
    one, so this is an ordinary pull - the cached text stays and gets refreshed."""
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"]}, [Listed("s", "/tmp/p", "t", "2026-10-02T00:00:00Z")])
    cache.local_reading(paths, agent, "s", "2026-10-01T00:00:00Z")     # cached, and stale
    agent.texts["s"] = ["問", "答", "補一句"]

    assert cache.pull(paths, ["claude:s"], {"claude": agent}, not_exist_delete=True) == (1, 0)
    assert "快取已刪" not in capsys.readouterr().err
    assert "## user\n問" in (paths.reading / "claude" / "s.md").read_text()
    assert agent.exports == 2             # the one above, plus this pull refreshing the cache


def test_not_exist_delete_keeps_the_cache_when_the_agent_lists_nothing(drive, capsys):
    """F5: an agent whose listing comes back empty while we hold its text is an
    agent we could not read (no db, unknown layout), not one that lost everything."""
    paths = store.Paths.from_env()
    blind = Agent({}, [])                       # cannot read anything
    cache.local_reading(paths, Agent({"s": ["問", "答"]}, name="claude"), "s")

    assert cache.pull(paths, ["claude:s"], {"claude": blind}, not_exist_delete=True) == (1, 0)
    assert "清單讀不到" in capsys.readouterr().err
    assert (paths.reading / "claude" / "s.md").is_file()


def test_not_exist_upload_sends_the_session_back(drive):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    assert store.Index(paths).missing_in_cloud() == [ulid]

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (1, 0)
    assert _uploaded(drive, ulid) == {"session.md", _raw_name_on_drive(drive, ulid)}
    assert store.sync(paths).missing_in_cloud() == []      # and the marker is gone


def test_not_exist_upload_refuses_when_the_raw_is_not_here(drive, capsys):
    """Half a session is not a revived session: the raw is the point of it."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    (paths.mirror / ulid / _raw_name(paths, ulid)).unlink()

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)
    assert "不傳半套" in capsys.readouterr().err
    assert not (sessions_on(drive) / ulid).exists()
