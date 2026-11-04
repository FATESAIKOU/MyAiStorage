"""`pmo`: the PMO skill command package (`#110`, design `#351`).

T-1 built the skeleton: the schemas and validation for the settings and state
files (design notes `110-pmo-skill.md` §3.4), the four port interfaces and their
fakes (§3.1, §3.3), `sanitize` (D-12), the two environment paths (§3.4) and
`_io`. T-2 added the inbox half: `pmo inbox read|list|ack|report|decision-send`
(`pmo.cli`), the state stores (`pmo.ledger`, `pmo.teamstate`), the round that
`pmo tick` will share (`pmo.round`), the decision questions (`pmo.decision`),
the timestamp shape (`pmo.timeutil`), and the Angareion adapter. T-3 added the
ledger rebuild (`pmo inbox rebuild`, `pmo.rebuild`); T-4a added the dispatch
front half (`pmo check`／`pmo send`, `pmo.dispatch`, the GitHub adapter).
T-5's first half added the one-round `pmo tick` (`pmo.tick`, #358): the inbox
round, the `GET /agents` comparison, the watchdog and the worker-team
hand-offs; `pmo wake` is the rest of T-5 (§5.2 T-5).
"""

from __future__ import annotations

__version__ = "0.1.0"
