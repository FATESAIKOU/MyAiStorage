"""The raw's layout on Drive, read side only (#168, #109 R3a): `raw_complete`, `raw_name`,
the sync of a `chunks/1` / legacy / unknown session, `ensure_raw_file`'s failure paths and
the clear refusals for a raw this version does not read.

Everything is hand-built on the fake rclone: `stage` still writes the one-file raw (R3b-1
changes that), so the segments and the `chunks/1` headers are made here, directly.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from agora import cli, header as h, store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
SEG = 4096


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    (remote / "agora" / "sessions").mkdir(parents=True)
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


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def calls(remote: Path) -> list[list[str]]:
    log = remote.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _data(n_segments: int = 3, tail: int = 100) -> bytes:
    return b"".join(bytes([65 + i]) * SEG for i in range(n_segments)) + b"z" * tail


def _manifest(data: bytes, gen: int = 1) -> list[dict]:
    parts = [data[i:i + SEG] for i in range(0, len(data), SEG)]
    return [{"file": f"seg-{gen:04d}-{i:04d}-{md5(p)[:12]}.part", "size": len(p), "md5": md5(p)}
            for i, p in enumerate(parts)]


def _put(remote: Path, data: bytes | None = None, *, layout="chunks/1", ulid=None,
         title="分段", gen=1, segs=True, extra_raw=None) -> tuple[str, dict, bytes]:
    """A session as another machine would have left it on Drive."""
    data = _data() if data is None else data
    ulid = ulid or h.new_ulid()
    manifest = _manifest(data, gen)
    raw = {"md5": md5(data), "size": len(data)}
    if layout:
        raw["layout"] = layout
        raw["segments"] = manifest
    else:
        raw["file"] = f"raw-{md5(data)[:12]}.json"
    raw.update(extra_raw or {})
    hdr = {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [],
           "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                     "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                     "source": {"agent": "opencode", "session_id": "ses_x",
                                "created_at": "2026-10-01T00:00:00Z"},
                     "raw": raw}}
    folder = remote / "agora" / "sessions" / ulid
    folder.mkdir(parents=True)
    (folder / "session.md").write_text(h.dump_document(hdr, "## user\n分段的對話\n"),
                                       encoding="utf-8")
    if layout == "chunks/1" and segs:
        for seg, i in zip(manifest, range(0, len(data), SEG)):
            (folder / seg["file"]).write_bytes(data[i:i + SEG])
    elif not layout:
        (folder / f"raw-{md5(data)[:12]}.json").write_bytes(data)   # where the real one would be
    return ulid, hdr, data


# --- 3.1: raw_layout / raw_segments / raw_complete / raw_name ------------------------------


def _files(raw: dict) -> dict:
    return {s["file"]: s["md5"] for s in raw["segments"]}


def _raw(data: bytes | None = None) -> dict:
    data = _data() if data is None else data
    return {"md5": md5(data), "size": len(data), "layout": "chunks/1", "segments": _manifest(data)}


def test_a_whole_chunked_raw_is_complete():
    raw = _raw()
    assert store.raw_layout(raw) == "chunks/1"
    assert len(store.raw_segments(raw)) == 4
    assert store.raw_complete(raw, _files(raw)) is True


def test_no_raw_is_whole_by_itself():
    assert store.raw_complete(None, {}) is True
    assert store.raw_complete({}, {}) is True


@pytest.mark.parametrize("layout", ["chunks/9", "single/1", "", 3])
def test_an_unknown_layout_is_none_not_false(layout):
    raw = {**_raw(), "layout": layout}
    assert store.raw_complete(raw, _files(raw)) is None


def test_no_layout_is_none_not_false():
    raw = {"md5": "a" * 32, "size": 3, "file": "raw-aaaaaaaaaaaa.json"}
    assert store.raw_complete(raw, {"raw-aaaaaaaaaaaa.json": "a" * 32}) is None


def test_a_missing_or_changed_segment_is_false():
    raw = _raw()
    files = _files(raw)
    first = raw["segments"][0]["file"]
    assert store.raw_complete(raw, {k: v for k, v in files.items() if k != first}) is False
    assert store.raw_complete(raw, {**files, first: "0" * 32}) is False


def test_sizes_that_do_not_add_up_are_false():
    raw = _raw()
    raw["size"] += 1
    assert store.raw_complete(raw, _files(raw)) is False


def test_an_empty_manifest_is_false():
    raw = {**_raw(), "segments": []}
    assert store.raw_complete(raw, {}) is False


@pytest.mark.parametrize("name", ["../evil.part", "a/b.part", "a\\b.part", ".hidden.part",
                                  "..", "", None, 7])
def test_an_unsafe_segment_name_is_false(name):
    raw = _raw()
    raw["segments"][0] = {**raw["segments"][0], "file": name}
    files = {s["file"]: s["md5"] for s in raw["segments"] if isinstance(s["file"], str)}
    assert store.raw_complete(raw, files) is False


@pytest.mark.parametrize("bad", ["XYZ", "a" * 31, "A" * 32, 5, None])
def test_a_segment_md5_that_is_not_hex_is_false(bad):
    raw = _raw()
    raw["segments"][1] = {**raw["segments"][1], "md5": bad}
    assert store.raw_complete(raw, {s["file"]: s["md5"] for s in raw["segments"]}) is False


def test_raw_name_comes_from_the_md5_and_nothing_else():
    md5hex = "0123456789abcdef" * 2
    assert store.raw_name({"md5": md5hex}) == "raw-0123456789ab.json"
    # whatever else the header says is ignored: a name taken from a header can leave the mirror
    for file in ("raw-0123456789ab.json", "../../escaped.json", "/tmp/abs.json", "sub/x.json", ""):
        assert store.raw_name({"md5": md5hex, "file": file}) == "raw-0123456789ab.json"


@pytest.mark.parametrize("md5hex", [None, "", "abc", "A" * 32, "g" * 32, "a" * 33, 7])
def test_no_name_without_a_proper_md5(md5hex):
    with pytest.raises(store.StoreError):
        store.raw_name({"md5": md5hex})


# --- 3.28: the two environment settings -------------------------------------------------------


def test_chunk_and_grace_defaults(monkeypatch):
    monkeypatch.delenv("AGORA_RAW_CHUNK", raising=False)
    monkeypatch.delenv("AGORA_RAW_ORPHAN_GRACE", raising=False)
    assert store.raw_chunk_size() == 4194304
    assert store.raw_orphan_grace() == 3600


def test_chunk_and_grace_can_be_set(monkeypatch):
    monkeypatch.setenv("AGORA_RAW_CHUNK", "4096")
    monkeypatch.setenv("AGORA_RAW_ORPHAN_GRACE", "5")
    assert (store.raw_chunk_size(), store.raw_orphan_grace()) == (4096, 5)


@pytest.mark.parametrize("name,fn", [("AGORA_RAW_CHUNK", store.raw_chunk_size),
                                     ("AGORA_RAW_ORPHAN_GRACE", store.raw_orphan_grace)])
@pytest.mark.parametrize("bad", ["4k", "1.5", "-3", "0", "lots"])
def test_a_bad_setting_is_an_error_not_the_default(monkeypatch, name, fn, bad):
    monkeypatch.setenv(name, bad)
    with pytest.raises(store.StoreError, match=name):
        fn()


def test_there_is_no_layout_or_threshold_setting():
    for gone in ("AGORA_RAW_LAYOUT", "AGORA_RAW_SEGMENT_MIN"):
        assert gone not in Path(store.__file__).read_text(encoding="utf-8")


# --- 3.12: sync --------------------------------------------------------------------------------


def _unfinished(paths) -> dict:
    return json.loads((paths.state / "unfinished.json").read_text(encoding="utf-8"))


def _downloads_of_session_md(remote: Path, ulid: str) -> int:
    return sum(1 for c in calls(remote)
               if any(f"{ulid}/session.md" in a or a.endswith(f"/{ulid}/session.md") for a in c)
               or any(a.endswith(f"sessions/{ulid}/session.md") for a in c))


def test_a_whole_chunked_session_is_indexed_and_its_raw_waits(env):
    paths = store.Paths.from_env()
    ulid, _, _ = _put(env)
    index = store.sync(paths)
    assert index.header(ulid) is not None
    assert (paths.mirror / ulid / "session.md").is_file()
    assert not list((paths.mirror / ulid).glob("raw-*")), "the raw is fetched on demand only"


def test_a_chunked_session_with_a_missing_segment_is_unfinished_and_not_downloaded_again(env):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    gone = hdr["agora"]["raw"]["segments"][1]["file"]
    (env / "agora" / "sessions" / ulid / gone).unlink()

    index = store.sync(paths)

    assert index.header(ulid) is None
    assert not (paths.mirror / ulid / "session.md").exists()
    record = _unfinished(paths)
    assert record["version"] == 2
    assert record["sessions"][ulid]["legacy"] is False
    before = len(calls(env))
    store.sync(paths)
    # the same session.md and the same listing: remembered, not downloaded again (#119)
    assert not any("copyto" in c and f"{ulid}/session.md" in " ".join(c) for c in calls(env)[before:])
    assert index.header(ulid) is None

    # the segment shows up: now it is whole and gets indexed
    data = _data()
    (env / "agora" / "sessions" / ulid / gone).write_bytes(data[SEG:2 * SEG])
    assert store.sync(paths).header(ulid) is not None


def test_a_version_1_unfinished_file_is_ignored(env):
    paths = store.Paths.from_env()
    paths.state.mkdir(parents=True)
    old = {"session_md5": "a", "raw_file": "b", "raw_md5": "c"}
    (paths.state / "unfinished.json").write_text(json.dumps({"version": 1, "sessions": {"01X": old}}))
    assert store._unfinished(paths) == {}
    # and a v1 file is ignored by its version, not merely because its records look different
    new = {"session_md5": "a", "raw_manifest": [], "raw_fingerprint": "f", "legacy": False}
    (paths.state / "unfinished.json").write_text(json.dumps({"version": 1, "sessions": {"01X": new}}))
    assert store._unfinished(paths) == {}
    (paths.state / "unfinished.json").write_text(json.dumps({"version": 2, "sessions": {"01X": new}}))
    assert list(store._unfinished(paths)) == ["01X"]


@pytest.mark.parametrize("variant", ["no-layout", "unknown-layout"])
def test_a_legacy_session_is_indexed_kept_and_not_downloaded_again(env, monkeypatch, variant):
    """3.24: legacy (no `layout`, once R3b-1 stops writing the one-file raw) and unknown
    (`chunks/9`) are one behaviour: the reading version works, the raw does not."""
    monkeypatch.setattr(store, "_SINGLE_FILE_READABLE", False)
    paths = store.Paths.from_env()
    if variant == "no-layout":
        ulid, _, _ = _put(env, layout=None)
    else:
        ulid, _, _ = _put(env, layout="chunks/9")

    index = store.sync(paths)

    assert index.header(ulid) is not None, "the reading version is searchable"
    assert (paths.mirror / ulid / "session.md").is_file(), "the mirror keeps it"
    assert _unfinished(paths)["sessions"][ulid]["legacy"] is True
    before = len(calls(env))
    again = store.sync(paths)
    assert again.header(ulid) is not None
    assert _unfinished(paths)["sessions"][ulid]["legacy"] is True      # still remembered
    assert not any(f"{ulid}/session.md" in " ".join(c) and ("copy" in c or "copyto" in c)
                   for c in calls(env)[before:]), "no further download of a legacy session"


def test_the_one_file_raw_is_still_read_while_it_is_the_only_thing_stage_writes(env):
    """R3a -> R3b-1 transition: no `layout` + `file` is today's own format and must keep working."""
    assert store._SINGLE_FILE_READABLE is True
    paths = store.Paths.from_env()
    ulid, _, data = _put(env, layout=None)
    assert store.sync(paths).header(ulid) is not None
    assert not _unfinished_exists(paths)
    hdr = store.Index(paths).header(ulid)
    assert store.fetch_raw(paths, store.Drive(paths), ulid, hdr) == data


