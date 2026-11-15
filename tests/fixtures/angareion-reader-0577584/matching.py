# Function bodies of recipient_matches and mention_matches, copied verbatim from main 0577584.
# Get them with: git show 0577584:src/angareion/backend.py
# Do not edit: this is the old reader under test; see tests/unit/test_angareion_datatype_compat.py.
"""Verbatim copy of recipient_matches and mention_matches at main 0577584 (#468, C-24).

Frozen on purpose: the old reader is the thing under test. Do not edit; see test_angareion_datatype_compat.py."""

from __future__ import annotations


def recipient_matches(message: Message, recipient: str | None) -> bool:
    """Whether a message counts as "to me": addressed to me, a broadcast, or it mentions me."""
    if recipient is None:
        return True
    return message.to is None or message.to == recipient or recipient in message.mentions


def mention_matches(message: Message, mentioned: str | None) -> bool:
    """Whether a message counts as "@ me": addressed to me or it mentions me; a broadcast does not."""
    if mentioned is None:
        return True
    return message.to == mentioned or mentioned in message.mentions

