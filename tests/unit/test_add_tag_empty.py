"""Test --add-tag empty value validation (#303)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

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


def _pending_written(sid: str) -> bool:
    path = store.Paths.from_env().pending / (sid.split(":")[1] + ".json")
    return path.exists()


def test_edit_add_tag_empty_string(env, capsys, no_editor):
    """Empty string for --add-tag in edit: InputError (exit 1), no editor, no change."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, err = run(capsys, "edit", "session", sid, "--add-tag", "")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert not no_editor.exists()
    assert _tags(sid) == ["valid"]


def test_edit_add_tag_whitespace_only(env, capsys, no_editor):
    """Whitespace-only string for --add-tag in edit: InputError (exit 1), no editor."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, err = run(capsys, "edit", "session", sid, "--add-tag", "   ")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert not no_editor.exists()
    assert _tags(sid) == ["valid"]


def test_edit_add_tag_with_other_valid_tags(env, capsys, no_editor):
    """One empty tag among valid ones fails the whole edit; nothing changes."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, err = run(capsys, "edit", "session", sid,
                         "--add-tag", "first",
                         "--add-tag", "",  # empty
                         "--add-tag", "last")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert not no_editor.exists()
    assert _tags(sid) == ["valid"]


def test_import_add_tag_empty_string(env, capsys):
    """Empty string for --add-tag in import: InputError (exit 1), nothing imported."""
    code, out, err = run(capsys, "import", "session",
                        "--external-session-id", "ses_a",
                        "--agent", "opencode",
                        "--add-tag", "")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert store.Index(store.Paths.from_env()).by_source("opencode", "ses_a") == []


def test_import_add_tag_whitespace_only(env, capsys):
    """Whitespace-only string for --add-tag in import: InputError (exit 1)."""
    code, out, err = run(capsys, "import", "session",
                        "--external-session-id", "ses_a",
                        "--agent", "opencode",
                        "--add-tag", "   ")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert store.Index(store.Paths.from_env()).by_source("opencode", "ses_a") == []


def test_continue_add_tag_empty_string(env, capsys, no_editor):
    """Empty string for --add-tag in continue: InputError (exit 1) before the agent
    half starts or a pending record is written."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    launched_before = len(env.launched)
    code, out, err = run(capsys, "continue", "session", sid,
                        "--agent", "opencode",
                        "--dir", "/tmp",
                        "--add-tag", "")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert len(env.launched) == launched_before          # no agent was started
    assert not _pending_written(sid)                     # no pending record was written
    assert _tags(sid) == ["valid"]                       # the header is unchanged


def test_continue_add_tag_whitespace_only(env, capsys, no_editor):
    """Whitespace-only string for --add-tag in continue: InputError (exit 1), nothing runs."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    launched_before = len(env.launched)
    code, out, err = run(capsys, "continue", "session", sid,
                        "--agent", "opencode",
                        "--dir", "/tmp",
                        "--add-tag", "   ")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert len(env.launched) == launched_before
    assert not _pending_written(sid)
    assert _tags(sid) == ["valid"]


def test_continue_add_tag_with_other_valid_tags(env, capsys):
    """One empty tag among valid ones fails the whole continue before it starts."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    launched_before = len(env.launched)
    code, out, err = run(capsys, "continue", "session", sid,
                        "--agent", "opencode",
                        "--dir", "/tmp",
                        "--add-tag", "first",
                        "--add-tag", "",  # empty
                        "--add-tag", "last")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert len(env.launched) == launched_before
    assert not _pending_written(sid)
    assert _tags(sid) == ["valid"]


def test_import_add_tag_with_other_valid_tags(env, capsys):
    """One empty tag among valid ones fails the whole import (nothing imports)."""
    code, out, err = run(capsys, "import", "session",
                        "--external-session-id", "ses_a",
                        "--agent", "opencode",
                        "--add-tag", "first",
                        "--add-tag", "",  # empty
                        "--add-tag", "last")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert store.Index(store.Paths.from_env()).by_source("opencode", "ses_a") == []


def test_import_add_tag_empty_refuses_whole_multi_id_import(env, capsys):
    """With several --external-session-id values, a bad --add-tag refuses the whole
    command up front: no per-id attempt (the import loop would otherwise report each id)."""
    code, out, err = run(capsys, "import", "session",
                        "--external-session-id", "ses_a,ses_b",
                        "--agent", "opencode",
                        "--add-tag", "")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert "匯入失敗" not in err
    assert store.Index(store.Paths.from_env()).by_source("opencode", "ses_a") == []
    assert store.Index(store.Paths.from_env()).by_source("opencode", "ses_b") == []


def test_edit_add_tag_valid_still_works(env, capsys, no_editor):
    """Valid tags should still work normally."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, err = run(capsys, "edit", "session", sid, "--add-tag", "new_tag")
    assert code == 0
    assert out == sid
    assert _tags(sid) == ["valid", "new_tag"]
    assert not no_editor.exists()