def _unfinished_exists(paths) -> bool:
    return (paths.state / "unfinished.json").exists() and bool(
        _unfinished(paths)["sessions"])


# --- 3.13 / 3.26: ensure_raw_file --------------------------------------------------------------


def _header_of(remote: Path, ulid: str) -> dict:
    hdr, _ = h.split_document(
        (remote / "agora" / "sessions" / ulid / "session.md").read_text(encoding="utf-8"))
    return hdr


def test_ensure_raw_file_joins_the_segments_and_cleans_up(env):
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env)
    stale = paths.mirror / ulid / "raw-000000000000.json"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"an older whole raw")

    got = store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert got == paths.mirror / ulid / f"raw-{md5(data)[:12]}.json"
    assert got.read_bytes() == data
    assert not stale.exists(), "other raw-* in the mirror go"
    assert not (paths.cache / "segments" / ulid).exists(), "the temp segments are gone"
    assert [p.name for p in (paths.mirror / ulid).iterdir() if p.name.startswith(".")] == []
    before = len(calls(env))
    assert store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr) == got
    assert len(calls(env)) == before, "a whole raw with the right md5 costs no network"


def test_the_segments_come_in_one_download_call(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    seen: list[list[str]] = []
    real = store.Drive.download_segments
    monkeypatch.setattr(store.Drive, "download_segments",
                        lambda self, u, names, dest: (seen.append(list(names)), real(self, u, names, dest))[1])
    store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)
    assert len(seen) == 1 and len(seen[0]) == 4


