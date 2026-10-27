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
import os
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


def test_an_integration_test_also_loses_the_outer_opencode_config(monkeypatch, tmp_path):
    """#269: the drop at the top of `isolated_home` must reach integration tests too.

    A unit test that forgets its fake opencode fails loudly; an integration test is
    the one that would run the real agent with the outer shell's config. Calling the
    fixture with an `integration` request keeps this a unit test (an `integration`
    file would make the repo-clean guard run the real `opencode session list`, and
    `testpaths` would not even collect it). The second assertion proves which branch
    ran: only the unit branch sets the `/nonexistent` agent commands.
    """
    module = guard()
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    monkeypatch.delenv("AGORA_OPENCODE_CMD", raising=False)
    request = type("Request", (), {"keywords": {"integration": True}})()
    # The autouse fixture already made tmp_path/home for this test; call it on a fresh one.
    again = tmp_path / "again"
    again.mkdir()

    module.isolated_home.__wrapped__(again, monkeypatch, request)

    assert "OPENCODE_CONFIG_CONTENT" not in os.environ
    assert "AGORA_OPENCODE_CMD" not in os.environ
