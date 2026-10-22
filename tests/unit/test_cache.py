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
from agora.agents.base import Exported, Listed, reading

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
CLAUDE_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
CLAUDE_SID = "11111111-2222-4333-8444-555555555555"


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


def copied_since(drive: Path, mark: int) -> list[str]:
    """Every file name the `copy --files-from` calls after `mark` carried (copies.log)."""
    log = drive.parent / "copies.log"
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [f["name"].split("/", 1)[1] for row in rows[mark:] for f in row["files"]]


def copies_mark(drive: Path) -> int:
    log = drive.parent / "copies.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def _raw_name(paths, ulid: str) -> str:
    hdr, _ = store.h.split_document((paths.mirror / ulid / "session.md").read_text())
    return store.raw_name(hdr["agora"]["raw"])


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


def _claude_agent():
    """The real claude adapter over a fake session under the isolated HOME (CL4)."""
    from agora.agents import claude
    proj = claude.projects_dir() / "-tmp-p"
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy(CLAUDE_FIX / "cl-basic.jsonl", proj / f"{CLAUDE_SID}.jsonl")
    aux = CLAUDE_FIX / "cl-aux" / CLAUDE_SID
    if aux.is_dir():
        shutil.copytree(aux, proj / CLAUDE_SID)
    return claude.ADAPTER


def _watch_raw(monkeypatch):
    """Every `Exported.raw` access, by session id: the whole-bytes read-back (#118)."""
    reads, original = [], Exported.raw
    monkeypatch.setattr(
        Exported, "raw",
        property(lambda self: reads.append(self.session_id) or original.fget(self)))
    return reads


def test_filling_the_cache_does_not_read_the_raw_back_when_the_export_carries_turns(monkeypatch):
    """#118: claude's export already parsed the session, so caching its reading must not
    pull the whole raw back (it was written to a temp file, fsynced, then read again and
    parsed a second time). The text stays exactly what `reading` produced."""
    agent = _claude_agent()
    expected = reading(agent, agent.export(CLAUDE_SID).raw)      # the old path, as the oracle
    reads = _watch_raw(monkeypatch)

    text = cache.local_reading(store.Paths.from_env(), agent, CLAUDE_SID)

    assert reads == [], "the turns were already there; nothing had to read the raw back"
    assert text == expected
    assert (store.Paths.from_env().reading / "claude" / f"{CLAUDE_SID}.md").read_text(
        encoding="utf-8") == expected


def test_an_export_without_turns_still_parses_its_raw_for_the_cache(monkeypatch):
    """opencode's export carries no turns: `reading_of` falls back to the raw, exactly as
    `reading` always did. That read-back is allowed - #118 stops paying for a parse that
    already happened, it does not forbid `.raw` where it is the only way to a reading."""
    reads = _watch_raw(monkeypatch)
    text = cache.local_reading(store.Paths.from_env(), Agent({"s": ["問", "答"]}, name="opencode"), "s")
    assert reads == ["s"]
    assert text == "## user\n問\n\n## assistant\n答\n"


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