def test_a_segment_that_is_gone_follows_the_new_manifest_and_keeps_the_good_segments(env, monkeypatch):
    """3.26: the header we hold lists a segment another machine cleaned up after a new
    version. One fresh session.md, and only the segments the new manifest adds are fetched."""
    paths = store.Paths.from_env()
    ulid, old_hdr, _ = _put(env)
    old_segs = old_hdr["agora"]["raw"]["segments"]
    new_data = _data()[:3 * SEG] + b"Q" * 50                  # same first three segments, new tail
    new_segs = _manifest(new_data)
    folder = env / "agora" / "sessions" / ulid
    (folder / old_segs[3]["file"]).unlink()                  # the old tail is gone
    (folder / new_segs[3]["file"]).write_bytes(new_data[3 * SEG:])
    new_hdr = json.loads(json.dumps(old_hdr))
    new_hdr["agora"]["raw"] = {"md5": md5(new_data), "size": len(new_data),
                               "layout": "chunks/1", "segments": new_segs}
    (folder / "session.md").write_text(h.dump_document(new_hdr, "## user\n新版\n"), encoding="utf-8")
    asked: list[list[str]] = []
    real = store.Drive.download_segments
    monkeypatch.setattr(store.Drive, "download_segments",
                        lambda self, u, names, dest: (asked.append(sorted(names)), real(self, u, names, dest))[1])

    got = store.ensure_raw_file(paths, store.Drive(paths), ulid, old_hdr)

    assert got.read_bytes() == new_data
    assert len(asked) == 2
    assert asked[1] == [new_segs[3]["file"]], "the three segments already checked are not fetched again"
    assert not (paths.cache / "segments" / ulid).exists()


