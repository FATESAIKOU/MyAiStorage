"""跨進程檔案鎖＋進程內可重入鎖，綁在一個路徑上（#358；狀態儲存層共用）。

`ledger.state_lock` 與 `teamstate.state_lock` 原本各自有一份幾乎相同的實作
（登錄表、`RLock`、flock 的開關與深度）。這裡只留一份機制；路徑由呼叫端決定
（`ledger` 用 `<state_dir>/secretary-inbox.json.lock`、`teamstate` 用
`<state_dir>/<key>.json.lock`），兩個模組對外仍是原本的薄殼。

`timeout=None` 時阻塞到取得；給了秒數時以 `LOCK_NB` 加短暫重試，到期丟
`LockTimeout`（`PMOError` 的子類，訊息只帶路徑，不帶檔案內容）。逾時不會
留下半套狀態：深度、fd 與進程內的 `RLock` 都回到拿鎖前的樣子，之後仍可取。
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import PMOError

#: 有 timeout 時的重試間隔（秒）：夠短讓 10／30 秒的上限不會多等太久。
POLL_INTERVAL = 0.02


class LockTimeout(PMOError):
    """在給定的秒數內拿不到鎖（`path` 與 `timeout` 是屬性，訊息只帶路徑）。"""

    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__(f"拿不到鎖：{path}")
        self.path = path
        self.timeout = timeout


class _LockEntry:
    """One lock path's state: the fd, its depth, and a re-entrant guard."""

    __slots__ = ("fd", "depth", "guard")

    def __init__(self) -> None:
        self.fd: int | None = None
        self.depth = 0
        self.guard = threading.RLock()


#: Locks are per lock file (state directory, or state directory + team key): the
#: old module-level fd meant a second path in the same process was not locked.
_locks: dict[Path, _LockEntry] = {}
_locks_guard = threading.Lock()


def _entry_for(path: Path) -> _LockEntry:
    with _locks_guard:
        return _locks.setdefault(path, _LockEntry())


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


class _Expired(Exception):
    """期限到了（內部用；`path_lock` 換成對外的 `LockTimeout`）。"""


def _take_guard(entry: _LockEntry, deadline: float | None) -> None:
    """拿下這條路徑的進程內 `RLock`（同一線程可重入；別的線程排隊）。"""
    if deadline is None:
        entry.guard.acquire()
        return
    if not entry.guard.acquire(timeout=_remaining(deadline)):
        raise _Expired()


def _take_flock(path: Path, deadline: float | None) -> int:
    """開 fd 並取得 flock；有期限時 `LOCK_NB` 重試到期限為止（到期丟 `_Expired`）。"""
    if deadline is None:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    while True:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            left = _remaining(deadline)
            if left <= 0:
                raise _Expired() from None
            time.sleep(min(POLL_INTERVAL, left))
        except BaseException:
            os.close(fd)
            raise
        else:
            return fd


@contextmanager
def path_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """跨進程 flock ＋進程內可重入鎖，綁在一個檔案路徑上。

    - 同一線程可重入（巢狀拿同一路徑不會死鎖，flock 只開一次）；
    - 不同路徑各自獨立（兩個 state 目錄、或兩個 team 的狀態檔互不阻擋）；
    - `timeout=None` 時阻塞到取得；給秒數時到期丟 `LockTimeout`，且不留下
      深度、fd 或 `RLock` 的殘留（之後仍取得到）。
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    entry = _entry_for(lock_path)
    # `timeout=None` 一直等；給秒數時 RLock 與 flock 輪詢共用同一個期限。
    # `given` 是呼叫端原值（負值照傳，不夾成 0.0），只有拿來算期限時才夾。
    given = 0.0 if timeout is None else timeout
    limit = max(0.0, given)
    deadline = None if timeout is None else time.monotonic() + limit

    try:
        _take_guard(entry, deadline)
    except _Expired:
        raise LockTimeout(lock_path, given) from None
    try:
        if entry.depth == 0:
            try:
                entry.fd = _take_flock(lock_path, deadline)
            except _Expired:
                raise LockTimeout(lock_path, given) from None
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
            if entry.depth == 0 and entry.fd is not None:
                fd, entry.fd = entry.fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
    finally:
        entry.guard.release()
