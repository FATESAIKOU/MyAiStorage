"""Six-field message model, front matter serialization, parsing, and validation (D3/v5).

v5 speaks AI names, not GitHub shapes: `from`, `to` and the personal identity
are strings like `mylinuxpool-impl-w1-1` or `mypmo-pm-m1`. The grammar is
`ainame`'s (see `src/ainame`), and it is applied two ways:

- **sending is strict** (`validate_address`, the default): only an AI name of
  the current grammar may be sent, sent from, or used as the identity;
- **reading is lenient** (`validate_address(..., allow_legacy=True)`, what
  `parse_message` uses): a message is also accepted when its addresses follow
  the old grammar (`<repo>-(pm|arch|impl|test)[-<id>]`, case-sensitive), so
  messages written by older versions are still delivered instead of being
  silently skipped (#97).

`to` is optional: no `to` means the message went to the whole channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import os
import re
import time
from typing import Any

import yaml

from ainame import is_valid as is_ainame
from ainame import parse as parse_ainame

MAX_MESSAGE_LENGTH = 65536
ANGAREION_VERSION = 1

# The old (pre-#97) address shape: `<repo name>-(pm|arch|impl|test)[-<id>]`,
# case-sensitive, with at most one optional id segment. It is only used on the
# read side; see the module docstring. It is matched whole (`fullmatch`), not
# with `^…$`: reading is wide, but a trailing newline is still not a name.
LEGACY_AGENT_NAME_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?"
)
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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


@dataclass(frozen=True)
class AgentAddress:
    """An AI name taken apart: `mylinuxpool-impl-w1-1` -> project, role, team, seq.

    The fields are `ainame.Name`'s; this alias keeps the old name of the type
    working for callers that imported it (#97 renamed `repo`/`agent_id` to the
    grammar's `project`/`team`/`seq`).
    """

    project: str
    role: str
    team: str
    seq: int | None = None

    def __str__(self) -> str:
        if self.seq is None:
            return f"{self.project}-{self.role}-{self.team}"
        return f"{self.project}-{self.role}-{self.team}-{self.seq}"


def validate_address(
    address: Any,
    *,
    field_name: str = "address",
    allow_legacy: bool = False,
) -> None:
    """Check that an address is an AI name of the current grammar (#97).

    With `allow_legacy=True` an address of the old grammar is accepted too;
    that is the read side (`parse_message`), never the send side.
    """
    if not isinstance(address, str):
        raise MessageValidationError(
            f"{field_name} must be an AI name string, got {type(address).__name__}"
        )
    if is_ainame(address):
        return
    if allow_legacy and LEGACY_AGENT_NAME_RE.fullmatch(address):
        return
    try:
        parse_ainame(address)
        reason = "it is not a legal AI name"
    except ValueError as e:
        reason = str(e)
    raise MessageValidationError(
        f"{field_name} must be an AI name '<project>-<role>-<team>[-<seq>]' such as "
        f"'mylinuxpool-impl-w1-1' or 'mypmo-pm-m1', got {address!r}: {reason}"
    )


def parse_identity(name: str) -> AgentAddress:
    """Take an AI name apart: project, role, team, and the sequence number (#97).

    Validation and parsing are two steps, still: the grammar is checked here,
    and the parts come from `ainame.parse`. `seq` is None for `pm` and
    `secretary`, which name one seat on their own.
    """
    validate_address(name, field_name="identity")
    parsed = parse_ainame(name)
    return AgentAddress(
        project=parsed.project,
        role=parsed.role,
        team=parsed.team,
        seq=parsed.seq,
    )


def validate_channel_name(name: Any, *, field_name: str = "channel") -> None:
    """Channel names are non-empty ASCII letters, digits, '-' and '_' (design section 4)."""
    if not isinstance(name, str) or not CHANNEL_NAME_RE.match(name):
        raise MessageValidationError(
            f"{field_name} must be a non-empty ASCII name (letters, digits, '-', '_'), got {name!r}"
        )


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
    from_: str
    to: str | None
    channel: str
    urgency: int = 5
    content: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=new_ulid)
    at: str = field(default_factory=now_iso)
    angareion: int = ANGAREION_VERSION

    def validate(self, *, allow_legacy_addresses: bool = False) -> None:
        """Validate all fields and metadata.

        Addresses are held to the current grammar unless the caller is reading
        (`allow_legacy_addresses=True` from `parse_message`), which also accepts
        the old grammar so that messages written by older versions are still
        delivered (#97).
        """
        if self.angareion != ANGAREION_VERSION:
            raise MessageValidationError(
                f"Unsupported angareion version: {self.angareion} (expected {ANGAREION_VERSION})"
            )
        if not isinstance(self.id, str) or not self.id.strip():
            raise MessageValidationError("Message id must be a non-empty ULID string")
        if not isinstance(self.at, str) or not self.at.strip():
            raise MessageValidationError("Message at must be a non-empty ISO 8601 string")

        validate_address(self.from_, field_name="from", allow_legacy=allow_legacy_addresses)
        if self.to is not None:
            validate_address(self.to, field_name="to", allow_legacy=allow_legacy_addresses)

        validate_channel_name(self.channel)

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


def message_to_dict(message: Message) -> dict[str, Any]:
    """The message as a plain dictionary (for `read --json`)."""
    return {
        "id": message.id,
        "at": message.at,
        "from": message.from_,
        "to": message.to,
        "channel": message.channel,
        "urgency": message.urgency,
        "content": message.content,
        "attachments": message.attachments,
    }


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
    """Serialize a message into YAML front matter + Markdown content.

    A broadcast (no `to`) omits the `to` field entirely.
    """
    msg.validate()

    fm: dict[str, Any] = {
        "angareion": msg.angareion,
        "id": msg.id,
        "at": msg.at,
        "from": msg.from_,
    }
    if msg.to is not None:
        fm["to"] = msg.to
    fm["channel"] = msg.channel
    fm["urgency"] = msg.urgency
    fm["attachments"] = msg.attachments

    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    text = f"---\n{fm_yaml}---\n{msg.content}"

    if len(text) > MAX_MESSAGE_LENGTH:
        raise MessageValidationError(
            f"Message length ({len(text)} chars) exceeds maximum limit of {MAX_MESSAGE_LENGTH} chars"
        )
    return text


def parse_message(text: str, *, strict: bool = False) -> Message | None:
    """Parse a comment/text into a Message object.

    If strict is False, returns None when text is not a valid angareion message
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

        required = {"angareion", "id", "at", "from", "channel", "urgency", "attachments"}
        missing = required - set(data.keys())
        if missing:
            raise MessageValidationError(f"Missing required front matter fields: {sorted(missing)}")

        at = data["at"]
        # L7: YAML parses an unquoted timestamp into datetime/date; turn it
        # back into an ISO 8601 string instead of rejecting a valid message.
        if isinstance(at, datetime):
            at = at.isoformat()
        elif isinstance(at, date):
            at = at.isoformat()

        msg = Message(
            angareion=data["angareion"],
            id=data["id"],
            at=at,
            from_=data["from"],
            to=data.get("to"),
            channel=data["channel"],
            urgency=data["urgency"],
            attachments=data["attachments"],
            content=body,
        )
        msg.validate(allow_legacy_addresses=True)
        return msg
    except MessageValidationError:
        if strict:
            raise
        return None
    except Exception as e:
        if strict:
            raise MessageValidationError(f"Failed to parse message: {e}") from e
        return None
