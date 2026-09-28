"""`agora-opencode`：把起點包載入成 opencode 的原生 session。

實作在 `loader.py`（不碰 CLI，可直接單元測試）。做法與三個不能省的規則見
`loader.py` 的模組說明與 `docs/spike/session-import.md`。
"""

from __future__ import annotations

from aistorage.adapters.opencode.loader import (
    TITLE_PREFIX,
    AdapterError,
    LoadResult,
    build_export,
    chain_parents,
    load,
    reidentify,
    run_import,
    truncate_to,
)

__all__ = [
    "AdapterError",
    "LoadResult",
    "TITLE_PREFIX",
    "build_export",
    "chain_parents",
    "load",
    "reidentify",
    "run_import",
    "truncate_to",
]
