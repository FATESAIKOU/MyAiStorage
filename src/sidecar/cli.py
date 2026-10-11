"""The sidecar command: `sidecar run` and `sidecar --version` (design D7/D8)."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from typing import Any, Sequence

from . import __version__
from .config import Config, load_config
from .http import make_server
from .protocols import SidecarError
from .wiring import build_core

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 1.0


def installed_sha() -> str | None:
    """The commit this install came from (PEP 610 direct_url.json), if known."""
    try:
        from importlib import metadata

        dist = metadata.distribution("agora")
        text = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        info = json.loads(text)
    except ValueError:
        return None
    vcs = info.get("vcs_info") or {}
    commit = vcs.get("commit_id")
    return commit or None


def package_version() -> str:
    try:
        from importlib import metadata

        return metadata.version("agora")
    except Exception:
        return __version__


def version_string() -> str:
    sha = installed_sha()
    if sha:
        return f"sidecar {sha}"
    return f"sidecar {package_version()} (install sha unknown)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidecar",
        description="agent 的生命週期控管 daemon（HTTP 只綁 127.0.0.1）",
    )
    parser.add_argument("--version", action="version", version=version_string())
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="start the HTTP daemon")
    run.add_argument(
        "--config",
        help="config path (default ~/.config/sidecar/config.json or SIDECAR_CONFIG)",
    )
    return parser


def run(config: Config, stop_event: threading.Event | None = None) -> int:
    core = build_core(config)
    server = make_server(core, port=config.port)
    stop = stop_event if stop_event is not None else threading.Event()
    if stop_event is None:
        def _on_signal(signum: Any, frame: Any) -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _on_signal)
            except ValueError:
                pass  # not the main thread (tests pass their own event)
    scheduler = threading.Thread(
        target=_scheduler_loop, args=(core, stop), name="sidecar-scheduler", daemon=True
    )
    scheduler.start()
    print(
        f"[sidecar] listening on http://127.0.0.1:{server.server_address[1]}",
        file=sys.stderr,
    )
    server.timeout = 0.2
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        stop.set()
        # Shutting down (D2/M5): refuse new work, wait for the scheduler's
        # current beat, then close every agent - only after all of them are
        # stopped does the process leave.
        core.begin_shutdown()
        scheduler.join()  # wait for the in-flight beat; every driver/lgw call is bounded
        server.server_close()
        try:
            core.shutdown()
        except Exception:
            logger.exception("shutdown failed")
    return 0


def _scheduler_loop(core: Any, stop: threading.Event, interval: float = SCHEDULER_INTERVAL) -> None:
    while not stop.wait(interval):
        try:
            core.tick()
        except Exception:
            logger.exception("scheduler tick failed")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help(sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        return run(config)
    except SidecarError as e:
        print(f"[sidecar] {e}", file=sys.stderr)
        return 1
    except OSError as e:  # e.g. the port is taken
        print(f"[sidecar] cannot listen: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
