import _guard

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from agora import cli, continue_shell, errors, round_child

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_AGORA = REPO_ROOT / "src" / "agora"

MOVED_SYMBOLS_CONTINUE_SHELL = (
    "CONTINUE_LOG",
    "ROUND_TIMEOUT_SECONDS",
    "ROUND_TIMEOUT_MAX_SECONDS",
    "_round_timeout",
    "RoundChildDead",
    "_round_child",
    "_kill_child_group",
    "_reap_group",
    "StartChildDead",
    "_start_child",
    "_start_payload",
    "_CHILD_ERRORS",
    "_raise_child_error",
    "ROUND_DIFF_KEYS",
    "_apply_diff",
    "state_of",
    "decode_fingerprint",
)

STALE_CHECK_SYMBOLS = set(MOVED_SYMBOLS_CONTINUE_SHELL) | {
    "_kill_round_group",
    "InputError",
}

FORBIDDEN_CONTINUE_SHELL_MODULES = (
    "agora.cli",
    "agora.round_child",
    "agora.start_child",
)
FORBIDDEN_CONTINUE_SHELL_AGORA_NAMES = (
    "cli",
    "round_child",
    "start_child",
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


def test_c1_symbols_moved_and_no_stale_definitions():
    for symbol in MOVED_SYMBOLS_CONTINUE_SHELL:
        assert hasattr(continue_shell, symbol), f"continue_shell is missing symbol: {symbol}"

    cli_defs = _top_level_defined_names(SRC_AGORA / "cli.py")
    stale_in_cli = cli_defs & STALE_CHECK_SYMBOLS
    assert not stale_in_cli, f"Stale top-level definitions remaining in cli.py: {stale_in_cli}"

    round_defs = _top_level_defined_names(SRC_AGORA / "round_child.py")
    stale_in_round = round_defs & {"state_of", "decode_fingerprint"}
    assert not stale_in_round, f"Stale top-level definitions remaining in round_child.py: {stale_in_round}"


def test_c3_dependencies_static():
    cs_path = SRC_AGORA / "continue_shell.py"
    tree = ast.parse(cs_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in FORBIDDEN_CONTINUE_SHELL_MODULES, f"Forbidden import: {alias.name}"
                assert not any(
                    alias.name.startswith(f"{m}.") for m in FORBIDDEN_CONTINUE_SHELL_MODULES
                ), f"Forbidden import prefix: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"Relative imports are not allowed: level={node.level}"
            if node.module == "agora":
                for alias in node.names:
                    assert (
                        alias.name not in FORBIDDEN_CONTINUE_SHELL_AGORA_NAMES
                    ), f"Forbidden name from agora: {alias.name}"
            elif node.module is not None:
                assert (
                    node.module not in FORBIDDEN_CONTINUE_SHELL_MODULES
                ), f"Forbidden import module: {node.module}"
                assert not any(
                    node.module.startswith(f"{m}.") for m in FORBIDDEN_CONTINUE_SHELL_MODULES
                ), f"Forbidden import module prefix: {node.module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in (
                    "__import__",
                    "import_module",
                ), f"Dynamic import call not allowed: {node.func.id}"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr != "import_module", f"import_module call not allowed: {node.func.attr}"

    errors_path = SRC_AGORA / "errors.py"
    errors_tree = ast.parse(errors_path.read_text(encoding="utf-8"))
    for node in ast.walk(errors_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "agora" and not alias.name.startswith(
                    "agora."
                ), f"errors.py must not import agora: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"Relative imports are not allowed in errors.py: level={node.level}"
            assert node.module != "agora" and (
                node.module is None or not node.module.startswith("agora.")
            ), f"errors.py must not import from agora: {node.module}"
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
        "import sys, agora.continue_shell; print(sorted(m for m in sys.modules if m in ('agora.cli','agora.round_child','agora.start_child')))",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "[]"


def test_c5_cli_reexports():
    reexported_names = [
        "CONTINUE_LOG",
        "RoundChildDead",
        "StartChildDead",
        "_apply_diff",
        "_reap_group",
        "_round_child",
        "_round_timeout",
        "_start_child",
        "_start_payload",
    ]
    for name in reexported_names:
        cli_obj = getattr(cli, name)
        cs_obj = getattr(continue_shell, name)
        assert cli_obj is cs_obj, f"{name} in cli is not identical to continue_shell.{name}"
        if name != "CONTINUE_LOG":
            assert (
                cli_obj.__module__ == "agora.continue_shell"
            ), f"{name}.__module__ is {cli_obj.__module__}, expected agora.continue_shell"

    assert cli.InputError is errors.InputError
    assert errors.InputError.__module__ == "agora.errors"


def test_c7_round_child_names_preserved():
    assert round_child.state_of is continue_shell.state_of
    assert round_child.decode_fingerprint is continue_shell.decode_fingerprint


def test_c8_kill_child_group_renamed():
    assert hasattr(continue_shell, "_kill_child_group")
    assert not hasattr(cli, "_kill_round_group")
    assert not hasattr(continue_shell, "_kill_round_group")
