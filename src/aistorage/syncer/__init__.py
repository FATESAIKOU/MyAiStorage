"""同步器的套件初始化（tasks 5.2／5.3）。

對外提供：OpencodeApi、SyncState、sync_once、sync_and_commit。
"""

from __future__ import annotations

from aistorage.syncer.commit import (
    Awaited,
    CommitWaitResult,
    SyncDeps,
    sync_and_commit,
    trigger_committer,
    wait_visible,
)
from aistorage.syncer.config import ConfigError, SyncerConfig
from aistorage.syncer.core import (
    MAX_RAW_BYTES,
    Signer,
    SyncOutcome,
    sync_once,
)
from aistorage.syncer.opencode_api import OcSession, OpencodeApi
from aistorage.syncer.state import SessionSyncRecord, SyncState

__all__ = [
    "MAX_RAW_BYTES",
    "Awaited",
    "CommitWaitResult",
    "ConfigError",
    "OcSession",
    "OpencodeApi",
    "SessionSyncRecord",
    "Signer",
    "SyncDeps",
    "SyncOutcome",
    "SyncState",
    "SyncerConfig",
    "sync_and_commit",
    "sync_once",
    "trigger_committer",
    "wait_visible",
]
