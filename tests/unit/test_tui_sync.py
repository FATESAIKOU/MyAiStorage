"""The interactive mode's background sync, as the TUI sees it (#491 PR-1, C-6 and C-7).

No Textual here. The child is a fake with a gate, the `post` is either the plain
synchronous call or a stand-in that refuses to run on the thread that called `start()`
(Textual's `call_from_thread` does the same there), and the outcome is waited for with
an Event - never with a sleep.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import os
import sys
import threading
from pathlib import Path

import pytest

from agora import sync_child, tui_sync
from agora.tui_sync import BackgroundSync, DONE, FAILED, OFFLINE, parse_outcome

RESULT, ERROR = tui_sync.RESULT, tui_sync.ERROR


class FakeProc:
    """A child whose stdout waits for `gate` before it yields `lines`; `wait()` gives `code`.

    Like a real pipe, the child cannot exit while its output is still unread: `wait()`
    blocks until the reader has taken every line, and a reader that waits for the exit
    before it reads deadlocks here (the wait gives up after 5 s and raises).
    """

    def __init__(self, lines=(), code=0, gate=None, broken_read=False):
        self.pid = 4242
        self._lines = list(lines)
        self._code = code
        self._gate = gate
        self._broken = broken_read
        self._drained = threading.Event()
        self.stdout = self._read()

    def _read(self):
        if self._gate is not None:
            self._gate.wait(10)
        if self._broken:
            raise OSError("pipe went bad")
        yield from self._lines
        self._drained.set()

    def wait(self):
        if not self._drained.wait(5):
            raise RuntimeError("the child is blocked on its unread output")
        return self._code


class SameThreadPost:
    """Stands in for `app.call_from_thread`: refuses the thread that called `start()`.

    `owner` is set by the test just before `start()`, not at construction, so the stand-in
    is not tied to whichever thread built it.
    """

    def __init__(self):
        self.owner = None
        self.calls = 0

    def __call__(self, fn, *args):
        if threading.get_ident() == self.owner:
            raise RuntimeError("call_from_thread on the app's own thread")
        self.calls += 1
        return fn(*args)


class Recorder:
    def __init__(self):
        self.outcomes = []
        self.called = threading.Event()

    def __call__(self, outcome):
        self.outcomes.append(outcome)
        self.called.set()


def make_sync(spawn, post=None, clock=None):
    recorder = Recorder()
    post = post or SameThreadPost()
    kwargs = {"clock": clock} if clock else {}
    sync = BackgroundSync(spawn=spawn, post=post, on_done=recorder, **kwargs)
    return sync, recorder, post


def start_here(sync, post):
    post.owner = threading.get_ident()
    return sync.start()


def finished(recorder):
    assert recorder.called.wait(10), "on_done never came"
    return recorder.outcomes


# ---- C-6: the outcome rules ----

def test_c6_zero_with_result_line_is_done():
    assert parse_outcome(0, [f"{RESULT}" + '{"offline": false}']).state == DONE


def test_c6_zero_with_offline_true_is_offline():
    assert parse_outcome(0, [f"[agora] x", f"{RESULT}" + '{"offline": true}']).state == OFFLINE


def test_c6_last_result_line_decides_and_extra_lines_after_are_fine():
    lines = [f"{RESULT}" + '{"offline": true}', "tail", f"{RESULT}" + '{"offline": false}']
    assert parse_outcome(0, lines).state == DONE


def test_c6_zero_without_result_line_is_failed_with_last_line():
    out = parse_outcome(0, ["[agora] a", "", "[agora] 最後"])
    assert out.state == FAILED
    assert out.detail == "同步子行程結束碼 0：[agora] 最後"


def test_c6_zero_without_result_line_and_no_output_has_no_suffix():
    out = parse_outcome(0, [])
    assert out.state == FAILED and out.detail == "同步子行程結束碼 0"


def test_c6_zero_with_result_that_is_not_json_is_failed():
    assert parse_outcome(0, [RESULT + "{offline"]).state == FAILED


def test_c6_result_object_without_offline_key_is_done():
    # only an explicit true says offline; a missing key is not a dead Drive
    assert parse_outcome(0, [RESULT + '{"x": 1}']).state == DONE


def test_c6_zero_with_result_that_is_not_an_object_is_failed():
    assert parse_outcome(0, [RESULT + "[1, 2]"]).state == FAILED


def test_c6_code_two_with_error_line_is_failed_with_its_text():
    out = parse_outcome(2, ["[agora] x", ERROR + "RuntimeError: 壞 了"])
    assert out.state == FAILED and out.detail == "RuntimeError: 壞 了"


def test_c6_code_two_without_error_line_names_the_code():
    out = parse_outcome(2, ["[agora] 還在跑"])
    assert out.state == FAILED and out.detail == "同步子行程結束碼 2：[agora] 還在跑"


def test_c6_code_130_is_interrupted_failed():
    out = parse_outcome(130, [])
    assert out.state == FAILED and "被中斷" in out.detail


def test_c6_negative_code_is_interrupted_failed():
    out = parse_outcome(-15, ["[agora] x"])
    assert out.state == FAILED and "被中斷" in out.detail and "-15" in out.detail


def test_c6_130_is_never_done_even_with_a_result_line():
    assert parse_outcome(130, [RESULT + '{"offline": false}']).state == FAILED


def test_c6_detail_is_at_most_200_characters():
    assert len(parse_outcome(2, [ERROR + "字" * 500]).detail) == 200
    assert len(parse_outcome(3, ["字" * 500]).detail) == 200


def test_c6_outcome_keeps_every_line():
    lines = ["[agora] a", RESULT + '{"offline": false}']
    assert parse_outcome(0, lines).lines == tuple(lines)


def test_c6_the_constants_match_sync_child():
    assert tui_sync.RESULT == sync_child.RESULT == "AGORA-SYNC "
    assert tui_sync.ERROR == sync_child.ERROR == "AGORA-SYNC-ERROR "


def test_c6_child_argv_runs_the_module():
    assert tui_sync.child_argv() == [sys.executable, "-m", "agora.sync_child"]


def test_c7_child_env_sets_the_three_dirs_and_unbuffered_output(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEP_ME", "yes")
    config, cache, state = tmp_path / "c", tmp_path / "k", tmp_path / "s"
    env = tui_sync.child_env(config, cache, state)
    assert env["AGORA_CONFIG"] == str(config)
    assert env["AGORA_CACHE_DIR"] == str(cache)
    assert env["AGORA_STATE_DIR"] == str(state)
    assert env["PYTHONUNBUFFERED"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert env["KEEP_ME"] == "yes"


# ---- C-7: BackgroundSync ----

def test_c7_spawn_child_starts_a_new_session():
    # Q-3: the child leads its own session, so a ctrl+c in the terminal does not reach it
    proc = tui_sync.spawn_child([sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
                                dict(os.environ))
    try:
        assert proc.stdout.read().strip() == "True"
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_c7_start_while_running_is_false_and_spawns_nothing():
    gate = threading.Event()
    spawned = []

    def spawn():
        spawned.append(1)
        return FakeProc([RESULT + '{"offline": false}'], gate=gate)

    sync, recorder, post = make_sync(spawn)
    assert start_here(sync, post) is True
    assert sync.running is True
    assert start_here(sync, post) is False
    assert len(spawned) == 1
    gate.set()
    finished(recorder)


def test_c7_on_done_once_per_start_and_through_post():
    sync, recorder, post = make_sync(lambda: FakeProc([RESULT + '{"offline": false}']))
    assert start_here(sync, post) is True
    outcomes = finished(recorder)
    assert len(outcomes) == 1 and outcomes[0].state == DONE
    assert post.calls == 1                                 # the only way to tell the app
    assert sync.running is False


def test_c7_a_second_start_after_done_runs_again_and_tells_once_more():
    procs = iter([FakeProc([RESULT + '{"offline": false}']), FakeProc([RESULT + '{"offline": true}'])])
    sync, recorder, post = make_sync(lambda: next(procs))
    assert start_here(sync, post) is True
    finished(recorder)
    recorder.called.clear()
    assert start_here(sync, post) is True
    recorder.called.wait(10)
    assert [o.state for o in recorder.outcomes] == [DONE, OFFLINE]


def test_c7_output_of_two_thousand_lines_does_not_block_the_reader():
    lines = [f"[agora] 第 {i} 行" + "．" * 40 for i in range(1999)] + [RESULT + '{"offline": false}']
    sync, recorder, post = make_sync(lambda: FakeProc(lines))
    assert start_here(sync, post) is True
    outcome = finished(recorder)[0]
    assert outcome.state == DONE and len(outcome.lines) == 2000


def test_c7_spawn_oserror_is_raised_and_nothing_is_told(monkeypatch):
    calls = []

    def spawn():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("no such python")
        return FakeProc([RESULT + '{"offline": false}'])

    sync, recorder, post = make_sync(spawn)
    post.owner = threading.get_ident()
    with pytest.raises(OSError):
        sync.start()
    assert sync.running is False
    assert post.calls == 0 and recorder.outcomes == [] and not recorder.called.is_set()
    assert start_here(sync, post) is True                  # a later start works
    assert len(calls) == 2
    assert finished(recorder)[0].state == DONE


def test_c7_unreadable_stdout_is_failed_with_the_reason():
    sync, recorder, post = make_sync(lambda: FakeProc(broken_read=True))
    assert start_here(sync, post) is True
    outcome = finished(recorder)[0]
    assert outcome.state == FAILED and "讀不到輸出" in outcome.detail


def test_c7_elapsed_follows_the_injected_clock():
    gate = threading.Event()
    now = [100.0]
    sync, recorder, post = make_sync(lambda: FakeProc([RESULT + '{"offline": false}'], gate=gate),
                                     clock=lambda: now[0])
    assert sync.elapsed() == 0.0 and sync.started_at is None
    start_here(sync, post)
    now[0] = 107.5
    assert sync.started_at == 100.0 and sync.elapsed() == 7.5
    gate.set()
    finished(recorder)
    assert sync.elapsed() == 0.0


def test_c7_the_stand_in_refuses_the_start_thread():
    # the stand-in itself must bite, or the tests above prove nothing about the thread
    post = SameThreadPost()
    post.owner = threading.get_ident()
    with pytest.raises(RuntimeError):
        post(lambda: None)
