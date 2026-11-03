"""`pmo`: the PMO skill command package (`#110` T-1, design `#351`).

T-1 is the skeleton only: the schemas and validation for the settings and
state files (design notes `110-pmo-skill.md` §3.4), the four port interfaces
and their fakes (§3.1, §3.3), `sanitize` (D-12) and the two environment paths
(§3.4, `PMO_SKILL_CONFIG` / `PMO_SKILL_STATE_DIR`). It deliberately has no
command behaviour yet: `pmo tick`／`inbox`／`send` are later tickets (§5.2
T-2..T-5).
"""

from __future__ import annotations

__version__ = "0.1.0"
