# Function body of _format_message, copied verbatim from main 0577584.
# Get it with: git show 0577584:src/angareion/cli.py
# Do not edit: this is the old reader under test; see tests/unit/test_angareion_datatype_compat.py.
"""Verbatim copy of _format_message at main 0577584 (#468, C-24).

Frozen on purpose: the old reader is the thing under test. Do not edit; see test_angareion_datatype_compat.py."""

from __future__ import annotations


def _format_message(message: Message) -> str:
    header = f"[{message.urgency}] {message.id} {message.at}"
    received_at = getattr(message, "received_at", None)
    if received_at:
        header += f" (received {received_at})"
    header += f" <{message.from_}> channel: {message.channel}"
    lines = [header]
    if message.mentions:
        lines.append("  mentions: " + ", ".join(message.mentions))
    if message.in_reply_to:
        lines.append(f"  in_reply_to: {message.in_reply_to}")
    body = message.content.rstrip("\n")
    if body:
        lines.extend("  " + line for line in body.splitlines())
    if message.attachments:
        kinds = [f"{att.get('name')} ({'ref' if 'ref' in att else 'inline'})" for att in message.attachments]
        lines.append("  attachments: " + ", ".join(kinds))
    return "\n".join(lines) + "\n"

