"""#153: `agora edit --add-tag` appends tags and never opens the editor; the commands that do not
read `--add-tag` refuse it instead of ignoring it."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path

import pytest

from agora import cli, store
from test_cli import _import, env, run  # noqa: F401


@pytest.fixture
def no_editor(monkeypatch, tmp_path):
    """An $EDITOR that fails loudly and leaves a trace: proof it was (not) called."""
    trace = tmp_path / "editor-was-called"
    script = tmp_path / "ed.sh"
    script.write_text(f"#!/bin/sh\ntouch {trace}\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.setenv("VISUAL", str(script))
    return trace


def _tags(sid: str) -> list:
    return store.Index(store.Paths.from_env()).header(sid.split(":")[1]).get("tags")


def _copies() -> list[list[str]]:
    log = Path(os.environ["FAKE_REMOTE"]).parent / "copies.log"
    rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return [[f["name"].split("/", 1)[1] for f in row["files"]] for row in rows]


def test_edit_add_tag_appends_without_opening_the_editor(env, capsys, no_editor):
    _, sid, _ = _import(capsys, "--add-tag", "first")
    code, out, _ = run(capsys, "edit", "session", sid, "--add-tag", "second", "--add-tag", "third")
    assert code == 0 and out == sid
    assert _tags(sid) == ["first", "second", "third"], "appended; what was there is kept"
    assert not no_editor.exists()


def test_edit_add_tag_does_not_double_a_tag_it_already_has(env, capsys, no_editor):
    _, sid, _ = _import(capsys, "--add-tag", "keep")
    code, out, err = run(capsys, "edit", "session", sid, "--add-tag", "keep", "--add-tag", "new", "--add-tag", "new")
    assert code == 0
    assert _tags(sid) == ["keep", "new"]
    code, out, err = run(capsys, "edit", "session", sid, "--add-tag", "keep")     # nothing to add: nothing changes
    assert code == 0 and "標頭沒有變" in err and _tags(sid) == ["keep", "new"]
    assert not no_editor.exists()


def test_edit_add_tag_goes_after_header_when_both_are_given(env, capsys, no_editor):
    """`--header` is overlaid first, then `--add-tag` is appended to the result."""
    _, sid, _ = _import(capsys, "--add-tag", "old")
    code, _, _ = run(capsys, "edit", "session", sid, "--header", "tags=[from-header]",
                     "--header", "description=改過", "--add-tag", "added")
    assert code == 0
    hdr = store.Index(store.Paths.from_env()).header(sid.split(":")[1])
    assert hdr["tags"] == ["from-header", "added"], "the header replaced old; the added tag follows it"
    assert hdr["description"] == "改過"
    assert not no_editor.exists()


def test_edit_add_tag_changes_only_the_header_and_sends_no_segment(env, capsys):
    _, sid, _ = _import(capsys)
    ulid = sid.split(":")[1]
    before = store.Index(store.Paths.from_env()).header(ulid)["agora"]["raw"]
    mark = len(_copies())

    code, out, _ = run(capsys, "edit", "session", sid, "--add-tag", "just-a-tag")

    assert code == 0
    after = store.Index(store.Paths.from_env()).header(ulid)["agora"]["raw"]
    assert after == before, "same raw block: same md5, same segments"
    sent = [name for row in _copies()[mark:] for name in row]
    assert sent == ["session.md"], sent


@pytest.mark.parametrize("argv", [["show", "session", "agora:X"], ["delete", "session", "agora:X", "--yes"],
                                  ["pull", "session", "agora:X"], ["push", "session", "agora:X"],
                                  ["search", "session"], ["merge", "session", "agora:X", "agora:Y", "--agent", "opencode"]])
def test_a_command_that_does_not_read_add_tag_refuses_it(env, capsys, argv):
    code, out, err = run(capsys, *argv, "--add-tag", "ignored")
    assert code == cli.EXIT_INPUT and out == ""
    assert "不吃 --add-tag" in err and "import、continue、edit" in err


def test_the_help_names_the_commands_that_read_add_tag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    assert "--add-tag" in text and "import／continue／edit" in text


def test_import_and_continue_still_take_add_tag(env, capsys):
    _, sid, _ = _import(capsys, "--add-tag", "x")
    assert _tags(sid) == ["x"]
    assert "import" in cli.ADD_TAG_ACTIONS and "continue" in cli.ADD_TAG_ACTIONS and "edit" in cli.ADD_TAG_ACTIONS
