"""The tests' helpers must only run in an isolated environment (T8).

`tests/conftest.py` isolates every test: a fresh HOME, and agora's config, cache and
state in a temporary directory. That isolation only exists *inside* pytest. Importing a
test module directly - `python -c "import test_tui"`, or a probe that copies a helper
out - runs its helpers against the user's real `~/.cache/agora` and
`~/.local/state/agora`: three fake sessions ended up in the real mirror that way, and on
10-04 a diagnosis printed real sessions' ids and titles into an external model's
conversation.

So this module refuses to be imported outside pytest unless every directory that
matters points at a temporary one. Import it first thing in a test module (or in a
helper the test modules share) and the refusal happens at the import itself.
"""

from __future__ import annotations

import os
from pathlib import Path

TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


def _in_temp(value: str | None) -> bool:
    """Whether this setting points inside a temporary directory."""
    if not value:
        return False
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return any(path == Path(root) or path.is_relative_to(root) for root in TEMP_ROOTS)


def _check() -> None:
    # pytest sets this; a plain interpreter does not. Under pytest the conftest has
    # already isolated everything, and this module has nothing to say.
    if os.environ.get("PYTEST_VERSION"):
        return
    loose = [name for name in ("AGORA_CACHE_DIR", "AGORA_STATE_DIR", "AGORA_CONFIG", "HOME")
             if not _in_temp(os.environ.get(name))]
    if loose:
        # The names only - never a path, a title or anything from a session.
        raise SystemExit("[tests] 測試的 helper 只能在隔離的環境用："
                         f"{'、'.join(loose)} 沒指到暫存目錄。"
                         "請用 pytest（uv run pytest），或先把這些環境變數指到 /tmp 底下。")


_check()