def test_push_sends_session_md_and_the_segments_it_names_and_nothing_else(drive, capsys):
    """R7: an older raw, a *.partial and a .DS_Store stay here. Segments are content-addressed,
    so one Drive already has is not sent again - only session.md goes - and a segment it
    lost is sent in the same `copy` call as nothing else."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})                     # so the mirror really has the raw
    for name in ("raw-000000000000.json", "session.md.partial", ".DS_Store"):
        (paths.mirror / ulid / name).write_text("不該被傳上去", encoding="utf-8")
    segments = _segments_on_drive(drive, ulid)

    mark = copies_mark(drive)
    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert "push 1/1" in capsys.readouterr().err
    assert copied_since(drive, mark) == ["session.md"], "the segments Drive has are not sent again"
    assert _uploaded(drive, ulid) == {"session.md", *segments}
    assert (paths.mirror / ulid / ".DS_Store").is_file()      # still here, still not up there

    # a segment Drive lost goes back, with session.md, and nothing else
    (sessions_on(drive) / ulid / sorted(segments)[0]).unlink()
    mark = copies_mark(drive)
    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert sorted(copied_since(drive, mark)) == sorted([sorted(segments)[0], "session.md"])
    assert _uploaded(drive, ulid) == {"session.md", *segments}


def test_push_goes_up_in_two_copy_calls_however_many_segments(drive, monkeypatch):
    """3.17: a session of many segments is one `copy --files-from` for the segments and one
    for session.md - not a `copyto` per segment."""
    monkeypatch.setenv("AGORA_RAW_CHUNK", "4096")
    paths = store.Paths.from_env()
    hdr = _header()
    store.stage(paths, hdr, "## user\n很多段\n", bytes(range(256)) * 160)      # 40 KiB: 10 segments
    store.upload_batch(store.Drive(paths), paths)
    ulid = hdr["id"].split(":", 1)[1]
    segments = _segments_on_drive(drive, ulid)
    assert len(segments) == 10
    cache.pull(paths, [ulid], {})                        # the mirror gets the whole raw, joined
    shutil.rmtree(sessions_on(drive) / ulid)             # lost: a revive sends everything
    mark_calls, mark = len(calls(drive)), copies_mark(drive)

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (1, 0)

    sent = calls(drive)[mark_calls:]
    assert not [c for c in sent if "copyto" in c], "no per-segment copyto"
    log = (drive.parent / "copies.log").read_text().splitlines()[mark:]
    assert len(log) == 2 and len(json.loads(log[0])["files"]) == 10
    assert [f["name"].split("/", 1)[1] for f in json.loads(log[1])["files"]] == ["session.md"]
    assert _uploaded(drive, ulid) == {"session.md", *segments}

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

    mark = copies_mark(drive)
    assert cache.push(paths, [ulid], {}) == (1, 0)
    assert "本機沒有" in capsys.readouterr().err
    assert copied_since(drive, mark) == ["session.md"]
    # Drive keeps the segments it already had: push overwrites, it does not tidy up
    assert _uploaded(drive, ulid) == {"session.md", *_segments_on_drive(drive, ulid)}


def test_push_does_not_send_half_a_session_when_both_the_raw_and_the_segments_are_gone(drive, capsys):
    """If Drive lacks a segment and the whole raw is not here either, nothing can be cut:
    refuse that one, upload nothing."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    (paths.mirror / ulid / _raw_name(paths, ulid)).unlink()
    (sessions_on(drive) / ulid / sorted(_segments_on_drive(drive, ulid))[0]).unlink()

    mark = copies_mark(drive)
    assert cache.push(paths, [ulid], {}) == (0, 1)
    assert "不傳半套" in capsys.readouterr().err
    assert copied_since(drive, mark) == []


def _segments_on_drive(drive: Path, ulid: str) -> set[str]:
    """The segment names the session.md on Drive lists."""
    hdr, _ = store.h.split_document((sessions_on(drive) / ulid / "session.md").read_text())
    return {seg["file"] for seg in hdr["agora"]["raw"]["segments"]}


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
    assert _uploaded(drive, ulid) == {"session.md", *[s["file"] for s in hdr["agora"]["raw"]["segments"]]}


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
    (sessions_on(drive) / ulid / sorted(_segments_on_drive(drive, ulid))[0]).unlink()   # Drive lost the raw

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
    assert _uploaded(drive, ulid) == {"session.md", *_segments_on_drive(drive, ulid)}
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


# --- #159: `--not-exist-upload` asks the folder before it sends -----------------


def test_not_exist_upload_does_not_overwrite_a_session_the_listing_only_missed(
        drive, capsys, monkeypatch):
    """#159: the whole listing can lose a live folder (#152). `--not-exist-upload`
    used to take that as its premise and send the local copy - an older one over
    the newer one still on Drive. One directed look sees the folder, so this entry
    is refused and Drive keeps its bytes."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    on_drive = sessions_on(drive) / ulid / "session.md"
    newer = on_drive.read_text(encoding="utf-8") + "\n別台比較新\n"   # the version here is older
    on_drive.write_text(newer, encoding="utf-8")
    names = ("session.md", *sorted(_segments_on_drive(drive, ulid)))
    before = {name: store.md5_file(sessions_on(drive) / ulid / name) for name in names}
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)     # the whole listing loses the folder

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    err = capsys.readouterr().err
    assert "其實還在" in err and "稍後重試" in err and "直接 push（不加旗標）或先 pull" in err
    assert on_drive.read_text(encoding="utf-8") == newer
    assert {name: store.md5_file(sessions_on(drive) / ulid / name) for name in names} == before


def test_not_exist_upload_refuses_when_the_folder_is_there_but_empty(
        drive, capsys, monkeypatch):
    """#152 B1: the same Drive failure that drops a folder from the listing can
    answer the directed look with `[]`. That is not proof it is gone (the folder
    is), so it is not a revive either."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)
    monkeypatch.setattr(store.Drive, "list_one", lambda self, _ulid: {})

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    assert "直接查是空的" in capsys.readouterr().err
    assert not (sessions_on(drive) / ulid).exists()


