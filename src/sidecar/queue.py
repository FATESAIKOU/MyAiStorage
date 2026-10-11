"""Persistent priority message queue for sidecar identities."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.parse

logger = logging.getLogger(__name__)


def _encode_identity(identity: str) -> str:
    return urllib.parse.quote(identity, safe="")


def _decode_identity(stem: str) -> str:
    return urllib.parse.unquote(stem)


def _sort_key(msg: dict[str, Any]) -> tuple[int, str]:
    # Higher urgency first (negative urgency for ascending sort), then earlier timestamp first
    urgency = int(msg.get("urgency", 5))
    at = str(msg.get("at", ""))
    return (-urgency, at)


class MessageQueue:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.queue_dir = self.state_dir / "queue"

    def _queue_file(self, identity: str) -> Path:
        safe_name = _encode_identity(identity)
        return self.queue_dir / f"{safe_name}.json"

    def _load_raw(self, identity: str) -> list[dict[str, Any]]:
        qfile = self._queue_file(identity)
        if not qfile.is_file():
            return []
        try:
            with open(qfile, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "messages" in data:
                return list(data["messages"])
            raise ValueError(f"Invalid queue structure in {qfile}")
        except Exception as e:
            logger.warning("Corrupt queue file detected at %s: %s", qfile, e)
            try:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                corrupt_path = qfile.with_name(f"{qfile.name}.corrupt-{timestamp}")
                qfile.rename(corrupt_path)
            except Exception as re_err:
                logger.error("Failed to rename corrupt queue file %s: %s", qfile, re_err)
            return []

    def _save_raw(self, identity: str, messages: list[dict[str, Any]]) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _encode_identity(identity)
        qfile = self.queue_dir / f"{safe_name}.json"
        tmp_file = self.queue_dir / f".tmp-{safe_name}.json"
        payload = {"identity": identity, "messages": messages}
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, qfile)

    def enqueue(self, identity: str, message: dict[str, Any]) -> bool:
        """Enqueue a message. If message ID already exists, do not duplicate and return False."""
        msg_id = message.get("id")
        messages = self._load_raw(identity)
        if msg_id and any(m.get("id") == msg_id for m in messages):
            return False

        messages.append(message)
        messages.sort(key=_sort_key)
        self._save_raw(identity, messages)
        return True

    def peek(self, identity: str) -> list[dict[str, Any]]:
        """Return queued messages sorted by priority without removing them."""
        messages = self._load_raw(identity)
        messages.sort(key=_sort_key)
        return messages

    def pop(self, identity: str, message_id: str | None = None) -> dict[str, Any] | None:
        """Pop and return the highest priority message, or a specific message by ID."""
        messages = self._load_raw(identity)
        if not messages:
            return None

        messages.sort(key=_sort_key)
        if message_id is None:
            chosen = messages.pop(0)
            self._save_raw(identity, messages)
            return chosen

        for i, m in enumerate(messages):
            if m.get("id") == message_id:
                chosen = messages.pop(i)
                self._save_raw(identity, messages)
                return chosen
        return None

    def remove(self, identity: str, message_id: str) -> bool:
        """Remove a message by its ID."""
        messages = self._load_raw(identity)
        initial_len = len(messages)
        messages = [m for m in messages if m.get("id") != message_id]
        if len(messages) < initial_len:
            self._save_raw(identity, messages)
            return True
        return False

    def count(self, identity: str) -> int:
        """Return the number of messages queued for this identity."""
        return len(self._load_raw(identity))

    def list_identities(self) -> list[str]:
        """List all identities that currently have queues."""
        if not self.queue_dir.is_dir():
            return []
        res = []
        for p in self.queue_dir.glob("*.json"):
            if not p.name.startswith(".") and ".corrupt-" not in p.name:
                res.append(_decode_identity(p.stem))
        return sorted(res)
