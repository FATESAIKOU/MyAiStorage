"""`agora`：Agora 的單一指令入口（`docs/design/agora-session-operations.md`）。

這個套件**不 import 任何 coding agent 專屬的東西**。載入起點包成原生 session
是轉接器（`agora-opencode load`）的事；Agora 只管讀、寫交接單、產出起點包。
"""

from __future__ import annotations

from aistorage.agora_cli.checkout import (
    CheckoutDeps,
    CheckoutError,
    ClaimRejected,
    checkout,
)
from aistorage.agora_cli.claims import (
    ClaimJournal,
    ClaimJournalError,
    ClaimRecord,
    startpoint_key,
)
from aistorage.agora_cli.package import (
    DEFAULT_MAX_CONTEXT_CHARS,
    ContextLimitExceeded,
    ContextPackage,
    ContextPackageError,
    PackageSegment,
    order_segments,
    read_package,
    write_package,
)
from aistorage.agora_cli.startpoint import (
    DEFAULT_SOURCE,
    ResolvedStartPoint,
    StartPoint,
    StartPointError,
    parse_startpoint,
    resolve_startpoint,
)

__all__ = [
    "CheckoutDeps",
    "CheckoutError",
    "ClaimJournal",
    "ClaimJournalError",
    "ClaimRecord",
    "ClaimRejected",
    "ContextLimitExceeded",
    "ContextPackage",
    "ContextPackageError",
    "DEFAULT_MAX_CONTEXT_CHARS",
    "DEFAULT_SOURCE",
    "PackageSegment",
    "ResolvedStartPoint",
    "StartPoint",
    "StartPointError",
    "checkout",
    "order_segments",
    "parse_startpoint",
    "read_package",
    "resolve_startpoint",
    "startpoint_key",
    "write_package",
]
