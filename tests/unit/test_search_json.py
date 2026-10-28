"""`agora search session --json` (#301): one machine-readable document on stdout.

The text rows are the old contract and are covered by test_cli.py; these tests ask the
same questions of the JSON form and check that the two pick the same sessions.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import shutil
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A fake Drive the command can list, so a test can talk to「Drive」without rclone."""
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def _header(*, title="CSV 規劃", description="先做讀取", tags=None, relation="import",
            agent="opencode", session_id="ses_x",
            updated_at="2026-10-20T09:00:00Z", created_at="2026-10-01T09:00:00Z"):
    ulid = h.new_ulid()
    agora = {"header": 2, "created_at": created_at, "updated_at": updated_at,
             "relation": relation, "parents": []}
    if agent is not None:
        agora["source"] = {"agent": agent, "session_id": session_id, "created_at": created_at}
    return {"type": "Session", "title": title, "description": description,
            "tags": list(tags or []), "id": f"agora:{ulid}", "refs": [], "case": None,
            "agora": agora}


def _staged(*, body="## user\n把 CSV 轉成 Markdown 表格\n", **kw):
    """One session in the local mirror/index, still in the outbox (not uploaded)."""
    hdr = _header(**kw)
    paths = store.Paths.from_env()
    folder = store.stage(paths, hdr, body, b'{"x": 1}')
    store.remember(paths, folder)
    return hdr["id"]


def _forget_the_outbox(agora_id: str) -> None:
    """The mirror copy stays; the outbox entry goes, so no uploader is kicked."""
    shutil.rmtree(store.Paths.from_env().outbox / agora_id.split(":", 1)[1], ignore_errors=True)


def _live_continue(paths, agora_id, *, agent="opencode", started_at="2026-10-18T05:01:00Z"):
    """A pending record somebody holds, written the way `_write_pending` writes it."""
    record = {"agora_id": agora_id, "agent": agent, "agent_session_id": "ses_x",
              "dir": str(Path.home() / "proj"), "before_count": 1,
              "started_at": started_at, "parent": {"id": agora_id, "raw_md5": None}}
    return cli._write_pending(paths, record)


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def test_json_has_every_field_with_the_right_type(remote, capsys):
    """"#301: id/agent/relation/tags/updated_at/conversation_date/title/description/
    locked/locked_by/in_outbox/missing_in_cloud - and the whole stdout is that one array."""
    now_id = _staged(
        title="把 CSV／表格轉成 \"JSON\"：清單",
        description="## user\n用逗號分隔",
        tags=["ai:claude-work", "workdir:/Users/x/y", "note:a=b"],
        updated_at="2026-10-20T09:00:00Z",
        created_at="2026-10-01T09:00:00Z")
    code, out, err = run(capsys, "search", "session", "--json", "--no-sync")

    assert code == 0
    doc = json.loads(out)                 # the whole of stdout, one document
    assert isinstance(doc, list) and len(doc) == 1
    row = doc[0]
    assert set(row) == {"id", "agent", "relation", "tags", "updated_at", "conversation_date",
                        "title", "description", "locked", "locked_by", "in_outbox",
                        "missing_in_cloud"}
    assert row["id"] == now_id and row["id"].startswith("agora:")
    assert row["agent"] == "opencode"
    assert row["relation"] == "import"
    assert row["tags"] == ["ai:claude-work", "workdir:/Users/x/y", "note:a=b"]
    assert row["updated_at"] == "2026-10-20T09:00:00Z"      # RFC 3339, not cut to the day
    assert row["conversation_date"] == "2026-10-01T09:00:00Z"
    assert row["title"] == "把 CSV／表格轉成 \"JSON\"：清單"
    assert row["description"] == "## user\n用逗號分隔"
    assert row["locked"] is False and row["locked_by"] is None
    assert row["in_outbox"] is True and row["missing_in_cloud"] is False
    assert isinstance(row["locked"], bool) and isinstance(row["in_outbox"], bool)
    assert isinstance(row["missing_in_cloud"], bool)
    assert err == "" or "上傳" in err


def test_json_agent_is_null_for_a_merge(remote, capsys):
    """A merge has no `agora.source`: `agent` has to be null, not the relation."""
    merged = _staged(title="合併 A 與 B", description="合併 2 個 Session",
                     relation="merge", agent=None, tags=["ai:claude-work"])
    code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0
    doc = json.loads(out)
    row = next(r for r in doc if r["id"] == merged)
    assert row["agent"] is None and row["relation"] == "merge"


def test_json_empty_is_an_array_and_exit_zero(remote, capsys):
    code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0 and out == "[]"


def test_json_says_who_is_continuing(remote, capsys):
    agora_id = _staged()
    paths = store.Paths.from_env()
    _, lock = _live_continue(paths, agora_id)
    try:
        code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    finally:
        lock.close()
    assert code == 0
    row = json.loads(out)[0]
    assert row["locked"] is True
    assert row["locked_by"] == {"agent": "opencode", "dir": str(Path.home() / "proj"),
                                "started_at": "2026-10-18T05:01:00Z"}

    code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    row = json.loads(out)[0]
    assert code == 0 and row["locked"] is False and row["locked_by"] is None


def test_json_and_text_name_the_same_sessions(remote, capsys):
    """The ids and their order are the text mode's, so a caller can switch forms."""
    first = _staged(session_id="ses_a", created_at="2026-10-01T09:00:00Z",
                    updated_at="2026-10-01T09:00:00Z", title="先")
    second = _staged(session_id="ses_b", created_at="2026-10-02T09:00:00Z",
                     updated_at="2026-10-02T09:00:00Z", title="後")
    code, text, _ = run(capsys, "search", "session", "--no-sync")
    text_ids = [line.split()[0] for line in text.splitlines()]
    code2, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0 and code2 == 0
    assert text_ids == [second, first]                 # newest conversation first
    assert [row["id"] for row in json.loads(out)] == text_ids


