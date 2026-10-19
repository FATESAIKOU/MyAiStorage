"""The load-and-launch half of `agora continue`, run in a fresh interpreter.

Why this exists (ticket impl-mem-start): `cmd_continue` used to read the
session, parse it and call `start_native` in the shell that then sat waiting
for the agent for the rest of the day. Releasing the references (this slice
does that too, in `cli._load_for_launch`) hands the bytes back to Python, but
the allocator's freed peak stays mapped - and under memory pressure macOS
compresses it and keeps charging it to the shell's footprint, so a 100 MB
session left the shell anywhere between ~25 MB and ~800 MB depending on how
the allocator and the OS felt. The fix is to never allocate the peak in the
long-lived process: this child does the whole load, answers with one small
JSON payload, and exits, so the memory goes with it. The round's own boundary
(`agora.round_child`) is the same shape, and its docstring carries the
measurement for why a fresh interpreter rather than `os.fork()`.

The shell (`cli._start_child`) writes the state JSON on stdin and reads the
payload on fd 1. The load itself says nothing on stdout (the agent CLIs it
runs have their output captured), so the pipe carries only the one payload.
stderr is left as the shell's own, so the child's warnings read exactly where
the shell's always did. An error comes back as
`{"err": {"kind": <class name>, "text": str(e)}}`, and the shell raises the
same class again, so the exit codes and the words stay the CLI's.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

from agora import cli, store


def run(state: dict, paths: store.Paths | None = None) -> dict:
    """The payload for one state; a failure is answered, not raised.

    The shell's seam (`tests/unit/_round_seam.in_process_start_child`) calls
    this exact function, so the wire and the error shape are the ones the real
    child uses, boundary aside."""
    try:
        return cli._load_for_launch(paths or store.Paths.from_env(), state)
    except Exception as e:      # answered, the way a round answers its own failure
        return {"err": {"kind": type(e).__name__, "text": str(e)}}


def main() -> int:
    state = json.loads(sys.stdin.read())
    # fd 1 is the shell's pipe, not the user's stdout: anything the load prints
    # (nothing does today) goes to the shell's stderr instead of corrupting the
    # one JSON answer, the same discipline `round_child` keeps.
    with contextlib.redirect_stdout(sys.stderr):
        payload = run(state)
    os.write(1, json.dumps(payload, ensure_ascii=False).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
