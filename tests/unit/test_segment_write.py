"""The write side of the segmented raw (#169, #109 R3b-1): what `stage` puts in the outbox, what
`upload_batch` and `push` send, how every segment is verified, how old segments are cleaned,
the entries staged before the upgrade, and the crash points.

Everything runs on the fake rclone; `AGORA_RAW_CHUNK=4096` makes a few KiB several segments.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agora import cache, cli, header as h, store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
SRC = Path(__file__).resolve().parent.parent.parent / "src"
C = 4096


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
    monkeypatch.setenv("AGORA_RAW_CHUNK", str(C))
    (tmp_path / "config").mkdir()
    return remote


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _header(title="分段寫入", ulid=None) -> dict:
    ulid = ulid or h.new_ulid()
    return {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [],
            "case": None,
            "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                      "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                      "source": {"agent": "opencode", "session_id": "ses_x",
                                 "created_at": "2026-10-01T00:00:00Z"}}}


def data_of(n_segments: int, tail: int = 100, tag: int = 0) -> bytes:
    return b"".join(bytes([65 + (i + tag) % 26]) * C for i in range(n_segments)) + b"z" * tail


def _stage(paths, raw: bytes, title="分段寫入", ulid=None, *, update=False, kind="") -> tuple[str, dict]:
    hdr = _header(title, ulid)
    folder = store.stage(paths, hdr, f"## user\n{title}\n", raw, update=update, kind=kind)
    store.remember(paths, folder)
    return folder.name, hdr["agora"]["raw"]


def _drive_folder(env: Path, ulid: str) -> Path:
    return env / "agora" / "sessions" / ulid


def _drive_names(env: Path, ulid: str) -> set[str]:
    folder = _drive_folder(env, ulid)
    return {p.name for p in folder.iterdir()} if folder.is_dir() else set()


def _log(env: Path, name: str) -> list[dict]:
    path = env.parent / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _sent(env: Path, since: int = 0) -> list[str]:
    """Every file name a `copy --files-from` carried since `since`, in order."""
    return [f["name"].split("/", 1)[1] for row in _log(env, "copies.log")[since:] for f in row["files"]]


def _mark(env: Path) -> int:
    return len(_log(env, "copies.log"))


def _manifest(env: Path, ulid: str) -> list[dict]:
    hdr, _ = h.split_document((_drive_folder(env, ulid) / "session.md").read_text(encoding="utf-8"))
    return hdr["agora"]["raw"]["segments"]


def _joined(env: Path, ulid: str) -> bytes:
    return b"".join((_drive_folder(env, ulid) / s["file"]).read_bytes() for s in _manifest(env, ulid))


# --- 3.3: stage --------------------------------------------------------------------------------


def test_stage_cuts_the_manifest_in_the_pass_that_stages_the_raw(env):
    paths = store.Paths.from_env()
    data = data_of(2, tail=1808)                              # 4096 + 4096 + 1808
    ulid, raw = _stage(paths, data)

    assert raw["md5"] == md5(data) and raw["size"] == len(data) and raw["layout"] == "chunks/1"
    assert "file" not in raw
    segs = raw["segments"]
    assert [s["size"] for s in segs] == [C, C, 1808]
    assert [s["md5"] for s in segs] == [md5(data[:C]), md5(data[C:2 * C]), md5(data[2 * C:])]
    assert [s["file"] for s in segs] == [
        f"seg-0001-{i:04d}-{s['md5'][:12]}.part" for i, s in enumerate(segs)]
    # the outbox still holds only session.md and the one whole raw - segments are cut at upload
    assert sorted(p.name for p in (paths.outbox / ulid).iterdir()) == sorted(
        ["session.md", f"raw-{md5(data)[:12]}.json"])


@pytest.mark.parametrize("data,sizes", [(b"", [0]), (b"x", [1]), (b"y" * C, [C]),
                                        (b"y" * (C + 1), [C, 1])])
def test_the_edges_of_the_cut(env, data, sizes):
    paths = store.Paths.from_env()
    _, raw = _stage(paths, data)
    assert [s["size"] for s in raw["segments"]] == sizes
    assert store.raw_kind(raw) == "chunks"


def test_a_changed_raw_is_the_next_generation_and_an_unchanged_one_keeps_its_manifest(env, monkeypatch):
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    store.stage(paths, hdr, "## user\n一\n", data_of(1))
    first = json.loads(json.dumps(hdr["agora"]["raw"]))
    assert first["segments"][0]["file"].startswith("seg-0001-")

    again = _header(ulid=ulid)
    store.stage(paths, again, "## user\n二（改標題）\n", data_of(1))            # same bytes: an edit
    assert again["agora"]["raw"] == first, "the same raw keeps the very same manifest"

    monkeypatch.setenv("AGORA_RAW_CHUNK", str(C * 2))                        # even if C changed since
    same = _header(ulid=ulid)
    store.stage(paths, same, "## user\n三\n", data_of(1))
    assert same["agora"]["raw"] == first

    changed = _header(ulid=ulid)
    store.stage(paths, changed, "## user\n四\n", data_of(1, tag=5))
    assert all(s["file"].startswith("seg-0002-") for s in changed["agora"]["raw"]["segments"])
    assert changed["agora"]["raw"]["md5"] != first["md5"]


def test_stage_reads_the_previous_manifest_from_the_mirror_when_the_outbox_is_empty(env):
    paths = store.Paths.from_env()
    ulid, first = _stage(paths, data_of(1))
    store.upload_batch(store.Drive(paths), paths)                             # leaves the outbox
    assert not (paths.outbox / ulid).exists()
    edit = _header(ulid=ulid)
    store.stage(paths, edit, "## user\n只改標題\n", data_of(1))
    assert edit["agora"]["raw"] == first


# --- 3.8 / 3.9 / 3.11: upload_batch --------------------------------------------------------------


def test_a_new_session_sends_every_segment_then_session_md(env):
    paths = store.Paths.from_env()
    data = data_of(3)
    ulid, raw = _stage(paths, data)
    mark = _mark(env)

    assert store.upload_batch(store.Drive(paths), paths) == []

    sent = _sent(env, mark)
    assert sent == [s["file"] for s in raw["segments"]] + ["session.md"]
    assert _drive_names(env, ulid) == {"session.md", *[s["file"] for s in raw["segments"]]}, \
        "Drive has no whole raw"
    assert _joined(env, ulid) == data
    assert not (paths.outbox / ulid).exists() and not list(paths.state.glob("batch-*"))


def test_an_update_sends_only_the_segments_drive_lacks(env):
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(3))
    store.upload_batch(store.Drive(paths), paths)
    lost = raw["segments"][1]["file"]
    (_drive_folder(env, ulid) / lost).unlink()                               # cleaned by someone
    edit = _header("只改了標題", ulid=ulid)
    folder = store.stage(paths, edit, "## user\n只改了標題\n", data_of(3), update=True, kind="edit")
    store.remember(paths, folder)
    mark = _mark(env)

    assert store.upload_batch(store.Drive(paths), paths) == []

    assert _sent(env, mark) == [lost, "session.md"], "the segments Drive has are not sent again"
    assert _joined(env, ulid) == data_of(3)


def test_a_segment_that_does_not_cut_to_its_manifest_is_not_published(env, capsys):
    """B.5.1: a whole raw that disagrees with the manifest in its own header is never sent."""
    paths = store.Paths.from_env()
    data = data_of(2)
    ulid, raw = _stage(paths, data)
    whole = paths.outbox / ulid / f"raw-{md5(data)[:12]}.json"
    damaged = bytearray(whole.read_bytes())
    damaged[C + 10] ^= 0xFF                                                    # same size, other bytes
    whole.write_bytes(bytes(damaged))
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert any(f"{ulid} 切段失敗" in s and "md5" in s for s in said), said
    assert not _drive_folder(env, ulid).exists(), "nothing of a version that disagrees with itself"
    assert ulid in store.outbox_ulids(paths)


def test_one_bad_entry_does_not_stop_the_batch(env):
    paths = store.Paths.from_env()
    bad, _ = _stage(paths, data_of(2), "壞的")
    good, _ = _stage(paths, data_of(1), "好的")
    whole = next((paths.outbox / bad).glob("raw-*.json"))
    whole.write_bytes(b"\x00" * whole.stat().st_size)

    assert store.upload_batch(store.Drive(paths), paths) == [bad]

    assert _joined(env, good) == data_of(1) and not _drive_folder(env, bad).exists()


def test_the_confirmation_checks_every_segment_not_just_session_md(env, monkeypatch):
    """3.9: session.md is up and matches, but the listing right after does not show one
    segment: the version is not whole, so it stays in the outbox; the next round sends just
    that segment."""
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(3))
    victim = raw["segments"][2]["file"]
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FILES", victim)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert ulid in store.outbox_ulids(paths)
    assert any("段在 Drive 上缺或 md5 不符" in s for s in said), said
    monkeypatch.delenv("FAKE_RCLONE_OMIT_FILES")
    mark = _mark(env)
    # not an update (Drive never confirmed it) so the whole set goes again - idempotent, and
    # now every segment shows up
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert _joined(env, ulid) == data_of(3) and _sent(env, mark)[-1] == "session.md"


def test_a_failed_segment_copy_sends_no_session_md(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(2))
    monkeypatch.setenv("FAKE_RCLONE_FAIL", raw["segments"][1]["file"])
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == [ulid]

    assert "session.md" not in _drive_names(env, ulid)
    assert any("不傳 session.md" in s for s in said), said


def test_segments_are_stamped_with_the_current_time_before_they_go_up(env, monkeypatch):
    """3.11 / K.1: rclone gives Drive the file's mtime as its ModTime, and the orphan grace is
    counted from it. A segment that carries an old mtime (a hard link, a pre-cut file) would
    be 'older than the grace period' the moment it lands - so every file in the batch dir is
    `os.utime`d to now first."""
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(2))
    real_cut = store._cut_segments

    def old_files(raw_path, into, manifest, needed):
        real_cut(raw_path, into, manifest, needed)
        for p in into.iterdir():
            os.utime(p, (946684800, 946684800))                           # the year 2000

    monkeypatch.setattr(store, "_cut_segments", old_files)
    before = time.time() - 5

    assert store.upload_batch(store.Drive(paths), paths) == []

    carried = [f for row in _log(env, "copies.log") for f in row["files"]]
    assert carried and all(f["mtime"] >= before for f in carried), carried
    assert all((_drive_folder(env, ulid) / n).stat().st_mtime >= before
               for n in _drive_names(env, ulid))


def test_the_fake_keeps_the_source_mtime_and_reports_it_as_modtime(env):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    store.upload_batch(store.Drive(paths), paths)
    seg = next(_drive_folder(env, ulid).glob("seg-*"))
    os.utime(seg, (1_000_000_000, 1_000_000_000))
    listing = store.Drive(paths).list_sessions_with_ids()
    assert listing.mtimes[ulid][seg.name].startswith("2001-09-09T01:46:40")


def _pushed_session(env, paths, n=2):
    """A session on Drive whose whole raw is in the mirror too, ready for `push`."""
    ulid, raw = _stage(paths, data_of(n))
    store.upload_batch(store.Drive(paths), paths)
    cache.pull(paths, [ulid], {})
    return ulid, raw


def test_push_stamps_the_segments_it_sends_with_the_current_time(env, monkeypatch):
    """3.17 + 3.11: `push` goes through the same batch dir, so the same `os.utime`."""
    paths = store.Paths.from_env()
    ulid, raw = _pushed_session(env, paths)
    lost = raw["segments"][0]["file"]
    (_drive_folder(env, ulid) / lost).unlink()
    real_cut = store._cut_segments

    def old_files(raw_path, into, manifest, needed):
        real_cut(raw_path, into, manifest, needed)
        for p in into.iterdir():
            os.utime(p, (946684800, 946684800))

    monkeypatch.setattr(store, "_cut_segments", old_files)
    before, mark = time.time() - 5, _mark(env)

    assert cache.push(paths, [ulid], {}) == (1, 0)

    carried = [f for row in _log(env, "copies.log")[mark:] for f in row["files"]]
    assert {f["name"].split("/", 1)[1] for f in carried} == {lost, "session.md"}
    assert all(f["mtime"] >= before for f in carried), carried


def test_push_checks_session_md_and_every_segment_after_sending(env, monkeypatch):
    """3.19: Drive's own md5s decide. The listing right after the copies does not show a
    segment with its md5: the push is a failure, not a success."""
    paths = store.Paths.from_env()
    ulid, raw = _pushed_session(env, paths)
    (_drive_folder(env, ulid) / raw["segments"][1]["file"]).unlink()
    real, calls_to = store.Drive.list_one, {"n": 0}

    def lies_the_second_time(self, u):
        calls_to["n"] += 1
        got = real(self, u)
        if calls_to["n"] >= 2:
            got = {k: v for k, v in got.items() if k != raw["segments"][1]["file"]}
        return got

    monkeypatch.setattr(store.Drive, "list_one", lies_the_second_time)

    assert cache.push(paths, [ulid], {}) == (0, 1)


# --- R2's deletion detection across segments ----------------------------------------------------


def test_ours_is_the_session_md_and_every_segment(env, monkeypatch):
    """The folder was purged before our first copy and the copies re-created it holding our
    segments. Those are `ours`, so the recreated folder is recognised, taken down again and
    the work kept as a session of its own - not mistaken for somebody else's live folder."""
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    edit = _header("改過了", ulid=ulid)
    folder = store.stage(paths, edit, "## user\n改過了\n", data_of(2, tag=3), update=True, kind="continue")
    store.remember(paths, folder)
    real_copy, state = store._copy_batch, {"n": 0}

    def purge_first(drive, paths_, names, **kw):
        state["n"] += 1
        if state["n"] == 1:
            import shutil
            shutil.rmtree(_drive_folder(env, ulid))                         # purged before our copy
        return real_copy(drive, paths_, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", purge_first)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert any("copy 期間被別台刪掉" in s for s in said), said
    assert not _drive_folder(env, ulid).exists(), "the purged session is not brought back"
    (new,) = store.outbox_ulids(paths)
    assert new != ulid and left == [new]
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert _joined(env, new) == data_of(2, tag=3), "the new session is segmented too"


def test_a_rescued_session_is_staged_from_the_raw_file_not_its_bytes(env, monkeypatch):
    """3.18: Y is staged by path (an 800 MB raw is not read into the uploader)."""
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    store.upload_batch(store.Drive(paths), paths)
    folder = store.stage(paths, _header("改", ulid=ulid), "## user\n改\n", data_of(1, tag=2),
                         update=True, kind="continue")
    store.remember(paths, folder)
    import shutil
    shutil.rmtree(_drive_folder(env, ulid))                                  # another machine deleted it
    seen = []
    real_stage = store.stage
    monkeypatch.setattr(store, "stage", lambda p, hdr, body, raw, **kw: (
        seen.append(type(raw)), real_stage(p, hdr, body, raw, **kw))[1])

    store.upload_batch(store.Drive(paths), paths)

    assert len(seen) == 1 and issubclass(seen[0], Path), seen
    (new,) = [n for n in (env / "agora" / "sessions").iterdir() if n.name != ulid]
    assert _joined(env, new.name) == data_of(1, tag=2), "Y is segmented and went up with the rest"


# --- 3.10 / 3.25: cleanup ----------------------------------------------------------------------


def _age(env: Path, ulid: str, names, seconds: float) -> None:
    for name in names:
        path = _drive_folder(env, ulid) / name
        old = time.time() - seconds
        os.utime(path, (old, old))


def _orphan(env: Path, ulid: str, name: str, age: float, content=b"o") -> None:
    path = _drive_folder(env, ulid) / name
    path.write_bytes(content)
    old = time.time() - age
    os.utime(path, (old, old))


def _update(paths, env, ulid, data, title="新版"):
    hdr = _header(title, ulid=ulid)
    folder = store.stage(paths, hdr, f"## user\n{title}\n", data, update=True, kind="continue")
    store.remember(paths, folder)
    return hdr["agora"]["raw"]


def test_cleanup_keeps_the_manifest_the_previous_one_and_anything_inside_the_grace_period(env):
    paths = store.Paths.from_env()
    ulid, v1 = _stage(paths, data_of(2, tag=0))
    store.upload_batch(store.Drive(paths), paths)                          # v1 published
    v1_names = [s["file"] for s in v1["segments"]]
    _age(env, ulid, v1_names, 7200)                                        # older than the grace (3600 s)
    _orphan(env, ulid, "seg-0099-0000-aaaaaaaaaaaa.part", 7200)           # an old orphan
    _orphan(env, ulid, "seg-0098-0000-bbbbbbbbbbbb.part", 60)             # another machine's, just sent
    v2 = _update(paths, env, ulid, data_of(2, tag=7))

    assert store.upload_batch(store.Drive(paths), paths) == []

    names = _drive_names(env, ulid)
    v2_names = {s["file"] for s in v2["segments"]}
    assert v2_names <= names, "the manifest just published"
    assert set(v1_names) <= names, "the previous manifest, recorded in state/published and matching Drive"
    assert "seg-0098-0000-bbbbbbbbbbbb.part" in names, "inside the grace period: not ours to delete"
    assert "seg-0099-0000-aaaaaaaaaaaa.part" not in names, "an orphan past the grace period goes"


def test_cleanup_does_not_trust_a_record_that_no_longer_matches_drive(env):
    paths = store.Paths.from_env()
    ulid, v1 = _stage(paths, data_of(2, tag=0))
    store.upload_batch(store.Drive(paths), paths)
    v1_names = [s["file"] for s in v1["segments"]]
    _age(env, ulid, v1_names, 7200)
    # another machine published in between: Drive's session.md is not the one we recorded
    hdr, body = h.split_document((_drive_folder(env, ulid) / "session.md").read_text(encoding="utf-8"))
    (_drive_folder(env, ulid) / "session.md").write_text(h.dump_document(hdr, body + "\n別台寫的\n"), encoding="utf-8")
    _update(paths, env, ulid, data_of(2, tag=7))

    assert store.upload_batch(store.Drive(paths), paths) == []

    assert not set(v1_names) & _drive_names(env, ulid), "no prev: only the grace period protected them"


def test_three_offline_generations_keep_the_published_one(env):
    """3.25: stage v2 and v3 offline; Drive still has v1. What was published is v1, and that
    record matches Drive's session.md - so v1's segments survive v3's cleanup."""
    paths = store.Paths.from_env()
    ulid, v1 = _stage(paths, data_of(2, tag=0))
    store.upload_batch(store.Drive(paths), paths)
    v1_names = [s["file"] for s in v1["segments"]]
    _age(env, ulid, v1_names, 7200)
    _update(paths, env, ulid, data_of(2, tag=3), "第二代")
    v3 = _update(paths, env, ulid, data_of(2, tag=9), "第三代")

    assert store.upload_batch(store.Drive(paths), paths) == []

    names = _drive_names(env, ulid)
    assert {s["file"] for s in v3["segments"]} <= names and set(v1_names) <= names
    assert names == {"session.md", *[seg["file"] for seg in v3["segments"]], *v1_names}, \
        "exactly: session.md, the manifest just published, and the recorded previous one"


def test_nothing_is_cleaned_before_the_version_is_confirmed(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, v1 = _stage(paths, data_of(2, tag=0))
    store.upload_batch(store.Drive(paths), paths)
    _age(env, ulid, [s["file"] for s in v1["segments"]], 7200)
    _orphan(env, ulid, "seg-0099-0000-aaaaaaaaaaaa.part", 7200)
    v2 = _update(paths, env, ulid, data_of(2, tag=7))
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FILES", v2["segments"][0]["file"])      # not whole on Drive

    assert store.upload_batch(store.Drive(paths), paths) == [ulid]

    assert "seg-0099-0000-aaaaaaaaaaaa.part" in _drive_names(env, ulid)


def test_cleanup_never_touches_a_whole_raw_a_legacy_session_still_has(env):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    store.upload_batch(store.Drive(paths), paths)
    _orphan(env, ulid, "raw-000000000000.json", 99999, b"legacy raw")
    _update(paths, env, ulid, data_of(1, tag=4))
    store.upload_batch(store.Drive(paths), paths)
    assert (_drive_folder(env, ulid) / "raw-000000000000.json").is_file()


def test_the_published_record_is_written_after_a_confirmed_round(env):
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    record = json.loads((paths.state / "published" / f"{ulid}.json").read_text(encoding="utf-8"))
    assert record["session_md5"] == md5((_drive_folder(env, ulid) / "session.md").read_bytes())
    assert record["segments"] == [s["file"] for s in raw["segments"]]


# --- 3.31: entries staged before the upgrade ------------------------------------------------------


def _legacy_entry(paths, title="升級前的版本") -> str:
    """What the old `stage` left in the outbox: no `layout`, a `file`, one whole raw."""
    hdr = _header(title)
    data = b'{"old": true}'
    name = f"raw-{md5(data)[:12]}.json"
    hdr["agora"]["raw"] = {"file": name, "md5": md5(data), "size": len(data)}
    ulid = hdr["id"].split(":", 1)[1]
    folder = paths.outbox / ulid
    folder.mkdir(parents=True)
    (folder / name).write_bytes(data)
    (folder / "session.md").write_text(h.dump_document(hdr, f"## user\n{title}\n"), encoding="utf-8")
    return ulid


def test_a_pre_upgrade_entry_is_not_published_and_the_rest_of_the_batch_goes(env):
    paths = store.Paths.from_env()
    old = _legacy_entry(paths)
    good, _ = _stage(paths, data_of(2), "正常的")
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [old]
    assert old in store.outbox_ulids(paths) and not _drive_folder(env, old).exists()
    assert _joined(env, good) == data_of(2), "the other entry is sent as usual"
    assert sum(1 for s in said if old in s) == 1


def test_the_warning_for_a_pre_upgrade_entry_is_said_once_ever(env):
    paths = store.Paths.from_env()
    old = _legacy_entry(paths)
    first: list[str] = []
    store.upload_batch(store.Drive(paths), paths, warn=first.append)
    assert len([s for s in first if old in s]) == 1
    for _ in range(2):                                                    # the same round again, the next round
        again: list[str] = []
        assert store.upload_batch(store.Drive(paths), paths, warn=again.append) == [old]
        assert not [s for s in again if old in s], again
    assert (paths.state / "legacy-warned").read_text(encoding="utf-8").split() == [old], "ids only"


def test_the_background_leaves_the_pre_upgrade_warning_for_the_next_command(env):
    paths = store.Paths.from_env()
    old = _legacy_entry(paths)
    store.upload_batch(store.Drive(paths), paths, warn=lambda s: None, notices=True)
    notices = store.take_notices(paths)
    assert len(notices) == 1 and old in notices[0]


def test_deleting_the_session_clears_a_pre_upgrade_entry_and_its_set_aside_copy(env, capsys):
    paths = store.Paths.from_env()
    old = _legacy_entry(paths)
    store.upload_batch(store.Drive(paths), paths, warn=lambda s: None)
    (paths.outbox / f".done-{old}").mkdir()
    (paths.outbox / f".done-{old}" / "session.md").write_text("x", encoding="utf-8")

    code = cli.main(["delete", "session", f"agora:{old}", "--yes"])
    capsys.readouterr()

    # Exit 3, always: with a `.done-` copy beside the entry an uploader may be comparing this very id,
    # so `never_confirmed` cannot say "never" and the purge that finds no `sessions/` waits in the
    # queue (#159). The local side is cleared by `agora delete` itself, which is what is asserted below.
    assert code == 3
    assert not (paths.outbox / old).exists() and not (paths.outbox / f".done-{old}").exists()


# --- 3.22: crash points ------------------------------------------------------------------------------

_CHILD = """
import json, sys
from pathlib import Path
from agora import store
paths = store.Paths.from_env()
hdr = json.loads(Path(sys.argv[1]).read_text())
data = bytes.fromhex(sys.argv[2])
folder = store.stage(paths, hdr, "## user\\n第二版\\n", data, update=True, kind="continue")
store.remember(paths, folder)
store.upload_batch(store.Drive(paths), paths)
"""


@pytest.mark.parametrize("point,session_md_is_v2", [
    ("after-manifest", False),
    ("after-segment-cut", False),
    ("after-segment-upload", False),
    ("after-session-upload", True),
    ("after-confirm", True),
    ("before-cleanup", True),
    ("after-cleanup", True),
])
def test_a_crash_at_every_point_leaves_the_old_version_whole_and_the_next_round_converges(
        env, tmp_path, point, session_md_is_v2):
    """I1-I5: v1 is on Drive. A second process stages v2 and uploads it, and dies (exit 137)
    at `point`. Whatever Drive holds is a complete version at every moment; the next round
    finishes the job; the outbox ends empty."""
    paths = store.Paths.from_env()
    ulid, v1 = _stage(paths, data_of(2, tag=0))
    store.upload_batch(store.Drive(paths), paths)
    v1_md5 = md5((_drive_folder(env, ulid) / "session.md").read_bytes())
    hdr = _header("第二版", ulid=ulid)
    hdr_file = tmp_path / "hdr.json"
    hdr_file.write_text(json.dumps(hdr), encoding="utf-8")
    v2_data = data_of(3, tag=11)

    child_env = {**os.environ, "AGORA_TEST_FAULT": point, "PYTHONPATH": str(SRC)}
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(hdr_file), v2_data.hex()],
                          env=child_env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137, (point, proc.returncode, proc.stderr[-400:])

    # whatever is on Drive right now is a whole version: its session.md names only segments that are there
    text = (_drive_folder(env, ulid) / "session.md").read_text(encoding="utf-8")
    on_drive, _ = h.split_document(text)
    for seg in on_drive["agora"]["raw"]["segments"]:
        assert store.md5_file(_drive_folder(env, ulid) / seg["file"]) == seg["md5"], (point, seg)
    if session_md_is_v2:
        assert md5(text.encode()) != v1_md5 and "第二版" in text
    else:
        assert md5(text.encode()) == v1_md5, "a crash before session.md went up leaves v1 as it was"

    # the next round converges
    left = store.upload_batch(store.Drive(paths), paths)
    assert left == [] or point == "after-manifest"
    if point == "after-manifest":
        assert md5((_drive_folder(env, ulid) / "session.md").read_bytes()) == v1_md5
        return
    assert _joined(env, ulid) == v2_data
    assert ulid not in store.outbox_ulids(paths)
    assert not list(paths.state.glob("batch-*")) or point in ("after-segment-cut",)
    _consistent(env)


def _consistent(env: Path) -> None:
    sessions = env / "agora" / "sessions"
    for folder in sorted(p for p in sessions.iterdir() if p.is_dir()):
        hdr, _ = h.split_document((folder / "session.md").read_text(encoding="utf-8"))
        for seg in (hdr["agora"].get("raw") or {}).get("segments", []):
            assert store.md5_file(folder / seg["file"]) == seg["md5"]


def test_the_stage_after_manifest_crash_leaves_no_half_outbox_entry(env, tmp_path):
    paths = store.Paths.from_env()
    hdr = _header("沒進 outbox", ulid=None)
    ulid = hdr["id"].split(":", 1)[1]
    hdr_file = tmp_path / "hdr.json"
    hdr_file.write_text(json.dumps(hdr), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(hdr_file), data_of(2).hex()],
        env={**os.environ, "AGORA_TEST_FAULT": "after-manifest", "PYTHONPATH": str(SRC)},
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137
    assert not (paths.outbox / ulid).exists(), "no entry without a session.md"
    assert not store.outbox_ulids(paths)


# --- round 2 of the review (#169) ----------------------------------------------------------------


def _tamper_last_segment_name(paths, ulid: str, new_name: str) -> None:
    """Rewrite the outbox entry's manifest so its last segment carries `new_name`."""
    path = paths.outbox / ulid / "session.md"
    hdr, body = h.split_document(path.read_text(encoding="utf-8"))
    hdr["agora"]["raw"]["segments"][-1]["file"] = new_name
    path.write_text(h.dump_document(hdr, body), encoding="utf-8")


@pytest.mark.parametrize("name", ["session.md", "SESSION.MD", "Session.md", "seg-0001-0000-000000000000.part",
                                  "seg-1-0-aaaaaaaaaaaa.part", "seg-0001-0001-ABCDEF012345.PART", "notes.txt"])
def test_a_segment_name_outside_the_a1_pattern_is_not_a_chunked_manifest(env, name):
    """Blocker 1: a segment may not be called `session.md` (it would overwrite the completion
    marker in the batch dir and on Drive), nor anything but `seg-<gen>-<seq>-<md5[:12]>.part`
    whose md5 prefix is the segment's own. Such a header is corrupt: legacy, never published,
    never deleted."""
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(1))
    store.upload_batch(store.Drive(paths), paths)
    good_session_md = (_drive_folder(env, ulid) / "session.md").read_bytes()
    edit = _header("v2", ulid=ulid)
    folder = store.stage(paths, edit, "## user\nv2\n", data_of(2, tag=3), update=True, kind="continue")
    _tamper_last_segment_name(paths, ulid, name)
    tampered = h.agora_of(store.read_entry(paths.outbox / ulid))["raw"]
    assert store.raw_kind(tampered) == "legacy"

    store.upload_batch(store.Drive(paths), paths, warn=lambda s: None)

    assert (_drive_folder(env, ulid) / "session.md").read_bytes() == good_session_md, \
        "the completion marker on Drive is untouched"
    assert ulid in store.outbox_ulids(paths)