def test_a_segment_that_stays_gone_is_an_error_and_leaves_nothing(env):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    (env / "agora" / "sessions" / ulid / hdr["agora"]["raw"]["segments"][2]["file"]).unlink()

    with pytest.raises(store.StoreError):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))
    assert not list((paths.mirror / ulid).glob(".*joining*"))
    assert not (paths.cache / "segments" / ulid).exists()


def test_a_segment_with_the_wrong_md5_is_not_installed(env):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    victim = env / "agora" / "sessions" / ulid / hdr["agora"]["raw"]["segments"][1]["file"]
    victim.write_bytes(b"x" * SEG)                            # same size, other content

    with pytest.raises(store.StoreError, match="md5"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))
    assert not (paths.cache / "segments" / ulid).exists()


def test_a_segment_whose_own_md5_is_wrong_is_caught_even_when_the_whole_is_right(env):
    """The per-segment check is its own line of defence: the manifest md5 of one segment is
    wrong while the bytes joined (and so the whole md5) would be fine."""
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env)
    hdr["agora"]["raw"]["segments"][1]["md5"] = "f" * 32
    # the listing the writer left would not agree either, but ensure_raw_file only has the header

    with pytest.raises(store.StoreError, match="md5"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))


def test_a_whole_raw_md5_that_does_not_match_is_not_installed(env):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    hdr["agora"]["raw"]["md5"] = "0" * 32                      # the segments are fine, the whole is not

    with pytest.raises(store.StoreError, match="md5"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))
    assert not list((paths.mirror / ulid).glob(".*joining*"))


