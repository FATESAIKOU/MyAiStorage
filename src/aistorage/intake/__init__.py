"""AiStorage 收件匣處理模組套件。

依據規格：
- docs/impl/group3-modules.md 第 4 節
- tasks 3.3/3.4 收件匣處理
"""

from aistorage.intake.evaluate import (
    Decision,
    DecisionKind,
    evaluate,
    sort_accepted_decisions,
    stamp_record,
    strict_json,
)
from aistorage.intake.ledger import (
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETENTION_MONTHS,
    Ledger,
    LedgerEntry,
    is_item_key_too_old,
    parse_ulid_timestamp_ms,
)
from aistorage.intake.scan import (
    InboxItem,
    count_shaped,
    is_actionable,
    scan_inboxes,
)

__all__ = [
    # scan
    "InboxItem",
    "is_actionable",
    "count_shaped",
    "scan_inboxes",
    # ledger
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_RETENTION_MONTHS",
    "LedgerEntry",
    "Ledger",
    "parse_ulid_timestamp_ms",
    "is_item_key_too_old",
    # evaluate
    "DecisionKind",
    "Decision",
    "strict_json",
    "stamp_record",
    "evaluate",
    "sort_accepted_decisions",
]
