"""The tests' helpers must only run in an isolated environment (T8).

`tests/conftest.py` isolates every test: a fresh HOME, and agora's config, cache and
state in a temporary directory. That isolation only exists *inside* pytest. Importing a
test module directly - `python -c "import test_tui"`, or a probe that copies a helper
out - runs its helpers against the user's real `~/.cache/agora` and
`~/.local/state/agora`: three fake sessions ended up in the real mirror that way, and on
10-04 a diagnosis printed real sessions' ids and titles into an external model's
conversation.

Being under pytest is not isolation by itself. pytest loads a conftest for the test
files *under its own directory*, so on 10-04 a test file kept outside the repository and
run with `uv run pytest` had no conftest, nothing was isolated, and two fake sessions
were written into the real cache while this module said nothing. So under pytest it
refuses unless this repository's own `tests/conftest.py` is loaded - or this process is a
subprocess of a run that had it, which inherits the conftest's token together with the
temporary HOME it set. Otherwise exactly as outside pytest: it refuses unless every
directory that matters points at a temporary one.

Import it first thing in a test module (or in a helper the test modules share) and the
refusal happens at the import itself, before any helper touches a directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")

# The module that isolates every test - `tests/conftest.py`, next to this file.
CONFTEST = Path(__file__).resolve().with_name("conftest.py")

# What `tests/conftest.py` leaves in the environment when it is imported, so a subprocess
# of an isolated run can tell where it comes from. Set there, read only here.
ISOLATED_TOKEN = "AGORA_TESTS_ISOLATED"


def _in_temp(value: str | None) -> bool:
    """Whether this setting points inside a temporary directory."""
    if not value:
        return False
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return any(path == Path(root) or path.is_relative_to(root) for root in TEMP_ROOTS)


def _repo_conftest_loaded() -> bool:
    """Whether *this* process imported this repository's `tests/conftest.py`.

    Looked up in `sys.modules` by the file each module came from, rather than by a marker
    the conftest would leave here: what isolates a test is that conftest's `isolated_home`
    fixture, and it only runs while a test runs, so at import time the environment says
    nothing about it - and a marker could have been set, or left, by anything. Which module
    got imported is the one fact nobody can fake by accident. The module *name* is not
    compared, only the file: pytest imports the conftest as `conftest`, as `tests.conftest`
    or under another name depending on the import mode, and `sys.modules` holds a module
    with its `__file__` already set while its body runs - which is what makes this true for
    the conftest's own `import _guard` too. What an *ancestor* process had is a different
    question, answered by `ISOLATED_TOKEN` in `_check()`.
    """
    for module in tuple(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            if Path(origin).resolve() == CONFTEST:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _check() -> None:
    # pytest sets this; a plain interpreter does not. Under pytest only this repository's
    # conftest isolates anything, and pytest loads it for test files under `tests/` only -
    # a test file kept anywhere else runs unisolated with this module silent. So under
    # pytest the question is not "is this pytest?" but "did the conftest come with it?".
    if os.environ.get("PYTEST_VERSION"):
        # Two ways to be under an isolated run, and only two: this process imported the
        # conftest that isolates every test, or a process that had it started this one. A
        # subprocess inherits the conftest's token along with the temporary HOME it set, so
        # it is isolated as well - refusing it would only teach people to drop the
        # isolation instead (a test's own probe subprocess needs it). `PYTEST_VERSION`
        # proves nothing on its own: pytest hands it down to every child it starts, so a
        # pytest run of a file outside the repository looks the same from in here.
        if _repo_conftest_loaded() or os.environ.get(ISOLATED_TOKEN):
            return
        # Same rule as below, and the same discipline about the message: the reason only,
        # no path, no title, nothing from a session.
        raise SystemExit("[tests] 測試的 helper 只能在隔離的環境用："
                         "這次 pytest 沒有載入 repo 的 conftest，沒有隔離。"
                         "請把測試檔放在 repo 的 tests 底下（uv run pytest）。")
    loose = [name for name in ("AGORA_CACHE_DIR", "AGORA_STATE_DIR", "AGORA_CONFIG", "HOME")
             if not _in_temp(os.environ.get(name))]
    if loose:
        # The names only - never a path, a title or anything from a session.
        raise SystemExit("[tests] 測試的 helper 只能在隔離的環境用："
                         f"{'、'.join(loose)} 沒指到暫存目錄。"
                         "請用 pytest（uv run pytest），或先把這些環境變數指到 /tmp 底下。")


_check()