def test_two_segments_with_the_same_name_are_not_a_manifest():
    raw = {"md5": "a" * 32, "size": 8, "layout": "chunks/1", "segments": [
        {"file": f"seg-0001-0000-{md5(b'x')[:12]}.part", "size": 4, "md5": md5(b"x")},
        {"file": f"seg-0001-0000-{md5(b'x')[:12]}.part", "size": 4, "md5": md5(b"x")}]}
    assert store.raw_kind(raw) == "legacy"


def test_the_cutter_itself_refuses_a_reserved_name(tmp_path):
    whole = tmp_path / "raw.json"
    whole.write_bytes(b"tail")
    into = tmp_path / "batch"
    into.mkdir()
    (into / "session.md").write_text("marker", encoding="utf-8")
    manifest = [{"file": "session.md", "size": 4, "md5": md5(b"tail")}]
    with pytest.raises(ValueError):
        store._cut_segments(whole, into, manifest, {"session.md"})
    assert (into / "session.md").read_text(encoding="utf-8") == "marker"


def test_a_header_only_update_purged_and_crashed_is_not_revived(env, tmp_path):
    """Blocker 2: the version only changes the title (same raw, same manifest), so no segment
    needs sending - and session.md used to be the first copy after another machine's purge.
    A crash right after it left a folder with a session.md naming segments that are not there,
    and the next round completed it: the deleted session came back. Sending one segment first
    would not help (the crash can come after session.md too), so the folder ID this entry was
    aimed at is written to disk before the copies and compared by the next round: a different
    ID is the purge, and the next round rescues instead of reviving (#166's direction)."""
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(3))
    store.upload_batch(store.Drive(paths), paths)
    hdr = _header("只改標題", ulid=ulid)
    hdr_file = tmp_path / "hdr.json"
    hdr_file.write_text(json.dumps(hdr), encoding="utf-8")
    trigger = env.parent / "purge-first-copy.json"
    trigger.write_text(json.dumps({"after": "NEVER", "purge": f"sessions/{ulid}"}), encoding="utf-8")
    import shutil

    def run_child(point: str):
        return subprocess.run(
            [sys.executable, "-c", _CHILD_PURGING, str(hdr_file), data_of(3).hex(), str(_drive_folder(env, ulid))],
            env={**os.environ, "AGORA_TEST_FAULT": point, "PYTHONPATH": str(SRC)},
            capture_output=True, text=True, timeout=120)

    proc = run_child("after-session-upload")
    assert proc.returncode == 137, proc.stderr[-400:]
    folder = _drive_folder(env, ulid)
    assert (paths.state / "folder-ids" / f"{ulid}.json").is_file(), "the pre-send folder ID is on disk"

    left = store.upload_batch(store.Drive(paths), paths)

    assert not (folder / "session.md").exists(), "the purged session did not come back"
    new = [p.name for p in (env / "agora" / "sessions").iterdir() if p.name != ulid]
    assert len(new) == 1, "the work was kept as a session of its own"
    assert _joined(env, new[0]) == data_of(3)


