"""轉接器：把 Agora 的起點包載入成某個 coding agent 的原生 session。

**每個 coding agent 一個子套件**，命名對應 `agora-<coding agent 名稱>`
（`agora-opencode`、之後的 `agora-claude-code`…），和同步器放在一起。

為什麼分開：Agora 本身不依賴任何 coding agent——它只管讀、寫交接單、產出
起點包。載入與開 agent 是轉接器與呼叫者的事。所以 `aistorage.agora_cli`
**不 import 這裡的任何東西**（`tests/unit/test_agora_cli_smoke.py` 有測這件事）。
"""

from __future__ import annotations

__all__ = ["opencode"]
