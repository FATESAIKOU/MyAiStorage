"""R4 4.12: the migration of design F.1, walked end to end in an isolated world.

Fake rclone, fake HOME, a hand-made index of old-format Sessions, real claude sources in a fake
claude home. The steps are the ones docs/migration.md lists: inventory, re-import with `--as-new`,
inventory again, delete only the `drop_list` of that last inventory, verify.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from agora import cli, header as h, store, background
from agora.agents import claude as C
from test_agent_claude_increment import Session
from test_migrate_inventory import HOST, U, by_id, header, listfile, md5, world  # noqa: F401


def _source(world, sid: str, turns: int = 6) -> Session:
    proj = world.tmp / "chome" / ".claude" / "projects" / "-proj"
    s = Session(proj, sid=sid)
    s.turns(turns)
    return s


def _legacy_from_source(world, ulid: str, sid: str, **kw) -> None:
    """An old-format Session whose raw is what the source exports now (so a re-import is identical)."""
    raw = C.ClaudeAgent().export(sid).raw
    world.old(ulid, sid, raw=raw, **kw)


def _inventory(world, capsys, keep: str | None = None) -> dict:
    return world.report(capsys, *(["--must-keep", keep] if keep else []))


def _import(capsys, sid: str):
    return cli.main(["import", "session", "--external-session-id", sid, "--agent", "claude", "--as-new"]), \
        capsys.readouterr()


SID = {n: f"{n:08d}-aaaa-4bbb-8ccc-0123456789ab" for n in range(1, 8)}


def test_the_whole_migration_drops_only_what_the_last_inventory_says(world, capsys, monkeypatch):
    # A: ordinary. B: the source gets shorter before the re-import. C: another machine's.
    # D: a pre-upgrade version is stuck in the outbox. E: must-keep and being continued. F: a merge.
    for n in (1, 2, 4, 5):
        _source(world, SID[n])
        _legacy_from_source(world, U(n), SID[n])
    world.old(U(3), SID[3], host="another-machine")
    world.old(U(6), "none", source=False, relation="merge")
    world.outbox(U(4), legacy=True)
    monkeypatch.setattr(store, "continuing_details", lambda paths, ulid: {} if ulid == U(5) else None)
    keep = listfile(world, f"# the ones that matter\nagora:{U(1)}\n{U(5)}\n")

    # 2. inventory: nothing is replaced yet, so nothing can go
    first = _inventory(world, capsys, keep)
    assert first["drop_list"] == []
    assert {s["ulid"]: s["not_droppable_reason"] for s in first["sessions"]} == {
        U(1): "no-replacement", U(2): "no-replacement", U(3): "class-not-local-present",
        U(4): "no-replacement", U(5): "continuing", U(6): "class-not-local-present"}

    # B's source is cut down, then everything but the one being continued is re-imported
    path = world.tmp / "chome" / ".claude" / "projects" / "-proj" / f"{SID[2]}.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:4]))
    for n in (1, 2, 4):
        code, out = _import(capsys, SID[n])
        assert code in (0, 3), out.err          # 3: inline mode says "not all uploaded" while D's stuck entry waits
        assert out.out.startswith("agora:"), (n, out.out, out.err)
    # (E is skipped: it is being continued, the inventory says so)

    # 3. confirmation point 2
    second = _inventory(world, capsys, keep)
    s = by_id(second)
    assert s[U(1)]["replacement"]["relation_to_old"] == "identical"
    assert s[U(2)]["replacement"]["relation_to_old"] == "different"
    # D: the stuck pre-upgrade version is the newer one in the local index, so the re-import no longer
    # matches it - and the stuck entry is listed in any case
    assert s[U(4)]["replacement"]["relation_to_old"] == "different"
    assert s[U(4)]["outbox_legacy"] is True and s[U(4)]["droppable"] is False
    assert s[U(5)]["not_droppable_reason"] == "continuing"
    assert second["drop_list"] == [U(1)]

    # 4. right before deleting, the inventory is run again and only its drop_list goes to `delete`
    final = _inventory(world, capsys, keep)
    drop = final["drop_list"]
    assert drop == [U(1)]
    assert cli.main(["delete", "session", *[f"agora:{u}" for u in drop], "--yes"]) in (0, 3)
    capsys.readouterr()
    background.process_trash_queue(world.paths, store.Drive(world.paths))
    assert not (world.remote / "agora" / "sessions" / U(1)).exists(), "purged on Drive"
    index = store.Index(world.paths)
    assert index.header(U(1)) is None
    for kept in (U(2), U(3), U(4), U(5), U(6)):
        assert index.header(kept) is not None and (world.remote / "agora" / "sessions" / kept).exists()
    assert len(index.by_source("claude", SID[1])) == 1, "the new Session of A is the one left"

    # 5. verification
    after = _inventory(world, capsys, keep)
    assert after["drop_list"] == [U(4)] or after["drop_list"] == [], after["drop_list"]
    assert after["must_keep_deleted"] == [U(1)]
    assert U(1) not in by_id(after)
    assert by_id(after)[U(2)]["droppable"] is False and by_id(after)[U(3)]["droppable"] is False


def test_a_replacement_that_is_not_on_drive_yet_is_not_identical(world, capsys, monkeypatch):
    _source(world, SID[1])
    _legacy_from_source(world, U(1), SID[1])
    monkeypatch.setattr(background, "start", lambda paths=None: background.FAILED)    # queued, not uploaded
    code, out = _import(capsys, SID[1])
    assert code != 0 and "outbox" in out.err
    s = by_id(_inventory(world, capsys))[U(1)]
    assert s["replacement"]["relation_to_old"] == "different"
    assert s["replacement"]["reason"] == "drive-version-elsewhere"
    assert s["droppable"] is False
    monkeypatch.undo()


def test_a_must_keep_session_with_a_different_replacement_keeps_its_whole_lineage(world, capsys):
    for n in (1, 2):
        _source(world, SID[n])
    _legacy_from_source(world, U(1), SID[1])
    world.old(U(2), SID[2], raw=C.ClaudeAgent().export(SID[2]).raw, relation="continue", parents=[U(1)])
    path = world.tmp / "chome" / ".claude" / "projects" / "-proj" / f"{SID[2]}.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:2]))        # B was cut down
    for n in (1, 2):
        assert _import(capsys, SID[n])[0] == 0
    report = _inventory(world, capsys, listfile(world, U(2) + "\n"))
    s = by_id(report)
    assert s[U(2)]["not_droppable_reason"] == "relation-different"
    assert s[U(1)]["not_droppable_reason"] == "lineage-member-not-droppable"
    assert report["drop_list"] == []


@pytest.mark.parametrize("text", ["", "# nothing\n", b"\xff\xff", "not an id\n"])
def test_a_bad_list_gives_an_error_and_no_candidates_even_when_everything_else_is_ready(world, capsys, text):
    _source(world, SID[1])
    _legacy_from_source(world, U(1), SID[1])
    assert _import(capsys, SID[1])[0] == 0
    ok = _inventory(world, capsys)
    assert ok["drop_list"] == [U(1)]
    code, out, err = world.run(capsys, "--must-keep", listfile(world, text))
    assert code != 0 and out == ""


def test_a_session_with_no_original_record_is_never_in_the_drop_list(world, capsys):
    world.old(U(1), "gone-source", present=False)                                  # local-missing
    world.old(U(2), "s", host="elsewhere")
    world.old(U(3), "s3", source=False, relation="merge")
    report = _inventory(world, capsys)
    assert report["drop_list"] == []
    assert {s["class"] for s in report["sessions"]} == {"local-missing", "other-machine", "no-source"}