def test_a_whole_raw_size_that_does_not_match_is_not_installed(env):
    """The segments' claimed sizes add up to `raw.size`, and every segment md5 is right,
    but the bytes actually joined are longer: only the size check can see that."""
    paths = store.Paths.from_env()
    data = _data()
    ulid, hdr, _ = _put(env, data)
    segs = hdr["agora"]["raw"]["segments"]
    segs[0]["size"] -= 4                                       # claim 4 bytes less in the first ...
    segs[1]["size"] += 4                                       # ... and 4 more in the second
    # the sum is unchanged, so the static manifest checks pass; the real total is data's

    # make the real total differ from raw.size while the claimed sizes still add up to it
    hdr["agora"]["raw"]["size"] = len(data) - 1
    segs[-1]["size"] -= 1

    with pytest.raises(store.StoreError, match="大小"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))
    assert not list((paths.mirror / ulid).glob(".*joining*"))


def test_a_network_error_is_a_store_error_and_leaves_nothing(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "--files-from")     # only the segment download

    with pytest.raises(store.StoreError):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))
    assert not (paths.cache / "segments" / ulid).exists()


def test_an_unsafe_manifest_is_refused_before_anything_is_downloaded(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env)
    hdr["agora"]["raw"]["segments"][0]["file"] = "../session.md"
    asked = []
    monkeypatch.setattr(store.Drive, "download_segments",
                        lambda self, u, names, dest: asked.append(names))

    with pytest.raises(store.StoreError):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert asked == []


@pytest.mark.parametrize("layout", ["chunks/9", "single/1"])
def test_an_unknown_layout_has_no_usable_raw(env, layout):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env, layout=layout)
    with pytest.raises(store.StoreError, match="舊格式或未知格式"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)
    with pytest.raises(store.StoreError, match="--as-new"):
        store.require_usable_raw(hdr)


def test_a_session_without_a_raw_has_nothing_to_refuse():
    store.require_usable_raw({"agora": {"relation": "merge"}})


# --- 3.14 / 3.15: show --raw, edit, continue ---------------------------------------------------


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_show_raw_streams_the_joined_file(env, capfdbinary):
    paths = store.Paths.from_env()
    ulid, _, data = _put(env)
    code = cli.main(["show", "session", f"agora:{ulid}", "--raw"])
    assert code == 0
    assert capfdbinary.readouterr().out == data


