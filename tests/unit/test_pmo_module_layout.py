"""模組切割的 import 方向與公開面（#362／#362b）。

兩條單向規則：

- `pmo.decision` 是決定題的家，**不** import `pmo.inbox`（`inbox` 反過來用它）；
- `pmo.ledger` 只依賴儲存層與小工具，**不** import `pmo.runtime`（時戳在
  `pmo.timeutil`，runtime 帶的是指令環境與身分守門，方向不對）。

公開面：決定題的 6 個名稱只在 `pmo.decision.__all__`；`pmo.inbox` 不再轉一手，
只 import 它自己用到的 2 個（內部依賴，不在 `inbox.__all__`）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import ast
from datetime import datetime, timezone
from pathlib import Path

from pmo import decision, inbox, ledger, timeutil

PKG_DIR = Path(__file__).resolve().parents[2] / "src" / "pmo"

DECISION_PUBLIC_NAMES = (
    "build_decision_body",
    "build_decision_send_body",
    "parse_decision_answer",
    "format_decision_event",
    "latest_decision_answers",
    "send_decision",
)


def imported_modules(path: Path) -> set[str]:
    """One module's import list, relative ones included (`from .runtime` -> `runtime`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}" if node.module else alias.name)
    return names


def test_ledger_imports_timeutil_not_runtime():
    imported = imported_modules(PKG_DIR / "ledger.py")
    assert "timeutil" in imported
    assert "runtime" not in imported
    assert ledger.iso_utc is timeutil.iso_utc


def test_decision_does_not_import_inbox():
    imported = imported_modules(PKG_DIR / "decision.py")
    assert "inbox" not in imported
    assert "inbox" not in {name.split(".")[0] for name in imported}


def test_decision_names_live_only_in_decision():
    """6 個名稱只在 `pmo.decision`；`inbox` 不轉匯出（#362b 低-1）。"""
    for name in DECISION_PUBLIC_NAMES:
        assert name in decision.__all__
        assert name not in inbox.__all__

    # 另外 4 個 inbox 沒有用到，連模組屬性都不該有
    for name in (
        "build_decision_body",
        "build_decision_send_body",
        "parse_decision_answer",
        "send_decision",
    ):
        assert not hasattr(inbox, name)

    # inbox 真的用到的 2 個是內部依賴：同一個函式，但不在公開面
    for name in ("format_decision_event", "latest_decision_answers"):
        assert name not in inbox.__all__
        assert getattr(inbox, name) is getattr(decision, name)


def test_inbox_imports_only_the_two_decision_names_it_uses():
    tree = ast.parse((PKG_DIR / "inbox.py").read_text(encoding="utf-8"))
    imported_from_decision: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "decision":
            imported_from_decision.update(alias.name for alias in node.names)
    assert imported_from_decision == {"format_decision_event", "latest_decision_answers"}


def test_timeutil_owns_the_timestamp_shape():
    assert timeutil.iso_utc(datetime(2026, 10, 19, 10, 15, 30, tzinfo=timezone.utc)) == (
        "2026-10-19T10:15:30Z"
    )
