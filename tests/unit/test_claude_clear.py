"""After /clear, collect names the session that holds the rest (CL5, verified on 2.1.286)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import time

from agora.agents import claude as C
from agora.agents.base import Launch


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))


def test_collect_points_at_the_post_clear_session(tmp_path, capsys):
    work = tmp_path / "proj"
    work.mkdir()
    project = C.projects_dir() / C.encode_project_dir(work)
    old, new = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    _write(project / f"{old}.jsonl", [
        {"type": "user", "sessionId": old, "cwd": str(work), "timestamp": "2026-10-02T00:00:00Z",
         "message": {"role": "user", "content": "把 CSV 轉成表格"}},
        {"type": "assistant", "sessionId": old, "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}]}},
    ])
    time.sleep(0.05)
    _write(project / f"{new}.jsonl", [
        {"type": "attachment", "sessionId": new, "session_id": old},
        {"type": "user", "sessionId": new, "message": {"role": "user", "content": "清除後"}},
    ])
    os.utime(project / f"{new}.jsonl")
    got = C.ADAPTER.collect(Launch(argv=[], cwd=str(work), agent_session_id=old, before_count=0))
    assert got is not None and got.session_id == old
    err = capsys.readouterr().err
    assert new in err and "agora import session --external-session-id" in err


def _fork(project, old, new, work):
    """What start_native writes for a fork: our new id on the lines, the old one still on
    the attachment line's `session_id` (CL5), and the launched marker (#199)."""
    target = project / f"{new}.jsonl"
    _write(target, [
        {"type": "attachment", "sessionId": new, "cwd": str(work), "session_id": old},
        {"type": "user", "sessionId": new, "cwd": str(work),
         "message": {"role": "user", "content": "從這條分支出去"}},
    ])
    C._mark_launched(new, target)
    return target


def _old_session(project, old, work):
    path = project / f"{old}.jsonl"
    _write(path, [
        {"type": "user", "sessionId": old, "cwd": str(work), "timestamp": "2026-10-02T00:00:00Z",
         "message": {"role": "user", "content": "把 CSV 轉成表格"}},
        {"type": "assistant", "sessionId": old, "message": {"role": "assistant", "content": [{"type": "text", "text": "好"}]}},
    ])
    return path


def test_a_file_agora_forked_is_not_reported_as_a_clear(tmp_path, capsys):
    """The fork's lines still name the old session; agora wrote that file itself."""
    work = tmp_path / "proj"
    work.mkdir()
    project = C.projects_dir() / C.encode_project_dir(work)
    old, new = "44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"
    _old_session(project, old, work)
    time.sleep(0.05)
    target = _fork(project, old, new, work)
    os.utime(target)
    got = C.ADAPTER.collect(Launch(argv=[], cwd=str(work), agent_session_id=old, before_count=0))
    assert got is not None and got.session_id == old
    err = capsys.readouterr().err
    assert new not in err and "/clear" not in err


def test_a_clear_file_agora_did_not_write_still_warns(tmp_path, capsys):
    """No launched marker (a real /clear): the old behaviour is kept."""
    work = tmp_path / "proj"
    work.mkdir()
    project = C.projects_dir() / C.encode_project_dir(work)
    old, new = "66666666-6666-4666-8666-666666666666", "77777777-7777-4777-8777-777777777777"
    _old_session(project, old, work)
    time.sleep(0.05)
    target = project / f"{new}.jsonl"
    _write(target, [
        {"type": "attachment", "sessionId": new, "cwd": str(work), "session_id": old},
        {"type": "user", "sessionId": new, "cwd": str(work),
         "message": {"role": "user", "content": "清除後"}},
    ])
    os.utime(target)
    C.ADAPTER.collect(Launch(argv=[], cwd=str(work), agent_session_id=old, before_count=0))
    err = capsys.readouterr().err
    assert new in err and "agora import session --external-session-id" in err


def test_no_warning_without_clear(tmp_path, capsys):
    work = tmp_path / "proj"
    work.mkdir()
    project = C.projects_dir() / C.encode_project_dir(work)
    sid = "33333333-3333-4333-8333-333333333333"
    _write(project / f"{sid}.jsonl", [
        {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "sessionId": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ])
    C.ADAPTER.collect(Launch(argv=[], cwd=str(work), agent_session_id=sid, before_count=0))
    assert "/clear" not in capsys.readouterr().err
