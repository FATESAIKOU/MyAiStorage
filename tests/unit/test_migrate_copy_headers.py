"""#109: `agora migrate copy-headers` - the user's fields of an old Session onto its `--as-new` re-import.

Fake rclone, made-up ids (`2T` + digits), a fake claude home. The secret strings below must never
reach the output of a dry run.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from agora import cli, header as h, migrate, store
from test_migrate_inventory import BODY, U, world  # noqa: F401
from test_migrate_flow import SID, _import, _legacy_from_source, _source

SECRET_TITLE, SECRET_DESC, SECRET_EXTRA = "手改的標題QQ", "手寫的描述QQ", "自訂值QQ"
CASE, REFS = "mybrain:case-x", ["foundry:repo-x@main"]


def _rich(world, ulid: str, *, tags=("keep-me",), updated="2026-10-01T00:00:00Z", **extra) -> None:
    """Give the old Session in the index the fields a person would have put on it."""
    hdr = world.index.header(ulid)
    hdr.update({"title": SECRET_TITLE, "description": SECRET_DESC, "case": CASE, "refs": list(REFS),
                "verified": ["foundry:v-x"], "custom_field": SECRET_EXTRA, "tags": list(tags),
                "sources": [{"id": "x:1", "title": "old"}], "generated": {"by": "human:me", "at": "2026-09-01T00:00:00Z"},
                **extra})
    hdr["agora"]["updated_at"] = updated
    world.put(ulid, hdr)                    # Drive and the index both: a sync would put Drive's header back


def _inventory_file(world, capsys) -> str:
    code = cli.main(["migrate", "inventory", "--json"])
    out = capsys.readouterr()
    assert code == 0, out.err
    path = world.tmp / "inventory.json"
    path.write_text(out.out)
    return str(path)


def _copy(capsys, inv: str, *flags):
    code = cli.main(["migrate", "copy-headers", "--inventory", inv, *flags])
    out = capsys.readouterr()
    return code, out.out, out.err


def _setup(world, capsys, *olds, sid=1):
    _source(world, SID[sid])
    for n in olds:
        _legacy_from_source(world, U(n), SID[sid])
    for n in olds:
        _rich(world, U(n))
    code, out = _import(capsys, SID[sid])
    assert code in (0, 3) and out.out.startswith("agora:"), out.err
    return out.out.strip().split(":")[1]


def _new_header(new: str) -> dict:
    return store.Index(store.Paths.from_env()).header(new)


def _drive_state(world, ulid: str) -> dict:
    d = world.remote / "agora" / "sessions" / ulid
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.iterdir()}


def test_dry_run_prints_no_title_and_writes_nothing_then_apply_copies_field_by_field(world, capsys):
    new = _setup(world, capsys, 1)
    before = _new_header(new)
    drive_before = _drive_state(world, new)
    inv = _inventory_file(world, capsys)
    mark = len(world.calls())

    code, out, err = _copy(capsys, inv)
    assert code == 0, err
    for secret in (SECRET_TITLE, SECRET_DESC, SECRET_EXTRA):
        assert secret not in out + err
    assert f"agora:{new} ← agora:{U(1)}" in out
    for name in ("title", "description", "case", "refs", "verified", "custom_field", "tags"):
        assert name in out, name
    assert CASE in out and "migrated-from-" + U(1) in out
    assert "待寫 1" in out and "乾跑" in out
    assert _new_header(new) == before and _drive_state(world, new) == drive_before
    assert len(world.calls()) == mark, "a dry run with --inventory does not touch Drive"

    segs_before = {k: v for k, v in drive_before.items() if k.endswith(".part")}
    assert segs_before
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0, err
    assert "寫了 1、跳過（已一致）0、不抄 0、失敗 0" in out
    for secret in (SECRET_TITLE, SECRET_DESC, SECRET_EXTRA):
        assert secret not in out + err
    after = _new_header(new)
    assert (after["title"], after["description"], after["case"], after["refs"]) == (SECRET_TITLE, SECRET_DESC, CASE, REFS)
    assert after["verified"] == ["foundry:v-x"] and after["custom_field"] == SECRET_EXTRA
    assert f"migrated-from-{U(1)}" in after["tags"] and "keep-me" in after["tags"]
    assert after["sources"] == before["sources"] and after["generated"] == before["generated"]
    a0, a1 = dict(before["agora"]), dict(after["agora"])
    a0.pop("updated_at"), a1.pop("updated_at")
    assert a0 == a1 and a1["parents"] == [] and a1["relation"] == "import"
    drive_after = _drive_state(world, new)
    assert {k: v for k, v in drive_after.items() if k.endswith(".part")} == segs_before, "no segment was sent again"
    assert drive_after["session.md"] != drive_before["session.md"]

    # a second run is a no-op
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0 and "寫了 0、跳過（已一致）1、不抄 0、失敗 0" in out
    assert _drive_state(world, new) == drive_after
    assert _copy(capsys, inv, "--verify")[0] == 0


def test_verify_says_so_when_something_is_not_copied_yet(world, capsys):
    _setup(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    code, out, _ = _copy(capsys, inv, "--verify")
    assert code != 0 and "待寫 1" in out


def test_a_tag_or_field_that_points_at_the_old_id_is_not_carried(world, capsys):
    new = _setup(world, capsys)                       # no old yet: only the re-import
    _source(world, SID[2])
    _legacy_from_source(world, U(2), SID[2])
    _rich(world, U(2), tags=("keep-me", f"superseded-by-{U(2)}", f"see-{U(2)}", "superseded-by-2T999"),
          refs=[f"foundry:{U(2)}"])
    code, out = _import(capsys, SID[2])
    new2 = out.out.strip().split(":")[1]
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR, err      # something is lost or broken: the run says so
    got = _new_header(new2)
    assert f"superseded-by-{U(2)}" not in got["tags"] and f"see-{U(2)}" not in got["tags"]
    assert "superseded-by-2T999" not in got["tags"], "superseded-by-* is never carried, whatever it points at"
    assert "keep-me" in got["tags"]
    assert f"migrated-from-{U(2)}" in got["tags"]
    assert got["refs"] == [] and "丟掉了（指向舊 id、沒有新 id）：refs: foundry:" + U(2) in out and "沒抄（刻意）：superseded-by-" in out
    assert new != new2


def test_the_old_parents_are_not_carried_and_the_old_can_still_be_deleted(world, capsys):
    new = _setup(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == 0
    assert _new_header(new)["agora"]["parents"] == []
    index = store.Index(store.Paths.from_env())
    assert index.children(U(1)) == [] and index.children(new) == []


def _two_olds(world, capsys, tags1, tags2):
    _source(world, SID[1])
    for n in (1, 2):
        _legacy_from_source(world, U(n), SID[1])
    _rich(world, U(1), tags=tags1, updated="2026-10-02T00:00:00Z")
    _rich(world, U(2), tags=tags2, updated="2026-10-01T00:00:00Z")
    code, out = _import(capsys, SID[1])
    assert out.out.startswith("agora:"), out.err
    return out.out.strip().split(":")[1]


def test_two_old_ones_copy_the_one_that_is_not_superseded_even_if_it_is_older(world, capsys):
    new = _two_olds(world, capsys, ("keep-1", f"superseded-by-{U(2)}"), ("keep-2",))
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0, err
    got = _new_header(new)
    assert f"migrated-from-{U(2)}" in got["tags"] and "keep-2" in got["tags"]
    assert "keep-1" not in got["tags"] and f"migrated-from-{U(1)}" not in got["tags"]
    assert U(1) in out and "只列出" in out


@pytest.mark.parametrize("tags1,tags2,reason", [
    (("a",), ("b",), "several-old-none-or-many-not-superseded"),
    ((f"superseded-by-{U(2)}",), (f"superseded-by-{U(1)}",), "several-old-all-superseded"),
])
def test_two_old_ones_that_cannot_be_told_apart_are_listed_and_nothing_is_copied(world, capsys, tags1, tags2, reason):
    new = _two_olds(world, capsys, tags1, tags2)
    before = _new_header(new)
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR, err      # something is lost or broken: the run says so
    assert reason in out and U(1) in out and U(2) in out and "不抄 1" in out
    assert _new_header(new) == before


def test_a_failed_pair_does_not_stop_the_next_and_the_run_fails(world, capsys, monkeypatch):
    new1 = _setup(world, capsys, 1, sid=1)
    new2 = _setup(world, capsys, 2, sid=2)
    inv = _inventory_file(world, capsys)
    real = cli._edit_with
    monkeypatch.setattr(cli, "_edit_with", lambda ulid, fields: (False, "exit 2：boom") if ulid == new1 else real(ulid, fields))
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR and "寫了 1" in out and "失敗 1" in out and f"agora:{new1} 沒寫成" in err
    assert f"migrated-from-{U(2)}" in _new_header(new2)["tags"]


def test_a_write_that_does_not_read_back_the_same_fails_the_check(world, capsys, monkeypatch):
    new = _setup(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    monkeypatch.setattr(cli, "_edit_with", lambda ulid, fields: (True, ""))       # says yes, writes nothing
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR and "核對不一致" in err and "失敗 1" in out


def test_without_an_inventory_file_it_takes_one_itself(world, capsys):
    new = _setup(world, capsys, 1)
    code = cli.main(["migrate", "copy-headers"])
    out = capsys.readouterr()
    assert code == 0 and f"agora:{new} ← agora:{U(1)}" in out.out


def _edit_inventory(world, inv: str, fn) -> str:
    data = json.loads(open(inv).read())
    fn(data)
    path = world.tmp / "inventory-edited.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_the_choice_among_old_ones_looks_at_the_index_not_only_at_the_inventory(world, capsys):
    """An inventory that names only the superseded one must not make it the one to copy."""
    new = _two_olds(world, capsys, ("keep-1", f"superseded-by-{U(2)}"), ("keep-2",))
    inv = _inventory_file(world, capsys)
    only_u1 = _edit_inventory(world, inv, lambda d: d.update(sessions=[s for s in d["sessions"] if s["ulid"] != U(2)]))
    code, out, err = _copy(capsys, only_u1, "--apply")
    assert code == cli.EXIT_ERROR, err      # something is lost or broken: the run says so
    got = _new_header(new)
    assert f"migrated-from-{U(2)}" in got["tags"] and "keep-2" in got["tags"]
    assert "keep-1" not in got["tags"] and f"migrated-from-{U(1)}" not in got["tags"]
    assert not any(t.startswith("superseded-by-") for t in got["tags"])


def test_a_tag_naming_another_old_session_is_not_carried_but_other_tags_are(world, capsys):
    _source(world, SID[1])
    _legacy_from_source(world, U(1), SID[1])
    _source(world, SID[3])
    _legacy_from_source(world, U(3), SID[3])           # another source's old Session: still about to go
    _rich(world, U(1), tags=("keep-1", f"see-{U(3)}", "plain"), refs=[f"foundry:{U(3)}"], custom_field=f"x {U(3)}")
    new = _import(capsys, SID[1])[1].out.strip().split(":")[1]
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR, err      # something is lost or broken: the run says so
    got = _new_header(new)
    assert f"see-{U(3)}" not in got["tags"] and {"keep-1", "plain"} <= set(got["tags"])
    assert got["refs"] == [] and "custom_field" not in got
    assert "refs" in out and "custom_field" in out
    assert f"tags: see-{U(3)}" in out, "a tag that was lost is listed with its value"


@pytest.mark.parametrize("bad", [{}, {"sessions": None}, {"sessions": {"x": 1}}, {"sessions": [1, "a"]}, []])
def test_an_inventory_of_the_wrong_shape_is_refused_cleanly(world, capsys, bad):
    new = _setup(world, capsys, 1)
    before = _new_header(new)
    path = world.tmp / "bad.json"
    path.write_text(json.dumps(bad))
    for flags in ((), ("--apply",), ("--verify",)):
        code, out, err = _copy(capsys, str(path), *flags)
        assert code == cli.EXIT_INPUT and "inventory" in err and "Traceback" not in err and out == ""
    assert _new_header(new) == before


def test_every_pair_that_does_not_hold_is_listed_as_not_copied_and_counted(world, capsys):
    new1 = _setup(world, capsys, 1, sid=1)
    new2 = _setup(world, capsys, 2, sid=2)
    new3 = _setup(world, capsys, 3, sid=3)
    inv = _inventory_file(world, capsys)

    def spoil(d):
        by = {s["ulid"]: s for s in d["sessions"]}
        by[U(1)]["replacement"]["ulid"] = new2              # another source's new Session
        by[U(2)]["replacement"]["ulid"] = "2T999999999999999999999999"   # not there at all
    spoiled = _edit_inventory(world, inv, spoil)
    before = {n: _new_header(n) for n in (new1, new2, new3)}
    code, out, err = _copy(capsys, spoiled, "--apply")
    assert code == cli.EXIT_ERROR, err      # something is lost or broken: the run says so
    assert "replacement-is-of-another-source" in out and "replacement-not-in-index" in out
    assert "總數 3：寫了 1、跳過（已一致）0、不抄 2、失敗 0" in out
    assert _new_header(new1) == before[new1] and _new_header(new2) == before[new2]
    assert f"migrated-from-{U(3)}" in _new_header(new3)["tags"]
    assert _copy(capsys, spoiled, "--verify")[0] != 0, "a pair that was not copied is not 'verified'"


def test_an_inventory_from_before_the_re_import_says_so_and_fails(world, capsys):
    _source(world, SID[1])
    _legacy_from_source(world, U(1), SID[1])
    _rich(world, U(1))
    inv = _inventory_file(world, capsys)                 # every replacement is null
    new = _import(capsys, SID[1])[1].out.strip().split(":")[1]
    before = _new_header(new)
    for flags in ((), ("--apply",), ("--verify",)):
        code, out, err = _copy(capsys, inv, *flags)
        assert code == cli.EXIT_INPUT and "重匯之前的 inventory" in err
    assert _new_header(new) == before


def test_verify_fails_on_a_refused_item_even_when_nothing_is_left_to_write(world, capsys):
    new = _two_olds(world, capsys, ("a",), ("b",))       # cannot be told apart
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--verify")
    assert code != 0 and "待寫 0" in out and "不抄 1" in out


def test_the_header_file_lives_in_the_private_state_tmp_and_is_gone_afterwards(world, capsys, monkeypatch):
    new = _setup(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    seen = []
    real = cli.main

    def spy(argv=None):
        if argv and argv[0] == "edit" and "--header-file" in argv:
            path = _p.Path(argv[argv.index("--header-file") + 1])
            seen.append((str(path), path.exists(), oct(path.stat().st_mode & 0o777), oct(path.parent.parent.stat().st_mode & 0o777)))
        return real(argv)
    monkeypatch.setattr(cli, "main", spy)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0, err
    state = store.Paths.from_env().state
    (path, existed, mode, dirmode), = seen
    assert path.startswith(str(state / "tmp")) and existed and mode == "0o600" and dirmode == "0o700"
    assert not _p.Path(path).exists() and not list((state / "tmp").glob("agora-copy-headers-*"))


# --- round 3: nothing is dropped as a whole field; "expected" is told from "broken" ----------------


def _expected_five(world):
    world.old(U(11), "s-nosrc", source=False, relation="merge")             # no-source
    world.old(U(12), "s-other", host="some-other-machine")                  # other-machine
    world.old(U(13), "s-missing", present=False)                            # local-missing
    world.old(U(14), "s-nohost", host=None)                                 # host-unknown
    world.old(U(15), "s-agy", agent="agy")                                  # agent-unavailable


def _reimported(world, capsys, n: int, **rich) -> str:
    _source(world, SID[n])
    _legacy_from_source(world, U(n), SID[n])
    _rich(world, U(n), **rich)
    return _import(capsys, SID[n])[1].out.strip().split(":")[1]


def test_a_ref_to_another_old_session_becomes_its_new_id_and_the_rest_of_the_list_stays(world, capsys):
    new3 = _reimported(world, capsys, 3)
    new1 = _reimported(world, capsys, 1, refs=["foundry:repo-x@main", f"agora:{U(3)}"])
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0, err
    assert _new_header(new1)["refs"] == ["foundry:repo-x@main", f"agora:{new3}"]
    assert _copy(capsys, inv, "--verify")[0] == 0


def test_a_ref_to_an_old_session_with_no_new_id_loses_only_that_element_and_is_not_consistent(world, capsys):
    _source(world, SID[3])
    _legacy_from_source(world, U(3), SID[3])                                 # not re-imported: no new id
    new1 = _reimported(world, capsys, 1, refs=["foundry:repo-x@main", f"agora:{U(3)}"],
                       verified=["foundry:v-x", f"agora:{U(3)}"])
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    got = _new_header(new1)
    assert got["refs"] == ["foundry:repo-x@main"] and got["verified"] == ["foundry:v-x"]
    assert f"refs: agora:{U(3)}" in out and f"verified: agora:{U(3)}" in out
    assert code == cli.EXIT_ERROR, "an element was lost"
    assert "跳過（已一致）0" in out, "written, but not 'consistent'"
    code, out, err = _copy(capsys, inv, "--verify")
    assert code != 0 and "已一致 0" in out and "丟掉了" in out


def test_a_custom_field_that_names_an_old_session_somewhere_inside_is_left_for_a_person(world, capsys):
    new1 = _reimported(world, capsys, 1, custom_dict={"note": "plain", "deep": {"ref": f"agora:{U(1)}"}},
                       other_custom="fine")
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    got = _new_header(new1)
    assert "custom_dict" not in got and got["other_custom"] == "fine" and got["title"] == SECRET_TITLE
    assert "需人決定" in out and "custom_dict" in out and code == cli.EXIT_ERROR
    code, out, err = _copy(capsys, inv, "--verify")
    assert code != 0 and "已一致 0" in out


def test_the_five_expected_kinds_do_not_fail_verify_after_the_one_real_re_import(world, capsys):
    _expected_five(world)
    new = _reimported(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0, (out, err)
    assert "預期不適用 5" in out and "重匯之前" not in err
    code, out, err = _copy(capsys, inv, "--verify")
    assert code == 0, (out, err)
    assert f"migrated-from-{U(1)}" in _new_header(new)["tags"]


def test_an_inventory_with_only_expected_kinds_is_not_a_pre_re_import_one_and_verifies(world, capsys):
    _expected_five(world)
    inv = _inventory_file(world, capsys)
    for flags in ((), ("--apply",), ("--verify",)):
        code, out, err = _copy(capsys, inv, *flags)
        assert code == 0 and "重匯之前" not in err and "預期不適用 5" in out, (flags, out, err)


def test_a_local_present_old_session_with_no_replacement_fails_verify_and_apply(world, capsys):
    _source(world, SID[3])
    _legacy_from_source(world, U(3), SID[3])                                 # forgotten: never re-imported
    _reimported(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR and "no-replacement" in out and "重匯之前" not in err
    assert _copy(capsys, inv, "--verify")[0] != 0


def test_verify_does_not_trust_the_file_for_what_is_missing_or_relabelled(world, capsys):
    new = _reimported(world, capsys, 1)
    _reimported(world, capsys, 2)
    inv = _inventory_file(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == 0
    assert _copy(capsys, inv, "--verify")[0] == 0
    gone = _edit_inventory(world, inv, lambda d: d.update(sessions=[s for s in d["sessions"] if s["ulid"] != U(2)]))
    code, out, err = _copy(capsys, gone, "--verify")
    assert code != 0 and "missing-from-inventory" in out

    def relabel(d):
        for s in d["sessions"]:
            if s["ulid"] == U(2):
                s["class"] = "new-format"
    code, out, err = _copy(capsys, _edit_inventory(world, inv, relabel), "--verify")
    assert code != 0 and "missing-from-inventory" in out


def test_a_drop_list_entry_without_a_consistent_pair_fails_verify(world, capsys):
    _expected_five(world)
    _reimported(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == 0
    sneaked = _edit_inventory(world, inv, lambda d: d["drop_list"].append(U(11)))
    code, out, err = _copy(capsys, sneaked, "--verify")
    assert code != 0 and U(11) in err and "沒有對應的「已一致」" in err


def test_an_id_only_the_inventory_knows_is_filtered_too(world, capsys):
    ghost = "2T000000000000000000000777"
    new = _reimported(world, capsys, 1, tags=("keep-1", f"see-{ghost}"))
    inv = _inventory_file(world, capsys)
    ghosted = _edit_inventory(world, inv, lambda d: d["sessions"].append(
        {"ulid": ghost, "class": "drive-only", "replacement": None}))
    code, out, err = _copy(capsys, ghosted, "--apply")
    assert f"see-{ghost}" not in _new_header(new)["tags"] and "keep-1" in _new_header(new)["tags"]


@pytest.mark.parametrize("bad", [{"sessions": [{"class": "new-format"}]}, {"sessions": [{"ulid": 5}]}])
def test_an_entry_without_a_string_ulid_is_refused(world, capsys, bad):
    _reimported(world, capsys, 1)
    path = world.tmp / "bad2.json"
    path.write_text(json.dumps(bad))
    code, out, err = _copy(capsys, str(path), "--verify")
    assert code == cli.EXIT_INPUT and "ulid" in err and out == ""


def test_a_new_session_gone_from_drive_is_not_written_to(world, capsys):
    new = _reimported(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    before = _new_header(new)
    world.index.mark_missing([new])
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR and "replacement-gone-from-drive" in out
    assert _new_header(new) == before


def test_a_replacement_that_is_chunks_but_not_an_import_is_not_written_to(world, capsys):
    new = _reimported(world, capsys, 1)
    inv = _inventory_file(world, capsys)
    hdr = _new_header(new)
    hdr["agora"]["relation"] = "continue"
    md5 = world.index.db.execute("SELECT md5 FROM sessions WHERE ulid=?", (new,)).fetchone()[0]
    world.index.put(new, md5, hdr, BODY)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == cli.EXIT_ERROR and "replacement-is-not-a-chunks-import" in out
    assert _new_header(new)["title"] == hdr["title"]


# --- round 4: tags like refs, no dead end, containment, accept -------------------------------------


def test_a_human_tag_naming_an_old_session_follows_it_to_its_new_id_or_is_listed_as_lost(world, capsys):
    new3 = _reimported(world, capsys, 3)
    _source(world, SID[4])
    _legacy_from_source(world, U(4), SID[4])                                  # never re-imported: no new id
    new1 = _reimported(world, capsys, 1, tags=("keep", f"see-{U(3)}", f"followup-of-{U(4)}", f"superseded-by-{U(3)}"))
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    tags = _new_header(new1)["tags"]
    assert f"see-{new3}" in tags and f"see-{U(3)}" not in tags
    assert not any(t.startswith("superseded-by-") for t in tags) and f"followup-of-{U(4)}" not in tags
    assert f"tags: followup-of-{U(4)}" in out, "the lost tag is listed with its value"
    assert f"沒抄（刻意）：superseded-by-{U(3)}" in out
    assert code == cli.EXIT_ERROR


def test_superseded_by_alone_is_deliberate_and_does_not_block(world, capsys):
    new = _reimported(world, capsys, 1, tags=("keep", "superseded-by-2T888888888888888888888888"))
    inv = _inventory_file(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == 0
    assert _copy(capsys, inv, "--verify")[0] == 0
    assert not any(t.startswith("superseded-by-") for t in _new_header(new)["tags"])


def test_a_ref_that_would_point_at_the_new_session_itself_is_dropped_and_is_not_a_loss(world, capsys):
    _source(world, SID[1])
    for n in (1, 2):
        _legacy_from_source(world, U(n), SID[1])                              # two olds, one source
    _rich(world, U(1), tags=("a", f"superseded-by-{U(2)}"), refs=["foundry:r", f"agora:{U(2)}"])
    _rich(world, U(2), refs=["foundry:r", f"agora:{U(1)}"], tags=("b",))
    new = _import(capsys, SID[1])[1].out.strip().split(":")[1]
    inv = _inventory_file(world, capsys)
    code, out, err = _copy(capsys, inv, "--apply")
    assert code == 0, (out, err)
    assert _new_header(new)["refs"] == ["foundry:r"]
    assert _copy(capsys, inv, "--verify")[0] == 0


def test_a_ref_to_a_sibling_old_session_the_inventory_does_not_name_is_still_replaced(world, capsys):
    """The pairing table comes from the index, not only from the inventory file."""
    _source(world, SID[1])
    for n in (1, 2):
        _legacy_from_source(world, U(n), SID[1])
    _rich(world, U(1), tags=("a", f"superseded-by-{U(2)}"))
    _rich(world, U(2), tags=("b",))
    new12 = _import(capsys, SID[1])[1].out.strip().split(":")[1]
    new3 = _reimported(world, capsys, 3, refs=[f"agora:{U(1)}"])
    inv = _inventory_file(world, capsys)
    without_u1 = _edit_inventory(world, inv, lambda d: d.update(sessions=[s for s in d["sessions"] if s["ulid"] != U(1)]))
    _copy(capsys, without_u1, "--apply")
    assert _new_header(new3)["refs"] == [f"agora:{new12}"]


def test_case_that_names_an_old_session_is_rewritten_like_a_ref(world, capsys):
    new3 = _reimported(world, capsys, 3)
    new1 = _reimported(world, capsys, 1, case=f"agora:{U(3)}")
    inv = _inventory_file(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == 0
    assert _new_header(new1)["case"] == f"agora:{new3}"
    assert _copy(capsys, inv, "--verify")[0] == 0


def _lost_ref_world(world, capsys):
    """U1's refs name U6, an old Session of another machine: it will never have a new id."""
    world.old(U(6), "s-other", host="some-other-machine")
    new = _reimported(world, capsys, 1, refs=["foundry:repo-x@main", f"agora:{U(6)}"])
    return new, _inventory_file(world, capsys)


