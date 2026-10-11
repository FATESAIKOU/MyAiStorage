"""Resident sidecar daemon loop (D10, D-run, 3.12).

Joins the long-running duties into one process (the unit's ExecStart target):
1. inbound polling: Receiver.poll_once over every configured identity
   (Angareion ETag/since throttling is honored by the backend);
2. orchestration (3.12/D10): mail for an identity with no running agent
   starts a team (at most one agent per identity); running agents are fed
   by the D3 delivery paths;
3. report relay: RelayServer on the unix socket (agent -> Angareion);
4. recovery + heartbeat: periodically reconcile the registry against herdr
   (recover_agents) and refresh running agents' updated_at.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Callable

from angareion.backend import BackendError, RateLimitError

logger = logging.getLogger(__name__)

# A single backoff never stalls heartbeat/recover longer than this; if it is
# not enough, the next cycle backs off again (D10, 3.12).
MAX_BACKOFF_SECONDS = 300.0


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
    team_fork_timeout_ms: int | None = 60000,
) -> int:
    """Run poll + orchestrate + relay + recover cycles until interrupted.

    max_cycles bounds the loop for tests and --once runs; None loops forever.
    Backend errors never kill the daemon: rate limits back off (capped),
    other backend errors are logged and retried next cycle. SIGTERM in the
    main thread requests a clean stop (relay closed, own socket unlinked).
    """
    stop_requested = threading.Event()

    def _handle_sigterm(signum: int, frame: Any) -> None:
        logger.warning("Received SIGTERM; stopping cleanly")
        stop_requested.set()

    old_handler: Any = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, _handle_sigterm)

    relay_server.start()
    try:
        cycles = 0
        last_recover = 0.0
        while not stop_requested.is_set():
            try:
                receiver.poll_once()
            except RateLimitError as e:
                backoff = min(float(e.retry_after), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Angareion rate limit hit; backing off %ss: %s",
                    backoff,
                    e,
                )
                sleeper(backoff)
            except BackendError as e:
                logger.warning("Angareion poll failed, retrying next cycle: %s", e)
            except Exception as e:
                logger.warning("Unexpected poll error, retrying next cycle: %s", e)

            _start_teams_for_queued_mail(receiver, registry, team_fork_timeout_ms)

            now = clock()
            if now - last_recover >= recover_interval:
                try:
                    receiver.host.recover_agents()
                except Exception as e:
                    logger.warning("Recover pass failed, retrying next time: %s", e)
                for rec in _safe_running_agents(registry):
                    try:
                        registry.touch(rec.agent_id)
                    except Exception as e:
                        logger.warning(
                            "Heartbeat write failed for agent '%s': %s",
                            rec.agent_id,
                            e,
                        )
                last_recover = now

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            sleeper(poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        relay_server.stop()
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)
    return 0


def _safe_running_agents(registry: Any) -> list[Any]:
    try:
        return registry.list_agents(status="running")
    except Exception as e:
        logger.warning("Could not list running agents for heartbeat: %s", e)
        return []


def _start_teams_for_queued_mail(
    receiver: Any, registry: Any, fork_timeout_ms: int | None = 60000
) -> None:
    """Start one team per identity that has queued mail but no live attempt.

    At most one agent per identity: identities with a running (or starting)
    record are left alone; a failed start is logged and retried only after
    an operator clears the record, never by spawning another pane.
    """
    try:
        live_identities = {
            rec.identity
            for rec in registry.list_agents()
            if rec.status in ("running", "starting")
        }
    except Exception as e:
        logger.warning("Orchestration pass skipped (registry unreadable): %s", e)
        return

    for identity in receiver.config.identities:
        if identity in live_identities:
            continue
        try:
            queued = receiver.queue.peek(identity)
        except Exception as e:
            logger.warning("Queue unreadable for '%s', skipping: %s", identity, e)
            continue
        if not queued:
            continue
        trigger_id = queued[0].get("id")
        try:
            receiver.host.start_team(
                identity,
                receiver=receiver,
                trigger_message_id=trigger_id,
                fork_timeout_ms=fork_timeout_ms,
            )
        except Exception as e:
            logger.warning(
                "Could not start team '%s' for queued mail: %s", identity, e
            )
        live_identities.add(identity)
