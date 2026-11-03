"""Unit tests for the shared state lock (`#358`): flock ＋進程內可重入守衛。

`pmo.ledger.state_lock` 與 `pmo.teamstate.state_lock` 的機制搬進
`pmo._lock.path_lock` 之後，這裡守住它的保證：同一線程可重入、不同路徑互不
阻擋、兩個線程排隊、`timeout=None` 阻塞、逾時丟 `LockTimeout` 且不留殘鎖、
例外時釋放、跨進程仍靠 flock 互斥。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pmo._lock import LockTimeout, _entry_for, path_lock
from pmo.errors import PMOError

#: 這個 repo 的根（給子行程找 `pmo` 套件）。
REPO = Path(__file__).resolve().parents[2]

#: 子行程拿鎖：`acquired`／`timeout` 印一行就結束（時間門檻由呼叫端給寬）。
CHILD = """
import sys
from pathlib import Path
from pmo._lock import LockTimeout, path_lock

try:
    with path_lock(Path(sys.argv[1]), timeout=float(sys.argv[2])):
        print("acquired")
except LockTimeout:
    print("timeout")
"""

#: 子行程拿著鎖不放（給「別的行程持有時的逾時」用），拿到就印一行。
HOLDER = """
import sys
import time
from pathlib import Path
from pmo._lock import path_lock

