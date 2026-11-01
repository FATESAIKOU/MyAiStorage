"""More header/store coverage (code-pm section 2): split errors and U-ST gaps.

Store-level tests over the fake rclone; a second machine is just another
cache/state pair on the same fake remote. Mirrors test_store.py conventions.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import hashlib
import fcntl
import json
import shutil
import sys
from pathlib import Path

import pytest

from agora import header as h
from agora import store

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def machine2(tmp_path):
    paths = store.Paths.from_env()
    other = tmp_path / "m2"
    return store.Paths(config=paths.config, cache=other / "cache",
                       state=other / "state")


def _header(title="CSV 規劃表格", **kw):
    ulid = kw.pop("ulid", None) or h.new_ulid()
    hdr = {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                     "updated_at": "2026-10-02T00:00:00Z",
                     "relation": "import", "parents": [],
                     "source": {"agent": "opencode", "session_id": "ses_test",
                                "created_at": "2026-10-01T00:00:00Z"}}}
    hdr.update(kw)
    return hdr


def T(keyword: str) -> list:
    """A full-text filter, or none for an empty keyword."""
    return [(("text",), "~=", keyword)] if keyword else []


def _save(paths, hdr, body="## user\n把 CSV 轉成 Markdown 表格\n", raw=b'{"x": 1}'):
    store.stage(paths, hdr, body, raw)
    store.upload_batch(store.Drive(paths), paths)
    return hdr["id"].split(":", 1)[1]


def calls(remote):
    log = remote.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def gdrive_targets(call):
    """The gdrive: paths in a logged call (Drive._run prefixes --config etc)."""
    return [a for a in call if isinstance(a, str) and a.startswith("gdrive:")]


# --- header split errors (new, C2) --------------------------------------------

def test_split_document_rejects_broken_yaml():
    with pytest.raises(h.HeaderError):
        h.split_document("---\ntitle: [unclosed\n---\nbody\n")


def test_read_entry_rejects_non_utf8(remote):
    paths = store.Paths.from_env()
    folder = store.stage(paths, _header(), "## user\nCSV\n", b"{}")
    (folder / "session.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(h.HeaderError):
        store.read_entry(folder)


# --- U-ST gaps -----------------------------------------------------------------

def test_no_snapshot_has_a_dangling_raw_pointer(remote):  # U-ST-03, snapshots
    paths = store.Paths.from_env()
    hdr = _header()
    _save(paths, hdr, raw=b'{"v": 1}')
    _save(paths, hdr, raw=b'{"v": 2}')
    snaps = sorted((remote.parent / "snapshots").iterdir())
    assert snaps, "fake rclone should snapshot after every call"
    checked = 0
    for snap in snaps:
        for md in (snap / "agora" / "sessions").glob("*/session.md") if (snap / "agora" / "sessions").exists() else []:
            hdr, _ = h.split_document(md.read_text(encoding="utf-8"))
            raw = h.agora_of(hdr).get("raw")
            if not raw:
                continue
            for seg in raw["segments"]:
                target = md.parent / seg["file"]
                assert target.is_file(), f"{snap.name}: {seg['file']} missing"
                assert hashlib.md5(target.read_bytes()).hexdigest() == seg["md5"]
            checked += 1
    assert checked >= 2


def test_missing_raw_not_indexed_until_added(remote, tmp_path):  # U-ST-04
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV 表格\n", b'{"x": 1}')
    drive = store.Drive(paths)
    drive.upload(folder / "session.md", hdr["id"].split(":", 1)[1], "session.md")
    other = machine2(tmp_path)
    assert store.sync(other).search(T("表格")) == []
    seg = h.agora_of(hdr)["raw"]["segments"][0]["file"]          # one small segment: the raw itself
    (remote / "agora" / "sessions" / hdr["id"].split(":", 1)[1] / seg).write_bytes(
        (folder / store.raw_name(h.agora_of(hdr)["raw"])).read_bytes())
    assert [hit[0] for hit in store.sync(other).search(T("表格"))] == [hdr["id"].split(":", 1)[1]]


def test_session_md_upload_failure_leaves_only_segments(remote, tmp_path, monkeypatch):  # U-ST-07
    paths = store.Paths.from_env()
    hdr = _header()
    store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "session.md")
    ulid = hdr["id"].split(":", 1)[1]
    assert store.upload_batch(store.Drive(paths), paths) == [ulid]
    assert sorted(p.name for p in (remote / "agora" / "sessions" / ulid).iterdir()) == \
        sorted(seg["file"] for seg in h.agora_of(hdr)["raw"]["segments"])
    assert store.sync(machine2(tmp_path)).search(T("CSV")) == []


def test_duplicate_drive_folder_errors(remote, monkeypatch):  # U-ST-16
    monkeypatch.setenv("FAKE_RCLONE_DUP", "agora")
    with pytest.raises(store.StoreError, match="請先處理同名資料夾"):
        store.Drive(store.Paths.from_env()).folder_id()


def test_merge_without_raw_is_indexed(remote, tmp_path):  # U-ST-18, N5
    paths = store.Paths.from_env()
    hdr = _header()
    hdr.pop("source", None)
    store.stage(paths, hdr, "## user\n合併結果表格\n", None)
    store.upload_batch(store.Drive(paths), paths)
    assert "raw" not in h.agora_of(hdr)
    assert store.sync(machine2(tmp_path)).search(T("表格"))


def test_sync_never_downloads_raw(remote, tmp_path):  # U-ST-19, N11
    paths = store.Paths.from_env()
    _save(paths, _header())
    _save(paths, _header())
    before = len(calls(remote))
    store.sync(machine2(tmp_path))
    downloads = [c for c in calls(remote)[before:]
                 if "copyto" in c and any(t.endswith(".json") for t in gdrive_targets(c))]
    assert downloads == []


def _on_drive(remote, ulid):
    return remote / "agora" / "sessions" / ulid


def _raw_of(remote, ulid):
    hdr, _ = h.split_document((_on_drive(remote, ulid) / "session.md").read_text(encoding="utf-8"))
    return hdr["agora"]["raw"]


def _session_downloads(remote, ulid, before: int = 0):
    return [c for c in calls(remote)[before:]
            if "copyto" in c and any(t.endswith(f"sessions/{ulid}/session.md")
                                     for t in gdrive_targets(c))]


def _stuck_session(remote, paths):
    """A session on Drive whose session.md is there and whose raw is not (#119).

    Uploaded without `remember`, like a session another machine wrote: the local
    index does not know it, so the first sync has to read it and find it unfinished.
    Returns (ulid, raw header, raw bytes).
    """
    ulid = _save(paths, _header())
    raw = _raw_of(remote, ulid)
    (seg,) = raw["segments"]                              # a small raw is one segment
    raw_path = _on_drive(remote, ulid) / seg["file"]
    data = raw_path.read_bytes()
    raw_path.unlink()
    return ulid, raw, data


def test_a_stuck_session_is_downloaded_once_not_on_every_command(remote, capsys):
    """#119: the same unfinished session.md and raw listing is remembered locally."""
    paths = store.Paths.from_env()
    ulid, _, _ = _stuck_session(remote, paths)
    capsys.readouterr()

    before = len(calls(remote))
    index = store.sync(paths)
    assert index.header(ulid) is None
    assert len(_session_downloads(remote, ulid, before)) == 1
    assert "還沒寫完" in capsys.readouterr().err

    before = len(calls(remote))
    index = store.sync(paths)                 # the next command
    assert index.header(ulid) is None
    assert _session_downloads(remote, ulid, before) == []
    assert "還沒寫完" not in capsys.readouterr().err
    assert json.loads((paths.state / store.UNFINISHED_FILE).read_text())["sessions"][ulid]


def test_a_skipped_download_still_keeps_the_unfinished_session_out_of_the_mirror(remote, capsys):
    """#119 review N1 (G3): not downloading must not leave the unfinished session.md in the
    mirror - an offline rebuild would turn it into a finished-looking session. Put one there
    the way a stale copy would be (an earlier build of the mirror) and the remembered round
    has to take it out again."""
    paths = store.Paths.from_env()
    ulid, _, _ = _stuck_session(remote, paths)
    store.sync(paths)
    stale = paths.mirror / ulid / "session.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes((_on_drive(remote, ulid) / "session.md").read_bytes())
    before = len(calls(remote))
    store.sync(paths)                         # the remembered round: no download
    assert _session_downloads(remote, ulid, before) == []
    assert not stale.exists(), "the unfinished session.md is out of the mirror again"
    assert store.Index(paths).header(ulid) is None


def test_a_listing_without_sessions_leaves_the_memory_alone(remote, capsys):
    """#119 review N1: Drive having no `sessions/` at all says nothing about what is
    unfinished (like the cloud_missing marks) - the memory file comes out of that round
    byte for byte as it went in."""
    paths = store.Paths.from_env()
    ulid, _, _ = _stuck_session(remote, paths)
    store.sync(paths)
    memory = paths.state / store.UNFINISHED_FILE
    before = memory.read_bytes()
    assert ulid in json.loads(before)["sessions"]
    shutil.rmtree(remote / "agora" / "sessions")
    store.sync(paths)
    assert memory.read_bytes() == before


def test_a_raw_that_arrives_later_is_not_blocked_by_the_memory(remote, capsys):
    """#119: the raw appears with the md5 its header names; the session must be indexed."""
    paths = store.Paths.from_env()
    ulid, raw, data = _stuck_session(remote, paths)
    store.sync(paths)
    capsys.readouterr()
    before = len(calls(remote))
    store.sync(paths)                         # remembered: no download
    assert _session_downloads(remote, ulid, before) == []

    (_on_drive(remote, ulid) / raw["segments"][0]["file"]).write_bytes(data)
    before = len(calls(remote))
    index = store.sync(paths)
    assert [hit[0] for hit in index.search(T("CSV"))] == [ulid]
    assert len(_session_downloads(remote, ulid, before)) == 1
    assert "還沒寫完" not in capsys.readouterr().err
    assert ulid not in json.loads((paths.state / store.UNFINISHED_FILE).read_text())["sessions"]


def test_a_new_session_md_is_evaluated_again(remote, capsys):
    """#119: session.md changed on Drive, so the remembered answer no longer applies."""
    paths = store.Paths.from_env()
    ulid, _, _ = _stuck_session(remote, paths)
    store.sync(paths)
    capsys.readouterr()
    before = len(calls(remote))
    store.sync(paths)
    assert _session_downloads(remote, ulid, before) == []

    path = _on_drive(remote, ulid) / "session.md"
    hdr, body = h.split_document(path.read_text(encoding="utf-8"))
    hdr["title"] = "同一個 raw 還沒到，但標題改了"
    path.write_text(h.dump_document(hdr, body), encoding="utf-8")
    before = len(calls(remote))
    index = store.sync(paths)
    assert index.header(ulid) is None
    assert len(_session_downloads(remote, ulid, before)) == 1
    assert "還沒寫完" in capsys.readouterr().err

    before = len(calls(remote))
    store.sync(paths)
    assert _session_downloads(remote, ulid, before) == []


def test_a_broken_or_deleted_memory_falls_back_to_one_download(remote, capsys):
    """#119: a corrupt or missing state file is no memory, not a skipped session."""
    paths = store.Paths.from_env()
    ulid, _, _ = _stuck_session(remote, paths)
    store.sync(paths)
    capsys.readouterr()
    before = len(calls(remote))
    store.sync(paths)
    assert _session_downloads(remote, ulid, before) == []

    (paths.state / store.UNFINISHED_FILE).write_text("{not json", encoding="utf-8")
    before = len(calls(remote))
    store.sync(paths)
    assert len(_session_downloads(remote, ulid, before)) == 1

    (paths.state / store.UNFINISHED_FILE).unlink()
    before = len(calls(remote))
    store.sync(paths)
    assert len(_session_downloads(remote, ulid, before)) == 1


def test_missing_sessions_dir_keeps_mirror(remote, capsys):  # U-ST-20, N12
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    assert store.sync(paths).search(T("CSV"))
    import shutil
    shutil.rmtree(remote / "agora" / "sessions")
    index = store.sync(paths)
    assert [hit[0] for hit in index.search(T("CSV"))] == [ulid]
    assert (paths.mirror / ulid / "session.md").is_file()
    # No listing is no evidence of a deletion, so no marker moves either (T1 R6, Q4)
    assert index.missing_in_cloud() == []
    assert "標記不動" in capsys.readouterr().err


def test_failed_listing_keeps_mirror(remote, monkeypatch):  # U-ST-20, N12
    paths = store.Paths.from_env()
    _save(paths, _header())
    assert store.sync(paths).search(T("CSV"))
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    assert store.sync(paths).search(T("CSV"))


def test_a_mark_that_is_already_there_survives_a_broken_listing(remote, monkeypatch):
    """L4, spec: 「原本的標記也不變」. Start from a session already marked."""
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    import shutil
    assert store.sync(paths).search(T("CSV"))            # indexed first: it is a local session
    shutil.rmtree(remote / "agora" / "sessions" / ulid)
    assert store.sync(paths).missing_in_cloud() == [ulid]

    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")       # offline
    assert store.sync(paths).missing_in_cloud() == [ulid]
    monkeypatch.delenv("FAKE_RCLONE_FAIL")
    shutil.rmtree(remote / "agora" / "sessions")           # no sessions/ at all
    assert store.sync(paths).missing_in_cloud() == [ulid]
    assert store.sync(paths).cloud_has(ulid) is False


def test_a_session_in_the_outbox_is_not_marked(remote, monkeypatch, capsys):  # L4, Q2
    """Not up yet is not "deleted on another machine", even when the listing works."""
    paths = store.Paths.from_env()
    _save(paths, _header("先讓 Drive 上有 sessions/"))       # F6: without this the listing is
    capsys.readouterr()                                   # None and no marker moves anyway
    folder = store.stage(paths, _header(), "## user\n還沒上傳的內容\n", None)
    ulid = folder.name
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")       # the upload fails, the listing does not
    assert ulid in store.outbox_ulids(paths)
    store.remember(paths, folder)      # it has to be in the index, or there is nothing to mark
    assert ulid in store.Index(paths).known()

    index = store.sync(paths)
    assert index.header(ulid) is not None
    assert index.missing_in_cloud() == []                  # ours, not another machine's delete
    assert ulid in store.outbox_ulids(paths)               # and it really is still staged


def test_a_session_being_continued_is_not_marked(remote):
    """Spec「標記：**不在接續中的**才標」: a continue that is *running* right now is not
    a delete on another machine. G1 is what makes this reachable - a leftover record
    no longer blocks the mark, a held lock still does."""
    paths = store.Paths.from_env()
    ulid = _save(paths, _header())
    assert store.sync(paths).search(T("CSV"))
    import shutil
    shutil.rmtree(remote / "agora" / "sessions" / ulid)
    paths.pending.mkdir(parents=True, exist_ok=True)
    lock = open(paths.pending / f"{ulid}.json", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)              # an agent is working on it

    index = store.sync(paths)
    assert index.missing_in_cloud() == []         # not marked while it is in flight
    assert index.header(ulid) is not None

    lock.close()                                  # the run ends (or gives up)
    assert store.sync(paths).missing_in_cloud() == [ulid]


def test_a_staged_session_the_index_cannot_read_is_not_marked_either(remote, monkeypatch, capsys):
    """F6/G4, with the truth spelled out (review H2): for a session we *can* index, the
    exclusion is a second layer - `_index_outbox` runs after the marking and `Index.put`
    drops the marker again. This test is not that case: a broken session.md is
    quarantined by `push_outbox` before anything is marked, so this one was never
    markable. Kept as a second observation of the rule, not as a mutation guard."""
    paths = store.Paths.from_env()
    _save(paths, _header("先讓 Drive 上有 sessions/"))
    capsys.readouterr()
    folder = store.stage(paths, _header(), "## user\n還沒上傳\n", None)
    (folder / "session.md").write_text("壞掉的 session.md", encoding="utf-8")
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")

    index = store.sync(paths)
    assert index.missing_in_cloud() == []                  # not marked: it is ours


def test_fetch_raw_refetches_session_md(remote, tmp_path):  # U-ST-21, N8
    paths = store.Paths.from_env()
    hdr = _header()
    _save(paths, hdr, raw=b'{"v": 1}')
    other = machine2(tmp_path)
    stale = store.sync(other).header(hdr["id"].split(":", 1)[1])
    _save(paths, hdr, body="## user\n第二版表格\n", raw=b'{"v": 2}')
    # the first version's segment is cleaned up once its grace period is over (design B.5.5);
    # a reader still holding the old header meets a 404 and follows session.md's new manifest
    old_seg = h.agora_of(stale)["raw"]["segments"][0]["file"]
    (remote / "agora" / "sessions" / hdr["id"].split(":", 1)[1] / old_seg).unlink()
    got = store.fetch_raw(other, store.Drive(other), hdr["id"].split(":", 1)[1], stale)
    assert got == b'{"v": 2}'
    fresh, _ = h.split_document((other.mirror / hdr["id"].split(":", 1)[1] / "session.md").read_text())
    assert h.agora_of(fresh)["raw"]["md5"] == hashlib.md5(b'{"v": 2}').hexdigest()


def test_join_segments_keeps_another_processes_staging_raw(remote, tmp_path):  # #227
    """`_join_segments` drops the session's older `raw-*`, and leaves another process's
    `copy_atomic` staging file (`raw-<md5>.json<random>.tmp`) alone: it is that process's
    half-written copy, renamed in when it is done (same rule as `remember`, #227)."""
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = _save(paths, hdr, raw=b'{"v": 1}')
    other = machine2(tmp_path)
    stale = store.sync(other).header(ulid)
    mirror = other.mirror / ulid
    final = store.raw_name(h.agora_of(stale)["raw"])
    staging = mirror / f"{final}4f2a9c1b.tmp"                 # where copy_atomic stages its copy
    staging.write_bytes(b'half')
    old = mirror / "raw-0123456789ab.json"                    # what a new header replaced
    old.write_bytes(b'{"old": true}')

    got = store.fetch_raw(other, store.Drive(other), ulid, stale)

    assert got == b'{"v": 1}'
    assert (mirror / final).read_bytes() == got
    assert staging.read_bytes() == b'half'                     # still being written elsewhere
    assert not old.exists()                                   # the older raw is cleaned up


def test_broken_outbox_entry_quarantined(remote, capsys):  # new, C2/C4
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", b"{}")
    (folder / "session.md").write_text("---\ntitle: [unclosed\n---\nbody\n")
    store.sync(paths)
    assert (paths.outbox / ".bad" / hdr["id"].split(":", 1)[1]).is_dir()
    assert "壞了" in capsys.readouterr().err