_CHILD_PURGING = """
import json, shutil, sys
from pathlib import Path
from agora import store
paths = store.Paths.from_env()
hdr = json.loads(Path(sys.argv[1]).read_text())
data = bytes.fromhex(sys.argv[2])
victim = Path(sys.argv[3])
folder = store.stage(paths, hdr, "## user\\n只改標題\\n", data, update=True, kind="edit")
store.remember(paths, folder)
real = store._copy_batch
state = {"n": 0}
def purge_before_first_copy(drive, p, names, **kw):
    state["n"] += 1
    if state["n"] == 1:
        shutil.rmtree(victim)            # another machine purges right before our first copy
    return real(drive, p, names, **kw)
store._copy_batch = purge_before_first_copy
store.upload_batch(store.Drive(paths), paths)
"""


def test_push_publishes_the_session_md_it_read_not_a_newer_one(env, monkeypatch):
    """Blocker 3: `push` reads session.md once and takes the manifest, the batch and the
    verification from those bytes. A `stage` landing in the middle can no longer make it
    report success while Drive holds a session.md whose segments are not there."""
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(3))
    store.upload_batch(store.Drive(paths), paths)
    cache.pull(paths, [ulid], {})
    mirror_md = paths.mirror / ulid / "session.md"                         # something to send:
    hdr, body = h.split_document(mirror_md.read_text(encoding="utf-8"))   # a local edit of the body
    mirror_md.write_text(h.dump_document(hdr, body + "\n本機改的\n"), encoding="utf-8")
    real_list, hit = store.Drive.list_one, {"n": 0}

    def stage_during(self, u):
        hit["n"] += 1
        if hit["n"] == 1:
            folder = store.stage(paths, _header("第二版", ulid=ulid), "## user\n第二版\n",
                                 data_of(3, tag=9), update=True, kind="continue")
            store.remember(paths, folder)
        return real_list(self, u)

    monkeypatch.setattr(store.Drive, "list_one", stage_during)

    done, failed = cache.push(paths, [ulid], {})

    assert (done, failed) == (1, 0), "the version it read went up whole, so it succeeded"
    text = (_drive_folder(env, ulid) / "session.md").read_text(encoding="utf-8")
    on_drive, _ = h.split_document(text)
    for seg in on_drive["agora"]["raw"]["segments"]:
        assert (_drive_folder(env, ulid) / seg["file"]).is_file(), \
            f"push reported {(done, failed)} but Drive's session.md names a missing {seg['file']}"
    assert "第二版" not in text and "本機改的" in text, "it published the version it had read"


