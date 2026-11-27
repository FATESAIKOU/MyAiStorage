"""The interactive mode's background sync, as a child process (#491).

Started as `[sys.executable, "-m", "agora.sync_child"]` by `tui_sync`, never through
`cli.main()`: it runs one throttled `store.sync` and says how it went on the last line.
"""

from __future__ import annotations

import json
import sys

from agora import store

RESULT = "AGORA-SYNC "          # last stdout line of a sync that returned: RESULT + JSON
ERROR = "AGORA-SYNC-ERROR "     # last stdout line of a sync that raised: ERROR + one line


def _line(msg: str) -> None:
    store.say_out(f"[agora] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    # argv is not used (kept for later)
    paths = store.Paths.from_env()
    offline = False

    def warn(msg: str) -> None:
        nonlocal offline
        offline = offline or msg.startswith(store.OFFLINE_NOTE)
        _line(msg)

    try:
        with store.terminal_may_be_gone():
            store.sync(paths, throttle=True, warn=warn)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        text = f"{type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")
        store.say_out(ERROR + text[:300], flush=True)
        return 2
    store.say_out(RESULT + json.dumps({"offline": offline}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
