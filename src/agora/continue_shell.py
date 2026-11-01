"""Subprocess protocol shell for continue rounds and start child.

Handles spawning child processes for round execution and session loading,
sending state, enforcing timeouts, reaping process groups, mapping errors,
and applying diffs back to the caller's run state.

Does not import cli, round_child, or start_child.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import subprocess
import sys
from pathlib import Path

from agora import header as h
from agora import store
from agora.agents.base import Agent, AgentError, env_seconds
from agora.errors import InputError

# --- moved verbatim (#341) ---
CONTINUE_LOG = "continue.log"
#: The dead-man switch on a round. Everything else in the round carries its own
#: deadline (the opencode export, the summarize budget) except rclone - which has
#: none (store.Drive._run) - so the round as a whole gets one here. Generous by
#: design: a killed round loses a round's work, and on a slow link a big session
#: can legitimately take minutes to stage and upload; this only fires when the
#: round is truly wedged. `AGORA_ROUND_TIMEOUT`, seconds, like the other two.
ROUND_TIMEOUT_SECONDS = 1800.0
ROUND_TIMEOUT_MAX_SECONDS = 7 * 24 * 3600.0   # `communicate` raises OverflowError past ~2.1e6 s (#117)


def _round_timeout() -> float:
    """`communicate(timeout=0)` fires at once: a 0 or negative
    `AGORA_ROUND_TIMEOUT` - often typed for "no limit" - would kill every round
    (review #1). `inf` is no deadline either (`communicate` raises OverflowError)
    and `nan` is not a number at all (ValueError), so both killed every round the
    same way (#117), and so does any finite number past ~2.1e6 seconds (the C int
    `selectors` converts to): that is clamped to a week. A number that is not a usable,
    finite deadline answers as the default. The guard is HERE, not in `env_seconds`: the other callers keep their
    exact reading, zero and all."""
    value = env_seconds("AGORA_ROUND_TIMEOUT", ROUND_TIMEOUT_SECONDS)
    if not (math.isfinite(value) and value > 0):
        return ROUND_TIMEOUT_SECONDS
    return min(value, ROUND_TIMEOUT_MAX_SECONDS)    # "no limit" typed as 1e10 means "very long", not "kill at 30 min"


class RoundChildDead(Exception):
    """A round that did not come back with an answer the shell can use: killed,
    crashed, exited nonzero, or returned a broken payload. One counted failure,
    nothing applied, the next round looks again (design impl-mem-subproc)."""


def _round_child(paths: store.Paths, run: "_Run", agent: Agent, lock_fd: int | None = None):
    """Run one write-back round in a fresh interpreter and return its payload.

    The heavy work - read the whole session, parse it, lay it out, stage it -
    happens in `agora.round_child`, a child that exits at the round's end, so
    the session-sized peak is memory the OS takes back instead of one the
    day-long shell keeps resident (impl-mem-subproc; why not `os.fork()`: the
    module's docstring, with the measurement).

    `agent` is not used by the real child - a fresh interpreter loads it by
    name from the record - but the signature is the seam the test world
    replaces with an in-process round, and both then feed the same `_apply_diff`
    and the same outcome logic below. The child's stderr is continue.log, so a
    death before its own logging starts is still readable where every other
    round word lands; the pending lock fd is passed on so an orphaned round
    (the shell SIGKILLed) holds the record busy until it finishes, keeping the
    next command's recovery from writing beside it. Raises RoundChildDead with
    the text for this round's one line."""
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(paths.state / CONTINUE_LOG, "ab") as err_sink:
        proc = subprocess.Popen([sys.executable, "-m", "agora.round_child"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err_sink,
                                pass_fds=(lock_fd,) if lock_fd is not None else (),
                                start_new_session=True)
        try:
            out, _err = proc.communicate(json.dumps(state_of(run)).encode(),
                                         timeout=_round_timeout())
        except subprocess.TimeoutExpired:
            _reap_group(proc)
            raise RoundChildDead("子行程異常結束（逾時）")
    if proc.returncode != 0:
        raise RoundChildDead(f"子行程異常結束（rc={proc.returncode}）")
    try:
        return json.loads(out)
    except ValueError as e:
        raise RoundChildDead(f"子行程異常結束（狀態壞掉：{e}）")


def _kill_child_group(proc) -> bool:
    """SIGTERM the round's whole group - the child leads its own
    (`start_new_session`), so the rclone or `opencode export` grandchildren
    live there too - then ALWAYS SIGKILL the group: the round child now exits
    on TERM by itself, which says nothing about a grandchild that ignores it
    (#74). Anything that must outlive the round has to `setsid` itself: the
    detached uploader does (`start_new_session`), so a wedged round can be
    killed without ever taking an in-flight upload down with it. Returns
    whether the group is known dead: False when `killpg` was refused or the
    leader is still alive after SIGKILL (review #4)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True                             # already gone: nothing to kill
    except PermissionError:
        return False                            # refused: the round is still live
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    # SIGKILL the group either way: a leader that left quietly on SIGTERM (since B1b-2
    # the round child does) says nothing about a wedged rclone/opencode grandchild
    # that ignores SIGTERM and would otherwise outlive the round. No member left means
    # ProcessLookupError, which is the good answer.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5.0)                  # bounded: a refused kill must not hang us
    except subprocess.TimeoutExpired:
        return False                            # the leader is still live: nothing worked
    return True


def _reap_group(proc, *, catch: type[BaseException] = Exception) -> None:
    """Kill a round or start child's process group and drain its pipes.

    First terminates the group with _kill_child_group(proc), then boundedly
    drains what the corpse left. If the group is still alive or drain fails,
    forces proc.kill() and proc.wait(timeout=5). Suppresses exceptions matching
    `catch` (defaults to Exception; callers dealing with cancellation pass
    BaseException so secondary interrupts during cleanup do not leave orphans).

    The invariant: cleanup is bounded - the shell never hangs on a dead child's
    pipes - and on cancellation the original exception wins, which is why `catch`
    must accept BaseException there; do not narrow it to Exception."""
    group_dead = _kill_child_group(proc)
    try:
        proc.communicate(timeout=5)
    except catch:
        group_dead = False
    if not group_dead:
        with contextlib.suppress(catch):
            proc.kill()
            proc.wait(timeout=5)


class StartChildDead(AgentError):
    """The startup load child did not come back with an answer the shell can use:
    killed, crashed, exited nonzero, or answered garbage. A failed continue, the
    way a load failure always was - the message says it was the load child
    (impl-mem-start; before this, a hard death here was the shell's own)."""


def _start_child(paths: store.Paths, state: dict) -> dict:
    """Run the load-and-launch half of `continue` in a fresh interpreter.

    `agora.start_child` fetches the raw, takes the md5s, calls `start_native`
    and - for a fork - reads the new session back and stages it, then exits:
    the session-sized bytes live and die in that child, and the shell carries
    nothing of them while it waits (impl-mem-start; why a fresh interpreter
    and not `os.fork()`: `agora.round_child`'s docstring). What comes back is
    one small JSON payload. The child's stderr is the shell's, so a warning it
    makes reads exactly where it always did; a killed or broken child counts
    as a failed continue, with the group killed like a wedged round's. `paths`
    is the seam's signature; the real child rebuilds the paths from the env it
    inherits (the same directories the shell has)."""
    proc = subprocess.Popen([sys.executable, "-m", "agora.start_child"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            start_new_session=True)
    try:
        out, _err = proc.communicate(json.dumps(state).encode(), timeout=_round_timeout())
    except subprocess.TimeoutExpired:
        _reap_group(proc)
        raise StartChildDead("載入接續失敗：子行程逾時")
    except BaseException:
        # Ctrl-C (or anything else driving the shell out) while the load child is
        # still working: it leads its own session, so the terminal's SIGINT never
        # reached it - kill the group here, then let the original exception go.
        _reap_group(proc, catch=BaseException)
        raise
    if proc.returncode != 0:
        raise StartChildDead(f"載入接續失敗：子行程異常結束（rc={proc.returncode}）")
    try:
        payload = json.loads(out)
    except ValueError as e:
        raise StartChildDead(f"載入接續失敗：子行程狀態壞掉（{e}）")
    return _start_payload(payload)


def _start_payload(payload: dict) -> dict:
    """A child's answer, with its named failure raised in this process.

    The seam's in-process child uses this too, so a test world sees the same
    conversion the real boundary performs."""
    if payload.get("err") is not None:
        _raise_child_error(payload["err"])
    return payload


#: The exceptions `agora.start_child` can name in its payload, by class name.
#: A named type is raised as itself again - the CLI's exit code and words hang
#: on it (InputError vs AgentError, #54 and the M7 fork messages).
_CHILD_ERRORS = {"InputError": InputError, "AgentError": AgentError,
                 "StoreError": store.StoreError, "HeaderError": h.HeaderError}


def _raise_child_error(err: dict) -> None:
    """Raise what the load child caught, so the caller sees the same failure.

    Anything not in `_CHILD_ERRORS` is rebuilt under its own name: that is all
    the generic handler prints of an unexpected exception (`非預期的錯誤：Name: …`)."""
    cls = _CHILD_ERRORS.get(err.get("kind"))
    if cls is None:
        cls = type(err.get("kind") or "Exception", (Exception,), {})
    raise cls(err.get("text", ""))


#: The seven fields one round owes the outer shell; `_apply_diff` refuses a
#: payload that is missing any of them (a broken answer lands whole or not at
#: all), and the tests name the same list.
ROUND_DIFF_KEYS = ("last_md5", "fingerprint", "collect_failed", "wrote",
                   "target", "pending", "first_note")


def _apply_diff(run: "_Run", payload: dict) -> None:
    """Bring a clean round's small state back into the outer shell's `_Run`.
    Every field is read by name: a payload missing one is a broken return and
    raises `KeyError`, which the round's death branch counts as a failure -
    silently skipping a field would make a round half-land. The keys are
    checked BEFORE anything moves (review #2): assigning field by field let an
    early field land when a later one was missing, so a dead round changed the
    shell's `last_md5` or `target` after all. Refused whole instead."""
    missing = [key for key in ROUND_DIFF_KEYS if key not in payload]
    if missing:
        raise KeyError("round payload is missing " + ", ".join(missing))
    run.last_md5 = payload["last_md5"]
    run.fingerprint = decode_fingerprint(payload["fingerprint"])
    run.collect_failed = payload["collect_failed"]
    run.wrote = payload["wrote"]
    run.target = payload["target"]
    if payload["pending"] is not None:
        run.pending = Path(payload["pending"])
    run.first_note = payload["first_note"]


def state_of(run) -> dict:
    """The outer shell's `_Run`, minus everything big. `record` crosses whole
    because the round only reads it (nothing on the round path writes it)."""
    return {"record": run.record,
            "last_md5": run.last_md5,
            "fingerprint": run.fingerprint,
            "collect_failed": run.collect_failed,
            "target": run.target,
            "wrote": run.wrote,
            "first_note": run.first_note,
            "pending": str(run.pending) if run.pending else None}


def decode_fingerprint(fp):
    """JSON turns every tuple into a list; `_collect_new` compares a stat MARK
    against `run.fingerprint` with `==`, and a tuple never equals a list -
    restore it or the skip check silently re-reads every session every round."""
    if isinstance(fp, list):
        return tuple(decode_fingerprint(x) for x in fp)
    return fp
