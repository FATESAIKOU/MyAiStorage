"""Keep the agent from outliving a `kill -9`ed agora (#229).

`agora continue` starts the agent with its own `Popen`. Ctrl-C is left to the
agent (agora ignores SIGINT, waits for the agent, writes the last conversation
back and leaves), and a headless agora handles SIGTERM the same way; but a
plain (not `--headless`) agora that gets SIGTERM dies on the default action
without writing back, and a SIGKILL or an OOM kill runs no handler at all: the
agent - and its own children - would keep running with nothing waiting for
them.

So agora starts this module (`python -m agora.orphan_guard`) as a second,
minimal process *before* the agent (#422), and the agent reports its own pid:

* agora holds the write end of a pipe that the agent never inherits (the
  fds are not inheritable; the agent's `Popen` closes them before exec);
* the pipe speaks one record per line: ``P<pid>`` (written by the agent's own
  child process, between fork and exec, so a kill at any moment after the fork
  still leaves the reaper knowing who the agent is), ``S<lstart>`` (agora's
  reading of the agent's start time, taken right after `Popen` returns, before
  any `wait`) and ``D`` (goodbye);
* every normal way out of `cmd_continue` writes ``D`` into the pipe and closes
  it - the reaper sees a line that is exactly ``D`` and leaves 0, killing
  nothing;
* if the pipe reaches EOF without that line, agora was killed without saying
  goodbye: when a ``P`` was seen, the reaper sends SIGTERM to the agent (and
  its direct children) and, after a grace period, SIGKILL to whatever is
  still there; with no ``P`` the agent was never born and nothing is killed.

Limits, on purpose:

* Only a dead agora makes the reaper act. A SIGTERM'ed headless agora is not
  an orphan case: it waits for the agent, writes back, then says goodbye. A
  plain agora is not that patient - SIGTERM kills it outright, with no
  write-back and no goodbye byte - so the reaper collects the agent; that is
  accepted on purpose (the agent used to be left behind, with nobody to write
  its work back and nobody to stop it).
* Ctrl-C is not a death: agora ignores SIGINT and lets the agent have it, then
  leaves through the normal path (goodbye byte and all).
* If agora and the reaper die together (the same OOM sweep, `kill -9` of the
  whole session), nobody is left to reap.
* Grandchildren are best effort: they are collected through the agent's direct
  children only, and only while they still look like the same processes
  (start time re-checked) so a reused pid is never signalled.
* There is no process-group kill: agora's process group may well be the
  person's shell job group.

`ps` is the portable way to ask both questions (`-o lstart= -p <pid>` and
`-eo pid=,ppid=` work the same on macOS and Linux); a process we cannot
identify is never signalled.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
# The reaper's own launch goes through this alias, so a test can make the
# start fail without touching every other Popen in the process (and a test
# that wraps `subprocess.Popen` elsewhere does not wrap the reaper).
from subprocess import Popen as _spawn

NORMAL = b"D"                 # the goodbye line (``D\n``) agora writes on every normal leave
GRACE_ENV = "AGORA_ORPHAN_GRACE"
DEFAULT_GRACE = 10.0
POLL_SECONDS = 0.1


def _warn(why: object) -> None:
    print(f"[agora] 無法啟動孤兒防護（agent 照常執行，agora 被 kill -9／OOM 時它不會被收）：{why}",
          file=sys.stderr)


def start_time(pid: int) -> str | None:
    """The `ps -o lstart=` line of a live pid, or None when it is gone/unknown.

    Two readings of the same process are equal strings; a pid that now belongs
    to something else answers with a different start time (or None). That is
    what keeps a reused pid from being killed.
    """
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    return line or None


def same_process(pid: int, recorded: str | None) -> bool:
    """Is `pid` still the process whose start time is `recorded`?"""
    if recorded is None:
        return False
    return start_time(pid) == recorded


def _children(pid: int) -> list[int]:
    """The direct children of `pid` (one portable `ps`, best effort)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == str(pid):
            try:
                found.append(int(fields[0]))
            except ValueError:
                continue
    return found


