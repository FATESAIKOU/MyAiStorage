"""每個模組都必須能單獨 import（回歸測試）。

先前 `aistorage.agora.__init__` → `agora.apply` → `intake.evaluate` → `agora.store`
形成循環，導致 `import aistorage.intake.evaluate`（連帶 intake 套件）在任何
「先 import 它」的順序下都直接失敗——只在某個測試先 import 了別的模組時才會被遮住。

每個模組都在**全新的 interpreter** 裡單獨 import，才測得到真正的「它是不是第一個
被 import 的模組」；同一個行程裡依序 import 會被前面已載入的模組遮掉循環。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import pytest

SRC = Path(__file__).resolve().parent.parent.parent / "src"


def _module_names() -> list[str]:
    """列出 aistorage 套件下所有可 import 的模組（__init__ 對應套件名）。"""
    names: list[str] = []
    for path in sorted((SRC / "aistorage").rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        names.append(".".join(parts))
    return names


MODULE_NAMES = _module_names()


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
    )


def test_module_list_is_not_empty():
    assert len(MODULE_NAMES) > 30
    assert "aistorage.intake.evaluate" in MODULE_NAMES
    assert "aistorage.agora.apply" in MODULE_NAMES


#: 當初形成循環的幾個模組：逐一當成第一個 import，失敗時指名報錯
WATCHED = (
    "aistorage.intake",
    "aistorage.intake.evaluate",
    "aistorage.intake.ledger",
    "aistorage.intake.scan",
    "aistorage.agora",
    "aistorage.agora.apply",
    "aistorage.agora.store",
)


@pytest.mark.parametrize("module", WATCHED)
def test_watched_module_imports_standalone(module: str):
    result = _import_in_fresh_interpreter(module)
    assert result.returncode == 0, (
        f"`import {module}` 失敗（單獨 import）：\n{result.stderr.strip()[-800:]}"
    )


def test_every_module_imports_standalone():
    """全部模組各自在全新 interpreter 裡當第一個 import（並行執行以免太慢）。"""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_import_in_fresh_interpreter, MODULE_NAMES))
    failed = [
        (name, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")
        for name, r in zip(MODULE_NAMES, results)
        if r.returncode != 0
    ]
    assert not failed, "這些模組無法單獨 import：" + "; ".join(
        f"{name}: {err}" for name, err in failed
    )
