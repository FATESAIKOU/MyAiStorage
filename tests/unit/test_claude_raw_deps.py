import _guard

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from agora.agents import claude, claude_raw

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_AGORA_AGENTS = REPO_ROOT / "src" / "agora" / "agents"

MOVED_SYMBOLS = (
    "FORMAT",
    "_AUX_CHUNK",
    "_read_chunks",
    "_is_utf8",
    "_iter_aux",
    "_aux_files",
    "_pack_raw",
    "_RAW_HEAD",
    "_AUX_SEP",
    "PackedRaw",
    "_unpack_raw",
)


def _extract_target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in target.elts:
            names.update(_extract_target_names(elt))
        return names
    return set()


def _top_level_defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_extract_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_extract_target_names(node.target))
    return names


def _top_level_imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def test_c1_symbols_moved_and_no_stale_definitions():
    for symbol in MOVED_SYMBOLS:
        assert hasattr(claude_raw, symbol), f"claude_raw missing symbol: {symbol}"

    claude_defs = _top_level_defined_names(SRC_AGORA_AGENTS / "claude.py")
    stale = claude_defs & set(MOVED_SYMBOLS)
    assert not stale, f"Stale top-level definitions remaining in claude.py: {stale}"

    claude_imports = _top_level_imported_modules(SRC_AGORA_AGENTS / "claude.py")
    stale_imports = claude_imports & {"base64", "codecs", "io"}
    assert not stale_imports, f"Stale top-level imports remaining in claude.py: {stale_imports}"


def test_c3_dependencies_static():
    raw_path = SRC_AGORA_AGENTS / "claude_raw.py"
    tree = ast.parse(raw_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "agora" or alias.name.startswith("agora."):
                    assert alias.name == "agora.agents.base", f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"Relative imports are not allowed: level={node.level}"
            if node.module == "agora" or (node.module and node.module.startswith("agora.")):
                assert node.module == "agora.agents.base", f"Forbidden import module: {node.module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in (
                    "__import__",
                    "import_module",
                ), f"Dynamic import call not allowed: {node.func.id}"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr != "import_module", f"import_module call not allowed: {node.func.attr}"


def test_c4_dependencies_runtime():
    cmd = [
        sys.executable,
        "-c",
        "import sys, agora.agents.claude_raw; "
        "print(sorted(m for m in sys.modules if m in ('agora.agents.claude', 'agora.store')))",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Importing claude_raw failed: {res.stderr}"
    assert res.stdout.strip() == "[]"


def test_c5_reexports():
    reexported = (
        "FORMAT",
        "PackedRaw",
        "_AUX_SEP",
        "_RAW_HEAD",
        "_aux_files",
        "_iter_aux",
        "_pack_raw",
        "_unpack_raw",
    )
    for name in reexported:
        claude_obj = getattr(claude, name)
        raw_obj = getattr(claude_raw, name)
        assert claude_obj is raw_obj, f"{name} in claude is not identical to claude_raw.{name}"

    callables_or_classes = (
        "PackedRaw",
        "_aux_files",
        "_iter_aux",
        "_pack_raw",
        "_unpack_raw",
    )
    for name in callables_or_classes:
        obj = getattr(claude, name)
        assert (
            obj.__module__ == "agora.agents.claude_raw"
        ), f"{name}.__module__ is {obj.__module__}, expected agora.agents.claude_raw"


def test_c6_not_reexported():
    for name in ("_AUX_CHUNK", "_read_chunks", "_is_utf8"):
        assert not hasattr(claude, name), f"{name} should not be re-exported by claude"


def test_c7_unpack_raw_seam_active(monkeypatch):
    calls = []
    real = claude._unpack_raw

    def spy(raw: bytes):
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(claude, "_unpack_raw", spy)
    turns = claude.ADAPTER.turns(claude_raw._pack_raw([], {}))
    assert len(calls) == 1
    assert turns == []


def test_c7_pack_raw_seam_active(monkeypatch):
    calls = []
    real = claude._pack_raw

    def spy(main: list[str], aux: dict):
        calls.append((main, aux))
        return real(main, aux)

    monkeypatch.setattr(claude, "_pack_raw", spy)
    raw = claude.ADAPTER.native([])
    assert len(calls) == 1
    assert isinstance(raw, bytes)
