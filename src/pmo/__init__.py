"""`pmo`: the PMO skill command package (`#110`, design `#351`).

T-1 built the skeleton: the schemas and validation for the settings and state
files (design notes `110-pmo-skill.md` §3.4), the four port interfaces and their
fakes (§3.1, §3.3), `sanitize` (D-12), the two environment paths (§3.4) and
`_io`. T-2 added the inbox half: `pmo inbox read|list|ack|report|decision-send`
(`pmo.cli`), the state stores (`pmo.ledger`, `pmo.teamstate`), the round that
`pmo tick` will share (`pmo.round`), the decision questions (`pmo.decision`),
the timestamp shape (`pmo.timeutil`), and the Angareion adapter. `pmo tick` and
the sending/team commands are later tickets (§5.2 T-3..T-5).
"""

from __future__ import annotations

__version__ = "0.1.0"
