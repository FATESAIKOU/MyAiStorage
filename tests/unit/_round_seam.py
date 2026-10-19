"""The round child's boundary, run in-process (test-side DI on a real seam).

`cli._round_child` normally spawns `python -m agora.round_child` for the heavy
half of a periodic write-back round. Tests whose fake agent lives in THIS
process (the `env` fixtures: in-memory sessions, in-memory spies) patch the seam
with `in_process_round_child`: the state still round-trips through JSON, the
same `round_child.run_round` body runs on a rebuilt mini-run, and the outer
`_periodic_write` still applies the payload - every wire rule is exercised,
only the process boundary is not. What that cannot show - spawning, death
classes, the group kill, the lock hand-down - is exactly what
`test_continue_round_child.py` runs with REAL children.
"""

from __future__ import annotations

import json

from agora import cli, round_child


def in_process_round_child(paths_, run_, agent_, lock_fd=None):
    state = json.loads(json.dumps(round_child.state_of(run_)))
    mini = round_child.decode_run(state, paths_)
    return round_child.run_round(mini, agent_)
