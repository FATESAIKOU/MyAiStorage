"""AiStorage 提交流程完整性機制模組。

依據規格：
- docs/impl/group3-modules.md 第 3 節
- design.md D2（13 步完整性防線）、ADR 0008
- pin: 信任錨點釘選值與 PinStore 儲存
- settle: 結算待定釘選值
- sweep: 上層同名檢查與清掃
- verify: 核對、預檢與 push 後驗證
- gc: 回收已移除 bundle 與隔離資料夾清理
"""

from __future__ import annotations

from aistorage.integrity.gc import (
    DEFAULT_QUARANTINE_DAYS,
    MAX_GC_PER_RUN,
    collect_removed_bundles,
    gc_removed,
    purge_quarantine,
)
from aistorage.integrity.pin import (
    GitPinStore,
    MemoryPinStore,
    PinPending,
    PinState,
    PinStore,
)
from aistorage.integrity.settle import (
    RepoListing,
    SettleOutcome,
    check_manifest_continuity,
    settle,
)
from aistorage.integrity.sweep import (
    Disposition,
    PrefixLevel,
    SettleAndSweepResult,
    SweepDecision,
    apply_sweep,
    check_parents,
    plan_readview_sweep,
    plan_sweep,
    resolve_content_checks,
    resolve_manifest_evidence,
    run_settle_and_sweep,
)
from aistorage.integrity.verify import (
    PushVerification,
    precheck,
    verify_after_push,
    verify_annex_coverage,
    verify_clone,
)

__all__ = [
    "DEFAULT_QUARANTINE_DAYS",
    "Disposition",
    "GitPinStore",
    "MAX_GC_PER_RUN",
    "MemoryPinStore",
    "PinPending",
    "PinState",
    "PinStore",
    "PrefixLevel",
    "PushVerification",
    "RepoListing",
    "SettleAndSweepResult",
    "SettleOutcome",
    "SweepDecision",
    "apply_sweep",
    "check_manifest_continuity",
    "check_parents",
    "collect_removed_bundles",
    "gc_removed",
    "plan_readview_sweep",
    "plan_sweep",
    "precheck",
    "purge_quarantine",
    "resolve_content_checks",
    "resolve_manifest_evidence",
    "run_settle_and_sweep",
    "settle",
    "verify_after_push",
    "verify_annex_coverage",
    "verify_clone",
]
