"""R4 4.3: `agora import --as-new` and the refusal of an ordinary import over an old-format Session."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C
from test_agent_claude_increment import SID, Session
from test_cli_more import paths, run
from test_cli_incremental import home  # noqa: F401
from test_segment_write import env as _env  # noqa: F401  (the fake-rclone Drive)

OLD = "2T000000000000000000000001"


def _legacy(index_paths) -> dict:
    """An old-format Session of the same source, in the local index."""
    hdr = {"type": "Session", "title": "舊的", "tags": ["手貼的標籤"], "id": f"agora:{OLD}", "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z",
                     "relation": "import", "parents": [],
                     "source": {"agent": "claude", "session_id": SID, "dir": "/golden/inc", "host": "h"},
                     "raw": {"file": "raw-aaaaaaaaaaaa.json", "md5": "a" * 32, "size": 9}}}
    store.Index(index_paths).put(OLD, "m" * 32, hdr, "舊的內文")
    return hdr


def _args(*extra):
    return ["import", "session", "--external-session-id", SID, "--agent", "claude", *extra]


def _sources(p):
    return store.Index(p).by_source("claude", SID)


def test_as_new_makes_a_new_session_beside_the_old_one_and_touches_nothing_of_it(_env, home, capsys):
    s = Session(home)
    s.turns(40)
    p = paths()
    old = _legacy(p)
    code, out, err = run(capsys, *_args("--as-new"))
    assert code == 0, err
    new = out.split(":")[1]
    assert new != OLD
    hdr = store.Index(p).header(new)
    agora = h.agora_of(hdr)
    assert agora["relation"] == "import" and not agora.get("parents")
    assert store.raw_kind(agora["raw"]) == "chunks" and len(agora["raw"]["segments"]) > 1
    assert store.Index(p).header(OLD) == h.upgrade(json.loads(json.dumps(old)))
    assert store.Index(p).children(OLD) == [], "no parents, so the old one can still be deleted"
    assert sorted(_sources(p)) == sorted([OLD, new])


def test_as_new_twice_is_a_no_op_and_does_not_read_the_source_again(_env, home, capsys, monkeypatch):
    s = Session(home)
    s.turns(40)
    _legacy(paths())
    first = run(capsys, *_args("--as-new"))[1]
    calls = []
    real = C.ClaudeAgent.export_increment
    monkeypatch.setattr(C.ClaudeAgent, "export_increment", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    s.turns(5)                                           # even a source that grew: the answer is the same id
    code, again, _ = run(capsys, *_args("--as-new"))
    assert code == 0 and again == first and calls == []
    assert len(_sources(paths())) == 2


def test_as_new_with_a_chunked_session_of_that_source_already_there_returns_it(_env, home, capsys):
    s = Session(home)
    s.turns(40)
    made = run(capsys, *_args())[1]                      # no old one: an ordinary import makes a chunks/1
    code, out, _ = run(capsys, *_args("--as-new"))
    assert code == 0 and out == made


def test_an_ordinary_import_over_an_old_format_session_is_refused_and_changes_nothing(_env, home, capsys):
    s = Session(home)
    s.turns(40)
    p = paths()
    old = _legacy(p)
    before = list(_sources(p))
    code, out, err = run(capsys, *_args())
    assert code != 0 and out == "" and "--as-new" in err
    assert _sources(p) == before == [OLD], "no new Session, no child"
    assert store.Index(p).header(OLD) == h.upgrade(json.loads(json.dumps(old)))
    assert not (p.outbox.exists() and any(p.outbox.iterdir()))
    s.turns(3)                                           # a changed source takes the same road
    code, out, err = run(capsys, *_args("--header", "title=改標題"))
    assert code != 0 and _sources(p) == [OLD]


def test_the_refusal_also_stops_the_branch_path(_env, home, capsys):
    """An old Session with a child used to branch (a new Session whose parents point at the old one)."""
    s = Session(home)
    s.turns(40)
    p = paths()
    _legacy(p)
    child = {"type": "Session", "title": "子", "tags": [], "id": f"agora:2T{'0' * 23}2", "refs": [], "case": None,
             "agora": {"header": 2, "created_at": "2026-09-02T00:00:00Z", "updated_at": "2026-09-02T00:00:00Z",
                       "relation": "continue", "parents": [{"id": f"agora:{OLD}", "raw_md5": None}]}}
    store.Index(p).put(f"2T{'0' * 23}2", "c" * 32, child, "子")
    code, out, err = run(capsys, *_args())
    assert code != 0 and "--as-new" in err
    assert len(_sources(p)) == 1