with path_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def _assert_another_fd_cannot_take(path: Path) -> None:
    """鎖被持有時，同一行程另一個 fd 也拿不到（flock 真的拿著，不只是深度）。"""
    fd = os.open(str(path), os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def _assert_another_fd_can_take(path: Path) -> None:
    """鎖放掉之後，另一個 fd 立刻拿得到（沒有殘留的 fd／深度）。"""
    fd = os.open(str(path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _run_child(path: Path, timeout: float) -> str:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(path), str(timeout)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _run_within(seconds: float, body) -> None:
    """在線程裡跑 `body`；超過 `seconds` 沒結束就當失敗（死鎖會掛住而不是紅燈）。"""
    errors: list[BaseException] = []
    done = threading.Event()

    def runner() -> None:
        try:
            body()
        except BaseException as error:  # noqa: BLE001 - handed back to the test
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"{seconds} 秒內沒有結束（疑似死鎖）")
    assert done.is_set()
    if errors:
        raise errors[0]


def _open_fd_count() -> int:
    """本行程目前開著的 fd 數（`/proc/self/fd`；非 Linux 由呼叫端 skip）。"""
    return len(os.listdir("/proc/self/fd"))


def test_the_same_thread_may_take_the_lock_again(tmp_path):
    """巢狀拿同一把鎖（`send` 與 tick 的讀改寫都在鎖內再呼叫）不會死鎖。"""
    path = tmp_path / "nested.lock"

    def body() -> None:
        with path_lock(path):
            with path_lock(path):
                with path_lock(path, timeout=1.0):
                    _assert_another_fd_cannot_take(path)
                # 內層離開了、外層還拿著：flock 不能跟著放掉（深度計數要對）
                _assert_another_fd_cannot_take(path)
            _assert_another_fd_cannot_take(path)
        _assert_another_fd_can_take(path)

    _run_within(10, body)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="需要 /proc/self/fd（Linux）")
def test_a_timeout_while_polling_does_not_leak_a_descriptor(tmp_path):
    """逾時輪詢每次重試都開一個 fd：到期時要關掉，不能一個一個漏。"""
    path = tmp_path / "poll.lock"

    def body() -> None:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = _open_fd_count()
            with pytest.raises(LockTimeout):
                with path_lock(path, timeout=0.2):
                    pass
            after = _open_fd_count()
        finally:
            os.close(fd)

        assert after == before, f"逾時輪詢漏了 {after - before} 個 fd"

    # 輪詢永不到期時要在這裡變成失敗，而不是把整個測試掛住
    _run_within(20, body)


def test_locks_on_different_paths_do_not_block_each_other(tmp_path):
    """兩個 state 目錄（或兩個 team key）的鎖各自獨立。"""
    first = tmp_path / "state-a" / "a.lock"
    second = tmp_path / "state-b" / "b.lock"
    with path_lock(first):
        with path_lock(second, timeout=0.5):
            assert first.exists()
            assert second.exists()


def test_two_threads_take_the_lock_in_turns(tmp_path):
    path = tmp_path / "turn.lock"
    order: list[str] = []
    second_in = threading.Event()

    def worker() -> None:
        with path_lock(path, timeout=10):
            order.append("second")
            second_in.set()

    with path_lock(path):
        thread = threading.Thread(target=worker, daemon=True)  # 卡住的 worker 不能拖住整個行程
        thread.start()
        time.sleep(0.2)
        assert not second_in.is_set(), "第二個線程在鎖被持有時進來了"
        order.append("first")

    thread.join(10)
    assert not thread.is_alive()
    assert order == ["first", "second"]


def test_timeout_none_blocks_until_the_lock_is_free(tmp_path):
    path = tmp_path / "block.lock"
    got = threading.Event()

    def worker() -> None:
        with path_lock(path):  # timeout=None：一直等
            got.set()

    with path_lock(path):
        thread = threading.Thread(target=worker, daemon=True)  # 卡住的 worker 不能拖住整個行程
        thread.start()
        assert not got.wait(0.2), "鎖還拿著，第二個線程不該拿到"

    assert got.wait(5)
    thread.join(5)
    assert not thread.is_alive()


def test_a_timeout_raises_and_leaves_no_lock_behind(tmp_path):
    path = tmp_path / "busy.lock"
    errors: list[BaseException] = []
    done = threading.Event()
    started = time.monotonic()

    def worker() -> None:
        try:
            with path_lock(path, timeout=0.2):
                errors.append(AssertionError("逾時的鎖不該拿到"))
        except BaseException as error:  # noqa: BLE001 - handed back to the test
            errors.append(error)
        finally:
            done.set()

    with path_lock(path):
        thread = threading.Thread(target=worker, daemon=True)  # 卡住的 worker 不能拖住整個行程
        thread.start()
        thread.join(10)
        assert not thread.is_alive()

    assert done.is_set()
    assert len(errors) == 1, errors
    error = errors[0]
    assert isinstance(error, LockTimeout)
    assert isinstance(error, PMOError)
    assert error.timeout == 0.2
    assert str(path) in str(error)
    assert time.monotonic() - started >= 0.15, "太早放棄（門檻放寬，避免 flaky）"

    # 不留殘鎖：還拿得到，而且拿到的真的是鎖（別人的 fd 進不來）
    with path_lock(path, timeout=1.0):
        _assert_another_fd_cannot_take(path)
    _assert_another_fd_can_take(path)


def test_the_lock_is_released_when_the_body_raises(tmp_path):
    path = tmp_path / "raise.lock"
    with pytest.raises(RuntimeError):
        with path_lock(path):
            raise RuntimeError("boom")

    # 例外之後沒有殘留：還拿得到，且仍是排他的
    with path_lock(path, timeout=1.0):
        _assert_another_fd_cannot_take(path)
    _assert_another_fd_can_take(path)


def test_a_timeout_while_another_process_holds_it_leaves_no_lock_behind(tmp_path):
    """flock 那條路的逾時：別的行程拿著時逾時，之後本行程拿到的仍是真鎖。"""
    path = tmp_path / "held.lock"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), os.environ.get("PYTHONPATH", "")])}
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), "1.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    def body() -> None:
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "locked"
            with pytest.raises(LockTimeout):
                with path_lock(path, timeout=0.2):
                    pass
        finally:
            holder.wait(timeout=30)
            assert holder.returncode == 0, (holder.stderr.read() if holder.stderr else "")

        # 逾時那一次沒有留下深度／fd：這裡拿到的仍是真的 flock
        with path_lock(path, timeout=1.0):
            _assert_another_fd_cannot_take(path)
        _assert_another_fd_can_take(path)

        # 守衛（RLock）也要放掉：本線程可重入，看不出洩漏，換一個線程拿才算數
        def take_in_another_thread() -> None:
            with path_lock(path, timeout=1.0):
                _assert_another_fd_cannot_take(path)

        _run_within(10, take_in_another_thread)

    # 輪詢永不到期時要在這裡變成失敗，而不是把整個測試掛住
    _run_within(20, body)


