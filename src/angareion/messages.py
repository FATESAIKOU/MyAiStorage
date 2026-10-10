"""Six-field message model, front matter serialization, parsing, and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
import time
from typing import Any

import yaml

MAX_MESSAGE_LENGTH = 65536
A2A_VERSION = 1

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    """Generate a 26-character ULID using Crockford base32."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def now_iso() -> str:
    """Current UTC timestamp in ISO 8601 format with timezone offset."""
    return datetime.now(timezone.utc).isoformat()


class MessageValidationError(ValueError):
    """Raised when message fields, address, attachments, or length are invalid."""


def is_group_address(address: Any) -> bool:
    """Check if address is a group address shape: {group: ...}."""
    return isinstance(address, dict) and "group" in address


def validate_address(address: Any, *, field_name: str = "address") -> None:
    """Validate member address {repo, role, [name]} or group address {group}."""
    if not isinstance(address, dict):
        raise MessageValidationError(f"{field_name} must be a dictionary, got {type(address).__name__}")

    if "group" in address:
        group_val = address["group"]
        if not isinstance(group_val, str) or not group_val.strip():
            raise MessageValidationError(f"{field_name}.group must be a non-empty string")
        extra = set(address.keys()) - {"group"}
        if extra:
            raise MessageValidationError(f"Group {field_name} must not contain extra keys: {sorted(extra)}")
        return

    # Member address
    if "repo" not in address or "role" not in address:
        raise MessageValidationError(f"{field_name} requires 'repo' and 'role'")

    repo_val = address["repo"]
    if not isinstance(repo_val, str) or not repo_val.strip():
        raise MessageValidationError(f"{field_name}.repo must be a non-empty string")

    role_val = address["role"]
    if not isinstance(role_val, str) or not role_val.strip():
        raise MessageValidationError(f"{field_name}.role must be a non-empty string")

    allowed = {"repo", "role", "name"}
    extra = set(address.keys()) - allowed
    if extra:
        raise MessageValidationError(f"{field_name} has unknown keys: {sorted(extra)}")

    if "name" in address and address["name"] is not None and not isinstance(address["name"], str):
        raise MessageValidationError(f"{field_name}.name must be a string if provided")


def validate_attachment(attachment: Any, index: int = 0) -> None:
    """Validate attachment item: name + exactly one of ref or inline."""
    if not isinstance(attachment, dict):
        raise MessageValidationError(f"Attachment [{index}] must be a dictionary")

    if "name" not in attachment or not isinstance(attachment["name"], str) or not attachment["name"].strip():
        raise MessageValidationError(f"Attachment [{index}] requires non-empty string 'name'")

    has_ref = "ref" in attachment
    has_inline = "inline" in attachment

    if has_ref and has_inline:
        raise MessageValidationError(f"Attachment [{index}] must have exactly one of 'ref' or 'inline', got both")
    if not has_ref and not has_inline:
        raise MessageValidationError(f"Attachment [{index}] must have exactly one of 'ref' or 'inline', got neither")

    if has_ref:
        if not isinstance(attachment["ref"], str) or not attachment["ref"].strip():
            raise MessageValidationError(f"Attachment [{index}].ref must be a non-empty string")
    if has_inline:
        if not isinstance(attachment["inline"], str):
            raise MessageValidationError(f"Attachment [{index}].inline must be a string")

    allowed = {"name", "ref", "inline"}
    extra = set(attachment.keys()) - allowed
    if extra:
        raise MessageValidationError(f"Attachment [{index}] has unknown keys: {sorted(extra)}")