def test_not_exist_upload_goes_ahead_when_the_direct_look_says_rc3(drive, monkeypatch):
    """rclone saying the folder itself is not there (rc=3) is still the one answer
    that means gone, so the revive goes through as before."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    monkeypatch.setenv("FAKE_RCLONE_NOT_FOUND_RC", "3")

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (1, 0)
    assert _uploaded(drive, ulid) == {"session.md", *_segments_on_drive(drive, ulid)}


def test_not_exist_upload_refuses_when_the_direct_look_fails(drive, capsys, monkeypatch):
    """V6: not seeing it is not seeing it gone. A look that fails uploads nothing,
    says to try again, and is a failed entry, so the command exits non-zero."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _lost_on_drive(paths, drive, ulid)
    mark = len(calls(drive))
    monkeypatch.setenv("FAKE_RCLONE_FAIL", f"gdrive:sessions/{ulid}")   # only the directed look

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    err = capsys.readouterr().err
    assert "查不動" in err and "下一次再試" in err
    assert not [c for c in calls(drive)[mark:] if "copyto" in c]
    assert not (sessions_on(drive) / ulid).exists()


# --- #162: a new or emptied Drive ------------------------------------------------------------------


def _config_json(paths) -> Path:
    return paths.config / "config.json"


def _wrong_root(paths) -> None:
    data = json.loads(_config_json(paths).read_text())
    data["folders"] = {name: "BOGUS-ROOT-ID" for name in data["folders"]}
    _config_json(paths).write_text(json.dumps(data))


def _emptied_drive(drive, paths, ulid) -> None:
    """The session is in the mirror; Drive keeps its root folder but has no `sessions/` any more."""
    cache.pull(paths, [ulid], {})
    shutil.rmtree(sessions_on(drive))


def _copies(drive, mark) -> list[str]:
    return [c for c in calls(drive)[mark:] if "copy" in c or "copyto" in c]


def test_not_exist_upload_revives_the_mirror_on_a_new_or_emptied_drive(drive, capsys):
    """#162: the root is there and has no `sessions/`: nothing was deleted by anybody, the mirror is
    all there is. The session goes up (the folder is created) and the person is told first."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    store._note_folder_record(paths, ulid, "an-id-from-before", ["session.md"], lambda *_: None)   # #173
    _emptied_drive(drive, paths, ulid)

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (1, 0)

    err = capsys.readouterr().err
    assert "Drive 上沒有 sessions/，將建立並上傳 1 個" in err
    assert "session.md" in _uploaded(drive, ulid) and len(_uploaded(drive, ulid)) >= 2
    assert not (paths.state / "folder-ids" / f"{ulid}.json").exists(), "#173: the old record is cleared"


def test_the_fresh_drive_question_is_asked_once_for_the_whole_push(drive):
    paths = store.Paths.from_env()
    first, second = _on_drive(paths), _on_drive(paths, body="## user\n第二個\n")
    cache.pull(paths, [first, second], {})
    shutil.rmtree(sessions_on(drive))
    mark = len(calls(drive))

    assert cache.push(paths, [first, second], {}, not_exist_upload=True) == (2, 0)

    root_questions = [c for c in calls(drive)[mark:] if "lsjson" in c and "--dirs-only" in c and "gdrive:" in c]
    assert len(root_questions) == 2, "My Drive (is the root there?) and the root (no sessions/?), once each"


@pytest.mark.parametrize("what", ["root-not-found", "bad-token", "network-error", "root-lookup-lies"])
def test_not_exist_upload_still_refuses_when_the_root_cannot_be_vouched_for(drive, capsys, monkeypatch, what):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _emptied_drive(drive, paths, ulid)
    if what == "root-not-found":
        _wrong_root(paths)
    elif what in ("bad-token", "network-error"):
        real = store.Drive._run

        def failing(self, *args, root=True):
            if args[:1] == ("lsjson",) and "--dirs-only" in args:
                raise store.StoreError("rclone lsjson 失敗（rc=1）：" + (
                    "invalid_grant: Token has been expired or revoked" if what == "bad-token"
                    else "dial tcp: lookup www.googleapis.com: no such host"))
            return real(self, *args, root=root)
        monkeypatch.setattr(store.Drive, "_run", failing)
    else:
        # A wrong root that answers an empty list: the root listing alone would say "fresh Drive".
        # My Drive's own folders do not contain this ID, so it must not pass.
        _wrong_root(paths)
        real = store.Drive._run

        def lies(self, *args, root=True):
            if root and args[:1] == ("lsjson",) and "--dirs-only" in args:
                return "[]"
            return real(self, *args, root=root)
        monkeypatch.setattr(store.Drive, "_run", lies)
    mark = len(calls(drive))

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    err = capsys.readouterr().err
    assert "找不到 sessions/" in err and "這是新的或清空的 Drive 嗎" in err and "所以不傳" in err
    assert not _copies(drive, mark), "nothing was sent anywhere"
    assert not sessions_on(drive).exists()


def test_a_legacy_session_is_still_refused_on_a_fresh_drive(drive, capsys):
    """#169: a session whose raw this version does not read is never written back, new Drive or not."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    md = paths.mirror / ulid / "session.md"
    hdr, body = store.h.split_document(md.read_text(encoding="utf-8"))
    hdr["agora"]["raw"] = {"file": "raw-aaaaaaaaaaaa.json", "md5": "a" * 32, "size": 3}
    md.write_text(store.h.dump_document(hdr, body), encoding="utf-8")
    shutil.rmtree(sessions_on(drive))
    mark = len(calls(drive))

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    assert not _copies(drive, mark) and not sessions_on(drive).exists()


