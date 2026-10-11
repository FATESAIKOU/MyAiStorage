"""Angareion message polling and delivery loop (Task 3.3, Design D3, D8)."""

from __future__ import annotations

import logging
import time
from typing import Any

from angareion.backend import Backend
from angareion.messages import Message
from sidecar.config import SidecarConfig
from sidecar.host import AgentHost
from sidecar.queue import MessageQueue
from sidecar.registry import Registry
from sidecar.seen import SeenStore

logger = logging.getLogger(__name__)


def _match_identity(to_addr: Any, identities: list[str]) -> str | None:
    if isinstance(to_addr, str):
        return to_addr if to_addr in identities else None
    if isinstance(to_addr, dict):
        for key in ("role", "name", "group"):
            val = to_addr.get(key)
            if val and val in identities:
                return val
    return None


class Receiver:
    """Polls Angareion messages with ETag/304, queues, delivers by urgency, and acks."""

    def __init__(
        self,
        backend: Backend,
        config: SidecarConfig,
        registry: Registry,
        queue: MessageQueue,
        host: AgentHost,
    ) -> None:
        self.backend = backend
        self.config = config
        self.registry = registry
        self.queue = queue
        self.host = host
        self.seen = SeenStore(config.state_dir)
        self.last_etag: str | None = None
        self.last_since: str | None = None

    def _ack_message(self, channel: str, msg_id: str, to_addr: Any, identity: str) -> None:
        ack_to = to_addr if isinstance(to_addr, dict) else {"repo": "pmo", "role": str(to_addr)}
        ack_msg = Message(
            from_={"repo": "sidecar", "role": identity},
            to=ack_to,
            channel=channel,
            ack=msg_id,
            content=f"ack {msg_id}",
        )
        self.backend.post_message(channel, ack_msg)

    def poll_once(self) -> int:
        """Execute one poll cycle.

        1. Fetches messages via backend with ETag/304 conditional query,
           filtered by last_since cursor when available.
        2. Filters out acks, duplicates (seen.json), and messages not targeting this sidecar.
        3. Enqueues new messages into persistent queue first, and only then
           marks them seen (M1: crashing between the two must not lose mail).
        4. Delivers to agent if eligible:
           - urgency >= 8: interrupts active turn or prompts immediately.
           - urgency < 8: if agent is currently running, prompts to queue in input;
             if not running, leaves in persistent queue for next hand-off.
        5. Acks on channel ONLY after successful delivery into agent input.
        """
        res = self.backend.list_messages(since=self.last_since, etag=self.last_etag)
        if getattr(res, "not_modified", False):
            return 0

        self.last_etag = res.etag
        messages = res.messages
        if not messages:
            return 0

        batch_max_at: str | None = None
        processed = 0
        for msg in messages:
            # Skip ack messages
            if msg.ack:
                continue

            # Deduplication: skip if already seen
            if self.seen.has(msg.id):
                continue

            target_identity = _match_identity(msg.to, self.config.identities)
            if not target_identity:
                continue

            msg_dict = {
                "id": msg.id,
                "from": msg.from_,
                "to": msg.to,
                "channel": msg.channel,
                "urgency": msg.urgency,
                "at": msg.at,
                "content": msg.content,
            }

            # 1. Enqueue to persistent queue first; mark seen only after the
            # queue write succeeded, so a crash in between leaves the message
            # to be redelivered (at-least-once) instead of lost.
            self.queue.enqueue(target_identity, msg_dict)
            self.seen.add(msg.id)
            processed += 1
            if msg.at and (batch_max_at is None or msg.at > batch_max_at):
                batch_max_at = msg.at

            # 2. Check for active agent
            active_agents = [a for a in self.registry.list_agents(status="running") if a.identity == target_identity]

            if msg.urgency >= 8:
                if active_agents:
                    agent = active_agents[0]
                    try:
                        if self.host.is_working(agent.agent_name):
                            self.host.interrupt_and_prompt(agent.agent_name, msg.content)
                        else:
                            self.host.send_prompt(agent.agent_name, msg.content)
                        # Delivery succeeded: remove from queue and ack
                        self.queue.remove(target_identity, msg.id)
                        self._ack_message(msg.channel, msg.id, msg.from_, target_identity)
                    except Exception as e:
                        logger.warning(
                            "High-urgency delivery failed for message %s to '%s': %s "
                            "(message stays queued)",
                            msg.id,
                            agent.agent_name,
                            e,
                        )
            else:
                # urgency < 8
                if active_agents:
                    agent = active_agents[0]
                    try:
                        # D3: directly prompt so it queues in agent input
                        self.host.send_prompt(agent.agent_name, msg.content)
                        self.queue.remove(target_identity, msg.id)
                        self._ack_message(msg.channel, msg.id, msg.from_, target_identity)
                    except Exception as e:
                        logger.warning(
                            "Low-urgency delivery failed for message %s to '%s': %s "
                            "(message stays queued)",
                            msg.id,
                            agent.agent_name,
                            e,
                        )
                else:
                    # Agent not currently running: remains in persistent queue for next hand-off
                    pass

        if batch_max_at is not None and (
            self.last_since is None or batch_max_at > self.last_since
        ):
            self.last_since = batch_max_at

        return processed

    def deliver_hand_off(
        self,
        identity: str,
        agent_name: str,
        wait_timeout_ms: int | None = 60000,
        ready_poll_interval: float = 0.5,
    ) -> int:
        """Deliver queued messages upon agent startup/hand-off in priority order.

        Waits for the agent to be ready first (M3/D10-3.12): on timeout
        nothing is sent and the messages stay queued.
        """
        messages = self.queue.peek(identity)
        if not messages:
            return 0

        rec = self.registry.get_by_name(agent_name)
        if rec is not None and wait_timeout_ms is not None:
            try:
                self.host.wait_for_ready(
                    agent_name,
                    rec.herdr_pane_id,
                    rec.agent_kind,
                    timeout_ms=wait_timeout_ms,
                    poll_interval=ready_poll_interval,
                )
            except Exception as e:
                logger.warning(
                    "Hand-off to '%s' deferred: agent not ready: %s",
                    agent_name,
                    e,
                )
                return 0

        delivered = 0
        for m in list(messages):
            msg_id = m["id"]
            content = m.get("content", "")
            channel = m.get("channel", "general")
            from_addr = m.get("from", {"repo": "pmo", "role": "PM"})

            try:
                self.host.send_prompt(agent_name, content)
                self.queue.remove(identity, msg_id)
                self._ack_message(channel, msg_id, from_addr, identity)
                delivered += 1
            except Exception as e:
                logger.warning(
                    "Hand-off delivery of message %s to '%s' failed: %s",
                    msg_id,
                    agent_name,
                    e,
                )
                break

        return delivered