@pytest.mark.parametrize("layout", ["chunks/9", "single/1"])
def test_show_raw_edit_and_continue_refuse_an_unknown_raw_up_front(env, capsys, layout):
    paths = store.Paths.from_env()
    ulid, _, _ = _put(env, layout=layout)
    store.sync(paths)
    agora_id = f"agora:{ulid}"
    for argv in (["show", "session", agora_id, "--raw"],
                 ["edit", "session", agora_id, "--header", "title=改"],
                 ["continue", "session", agora_id, "--agent", "claude"]):
        code, _, err = _run(capsys, *argv)
        assert code != 0, argv
        assert "舊格式或未知格式" in err and "--as-new" in err, (argv, err)
    # nothing was fetched or staged, and the reading version still works
    assert not (paths.outbox / ulid).exists()
    assert not (paths.cache / "segments").exists()
    code, out, _ = _run(capsys, "show", "session", agora_id)
    assert code == 0 and "分段的對話" in out


def test_pull_of_a_legacy_session_keeps_the_reading_version_and_says_the_raw_is_unreadable(
        env, capsys):
    paths = store.Paths.from_env()
    ulid, _, _ = _put(env, layout="chunks/9")
    code, _, err = _run(capsys, "pull", "session", f"agora:{ulid}")
    assert code == 0, err
    assert (paths.mirror / ulid / "session.md").is_file()
    assert "舊格式或未知格式" in err


# --- #168 round 2: the four blockers -----------------------------------------------------------


@pytest.mark.parametrize("file", ["../../escaped.json", "/tmp/agora-abs-escape.json",
                                  "sub/dir.json", "raw-aaaaaaaaaaaa.json"])
def test_a_file_field_cannot_move_a_chunked_raw_out_of_its_place(env, tmp_path, file):
    """Blocker 1: a `chunks/1` header with a `file` of its own. The raw lands where its md5
    says, in the session's mirror folder, and nowhere else."""
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, extra_raw={"file": file})

    got = store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert got == paths.mirror / ulid / f"raw-{md5(data)[:12]}.json"
    assert got.read_bytes() == data
    assert not (paths.cache / "escaped.json").exists()
    assert not Path("/tmp/agora-abs-escape.json").exists()
    assert sorted(p.name for p in paths.mirror.parent.rglob("*.json")) == [got.name]


@pytest.mark.parametrize("file", ["../../escaped.json", "/tmp/agora-abs-escape2.json",
                                  "sub/dir.json", "raw-aaaaaaaaaaaa.json"])
def test_a_one_file_header_whose_file_is_not_its_md5_name_is_a_bad_header(env, file):
    """Blocker 1, transition format: `single` only when `file` is exactly the md5's name.
    Otherwise nothing is fetched or written, and the session itself is kept."""
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, layout=None, extra_raw={"file": file})
    assert store.raw_kind(hdr["agora"]["raw"]) == "legacy"

    index = store.sync(paths)

    assert index.header(ulid) is not None and (paths.mirror / ulid / "session.md").is_file()
    with pytest.raises(store.StoreError):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)
    assert not Path("/tmp/agora-abs-escape2.json").exists()
    assert not list(paths.mirror.rglob("*.json"))


def test_an_outbox_entry_with_a_bad_md5_is_a_broken_entry(tmp_path):
    folder = tmp_path / "entry"
    folder.mkdir()
    ulid = h.new_ulid()
    hdr = {"type": "Session", "title": "x", "tags": [], "id": f"agora:{ulid}", "refs": [],
           "case": None, "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                                   "updated_at": "2026-10-02T00:00:00Z", "relation": "import",
                                   "parents": [], "raw": {"file": "../x.json", "md5": "nope", "size": 1}}}
    (folder / "session.md").write_text(h.dump_document(hdr, "## user\nx\n"), encoding="utf-8")
    with pytest.raises(h.HeaderError):
        store.read_entry(folder)