def test_without_the_flag_a_missing_sessions_folder_is_still_just_absent(drive, capsys):
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _emptied_drive(drive, paths, ulid)
    mark = len(calls(drive))

    assert cache.push(paths, [ulid], {}) == (0, 0)

    assert "雲端沒有" in capsys.readouterr().err and not _copies(drive, mark)


def test_not_exist_upload_refuses_when_the_root_does_have_a_sessions_folder(drive, capsys, monkeypatch):
    """The listing said "no `sessions/`" but the root lists one: the listing is what is wrong, so
    this is not a new Drive and nothing may be sent over what is there."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    cache.pull(paths, [ulid], {})
    monkeypatch.setattr(store, "_listing_with_md5", lambda drive_: None)      # what push lists with
    mark = len(calls(drive))

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    assert "root 裡其實有 sessions/" in capsys.readouterr().err
    assert not _copies(drive, mark)


@pytest.mark.parametrize("how", ["sessions-answers-empty", "sessions-answers-files", "look-fails"])
def test_not_exist_upload_refuses_when_only_the_directed_look_finds_sessions(drive, capsys, monkeypatch, how):
    """#162 review: both root questions say "no `sessions/`", but one directed look at `sessions/`
    itself (the same look every other irreversible path asks) finds it - or cannot say. Not a new Drive."""
    paths = store.Paths.from_env()
    ulid = _on_drive(paths)
    _emptied_drive(drive, paths, ulid)
    real = store.Drive.list_one

    def look(self, u):
        if u != "":
            return real(self, u)
        if how == "look-fails":
            raise store.StoreError("rclone lsjson 失敗（rc=1）：500 backend error")
        return {} if how == "sessions-answers-empty" else {"stray.txt": "m"}
    monkeypatch.setattr(store.Drive, "list_one", look)
    mark = len(calls(drive))

    assert cache.push(paths, [ulid], {}, not_exist_upload=True) == (0, 1)

    err = capsys.readouterr().err
    assert "這是新的或清空的 Drive 嗎" in err and "直接查 sessions/" in err
    assert not _copies(drive, mark) and not sessions_on(drive).exists()


def test_the_directed_look_at_sessions_is_part_of_the_one_question_per_push(drive):
    paths = store.Paths.from_env()
    first, second = _on_drive(paths), _on_drive(paths, body="## user\n第二個\n")
    cache.pull(paths, [first, second], {})
    shutil.rmtree(sessions_on(drive))
    mark = len(calls(drive))

    assert cache.push(paths, [first, second], {}, not_exist_upload=True) == (2, 0)

    looks = [c for c in calls(drive)[mark:] if "lsjson" in c and "gdrive:sessions/" in c]
    assert len(looks) == 1, "asked once for the whole push, the answer is remembered"