def test_edit_add_tag_valid_forms_unchanged(env, capsys):
    """A leading-dash value needs `--add-tag=-x`; duplicates drop; / : . pass through."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, _ = run(capsys, "edit", "session", sid,
                       "--add-tag=-x", "--add-tag=-x", "--add-tag", "a/b:c.d")
    assert code == 0
    assert out == sid
    assert _tags(sid) == ["valid", "-x", "a/b:c.d"]


def test_import_add_tag_valid_still_works(env, capsys):
    """Valid tags for import should still work normally."""
    code, out, err = run(capsys, "import", "session",
                        "--external-session-id", "ses_a",
                        "--agent", "opencode",
                        "--add-tag", "new_tag")
    assert code == 0
    assert out.startswith("agora:")
    sid = out
    assert _tags(sid) == ["new_tag"]


def test_continue_add_tag_valid_still_works(env, capsys):
    """Valid tags for continue should still work normally."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, err = run(capsys, "continue", "session", sid,
                        "--agent", "opencode",
                        "--dir", "/tmp",
                        "--add-tag", "new_tag")
    assert code == 0
    assert out == sid
    assert _tags(sid) == ["valid", "new_tag"]


def _header(sid: str) -> dict:
    return store.Index(store.Paths.from_env()).header(sid.split(":")[1])


def test_no_add_tag_leaves_tags_alone(env, capsys):
    """Without --add-tag, edit --header does not touch `tags` (the rule behind #303 rev 中-1):
    the header keeps its tags as they are."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")
    code, out, _ = run(capsys, "edit", "session", sid, "--header", "title=second")
    assert code == 0 and out == sid
    assert _header(sid)["title"] == "second"
    assert _tags(sid) == ["valid"]


def test_add_tags_with_no_tags_is_a_no_op():
    """`_add_tags` with nothing to add returns at once: no `tags` key is created and an
    existing non-list value is left as it is (rev 中-1: without the early return both change)."""
    no_key = {"title": "x"}
    cli._add_tags(no_key, [])
    assert no_key == {"title": "x"}
    as_text = {"tags": "csv"}
    cli._add_tags(as_text, [])
    assert as_text == {"tags": "csv"}


def test_edit_add_tag_empty_refuses_before_sync(env, capsys, monkeypatch):
    """A bad --add-tag is refused before edit syncs from Drive (#303, rev 低-1)."""
    _, sid, _ = _import(capsys, "--add-tag", "valid")

    def no_sync(*args, **kwargs):
        raise AssertionError("_sync_for ran before the --add-tag check")
    monkeypatch.setattr(cli, "_sync_for", no_sync)
    code, out, err = run(capsys, "edit", "session", sid, "--add-tag", "")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err


def test_edit_add_tag_empty_reports_tag_error_before_gone_check(env, capsys, monkeypatch):
    """With a bad --add-tag, edit reports the tag error before the id is looked up:
    the gone check is forced to refuse, and the tag error still wins (#303, rev 低-1)."""
    def refuse(paths, index, agora_id):
        raise cli.InputError("gone check ran first")
    monkeypatch.setattr(cli, "_refuse_if_gone", refuse)
    code, out, err = run(capsys, "edit", "session", "agora:01NOSUCHID0000000000000000", "--add-tag", "")
    assert code == cli.EXIT_INPUT
    assert out == ""
    assert "--add-tag 的值不能是空的" in err
    assert "gone check ran first" not in err