_KIND_TABLE = [
    (None, "none"), ({}, "none"),
    (_raw(), "chunks"),
    ({**_raw(), "file": "../../x.json"}, "chunks"),                       # `file` is not consulted
    ({"md5": "a" * 32, "size": 3, "file": "raw-aaaaaaaaaaaa.json"}, "single"),
    ({"md5": "a" * 32, "size": 3, "file": "raw-aaaaaaaaaaaa.json", "layout": None}, "single"),
    ({"md5": "a" * 32, "size": 3}, "legacy"),                             # no file: nothing to read
    ({"md5": "a" * 32, "size": 3, "file": "raw-bbbbbbbbbbbb.json"}, "legacy"),
    ({"md5": "a" * 32, "size": 3, "file": "../raw-aaaaaaaaaaaa.json"}, "legacy"),
    ({"md5": "short", "size": 3, "file": "raw-short.json"}, "legacy"),
    ("a string", "legacy"), ([1, 2], "legacy"), (7, "legacy"),
]
_NON_STRING_LAYOUTS = [3, True, False, [], {}, ["chunks/1"], 0]


@pytest.mark.parametrize("raw,kind", _KIND_TABLE)
def test_raw_kind_table(raw, kind):
    assert store.raw_kind(raw) == kind


@pytest.mark.parametrize("layout", _NON_STRING_LAYOUTS + ["chunks/9", "", "CHUNKS/1", "single"])
def test_any_layout_value_that_is_not_chunks_1_is_legacy_even_with_a_matching_file(layout):
    """Blocker 2: only a *missing or null* `layout` may be the one-file format."""
    raw = {"md5": "a" * 32, "size": 3, "file": "raw-aaaaaaaaaaaa.json", "layout": layout}
    assert store.raw_kind(raw) == "legacy"
    assert store.raw_complete(raw, {"raw-aaaaaaaaaaaa.json": "a" * 32}) is None


@pytest.mark.parametrize("raw,kind", [
    ({"md5": "a" * 32, "size": 3, "file": "raw-aaaaaaaaaaaa.json"}, "legacy"),
    ({"md5": "a" * 32, "size": 3, "file": "raw-aaaaaaaaaaaa.json", "layout": None}, "legacy"),
    (_raw(), "chunks"), (None, "none")])
def test_raw_kind_after_r3b1_flips_the_constant(monkeypatch, raw, kind):
    monkeypatch.setattr(store, "_SINGLE_FILE_READABLE", False)
    assert store.raw_kind(raw) == kind


@pytest.mark.parametrize("layout", _NON_STRING_LAYOUTS)
def test_a_non_string_layout_does_not_delete_the_session(env, layout):
    """Blocker 2: `layout: 3` plus a `file` whose raw is not on Drive used to read as the
    one-file format, come out "not written yet" and drop the session from index and mirror."""
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env, layout=None, extra_raw={"layout": layout})
    (env / "agora" / "sessions" / ulid / f"raw-{md5(_data())[:12]}.json").unlink()   # raw not there

    index = store.sync(paths)

    assert index.header(ulid) is not None, "kept in the index"
    assert (paths.mirror / ulid / "session.md").is_file(), "kept in the mirror"
    assert _unfinished(paths)["sessions"][ulid]["legacy"] is True


@pytest.mark.parametrize("raw_value", ["a string", [1, 2], 7])
def test_a_raw_that_is_not_a_mapping_does_not_stop_the_round(env, raw_value):
    paths = store.Paths.from_env()
    good, _, _ = _put(env)
    bad, bad_hdr, _ = _put(env)
    bad_hdr["agora"]["raw"] = raw_value
    (env / "agora" / "sessions" / bad / "session.md").write_text(
        h.dump_document(bad_hdr, "## user\n壞的 raw\n"), encoding="utf-8")

    index = store.sync(paths)

    assert index.header(good) is not None
    assert index.header(bad) is not None, "kept, its raw unusable"


def _mirror_a_whole_legacy_raw(paths, ulid, data):
    (paths.mirror / ulid).mkdir(parents=True, exist_ok=True)
    (paths.mirror / ulid / f"raw-{md5(data)[:12]}.json").write_bytes(data)


