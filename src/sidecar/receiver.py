"""Angareion message polling and delivery loop (Task 3.3, Design D3, D8)."""

from __future__ import annotations

import time
from typing import Any

from angareion.backend import Backend
from angareion.messages import Message
from sidecar.config import SidecarConfig
from sidecar.host import AgentHost
from sidecar.queue import MessageQueue
from sidecar.registry import Registry
from sidecar.seen import SeenStore


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

        1. Fetches messages via backend with ETag/304 conditional query.
        2. Filters out acks, duplicates (seen.json), and messages not targeting this sidecar.
        3. Enqueues new messages into persistent queue first.
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

            # Mark as seen so we don't duplicate
            self.seen.add(msg.id)

            msg_dict = {
                "id": msg.id,
                "from": msg.from_,
                "to": msg.to,
                "channel": msg.channel,
                "urgency": msg.urgency,
                "at": msg.at,
                "content": msg.content,
            }

            # 1. Enqueue to persistent queue first
            self.queue.enqueue(target_identity, msg_dict)
            processed += 1

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
                    except Exception:
                        pass
            else:
                # urgency < 8
                if active_agents:
                    agent = active_agents[0]
                    try:
                        # D3: directly prompt so it queues in agent input
                        self.host.send_prompt(agent.agent_name, msg.content)
                        self.queue.remove(target_identity, msg.id)
                        self._ack_message(msg.channel, msg.id, msg.from_, target_identity)
                    except Exception:
                        pass
                else:
                    # Agent not currently running: remains in persistent queue for next hand-off
                    pass

        return processed

    def deliver_hand_off(self, identity: str, agent_name: str) -> int:
        """Deliver queued messages upon agent startup/hand-off in priority order."""
        messages = self.queue.peek(identity)
        if not messages:
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
            except Exception:
                break

        return delivered