def _accept_file(world, text: str) -> str:
    path = world.tmp / "accept.txt"
    path.write_text(text)
    return str(path)


def test_a_person_adding_what_was_lost_is_consistent_and_apply_does_not_remove_it(world, capsys):
    new, inv = _lost_ref_world(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == cli.EXIT_ERROR
    assert cli.main(["edit", "session", f"agora:{new}", "--header", f"refs=[foundry:repo-x@main, agora:{U(6)}]",
                     "--add-tag", "added-by-hand"]) in (0, 3)
    capsys.readouterr()
    code, out, err = _copy(capsys, inv, "--verify")
    assert "待寫 0" in out, "the person's extra element is not an inconsistency"
    assert code != 0, "still lost, until someone says it is fine"
    assert _copy(capsys, inv, "--apply")[0] == cli.EXIT_ERROR
    got = _new_header(new)
    assert got["refs"] == ["foundry:repo-x@main", f"agora:{U(6)}"] and "added-by-hand" in got["tags"]


def test_accept_turns_an_attention_item_into_a_pass_and_says_which_ones(world, capsys):
    new, inv = _lost_ref_world(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == cli.EXIT_ERROR
    acc = _accept_file(world, f"# looked at it\n\nagora:{new}   # the ref to the other machine's one is fine\n")
    code, out, err = _copy(capsys, inv, "--verify", "--accept", acc)
    assert code == 0, (out, err)
    assert f"靠 --accept 通過的" in out and f"agora:{new}" in out
    code, out, err = _copy(capsys, inv, "--accept", acc)               # the dry run says it too
    assert code == 0 and f"agora:{new}" in out
    assert _copy(capsys, inv, "--apply", "--accept", acc)[0] == 0


def test_accept_does_not_cover_a_pair_that_still_has_something_to_write(world, capsys):
    new, inv = _lost_ref_world(world, capsys)                            # not applied yet: the pair is `todo`
    acc = _accept_file(world, f"{new}\n")
    code, out, err = _copy(capsys, inv, "--verify", "--accept", acc)
    assert code != 0 and "待寫 1" in out


@pytest.mark.parametrize("content", ["not-a-ulid\n", "2T999999999999999999999999\n", "{new}\nnonsense\n"])
def test_an_accept_file_with_a_line_that_is_not_a_new_ulid_of_the_plan_is_refused_whole(world, capsys, content):
    new, inv = _lost_ref_world(world, capsys)
    assert _copy(capsys, inv, "--apply")[0] == cli.EXIT_ERROR
    acc = _accept_file(world, content.replace("{new}", new))
    code, out, err = _copy(capsys, inv, "--verify", "--accept", acc)
    assert code == cli.EXIT_INPUT and "accept" in err and out == ""


def test_an_accept_file_that_cannot_be_read_is_refused(world, capsys):
    new, inv = _lost_ref_world(world, capsys)
    code, out, err = _copy(capsys, inv, "--verify", "--accept", str(world.tmp / "nope.txt"))
    assert code == cli.EXIT_INPUT and "accept" in err and out == ""
