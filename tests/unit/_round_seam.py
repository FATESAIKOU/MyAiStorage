"""The two process boundaries of a continue, run in-process (test-side DI).

`cli._start_child` normally spawns `python -m agora.start_child` for the
load-and-launch half, and `cli._round_child` spawns `python -m
agora.round_child` for the heavy half of a periodic write-back round. Tests
whose fake agent lives in THIS process (the `env` fixtures: in-memory
sessions, in-memory spies) patch the seams with the in-process versions:
the state still round-trips through JSON, the same child body runs, and the
shell still handles the answer - every wire rule is exercised, only the
process boundary is not. What that cannot show - spawning, death classes,
the group kill, the lock hand-down - is exactly what
`test_continue_round_child.py` (and, for the load, the real-child tests in
`test_continue_headless.py`) runs with REAL children.
"""

from __future__ import annotations

import json

from agora import cli, round_child, start_child


def in_process_round_child(paths_, run_, agent_, lock_fd=None):
    state = json.loads(json.dumps(round_child.state_of(run_)))
    mini = round_child.decode_run(state, paths_)
    return round_child.run_round(mini, agent_)


def in_process_start_child(paths_, state_):
    return cli._start_payload(start_child.run(json.loads(json.dumps(state_)), paths_))
