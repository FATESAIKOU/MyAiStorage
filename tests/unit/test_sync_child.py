"""The interactive mode's background sync child (#491 PR-1, C-3 to C-5).

`sync_child.main()` runs one throttled `store.sync` and says how it went on its last
stdout line. The remote is the fake rclone's folder; the second machine that puts
sessions there is another state/cache pair on the same remote (see test_store_more).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import sys
import subprocess
import time
from pathlib import Path

import pytest

from agora import header as h
from agora import store, sync_child

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


def other_machine(tmp_path):
    """The machine that puts sessions on the remote; this test's own paths stay isolated."""
    paths = store.Paths.from_env()
    other = tmp_path / "m2"
    return store.Paths(config=paths.config, cache=other / "cache", state=other / "state")


def publish(paths, n, *, tag="會話"):
    for i in range(n):
        ulid = h.new_ulid()
        hdr = {"type": "Session", "title": f"{tag} {i}", "tags": [], "id": f"agora:{ulid}",
               "refs": [], "case": None,
               "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z",
                         "updated_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": [],
                         "source": {"agent": "opencode", "session_id": f"ses_{i}",
                                    "created_at": "2026-10-01T00:00:00Z"}}}
        store.stage(paths, hdr, "## user\n把 CSV 轉成 Markdown 表格\n", b'{"x": 1}')
    store.upload_batch(store.Drive(paths), paths)


def run_main(capsys):
    code = sync_child.main()
    out = capsys.readouterr().out.splitlines()
    return code, out


def test_c3_result_line_is_last(remote, tmp_path, capsys):
    publish(other_machine(tmp_path), 3)
    code, out = run_main(capsys)
    assert code == 0
    assert out[-1].startswith(sync_child.RESULT)
    assert json.loads(out[-1][len(sync_child.RESULT):]) == {"offline": False}
    assert len(store.Index(store.Paths.from_env()).known()) == 3
    assert (store.Paths.from_env().state / "last-sync").exists()


def test_c3_warn_lines_are_prefixed_and_come_before_the_result(remote, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    code, out = run_main(capsys)
    assert code == 0
    assert out[-1] == sync_child.RESULT + json.dumps({"offline": True}, sort_keys=True)
    assert any(line.startswith("[agora] 連不上 Drive") for line in out[:-1])


def test_c3_offline_is_only_the_offline_note(remote, capsys, monkeypatch):
    # an ordinary warn line is not "offline" - only the sync's offline line is
    monkeypatch.setattr(store, "sync",
                        lambda paths, **kw: kw["warn"]("某個檔讀不到，先跳過") or store.Index(paths))
    code, out = run_main(capsys)
    assert code == 0
    assert out[-1] == sync_child.RESULT + json.dumps({"offline": False}, sort_keys=True)


def test_c3_sync_is_called_once_and_throttled(remote, capsys, monkeypatch):
    calls = []

    def spy(paths, **kw):
        calls.append(kw)
        return store.Index(paths)
    monkeypatch.setattr(store, "sync", spy)
    code, out = run_main(capsys)
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["throttle"] is True
    assert callable(calls[0]["warn"])


def test_c3_fresh_stamp_means_no_listing_of_drive(remote, tmp_path, capsys):
    publish(other_machine(tmp_path), 3)                  # something new is on the remote
    state = store.Paths.from_env().state
    state.mkdir(parents=True, exist_ok=True)
    (state / "last-sync").write_text(str(time.time() - 10))
    calls = tmp_path / "calls.log"
    calls.unlink(missing_ok=True)                        # only the child's calls count
    code, out = run_main(capsys)
    assert code == 0
    assert out[-1] == sync_child.RESULT + json.dumps({"offline": False}, sort_keys=True)
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert not [line for line in lines if "lsjson" in json.loads(line)]


def test_c4_exception_is_the_error_line_one_line(remote, capsys, monkeypatch):
    def boom(paths, **kw):
        raise RuntimeError("壞\n了")
    monkeypatch.setattr(store, "sync", boom)
    code, out = run_main(capsys)
    assert code == 2
    assert out[-1] == "AGORA-SYNC-ERROR RuntimeError: 壞 了"


def test_c4_error_line_is_at_most_300_characters(remote, capsys, monkeypatch):
    def boom(paths, **kw):
        raise RuntimeError("字" * 1000)
    monkeypatch.setattr(store, "sync", boom)
    code, out = run_main(capsys)
    assert code == 2
    assert len(out[-1][len(sync_child.ERROR):]) <= 300


def test_c4_keyboard_interrupt_has_no_result_line(remote, capsys, monkeypatch):
    def interrupted(paths, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(store, "sync", interrupted)
    code, out = run_main(capsys)
    assert code == 130
    assert not [line for line in out if line.startswith((sync_child.RESULT, sync_child.ERROR))]


def test_c5_closed_stdout_still_finishes_the_sync(remote, tmp_path, monkeypatch):
    publish(other_machine(tmp_path), 12)
    # one session whose session.md is no longer a session: the download says so, with a warn line
    next(remote.rglob("session.md")).write_bytes(b"not a session\n")
    proc = subprocess.Popen([sys.executable, "-m", "agora.sync_child"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)   # like spawn_child: one pipe
    try:
        proc.stdout.close()                              # the reader hangs up at once
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert (store.Paths.from_env().state / "last-sync").exists()


def test_c5_inline_uploader_lines_do_not_fail_the_sync(remote, tmp_path):
    # this machine has a session waiting in the outbox: the sync kicks the uploader, which
    # (AGORA_UPLOAD=inline, set by conftest for unit tests) prints bare lines on stderr
    own = store.Paths.from_env()
    publish(own, 1)                                      # stage + upload
    ulid = h.new_ulid()                                  # and stage another, without uploading
    hdr = {"type": "Session", "title": "waiting", "tags": [], "id": f"agora:{ulid}", "refs": [], "case": None,
           "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
                     "relation": "import", "parents": [],
                     "source": {"agent": "opencode", "session_id": "ses_w", "created_at": "2026-10-01T00:00:00Z"}}}
    store.stage(own, hdr, "## user\nx\n", b'{"x": 1}')
    assert store.waiting_ulids(own)
    proc = subprocess.Popen([sys.executable, "-m", "agora.sync_child"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)   # one pipe, like spawn_child
    try:
        proc.stdout.close()                              # the reader hangs up at once
        code = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert code == 0
    assert (own.state / "last-sync").exists()
    assert not store.waiting_ulids(own)


def test_c3_sync_child_holds_at_the_listing_until_released(remote, tmp_path, monkeypatch):
    publish(other_machine(tmp_path), 3)
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_RCLONE_HOLD", str(gate))
    proc = subprocess.Popen([sys.executable, "-m", "agora.sync_child"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        waiting = Path(f"{gate}.waiting")
        deadline = time.monotonic() + 10
        while not waiting.exists():                      # wait for the file, not for a time
            assert time.monotonic() < deadline, "the child never reached the listing"
            time.sleep(0.02)
        assert proc.poll() is None
        gate.touch()
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
