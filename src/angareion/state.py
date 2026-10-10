"""Local state cache for Angareion: since, etags, acked, and channels.

This state is purely a local cache: deleting it only causes Angareion to
rescan and rebuild state from the channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from angareion.config import get_state_dir
from angareion.messages import Message


def identity_slug(identity: dict[str, Any]) -> str:
    """Derive a filesystem-safe filename slug for an identity."""
    if "group" in identity:
        group = str(identity.get("group", "")).replace("/", "_").replace(":", "_")
        return f"group__{group}"
    repo = str(identity.get("repo", "unknown")).replace("/", "_").replace(":", "_")
    role = str(identity.get("role", "unknown")).replace("/", "_").replace(":", "_")
    return f"{repo}__{role}"


def state_file_path(state_dir: Path, identity: dict[str, Any]) -> Path:
    """Get the state JSON path for a given identity inside state_dir."""
    slug = identity_slug(identity)
    return state_dir / f"{slug}.json"


@dataclass
class LocalState:
    identity: dict[str, Any]
    since: str | None = None
    etags: dict[str, str] = field(default_factory=dict)
    acked: set[str] = field(default_factory=set)
    channels: dict[str, int] = field(default_factory=dict)
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "since": self.since,
            "etags": self.etags,
            "acked": sorted(list(self.acked)),
            "channels": self.channels,
        }

    def save(self, path: Path | None = None) -> None:
        """Save the state to JSON file."""
        target_path = path or self.path
        if not target_path:
            target_path = state_file_path(get_state_dir(), self.identity)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Write via temporary file for atomic update
        tmp_file = target_path.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp_file.replace(target_path)
        self.path = target_path

    @classmethod
    def load(cls, path: Path, identity: dict[str, Any] | None = None) -> LocalState:
        """Load state from file. If file does not exist, raises FileNotFoundError."""
        data = json.loads(path.read_text(encoding="utf-8"))
        id_data = identity or data.get("identity") or {}
        return cls(
            identity=id_data,
            since=data.get("since"),
            etags=data.get("etags", {}),
            acked=set(data.get("acked", [])),
            channels=data.get("channels", {}),
            path=path,
        )

    @classmethod
    def load_or_create(cls, state_dir: Path, identity: dict[str, Any]) -> LocalState:
        """Load existing state from state_dir, or create a fresh empty state."""
        target_path = state_file_path(state_dir, identity)
        if target_path.is_file():
            try:
                return cls.load(target_path, identity=identity)
            except Exception:
                pass
        return cls(identity=identity, path=target_path)

    def mark_acked(self, message_id: str) -> None:
        """Record a message as acked."""
        self.acked.add(message_id)

    def set_channel(self, channel_name: str, issue_number: int) -> None:
        """Record mapping between channel name and issue number."""
        self.channels[channel_name] = issue_number


def rebuild_from_backend(
    backend: Any,
    identity: dict[str, Any],
    state_dir: Path | None = None,
) -> LocalState:
    """Rebuild local state from the channel backend by scanning messages."""
    actual_state_dir = state_dir or get_state_dir()
    state = LocalState(
        identity=identity,
        path=state_file_path(actual_state_dir, identity),
    )

    # Call backend listing interface
    res = backend.list_messages(since=None)
    messages: list[Any]
    if isinstance(res, tuple) and len(res) == 2:
        messages, etag = res
        if etag:
            state.etags["messages"] = etag
    else:
        messages = res

    latest_at: str | None = None
    for item in messages:
        # Item might be a Message instance or a dict or an object with message attributes
        msg_channel: str | None = None
        msg_ack: str | None = None
        msg_at: str | None = None
        issue_number: int | None = None

        if isinstance(item, Message):
            msg_channel = item.channel
            msg_ack = item.ack
            msg_at = item.at
            issue_number = getattr(item, "issue_number", None)
        elif isinstance(item, dict):
            msg_channel = item.get("channel")
            msg_ack = item.get("ack")
            msg_at = item.get("at")
            issue_number = item.get("issue_number")
            issue_url = item.get("issue_url", "")
            if issue_number is None and issue_url:
                match = re.search(r"/issues/(\d+)", str(issue_url))
                if match:
                    issue_number = int(match.group(1))
        else:
            msg_channel = getattr(item, "channel", None)
            msg_ack = getattr(item, "ack", None)
            msg_at = getattr(item, "at", None)
            issue_number = getattr(item, "issue_number", None)
            issue_url = getattr(item, "issue_url", None)
            if issue_number is None and issue_url:
                match = re.search(r"/issues/(\d+)", str(issue_url))
                if match:
                    issue_number = int(match.group(1))

        if msg_channel and issue_number is not None:
            state.set_channel(msg_channel, issue_number)

        if msg_ack:
            state.mark_acked(msg_ack)

        if msg_at:
            if latest_at is None or msg_at > latest_at:
                latest_at = msg_at

    # If backend has channel lookup capability, query channels if missing
    if hasattr(backend, "list_channels"):
        try:
            for ch_name, issue_num in backend.list_channels():
                state.set_channel(ch_name, issue_num)
        except Exception:
            pass

    state.since = latest_at
    state.save()
    return state