def _signal(pid: int, recorded: str | None, sig: int) -> None:
    """Signal a pid only when it still is the recorded process."""
    if not same_process(pid, recorded):
        return
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _alive(pid: int) -> bool:
    """Does anything answer to this pid? (Cheap poll; identity is `_signal`'s job.)"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_orphan(pid: int, recorded: str | None, grace: float) -> str:
    """TERM the orphan agent (and its direct children), KILL what is left.

    Returns what happened (``"gone"``, ``"term"`` or ``"kill"``) for the log
    line; the caller does not act on it.
    """
    if not same_process(pid, recorded):
        return "gone"
    targets = {pid: recorded}
    for child in _children(pid):
        if child != pid:
            targets[child] = start_time(child)
    for target, start in targets.items():
        _signal(target, start, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(_alive(t) for t in targets):
            return "term"
        time.sleep(POLL_SECONDS)
    left = [t for t in targets if _alive(t)]
    if not left:
        return "term"
    for target in left:
        _signal(target, targets[target], signal.SIGKILL)
    return "kill"


def _lines(fd: int):
    """Each complete line agora wrote, as it arrives; a trailing partial line is dropped."""
    pending = b""
    while True:
        try:
            chunk = os.read(fd, 64)
        except InterruptedError:
            continue
        except OSError:
            return
        if not chunk:
            return
        pending += chunk
        while True:
            line, sep, rest = pending.partition(b"\n")
            if not sep:
                break
            pending = rest
            yield line


def grace() -> float:
    """The grace this process was told to use (`AGORA_ORPHAN_GRACE`, default 10).

    The reaper waits this long between TERM and KILL; `continue` uses the same
    number as the limit for an agent it already asked to stop (#107), so one
    setting moves both.
    """
    return _grace(None)


def _grace(value: float | None) -> float:
    if value is not None:
        return value
    raw = os.environ.get(GRACE_ENV)
    if not raw:
        return DEFAULT_GRACE
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_GRACE


def main(argv: list[str] | None = None) -> int:
    """The reaper: wait for agora's goodbye, or clean up after a dead agora."""
    parser = argparse.ArgumentParser(prog="agora.orphan_guard", add_help=True)
    parser.add_argument("--fd", type=int, required=True, help="the pipe's read end")
    parser.add_argument("--grace", type=float, default=None, help="seconds between TERM and KILL")
    args = parser.parse_args(argv)
    pid: int | None = None
    recorded: str | None = None
    said_start = False
    goodbye = False
    # Record by line, as they come: from the moment `P` arrives, a pid that
    # starts answering with another time is not ours.
    for line in _lines(args.fd):
        if line == NORMAL:
            goodbye = True
        elif line[:1] == b"P":
            try:
                pid = int(line[1:])
            except ValueError:
                continue
            if not said_start:
                recorded = start_time(pid)
        elif line[:1] == b"S":
            recorded = os.fsdecode(line[1:])
            said_start = True
    try:
        os.close(args.fd)
    except OSError:
        pass
    if goodbye:
        return 0
    if pid is None:
        return 0                  # the agent was never born
    if recorded is None:
        return 0                  # cannot identify it: never signal a stranger
    stop_orphan(pid, recorded, _grace(args.grace))
    return 0


class Guard:
    """Agora's handle on the reaper: the agent's pid, its start time, one goodbye."""

    def __init__(self, proc: subprocess.Popen, fd: int):
        self.proc = proc
        self.fd = fd
        self._done = False

    def child_hook(self) -> None:
        """Run in the agent's own child (`preexec_fn`): tell the reaper who it is.

        Only C-level calls, nothing imported or allocated at length: this runs
        between fork and exec. SIGPIPE is ignored around the write because
        `Popen` has put it back to the default already; a reaper that is gone
        must not kill the agent before it starts.
        """
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        try:
            os.write(self.fd, b"P%d\n" % os.getpid())
        except OSError:
            pass
        finally:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    def started(self, pid: int) -> None:
        """Right after `Popen` returns: agora's own reading of the agent's start time.

        Taken before any `wait`, so it is the agent and not a later owner of
        the pid; it overrides what the reaper read for itself. Best effort.
        """
        recorded = start_time(pid)
        if recorded is None:
            return
        try:
            os.write(self.fd, b"S" + os.fsencode(recorded) + b"\n")
        except OSError:
            pass

    def done(self) -> None:
        """The normal leave: say goodbye, close, and let the reaper go."""
        if self._done:
            return
        self._done = True
        try:
            os.write(self.fd, NORMAL + b"\n")
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            # It leaves on its own when agora's death finally closes the pipe;
            # a reaper that hangs must not hang agora's exit.
            pass


def arm() -> Guard | None:
    """Start the reaper, before the agent; None (after a warning) when it cannot.

    The read end travels through `pass_fds` only: the agent (not yet started),
    rclone, the round child and the background uploader never hold it - the
    write end is not inheritable at all, so EOF really does mean this process
    died. The agent learns the write end through `Guard.child_hook`, which runs
    in its own child before the fds are closed.
    """
    try:
        read_fd, write_fd = os.pipe()
    except OSError as exc:
        _warn(exc)
        return None
    argv = [sys.executable, "-m", "agora.orphan_guard", "--fd", str(read_fd)]
    try:
        proc = _spawn(argv,
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, start_new_session=True,
                      close_fds=True, pass_fds=(read_fd,),
                      # `-m` puts the cwd first on the path: point it at the
                      # directory this very module came from, so the reaper
                      # always runs this code and not a stray `agora` in the
                      # folder the person happened to be in.
                      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except Exception as exc:                     # noqa: BLE001 - never fail continue
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        _warn(exc)
        return None
    try:
        os.close(read_fd)          # the reaper has its own copy now
    except OSError:
        pass
    return Guard(proc, write_fd)


if __name__ == "__main__":
    sys.exit(main())