def test_the_guard_wait_and_the_flock_wait_share_one_deadline(tmp_path):
    """RLock 等待與 flock 輪詢共用同一個期限：總耗時約一個 timeout，不是兩段加起來。"""
    path = tmp_path / "shared.lock"
    entry = _entry_for(path)  # 白箱：只佔住進程內守衛，flock 另外用一個 fd 佔
    holding = threading.Event()
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)

    def hold_guard() -> None:
        entry.guard.acquire()
        holding.set()
        try:
            time.sleep(0.25)
        finally:
            entry.guard.release()

    def body() -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        thread = threading.Thread(target=hold_guard, daemon=True)
        thread.start()
        assert holding.wait(5), "另一個線程沒拿到守衛"
        started = time.monotonic()
        with pytest.raises(LockTimeout):
            with path_lock(path, timeout=0.4):
                pass
        elapsed = time.monotonic() - started
        thread.join(5)
        assert not thread.is_alive()

        assert elapsed >= 0.35, f"太早放棄：{elapsed:.2f} 秒"
        # 共用期限 ≈ 0.4；各自起算會是 0.25 + 0.4 ≈ 0.65
        assert elapsed < 0.4 + 0.12, f"守衛與 flock 沒有共用同一個期限：總耗時 {elapsed:.2f} 秒"

    try:
        _run_within(20, body)
    finally:
        os.close(fd)


def test_timeout_zero_tries_once_and_raises(tmp_path, monkeypatch):
    """`timeout=0`：只試一次就丟，不進輪詢、不睡（`error.timeout` 是呼叫端的 0）。"""
    path = tmp_path / "zero.lock"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    attempts: list[int] = []
    real_flock = fcntl.flock

    def counting_flock(target_fd: int, operation: int) -> None:
        attempts.append(operation)
        real_flock(target_fd, operation)

    monkeypatch.setattr(fcntl, "flock", counting_flock)
    try:
        with pytest.raises(LockTimeout) as info:
            with path_lock(path, timeout=0):
                pass
    finally:
        os.close(fd)

    assert attempts == [fcntl.LOCK_EX | fcntl.LOCK_NB], f"試了 {len(attempts)} 次，應該只試一次"
    assert info.value.timeout == 0


def test_a_negative_timeout_reports_the_value_the_caller_gave(tmp_path):
    """`timeout=-5` 一樣立刻逾時，`error.timeout` 是原值 -5（不是夾成 0.0）。"""
    path = tmp_path / "negative.lock"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockTimeout) as info:
            with path_lock(path, timeout=-5):
                pass
    finally:
        os.close(fd)

    assert info.value.timeout == -5


def test_a_second_process_cannot_take_the_lock(tmp_path):
    """不同的行程靠 flock 互斥；逾時之後也不留殘鎖。"""
    path = tmp_path / "process.lock"

    with path_lock(path):
        assert _run_child(path, timeout=1.0) == "timeout"

    # 放掉之後子行程拿得到（逾時的那一個沒留下任何鎖）
    assert _run_child(path, timeout=10.0) == "acquired"


def test_the_lock_file_is_created_with_its_parent_directory(tmp_path):
    """鎖檔的父目錄不存在時自己建（`state_lock` 原本的行為，薄殼後不變）。"""
    path = tmp_path / "state" / "deep" / "x.lock"
    with path_lock(path):
        assert path.exists()