def test_text_rows_are_unchanged_by_the_new_flag(remote, capsys):
    """The old row format is the contract (#90): id, date cell, agent/relation, text, marks."""
    agora_id = _staged(session_id="ses_a", created_at="2026-10-01T09:00:00Z",
                       updated_at="2026-10-01T09:00:00Z", title="CSV 規劃",
                       tags=["ai:claude-work"])
    code, out, _ = run(capsys, "search", "session", "--no-sync")
    assert code == 0
    line = out.splitlines()[0]
    assert line.startswith(f"{agora_id}  2026-10-01  opencode  ")
    assert "(使用中)" not in line and "{" not in line and "[]" not in line


def test_json_dedupes_the_same_source_session_like_the_text_mode(remote, capsys):
    """Two machines importing one agent session: one row, one id, and the note on stderr."""
    older = _staged(session_id="ses_same", created_at="2026-10-01T09:00:00Z",
                    updated_at="2026-10-01T09:00:00Z", title="舊的一份")
    newer = _staged(session_id="ses_same", created_at="2026-10-02T09:00:00Z",
                    updated_at="2026-10-02T09:00:00Z", title="新的一份")
    code, text, err_text = run(capsys, "search", "session", "--no-sync")
    code2, out, err_json = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0 and code2 == 0
    text_ids = [line.split()[0] for line in text.splitlines()]
    json_ids = [row["id"] for row in json.loads(out)]
    assert json_ids == text_ids == [newer]
    assert older != newer
    for err in (err_text, err_json):        # the explanation stays, in both forms
        assert f"{newer} 與 agora:{older.split(':', 1)[1]} 來自同一個來源 Session" in err


def test_json_is_still_a_document_when_drive_is_unreachable(remote, capsys, monkeypatch):
    """Drive down is exit 0 with the warning on stderr; stdout is still one JSON document."""
    agora_id = _staged()
    _forget_the_outbox(agora_id)
    monkeypatch.setenv("AGORA_RCLONE", "/nonexistent/rclone")
    code, out, err = run(capsys, "search", "session", "--json")
    assert code == 0
    assert [row["id"] for row in json.loads(out)] == [agora_id]
    assert "連不上 Drive" in err


def test_json_takes_the_same_filter_as_text(remote, capsys):
    """`--filter` is applied before the form is chosen: the same rows, in both forms."""
    kept = _staged(title="保留", tags=["ai:claude-work"])
    _staged(title="不要", tags=["ai:other"])
    code, text, _ = run(capsys, "search", "session", "--filter", "tags=ai:claude-work", "--no-sync")
    code2, out, _ = run(capsys, "search", "session", "--filter", "tags=ai:claude-work",
                        "--json", "--no-sync")
    assert code == 0 and code2 == 0
    assert [line.split()[0] for line in text.splitlines()] == [kept]
    assert [row["id"] for row in json.loads(out)] == [kept]


def test_json_escapes_control_characters_and_keeps_the_text(remote, capsys):
    """Tabs, quotes and newlines come back exactly; stdout is still one parsable document."""
    _staged(title="第一欄\t引號 \"x\" 反斜線 \\", description="行一\n行二")
    code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0
    row = json.loads(out)[0]
    assert row["title"] == "第一欄\t引號 \"x\" 反斜線 \\"
    assert row["description"] == "行一\n行二"


def test_json_is_refused_by_every_other_action_and_runs_nothing(remote, capsys, monkeypatch):
    """#301 review 中1: `--json` is search's output form. Any other action that gets it says so
    and exits 1 (EXIT_INPUT) before it touches anything (same rule as --add-tag, #153)."""
    agora_id = _staged()
    ran = []
    for action in ("show", "import", "edit"):
        monkeypatch.setitem(cli.ACTIONS, action, lambda args, paths, _a=action: ran.append(_a) or 0)
    argv_by_action = {
        "show": ["show", "session", agora_id, "--json"],
        "import": ["import", "session", "--agent", "opencode", "--json"],
        "edit": ["edit", "session", agora_id, "--json"],
    }
    for action, argv in argv_by_action.items():
        code, out, err = run(capsys, *argv)
        assert code == 1, action
        assert out == "", action
        assert f"{action} 不吃 --json（只有 search 吃）" in err, action
    assert ran == [], "a refused action must not run"


def test_json_on_search_is_unchanged_by_the_guard(remote, capsys):
    agora_id = _staged()
    code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0
    assert [row["id"] for row in json.loads(out)] == [agora_id]


def test_json_says_a_session_missing_in_drive_is_missing_in_cloud(remote, capsys, monkeypatch):
    """#301 review 低2: Drive had it and no longer does (deleted on another machine, synced
    here) - `missing_in_cloud` must say so. Same setup as test_cli's `_lost_in_the_cloud`."""
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    agora_id = _staged()
    paths = store.Paths.from_env()
    store.kick_uploader(paths)                      # foreground upload to the fake Drive
    assert store.outbox_count(paths) == 0
    shutil.rmtree(remote / "agora" / "sessions" / agora_id.split(":", 1)[1])
    assert store.sync(paths).missing_in_cloud() == [agora_id.split(":", 1)[1]]
    code, out, _ = run(capsys, "search", "session", "--json", "--no-sync")
    assert code == 0
    row = json.loads(out)[0]
    assert row["id"] == agora_id
    assert row["missing_in_cloud"] is True
