import _guard  # noqa: F401  (isolation first)

import ast
from pathlib import Path

import angareion
import angareion.backend as backend


def _production_modules():
    """Every module of the installed angareion package except the fake itself (D-35)."""
    package = Path(angareion.__file__).parent
    modules = sorted(p for p in package.glob("*.py") if p.name != "fake_backend.py")
    assert any(p.name == "backend.py" for p in modules), "scan found no backend.py"
    return modules


def _imports_fake_backend(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "angareion.fake_backend" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module == "angareion.fake_backend":
            return True
        if node.module == "angareion" and node.level == 0:
            return any(alias.name == "fake_backend" for alias in node.names)
        if node.level and node.module == "fake_backend":
            return True
        if node.level and node.module is None:
            return any(alias.name == "fake_backend" for alias in node.names)
    return False


def test_d35_no_production_module_imports_fake_backend():
    offenders = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _imports_fake_backend(node):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"production code imports the fake backend: {offenders}"


def test_d35_backend_does_not_reexport_the_fake():
    assert not hasattr(backend, "FakeBackend")
    assert not hasattr(backend, "FakeComment")
