"""住民工具（tasks 5.4，docs/impl/group5-7 第 4 節）。

plugin 端只做兩件事：把 `context.sessionID` 傳進來、轉呼叫
`python -m aistorage.skill <cmd>`。所有邏輯都在這裡。

**沒有認領工具**：接手新 session 走 `agora checkout`（`aistorage.agora_cli`），
它在產出起點包時一併登記認領（ADR 0010）。
"""

from __future__ import annotations

from aistorage.skill.tools import (
    MainSessionRequired,
    RejectedItems,
    SkillDeps,
    SkillError,
    find,
    handoff_end,
    list_handoffs,
    normalize_parts,
    read,
    reference,
    resolve_session,
    split,
    stop,
    whoami,
)

__all__ = [
    "MainSessionRequired",
    "RejectedItems",
    "SkillDeps",
    "SkillError",
    "find",
    "handoff_end",
    "list_handoffs",
    "normalize_parts",
    "read",
    "reference",
    "resolve_session",
    "split",
    "stop",
    "whoami",
]