@dataclass
class Message:
    from_: dict[str, Any]
    to: dict[str, Any]
    channel: str
    urgency: int = 5
    content: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=new_ulid)
    at: str = field(default_factory=now_iso)
    a2a: int = A2A_VERSION
    ack: str | None = None

    def validate(self) -> None:
        """Validate all six fields and metadata."""
        if self.a2a != A2A_VERSION:
            raise MessageValidationError(f"Unsupported a2a version: {self.a2a} (expected {A2A_VERSION})")
        if not isinstance(self.id, str) or not self.id.strip():
            raise MessageValidationError("Message id must be a non-empty ULID string")
        if not isinstance(self.at, str) or not self.at.strip():
            raise MessageValidationError("Message at must be a non-empty ISO 8601 string")

        validate_address(self.from_, field_name="from")
        if is_group_address(self.from_):
            raise MessageValidationError("'from' cannot be a group address")

        validate_address(self.to, field_name="to")

        if not isinstance(self.channel, str) or not self.channel.strip():
            raise MessageValidationError("channel must be a non-empty string")

        if not isinstance(self.urgency, int) or isinstance(self.urgency, bool):
            raise MessageValidationError("urgency must be an integer between 0 and 9")
        if not (0 <= self.urgency <= 9):
            raise MessageValidationError(f"urgency must be between 0 and 9, got {self.urgency}")

        if not isinstance(self.content, str):
            raise MessageValidationError("content must be a string")

        if not isinstance(self.attachments, list):
            raise MessageValidationError("attachments must be a list")
        for i, att in enumerate(self.attachments):
            validate_attachment(att, index=i)

        if self.ack is not None:
            if not isinstance(self.ack, str) or not self.ack.strip():
                raise MessageValidationError("ack must be a non-empty string when provided")


def split_front_matter(text: str) -> tuple[str, str]:
    """Split text into raw front matter YAML and content.

    Only the first closing `---` line is considered the boundary.
    Any `---` in content is preserved.
    """
    if not text.startswith("---"):
        raise MessageValidationError("Message does not start with front matter delimiter '---'")

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise MessageValidationError("Message first line is not '---'")

    closing_index = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing_index = idx
            break

    if closing_index == -1:
        raise MessageValidationError("Closing front matter delimiter '---' not found")

    raw_yaml = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1:])
    return raw_yaml, body


def serialize_message(msg: Message) -> str:
    """Serialize a message into YAML front matter + Markdown content."""
    msg.validate()

    fm: dict[str, Any] = {
        "a2a": msg.a2a,
        "id": msg.id,
        "at": msg.at,
        "from": msg.from_,
        "to": msg.to,
        "channel": msg.channel,
        "urgency": msg.urgency,
        "attachments": msg.attachments,
    }
    if msg.ack is not None:
        fm["ack"] = msg.ack

    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    text = f"---\n{fm_yaml}---\n{msg.content}"

    if len(text) > MAX_MESSAGE_LENGTH:
        raise MessageValidationError(
            f"Message length ({len(text)} chars) exceeds maximum limit of {MAX_MESSAGE_LENGTH} chars"
        )
    return text


def parse_message(text: str, *, strict: bool = False) -> Message | None:
    """Parse a comment/text into a Message object.

    If strict is False, returns None when text is not a valid a2a message
    (ignoring non-messages as required by spec).
    If strict is True, raises MessageValidationError on errors.
    """
    try:
        raw_yaml, body = split_front_matter(text)
        try:
            data = yaml.safe_load(raw_yaml)
        except Exception as e:
            raise MessageValidationError(f"Invalid YAML in front matter: {e}") from e

        if not isinstance(data, dict):
            raise MessageValidationError("Front matter is not a mapping")

        required = {"a2a", "id", "at", "from", "to", "channel", "urgency", "attachments"}
        missing = required - set(data.keys())
        if missing:
            raise MessageValidationError(f"Missing required front matter fields: {sorted(missing)}")

        msg = Message(
            a2a=data["a2a"],
            id=data["id"],
            at=data["at"],
            from_=data["from"],
            to=data["to"],
            channel=data["channel"],
            urgency=data["urgency"],
            attachments=data["attachments"],
            content=body,
            ack=data.get("ack"),
        )
        msg.validate()
        return msg
    except MessageValidationError:
        if strict:
            raise
        return None
    except Exception as e:
        if strict:
            raise MessageValidationError(f"Failed to parse message: {e}") from e
        return None
