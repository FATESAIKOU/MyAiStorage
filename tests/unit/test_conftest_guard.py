"""The repo-clean guard's failure path (change agora-opencode-v2, 1.10/2nd review).

The guard runs a real `opencode session list` against the user's store, so its
*call* cannot be a unit test; the shape of its answer can. What the second review
of step 1 asked to be pinned: when the call fails or lies, the guard fails the test
out loud - it must never answer an empty set, which is exactly how a guard whose
flags no longer match the installed opencode goes silently blind.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import importlib.util
import subprocess
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent.parent / "conftest.py"


def guard():
    """conftest.py loaded as a module of its own, so its helpers are callable.

    pytest loads the real conftest under its own name; this copy is only for
    calling the small functions in it.
    """
    spec = importlib.util.spec_from_file_location("conftest_under_test", CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Done(subprocess.CompletedProcess):
    def __init__(self, stdout="", stderr="", returncode=0):
        super().__init__(argv := [], returncode=returncode, stdout=stdout, stderr=stderr)


def test_a_failed_list_fails_the_test_instead_of_reporting_an_empty_set():
    """The 1.x flags on a 2.x opencode make the call exit non-zero; a guard that
    answered an empty set would let every integration test past it (review M5)."""
    proc = Done(stderr="Unexpected positional argument", returncode=1)
    with pytest.raises(pytest.fail.Exception, match="失敗"):
        guard()._session_ids_from(proc, "opencode session list")


def test_a_non_json_list_fails_the_test_too():
    proc = Done(stdout="Exporting session: x", returncode=0)
    with pytest.raises(pytest.fail.Exception, match="不是 JSON"):
        guard()._session_ids_from(proc, "opencode session list")


def test_only_the_ids_of_real_rows_are_kept():
    proc = Done(stdout='[{"id": "ses_a"}, {"no_id": true}, "not-a-row", {"id": "ses_b"}]',
                returncode=0)
    assert guard()._session_ids_from(proc, "opencode session list") == {"ses_a", "ses_b"}


@pytest.mark.parametrize("v2,standalone", [(True, True), (False, False)])
def test_the_list_uses_the_installed_opencode_s_syntax(monkeypatch, v2, standalone):
    module = guard()
    monkeypatch.setattr(module, "_agent",
                        type("Probe", (), {"session_export_supported": staticmethod(lambda: v2)}))
    seen: list[list[str]] = []

    def run(argv, **kwargs):
        seen.append(argv)
        return Done(stdout="[]", returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._repo_session_ids() == set()
    assert seen == [["opencode", "session", "list",
                     *(["--standalone"] if standalone else []), "--format", "json"]]
