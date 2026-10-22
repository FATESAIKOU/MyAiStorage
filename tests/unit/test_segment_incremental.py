"""R3b-2: `stage` takes a plan, and only the new segments go up (design B.2-B.5, I.2).

The adapter is replaced by a small stand-in: a raw is `main` bytes plus a `tail`; the plan names
where `main` ends (`seal_upto`) and where the previous round stopped (`append_from`). What is
checked is what `stage` does with it: sealed segments reused as they are, the cut points, the
cursor written only after the swap, and the fall back to a whole cut whenever the plan does not
add up. The claude adapter's side of the plan is in test_agent_claude_increment.py.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import random
import shutil
import subprocess
import sys
import types

import pytest

from agora import header as h, store
from test_segment_write import (C, SRC, _drive_folder, _drive_names, _joined, _log, _manifest,  # noqa: F401
                                _mark, _sent, _header, env, md5)


@pytest.fixture
def paths(env):
    return store.Paths.from_env()


def _main(n: int, tag: int = 0) -> bytes:
    """`n` lines of 1001 bytes: a main content that is never a multiple of C."""
    return b"".join(bytes([97 + (i + tag) % 26]) * 1000 + b"\n" for i in range(n))


def _put(paths, ulid, raw: bytes, seal_upto: int | None, *, append_from=None, cursor_ulid="same",
         lines: int = 1, update=False, cursor="default"):
    """Stage `raw` the way claude's save does; returns the `raw` block of the new header."""
    hdr = _header("增量", ulid)
    plan = None
    if seal_upto is not None:
        cur = {"version": 1, "main_line_count": lines, "main_md5": md5(raw[:seal_upto])}
        plan = {"append_from": append_from, "seal_upto": seal_upto,
                "cursor": cur if cursor == "default" else cursor,
                "cursor_ulid": ulid if cursor_ulid == "same" else cursor_ulid}
    folder = store.stage(paths, hdr, "## user\n增量\n", raw, update=update, plan=plan)
    store.remember(paths, folder)
    return hdr["agora"]["raw"]


def _up(paths):
    assert store.upload_batch(store.Drive(paths), paths) == []


def _gens(block) -> set[str]:
    return {s["file"].split("-")[1] for s in block["segments"]}


def _round(paths, ulid, raw, seal_upto, *, lines=1, **kw):
    """One later round the way claude does it: append_from is what the cursor says."""
    cur = store.read_cursor(paths, ulid)
    return _put(paths, ulid, raw, seal_upto, append_from=cur["raw_stable"] if cur else None,
                lines=lines, update=True, **kw)


def _first(paths, size_lines=25, tail=b"T1"):
    ulid = h.new_ulid()
    main = _main(size_lines)
    block = _put(paths, ulid, main + tail, len(main))
    return ulid, main, block


# --- 3.4 / 3.23: the first import is already cut ------------------------------------------------


