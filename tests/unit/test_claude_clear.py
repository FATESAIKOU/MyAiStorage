"""After /clear, collect names the session that holds the rest (CL5, verified on 2.1.286)."""

from __future__ import annotations

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
