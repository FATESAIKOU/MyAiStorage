"""R3b-2 at the command level: `import` and the continue round use the cursor (tasks 3.6, 3.16)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import types

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C
from test_agent_claude_increment import SID, Session
from test_cli_more import paths, run
from test_segment_write import C as CH, env as _env  # noqa: F401  (the fake-rclone Drive, AGORA_RAW_CHUNK)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_agent", lambda name: C.ADAPTER)
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(tmp_path / "claude-home"))
    proj = tmp_path / "claude-home" / ".claude" / "projects" / "-golden-inc"
    proj.mkdir(parents=True)
    return proj


def _import(capsys):
    return run(capsys, "import", "session", "--external-session-id", SID, "--agent", "claude")


def _raw_of(ulid):
    return h.agora_of(store.Index(paths()).header(ulid))["raw"]


def test_a_first_import_is_cut_in_segments_and_leaves_a_cursor(_env, home, capsys):
    s = Session(home)
    s.turns(40)
    code, out, _ = _import(capsys)
    assert code == 0
    ulid = out.split(":")[1]
    block = _raw_of(ulid)
    assert block["layout"] == "chunks/1" and len(block["segments"]) > 3
    assert store.read_cursor(paths(), ulid)["base_md5"] == block["md5"]


def test_a_second_import_of_a_longer_session_keeps_the_sealed_segments(_env, home, capsys):
    s = Session(home)
    s.turns(40)
    ulid = _import(capsys)[1].split(":")[1]
    first = _raw_of(ulid)
    s.turns(20)
    code, out, _ = _import(capsys)
    assert code == 0 and out.split(":")[1] == ulid
    second = _raw_of(ulid)
    assert second["segments"][:len(first["segments"]) - 1] == first["segments"][:-1]
    assert second["md5"] != first["md5"]


def test_import_will_not_update_or_branch_a_legacy_session_in_place(_env, home, capsys, monkeypatch):
    """3.16 / I.2 item 24: an ordinary import must not upgrade a session whose raw it cannot read."""
    s = Session(home)
    s.turns(40)
    ulid = _import(capsys)[1].split(":")[1]
    real = store.Index.header

    def legacy(self, u):
        hdr = real(self, u)
        if u == ulid:
            hdr["agora"]["raw"] = {"file": "raw-aaaaaaaaaaaa.json", "md5": "a" * 32, "size": 5}
        return hdr
    monkeypatch.setattr(store.Index, "header", legacy)
    before = store.read_cursor(paths(), ulid)
    s.turns(5)
    code, out, err = _import(capsys)
    assert code != 0 and "--as-new" in err
    assert store.read_cursor(paths(), ulid) == before, "nothing was staged"
    assert len(store.Index(paths()).by_source("claude", SID)) == 1, "and no branch was made"


def test_the_continue_collect_hands_the_adapter_the_cursor_of_the_session(_env, tmp_path):
    seen = {}

    class Agent:
        name = "claude"

        def collect_increment(self, launch, cursor):
            seen["cursor"] = cursor
            return types.SimpleNamespace(exported="E", cursor={"c": 1}, append_from=None, seal_upto=None)

        def collect(self, launch):
            raise AssertionError("the increment path is the one to take")

    p = paths()
    ulid = h.new_ulid()
    store._cursor_path(p, ulid).parent.mkdir(parents=True)
    store._cursor_path(p, ulid).write_text('{"version": 1, "x": 2}', encoding="utf-8")
    run_ = types.SimpleNamespace(record={"dir": str(tmp_path), "agent_session_id": "s", "before_count": 0},
                                 fingerprint=None, collect_failed=False, paths=p,
                                 target=f"agora:{ulid}", increment=None, increment_ulid=None)
    assert cli._collect_new(run_, Agent()) == "E"
    assert seen["cursor"] == {"version": 1, "x": 2}
    assert run_.increment_ulid == ulid


def test_a_plan_is_only_handed_over_for_the_export_it_came_with(_env):
    inc = types.SimpleNamespace(exported=object(), cursor={"c": 1}, append_from=3, seal_upto=9)
    run_ = types.SimpleNamespace(increment=inc, increment_ulid="U")
    assert cli._plan_of(run_, inc.exported) == {"append_from": 3, "seal_upto": 9, "cursor": {"c": 1},
                                                "cursor_ulid": "U"}
    assert cli._plan_of(run_, object()) is None
    run_.increment = types.SimpleNamespace(exported=inc.exported, cursor=None, append_from=None, seal_upto=None)
    assert cli._plan_of(run_, inc.exported) is None
