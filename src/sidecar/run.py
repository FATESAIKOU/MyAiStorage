"""Resident sidecar daemon loop (D10, D-run).

Joins the three long-running duties into one process (the systemd unit's
ExecStart target):
1. inbound polling: Receiver.poll_once over every configured identity
   (Angareion ETag/since throttling is honored by the backend);
2. report relay: RelayServer on the unix socket (agent -> Angareion);
3. recovery + heartbeat: periodically reconcile the registry against herdr
   (recover_agents) and refresh running agents' updated_at.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from angareion.backend import BackendError, RateLimitError

logger = logging.getLogger(__name__)


def run_loop(
    *,
    receiver: Any,
    registry: Any,
    relay_server: Any,
    poll_interval: float = 30.0,
    recover_interval: float = 300.0,
    max_cycles: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run poll + relay + recover cycles until interrupted.

    max_cycles bounds the loop for tests and --once runs; None loops forever.
    Backend errors never kill the daemon: rate limits back off for retry_after,
    other backend errors are logged and retried next cycle.
    """
    relay_server.start()
    try:
        cycles = 0
        last_recover = 0.0
        while True:
            try:
                receiver.poll_once()
            except RateLimitError as e:
                logger.warning(
                    "Angareion rate limit hit; backing off %ss: %s",
                    e.retry_after,
                    e,
                )
                sleeper(float(e.retry_after))
            except BackendError as e:
                logger.warning("Angareion poll failed, retrying next cycle: %s", e)
            except Exception as e:
                logger.warning("Unexpected poll error, retrying next cycle: %s", e)

            now = clock()
            if now - last_recover >= recover_interval:
                try:
                    receiver.host.recover_agents()
                except Exception as e:
                    logger.warning("Recover pass failed, retrying next time: %s", e)
                for rec in registry.list_agents(status="running"):
                    registry.touch(rec.agent_id)
                last_recover = now

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            sleeper(poll_interval)
    finally:
        relay_server.stop()
    return 0
