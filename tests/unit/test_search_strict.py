"""`agora search session --strict` (#441, #439 U-2＝B): Drive 連不上要明確失敗.

契約編號對應 docs/design-notes/439-role-session-recall.md §4／§7(b)／U-2／U-13：
  S1 Drive 列舉失敗 → exit 2（本機索引為空、有舊索引各一次）
  S2 sessions/ 找不到 → exit 2（兩種夾具各一次）
  S3 逐筆讀不到 → 仍 exit 0，其他結果正常（兩種夾具各一次）
  S4 不走 throttle（stamp 新鮮時仍連 Drive）
  S5 不加 --strict 行為不變（連不上／找不到 sessions/ 仍 exit 0）
  S6 沒有新介面以外的副作用：--strict 與 --no-sync 互斥、其他動作不吃 --strict
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from agora import cli, store
from agora import header as h

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def _header(title):
    ulid = h.new_ulid()
    agora = {"header": 2, "created_at": "2026-10-01T09:00:00Z", "updated_at": "2026-10-20T09:00:00Z",
             "relation": "import", "parents": [],
             "source": {"agent": "opencode", "session_id": f"ses_{ulid}", "created_at": "2026-10-01T09:00:00Z"}}
    return {"type": "Session", "title": title, "description": "d", "tags": ["role:r"],
            "id": f"agora:{ulid}", "refs": [], "case": None, "agora": agora}


def _staged(title):
    hdr = _header(title)
    paths = store.Paths.from_env()
    folder = store.stage(paths, hdr, "## user\nhello\n", b'{"x": 1}')
    store.remember(paths, folder)
    return hdr["id"]


def _on_drive(title):
    """One session that is in the local index AND uploaded to the fake Drive."""
    agora_id = _staged(title)
    paths = store.Paths.from_env()
    store.upload_batch(store.Drive(paths), paths)
    assert not store.waiting_ulids(paths)
    return agora_id


def _fixture(kind):
    """'empty': no local index at all; 'old': an index that already knows one session."""
    if kind == "old":
        return _on_drive("舊的")
    return None


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err


def _calls(remote):
    log = remote.parent / "calls.log"
    return log.read_text().count("lsjson") if log.exists() else 0


KINDS = ["empty", "old"]


@pytest.mark.parametrize("kind", KINDS)
def test_S1_listing_failure_exits_2(remote, capsys, monkeypatch, kind):
    _fixture(kind)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    code, out, err = _run(capsys, "search", "session", "--strict", "--json")
    assert code == 2, err
    assert out == ""                                  # no stale rows leak out as a result
    assert "連不上 Drive" in err


@pytest.mark.parametrize("kind", KINDS)
def test_S1_listing_failure_does_not_touch_last_sync(remote, capsys, monkeypatch, kind):
    _fixture(kind)
    stamp = store.Paths.from_env().state / "last-sync"
    before = stamp.read_text() if stamp.exists() else None
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    assert _run(capsys, "search", "session", "--strict")[0] == 2
    assert (stamp.read_text() if stamp.exists() else None) == before


@pytest.mark.parametrize("kind", KINDS)
def test_S2_missing_sessions_folder_exits_2(remote, capsys, kind):
    _fixture(kind)
    shutil.rmtree(remote / "agora" / "sessions", ignore_errors=True)     # Drive has no sessions/
    code, out, err = _run(capsys, "search", "session", "--strict", "--json")
    assert code == 2, err
    assert out == ""
    assert "sessions/" in err


@pytest.mark.parametrize("kind", KINDS)
def test_S3_one_unreadable_session_is_not_a_failure(remote, capsys, monkeypatch, kind):
    _fixture(kind)
    good = _on_drive("好的")
    bad = _on_drive("壞的")
    bad_ulid = bad.split(":", 1)[1]
    # the bad one: its session.md cannot be downloaded (read fails one by one)
    # and it is not yet in this machine's index
    paths = store.Paths.from_env()
    idx = store.Index(paths)
    shutil.rmtree(paths.mirror / bad_ulid, ignore_errors=True)
    idx.drop(bad_ulid)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", f"sessions/{bad_ulid}/session.md")
    code, out, err = _run(capsys, "search", "session", "--strict", "--json")
    assert code == 0, err
    assert "讀不到" in err                              # the bad one really was hit
    ids = {r["id"] for r in json.loads(out)}
    assert good in ids and bad not in ids


@pytest.mark.parametrize("kind", KINDS)
def test_S4_strict_does_not_use_the_throttle(remote, capsys, kind):
    _fixture(kind)
    (remote / "agora" / "sessions").mkdir(parents=True, exist_ok=True)   # an empty Drive still has sessions/
    assert _run(capsys, "search", "session")[0] == 0           # fresh stamp now
    stamp = store.Paths.from_env().state / "last-sync"
    assert stamp.exists() and time.time() - float(stamp.read_text()) < 60
    before = _calls(remote)
    assert _run(capsys, "search", "session")[0] == 0
    assert _calls(remote) == before                             # plain search: throttled
    assert _run(capsys, "search", "session", "--strict")[0] == 0
    assert _calls(remote) > before                              # --strict: asked Drive again


@pytest.mark.parametrize("kind", KINDS)
def test_S5_without_strict_a_listing_failure_still_exits_0(remote, capsys, monkeypatch, kind):
    old = _fixture(kind)
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    code, out, err = _run(capsys, "search", "session", "--json")
    assert code == 0 and "連不上 Drive" in err
    assert [r["id"] for r in json.loads(out)] == ([old] if old else [])


@pytest.mark.parametrize("kind", KINDS)
def test_S5_without_strict_missing_sessions_still_exits_0(remote, capsys, kind):
    old = _fixture(kind)
    shutil.rmtree(remote / "agora" / "sessions", ignore_errors=True)
    code, out, err = _run(capsys, "search", "session", "--json")
    assert code == 0 and "找不到 sessions/" in err
    assert [r["id"] for r in json.loads(out)] == ([old] if old else [])


@pytest.mark.parametrize("kind", KINDS)
def test_S5_strict_success_matches_plain_search(remote, capsys, kind):
    _fixture(kind)
    _on_drive("新的")
    plain = _run(capsys, "search", "session", "--json")
    strict = _run(capsys, "search", "session", "--strict", "--json")
    assert plain[0] == strict[0] == 0
    assert plain[1] == strict[1]


def test_S6_strict_and_no_sync_are_mutually_exclusive(remote, capsys):
    code, _, err = _run(capsys, "search", "session", "--strict", "--no-sync")
    assert code == 1 and "--strict" in err and "--no-sync" in err


def test_S6_other_actions_refuse_strict(remote, capsys):
    code, _, err = _run(capsys, "show", "session", "x", "--strict")
    assert code == 1 and "不吃 --strict" in err