def test_with_the_constant_off_a_legacy_raw_in_the_mirror_is_still_refused(env, monkeypatch):
    """Blocker 3: once R3b-1 clears the constant, a no-layout session is legacy - also when
    its whole raw happens to be in the mirror already."""
    monkeypatch.setattr(store, "_SINGLE_FILE_READABLE", False)
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, layout=None)
    _mirror_a_whole_legacy_raw(paths, ulid, data)

    with pytest.raises(store.StoreError, match="舊格式或未知格式"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)
    with pytest.raises(store.StoreError, match="舊格式或未知格式"):
        store.fetch_raw(paths, store.Drive(paths), ulid, hdr)


def test_with_the_constant_off_show_raw_and_pull_refuse_a_legacy_session(env, monkeypatch, capsys):
    monkeypatch.setattr(store, "_SINGLE_FILE_READABLE", False)
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, layout=None)
    _mirror_a_whole_legacy_raw(paths, ulid, data)
    agora_id = f"agora:{ulid}"

    code, out, err = _run(capsys, "show", "session", agora_id, "--raw")
    assert code != 0 and "舊格式或未知格式" in err and data.decode() not in out

    code, _, err = _run(capsys, "pull", "session", agora_id)
    assert code == 0 and "舊格式或未知格式" in err
    assert (paths.mirror / ulid / "session.md").is_file(), "pull keeps the reading version"


# --- the one-file download (blocker 4) ---------------------------------------------------------


def _single_mirror_files(paths, ulid) -> list[str]:
    folder = paths.mirror / ulid
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_a_one_file_raw_with_the_wrong_md5_leaves_nothing_in_the_mirror(env):
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, layout=None)
    (env / "agora" / "sessions" / ulid / f"raw-{md5(data)[:12]}.json").write_bytes(b"Z" * len(data))

    with pytest.raises(store.StoreError, match="md5"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not [n for n in _single_mirror_files(paths, ulid) if n != "session.md"]


def test_a_one_file_raw_with_the_wrong_size_leaves_nothing_in_the_mirror(env):
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, layout=None)
    hdr["agora"]["raw"]["size"] = len(data) + 1            # md5 is right, the size claim is not

    with pytest.raises(store.StoreError, match="大小"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)

    assert not [n for n in _single_mirror_files(paths, ulid) if n != "session.md"]


def test_a_one_file_download_that_fails_leaves_nothing_in_the_mirror(env, monkeypatch):
    paths = store.Paths.from_env()
    ulid, hdr, _ = _put(env, layout=None)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")

    with pytest.raises(store.StoreError):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr, retry=False)

    assert not [n for n in _single_mirror_files(paths, ulid) if n != "session.md"]


def test_a_good_one_file_raw_is_installed_by_the_temp_file(env):
    paths = store.Paths.from_env()
    ulid, hdr, data = _put(env, layout=None)
    got = store.ensure_raw_file(paths, store.Drive(paths), ulid, hdr)
    assert got.read_bytes() == data
    assert not [n for n in _single_mirror_files(paths, ulid) if n.startswith(".")]


# --- the non-blocking ones ---------------------------------------------------------------------


def test_a_manifest_without_a_proper_whole_md5_is_not_complete():
    raw = _raw()
    for bad in (None, "", "xyz", "A" * 32):
        assert store.raw_complete({**raw, "md5": bad}, _files(raw)) is False


def test_a_fresh_header_that_is_not_chunks_1_is_not_followed_after_a_404(env):
    paths = store.Paths.from_env()
    ulid, old_hdr, _ = _put(env)
    folder = env / "agora" / "sessions" / ulid
    (folder / old_hdr["agora"]["raw"]["segments"][3]["file"]).unlink()      # the 404
    newer = json.loads(json.dumps(old_hdr))
    newer["agora"]["raw"]["layout"] = "chunks/9"                          # a newer format that
    (folder / "session.md").write_text(h.dump_document(newer, "x\n"), encoding="utf-8")  # still lists segments

    with pytest.raises(store.StoreError, match="舊格式或未知格式"):
        store.ensure_raw_file(paths, store.Drive(paths), ulid, old_hdr)

    assert not list((paths.mirror / ulid).glob("raw-*"))
