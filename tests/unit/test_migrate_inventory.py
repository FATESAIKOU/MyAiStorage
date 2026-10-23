"""R4: `agora migrate inventory` - read-only, metadata only, fail-closed (design G.2, F.1.1).

The index and Drive are built by hand: a Session is a header put into the local index plus the
files it names on the fake Drive. Ids are made up (`2T` + digits) and look like no real id.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import hashlib
import json
import socket
import sqlite3
import sys
from pathlib import Path

import pytest

from agora import cli, header as h, store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
HOST = socket.gethostname()
TITLE, BODY, TAG = "機密標題ZZ", "機密內文ZZ", "機密標籤ZZ"
SESSION_KEYS = {"ulid", "agent", "external_id", "host", "relation", "layout", "raw_size", "class",
                "replacement", "old_children", "lineage", "must_keep", "lineage_protected",
                "continuing", "outbox_legacy", "droppable", "not_droppable_reason"}


def U(n: int) -> str:
    return f"2T{n:024d}"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
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
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(tmp_path / "chome"))
    monkeypatch.setenv("AGORA_AGY_HOME", str(tmp_path / "no-agy"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-opencode"))
    monkeypatch.delenv("AGORA_FOLDER_NAME", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "chome" / ".claude" / "projects" / "-proj").mkdir(parents=True)
    return World(tmp_path, remote)


class World:
    def __init__(self, tmp: Path, remote: Path):
        self.tmp, self.remote = tmp, remote
        self.paths = store.Paths.from_env()
        store.Drive(self.paths).folder_id()             # the machine has talked to Drive before
        self.index = store.Index(self.paths)
        self.calls_mark = len(self.calls())

    def calls(self) -> list[list[str]]:
        log = self.remote.parent / "calls.log"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def local_source(self, sid: str, present: bool = True) -> None:
        path = self.tmp / "chome" / ".claude" / "projects" / "-proj" / f"{sid}.jsonl"
        if present and not path.exists():
            path.write_text("{}\n")

    def folder(self, ulid: str) -> Path:
        d = self.remote / "agora" / "sessions" / ulid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put(self, ulid: str, header: dict, *, on_drive: bool = True, files: dict | None = None,
            index: bool = True) -> bytes:
        text = h.dump_document(header, BODY)
        data = text.encode("utf-8")
        if on_drive:
            d = self.folder(ulid)
            (d / "session.md").write_bytes(data)
            for name, content in (files or {}).items():
                (d / name).write_bytes(content)
        if index:
            self.index.put(ulid, md5(data), header, BODY)
        return data

    def old(self, ulid: str, sid: str, raw: bytes = b"old raw", *, host=HOST, agent="claude", relation="import",
            parents=(), present=True, source=True, on_drive=True) -> bytes:
        name = f"raw-{md5(raw)[:12]}.json"
        hdr = header(ulid, relation=relation, parents=parents,
                     raw={"file": name, "md5": md5(raw), "size": len(raw)},
                     source={"agent": agent, "session_id": sid, "dir": "/proj", **({"host": host} if host else {})}
                     if source else None)
        if source and agent == "claude" and host == HOST:
            self.local_source(sid, present)
        return self.put(ulid, hdr, on_drive=on_drive, files={name: raw})

    def new(self, ulid: str, sid: str, raw: bytes = b"old raw", *, on_drive=True, missing_segment=False,
            in_index=True) -> bytes:
        segs = [(raw[:3], 0), (raw[3:], 1)] if len(raw) > 3 else [(raw, 0)]
        manifest = [{"file": store.segment_name(1, n, md5(part)), "size": len(part), "md5": md5(part)}
                    for part, n in segs]
        files = {m["file"]: part for m, (part, _n) in zip(manifest, segs)}
        if missing_segment:
            files.pop(manifest[-1]["file"])
        hdr = header(ulid, relation="import",
                     raw={"md5": md5(raw), "size": len(raw), "layout": "chunks/1", "segments": manifest},
                     source={"agent": "claude", "session_id": sid, "dir": "/proj", "host": HOST})
        return self.put(ulid, hdr, on_drive=on_drive, files=files, index=in_index)

    def outbox(self, ulid: str, *, legacy: bool, done: bool = False, prefix: str | None = None) -> None:
        data = b'{"old": true}'
        raw = ({"file": f"raw-{md5(data)[:12]}.json", "md5": md5(data), "size": len(data)} if legacy else
               {"md5": "b" * 32, "size": 1, "layout": "chunks/1", "segments": []})
        folder = self.paths.outbox / ((prefix if prefix is not None else (".done-" if done else "")) + ulid)
        folder.mkdir(parents=True)
        if legacy:
            (folder / raw["file"]).write_bytes(data)
        base = self.index.header(ulid) or header(ulid)             # a newer version of the same Session
        base["agora"]["raw"] = raw
        (folder / "session.md").write_text(h.dump_document(base, BODY), encoding="utf-8")

    def run(self, capsys, *argv):
        code = cli.main(["migrate", "inventory", "--json", *argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    def report(self, capsys, *argv) -> dict:
        code, out, err = self.run(capsys, *argv)
        assert code == 0, err
        return json.loads(out)


def header(ulid: str, *, relation="import", parents=(), raw=None, source=None) -> dict:
    agora = {"header": 2, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
             "relation": relation, "parents": [{"id": f"agora:{p}", "raw_md5": None} for p in parents]}
    if source is not None:
        agora["source"] = source
    if raw is not None:
        agora["raw"] = raw
    return {"type": "Session", "title": TITLE, "tags": [TAG], "id": f"agora:{ulid}", "refs": [], "case": None,
            "description": BODY, "agora": agora}


def by_id(report: dict) -> dict:
    return {s["ulid"]: s for s in report["sessions"]}


# --- 4.2: classes and shape ----------------------------------------------------------------------


def test_every_class_is_reported_and_only_metadata_is_printed(world, capsys):
    world.old(U(1), "s-present")                                                   # local-present
    world.old(U(2), "s-missing", present=False)                                    # local-missing
    world.old(U(3), "s-other", host="some-other-machine")                          # other-machine
    world.old(U(4), "s-nohost", host=None)                                         # host-unknown
    world.old(U(5), "s-nosrc", source=False, relation="merge")                     # no-source
    world.old(U(6), "s-agy", agent="agy")                                          # agent-unavailable
    world.new(U(7), "s-new")                                                       # new-format
    world.folder(U(8)); (world.folder(U(8)) / "session.md").write_text("x")        # drive-only
    code, out, err = world.run(capsys)
    assert code == 0, err
    report = json.loads(out)
    got = {u: s["class"] for u, s in by_id(report).items()}
    assert got == {U(1): "local-present", U(2): "local-missing", U(3): "other-machine", U(4): "host-unknown",
                   U(5): "no-source", U(6): "agent-unavailable", U(7): "new-format", U(8): "drive-only"}
    assert set(report) == {"host", "counts", "drop_list", "must_keep_deleted", "sessions"}
    assert report["counts"]["local-present"] == 1 and sum(report["counts"].values()) == 8
    assert all(set(s) == SESSION_KEYS for s in report["sessions"])
    for secret in (TITLE, BODY, TAG, "raw-"):
        assert secret not in out and secret not in err
    assert by_id(report)[U(8)]["agent"] is None, "drive-only is a ULID and nothing else"
    assert by_id(report)[U(7)]["droppable"] is False
    assert by_id(report)[U(7)]["not_droppable_reason"] == "class-not-local-present"
    assert U(7) not in report["drop_list"]


def test_the_human_version_has_numbers_and_ids_only(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    assert cli.main(["migrate", "inventory"]) == 0
    out = capsys.readouterr().out
    assert U(1) in out and "drop_list (1)" in out
    for secret in (TITLE, BODY, TAG):
        assert secret not in out


def test_inventory_only_lists_drive_and_never_copies_syncs_or_writes(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    mirror = world.paths.mirror / U(1)
    mirror.mkdir(parents=True)
    (mirror / "session.md").write_text(h.dump_document(header(U(1)), BODY), encoding="utf-8")
    before = {p: p.stat().st_mtime_ns for p in world.tmp.rglob("*") if p.is_file() and "remote" not in p.parts and "snapshots" not in p.parts and p.name != "calls.log"}
    world.report(capsys)
    known = {"copy", "copyto", "move", "moveto", "delete", "deletefile", "purge", "mkdir", "lsjson", "cat",
             "rmdirs", "sync", "touch", "copyurl"}
    verbs = {a for call in world.calls()[world.calls_mark:] for a in call if a in known}
    assert verbs == {"lsjson"}, verbs
    after = {p: p.stat().st_mtime_ns for p in world.tmp.rglob("*") if p.is_file() and "remote" not in p.parts and "snapshots" not in p.parts and p.name != "calls.log"}
    assert {p: t for p, t in after.items() if p in before} == before
    assert set(after) == set(before), "no new file anywhere"


def test_a_wrong_index_version_is_an_error_not_a_rebuild(world, capsys):
    world.old(U(1), "s1")
    mirror = world.paths.mirror / U(1)
    mirror.mkdir(parents=True)
    (mirror / "session.md").write_text(h.dump_document(header(U(1)), BODY), encoding="utf-8")
    world.index.db.execute(f"PRAGMA user_version = {store.INDEX_VERSION + 7}")
    world.index.db.commit()
    code, out, err = world.run(capsys)
    assert code != 0 and out == "" and "先跑一次一般指令（例如 agora search session）" in err
    db = sqlite3.connect(world.paths.cache / "index.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == store.INDEX_VERSION + 7, "not rebuilt"
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_no_index_or_no_drive_folder_is_an_error_and_creates_nothing(tmp_path, monkeypatch, capsys):
    for key, sub in (("AGORA_CONFIG", "c"), ("AGORA_CACHE_DIR", "k"), ("AGORA_STATE_DIR", "s")):
        monkeypatch.setenv(key, str(tmp_path / sub))
    assert cli.main(["migrate", "inventory", "--json"]) != 0
    assert not (tmp_path / "k").exists(), "no index was created"


# --- 4.2: replacement ------------------------------------------------------------------------------


def test_an_identical_replacement_on_drive_is_droppable(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    report = world.report(capsys)
    old = by_id(report)[U(1)]
    assert old["replacement"] == {"ulid": U(2), "relation_to_old": "identical", "raw_size": 7, "reason": "md5-equal"}
    assert old["droppable"] is True and old["not_droppable_reason"] is None
    assert report["drop_list"] == [U(1)]


def test_a_shorter_original_re_imported_is_different_and_never_dropped(world, capsys):
    world.old(U(1), "s1", raw=b"the whole original")
    world.new(U(2), "s1", raw=b"the whole")                     # the source was cut down, then re-imported
    report = world.report(capsys)
    old = by_id(report)[U(1)]
    assert old["replacement"]["relation_to_old"] == "different" and old["replacement"]["reason"] == "md5-differs"
    assert (old["droppable"], old["not_droppable_reason"]) == (False, "relation-different")
    assert report["drop_list"] == []


def test_a_replacement_still_in_the_outbox_is_not_identical(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1", on_drive=False)                        # indexed, session.md not on Drive
    old = by_id(world.report(capsys))[U(1)]
    assert old["replacement"]["relation_to_old"] == "different"
    assert old["replacement"]["reason"] == "drive-version-elsewhere"
    assert old["droppable"] is False


def test_a_replacement_with_a_segment_missing_on_drive_is_not_identical(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1", missing_segment=True)
    report = world.report(capsys)
    old = by_id(report)[U(1)]
    assert old["replacement"]["relation_to_old"] == "different"
    assert old["replacement"]["reason"] == "segments-missing"
    assert report["drop_list"] == []


def test_the_old_one_changed_on_drive_or_gone_from_it_is_not_identical(world, capsys):
    world.old(U(1), "s1")
    world.old(U(3), "s3", on_drive=False)
    world.new(U(2), "s1")
    world.new(U(4), "s3")
    (world.folder(U(1)) / "session.md").write_text("another machine wrote this")      # Drive moved on
    report = by_id(world.report(capsys))
    assert report[U(1)]["replacement"]["reason"] == "old-drive-version-elsewhere"
    assert report[U(3)]["replacement"]["reason"] == "old-not-on-drive"
    assert not report[U(1)]["droppable"] and not report[U(3)]["droppable"]


def test_no_replacement_and_incomparable_ones(world, capsys):
    world.old(U(1), "s1")                                           # nothing re-imported yet
    world.old(U(3), "s3")
    hdr = header(U(5), source={"agent": "claude", "session_id": "s5", "dir": "/proj", "host": HOST})
    world.put(U(5), hdr, files={})                                  # a Session with no raw at all
    world.local_source("s5")
    world.new(U(6), "s5")
    report = by_id(world.report(capsys))
    assert report[U(1)]["replacement"] is None
    assert report[U(1)]["not_droppable_reason"] == "no-replacement"
    assert report[U(5)]["replacement"]["relation_to_old"] is None
    assert report[U(5)]["replacement"]["reason"] == "old-has-no-raw"
    assert report[U(5)]["not_droppable_reason"] == "relation-null"
    assert report[U(5)]["layout"] == "none"


# --- 4.2: lineage ------------------------------------------------------------------------------------


def _family(world):
    world.old(U(1), "s-parent")
    world.old(U(2), "s-child", relation="continue", parents=[U(1)])
    world.new(U(3), "s-parent")
    world.new(U(4), "s-child")


def test_a_whole_lineage_that_passes_is_dropped_together_and_sorted(world, capsys):
    _family(world)
    report = world.report(capsys)
    assert report["drop_list"] == [U(1), U(2)]
    s = by_id(report)
    assert s[U(1)]["old_children"] == [U(2)] and s[U(2)]["old_children"] == []
    assert s[U(1)]["lineage"] == s[U(2)]["lineage"] == [U(1), U(2)]


def test_one_member_that_differs_keeps_the_whole_lineage(world, capsys):
    _family(world)
    (world.folder(U(4)) / "session.md").write_text("elsewhere")          # the child's replacement is stale
    world.index.db.execute("UPDATE sessions SET md5=? WHERE ulid=?", ("x" * 32, U(4)))
    world.index.db.commit()
    report = world.report(capsys)
    s = by_id(report)
    assert s[U(2)]["not_droppable_reason"] == "relation-different"
    assert s[U(1)]["not_droppable_reason"] == "lineage-member-not-droppable"
    assert report["drop_list"] == []


def test_a_member_on_another_machine_keeps_the_lineage(world, capsys):
    world.old(U(1), "s-parent")
    world.old(U(2), "s-child", relation="continue", parents=[U(1)], host="elsewhere")
    world.new(U(3), "s-parent")
    s = by_id(world.report(capsys))
    assert s[U(1)]["not_droppable_reason"] == "lineage-member-not-droppable"
    assert s[U(2)]["not_droppable_reason"] == "class-not-local-present"


def test_a_session_drive_no_longer_has_is_not_a_child(world, capsys):
    _family(world)
    world.index.db.execute("INSERT INTO cloud_missing VALUES (?)", (U(2),))
    world.index.db.commit()
    assert by_id(world.report(capsys))[U(1)]["old_children"] == []


# --- 4.2: the outbox and continuing ----------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["", ".done-", ".old-"])
def test_a_pre_upgrade_entry_stuck_in_the_outbox_blocks_the_session(world, capsys, prefix):
    """A plain entry, a `.done-` set aside and a `.old-` a crashed stage left: the next command puts
    each of them back in the outbox, so each is a stuck entry now."""
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    world.outbox(U(1), legacy=True, prefix=prefix)
    report = world.report(capsys)
    s = by_id(report)[U(1)]
    assert s["outbox_legacy"] is True and s["droppable"] is False and s["not_droppable_reason"] == "outbox-legacy"
    assert report["drop_list"] == []


def test_inventory_names_the_same_entries_as_the_commands_that_repair_and_repairs_nothing(world):
    """`outbox_folders` and `waiting_ulids` must not drift apart: same ULIDs for every shape, and the
    read-only one renames nothing."""
    ob = world.paths.outbox
    for ulid, prefix in ((U(1), ""), (U(2), ".done-"), (U(3), ".old-"), (U(5), ".old-")):
        world.outbox(ulid, legacy=True, prefix=prefix)
    (ob / f".tmp-{U(5)}").mkdir()                      # a stage in flight for U(5): its .old- stays aside
    (ob / f".old-{U(6)}").mkdir()
    (ob / U(6)).mkdir()                                # U(6) has its entry: the .old- is not another one
    (ob / "notes.txt").write_text("x")
    names_before = sorted(p.name for p in ob.iterdir())
    folders = store.outbox_folders(world.paths)
    assert sorted(p.name for p in ob.iterdir()) == names_before, "nothing was renamed"
    assert set(folders) == {U(1), U(2), U(3), U(6)}
    assert set(folders) == store.waiting_ulids(world.paths), "the repairing function agrees (and repairs)"
    assert (ob / U(3)).is_dir() and not (ob / f".old-{U(3)}").exists()


def test_a_new_format_outbox_entry_or_none_is_not_outbox_legacy(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    world.outbox(U(1), legacy=False)
    s = by_id(world.report(capsys))[U(1)]
    assert s["outbox_legacy"] is False and s["droppable"] is True


def test_a_session_being_continued_here_is_not_droppable(world, capsys, monkeypatch):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    monkeypatch.setattr(store, "continuing_details", lambda paths, ulid: {} if ulid == U(1) else None)
    s = by_id(world.report(capsys))[U(1)]
    assert s["continuing"] is True and s["not_droppable_reason"] == "continuing"


# --- 4.4: the must-keep list ---------------------------------------------------------------------------


def listfile(world, text: str | bytes, name="keep.txt") -> str:
    d = world.tmp / "outside"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    p.chmod(0o600)
    return str(p)


def nowhere_in_the_world(world, needle: str, capsys_text: str = "") -> bool:
    if needle in capsys_text:
        return False
    for p in world.tmp.rglob("*"):
        if p.is_file() and "outside" not in p.parts and "snapshots" not in p.parts:
            try:
                if needle.encode() in p.read_bytes():
                    return False
            except OSError:
                pass
    return True


def test_a_listed_session_whose_replacement_differs_keeps_its_whole_lineage(world, capsys):
    _family(world)
    world.old(U(5), "s-other")
    world.new(U(6), "s-other")
    (world.folder(U(4)) / "session.md").write_text("stale")
    world.index.db.execute("UPDATE sessions SET md5=? WHERE ulid=?", ("x" * 32, U(4)))
    world.index.db.commit()
    report = world.report(capsys, "--must-keep", listfile(world, f"agora:{U(2)}\n"))
    s = by_id(report)
    assert s[U(2)]["must_keep"] is True and s[U(1)]["must_keep"] is False
    assert s[U(1)]["lineage_protected"] and s[U(2)]["lineage_protected"] and not s[U(5)]["lineage_protected"]
    assert (s[U(2)]["droppable"], s[U(2)]["not_droppable_reason"]) == (False, "relation-different")
    assert s[U(1)]["not_droppable_reason"] == "lineage-member-not-droppable"
    assert report["drop_list"] == [U(5)], "the other lineage is untouched, this one is whole out"


def test_a_listed_session_being_continued_keeps_its_lineage_and_is_flagged(world, capsys, monkeypatch):
    _family(world)
    monkeypatch.setattr(store, "continuing_details", lambda paths, ulid: {} if ulid == U(2) else None)
    report = world.report(capsys, "--must-keep", listfile(world, U(2) + "\n"))
    s = by_id(report)
    assert s[U(2)]["continuing"] is True and s[U(2)]["not_droppable_reason"] == "continuing"
    assert report["drop_list"] == []


def test_a_listed_lineage_that_passes_everywhere_is_dropped_whole(world, capsys):
    _family(world)
    report = world.report(capsys, "--must-keep", listfile(world, f"# keep these\n\n{U(1)}\n"))
    assert report["drop_list"] == [U(1), U(2)]
    assert by_id(report)[U(2)]["lineage_protected"] is True and by_id(report)[U(2)]["must_keep"] is False


def test_a_mistyped_id_stops_everything_and_gives_no_candidates(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    code, out, err = world.run(capsys, "--must-keep", listfile(world, f"{U(1)}\n{U(99)}\n"))
    assert code != 0 and out == "" and U(99) in err and U(1) not in err
    assert "drop_list" not in out and "sessions" not in out


def test_an_id_in_the_tombstones_is_deleted_not_an_error(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    (world.paths.state).mkdir(parents=True, exist_ok=True)
    (world.paths.state / "deleted").write_text(U(9) + "\n")
    report = world.report(capsys, "--must-keep", listfile(world, f"agora:{U(9)}\n{U(1)}\n"))
    assert report["must_keep_deleted"] == [U(9)]
    assert U(9) not in by_id(report) and U(9) not in report["drop_list"]
    assert report["drop_list"] == [U(1)], "the rest of the list works as usual"


def test_without_the_flag_must_keep_deleted_is_empty(world, capsys):
    world.old(U(1), "s1")
    report = world.report(capsys)
    assert report["must_keep_deleted"] == []
    assert all(s["must_keep"] is False and s["lineage_protected"] is False for s in report["sessions"])


@pytest.mark.parametrize("what", ["missing", "directory", "not-utf8"])
def test_a_list_that_cannot_be_read_gives_an_error_and_no_candidates(world, capsys, what):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    if what == "missing":
        path = str(world.tmp / "outside" / "nope.txt")
    elif what == "directory":
        path = str(world.tmp)
    else:
        path = listfile(world, b"\xff\xfe\x00bad\n")
    code, out, err = world.run(capsys, "--must-keep", path)
    assert code != 0 and out == ""
    assert path in err


@pytest.mark.parametrize("text", ["", "# only a comment\n\n   \n", "﻿\r\n\r\n"])
def test_a_list_with_no_id_is_an_error_not_an_empty_list(world, capsys, text):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    code, out, err = world.run(capsys, "--must-keep", listfile(world, text))
    assert code != 0 and out == "" and "0 個 id" in err and "不要給 --must-keep" in err


def test_a_line_that_is_not_an_id_is_reported_by_number_and_never_printed(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    text = "\n".join([U(1), "secretlowercase" + U(1).lower(), "# note", "ＳＥＣＲＥＴ全形" + U(1),
                      f"claude:{U(1)}", U(1).lower(), f"{U(1)} SECRETTAIL", ""])
    code, out, err = world.run(capsys, "--must-keep", listfile(world, text))
    assert code != 0 and out == ""
    assert "第 2、4、5、6、7 行" in err
    for needle in ("secretlowercase", "ＳＥＣＲＥＴ", "claude:", "SECRETTAIL", U(1).lower()):
        assert nowhere_in_the_world(world, needle, out + err), needle


def test_bom_crlf_spaces_and_the_two_spellings_are_one_id(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    text = f"﻿# header\r\n  agora:{U(1)}  \r\n\t{U(1)}\t\r\n\r\n"
    report = world.report(capsys, "--must-keep", listfile(world, text))
    assert [s["ulid"] for s in report["sessions"] if s["must_keep"]] == [U(1)]


def test_the_list_is_not_copied_anywhere(world, capsys):
    world.old(U(1), "s1")
    world.new(U(2), "s1")
    path = listfile(world, f"{U(1)}\n")
    world.report(capsys, "--must-keep", path)
    assert Path(path).read_text() == f"{U(1)}\n"
    for p in world.tmp.rglob("*"):
        if p.is_file() and "outside" not in p.parts and p.suffix not in (".sqlite",) and "snapshots" not in p.parts:
            assert "keep.txt" not in p.read_bytes().decode("utf-8", "ignore"), p