def test_the_first_import_is_already_cut_in_c_sized_segments_and_a_tail(paths):
    ulid, main, block = _first(paths)
    sizes = [s["size"] for s in block["segments"]]
    assert sizes[:-1] == [C] * (len(main) // C) + [len(main) % C]
    assert sizes[-1] == 2
    assert sum(sizes) == len(main) + 2
    cur = store.read_cursor(paths, ulid)
    assert cur["raw_stable"] == len(main) and cur["base_md5"] == block["md5"]
    assert cur["sealed_line_count"] == 1 and cur["sealed_md5"] == md5(main)


def test_a_small_session_stays_one_segment_and_seals_nothing(paths):
    ulid = h.new_ulid()
    main = _main(2)
    block = _put(paths, ulid, main + b"T", len(main))
    assert len(block["segments"]) == 1
    cur = store.read_cursor(paths, ulid)
    assert cur["raw_stable"] == 0 and cur["sealed_line_count"] == 0 and cur["sealed_md5"] == store.EMPTY_MD5


def test_no_plan_is_the_plain_cut_and_leaves_no_cursor(paths):
    ulid = h.new_ulid()
    block = _put(paths, ulid, _main(25), None)
    assert [s["size"] for s in block["segments"]][:-1] == [C] * 6
    assert store.read_cursor(paths, ulid) is None


# --- 3.5: append -------------------------------------------------------------------------------


def test_an_append_reuses_the_sealed_segments_and_cuts_only_the_new_bytes(paths):
    ulid, main1, b1 = _first(paths)
    main2 = main1 + _main(20, 3)
    b2 = _round(paths, ulid, main2 + b"T2", len(main2), lines=2)
    old, new = b1["segments"], b2["segments"]
    assert new[:len(old) - 1] == old[:-1]
    assert new[len(old) - 1]["file"] != old[-1]["file"]
    assert _gens(b2) == _gens(b1), "an append keeps the generation"
    assert [int(s["file"].split("-")[2]) for s in new] == list(range(len(new)))
    assert sum(s["size"] for s in new) == len(main2) + 2
    fresh = [s["size"] for s in new][len(old) - 1:-1]          # cut from append_from, by C
    assert fresh[:-1] == [C] * (len(fresh) - 1) and 0 < fresh[-1] <= C


def test_an_append_uploads_only_the_new_segments(env, paths):
    ulid, main1, b1 = _first(paths, 40)
    _up(paths)
    main2 = main1 + _main(10, 2)
    b2 = _round(paths, ulid, main2 + b"T2", len(main2), lines=2)
    mark = _mark(env)
    _up(paths)
    old_names = {s["file"] for s in b1["segments"][:-1]}
    sent = _sent(env, mark)
    assert not old_names & set(sent), "the sealed segments are not sent again"
    assert sent[-1] == "session.md"
    assert set(sent[:-1]) == {s["file"] for s in b2["segments"]} - old_names
    assert _joined(env, ulid) == main2 + b"T2"


def test_the_property_random_appends_always_join_to_the_whole_raw(env, paths, monkeypatch):
    rng = random.Random(170)
    for chunk in (4096, 5000, 65536, 1 << 20):
        monkeypatch.setenv("AGORA_RAW_CHUNK", str(chunk))
        ulid = h.new_ulid()
        main = b""
        for rnd in range(8):
            step = rng.choice([1, 3, 50, 400])
            main += _main(step, rnd)
            tail = bytes(rng.randrange(97, 123) for _ in range(rng.choice([1, 2, 700])))
            raw = main + tail
            block = _round(paths, ulid, raw, len(main), lines=rnd + 1) if rnd else \
                _put(paths, ulid, raw, len(main))
            assert sum(s["size"] for s in block["segments"]) == len(raw)
            _up(paths)
            assert _joined(env, ulid) == raw, (chunk, rnd)
            assert [s["md5"] for s in _manifest(env, ulid)] == [
                md5(_joined(env, ulid)[a:b]) for a, b in _spans(block)]
            assert store.read_cursor(paths, ulid)["base_md5"] == md5(raw)
            assert len(block["segments"]) <= 2 * (-(-len(raw) // chunk)) + 1


def _spans(block):
    at = 0
    for seg in block["segments"]:
        yield at, at + seg["size"]
        at += seg["size"]


def test_a_half_line_that_becomes_complete_is_only_a_new_tail(env, paths):
    ulid = h.new_ulid()
    main1 = _main(25)
    tail1 = b'{"half]'
    b1 = _put(paths, ulid, main1 + tail1, len(main1))
    _up(paths)
    main2 = main1 + b'{"half": 1}\n'
    b2 = _round(paths, ulid, main2 + b"]", len(main2), lines=2)
    assert b2["segments"][:len(b1["segments"]) - 1] == b1["segments"][:-1]
    _up(paths)
    assert _joined(env, ulid) == main2 + b"]"


# --- 3.6 / 3.7 / 3.21: the plan is checked, never trusted -----------------------------------------


def _bend(paths, ulid, **fields):
    cur = store.read_cursor(paths, ulid)
    cur.update(fields)
    store._cursor_path(paths, ulid).write_text(json.dumps(cur), encoding="utf-8")


def _whole(env, paths, ulid, raw, block, previous):
    """Fell back to a whole cut: the next generation, nothing carried over, bytes right."""
    assert _gens(block) == {f"{int(next(iter(_gens(previous)))) + 1:04d}"}
    assert not {s["file"] for s in block["segments"]} & {s["file"] for s in previous["segments"]}
    _up(paths)
    assert _joined(env, ulid) == raw


def _grown(paths, ulid, main1, lines=2):
    main2 = main1 + _main(12, 4)
    return main2 + b"T2", len(main2)


@pytest.mark.parametrize("what", [
    "append_from_off_by_one", "append_from_beyond_seal", "other_ulid", "no_cursor_file",
    "garbage_cursor_file", "cursor_base_md5_differs", "cursor_raw_stable_differs",
    "not_on_a_segment_boundary", "seal_upto_not_less_than_size", "seal_upto_zero",
    "append_from_not_an_int", "cursor_not_a_dict"])
def test_a_plan_that_does_not_add_up_is_a_whole_cut_and_never_a_wrong_increment(env, paths, what):
    ulid, main1, b1 = _first(paths)
    _up(paths)
    raw, seal = _grown(paths, ulid, main1)
    cur = store.read_cursor(paths, ulid)
    kw = {"append_from": cur["raw_stable"]}
    if what == "append_from_off_by_one":
        kw["append_from"] += 1
    elif what == "append_from_beyond_seal":
        kw["append_from"] = seal + 1
    elif what == "other_ulid":
        kw["cursor_ulid"] = h.new_ulid()
    elif what == "no_cursor_file":
        store.forget_cursor(paths, ulid)
    elif what == "garbage_cursor_file":
        store._cursor_path(paths, ulid).write_text("{not json", encoding="utf-8")
    elif what == "cursor_base_md5_differs":
        _bend(paths, ulid, base_md5=md5(b"other"))
    elif what == "cursor_raw_stable_differs":
        _bend(paths, ulid, raw_stable=cur["raw_stable"] - 1)
    elif what == "not_on_a_segment_boundary":
        kw["append_from"] = 5
        _bend(paths, ulid, raw_stable=5)
    elif what == "seal_upto_not_less_than_size":
        seal = len(raw)
    elif what == "seal_upto_zero":
        seal = 0
    elif what == "append_from_not_an_int":
        kw["append_from"] = "4096"
    elif what == "cursor_not_a_dict":
        store._cursor_path(paths, ulid).write_text("[1]", encoding="utf-8")
    block = _put(paths, ulid, raw, seal, lines=2, update=True, **kw)
    _whole(env, paths, ulid, raw, block, b1)


def test_a_rewritten_prefix_is_found_by_the_adapter_not_stage(paths):
    """`stage` cannot see the bytes behind `append_from`; the adapter's md5 check does (see
    test_agent_claude_increment). What stage does check: the manifest it extends is the one
    the cursor was written for."""
    ulid, main1, b1 = _first(paths)
    store.stage(paths, _header("別的版本", ulid), "x", _main(30, 9) + b"Z", update=True)   # no plan
    raw, seal = _grown(paths, ulid, main1)
    block = _put(paths, ulid, raw, seal, append_from=store.read_cursor(paths, ulid)["raw_stable"], update=True)
    assert _gens(block) != _gens(b1), "a version in between that the cursor does not know of"


def test_an_unchanged_raw_under_a_new_header_keeps_manifest_and_cursor(paths):
    ulid, main1, b1 = _first(paths)
    before = store.read_cursor(paths, ulid)
    hdr = _header("只改標題", ulid)
    store.stage(paths, hdr, "x", main1 + b"T1", update=True,
                plan={"append_from": before["raw_stable"], "seal_upto": len(main1),
                      "cursor": {"version": 1, "main_line_count": 1, "main_md5": md5(main1)},
                      "cursor_ulid": ulid})
    assert hdr["agora"]["raw"] == b1
    assert store.read_cursor(paths, ulid) == before


# --- 3.7: the cursor moves with the stage, not before it -----------------------------------------


def test_the_cursor_is_not_written_by_a_stage_that_fails(paths, monkeypatch):
    ulid, main1, b1 = _first(paths)
    before = store.read_cursor(paths, ulid)
    main2 = main1 + _main(12, 4)

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "_write_file", boom)
    with pytest.raises(OSError):
        _put(paths, ulid, main2 + b"T", len(main2), append_from=before["raw_stable"], update=True)
    monkeypatch.undo()
    assert store.read_cursor(paths, ulid) == before


def test_a_crash_after_the_manifest_leaves_the_old_cursor_and_the_old_entry(paths, tmp_path):
    ulid, main1, b1 = _first(paths)
    before = store.read_cursor(paths, ulid)
    main2 = main1 + _main(12, 4)
    script = f"""
import sys
sys.path.insert(0, {str(SRC)!r}); sys.path.insert(0, {str(_p.Path(__file__).parent)!r})
from agora import header as h, store
from test_segment_incremental import _put
paths = store.Paths.from_env()
raw = bytes.fromhex({(main2 + b"T").hex()!r})
_put(paths, {ulid!r}, raw, {len(main2)}, append_from={before['raw_stable']}, update=True)
"""
    import os
    env = dict(os.environ, AGORA_TEST_FAULT="after-manifest")
    assert subprocess.run([sys.executable, "-c", script], env=env).returncode == 137
    assert store.read_cursor(paths, ulid) == before
    assert h.agora_of(store.read_entry(paths.outbox / ulid))["raw"]["md5"] == b1["md5"]


def test_a_cursor_that_cannot_be_written_only_warns_and_the_next_round_cuts_whole(paths, monkeypatch, capsys):
    ulid, main1, b1 = _first(paths)
    real = store.write_atomic

    def fail_for_cursor(path, *a, **k):
        if "cursors" in str(path):
            raise OSError("read-only")
        return real(path, *a, **k)
    monkeypatch.setattr(store, "write_atomic", fail_for_cursor)
    main2 = main1 + _main(12, 4)
    b2 = _round(paths, ulid, main2 + b"T", len(main2), lines=2)
    assert "cursor 寫不進去" in capsys.readouterr().err
    monkeypatch.undo()
    # the cursor on disk still describes b1, not b2: the next round must not extend b2 with it
    main3 = main2 + _main(12, 5)
    b3 = _round(paths, ulid, main3 + b"T", len(main3), lines=3)
    assert _gens(b3) != _gens(b2)


def test_deleting_the_session_forgets_the_cursor(paths):
    ulid, main1, b1 = _first(paths)
    assert store.read_cursor(paths, ulid) is not None
    store.forget_local(paths, ulid)
    assert store.read_cursor(paths, ulid) is None


def test_deleting_a_cursor_restarts_with_a_whole_cut_and_then_appends_again(env, paths):
    ulid, main1, b1 = _first(paths)
    _up(paths)
    store.forget_cursor(paths, ulid)
    main2 = main1 + _main(12, 4)
    b2 = _put(paths, ulid, main2 + b"T", len(main2), lines=2, update=True)     # no append_from
    assert _gens(b2) != _gens(b1)
    _up(paths)
    main3 = main2 + _main(12, 5)
    b3 = _round(paths, ulid, main3 + b"T", len(main3), lines=3)
    assert _gens(b3) == _gens(b2) and b3["segments"][:-1][:len(b2["segments"]) - 1] == b2["segments"][:-1]
    _up(paths)
    assert _joined(env, ulid) == main3 + b"T"


# --- 3.27: aux order -------------------------------------------------------------------------------


def test_the_sealed_part_does_not_depend_on_what_comes_after_it(paths):
    """Whatever the tail turns into, the sealed segments of an append are the earlier ones."""
    ulid, main1, b1 = _first(paths)
    sealed = []
    for tail in (b"a", b"ab", b"x" * 5000):
        block = _put(paths, h.new_ulid(), main1 + tail, len(main1))
        sealed.append(block["segments"][:-1])
    assert sealed[0] == sealed[1] == sealed[2] == b1["segments"][:-1]


# --- 3.30: cross machine -----------------------------------------------------------------------------


def _machine(tmp_path, monkeypatch, name):
    """Another machine over the same Drive: its own cache, state and config."""
    for key, sub in (("AGORA_CACHE_DIR", "cache"), ("AGORA_STATE_DIR", "state"), ("AGORA_CONFIG", "config")):
        d = tmp_path / f"{name}-{sub}"
        d.mkdir(exist_ok=True)
        monkeypatch.setenv(key, str(d))
    return store.Paths.from_env()


def test_case1_a_writes_and_b_continues_with_an_append_of_its_own(env, paths, tmp_path, monkeypatch):
    ulid, main1, b1 = _first(paths, 40)
    _up(paths)
    pb = _machine(tmp_path, monkeypatch, "b")
    (pb.mirror / ulid).mkdir(parents=True)                       # B pulled A's version
    shutil.copyfile(_drive_folder(env, ulid) / "session.md", pb.mirror / ulid / "session.md")
    # B has no cursor for this session: its first write is cut whole (the next generation) ...
    main2 = main1 + _main(10, 2)
    b2 = _put(pb, ulid, main2 + b"B1", len(main2), lines=2, update=True)
    assert _gens(b2) != _gens(b1)
    store.upload_batch(store.Drive(pb), pb)
    assert _joined(env, ulid) == main2 + b"B1"
    # ... and its second is an append: only the new segments go up
    main3 = main2 + _main(10, 3)
    b3 = _round(pb, ulid, main3 + b"B2", len(main3), lines=3)
    assert b3["segments"][:len(b2["segments"]) - 1] == b2["segments"][:-1]
    mark = _mark(env)
    store.upload_batch(store.Drive(pb), pb)
    assert not {s["file"] for s in b2["segments"][:-1]} & set(_sent(env, mark))
    assert _joined(env, ulid) == main3 + b"B2"


def test_case2_a_cleans_the_base_segments_b_appended_to_and_b_cuts_them_again(env, paths, tmp_path, monkeypatch):
    ulid, main1, b1 = _first(paths, 40)
    _up(paths)
    pb = _machine(tmp_path, monkeypatch, "b")
    main2 = main1 + _main(10, 2)
    b2 = _put(pb, ulid, main2 + b"B1", len(main2), lines=2, update=True)
    store.upload_batch(store.Drive(pb), pb)
    main3 = main2 + _main(10, 3)
    b3 = _round(pb, ulid, main3 + b"B2", len(main3), lines=3)      # staged on top of b2, not sent yet
    # meanwhile another machine's cleanup took the base segments b3 builds on
    victims = [s["file"] for s in b2["segments"][:2]]
    for name in victims:
        (_drive_folder(env, ulid) / name).unlink()
    mark = _mark(env)
    assert store.upload_batch(store.Drive(pb), pb) == []
    assert set(victims) <= set(_sent(env, mark)), "the missing base is cut again from the whole raw"
    assert _joined(env, ulid) == main3 + b"B2"
    assert [s["file"] for s in _manifest(env, ulid)] == [s["file"] for s in b3["segments"]]


def test_case3_the_other_machines_tail_change_and_regeneration_are_not_a_delete(env, paths, tmp_path, monkeypatch):
    ulid, main1, b1 = _first(paths, 40)
    _up(paths)
    pb = _machine(tmp_path, monkeypatch, "b")
    store.upload_batch(store.Drive(pb), pb)        # B knows the folder
    main2 = main1 + _main(10, 2)
    _put(pb, ulid, main2 + b"B1", len(main2), lines=2, update=True)     # a whole cut: every name is new
    store.upload_batch(store.Drive(pb), pb)
    # back on A: its next version is staged on a Drive whose names all changed under it
    rescued = []
    real = store._rescue_deleted
    monkeypatch.setattr(store, "_rescue_deleted", lambda *a, **k: rescued.append(a) or real(*a, **k))
    pa = _machine(tmp_path, monkeypatch, "a2")
    main3 = main2 + _main(5, 5)
    _put(pa, ulid, main3 + b"A2", len(main3), lines=3, update=True)
    assert store.upload_batch(store.Drive(pa), pa) == []
    assert _joined(env, ulid) == main3 + b"A2"
    assert _drive_folder(env, ulid).is_dir() and not rescued


def test_a_second_machines_whole_cut_after_a_regeneration_still_converges(env, paths, tmp_path, monkeypatch):
    """3.30: both write the same session in turn; the last writer wins and Drive is whole."""
    ulid, main1, b1 = _first(paths, 40)
    _up(paths)
    pb = _machine(tmp_path, monkeypatch, "b")
    mainb = main1 + _main(8, 6)
    _put(pb, ulid, mainb + b"B", len(mainb), lines=2, update=True)
    store.upload_batch(store.Drive(pb), pb)
    maina = main1 + _main(8, 7)
    _put(paths, ulid, maina + b"A", len(maina), lines=2, update=True,
         append_from=store.read_cursor(paths, ulid)["raw_stable"])
    monkeypatch.setenv("AGORA_STATE_DIR", str(paths.state))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(paths.cache))
    assert store.upload_batch(store.Drive(paths), paths) == []
    assert _joined(env, ulid) == maina + b"A"


# --- the leftovers of R3b-1 ----------------------------------------------------------------------


@pytest.mark.parametrize("mtimes", [{}, {"seg-0001-0000-aaaaaaaaaaaa.part": ""},
                                    {"seg-0001-0000-aaaaaaaaaaaa.part": "garbage"},
                                    {"seg-0001-0000-aaaaaaaaaaaa.part": "2026-13-45T99:99:99Z"},
                                    {"seg-0001-0000-aaaaaaaaaaaa.part": None}])
def test_cleanup_keeps_a_segment_it_cannot_date(paths, monkeypatch, mtimes):
    ulid = h.new_ulid()
    deleted = []
    monkeypatch.setattr(store, "_delete_batch", lambda drive, paths_, names: deleted.extend(names))
    remote = types.SimpleNamespace(mtimes={ulid: mtimes})
    manifest = [{"file": "seg-0002-0000-bbbbbbbbbbbb.part", "size": 1, "md5": "b" * 32}]
    files = {"session.md": "m", "seg-0002-0000-bbbbbbbbbbbb.part": "b" * 32,
             "seg-0001-0000-aaaaaaaaaaaa.part": "a" * 32}
    store._clean_segments(None, paths, [(ulid, manifest, files)], remote, {ulid: {"session.md": "m"}},
                          lambda *_: None)
    assert deleted == []


def test_cleanup_deletes_the_same_segment_once_it_is_old_enough(paths, monkeypatch):
    ulid = h.new_ulid()
    deleted = []
    monkeypatch.setattr(store, "_delete_batch", lambda drive, paths_, names: deleted.extend(names))
    remote = types.SimpleNamespace(mtimes={ulid: {"seg-0001-0000-aaaaaaaaaaaa.part": "2020-01-01T00:00:00Z"}})
    manifest = [{"file": "seg-0002-0000-bbbbbbbbbbbb.part", "size": 1, "md5": "b" * 32}]
    files = {"session.md": "m", "seg-0002-0000-bbbbbbbbbbbb.part": "b" * 32,
             "seg-0001-0000-aaaaaaaaaaaa.part": "a" * 32}
    store._clean_segments(None, paths, [(ulid, manifest, files)], remote, {ulid: {"session.md": "m"}},
                          lambda *_: None)
    assert deleted == [f"{ulid}/seg-0001-0000-aaaaaaaaaaaa.part"]


@pytest.mark.parametrize("text,ok", [("2026-10-02T00:00:00Z", True), ("2026-10-02T00:00:00.123456789+08:00", True),
                                     ("2026-10-02T00:00:00", True), ("garbage", False), ("", False),
                                     ("2026-13-45T00:00:00Z", False), (None, False), (5, False)])
def test_iso_epoch_reads_rclone_times_and_says_none_to_the_rest(text, ok):
    assert (store._iso_epoch(text) is not None) is ok


def test_an_unlistable_drive_leaves_the_entry_and_says_why(env, paths, monkeypatch, capsys):
    ulid, main1, b1 = _first(paths)
    monkeypatch.setattr(store, "_listing_with_md5", lambda drive: None)
    assert store.upload_batch(store.Drive(paths), paths) == [ulid]
    assert "沒有 sessions/ 可以確認" in capsys.readouterr().err
    assert (paths.outbox / ulid).is_dir()