# --- the pre-send folder ID, across rounds (#166's direction) ---------------------------------------


def _record(paths, ulid):
    path = paths.state / "folder-ids" / f"{ulid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _update_session(paths, env, ulid, title="只改標題", data=None):
    folder = store.stage(paths, _header(title, ulid=ulid), f"## user\n{title}\n",
                         data_of(2) if data is None else data, update=True, kind="edit")
    store.remember(paths, folder)


def test_the_folder_id_is_written_before_the_copies_and_dropped_once_confirmed(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _update_session(paths, env, ulid)
    seen = {}
    real = store._copy_batch

    def look(drive, p, names, **kw):
        seen.setdefault("record", _record(p, ulid))
        return real(drive, p, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", look)

    assert store.upload_batch(store.Drive(paths), paths) == []

    assert seen["record"] and seen["record"]["folder_id"], "on disk before the first copy"
    assert "session.md" in seen["record"]["sent"]
    assert _record(paths, ulid) is None, "gone once the version is confirmed"


def test_confirmation_listing_that_loses_the_folder_does_not_revive_a_purged_session(env, monkeypatch):
    """#166: another machine purges the folder before our copy and our copies re-create it; the
    confirmation listing then loses the folder, so the round stops at the md5 check and never
    compares IDs. The next round used to see an ordinary live folder and carry on - undoing the
    delete. The ID written before the copies gives it away."""
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _update_session(paths, env, ulid, "改過", data_of(2, tag=5))
    real, state = store._copy_batch, {"n": 0}

    def purge_first(drive, p, names, **kw):
        state["n"] += 1
        if state["n"] == 1:
            import shutil
            shutil.rmtree(_drive_folder(env, ulid))
        return real(drive, p, names, **kw)

    monkeypatch.setattr(store, "_copy_batch", purge_first)
    monkeypatch.setenv("FAKE_RCLONE_OMIT", ulid)                  # the confirmation lists no such folder
    monkeypatch.setenv("FAKE_RCLONE_OMIT_FROM", "3")
    assert store.upload_batch(store.Drive(paths), paths) == [ulid], "round one stops at the md5 check"
    monkeypatch.setattr(store, "_copy_batch", real)
    monkeypatch.delenv("FAKE_RCLONE_OMIT")
    monkeypatch.delenv("FAKE_RCLONE_OMIT_FROM")

    left = store.upload_batch(store.Drive(paths), paths)

    assert not _drive_folder(env, ulid).exists(), "the purged session was not completed"
    new = [p.name for p in (env / "agora" / "sessions").iterdir()]
    assert len(new) == 1 and new[0] != ulid
    assert _joined(env, new[0]) == data_of(2, tag=5)
    assert _record(paths, ulid) is None


def test_an_unchanged_folder_id_after_a_crash_just_finishes_the_job(env, tmp_path):
    """No purge happened: a crash after session.md leaves the same folder, and the next round
    confirms it as usual - the record must not turn an ordinary retry into a rescue."""
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    hdr_file = tmp_path / "hdr.json"
    hdr_file.write_text(json.dumps(_header("第二版", ulid=ulid)), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(hdr_file), data_of(2, tag=4).hex()],
                          env={**os.environ, "AGORA_TEST_FAULT": "after-session-upload",
                               "PYTHONPATH": str(SRC)}, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137
    assert _record(paths, ulid)

    assert store.upload_batch(store.Drive(paths), paths) == []

    assert [p.name for p in (env / "agora" / "sessions").iterdir()] == [ulid]
    assert _joined(env, ulid) == data_of(2, tag=4) and _record(paths, ulid) is None


@pytest.mark.parametrize("garbage", ["not json", "[]", '{"folder_id": 5, "sent": []}', '{"folder_id": "x"}'])
def test_a_broken_record_is_no_record_and_never_a_verdict(env, garbage):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _update_session(paths, env, ulid, "第二版", data_of(2, tag=6))
    (paths.state / "folder-ids").mkdir(parents=True, exist_ok=True)
    (paths.state / "folder-ids" / f"{ulid}.json").write_text(garbage, encoding="utf-8")
    assert store._read_folder_record(paths, ulid) is None

    assert store.upload_batch(store.Drive(paths), paths) == []

    assert [p.name for p in (env / "agora" / "sessions").iterdir()] == [ulid], "no false rescue"
    assert _joined(env, ulid) == data_of(2, tag=6)


def test_a_record_that_cannot_be_written_only_warns(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _update_session(paths, env, ulid)
    real = store.write_atomic

    def no_room(path, text):
        if "folder-ids" in str(path):
            raise OSError("No space left on device")
        return real(path, text)

    monkeypatch.setattr(store, "write_atomic", no_room)
    said: list[str] = []

    assert store.upload_batch(store.Drive(paths), paths, warn=said.append) == []
    assert any("資料夾 ID 記錄寫不進去" in s for s in said), said


def test_deleting_the_session_clears_the_record(env):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    (paths.state / "folder-ids").mkdir(parents=True)
    (paths.state / "folder-ids" / f"{ulid}.json").write_text('{"folder_id": "x", "sent": []}', encoding="utf-8")
    store.forget_local(paths, ulid)
    assert _record(paths, ulid) is None


def test_push_mirror_takes_the_manifest_from_the_session_md_it_sends_not_from_the_header_it_was_handed(env):
    """The caller's header can be older than the mirror's session.md by the time push_mirror
    runs. What is sent, and the manifest it must be whole against, come from one read."""
    paths = store.Paths.from_env()
    ulid, raw = _stage(paths, data_of(3))
    store.upload_batch(store.Drive(paths), paths)
    cache.pull(paths, [ulid], {})
    old_header = store.Index(paths).header(ulid)
    folder = store.stage(paths, _header("第二版", ulid=ulid), "## user\n第二版\n", data_of(2, tag=8),
                         update=True, kind="continue")
    store.remember(paths, folder)                                  # the mirror now holds v2

    store.push_mirror(store.Drive(paths), paths, ulid, old_header)

    assert "第二版" in (_drive_folder(env, ulid) / "session.md").read_text(encoding="utf-8")
    _consistent(env)
    assert _joined(env, ulid) == data_of(2, tag=8)


# --- round 3 of the review (#169) ---------------------------------------------------------------------


def test_a_vanished_sessions_folder_must_not_make_the_recovery_look_like_a_purge(env, tmp_path):
    """N12: no `sessions/` is not a deletion. Round one finds none (so it has no folder ID and the
    stale record from an earlier attempt must go), rebuilds the folder and dies right after
    session.md. Round two then sees a new ID and only our own files: with the old record still
    there that read as "purged by another machine and re-created by us" and a live session was
    thrown into the trash and saved as Y."""
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    hdr = _header("第二版", ulid=ulid)
    hdr_file = tmp_path / "hdr.json"
    hdr_file.write_text(json.dumps(hdr), encoding="utf-8")
    # an earlier attempt that died after writing its record
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(hdr_file), data_of(2, tag=4).hex()],
                          env={**os.environ, "AGORA_TEST_FAULT": "after-session-upload",
                               "PYTHONPATH": str(SRC)}, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137 and _record(paths, ulid)
    import shutil
    shutil.rmtree(env / "agora" / "sessions")                         # sessions/ is gone altogether

    proc = subprocess.run([sys.executable, "-c", _CHILD_RESUME],
                          env={**os.environ, "AGORA_TEST_FAULT": "after-session-upload",
                               "PYTHONPATH": str(SRC)}, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137, proc.stderr[-300:]
    assert not _record(paths, ulid), "no stale record survives a round that had no folder to compare"

    left = store.upload_batch(store.Drive(paths), paths)

    assert [p.name for p in (env / "agora" / "sessions").iterdir()] == [ulid], "no purge, no Y"
    assert left == [] and _joined(env, ulid) == data_of(2, tag=4)


_CHILD_RESUME = """
from agora import store
paths = store.Paths.from_env()
store.upload_batch(store.Drive(paths), paths)
"""


def _purge_then_recreate_setup(env, paths, tmp_path):
    """v1 on Drive; a v2 update is staged and its upload dies after session.md having been
    written into a *purged and re-created* folder - the cross-round purge the record detects."""
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    hdr = _header("只改標題", ulid=ulid)
    hdr_file = tmp_path / "hdr.json"
    hdr_file.write_text(json.dumps(hdr), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_PURGING, str(hdr_file), data_of(2).hex(), str(_drive_folder(env, ulid))],
        env={**os.environ, "AGORA_TEST_FAULT": "after-session-upload", "PYTHONPATH": str(SRC)},
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 137, proc.stderr[-300:]
    return ulid


def test_a_failed_purge_does_not_rescue_across_rounds(env, tmp_path, monkeypatch):
    """The folder we re-created cannot be taken down: do not save Y either (the old X would stay
    alive on Drive AND the work would be duplicated). The entry and the record stay; the next
    round judges again, and then purges and rescues once the purge works."""
    paths = store.Paths.from_env()
    ulid = _purge_then_recreate_setup(env, paths, tmp_path)
    real = store.Drive._run

    def purge_fails(self, *args, **kw):
        if args and args[0] == "purge":
            raise store.StoreError("purge 失敗（注入）")
        return real(self, *args, **kw)

    monkeypatch.setattr(store.Drive, "_run", purge_fails)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid] and ulid in store.outbox_ulids(paths)
    assert [p.name for p in (env / "agora" / "sessions").iterdir()] == [ulid], "no Y was saved"
    assert _record(paths, ulid), "the evidence is kept for the next round"
    assert any("不另存" in s for s in said), said

    monkeypatch.setattr(store.Drive, "_run", real)
    store.upload_batch(store.Drive(paths), paths)
    assert not _drive_folder(env, ulid).exists() and len([p for p in (env / "agora" / "sessions").iterdir()]) == 1


def test_a_failed_purge_does_not_rescue_in_the_same_round(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(2))
    store.upload_batch(store.Drive(paths), paths)
    _update_session(paths, env, ulid, "改過", data_of(2, tag=5))
    real_copy, state = store._copy_batch, {"n": 0}

    def purge_first(drive, p, names, **kw):
        state["n"] += 1
        if state["n"] == 1:
            import shutil
            shutil.rmtree(_drive_folder(env, ulid))
        return real_copy(drive, p, names, **kw)

    real_run = store.Drive._run

    def purge_fails(self, *args, **kw):
        if args and args[0] == "purge":
            raise store.StoreError("purge 失敗（注入）")
        return real_run(self, *args, **kw)

    monkeypatch.setattr(store, "_copy_batch", purge_first)
    monkeypatch.setattr(store.Drive, "_run", purge_fails)
    said: list[str] = []

    left = store.upload_batch(store.Drive(paths), paths, warn=said.append)

    assert left == [ulid] and ulid in store.outbox_ulids(paths)
    assert [p.name for p in (env / "agora" / "sessions").iterdir()] == [ulid], "no Y"
    assert any("不另存" in s for s in said), said


def test_a_record_that_cannot_be_removed_only_warns(env, monkeypatch, capsys):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    (paths.state / "folder-ids").mkdir(parents=True)
    path = paths.state / "folder-ids" / f"{ulid}.json"
    path.write_text('{"folder_id": "x", "sent": []}', encoding="utf-8")
    real = Path.unlink

    def deny(self, *a, **k):
        if self == path:
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    store._forget_folder_record(paths, ulid)
    assert "刪不掉" in capsys.readouterr().err


def test_a_broken_chunk_manifest_is_not_called_a_pre_upgrade_entry(env):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    _tamper_last_segment_name(paths, ulid, "session.md")
    said: list[str] = []
    store.upload_batch(store.Drive(paths), paths, warn=said.append)
    (line,) = [s for s in said if ulid in s]
    assert "不合法" in line and "升級前" not in line and "agora delete" not in line.replace("不要直接 delete", "")


def test_delete_clears_the_published_record_too(env):
    paths = store.Paths.from_env()
    ulid, _ = _stage(paths, data_of(1))
    store.upload_batch(store.Drive(paths), paths)
    assert (paths.state / "published" / f"{ulid}.json").is_file()
    store.forget_local(paths, ulid)
    assert not (paths.state / "published" / f"{ulid}.json").exists()


def test_the_cloud_lost_hint_for_a_legacy_session_points_at_a_reimport(env):
    legacy = {"agora": {"raw": {"md5": "a" * 32, "size": 1, "file": "raw-aaaaaaaaaaaa.json"}}}
    text = store.cloud_lost("agora:01X", legacy)
    assert "--as-new" in text and "--not-exist-upload" not in text
    chunked = {"agora": {"raw": {"md5": md5(b"x"), "size": 1, "layout": "chunks/1", "segments": [
        {"file": f"seg-0001-0000-{md5(b'x')[:12]}.part", "size": 1, "md5": md5(b"x")}]}}}
    assert "--not-exist-upload" in store.cloud_lost("agora:01X", chunked)
    assert "--not-exist-upload" in store.cloud_lost("agora:01X")


def test_the_segment_pattern_takes_ascii_digits_only():
    name = "seg-\u0660\u0660\u0660\u0661-0000-aaaaaaaaaaaa.part"          # Arabic-Indic digits
    assert not store._is_segment_name(name)
