"""#502 (#462 v1 §12.9 C-11, C-12): the twin compression relies on `continue --fork` and
`edit --add-tag`; these tests lock the behaviour that is already there (no behaviour change)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from agora import header as h
from test_cli_fork import _fork, _import, document_of, env, header_of, paths, run  # noqa: F401  (env is a fixture)


def _tree(root) -> dict[str, bytes]:
    """Every file under a session's folder, by relative name: header and raw bytes alike."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _search_role(capsys, role="role:R"):
    code, out, err = run(capsys, "search", "session", "--filter", f"tags={role}",
                         "--sort", "updated", "--json", "--no-sync")
    assert code == 0, err
    return [row["id"] for row in json.loads(out)]


def _updated(agora_id) -> str:
    return h.agora_of(header_of(agora_id))["updated_at"]


def test_c11_fork_keeps_tags_and_leaves_the_source_alone(env, capsys):
    _, s, _ = _import(capsys, "--add-tag", "role:R", "--add-tag", "plain")
    mirror = paths().mirror / s.split(":")[1]
    before_header, before_tree = header_of(s), _tree(mirror)
    assert any(name != "session.md" for name in before_tree), "the raw is there to be compared"

    code, t, err = _fork(capsys, s)

    assert code == 0 and t.startswith("agora:") and t != s, err
    hdr = header_of(t)
    assert h.agora_of(hdr)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr)["parents"]] == [s], "S is the only parent"
    assert set(header_of(s)["tags"]) <= set(hdr["tags"])
    assert "role:R" in hdr["tags"] and "plain" in hdr["tags"]
    assert header_of(s) == before_header, "S's header"
    assert document_of(s) == before_tree["session.md"].decode("utf-8")
    assert _tree(mirror) == before_tree, "S's header file and raw bytes, unchanged"


def test_c12a_adding_a_new_tag_moves_updated_at_and_puts_t_first(env, capsys, monkeypatch):
    monkeypatch.setenv("AGORA_NOW", "1790000000")
    _, s, _ = _import(capsys, "--add-tag", "role:R")
    monkeypatch.setenv("AGORA_NOW", "1790000100")
    _, t, _ = _fork(capsys, s)
    assert _search_role(capsys) == [t, s]
    # make S newer than T first, so the order below is the edit's doing, not leftover
    monkeypatch.setenv("AGORA_NOW", "1790000200")
    assert run(capsys, "edit", "session", s, "--add-tag", "bump")[0] == 0
    assert _search_role(capsys)[0] == s
    before = _updated(t)

    monkeypatch.setenv("AGORA_NOW", "1790000300")
    code, out, err = run(capsys, "edit", "session", t, "--add-tag", "new-tag")

    assert code == 0 and out == t, err
    assert "new-tag" in header_of(t)["tags"]
    assert _updated(t) > before, "updated_at moved forward"
    assert _updated(t) > _updated(s)
    assert _search_role(capsys)[0] == t, "T is now the first row"


def test_c12b_adding_a_tag_it_already_has_changes_nothing(env, capsys, monkeypatch):
    monkeypatch.setenv("AGORA_NOW", "1790000000")
    _, t, _ = _import(capsys, "--add-tag", "role:R", "--add-tag", "keep")
    before_header, before_updated = header_of(t), _updated(t)

    monkeypatch.setenv("AGORA_NOW", "1790009999")
    code, out, err = run(capsys, "edit", "session", t, "--add-tag", "keep")

    assert code == 0 and out == t
    assert "標頭沒有變" in err
    assert _updated(t) == before_updated, "updated_at did not move"
    assert header_of(t) == before_header
